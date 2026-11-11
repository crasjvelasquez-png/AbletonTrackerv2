#!/usr/bin/env python3
"""End-to-end Song Planner check for the dashboard: real HTTP server, copied database.

Failure coverage (written before the dashboard code):
- A bare database without song tables crashes GET /api/song-planner list.
- Search query fails to filter by title/key or is case-sensitive.
- GET /api/song-planner?key=... does not return 404 on an unknown key.
- Task add/edit/done/reopen/delete fails to round-trip or leaves open_tasks out of sync.
- Bad due dates, empty task text, wrong-key task IDs, unknown actions, unknown keys,
  or malformed JSON are accepted or return 500 instead of 400.
- Song due date or stage actions fail to persist in song detail and list.
- Any planner action mutates the source-of-truth sessions rows.
- The source database is modified.

The source database is copied with SQLite's backup API and opened read-only.
If the source database does not exist, a small synthetic source database is built.
Artifact: results.json in --output.
"""
import argparse
import hashlib
import json
from datetime import date, timedelta
from http.server import HTTPServer
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard
import songs

SOURCE = Path.home() / ".ableton_tracker" / "sessions.db"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_db(source: Path, target: Path) -> None:
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def build_synthetic_source(target: Path) -> None:
    now = time.time()
    conn = sqlite3.connect(target)
    with conn:
        conn.execute(
            """CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL,
                last_seen_time REAL,
                active_seconds REAL DEFAULT 0
            )"""
        )
        sample_rows = [
            ("April 2026 - Prince - 130 bpm Emaj", now - 3 * 86400, now - 3 * 86400 + 7200, now - 3 * 86400 + 7200, 7200.0),
            ("August 2026 - DANCE - 118 bpm", now - 4 * 86400, now - 4 * 86400 + 5400, now - 4 * 86400 + 5400, 5400.0),
            ("September 2026 - Colorful - 138 bpm - Mixing", now - 5 * 86400, now - 5 * 86400 + 3600, now - 5 * 86400 + 3600, 3600.0),
        ]
        conn.executemany(
            "INSERT INTO sessions(project_name, start_time, end_time, last_seen_time, active_seconds) VALUES(?, ?, ?, ?, ?)",
            sample_rows,
        )
    conn.close()


def sessions_digest(db: Path) -> str:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


class Client:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def act(self, **body):
        return self.call("POST", "/api/song-planner", body)


