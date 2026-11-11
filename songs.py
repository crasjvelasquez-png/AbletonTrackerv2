"""Songs and On Deck: groups stage-split Live sets into songs (no GUI imports).

A song is every set that shares a normalized name: the song file holds idea,
production and recording; "- Mixing" and "- Master" files hold later stages.
Session rows stay the source of truth; this module only adds song_state and
song_events, and reads legacy planner tables when they exist.
"""
import math
from contextlib import contextmanager
import re
import sqlite3
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path

STAGES = ("idea", "production", "recording", "mixing", "mastering", "released")
STAGE_LABELS = {stage: stage.capitalize() for stage in STAGES}
STAGE_RANK = {stage: index for index, stage in enumerate(STAGES)}
# Labeled estimates used until the history has evidence for a stage.
DEFAULT_STAGE_HOURS = {"idea": 4.0, "production": 8.0, "recording": 4.0, "mixing": 4.0, "mastering": 1.0}
PACE_WINDOW_DAYS = 28
MIN_PACE_HOURS_PER_WEEK = 1.0
MIN_DUE_DAYS, MAX_DUE_DAYS = 3, 42
RECENT_DAYS = 45
MIN_SUGGEST_SECONDS = 15 * 60
MAX_TASK_LENGTH = 200

# Live dialogs and empty sets that the window-title parser reports as projects.
NOISE_NAMES = {"", "save", "menu", "message manager_popup", "input config", "save synplant patch", "bounce..."}

_MASTER = re.compile(r"\bmaster(ing)?\b", re.IGNORECASE)
_MIX = re.compile(r"\bmix(ing)?\b", re.IGNORECASE)
_TRAILING = (
    re.compile(r"\s*\[[^\]]*\]$"),                       # Live backup timestamps
    re.compile(r"\s+\d{1,2}\.\d{1,2}(\.\d+)?$"),         # Live version resaves: "12.4"
    re.compile(r"\s+v\d+$"),                             # "v2"
    re.compile(r"\s+stems$"),
    re.compile(r"[\s\-–_]*\b(mixing|mix|mastering|master)(\s+session)?$"),
    re.compile(r"[\s\-–_]+$"),
)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS song_state (
        song_key      TEXT PRIMARY KEY,
        stage         TEXT NOT NULL DEFAULT '',
        on_deck       INTEGER NOT NULL DEFAULT 0,
        on_deck_since REAL,
        snoozed_until TEXT NOT NULL DEFAULT '',
        due_date      TEXT NOT NULL DEFAULT '',
        next_task     TEXT NOT NULL DEFAULT '',
        released_at   REAL,
        updated_at    REAL NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_song_state_single_deck ON song_state(on_deck) WHERE on_deck = 1",
    """CREATE TABLE IF NOT EXISTS song_events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        song_key TEXT NOT NULL,
        kind     TEXT NOT NULL,
        value    TEXT NOT NULL DEFAULT '',
        at       REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_song_events_key ON song_events(song_key, at)",
    """CREATE TABLE IF NOT EXISTS song_tasks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        song_key   TEXT NOT NULL,
        text       TEXT NOT NULL,
        due_date   TEXT NOT NULL DEFAULT '',
        done_at    REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_song_tasks_key ON song_tasks(song_key, done_at)",
)


def ensure_song_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)


def is_noise(name: str) -> bool:
    folded = (name or "").strip().casefold()
    return folded in NOISE_NAMES or folded.startswith("untitled")


def file_stage(name: str):
    """'mastering' or 'mixing' for stage-split files; None for the song file."""
    if _MASTER.search(name or ""):
        return "mastering"
    if _MIX.search(name or ""):
        return "mixing"
    return None


def song_key(name: str) -> str:
    key = " ".join((name or "").casefold().split())
    previous = None
    while key != previous:
        previous = key
        for pattern in _TRAILING:
            key = pattern.sub("", key)
    return key


_VERSION_SUFFIX = re.compile(r"(\s*\[[^\]]*\]|\s+\d{1,2}\.\d{1,2}(\.\d+)?)+$")
TRACK_STAGES = STAGES[:-1]  # Released completes the track rather than being a step on it.


