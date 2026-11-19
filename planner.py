"""Planner: project plans (hold, priority, notes), contacts and the to-do agenda (no GUI imports).

Songs and their to-dos stay in songs.py (song_state, song_tasks); this module adds the
planner-only tables and read models. General to-dos are song_tasks rows with song_key ''.
Reads open the database read-only and never migrate; writes use one transaction each.
"""
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import songs

STALE_DAYS = 21
AUTO_ARCHIVE_DAYS = 45
AGENDA_WEEK_DAYS = 7
STATUSES = ("active", "on_hold")
LIST_OVERRIDES = ("", "active", "archive")
MAX_REASON, MAX_NOTES = 200, 5000
MAX_NAME, MAX_ROLE, MAX_EMAIL, MAX_PHONE, MAX_CONTACT_NOTES = 120, 80, 200, 60, 2000

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS project_plan (
        song_key      TEXT PRIMARY KEY,
        status        TEXT NOT NULL DEFAULT 'active',
        hold_reason   TEXT NOT NULL DEFAULT '',
        hold_until    TEXT NOT NULL DEFAULT '',
        hold_since    REAL,
        waiting_on    INTEGER,
        priority      INTEGER NOT NULL DEFAULT 0,
        notes         TEXT NOT NULL DEFAULT '',
        list_override TEXT NOT NULL DEFAULT '',
        updated_at    REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS planner_contacts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        role       TEXT NOT NULL DEFAULT '',
        email      TEXT NOT NULL DEFAULT '',
        phone      TEXT NOT NULL DEFAULT '',
        notes      TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS planner_contact_songs (
        contact_id INTEGER NOT NULL,
        song_key   TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (contact_id, song_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_planner_contact_songs_key ON planner_contact_songs(song_key)",
)


def ensure_planner_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)


@contextmanager
def _connect(db_path):
    """One committed (or rolled back) write transaction; never caches the connection."""
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        with conn:
            songs.ensure_song_schema(conn)
            ensure_planner_schema(conn)
            yield conn
    finally:
        conn.close()


def _today(now: float) -> date:
    return datetime.fromtimestamp(now).date()


# --- validation -------------------------------------------------------------

def _text(value, label: str, limit: int, required: bool = False, collapse: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    cleaned = " ".join(value.split()) if collapse else value.strip()
    if required and not cleaned:
        raise ValueError(f"{label} is required")
    if len(cleaned) > limit:
        raise ValueError(f"{label} is too long (max {limit} characters)")
    return cleaned


def _id(value, label: str = "id") -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} is required")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"{label} must be an integer")


def _key(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Song key must be text")
    return value


def _date(value, label: str = "date") -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Use a YYYY-MM-DD date")
    value = value.strip()
    if value and not songs._parse_date(value):
        raise ValueError("Use a YYYY-MM-DD date")
    return value


def _require_song(db_path, key: str, now: float) -> dict:
    if not key:
        raise ValueError("Song key is required")
    return songs._find(songs.load_songs(db_path, now=now), key)


def _require_contact(conn, contact_id: int) -> tuple:
    row = conn.execute("SELECT id, name FROM planner_contacts WHERE id = ?", (contact_id,)).fetchone()
    if row is None:
        raise ValueError("That contact no longer exists")
    return row


def _ensure_plan(conn, key: str, now: float) -> None:
    conn.execute("INSERT INTO project_plan(song_key, updated_at) VALUES(?, ?) ON CONFLICT(song_key) DO NOTHING", (key, now))


def _set_plan(conn, key: str, now: float, **fields) -> None:
    _ensure_plan(conn, key, now)
    assignments = ", ".join(f"{column} = ?" for column in fields)
    conn.execute(f"UPDATE project_plan SET {assignments}, updated_at = ? WHERE song_key = ?", (*fields.values(), now, key))


def _link(conn, contact_id: int, key: str, now: float) -> None:
    conn.execute("INSERT OR IGNORE INTO planner_contact_songs(contact_id, song_key, created_at) VALUES(?, ?, ?)",
                 (contact_id, key, now))


# --- read models ------------------------------------------------------------

def _read_context(db_path) -> dict:
    """Everything the read models need from one read-only connection; tolerates missing tables."""
    ctx = {"plans": {}, "contacts": {}, "links": [], "open": [], "counts": {}, "lanes": {}}
    path = Path(db_path)
    if not path.exists():
        return ctx
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.OperationalError:
        return ctx
    with conn:
        for row in songs._optional_rows(conn, """SELECT song_key, status, hold_reason, hold_until, hold_since,
                waiting_on, priority, notes, list_override FROM project_plan"""):
            ctx["plans"][row[0]] = {"status": row[1], "hold_reason": row[2], "hold_until": row[3], "hold_since": row[4],
                                    "waiting_on": row[5], "priority": int(row[6] or 0), "notes": row[7], "list_override": row[8]}
        for row in songs._optional_rows(conn, "SELECT id, name, role, email, phone, notes FROM planner_contacts"):
            ctx["contacts"][row[0]] = {"id": row[0], "name": row[1], "role": row[2], "email": row[3], "phone": row[4], "notes": row[5]}
        ctx["links"] = songs._optional_rows(conn, "SELECT contact_id, song_key FROM planner_contact_songs")
        ctx["open"] = songs._optional_rows(conn, """SELECT id, song_key, text, due_date, created_at FROM song_tasks
            WHERE done_at IS NULL ORDER BY CASE WHEN due_date = '' THEN 1 ELSE 0 END, due_date, created_at, id""")
        for key, done_at_null, count in songs._optional_rows(
                conn, "SELECT song_key, done_at IS NULL, COUNT(*) FROM song_tasks GROUP BY song_key, done_at IS NULL"):
            ctx["counts"].setdefault(key, [0, 0])[0 if done_at_null else 1] += count
        for key, lane, is_deck in songs._optional_rows(conn, "SELECT song_key, lane, is_deck FROM song_lanes"):
            ctx["lanes"][key] = (lane, bool(is_deck))
    conn.close()
    return ctx


def _hold_expired(plan: dict, today: date) -> bool:
    until = songs._parse_date(plan["hold_until"])
    return plan["status"] == "on_hold" and bool(until and until <= today)


def _group(song: dict, plan: dict, todo_open: int, days_idle: int, today: date, in_lane: str) -> str:
    if song["settled"] or plan["list_override"] == "archive":
        return "archive"
    on_hold = plan["status"] == "on_hold"
    if (plan["list_override"] != "active" and plan["priority"] == 0 and not on_hold and plan["notes"] == ""
            and todo_open == 0 and not songs._parse_date(song["due_date"]) and days_idle > AUTO_ARCHIVE_DAYS
            and not song["on_deck"] and not in_lane):
        return "archive"
    return "on_hold" if on_hold and not _hold_expired(plan, today) else "active"


def _project(song: dict, ctx: dict, today: date) -> dict:
    plan = dict(ctx["plans"].get(song["key"]) or {"status": "active", "hold_reason": "", "hold_until": "", "hold_since": None,
                                                  "waiting_on": None, "priority": 0, "notes": "", "list_override": ""})
    open_count, done_count = ctx["counts"].get(song["key"], [0, 0])
    days_idle = max((today - datetime.fromtimestamp(song["last_seen"]).date()).days, 0)
    expired = _hold_expired(plan, today)
    effective = "active" if expired else plan["status"]
    disp = songs.deck_display(song, today)
    due = songs._effective_due(song)
    contact = ctx["contacts"].get(plan["waiting_on"]) if plan["waiting_on"] is not None else None
    lane, is_deck = ctx["lanes"].get(song["key"], ("", False))
    next_todo = next((row[2] for row in ctx["open"] if row[1] == song["key"]), "")
    return {
        "key": song["key"],
        "title": songs.display_title(song["title"]),
        "full_title": song["title"],
        "stage": song["stage"],
        "stage_label": disp["stage_label"],
        "status": plan["status"],
        "effective_status": effective,
        "hold_expired": expired,
        "hold_reason": plan["hold_reason"],
        "hold_until": plan["hold_until"],
        "hold_since": plan["hold_since"],
        "waiting_on": plan["waiting_on"],
        "waiting_on_name": contact["name"] if contact else "",
        "list_override": plan["list_override"],
        "priority": plan["priority"],
        "due_value": due.isoformat() if due else "",
        "due": disp["due"],
        "overdue": disp["overdue"],
        "days_idle": days_idle,
        "stale": effective == "active" and not song["settled"] and days_idle >= STALE_DAYS,
        "todo_open": open_count,
        "todo_done": done_count,
        "next_todo": next_todo,
        "in_lane": lane,
        "is_deck": is_deck,
        "on_deck": bool(song["on_deck"]),
        "settled": bool(song["settled"]),
        "group": _group(song, plan, open_count, days_idle, today, lane),
        "last_seen": song["last_seen"],
    }


def _sort_key(project: dict):
    due = songs._parse_date(project["due_value"])
    return (-project["priority"], not project["overdue"], due.toordinal() if due else 10 ** 9, -project["last_seen"])


def _bucket(due: str, today: date) -> str:
    parsed = songs._parse_date(due)
    if not parsed:
        return "someday"
    if parsed < today:
        return "overdue"
    if parsed == today:
        return "today"
    return "week" if parsed <= today + timedelta(days=AGENDA_WEEK_DAYS) else "later"


def _agenda(ctx: dict, by_key: dict, today: date) -> list[dict]:
    items = []
    for task_id, key, text, due, created_at in ctx["open"]:
        project = by_key.get(key)
        items.append({
            "id": task_id, "text": text, "due_date": due or "", "song_key": key,
            "song_title": "" if not key else project["title"] if project else key,
            "orphan": bool(key) and project is None,
            "overdue": _bucket(due, today) == "overdue" and bool(due),
            "bucket": _bucket(due, today),
            "_sort": (not due, due or "", created_at, task_id),
        })
    items.sort(key=lambda item: item["_sort"])
    for item in items:
        del item["_sort"]
    return items


def _build(db_path, now: float):
    today = _today(now)
    ctx = _read_context(db_path)
    all_songs = songs.load_songs(db_path, now=now)
    projects = sorted((_project(song, ctx, today) for song in all_songs), key=_sort_key)
    by_key = {p["key"]: p for p in projects}
    return today, ctx, all_songs, projects, by_key


def get_overview(db_path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    today, ctx, all_songs, projects, by_key = _build(db_path, now)
    agenda = _agenda(ctx, by_key, today)
    counted = {p["key"] for p in projects if p["group"] != "archive" and p["effective_status"] != "on_hold"}
    deck = songs.current_deck(all_songs)
    link_counts: dict[int, int] = {}
    for contact_id, _key_ in ctx["links"]:
        link_counts[contact_id] = link_counts.get(contact_id, 0) + 1
    contacts = sorted(({"id": c["id"], "name": c["name"], "role": c["role"], "song_count": link_counts.get(c["id"], 0)}
                       for c in ctx["contacts"].values()), key=lambda c: (c["name"].casefold(), c["id"]))
    return {
        "today": today.isoformat(),
        "counts": {
            "active": sum(1 for p in projects if p["group"] == "active"),
            "on_hold": sum(1 for p in projects if p["group"] == "on_hold"),
            "overdue_todos": sum(1 for a in agenda if a["overdue"] and (not a["song_key"] or a["song_key"] not in by_key
                                                                         or a["song_key"] in counted)),
            "overdue_projects": sum(1 for p in projects if p["key"] in counted and p["overdue"]),
        },
        "deck": {"key": deck["key"], "title": songs.display_title(deck["title"]),
                 "stage_label": songs.STAGE_LABELS.get(deck["stage"], deck["stage"]),
                 "due": songs.deck_display(deck, today)["due"]} if deck else None,
        "projects": projects,
        "agenda": agenda,
        "contacts": contacts,
    }


def get_project(db_path, key: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    today, ctx, all_songs, projects, by_key = _build(db_path, now)
    project = by_key.get(key)
    if project is None:
        raise LookupError("That song is no longer in the tracker")
    song = next(s for s in all_songs if s["key"] == key)
    plan = ctx["plans"].get(key) or {}
    own_due = songs._parse_date(song["due_date"])
    linked = sorted((ctx["contacts"][cid] for cid, k in ctx["links"] if k == key and cid in ctx["contacts"]),
                    key=lambda c: (c["name"].casefold(), c["id"]))
    return {
        "project": {**project, "notes": plan.get("notes", ""), "next_task": song["next_task"], "seconds": song["seconds"],
                    "inferred_stage": song["inferred_stage"], "list_override": plan.get("list_override", ""),
                    "due_source": "song" if own_due else "legacy" if project["due_value"] else ""},
        "todos": songs.list_song_tasks(db_path, key, now=now),
        "people": [{k: c[k] for k in ("id", "name", "role", "email", "phone")} for c in linked],
        "contacts_all": sorted(({k: c[k] for k in ("id", "name", "role")} for c in ctx["contacts"].values()),
                               key=lambda c: (c["name"].casefold(), c["id"])),
    }


def get_contact(db_path, contact_id: int, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    ctx = _read_context(db_path)
    contact = ctx["contacts"].get(contact_id)
    if contact is None:
        raise LookupError("That contact no longer exists")
    by_key = {s["key"]: s for s in songs.load_songs(db_path, now=now)}
    linked = []
    for cid, key in ctx["links"]:
        if cid != contact_id:
            continue
        song = by_key.get(key)
        linked.append({"key": key, "title": songs.display_title(song["title"]) if song else key,
                       "stage_label": songs.STAGE_LABELS.get(song["stage"], song["stage"]) if song else "",
                       "orphan": song is None})
    linked.sort(key=lambda s: (s["title"].casefold(), s["key"]))
    return {"contact": dict(contact), "songs": linked}


def known_song_keys(db_path, now: float | None = None) -> set[str]:
    return {s["key"] for s in songs.load_songs(db_path, now=time.time() if now is None else now)}


# --- project plan writes ----------------------------------------------------

def set_hold(db_path, key: str, reason, until, waiting_on, now: float | None = None) -> None:
    now = time.time() if now is None else now
    today = _today(now)
    reason = _text(reason, "Reason", MAX_REASON)
    until = _date(until)
    if until and songs._parse_date(until) <= today:
        raise ValueError("Pick a back-on date after today")
    contact_id = None if waiting_on is None else _id(waiting_on, "Contact")
    _require_song(db_path, key, now)
    with _connect(db_path) as conn:
        if contact_id is not None:
            _require_contact(conn, contact_id)
        row = conn.execute("SELECT status, hold_until, hold_since FROM project_plan WHERE song_key = ?", (key,)).fetchone()
        held = bool(row and row[0] == "on_hold" and not _hold_expired({"status": row[0], "hold_until": row[1]}, today))
        since = row[2] if held and row[2] is not None else now
        _set_plan(conn, key, now, status="on_hold", hold_reason=reason, hold_until=until, hold_since=since, waiting_on=contact_id)
        if contact_id is not None:
            _link(conn, contact_id, key, now)
        songs._event(conn, key, "plan_hold", reason or until, now)


def resume(db_path, key: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    _require_song(db_path, key, now)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT status FROM project_plan WHERE song_key = ?", (key,)).fetchone()
        if row and row[0] == "on_hold":
            _set_plan(conn, key, now, status="active", hold_reason="", hold_until="", hold_since=None, waiting_on=None)
            songs._event(conn, key, "plan_resume", "", now)


def set_priority(db_path, key: str, value, now: float | None = None) -> None:
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, int) or value not in (0, 1):
        raise ValueError("Priority must be on or off")
    now = time.time() if now is None else now
    _require_song(db_path, key, now)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT priority FROM project_plan WHERE song_key = ?", (key,)).fetchone()
        if (row[0] if row else 0) != value:
            _set_plan(conn, key, now, priority=value)
            songs._event(conn, key, "plan_priority", str(value), now)


def set_notes(db_path, key: str, text, now: float | None = None) -> None:
    notes = _text(text, "Notes", MAX_NOTES, collapse=False)
    now = time.time() if now is None else now
    _require_song(db_path, key, now)
    with _connect(db_path) as conn:
        _set_plan(conn, key, now, notes=notes)


def set_list(db_path, key: str, value, now: float | None = None) -> None:
    if value is None:
        value = ""
    if value not in LIST_OVERRIDES:
        raise ValueError("List must be active, archive or empty")
    now = time.time() if now is None else now
    _require_song(db_path, key, now)
    with _connect(db_path) as conn:
        _set_plan(conn, key, now, list_override=value)


def set_stage(db_path, key: str, stage, now: float | None = None) -> None:
    now = time.time() if now is None else now
    if not isinstance(stage, str) or stage not in songs.STAGE_RANK:
        raise ValueError(f"Unknown stage: {stage}")
    song = _require_song(db_path, key, now)
    if songs.STAGE_RANK[stage] < songs.STAGE_RANK[song["inferred_stage"]]:
        raise ValueError("That stage is detected from your files")
    songs.set_stage(db_path, key, stage, now=now)


def set_due(db_path, key: str, value, now: float | None = None) -> None:
    now = time.time() if now is None else now
    _require_song(db_path, key, now)
    songs.set_due_date(db_path, key, _date(value), now=now)


# --- to-dos -----------------------------------------------------------------

def add_todo(db_path, key: str, text, due_date="", now: float | None = None) -> int:
    now = time.time() if now is None else now
    if key:
        _require_song(db_path, key, now)
    if not isinstance(text, str):
        raise ValueError("Write the task first")
    return songs.add_song_task(db_path, key, text, due_date=_date(due_date), now=now)


# --- contacts ---------------------------------------------------------------

def save_contact(db_path, contact_id, name, role="", email="", phone="", notes="", now: float | None = None) -> int:
    now = time.time() if now is None else now
    fields = (_text(name, "Name", MAX_NAME, required=True), _text(role, "Role", MAX_ROLE), _text(email, "Email", MAX_EMAIL),
              _text(phone, "Phone", MAX_PHONE), _text(notes, "Notes", MAX_CONTACT_NOTES, collapse=False))
    cid = None if contact_id is None else _id(contact_id, "Contact")
    with _connect(db_path) as conn:
        if cid is None:
            cur = conn.execute("""INSERT INTO planner_contacts(name, role, email, phone, notes, created_at, updated_at)
                                  VALUES(?, ?, ?, ?, ?, ?, ?)""", (*fields, now, now))
            return int(cur.lastrowid)
        _require_contact(conn, cid)
        conn.execute("UPDATE planner_contacts SET name = ?, role = ?, email = ?, phone = ?, notes = ?, updated_at = ? WHERE id = ?",
                     (*fields, now, cid))
        return cid


def delete_contact(db_path, contact_id, now: float | None = None) -> None:
    cid = _id(contact_id, "Contact")
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        _require_contact(conn, cid)
        conn.execute("DELETE FROM planner_contact_songs WHERE contact_id = ?", (cid,))
        conn.execute("UPDATE project_plan SET waiting_on = NULL, updated_at = ? WHERE waiting_on = ?", (now, cid))
        conn.execute("DELETE FROM planner_contacts WHERE id = ?", (cid,))


def link_contact(db_path, contact_id, key: str, now: float | None = None) -> None:
    cid = _id(contact_id, "Contact")
    now = time.time() if now is None else now
    _require_song(db_path, key, now)
    with _connect(db_path) as conn:
        _require_contact(conn, cid)
        _link(conn, cid, key, now)


def unlink_contact(db_path, contact_id, key: str, now: float | None = None) -> None:
    """Orphan keys are accepted so a renamed song's link can always be removed."""
    cid = _id(contact_id, "Contact")
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        _require_contact(conn, cid)
        conn.execute("DELETE FROM planner_contact_songs WHERE contact_id = ? AND song_key = ?", (cid, key))
        conn.execute("UPDATE project_plan SET waiting_on = NULL, updated_at = ? WHERE song_key = ? AND waiting_on = ?",
                     (now, key, cid))