def serve(db: Path) -> HTTPServer:
    dashboard.DB_PATH = db
    with dashboard.db_connection() as conn:
        dashboard.run_schema_migrations(conn)
    server = HTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def verify(output: Path, source: Path, keep_serving: bool = False) -> int:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    checks = []

    def check(name: str, ok: bool, detail="") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1. A database with no song tables at all must still answer list.
    bare = output / "bare.db"
    conn = sqlite3.connect(bare)
    conn.execute(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY, project_name TEXT, start_time REAL, end_time REAL, last_seen_time REAL, active_seconds REAL)"
    )
    conn.commit()
    conn.close()
    dashboard.DB_PATH = bare
    bare_list = dashboard.get_song_planner_list()
    check(
        "bare database answers list",
        bare_list == {"query": "", "songs": []},
        bare_list,
    )

    if source.exists():
        effective_source = source
    else:
        effective_source = output / "synthetic-source.db"
        build_synthetic_source(effective_source)

    source_hash = digest(effective_source)
    db = output / "sessions-copy.db"
    copy_db(effective_source, db)
    server = serve(db)
    client = Client(server.server_address[1])
    before_sessions = sessions_digest(db)

    status, initial = client.call("GET", "/api/song-planner")
    all_items = initial.get("songs", [])
    check(
        "GET /api/song-planner returns 200 and songs list",
        status == 200 and initial.get("query") == "" and len(all_items) >= 2,
        len(all_items),
    )
    if len(all_items) < 2:
        return finish(output, checks, effective_source, source_hash, server)

    first = all_items[0]
    second = all_items[1]
    first_key = first["key"]
    second_key = second["key"]

    # 2. Search filters and is case-insensitive.
    token = first["title"].split()[0]
    upper_q = urllib.parse.quote(token.upper())
    lower_q = urllib.parse.quote(token.lower())
    st_up, res_up = client.call("GET", f"/api/song-planner?q={upper_q}")
    st_lo, res_lo = client.call("GET", f"/api/song-planner?q={lower_q}")
    st_none, res_none = client.call("GET", "/api/song-planner?q=__unlikely_song_query_999__")
    up_keys = [s["key"] for s in res_up.get("songs", [])]
    lo_keys = [s["key"] for s in res_lo.get("songs", [])]
    check(
        "search filters and is case-insensitive",
        st_up == 200
        and st_lo == 200
        and st_none == 200
        and first_key in up_keys
        and up_keys == lo_keys
        and res_none.get("songs") == [],
        {"up": len(up_keys), "lo": len(lo_keys), "none": len(res_none.get("songs", []))},
    )

    # 3. Detail 404 on unknown key.
    st_404, body_404 = client.call("GET", "/api/song-planner?key=__no_such_song__")
    check("detail returns 404 on unknown key", st_404 == 404 and "error" in body_404, st_404)

    # Detail 200 on valid key.
    encoded_first = urllib.parse.quote(first_key)
    st_detail, detail = client.call("GET", f"/api/song-planner?key={encoded_first}")
    check(
        "detail returns song and empty tasks initially",
        st_detail == 200 and detail.get("song", {}).get("key") == first_key and detail.get("tasks") == [],
        st_detail,
    )

    # 4. Add / edit / done / reopen / delete round-trip.
    due_soon = (date.today() + timedelta(days=5)).isoformat()
    due_later = (date.today() + timedelta(days=10)).isoformat()
    st_add1, body_add1 = client.act(action="task_add", key=first_key, text="  record   ad-libs  ", due_date=due_later)
    st_add2, body_add2 = client.act(action="task_add", key=first_key, text="tighten kick", due_date=due_soon)
    tasks_after_add = body_add2.get("tasks", [])
    check(
        "task_add normalizes text and orders by due_date ascending",
        st_add1 == 200
        and st_add2 == 200
        and body_add2.get("ok") is True
        and [t["text"] for t in tasks_after_add] == ["tighten kick", "record ad-libs"],
        [t["text"] for t in tasks_after_add],
    )
    task_id = tasks_after_add[0]["id"]

    _, list_with_counts = client.call("GET", f"/api/song-planner?q={urllib.parse.quote(first_key)}")
    matched_song = next((s for s in list_with_counts.get("songs", []) if s["key"] == first_key), {})
    check("open_tasks count reflects added tasks", matched_song.get("open_tasks") == 2, matched_song.get("open_tasks"))

    st_edit, body_edit = client.act(
        action="task_update",
        key=first_key,
        id=task_id,
        text="  tighten   kick + bass ",
        due_date=due_soon,
    )
    edited_task = next((t for t in body_edit.get("tasks", []) if t["id"] == task_id), {})
    check(
        "task_update edits text and due_date",
        st_edit == 200 and edited_task.get("text") == "tighten kick + bass" and edited_task.get("due_date") == due_soon,
        edited_task,
    )

    st_done, body_done = client.act(action="task_update", key=first_key, id=task_id, done=True)
    done_task = next((t for t in body_done.get("tasks", []) if t["id"] == task_id), {})
    check(
        "task_update done=True marks task done and moves it after open tasks",
        st_done == 200
        and done_task.get("done") is True
        and done_task.get("done_at") is not None
        and body_done["tasks"][-1]["id"] == task_id,
        done_task,
    )

    st_reopen, body_reopen = client.act(action="task_update", key=first_key, id=task_id, done=False)
    reopened_task = next((t for t in body_reopen.get("tasks", []) if t["id"] == task_id), {})
    check(
        "task_update done=False reopens task and restores order",
        st_reopen == 200
        and reopened_task.get("done") is False
        and reopened_task.get("done_at") is None
        and body_reopen["tasks"][0]["id"] == task_id,
        reopened_task,
    )

    st_del, body_del = client.act(action="task_delete", key=first_key, id=task_id)
    remaining_ids = [t["id"] for t in body_del.get("tasks", [])]
    check(
        "task_delete removes task",
        st_del == 200 and task_id not in remaining_ids and len(remaining_ids) == 1,
        remaining_ids,
    )

    # 5. Error handling: bad date, empty text, wrong-key task id, unknown action, invalid json all 400.
    other_task_id = remaining_ids[0]
    st_bad_date, _ = client.act(action="task_add", key=first_key, text="valid", due_date="2026-99-99")
    check("bad task due_date returns 400", st_bad_date == 400, st_bad_date)

    st_empty_text, _ = client.act(action="task_add", key=first_key, text="   ")
    check("empty task text returns 400", st_empty_text == 400, st_empty_text)

    st_wrong_key, _ = client.act(action="task_update", key=second_key, id=other_task_id, text="steal")
    st_wrong_key_del, _ = client.act(action="task_delete", key=second_key, id=other_task_id)
    check(
        "wrong-key task id returns 400",
        st_wrong_key == 400 and st_wrong_key_del == 400,
        (st_wrong_key, st_wrong_key_del),
    )

    st_unknown_act, _ = client.act(action="launch_rockets", key=first_key)
    check("unknown action returns 400", st_unknown_act == 400, st_unknown_act)

    st_unknown_key, _ = client.act(action="due", key="__no_such_song__", date=due_soon)
    check("unknown song key on POST returns 400", st_unknown_key == 400, st_unknown_key)

    st_bad_json, _ = client.call("POST", "/api/song-planner", raw=b"{not valid json")
    check("invalid JSON returns 400", st_bad_json == 400, st_bad_json)

    # 6. Due + stage actions persist.
    song_due = (date.today() + timedelta(days=14)).isoformat()
    st_due, body_due = client.act(action="due", key=first_key, date=song_due)
    st_stage, body_stage = client.act(action="stage", key=first_key, stage="mastering")
    _, detail_after = client.call("GET", f"/api/song-planner?key={encoded_first}")
    check(
        "due and stage actions persist",
        st_due == 200
        and st_stage == 200
        and detail_after.get("song", {}).get("due_value") == song_due
        and detail_after.get("song", {}).get("due_source") == "song"
        and detail_after.get("song", {}).get("stage") == "mastering",
        detail_after.get("song"),
    )

    # 7. Sessions rows untouched.
    check("sessions rows untouched", sessions_digest(db) == before_sessions)

    if keep_serving:
        print(f"Serving copied database at {client.base} (Ctrl+C to stop)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    return finish(output, checks, effective_source, source_hash, server)


def finish(output: Path, checks: list, source: Path, source_hash: str, server: HTTPServer) -> int:
    server.shutdown()
    checks.append({"name": "source database unchanged", "ok": digest(source) == source_hash, "detail": ""})
    passed = all(c["ok"] for c in checks)
    (output / "results.json").write_text(json.dumps({"passed": passed, "checks": checks}, indent=2))
    for c in checks:
        print(
            f"{'PASS' if c['ok'] else 'FAIL'}  {c['name']}"
            + (f"  ({c['detail']})" if not c["ok"] and c["detail"] != "" else "")
        )
    print(f"\n{'All checks passed' if passed else 'Some checks failed'}. Artifact: {output / 'results.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=Path("/tmp/song-planner-dashboard"))
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--serve", action="store_true", help="keep serving the copied database for UI checks")
    args = parser.parse_args()
    sys.exit(verify(args.output, args.source, keep_serving=args.serve))
