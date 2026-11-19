#!/usr/bin/env python3
"""End-to-end Planner API check for the dashboard: real HTTP server, copied database.

Failure coverage (written before planner.py and the /api/planner routes):
- A bare database, or one with sessions but no planner tables, crashes GET /api/planner,
  or a GET creates tables / writes anything (the database content must be identical
  before and after every GET).
- The overview omits a project, misgroups it (active / on_hold / archive), or its
  counts disagree with the agenda and project rows.
- Hold: reason / back-on date / waiting_on are not stored; hold_since is reset when an
  already-held project is edited; a back-on date that is not in the future is accepted;
  waiting_on pointing at a missing contact is accepted; waiting_on does not auto-link the
  contact; an expired hold is not shown as active with hold_expired; resume leaves stale
  hold fields behind; an On Deck or Three Decks song loses its deck when put on hold.
- Held songs are still suggested by songs.suggestions, or an expired hold is not.
- Priority / notes / due / stage / list actions fail to persist, accept bad input, let a
  stage below the detected one through, or ignore Released settling a song into Archive.
- A legacy (older planner) due date is not reported as due_source "legacy".
- Auto-archive moves a project that has to-dos, notes, a star, a due date or a hold, or
  list_override does not beat / unbeat the automatic rule; settled songs leave Archive.
- To-dos: add / edit / done / reopen / delete do not round-trip; general (no song)
  to-dos are missing from the agenda; to-dos of settled or orphaned song keys are
  unreachable (agenda omits them, or update / delete reject them); agenda buckets
  (overdue / today / week / later / someday) or their ordering are wrong.
- Contacts: save / update / validation (empty name, over-long fields, unknown id) is lax;
  link accepts an unknown contact or song; unlink of an orphan key fails; unlinking the
  waiting_on contact leaves waiting_on set; deleting a contact leaves links or waiting_on
  behind (it must be one transaction).
- Any action returns 500 instead of 400 for bad types, bool ids, unknown actions or keys,
  malformed JSON, or non-object bodies; an unknown project / contact is not 404.
- plan events (plan_hold / plan_resume / plan_priority) are not written.
- "open" does not call the project opener with the resolved song path.
- Existing /api/song-planner, /api/on-deck and /api/lanes contracts change.
- Any planner action mutates the source-of-truth sessions rows, or the source database.

The source database is copied with SQLite's backup API and opened read-only; if it does
not exist a small synthetic one is built. Deterministic songs are appended to the copy.
Artifact: results.json in --output.

Usage:
  verify_planner_dashboard.py [--output DIR] [--source PATH]
  verify_planner_dashboard.py --serve PORT [--output DIR] [--synthetic]   (PORT 0 = any free port)
    Serves a copy seeded with the SEED_* data below for UI tests and runs until killed.
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
import planner
import songs

SOURCE = Path.home() / ".ableton_tracker" / "sessions.db"

# (title, days since last worked, hours worked). Keys are the casefolded song keys.
SEED_SONGS = (
    ("Planner Alpha - Zed", 2, 2.0),                   # production, starred, to-dos
    ("Planner Bravo - Zed", 3, 3.0),                   # on hold, waiting on Ana Ruiz
    ("Planner Charlie - Zed", 4, 1.5),                 # plain active, no to-dos
    ("Planner Delta - Zed - Mixing", 1, 2.0),          # key "planner delta - zed", mixing, On Deck
    ("Planner Echo - Zed", 100, 2.0),                  # long untouched -> auto Archive
    ("Planner Foxtrot - Zed", 30, 2.0),                # stale (>=21d) but still Active
)
K = {name: songs.song_key(title) for name, (title, _, _) in zip(
    ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"), SEED_SONGS)}
SEED_GENERAL_TODOS = ("Back up the sample library", "Renew plugin licence")
OVERVIEW_KEYS = {"today", "counts", "deck", "projects", "agenda", "contacts"}
PROJECT_KEYS = {"key", "title", "full_title", "stage", "stage_label", "status", "effective_status", "hold_expired",
                "hold_reason", "hold_until", "hold_since", "waiting_on", "waiting_on_name", "priority", "due_value",
                "due", "overdue", "days_idle", "stale", "todo_open", "todo_done", "next_todo", "in_lane", "is_deck",
                "on_deck", "settled", "group", "last_seen"}
DETAIL_EXTRA_KEYS = {"notes", "next_task", "seconds", "inferred_stage", "due_source", "list_override"}
AGENDA_KEYS = {"id", "text", "due_date", "song_key", "song_title", "orphan", "overdue", "bucket"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_digest(db: Path) -> str:
    """Logical digest of every table; immune to WAL / page-level noise."""
    conn = sqlite3.connect(db)
    text = "\n".join(conn.iterdump())
    conn.close()
    return hashlib.sha256(text.encode()).hexdigest()


def sessions_digest(db: Path) -> str:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def table_names(db: Path) -> list:
    conn = sqlite3.connect(db)
    names = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    conn.close()
    return names


def copy_db(source: Path, target: Path) -> None:
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def create_sessions_table(conn) -> None:
    conn.execute(
        """CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL,
            last_seen_time REAL,
            active_seconds REAL DEFAULT 0
        )""")


def add_session(conn, name: str, days_ago: float, hours: float) -> None:
    start = time.time() - days_ago * 86400 - hours * 3600
    conn.execute("INSERT INTO sessions(project_name, start_time, end_time, last_seen_time, active_seconds) VALUES(?,?,?,?,?)",
                 (name, start, start + hours * 3600, start + hours * 3600, hours * 3600))


def build_synthetic_source(target: Path) -> None:
    conn = sqlite3.connect(target)
    with conn:
        create_sessions_table(conn)
        add_session(conn, "April 2026 - Prince - 130 bpm Emaj", 3, 2)
        add_session(conn, "August 2026 - DANCE - 118 bpm", 4, 1.5)
    conn.close()


def add_seed_sessions(db: Path) -> None:
    conn = sqlite3.connect(db)
    with conn:
        for title, days_ago, hours in SEED_SONGS:
            add_session(conn, title, days_ago, hours)
    conn.close()


class Client:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def get(self, query: str = ""):
        return self.call("GET", "/api/planner" + (("?" + query) if query else ""))

    def act(self, **body):
        return self.call("POST", "/api/planner", body)

    def project(self, key: str):
        status, body = self.get("key=" + urllib.parse.quote(key))
        return body.get("project", {}) if status == 200 else {}

    def overview(self):
        return self.get()[1]


def serve(db: Path, port: int = 0) -> HTTPServer:
    dashboard.DB_PATH = db
    with dashboard.db_connection() as conn:
        dashboard.run_schema_migrations(conn)
    server = HTTPServer(("127.0.0.1", port), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def seed_planner(client: Client) -> dict:
    """Deterministic planner rows for UI tests; returns the ids it created."""
    today = date.today()
    ana = client.act(action="contact_save", name="Ana Ruiz", role="Vocalist", email="ana@example.com", phone="555-0101")[1]
    ben = client.act(action="contact_save", name="Ben Ortiz", role="Mixing engineer", email="ben@example.com")[1]
    ana_id, ben_id = ana["contact"]["id"], ben["contact"]["id"]
    client.act(action="priority", key=K["alpha"], value=True)
    client.act(action="todo_add", key=K["alpha"], text="Bounce stems for Ben", due_date=(today - timedelta(days=2)).isoformat())
    client.act(action="todo_add", key=K["alpha"], text="Re-record the bridge vocal", due_date=(today + timedelta(days=3)).isoformat())
    client.act(action="contact_link", id=ben_id, key=K["alpha"])
    client.act(action="hold", key=K["bravo"], reason="Waiting for vocal takes", until=(today + timedelta(days=9)).isoformat(),
               waiting_on=ana_id)
    client.act(action="todo_add", key=K["bravo"], text="Send reference mix")
    client.act(action="stage", key=K["delta"], stage="mixing")
    client.call("POST", "/api/on-deck", {"action": "pick", "key": K["delta"]})
    client.act(action="due", key=K["delta"], date=(today + timedelta(days=6)).isoformat())
    for text in SEED_GENERAL_TODOS:
        client.act(action="todo_add", key="", text=text)
    client.act(action="todo_add", key="", text="Send invoice to the label", due_date=today.isoformat())
    return {"ana": ana_id, "ben": ben_id}


def safe_output(output: Path, source: Path) -> None:
    """The output folder is wiped first; refuse any folder that holds the source database or the home folder."""
    out, src = output.resolve(), source.resolve()
    if out == Path.home().resolve() or out in src.parents or out == src.parent:
        raise SystemExit(f"Refusing to wipe {out}: it contains the source database or your home folder.")


def verify(output: Path, source: Path) -> int:
    safe_output(output, source)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    checks = []
    opened = []  # (path, app) recorded by the stubbed project opener

    def check(name: str, ok: bool, detail="") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    today = date.today()
    iso = lambda days: (today + timedelta(days=days)).isoformat()

    # 1. Bare database and sessions-only database: reads answer and never write.
    bare = output / "bare.db"
    sqlite3.connect(bare).close()
    dashboard.DB_PATH = bare
    bare_before = (table_names(bare), content_digest(bare))
    try:
        bare_overview = dashboard.get_planner()
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        bare_overview = {"error": repr(exc)}
    check("bare database answers overview with empty lists",
          bare_overview.get("projects") == [] and bare_overview.get("agenda") == [] and bare_overview.get("contacts") == []
          and bare_overview.get("deck") is None and bare_overview.get("counts") == {
              "active": 0, "on_hold": 0, "overdue_todos": 0, "overdue_projects": 0}, bare_overview)
    check("GET on a bare database writes nothing", (table_names(bare), content_digest(bare)) == bare_before)
    missing = output / "missing.db"
    dashboard.DB_PATH = missing
    try:
        missing_overview = dashboard.get_planner()
    except Exception as exc:  # noqa: BLE001
        missing_overview = {"error": repr(exc)}
    check("missing database answers and is not created", missing_overview.get("projects") == [] and not missing.exists(),
          missing_overview)

    plain = output / "no-planner-tables.db"
    conn = sqlite3.connect(plain)
    with conn:
        create_sessions_table(conn)
        add_session(conn, "Lonely Song - Zed", 2, 2)
    conn.close()
    dashboard.DB_PATH = plain
    plain_before = (table_names(plain), content_digest(plain))
    try:
        plain_overview = dashboard.get_planner()
    except Exception as exc:  # noqa: BLE001
        plain_overview = {"error": repr(exc)}
    plain_project = next(iter(plain_overview.get("projects", [])), {})
    check("sessions-only database lists projects with default plan",
          plain_project.get("key") == songs.song_key("Lonely Song - Zed") and plain_project.get("status") == "active"
          and plain_project.get("group") == "active" and plain_project.get("priority") == 0, plain_overview)
    check("GET on a database without planner tables creates none", (table_names(plain), content_digest(plain)) == plain_before)

    # 2. Real server on a copy plus deterministic songs.
    effective_source = source
    if not source.exists():
        effective_source = output / "synthetic-source.db"
        build_synthetic_source(effective_source)
    source_hash = digest(effective_source)
    db = output / "sessions-copy.db"
    copy_db(effective_source, db)
    add_seed_sessions(db)
    server = serve(db)
    client = Client(server.server_address[1])
    before_sessions = sessions_digest(db)
    dashboard.open_project = lambda path, app: opened.append((str(path), app))
    dashboard.resolve_song_path = lambda db_path, song, resolver=None: Path("/tmp/Planner Test Project/x.als")
    dashboard.find_ableton_app = lambda: "/Applications/Ableton Live.app"

    # 3. GETs are strictly read-only.
    content_before = content_digest(db)
    st, ov = client.get()
    st_p, _ = client.get("key=" + urllib.parse.quote(K["alpha"]))
    st_c, _ = client.get("contact=999999")
    client.get("key=nope")
    client.get("contact=abc")
    check("GETs do not write (content digest unchanged)", content_digest(db) == content_before)

    # 4. Overview shape.
    projects = {p["key"]: p for p in ov.get("projects", [])}
    check("overview is 200 with the documented keys", st == 200 and set(ov) == OVERVIEW_KEYS, sorted(ov))
    check("overview includes every seeded project with the documented keys",
          all(k in projects for k in K.values()) and all(set(p) == PROJECT_KEYS for p in projects.values()),
          sorted({tuple(sorted(PROJECT_KEYS ^ set(p))) for p in projects.values()})[:3])
    check("overview today is the local date", ov.get("today") == today.isoformat(), ov.get("today"))
    check("seeded groups: recent active, long-untouched archive, stale stays active",
          projects[K["alpha"]]["group"] == "active" and projects[K["echo"]]["group"] == "archive"
          and projects[K["foxtrot"]]["group"] == "active" and projects[K["foxtrot"]]["stale"] is True
          and projects[K["foxtrot"]]["days_idle"] in (30,) and projects[K["alpha"]]["stale"] is False,
          {k: (v["group"], v["days_idle"], v["stale"]) for k, v in projects.items() if k in K.values()})
    check("delta is mixing from its file name; stage labels present",
          projects[K["delta"]]["stage"] == "mixing" and projects[K["delta"]]["stage_label"] == "Mixing")

    # 5. Hold / resume.
    st_bad_until, _ = client.act(action="hold", key=K["alpha"], reason="x", until=today.isoformat())
    st_bad_past, _ = client.act(action="hold", key=K["alpha"], reason="x", until=iso(-3))
    st_bad_fmt, _ = client.act(action="hold", key=K["alpha"], reason="x", until="soon")
    st_bad_wait, _ = client.act(action="hold", key=K["alpha"], reason="x", waiting_on=987654)
    st_bad_wait_type, _ = client.act(action="hold", key=K["alpha"], reason="x", waiting_on=True)
    st_long_reason, _ = client.act(action="hold", key=K["alpha"], reason="r" * 201)
    st_no_song, _ = client.act(action="hold", key="__ghost__", reason="x")
    check("hold rejects today/past/garbage dates, missing contacts, bool ids, long reason, unknown key with 400",
          {st_bad_until, st_bad_past, st_bad_fmt, st_bad_wait, st_bad_wait_type, st_long_reason, st_no_song} == {400},
          (st_bad_until, st_bad_past, st_bad_fmt, st_bad_wait, st_bad_wait_type, st_long_reason, st_no_song))
    check("rejected holds changed nothing", client.project(K["alpha"]).get("status") == "active")

    st_c1, c1 = client.act(action="contact_save", name="  Hold   Tester ", role="Singer", email="h@example.com", phone="1", notes="n")
    contact_a = c1.get("contact", {}).get("id")
    check("contact_save normalizes name and returns contact detail",
          st_c1 == 200 and c1.get("ok") is True and c1["contact"]["name"] == "Hold Tester" and c1["contact"]["role"] == "Singer"
          and c1["contact"]["email"] == "h@example.com" and c1.get("songs") == [], c1)
    before_hold = time.time()
    st_h, h = client.act(action="hold", key=K["charlie"], reason="  Waiting   for   stems ", until=iso(10), waiting_on=contact_a)
    proj = h.get("project", {})
    check("hold stores reason, date, waiting_on and returns project detail",
          st_h == 200 and h.get("ok") is True and set(h) >= {"project", "todos", "people", "contacts_all"}
          and proj.get("status") == "on_hold" and proj.get("effective_status") == "on_hold" and proj.get("hold_expired") is False
          and proj.get("hold_reason") == "Waiting for stems" and proj.get("hold_until") == iso(10)
          and proj.get("waiting_on") == contact_a and proj.get("waiting_on_name") == "Hold Tester"
          and proj.get("group") == "on_hold" and (proj.get("hold_since") or 0) >= before_hold - 1
          and set(proj) >= PROJECT_KEYS | DETAIL_EXTRA_KEYS, proj)
    check("waiting_on auto-links the contact to the song",
          [p["id"] for p in h.get("people", [])] == [contact_a], h.get("people"))
    since = proj.get("hold_since")
    time.sleep(0.02)
    st_h2, h2 = client.act(action="hold", key=K["charlie"], reason="Still waiting", until="", waiting_on=None)
    proj2 = h2.get("project", {})
    check("editing a held project keeps hold_since and can clear until / waiting_on",
          st_h2 == 200 and proj2.get("hold_since") == since and proj2.get("hold_reason") == "Still waiting"
          and proj2.get("hold_until") == "" and proj2.get("waiting_on") is None and proj2.get("status") == "on_hold", proj2)
    st_r, r = client.act(action="resume", key=K["charlie"])
    pr = r.get("project", {})
    check("resume clears hold reason, until, since and waiting_on",
          st_r == 200 and pr.get("status") == "active" and pr.get("hold_reason") == "" and pr.get("hold_until") == ""
          and pr.get("hold_since") is None and pr.get("waiting_on") is None and pr.get("group") == "active", pr)
    st_r2, _ = client.act(action="resume", key="__ghost__")
    check("resume of an unknown key is 400", st_r2 == 400, st_r2)

    # 6. Expired holds are computed on read, never written.
    client.act(action="hold", key=K["charlie"], reason="Mastering queue", until=iso(2))
    wconn = sqlite3.connect(db)
    with wconn:
        wconn.execute("UPDATE project_plan SET hold_until=? WHERE song_key=?", (iso(-1), K["charlie"]))
    wconn.close()
    expired = client.project(K["charlie"])
    check("expired hold reads as active with hold_expired and group active",
          expired.get("status") == "on_hold" and expired.get("effective_status") == "active"
          and expired.get("hold_expired") is True and expired.get("group") == "active", expired)
    suggested = [s["key"] for s in songs.suggestions(songs.load_songs(db), today, limit=100)]
    check("expired hold is suggested again", K["charlie"] in suggested)
    client.act(action="resume", key=K["charlie"])

    # 7. Held songs are not suggested; On Deck and lane decks survive a hold.
    client.act(action="hold", key=K["bravo"], reason="Waiting for vocal takes")
    check("on-hold song is not suggested", K["bravo"] not in
          [s["key"] for s in songs.suggestions(songs.load_songs(db), today, limit=100)])
    st_od, _ = client.call("POST", "/api/on-deck", {"action": "pick", "key": K["delta"]})
    st_lane, _ = client.call("POST", "/api/lanes", {"action": "assign", "key": K["delta"], "lane": "mix"})
    st_deck, _ = client.call("POST", "/api/lanes", {"action": "deck", "key": K["delta"]})
    st_hd, hd = client.act(action="hold", key=K["delta"], reason="Client feedback")
    pd = hd.get("project", {})
    check("holding the On Deck / lane-deck song keeps both",
          (st_od, st_lane, st_deck, st_hd) == (200, 200, 200, 200) and pd.get("on_deck") is True and pd.get("is_deck") is True
          and pd.get("in_lane") == "mix" and pd.get("status") == "on_hold", (pd.get("on_deck"), pd.get("is_deck"), pd.get("in_lane")))
    ov = client.overview()
    check("overview deck reflects On Deck even when held",
          (ov.get("deck") or {}).get("key") == K["delta"] and set(ov["deck"]) >= {"key", "title", "stage_label", "due"}, ov.get("deck"))
    client.act(action="resume", key=K["delta"])

    # 8. Priority, notes.
    st_pr, pr1 = client.act(action="priority", key=K["alpha"], value=True)
    st_pr0, pr0 = client.act(action="priority", key=K["bravo"], value=1)
    check("priority accepts bool and 0/1 and persists",
          st_pr == 200 and pr1["project"]["priority"] == 1 and st_pr0 == 200 and pr0["project"]["priority"] == 1)
    st_pr_bad, _ = client.act(action="priority", key=K["alpha"], value="high")
    st_pr_bad2, _ = client.act(action="priority", key=K["alpha"], value=2)
    check("priority rejects other values", (st_pr_bad, st_pr_bad2) == (400, 400), (st_pr_bad, st_pr_bad2))
    client.act(action="priority", key=K["bravo"], value=False)
    st_n, nn = client.act(action="notes", key=K["alpha"], text="Line one\nLine two")
    check("notes persist with newlines", st_n == 200 and nn["project"]["notes"] == "Line one\nLine two"
          and client.project(K["alpha"]).get("notes") == "Line one\nLine two")
    st_n_long, _ = client.act(action="notes", key=K["alpha"], text="n" * 5001)
    st_n_type, _ = client.act(action="notes", key=K["alpha"], text=["x"])
    check("over-long or non-text notes return 400", (st_n_long, st_n_type) == (400, 400), (st_n_long, st_n_type))

    # 9. Due and stage.
    st_d, dd = client.act(action="due", key=K["alpha"], date=iso(12))
    check("due persists as the song's own date",
          st_d == 200 and dd["project"]["due_value"] == iso(12) and dd["project"]["due_source"] == "song"
          and dd["project"]["overdue"] is False, dd.get("project"))
    st_d2, dd2 = client.act(action="due", key=K["alpha"], date="")
    check("due '' clears", st_d2 == 200 and dd2["project"]["due_value"] == "" and dd2["project"]["due_source"] == "", dd2.get("project"))
    st_d3, _ = client.act(action="due", key=K["alpha"], date="31/12/2026")
    st_d4, od = client.act(action="due", key=K["alpha"], date=iso(-4))
    check("bad due is 400; past due is overdue", st_d3 == 400 and st_d4 == 200 and od["project"]["overdue"] is True, (st_d3, st_d4))
    client.act(action="due", key=K["alpha"], date="")

    lconn = sqlite3.connect(db)
    with lconn:
        lconn.execute("""CREATE TABLE IF NOT EXISTS project_metadata (project_name TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0)""")
        lconn.execute("INSERT OR REPLACE INTO project_metadata(project_name, status, due_date, updated_at) VALUES(?, '', ?, 0)",
                      ("Planner Foxtrot - Zed", iso(20)))
    lconn.close()
    legacy = client.project(K["foxtrot"])
    check("older-planner due date is reported with due_source legacy",
          legacy.get("due_source") == "legacy" and legacy.get("due_value") == iso(20), legacy.get("due_source"))
    st_ds, _ = client.act(action="due", key=K["foxtrot"], date=iso(25))
    check("setting a date replaces the legacy one", st_ds == 200 and client.project(K["foxtrot"]).get("due_source") == "song")
    client.act(action="due", key=K["foxtrot"], date="")

    inferred = client.project(K["delta"]).get("inferred_stage")
    st_s_low, _ = client.act(action="stage", key=K["delta"], stage="production")
    st_s_bad, _ = client.act(action="stage", key=K["delta"], stage="legendary")
    st_s_ok, so = client.act(action="stage", key=K["delta"], stage="mastering")
    check("stage below the detected one or unknown is 400; above persists",
          inferred == "mixing" and (st_s_low, st_s_bad) == (400, 400) and st_s_ok == 200 and so["project"]["stage"] == "mastering",
          (inferred, st_s_low, st_s_bad, st_s_ok))
    client.act(action="stage", key=K["delta"], stage="mixing")  # equal to detected is allowed

    # 10. Archive grouping rules.
    ov = client.overview()
    projects = {p["key"]: p for p in ov["projects"]}
    echo = projects[K["echo"]]
    check("auto-archive applies to an untouched plain project", echo["group"] == "archive" and echo["days_idle"] >= 99, echo["group"])
    lane_conn = sqlite3.connect(db)
    with lane_conn:
        lane_conn.execute("INSERT INTO song_lanes(song_key, lane, position, is_deck, updated_at) VALUES(?, 'mix', 0, 0, ?)",
                          (K["echo"], time.time()))
    lane_conn.close()
    in_lane = client.project(K["echo"])
    check("a Three Decks lane keeps an idle, untouched project out of auto-archive",
          in_lane.get("group") == "active" and in_lane.get("in_lane") == "mix", in_lane.get("group"))
    lane_conn = sqlite3.connect(db)
    with lane_conn:
        lane_conn.execute("DELETE FROM song_lanes WHERE song_key = ?", (K["echo"],))
    lane_conn.close()
    client.act(action="priority", key=K["echo"], value=True)
    starred = client.project(K["echo"])["group"]
    client.act(action="priority", key=K["echo"], value=False)
    client.act(action="notes", key=K["echo"], text="remember")
    noted = client.project(K["echo"])["group"]
    client.act(action="notes", key=K["echo"], text="")
    client.act(action="due", key=K["echo"], date=iso(30))
    dued = client.project(K["echo"])["group"]
    client.act(action="due", key=K["echo"], date="")
    st_t, t_echo = client.act(action="todo_add", key=K["echo"], text="Finish it")
    todo = client.project(K["echo"])["group"]
    client.act(action="todo_delete", key=K["echo"], id=t_echo["todos"][0]["id"])
    client.act(action="hold", key=K["echo"], reason="parked")
    held = client.project(K["echo"])["group"]
    client.act(action="resume", key=K["echo"])
    check("star, notes, due date, open to-do or hold each keep a stale project out of Archive",
          (starred, noted, dued, todo, held) == ("active", "active", "active", "active", "on_hold"),
          (starred, noted, dued, todo, held))
    check("project returns to Archive when the reasons are removed", client.project(K["echo"])["group"] == "archive")
    st_lo, lo = client.act(action="list", key=K["alpha"], value="archive")
    check("list override archive moves an active project to Archive",
          st_lo == 200 and lo["project"]["group"] == "archive" and lo["project"]["list_override"] == "archive", lo.get("project"))
    st_la, la = client.act(action="list", key=K["echo"], value="active")
    check("list override active pulls an auto-archived project back",
          st_la == 200 and la["project"]["group"] == "active" and la["project"]["list_override"] == "active", la.get("project"))
    st_lc, lc = client.act(action="list", key=K["alpha"], value="")
    check("list override '' restores automatic grouping", st_lc == 200 and lc["project"]["group"] == "active")
    st_lbad, _ = client.act(action="list", key=K["alpha"], value="sideways")
    check("list rejects other values", st_lbad == 400, st_lbad)
    client.act(action="list", key=K["echo"], value="")

    # 11. Released settles into Archive and clears On Deck.
    client.call("POST", "/api/on-deck", {"action": "pick", "key": K["charlie"]})
    client.act(action="todo_add", key=K["charlie"], text="Post-release thank-you note", due_date=iso(-1))
    st_rel, rel = client.act(action="stage", key=K["charlie"], stage="released")
    pc = rel.get("project", {})
    check("Released settles into Archive, clears On Deck, even with a list override of active",
          st_rel == 200 and pc.get("settled") is True and pc.get("group") == "archive" and pc.get("on_deck") is False, pc)
    client.act(action="list", key=K["charlie"], value="active")
    check("settled stays in Archive regardless of list override", client.project(K["charlie"]).get("group") == "archive")
    client.act(action="list", key=K["charlie"], value="")

    # 12. To-dos: round trip, validation, agenda buckets.
    base_agenda = {a["id"] for a in client.overview()["agenda"]}
    plan = {"overdue": iso(-3), "today": iso(0), "week": iso(5), "later": iso(30), "someday": ""}
    made = {}
    for bucket, due in plan.items():
        st_a, a = client.act(action="todo_add", key="", text=f"General {bucket}", due_date=due)
        made[bucket] = st_a
        check(f"general to-do ({bucket}) add returns the overview", st_a == 200 and a.get("ok") is True and set(a) >= OVERVIEW_KEYS, st_a)
    ov = client.overview()
    agenda = [a for a in ov["agenda"] if a["id"] not in base_agenda]
    by_bucket = {a["bucket"]: a for a in agenda if a["text"].startswith("General ")}
    check("agenda buckets general to-dos correctly",
          {b: x["text"] for b, x in by_bucket.items()} == {b: f"General {b}" for b in plan}
          and all(set(a) == AGENDA_KEYS for a in ov["agenda"])
          and all(x["song_key"] == "" and x["orphan"] is False for x in by_bucket.values())
          and by_bucket["overdue"]["overdue"] is True and by_bucket["today"]["overdue"] is False,
          {b: x["bucket"] for b, x in by_bucket.items()})
    order = [a["bucket"] for a in ov["agenda"]]
    ranks = {"overdue": 0, "today": 1, "week": 2, "later": 3, "someday": 4}
    check("agenda is ordered overdue, today, week, later, someday", [ranks[b] for b in order] == sorted(ranks[b] for b in order), order)
    dated = [a["due_date"] for a in ov["agenda"] if a["due_date"]]
    check("agenda sorts by due date within dated items", dated == sorted(dated), dated)
    week_edge, later_edge = client.act(action="todo_add", key="", text="Edge week", due_date=iso(7)), client.act(
        action="todo_add", key="", text="Edge later", due_date=iso(8))
    edge = {a["text"]: a["bucket"] for a in client.overview()["agenda"]}
    check("today+7 is 'week' and today+8 is 'later'", edge.get("Edge week") == "week" and edge.get("Edge later") == "later", edge)
    check("settled song to-do is on the agenda with a title",
          any(a["text"] == "Post-release thank-you note" and a["song_key"] == K["charlie"] and a["song_title"]
              and a["bucket"] == "overdue" and a["orphan"] is False for a in client.overview()["agenda"]))

    st_ta, ta = client.act(action="todo_add", key=K["bravo"], text="  mix   the   chorus ", due_date=iso(4))
    check("song to-do add returns the project detail",
          st_ta == 200 and ta.get("ok") is True and set(ta) >= {"project", "todos", "people", "contacts_all"}
          and ta["todos"][0]["text"] == "mix the chorus" and ta["project"]["todo_open"] >= 1, ta.get("todos"))
    tid = ta["todos"][0]["id"]
    st_tu, tu = client.act(action="todo_update", key=K["bravo"], id=tid, text="mix the chorus louder", due_date=iso(6))
    st_td, td = client.act(action="todo_update", key=K["bravo"], id=tid, done=True)
    check("todo_update edits and completes; progress counts done",
          st_tu == 200 and st_td == 200 and td["project"]["todo_done"] >= 1
          and next(t for t in td["todos"] if t["id"] == tid)["done"] is True, td.get("project"))
    check("completed to-do leaves the agenda", all(a["id"] != tid for a in client.overview()["agenda"]))
    st_tr, tr = client.act(action="todo_update", key=K["bravo"], id=tid, done=False)
    check("todo_update done=False reopens", st_tr == 200 and next(t for t in tr["todos"] if t["id"] == tid)["done"] is False)
    check("next_todo is the first open to-do",
          client.project(K["bravo"]).get("next_todo") in ("Send reference mix", "mix the chorus louder"))
    st_tx, txd = client.act(action="todo_delete", key=K["bravo"], id=tid)
    check("todo_delete removes it", st_tx == 200 and all(t["id"] != tid for t in txd["todos"]))
    bad = {
        "empty text": client.act(action="todo_add", key=K["bravo"], text="   ")[0],
        "bad date": client.act(action="todo_add", key=K["bravo"], text="x", due_date="2026-13-45")[0],
        "unknown song": client.act(action="todo_add", key="__ghost__", text="x")[0],
        "non-text": client.act(action="todo_add", key=K["bravo"], text=5)[0],
        "bool id": client.act(action="todo_update", key=K["bravo"], id=True, text="x")[0],
        "string id": client.act(action="todo_update", key=K["bravo"], id="abc", text="x")[0],
        "missing id": client.act(action="todo_delete", key=K["bravo"], id=987654)[0],
        "wrong key": client.act(action="todo_update", key=K["alpha"], id=tr["todos"][0]["id"], text="steal")[0],
        "done not bool": client.act(action="todo_update", key=K["bravo"], id=tr["todos"][0]["id"], done="yes")[0],
        "general bad id": client.act(action="todo_delete", key="", id=987654)[0],
    }
    check("to-do validation errors are all 400", set(bad.values()) == {400}, bad)

    # Orphan to-dos: a stored key that no longer matches any song.
    oconn = sqlite3.connect(db)
    with oconn:
        cur = oconn.execute("INSERT INTO song_tasks(song_key, text, due_date, done_at, created_at, updated_at) VALUES(?,?,?,NULL,?,?)",
                            ("ghost song - renamed", "Orphaned follow-up", iso(-5), time.time(), time.time()))
        orphan_id = cur.lastrowid
        oconn.execute("INSERT OR IGNORE INTO planner_contact_songs(contact_id, song_key, created_at) VALUES(?,?,?)",
                      (contact_a, "ghost song - renamed", time.time()))
    oconn.close()
    orphan_item = next((a for a in client.overview()["agenda"] if a["id"] == orphan_id), {})
    check("orphan to-do is on the agenda with the stored key as title",
          orphan_item.get("orphan") is True and orphan_item.get("song_title") == "ghost song - renamed"
          and orphan_item.get("bucket") == "overdue", orphan_item)
    st_ou, ou = client.act(action="todo_update", key="ghost song - renamed", id=orphan_id, done=True)
    check("orphan to-do can be completed and returns the overview",
          st_ou == 200 and set(ou) >= OVERVIEW_KEYS and all(a["id"] != orphan_id for a in ou["agenda"]), st_ou)
    st_ou2, ou2 = client.act(action="todo_update", key="ghost song - renamed", id=orphan_id, done=False)
    st_od2, od2 = client.act(action="todo_delete", key="ghost song - renamed", id=orphan_id)
    check("orphan to-do can be reopened and deleted", st_ou2 == 200 and st_od2 == 200 and set(od2) >= OVERVIEW_KEYS, (st_ou2, st_od2))
    st_add_orphan, _ = client.act(action="todo_add", key="ghost song - renamed", text="new")
    check("adding to a key with no song is 400", st_add_orphan == 400)

    # General update / delete round trip returns the overview.
    gen_id = next(a["id"] for a in client.overview()["agenda"] if a["text"] == "General someday")
    st_gu, gu = client.act(action="todo_update", key="", id=gen_id, text="General someday edited", due_date=iso(2))
    st_gd, gd = client.act(action="todo_delete", key="", id=gen_id)
    check("general to-do edit and delete return the overview",
          st_gu == 200 and st_gd == 200 and set(gu) >= OVERVIEW_KEYS and set(gd) >= OVERVIEW_KEYS
          and all(a["id"] != gen_id for a in gd["agenda"]), (st_gu, st_gd))

    # 13. Counts exclude archived and on-hold projects, include general to-dos.
    ov = client.overview()
    projects = {p["key"]: p for p in ov["projects"]}
    countable = {k for k, p in projects.items() if p["group"] != "archive" and p["effective_status"] != "on_hold"}
    exp_overdue_todos = sum(1 for a in ov["agenda"] if a["overdue"] and (a["song_key"] == "" or a["song_key"] in countable
                                                                           or a["song_key"] not in projects))
    exp_overdue_projects = sum(1 for k in countable if projects[k]["overdue"] and not projects[k]["settled"])
    exp_active = sum(1 for p in ov["projects"] if p["group"] == "active")
    exp_hold = sum(1 for p in ov["projects"] if p["group"] == "on_hold")
    check("counts agree with the rows (overdue excludes archived and on-hold)",
          ov["counts"] == {"active": exp_active, "on_hold": exp_hold, "overdue_todos": exp_overdue_todos,
                           "overdue_projects": exp_overdue_projects}, (ov["counts"], exp_active, exp_hold, exp_overdue_todos, exp_overdue_projects))
    check("a held project's overdue to-dos do not count", ov["counts"]["overdue_todos"] >= 1)
    client.act(action="todo_add", key=K["bravo"], text="Held overdue", due_date=iso(-9))
    held_proj = client.project(K["bravo"])
    pre = client.overview()["counts"]["overdue_todos"]
    client.act(action="resume", key=K["bravo"])
    post = client.overview()["counts"]["overdue_todos"]
    check("resuming a held project brings its overdue to-dos into the count", held_proj["effective_status"] == "on_hold" and post == pre + 1, (pre, post))

    # 14. Contacts.
    st_c2, c2 = client.act(action="contact_save", name="Mix Buddy", role="Engineer")
    contact_b = c2["contact"]["id"]
    st_cu, cu = client.act(action="contact_save", id=contact_b, name="Mix Buddy Jr", role="Mastering", email="b@example.com",
                           phone="22", notes="Prefers email")
    check("contact_save updates by id", st_cu == 200 and cu["contact"]["id"] == contact_b and cu["contact"]["name"] == "Mix Buddy Jr"
          and cu["contact"]["notes"] == "Prefers email" and set(cu["contact"]) == {"id", "name", "role", "email", "phone", "notes"}, cu.get("contact"))
    cbad = {
        "empty name": client.act(action="contact_save", name="   ")[0],
        "long name": client.act(action="contact_save", name="n" * 121)[0],
        "long role": client.act(action="contact_save", name="ok", role="r" * 81)[0],
        "long email": client.act(action="contact_save", name="ok", email="e" * 201)[0],
        "long phone": client.act(action="contact_save", name="ok", phone="1" * 61)[0],
        "long notes": client.act(action="contact_save", name="ok", notes="n" * 2001)[0],
        "unknown id": client.act(action="contact_save", id=987654, name="ok")[0],
        "bool id": client.act(action="contact_save", id=True, name="ok")[0],
        "delete unknown": client.act(action="contact_delete", id=987654)[0],
        "link unknown contact": client.act(action="contact_link", id=987654, key=K["alpha"])[0],
        "link unknown song": client.act(action="contact_link", id=contact_b, key="__ghost__")[0],
        "unlink unknown contact": client.act(action="contact_unlink", id=987654, key=K["alpha"])[0],
    }
    check("contact validation errors are all 400", set(cbad.values()) == {400}, cbad)
    st_l, ln = client.act(action="contact_link", id=contact_b, key=K["alpha"])
    st_l2, ln2 = client.act(action="contact_link", id=contact_b, key=K["alpha"])
    people = ln2.get("people", [])
    check("contact_link is idempotent and returns the project detail with email / phone",
          st_l == 200 and st_l2 == 200 and [p["id"] for p in people].count(contact_b) == 1
          and set(people[0]) == {"id", "name", "role", "email", "phone"} and any(c["id"] == contact_b for c in ln2["contacts_all"]),
          people)
    st_g, gc = client.get(f"contact={contact_b}")
    check("GET contact detail lists linked songs",
          st_g == 200 and set(gc) == {"contact", "songs"} and [s["key"] for s in gc["songs"]] == [K["alpha"]]
          and set(gc["songs"][0]) == {"key", "title", "stage_label", "orphan"} and gc["songs"][0]["orphan"] is False, gc)
    check("GET unknown contact is 404 and non-integer is 400",
          (client.get("contact=987654")[0], client.get("contact=abc")[0], client.get("key=__ghost__")[0]) == (404, 400, 404))
    ov = client.overview()
    mine = next((c for c in ov["contacts"] if c["id"] == contact_b), {})
    names = [c["name"].casefold() for c in ov["contacts"]]
    check("overview contacts carry song_count and are sorted by name",
          set(mine) == {"id", "name", "role", "song_count"} and mine["song_count"] == 1 and names == sorted(names), mine)

    st_u1, u1 = client.act(action="contact_unlink", id=contact_a, key="ghost song - renamed")
    check("unlinking an orphan key works", st_u1 == 200 and u1.get("ok") is True, u1)
    st_u2, u2 = client.act(action="contact_unlink", id=contact_b, key=K["alpha"], **{"from": "contact"})
    check("contact_unlink from contact view returns contact detail",
          st_u2 == 200 and set(u2) >= {"contact", "songs"} and u2["songs"] == [], u2)
    client.act(action="hold", key=K["bravo"], reason="Waiting", waiting_on=contact_b)
    check("waiting_on contact is linked", contact_b in [p["id"] for p in client.get("key=" + urllib.parse.quote(K["bravo"]))[1]["people"]])
    st_uw, uw = client.act(action="contact_unlink", id=contact_b, key=K["bravo"])
    check("unlinking the waiting_on contact clears waiting_on",
          st_uw == 200 and uw["project"]["waiting_on"] is None and uw["project"]["status"] == "on_hold", uw.get("project"))

    client.act(action="hold", key=K["bravo"], reason="Waiting", waiting_on=contact_b)
    client.act(action="contact_link", id=contact_b, key=K["foxtrot"])
    st_cd, cd = client.act(action="contact_delete", id=contact_b)
    check("contact_delete returns the overview", st_cd == 200 and set(cd) >= OVERVIEW_KEYS and all(c["id"] != contact_b for c in cd["contacts"]), st_cd)
    vconn = sqlite3.connect(db)
    leftovers = (vconn.execute("SELECT COUNT(*) FROM planner_contact_songs WHERE contact_id=?", (contact_b,)).fetchone()[0],
                 vconn.execute("SELECT COUNT(*) FROM project_plan WHERE waiting_on=?", (contact_b,)).fetchone()[0],
                 vconn.execute("SELECT COUNT(*) FROM planner_contacts WHERE id=?", (contact_b,)).fetchone()[0])
    kinds = {k: [r[0] for r in vconn.execute("SELECT kind FROM song_events WHERE song_key=? AND kind LIKE 'plan_%' ORDER BY id", (k,))]
             for k in (K["alpha"], K["bravo"], K["charlie"])}
    vconn.close()
    check("contact_delete removes links, clears waiting_on and the contact in one go", leftovers == (0, 0, 0), leftovers)
    check("hold / resume / priority changes are logged as plan_* events",
          {"plan_hold", "plan_resume"} <= set(kinds[K["charlie"]]) and "plan_priority" in kinds[K["alpha"]]
          and "plan_hold" in kinds[K["bravo"]], kinds)

    # 15. Open in Live.
    st_op, op = client.act(action="open", key=K["alpha"])
    check("open calls the project opener with the resolved path",
          st_op == 200 and op.get("ok") is True and opened and opened[-1][0].endswith("x.als"), (st_op, opened))
    st_op2, _ = client.act(action="open", key="__ghost__")
    check("open of an unknown song is 400", st_op2 == 400, st_op2)

    # 16. Transport-level errors.
    st_bj, _ = client.call("POST", "/api/planner", raw=b"{not json")
    st_arr, _ = client.call("POST", "/api/planner", raw=b"[1,2]")
    st_empty, _ = client.call("POST", "/api/planner", raw=b"")
    st_unknown, _ = client.act(action="launch_rockets")
    st_noaction, _ = client.act(key=K["alpha"])
    st_keylist, _ = client.act(action="hold", key=["a"], reason="x")
    st_idlist, _ = client.act(action="contact_delete", id=[1])
    check("bad JSON, non-object bodies, unknown actions and odd types are all 400",
          (st_bj, st_arr, st_empty, st_unknown, st_noaction, st_keylist, st_idlist) == (400,) * 7,
          (st_bj, st_arr, st_empty, st_unknown, st_noaction, st_keylist, st_idlist))

    # 17. Existing contracts unchanged.
    st_sp, sp = client.call("GET", "/api/song-planner")
    st_spd, spd = client.call("GET", "/api/song-planner?key=" + urllib.parse.quote(K["alpha"]))
    st_deck, deck = client.call("GET", "/api/on-deck")
    st_lanes, lanes = client.call("GET", "/api/lanes")
    check("existing planner / On Deck / lanes endpoints keep their shapes",
          st_sp == 200 and set(sp) == {"query", "songs"} and st_spd == 200 and set(spd) == {"song", "tasks"}
          and set(spd["song"]) == {"key", "title", "display_title", "stage", "stage_label", "inferred_stage", "due_value",
                                   "due_source", "due", "overdue", "on_deck", "settled", "seconds"}
          and st_deck == 200 and "mode" in deck and st_lanes == 200 and set(lanes) == {"now_lane", "lanes", "available"},
          (st_sp, st_spd, st_deck, st_lanes))
    st_sg, sg = client.call("POST", "/api/song-planner", {"action": "task_add", "key": K["alpha"], "text": "shared with planner"})
    check("a to-do added through the Song Planner popup shows in the Planner",
          st_sg == 200 and any(t["text"] == "shared with planner" for t in client.get("key=" + urllib.parse.quote(K["alpha"]))[1]["todos"]))

    # 18. Sessions untouched.
    check("sessions rows untouched", sessions_digest(db) == before_sessions)

    return finish(output, checks, effective_source, source_hash, server)


def finish(output: Path, checks: list, source: Path, source_hash: str, server: HTTPServer) -> int:
    server.shutdown()
    checks.append({"name": "source database unchanged", "ok": digest(source) == source_hash, "detail": ""})
    passed = all(c["ok"] for c in checks)
    (output / "results.json").write_text(json.dumps({"passed": passed, "checks": checks}, indent=2))
    for c in checks:
        print(f"{'PASS' if c['ok'] else 'FAIL'}  {c['name']}" + (f"  ({c['detail']})" if not c["ok"] and c["detail"] != "" else ""))
    print(f"\n{'All checks passed' if passed else 'Some checks failed'} ({sum(c['ok'] for c in checks)}/{len(checks)}). "
          f"Artifact: {output / 'results.json'}")
    return 0 if passed else 1


def serve_seeded(output: Path, source: Path, port: int, synthetic: bool) -> int:
    """Serve a seeded copy until killed, for the UI test."""
    safe_output(output, source)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    if synthetic or not source.exists():
        effective_source = output / "synthetic-source.db"
        build_synthetic_source(effective_source)
    else:
        effective_source = source
    db = output / "sessions-copy.db"
    copy_db(effective_source, db)
    add_seed_sessions(db)
    dashboard.open_project = lambda path, app: None  # never launch Live from a served test copy
    server = serve(db, port)
    client = Client(server.server_address[1])
    ids = seed_planner(client)
    (output / "seed.json").write_text(json.dumps({"keys": K, "contacts": ids, "general_todos": list(SEED_GENERAL_TODOS)}, indent=2))
    print(f"URL: {client.base}/#planner", flush=True)
    print(f"Seeded copy: {db} (Ctrl+C to stop)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=Path("/tmp/planner-dashboard"))
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--serve", type=int, metavar="PORT", default=None, help="serve a seeded copy for UI checks (0 = any free port)")
    parser.add_argument("--synthetic", action="store_true", help="use a small synthetic database instead of copying the real one")
    args = parser.parse_args()
    if args.serve is not None:
        sys.exit(serve_seeded(args.output, args.source, args.serve, args.synthetic))
    sys.exit(verify(args.output, args.source))