def display_title(name: str) -> str:
    """Set name without Live-version resave or backup suffixes; the full name stays in tooltips."""
    return _VERSION_SUFFIX.sub("", name or "").strip() or (name or "")


def _local_date(now: float) -> date:
    return datetime.fromtimestamp(now).date()


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _optional_rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []  # Legacy or not-yet-created table.


def load_songs(db_path, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.OperationalError:
        return []
    with conn:
        rows = _optional_rows(conn, """
            SELECT project_name, COALESCE(SUM(active_seconds), 0),
                   MAX(COALESCE(last_seen_time, end_time, start_time)),
                   COALESCE(SUM(CASE WHEN start_time >= ? THEN active_seconds END), 0)
            FROM sessions GROUP BY project_name""", (now - PACE_WINDOW_DAYS * 86400,))
        aliases = dict(_optional_rows(conn, "SELECT alias_name, canonical_name FROM project_aliases"))
        legacy = {name: (status or "", due or "") for name, status, due in
                  _optional_rows(conn, "SELECT project_name, status, due_date FROM project_metadata")}
        states = {row[0]: row for row in _optional_rows(conn, """
            SELECT song_key, stage, on_deck, on_deck_since, snoozed_until, due_date, next_task
            FROM song_state""")}
    conn.close()

    grouped: dict[str, dict] = {}
    for name, seconds, last_seen, recent in rows:
        if is_noise(name):
            continue
        key = song_key(aliases.get(name, name))
        if not key:
            continue
        song = grouped.setdefault(key, {"key": key, "files": []})
        song["files"].append({"name": name, "seconds": float(seconds or 0), "last_seen": float(last_seen or 0),
                              "recent_seconds": float(recent or 0), "stage": file_stage(name)})

    result = []
    for key, song in grouped.items():
        files = sorted(song["files"], key=lambda f: (-f["last_seen"], f["name"]))
        song["files"] = files
        song_files = [f for f in files if f["stage"] is None]
        seconds = sum(f["seconds"] for f in files)
        inferred = ("mastering" if any(f["stage"] == "mastering" for f in files)
                    else "mixing" if any(f["stage"] == "mixing" for f in files)
                    else "idea" if seconds < 3600 else "production")
        _, manual, on_deck, since, snoozed, due, task = states.get(key, (key, "", 0, None, "", "", ""))
        manual = manual if manual in STAGE_RANK else ""
        stage = manual if manual and STAGE_RANK[manual] > STAGE_RANK[inferred] else inferred
        stage_type = stage if stage in ("mixing", "mastering") else None
        stage_files = [f for f in files if f["stage"] == stage_type] or files
        statuses = [legacy.get(f["name"], ("", ""))[0].casefold() for f in files]
        legacy_due = min((d for d in (legacy.get(f["name"], ("", ""))[1] for f in files) if _parse_date(d)), default="")
        legacy_settled = any(s in ("finished", "abandoned") for s in statuses)
        song.update(
            title=(song_files or files)[0]["name"],
            seconds=seconds,
            recent_seconds=sum(f["recent_seconds"] for f in files),
            last_seen=files[0]["last_seen"],
            inferred_stage=inferred,
            manual_stage=manual,
            stage=stage,
            stage_seconds=sum(f["seconds"] for f in stage_files),
            open_name=stage_files[0]["name"],
            on_deck=bool(on_deck),
            on_deck_since=since,
            snoozed_until=snoozed or "",
            due_date=due or "",
            legacy_due_date=legacy_due,
            next_task=task or "",
            settled=stage == "released" or (legacy_settled and not on_deck and not manual),
        )
        result.append(song)
    return sorted(result, key=lambda s: (-s["last_seen"], s["title"].casefold()))


def current_deck(songs):
    return next((s for s in songs if s["on_deck"]), None)


def _effective_due(song):
    return _parse_date(song.get("due_date")) or _parse_date(song.get("legacy_due_date"))


def suggestions(songs, today: date, limit: int = 3) -> list[dict]:
    """Nearest deadline first, then furthest along among recent songs, then recency."""
    recent_cutoff = datetime.combine(today - timedelta(days=RECENT_DAYS), datetime.min.time()).timestamp()
    candidates = []
    for song in songs:
        snoozed = _parse_date(song["snoozed_until"])
        if song["on_deck"] or song["settled"] or song["seconds"] < MIN_SUGGEST_SECONDS or (snoozed and snoozed >= today):
            continue
        due = _effective_due(song)
        if due:
            rank = (0, due.toordinal(), 0, 0)
        elif song["last_seen"] >= recent_cutoff:
            rank = (1, 0, -STAGE_RANK[song["stage"]], -song["last_seen"])
        else:
            rank = (2, 0, 0, -song["last_seen"])
        candidates.append((rank, song["title"].casefold(), song))
    return [song for _, _, song in sorted(candidates, key=lambda c: (c[0], c[1]))][:limit]


def stage_estimate_hours(songs, stage: str) -> float:
    """Median of your own completed stage files when there is evidence."""
    samples = []
    if stage in ("mixing", "mastering"):
        for song in songs:
            stages = {f["stage"] for f in song["files"]}
            if {"mixing", "mastering"} <= stages:
                total = sum(f["seconds"] for f in song["files"] if f["stage"] == stage)
                if total > 0:
                    samples.append(total / 3600)
    if len(samples) >= 3:
        return round(statistics.median(samples), 1)
    return DEFAULT_STAGE_HOURS.get(stage, DEFAULT_STAGE_HOURS["production"])


def weekly_pace_hours(songs) -> float:
    recent = sum(s.get("recent_seconds", 0) for s in songs) / 3600
    return max(recent / (PACE_WINDOW_DAYS / 7), MIN_PACE_HOURS_PER_WEEK)


def propose_due_date(songs, song, today: date):
    stage = song["stage"] if song["stage"] in DEFAULT_STAGE_HOURS else "production"
    estimate = stage_estimate_hours(songs, stage)
    if stage in ("mixing", "mastering"):
        remaining = max(estimate - song.get("stage_seconds", 0) / 3600, 1.0)
    else:
        remaining = estimate  # Time spent in the song file does not measure what is left.
    pace = weekly_pace_hours(songs)
    days = min(max(math.ceil(remaining / pace * 7), MIN_DUE_DAYS), MAX_DUE_DAYS)
    return today + timedelta(days=days), f"≈{remaining:.1f}h left at {pace:.1f}h/week"


@contextmanager
def _connect(db_path):
    """One committed (or rolled back) write transaction; never caches the connection."""
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        with conn:
            ensure_song_schema(conn)
            yield conn
    finally:
        conn.close()


def _upsert(conn, key, now, **fields):
    conn.execute("INSERT INTO song_state(song_key, updated_at) VALUES(?, ?) ON CONFLICT(song_key) DO NOTHING", (key, now))
    if fields:
        assignments = ", ".join(f"{column} = ?" for column in fields)
        conn.execute(f"UPDATE song_state SET {assignments}, updated_at = ? WHERE song_key = ?",
                     (*fields.values(), now, key))


def _event(conn, key, kind, value, now):
    conn.execute("INSERT INTO song_events(song_key, kind, value, at) VALUES(?, ?, ?, ?)", (key, kind, value or "", now))


def _find(songs, key):
    song = next((s for s in songs if s["key"] == key), None)
    if song is None:
        raise ValueError("That song is no longer in the tracker")
    return song


def put_on_deck(db_path, key, now: float | None = None, snooze_previous_days: int | None = None) -> str:
    """Make one song On Deck (snoozing the previous one if asked); returns its due date."""
    now = time.time() if now is None else now
    today = _local_date(now)
    songs = load_songs(db_path, now=now)
    song = _find(songs, key)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT song_key FROM song_state WHERE on_deck = 1").fetchone()
        if row and row[0] != key:
            snoozed = (today + timedelta(days=snooze_previous_days)).isoformat() if snooze_previous_days else ""
            _upsert(conn, row[0], now, on_deck=0, on_deck_since=None, snoozed_until=snoozed)
            _event(conn, row[0], "snoozed" if snoozed else "off_deck", snoozed, now)
        if not row or row[0] != key:
            _upsert(conn, key, now, on_deck=1, on_deck_since=now, snoozed_until="")
            _event(conn, key, "on_deck", song["stage"], now)
        due = song["due_date"]
        if not _parse_date(due):
            legacy = _parse_date(song["legacy_due_date"])
            due = (legacy if legacy and legacy >= today else propose_due_date(songs, song, today)[0]).isoformat()
            _upsert(conn, key, now, due_date=due)
            _event(conn, key, "due_date", due, now)
    return due


def snooze_deck(db_path, days: int, now: float | None = None) -> None:
    now = time.time() if now is None else now
    until = (_local_date(now) + timedelta(days=days)).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT song_key FROM song_state WHERE on_deck = 1").fetchone()
        if row:
            _upsert(conn, row[0], now, on_deck=0, on_deck_since=None, snoozed_until=until)
            _event(conn, row[0], "snoozed", until, now)


def set_stage(db_path, key, stage: str, now: float | None = None) -> None:
    if stage not in STAGE_RANK:
        raise ValueError(f"Unknown stage: {stage}")
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        if stage == "released":
            _upsert(conn, key, now, stage=stage, on_deck=0, on_deck_since=None, released_at=now)
        else:
            _upsert(conn, key, now, stage=stage)
        _event(conn, key, "stage", stage, now)


def set_due_date(db_path, key, value: str, now: float | None = None) -> None:
    if value and not _parse_date(value):
        raise ValueError("Use a YYYY-MM-DD date")
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        _upsert(conn, key, now, due_date=value or "")
        _event(conn, key, "due_date", value, now)


def set_next_task(db_path, key, text: str, now: float | None = None) -> None:
    text = " ".join((text or "").split())[:MAX_TASK_LENGTH]
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        _upsert(conn, key, now, next_task=text)
        _event(conn, key, "next_task", text, now)


def complete_next_task(db_path, key, now: float | None = None) -> str:
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        row = conn.execute("SELECT next_task FROM song_state WHERE song_key = ?", (key,)).fetchone()
        task = row[0] if row else ""
        if task:
            _upsert(conn, key, now, next_task="")
            _event(conn, key, "task_done", task, now)
    return task


def list_song_tasks(db_path, key, now: float | None = None) -> list[dict]:
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.OperationalError:
        return []
    with conn:
        open_rows = _optional_rows(
            conn,
            """SELECT id, text, due_date, done_at, created_at
               FROM song_tasks
               WHERE song_key = ? AND done_at IS NULL
               ORDER BY CASE WHEN due_date = '' THEN 1 ELSE 0 END ASC,
                        due_date ASC,
                        created_at ASC,
                        id ASC""",
            (key,),
        )
        done_rows = _optional_rows(
            conn,
            """SELECT id, text, due_date, done_at, created_at
               FROM song_tasks
               WHERE song_key = ? AND done_at IS NOT NULL
               ORDER BY done_at DESC, id DESC
               LIMIT 20""",
            (key,),
        )
    conn.close()
    return [
        {
            "id": int(row[0]),
            "text": row[1],
            "due_date": row[2] or "",
            "done": row[3] is not None,
            "done_at": row[3],
            "created_at": float(row[4]),
        }
        for row in (*open_rows, *done_rows)
    ]


def open_task_counts(db_path, now: float | None = None) -> dict[str, int]:
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.OperationalError:
        return {}
    with conn:
        rows = _optional_rows(
            conn,
            "SELECT song_key, COUNT(*) FROM song_tasks WHERE done_at IS NULL GROUP BY song_key",
        )
    conn.close()
    return {row[0]: int(row[1]) for row in rows}


def _normalize_task_text(text: str) -> str:
    normalized = " ".join((text or "").split())[:MAX_TASK_LENGTH]
    if not normalized:
        raise ValueError("Write the task first")
    return normalized


def _normalize_task_due(due_date: str) -> str:
    due = (due_date or "").strip()
    if due and not _parse_date(due):
        raise ValueError("Use a YYYY-MM-DD date")
    return due


def add_song_task(db_path, key, text: str, due_date: str = "", now: float | None = None) -> int:
    clean_text = _normalize_task_text(text)
    clean_due = _normalize_task_due(due_date)
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO song_tasks(song_key, text, due_date, done_at, created_at, updated_at)
               VALUES(?, ?, ?, NULL, ?, ?)""",
            (key, clean_text, clean_due, now, now),
        )
        _event(conn, key, "song_task_added", clean_text, now)
        return int(cur.lastrowid)


def update_song_task(
    db_path,
    key,
    task_id: int,
    text: str | None = None,
    due_date: str | None = None,
    done: bool | None = None,
    now: float | None = None,
) -> None:
    clean_text = _normalize_task_text(text) if text is not None else None
    clean_due = _normalize_task_due(due_date) if due_date is not None else None
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT text, due_date, done_at FROM song_tasks WHERE id = ? AND song_key = ?",
            (task_id, key),
        ).fetchone()
        if row is None:
            raise ValueError("That task is no longer there")
        new_text = row[0] if clean_text is None else clean_text
        new_due = row[1] if clean_due is None else clean_due
        new_done_at = (float(now) if done else None) if done is not None else row[2]
        conn.execute(
            "UPDATE song_tasks SET text = ?, due_date = ?, done_at = ?, updated_at = ? WHERE id = ? AND song_key = ?",
            (new_text, new_due, new_done_at, now, task_id, key),
        )
        if text is not None or due_date is not None:
            _event(conn, key, "song_task_updated", new_text, now)
        if done is not None:
            _event(conn, key, "song_task_done" if done else "song_task_reopened", new_text, now)


def delete_song_task(db_path, key, task_id: int, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT text FROM song_tasks WHERE id = ? AND song_key = ?",
            (task_id, key),
        ).fetchone()
        if row is None:
            raise ValueError("That task is no longer there")
        conn.execute("DELETE FROM song_tasks WHERE id = ? AND song_key = ?", (task_id, key))
        _event(conn, key, "song_task_deleted", row[0], now)


def deck_display(song, today: date) -> dict:
    due = _effective_due(song)
    overdue = bool(due and due < today)
    if not due:
        due_text, primary, secondary = "No due date", "No due date", "Set one"
    elif overdue:
        late = (today - due).days
        plural = 's' if late != 1 else ''
        due_text, primary, secondary = f"Overdue by {late} day{plural}", "Overdue", f"by {late} day{plural}"
    elif due == today:
        due_text, primary, secondary = "Due today", "Due today", f"{due:%b} {due.day}"
    elif due == today + timedelta(days=1):
        due_text, primary, secondary = "Due tomorrow", "Due tomorrow", f"{due:%b} {due.day}"
    else:
        left = (due - today).days
        due_text, primary, secondary = f"Due {due:%b} {due.day} · {left} days left", f"Due {due:%b} {due.day}", f"{left} days left"
    return {
        "key": song["key"],
        "title": song["title"],
        "stage": song["stage"],
        "stage_label": STAGE_LABELS.get(song["stage"], song["stage"]),
        "next": f"Next: {song['next_task']}" if song.get("next_task") else "Add a next task…",
        "has_next": bool(song.get("next_task")),
        "due": due_text,
        "due_primary": primary,
        "due_secondary": secondary,
        "overdue": overdue,
        "display_title": display_title(song["title"]),
        "task": song.get("next_task") or "",
        "stage_index": TRACK_STAGES.index(song["stage"]) if song["stage"] in TRACK_STAGES else len(TRACK_STAGES),
        "stage_count": len(TRACK_STAGES),
    }


def panel_model(songs, today: date) -> dict:
    """Plain data for the menu-bar panel: either the On Deck song or pickable suggestions."""
    label = lambda s: (s["key"], display_title(s["title"]), STAGE_LABELS.get(s["stage"], s["stage"]))
    model = {
        "mode": "empty",
        "suggestions": [label(s) for s in suggestions(songs, today, limit=3)],
        "more": [label(s) for s in suggestions(songs, today, limit=6)],
        "all": [label(s) for s in songs if not s["settled"] and not s["on_deck"]][:40],
        "stages": [(stage, STAGE_LABELS[stage]) for stage in STAGES],
    }
    deck = current_deck(songs)
    if deck:
        model.update(deck_display(deck, today), mode="set")
    return model
