#!/usr/bin/env python3
"""End-to-end On Deck check for the dashboard: real HTTP server, copied database.

Failure coverage (written before the dashboard code): GET /api/on-deck crashes on
real history or on a database without song tables; Live dialog titles leak into
the picker; picking leaves zero or two songs On Deck; a swap drops the previous
song without the snooze the user chose; a stale client key edits the wrong song;
invalid due dates or unknown actions are accepted or return 500; clearing a due
date does not clear it; next-task text is not normalized or capped; completing a
task neither clears it nor records it; Released does not empty On Deck; Open
targets the wrong stage file or hides resolver errors; any action writes to the
sessions rows; the source database is modified.

The source database is copied with SQLite's backup API and opened read-only.
Open-in-Live is intercepted so the check never launches Live.
Artifact: results.json in --output (and the copied database, for UI checks).
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
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard
import songs

SOURCE = Path.home() / ".ableton_tracker" / "sessions.db"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_db(source, target):
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def sessions_digest(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def deck_rows(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT song_key FROM song_state WHERE on_deck = 1").fetchall()
    conn.close()
    return [r[0] for r in rows]


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def act(self, **body):
        return self.call("POST", "/api/on-deck", body)


def serve(db):
    dashboard.DB_PATH = db
    with dashboard.db_connection() as conn:
        dashboard.run_schema_migrations(conn)
    server = HTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def verify(output, source, keep_serving=False):
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # A database with no song tables at all must still answer.
    bare = output / "bare.db"
    conn = sqlite3.connect(bare)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, project_name TEXT, start_time REAL, end_time REAL, last_seen_time REAL, active_seconds REAL)")
    conn.commit()
    conn.close()
    dashboard.DB_PATH = bare
    model = dashboard.get_on_deck()
    check("bare database returns empty model", model["mode"] == "empty" and model["all"] == [], model["mode"])

    source_hash = digest(source)
    db = output / "sessions-copy.db"
    copy_db(source, db)
    server = serve(db)
    client = Client(server.server_address[1])
    opened = []
    dashboard.open_project = lambda path, app: opened.append(str(path))
    dashboard.resolve_project_path = lambda db_path, name: f"/sets/{name}.als"
    before_sessions = sessions_digest(db)

    status, model = client.call("GET", "/api/on-deck")
    check("GET returns 200", status == 200, status)
    names = [row[1] for row in model.get("all", [])] + [row[1] for row in model.get("suggestions", [])]
    noise = [n for n in names if songs.is_noise(n)]
    check("no dialog titles in picker", not noise, noise)
    check("track stages exclude released", [s for s, _ in model["track_stages"]] == list(songs.TRACK_STAGES))

    pickable = [row[0] for row in model.get("suggestions") or model.get("all") or []]
    if model["mode"] == "set":
        pickable = [model["key"]] + [row[0] for row in model.get("more") or model.get("all") or []]
    check("history yields at least two songs", len(pickable) >= 2, len(pickable))
    if len(pickable) < 2:
        return finish(output, checks, source, source_hash, server)
    first, second = pickable[0], pickable[1]

    status, body = client.act(action="pick", key=first)
    check("pick puts exactly one song On Deck", status == 200 and deck_rows(db) == [first], deck_rows(db))
    check("pick returns the set model", body.get("deck", {}).get("mode") == "set" and body["deck"]["key"] == first)
    check("pick does not open Live", opened == [], opened)

    status, body = client.act(action="pick", key=second, snooze_days=7)
    conn = sqlite3.connect(db)
    snoozed = conn.execute("SELECT snoozed_until FROM song_state WHERE song_key = ?", (first,)).fetchone()[0]
    conn.close()
    check("swap snoozes previous song for a week", deck_rows(db) == [second] and snoozed == (date.today() + timedelta(days=7)).isoformat(), snoozed)
    status, _ = client.act(action="pick", key=first, snooze_days=3)
    check("unsupported snooze length rejected", status == 400 and deck_rows(db) == [second], status)

    status, body = client.act(action="stage", key=first, stage="mixing")
    check("stale key rejected with 409", status == 409 and body.get("deck", {}).get("key") == second, status)

    status, _ = client.act(action="due", key=second, date="2026-13-40")
    check("invalid due date rejected", status == 400, status)
    target = (date.today() + timedelta(days=14)).isoformat()
    status, body = client.act(action="due", key=second, date=target)
    check("due date saved", status == 200 and body["deck"]["due_value"] == target, body.get("deck", {}).get("due_value"))
    status, body = client.act(action="due", key=second, date="")
    cleared = body.get("deck", {})
    check("due date cleared (older planner date may remain, marked legacy)", status == 200 and (
        (cleared["due_value"] == "" and cleared["due_primary"] == "No due date" and cleared["due_source"] == "")
        or (cleared["due_value"] and cleared["due_source"] == "legacy")), cleared.get("due_source"))

    long_text = "  bounce   the  vocal\tstems " + "x" * 400
    status, body = client.act(action="task", key=second, text=long_text)
    task = body.get("deck", {}).get("task", "")
    check("task normalized and capped", status == 200 and task.startswith("bounce the vocal stems") and len(task) == songs.MAX_TASK_LENGTH, len(task))
    status, body = client.act(action="task_done", key=second)
    conn = sqlite3.connect(db)
    done = conn.execute("SELECT value FROM song_events WHERE song_key = ? AND kind = 'task_done' ORDER BY id DESC LIMIT 1", (second,)).fetchone()
    conn.close()
    check("task completion clears and records", status == 200 and body["deck"]["task"] == "" and done and done[0] == task)

    status, body = client.act(action="stage", key=second, stage="mixing")
    check("stage raised", status == 200 and body["deck"]["stage"] in ("mixing", "mastering"), body.get("deck", {}).get("stage"))
    status, body = client.act(action="open", key=second)
    expected = songs.current_deck(songs.load_songs(db))["open_name"]
    check("open targets the stage file", status == 200 and opened == [f"/sets/{expected}.als"], opened)
    def missing(db_path, name):
        raise ValueError("Could not find the set. Reconnect the drive.")
    dashboard.resolve_project_path = missing
    status, body = client.act(action="open", key=second)
    check("open surfaces resolver errors", status == 400 and "Reconnect" in body.get("error", ""), body)

    status, _ = client.call("POST", "/api/on-deck", raw=b"{not json")
    check("malformed JSON rejected", status == 400, status)
    status, _ = client.act(action="explode", key=second)
    check("unknown action rejected", status == 400, status)
    status, _ = client.act(action="stage", key=second, stage="finished")
    check("unknown stage rejected", status == 400, status)

    status, body = client.act(action="snooze", key=second, days=7)
    check("snooze empties On Deck", status == 200 and deck_rows(db) == [] and body["deck"]["mode"] == "empty")
    client.act(action="pick", key=first)
    status, body = client.act(action="stage", key=first, stage="released")
    check("released empties On Deck", status == 200 and deck_rows(db) == [] and body["deck"]["mode"] == "empty")
    released_keys = [row[0] for row in body["deck"]["all"]]
    check("released song leaves the picker", first not in released_keys)

    check("sessions rows untouched", sessions_digest(db) == before_sessions)
    if keep_serving:
        client.act(action="pick", key=second)
        print(f"Serving copied database at {client.base} (Ctrl+C to stop)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    return finish(output, checks, source, source_hash, server)


def finish(output, checks, source, source_hash, server):
    server.shutdown()
    checks.append({"name": "source database unchanged", "ok": digest(source) == source_hash, "detail": ""})
    passed = all(c["ok"] for c in checks)
    (output / "results.json").write_text(json.dumps({"passed": passed, "checks": checks}, indent=2))
    for c in checks:
        print(f"{'PASS' if c['ok'] else 'FAIL'}  {c['name']}" + (f"  ({c['detail']})" if not c["ok"] and c["detail"] != "" else ""))
    print(f"\n{'All checks passed' if passed else 'Some checks failed'}. Artifact: {output / 'results.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=Path("/tmp/on-deck-dashboard"))
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--serve", action="store_true", help="keep serving the copied database for UI checks")
    args = parser.parse_args()
    sys.exit(verify(args.output, args.source, keep_serving=args.serve))
