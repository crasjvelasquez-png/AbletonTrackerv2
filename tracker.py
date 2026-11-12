#!/usr/bin/env python3
"""Ableton Live project time tracker daemon."""

import sqlite3
import subprocess
import time
import signal
import sys
import re
import threading
import ctypes
import ctypes.util
import fcntl
import json
import math
import uuid

from tracking_policy import (Observation, POLICY_VERSION, INPUT_ALLOWANCE, PLAY_ALLOWANCE,
                             LISTENING_ALLOWANCE, PROJECT_GRACE,
                             EVIDENCE_TTL, POLICY_TEXT)
from activity_store import ensure_activity_schema, load_activity_rows
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

STATE_TRACKING = "tracking"
STATE_IDLE_PAUSED = "idle_paused"
STATE_PAUSED = "paused"
STATE_ABLETON_OPEN = "ableton_open"
STATE_ABLETON_CLOSED = "ableton_closed"

POLL_INTERVAL = 5  # lightweight reconciliation; native events wake the loop sooner
IDLE_THRESHOLD = INPUT_ALLOWANCE  # compatibility alias; policy lives in tracking_policy.py
CLEANUP_INTERVAL = 15 * 60  # seconds between background cleanup passes
SESSION_CONDENSE_GAP_SECONDS = 5 * 60  # Deprecated, use DEFAULT_CONDENSE_GAP_MINUTES
DEFAULT_CONDENSE_GAP_MINUTES = 15
TRACKER_MAX_RETRIES = 5
TRACKER_MAX_BACKOFF = 60
# Gaps shorter than this are polling/project-switch jitter, not real breaks.
# 60s keeps focus-break data free of noise while preserving real pauses.
MIN_PAUSE_SECONDS = 60
PAUSE_REASON_IDLE = "idle"
PAUSE_REASON_MANUAL = "manual"
PAUSE_REASON_ABLETON_CLOSED = "ableton_closed"
PAUSE_REASON_PROJECT_SWITCH = "project_switch"
PAUSE_REASON_APP_RESTART = "app_restart"
PAUSE_REASON_UNKNOWN = "unknown"
# In-session reasons: no mouse/keyboard input for 60s+ while the session
# stayed open (audio kept it alive). The _listening variant means audio
# played at some point during the idle span (passive listening); plain
# no_input means silence throughout.
PAUSE_REASON_NO_INPUT = "no_input"
PAUSE_REASON_NO_INPUT_LISTENING = "no_input_listening"
PAUSE_KIND_BETWEEN = "between"
PAUSE_KIND_WITHIN = "within"
DB_PATH = Path.home() / ".ableton_tracker" / "sessions.db"
PAUSE_FILE = Path.home() / ".ableton_tracker" / "paused"
UNTITLED_NAMES = {"untitled", "untitled project"}
LIVE_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(Ableton\s*)?Live\s*\d*\s*(Suite|Standard|Lite|Intro|Trial)?\s*$",
    flags=re.IGNORECASE,
)
NON_PROJECT_TITLE_PATTERNS = [
    r"^export audio(?:\/video)?(?:\.\.\.)?$",
    r"^exporting\b",
    r"^rendering\b",
    r"^preferences$",
    r"^settings$",
    r"^open live set$",
    r"^save live set$",
    r"^save live set as$",
    r"^collect all and save$",
    r"^manage files$",
    # Generic accessibility/UI labels exposed as transient Live window titles.
    r"^tooltip$",
    r"^missing value$",
    r"^window$",
]
_NON_PROJECT_TITLE_RES = [re.compile(p, re.IGNORECASE) for p in NON_PROJECT_TITLE_PATTERNS]
_WHITESPACE_RE = re.compile(r"\s+")
_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_ALS_SUFFIX_RE = re.compile(r"\.als\s*$", re.IGNORECASE)


def is_untitled_project_name(name: str | None) -> bool:
    raw = (name or "").strip()
    if not raw:
        return False
    return _WHITESPACE_RE.sub(" ", raw).strip().lower() in UNTITLED_NAMES


def ensure_pauses_table(conn: sqlite3.Connection) -> None:
    """Create the pauses table used for LLM-readable focus-break analysis.

    Two kinds of rows share this table so one chronological query serves
    the whole focus timeline:
    - kind='between': gap between two activities (pause_start = previous
      activity end, pause_end = next activity start).
    - kind='within': idle span inside one open session (no mouse/keyboard
      input for 60s+ while audio kept the session alive). session_id
      points at the owning session row.
    Short polling jitter (< MIN_PAUSE_SECONDS) is never stored; callers
    should use record_pause() so the threshold and overlap guard stay
    in one place.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pauses (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            pause_start      REAL    NOT NULL,
            pause_end        REAL    NOT NULL,
            duration_seconds REAL    NOT NULL,
            reason           TEXT    NOT NULL DEFAULT '',
            prev_project     TEXT,
            next_project     TEXT,
            created_at       REAL    NOT NULL,
            kind             TEXT    NOT NULL DEFAULT 'between',
            session_id       INTEGER
        )
    """)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pauses)").fetchall()
    }
    if "kind" not in columns:
        conn.execute("ALTER TABLE pauses ADD COLUMN kind TEXT NOT NULL DEFAULT 'between'")
    if "session_id" not in columns:
        conn.execute("ALTER TABLE pauses ADD COLUMN session_id INTEGER")
    # Backfill only when needed: an unconditional UPDATE would open a
    # write transaction on every read path and block nested connections.
    needs_backfill = conn.execute(
        "SELECT 1 FROM pauses WHERE kind IS NULL OR kind = '' LIMIT 1"
    ).fetchone()
    if needs_backfill:
        conn.execute(
            "UPDATE pauses SET kind = 'between' WHERE kind IS NULL OR kind = ''"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pauses_start ON pauses(pause_start)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pauses_end ON pauses(pause_end)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pauses_session ON pauses(session_id)"
    )
    conn.commit()


def setup_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name   TEXT    NOT NULL,
                start_time     REAL    NOT NULL,
                last_seen_time REAL,
                end_time       REAL,
                active_seconds REAL    DEFAULT 0
            )
        """)
        ensure_pauses_table(conn)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "last_seen_time" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_seen_time REAL")
        conn.execute("""
            UPDATE sessions
            SET last_seen_time = COALESCE(last_seen_time, end_time, start_time)
            WHERE last_seen_time IS NULL
        """)
        if "notes" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN notes TEXT DEFAULT ''")
        if "todo_notes" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN todo_notes TEXT DEFAULT ''")
        if "todos_json" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN todos_json TEXT DEFAULT '[]'")
        conn.execute("""
            UPDATE sessions SET notes = COALESCE(notes, '') WHERE notes IS NULL
        """)
        conn.execute("""
            UPDATE sessions SET todo_notes = COALESCE(todo_notes, '') WHERE todo_notes IS NULL
        """)
        conn.execute("""
            UPDATE sessions SET todos_json = COALESCE(todos_json, '[]') WHERE todos_json IS NULL
        """)
        ensure_activity_schema(conn)
        conn.commit()


def is_phantom_session_name(name: str | None) -> bool:
    """Return True for closed rows that are clearly not real Live sets."""
    raw = (name or "").strip()
    if not raw:
        return False

    lowered = _WHITESPACE_RE.sub(" ", raw).strip().lower()
    if lowered in UNTITLED_NAMES:
        return False

    if any(r.match(lowered) for r in _NON_PROJECT_TITLE_RES):
        return True

    # Slash characters cannot appear in Live set filenames on macOS, but
    # plugin/editor windows often use them in their titles.
    if "/" in raw:
        return True

    return False


def _phantom_session_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT id, project_name
        FROM sessions
        WHERE end_time IS NOT NULL
        """
    ).fetchall()
    return [row[0] for row in rows if is_phantom_session_name(row[1])]


def count_phantom_sessions() -> int:
    if not DB_PATH.exists():
        return 0
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        return len(_phantom_session_ids(conn))


def cleanup_phantom_sessions() -> dict:
    """Delete clearly invalid historical rows left by older builds."""
    if not DB_PATH.exists():
        return {"ok": True, "deleted": 0}

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        ids = _phantom_session_ids(conn)
        if not ids:
            return {"ok": True, "deleted": 0}

        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", ids)
        conn.commit()
        return {"ok": True, "deleted": len(ids)}


def cleanup_untitled_sessions() -> dict:
    """Delete closed sessions whose project name is Untitled / Untitled Project."""
    if not DB_PATH.exists():
        return {"ok": True, "deleted": 0}

    placeholders = ",".join("?" * len(UNTITLED_NAMES))
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        cur = conn.execute(
            f"""
            DELETE FROM sessions
            WHERE end_time IS NOT NULL
              AND LOWER(TRIM(project_name)) IN ({placeholders})
            """,
            tuple(UNTITLED_NAMES),
        )
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount}


def close_stale_open_sessions() -> int:
    """Close leftover open rows from a previous tracker instance."""
    if not DB_PATH.exists():
        return 0

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        rows = conn.execute(
            """
            SELECT id, COALESCE(last_seen_time, start_time)
            FROM sessions
            WHERE end_time IS NULL
            """
        ).fetchall()
        if not rows:
            return 0

        conn.executemany(
            """
            UPDATE sessions
            SET last_seen_time = COALESCE(last_seen_time, ?),
                end_time = ?
            WHERE id = ?
            """,
            [(ts, ts, row_id) for row_id, ts in rows],
        )
        conn.commit()
        return len(rows)


def session_end_time(session) -> float:
    """Best-known session boundary for closed or still-open rows."""
    return float(
        session.get("end_time")
        or session.get("last_seen_time")
        or session.get("start_time")
        or 0
    )


def record_pause(
    pause_start: float,
    pause_end: float,
    reason: str = PAUSE_REASON_UNKNOWN,
    prev_project: str | None = None,
    next_project: str | None = None,
    kind: str = PAUSE_KIND_BETWEEN,
    session_id: int | None = None,
) -> int | None:
    """Persist one completed pause (between or within activities).

    Returns the new pause id, or None when the gap is too short to be a
    real break, inverted, or already covered by an existing pause row.
    Idempotent: overlapping intervals are skipped so resume/retry paths
    and history backfills can call this safely. Adjacent intervals
    (end == start) are allowed so a within-session pause ending at a
    session close sits cleanly next to the between-session pause that
    starts there.
    """
    try:
        start = float(pause_start or 0)
        end = float(pause_end or 0)
    except (TypeError, ValueError):
        return None
    duration = end - start
    if start <= 0 or end <= 0 or duration < MIN_PAUSE_SECONDS:
        return None
    kind = kind or PAUSE_KIND_BETWEEN

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        ensure_pauses_table(conn)
        existing = conn.execute(
            """
            SELECT id FROM pauses
            WHERE pause_start < ? AND pause_end > ?
            LIMIT 1
            """,
            (end, start),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            """
            INSERT INTO pauses
                (pause_start, pause_end, duration_seconds, reason,
                 prev_project, next_project, created_at, kind, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (start, end, duration, (reason or PAUSE_REASON_UNKNOWN),
             prev_project, next_project, time.time(), kind, session_id),
        )
        conn.commit()
        return cur.lastrowid


def get_pauses(
    since: float | None = None,
    until: float | None = None,
    limit: int = 500,
    kind: str | None = None,
) -> list[dict]:
    """Return recorded pauses ordered by start time (oldest first)."""
    if not DB_PATH.exists():
        return []
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        ensure_pauses_table(conn)
        conn.row_factory = sqlite3.Row
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("pause_end >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("pause_start <= ?")
            params.append(float(until))
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT id, pause_start, pause_end, duration_seconds,
                   reason, prev_project, next_project, created_at,
                   kind, session_id
            FROM pauses
            {where}
            ORDER BY pause_start ASC
            LIMIT ?
            """,
            (*params, max(int(limit or 0), 0) if limit else 500),
        ).fetchall()
        return [dict(row) for row in rows]


def rebuild_pauses_from_sessions(reason: str = PAUSE_REASON_UNKNOWN) -> dict:
    """Backfill pauses from existing session history (idempotent).

    Derives gaps between consecutive closed sessions ordered by end time.
    Gaps below MIN_PAUSE_SECONDS and intervals already covered by a pause
    row are skipped, so this is safe to run on real user data.
    """
    if not DB_PATH.exists():
        return {"ok": True, "created": 0, "skipped": 0}
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        ensure_pauses_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT project_name, start_time,
                   COALESCE(end_time, last_seen_time, start_time) AS end_ts
            FROM sessions
            ORDER BY end_ts ASC, start_time ASC, id ASC
            """
        ).fetchall()
    created = 0
    skipped = 0
    previous = None
    for row in rows:
        if previous is not None:
            prev_end = float(previous["end_ts"] or 0)
            cur_start = float(row["start_time"] or 0)
            if cur_start > prev_end and (cur_start - prev_end) >= MIN_PAUSE_SECONDS:
                pause_id = record_pause(
                    prev_end,
                    cur_start,
                    reason=reason,
                    prev_project=previous["project_name"],
                    next_project=row["project_name"],
                )
                if pause_id is not None:
                    created += 1
                else:
                    skipped += 1
        previous = row
    return {"ok": True, "created": created, "skipped": skipped}


def _format_clock(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def _format_span(seconds: float) -> str:
    total = int(round(float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


def build_focus_timeline(
    sessions: list[dict] | None = None,
    pauses: list[dict] | None = None,
    since: float | None = None,
    until: float | None = None,
    limit: int = 500,
) -> list[dict]:
    """Interleave sessions and pauses into one chronological LLM-ready list.

    When sessions/pauses are not supplied, they are read from the DB.
    Each entry is a plain dict with ISO/local timestamps and durations so
    an LLM can reason about focus rhythm without extra parsing.
    """
    if sessions is None or pauses is None:
        if not DB_PATH.exists():
            return []
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
            ensure_pauses_table(conn)
            conn.row_factory = sqlite3.Row
            session_clauses: list[str] = []
            session_params: list[float] = []
            if since is not None:
                session_clauses.append(
                    "COALESCE(end_time, last_seen_time, start_time) >= ?"
                )
                session_params.append(float(since))
            if until is not None:
                session_clauses.append("start_time <= ?")
                session_params.append(float(until))
            session_where = (
                f"WHERE {' AND '.join(session_clauses)}" if session_clauses else ""
            )
            if sessions is None:
                session_rows = conn.execute(
                    f"""
                    SELECT id, project_name, start_time, last_seen_time,
                           end_time, active_seconds
                    FROM sessions
                    {session_where}
                    ORDER BY start_time ASC
                    LIMIT ?
                    """,
                    (*session_params, max(int(limit or 0), 0) if limit else 500),
                ).fetchall()
                sessions = [dict(row) for row in session_rows]
            if pauses is None:
                pauses = get_pauses(since=since, until=until, limit=limit)

    events: list[dict] = []
    for row in sessions or []:
        start = float(row.get("start_time") or 0)
        end = float(
            row.get("end_time") or row.get("last_seen_time") or start or 0
        )
        events.append({
            "kind": "session",
            "project": row.get("project_name"),
            "start": start,
            "start_local": _format_clock(start) if start else "",
            "end": end,
            "end_local": _format_clock(end) if end else "",
            "active_seconds": float(row.get("active_seconds") or 0),
            "active_label": _format_span(row.get("active_seconds") or 0),
            "open": row.get("end_time") is None,
        })
    for row in pauses or []:
        start = float(row.get("pause_start") or 0)
        end = float(row.get("pause_end") or 0)
        pause_kind = row.get("kind") or PAUSE_KIND_BETWEEN
        events.append({
            # "pause" + pause_kind="between" is a BREAK between work;
            # "pause" + pause_kind="within" is an in-session PAUSE.
            "kind": "pause",
            "pause_kind": pause_kind,
            "reason": (row.get("reason") or PAUSE_REASON_UNKNOWN),
            "prev_project": row.get("prev_project"),
            "next_project": row.get("next_project"),
            "session_id": row.get("session_id"),
            "start": start,
            "start_local": _format_clock(start) if start else "",
            "end": end,
            "end_local": _format_clock(end) if end else "",
            "duration_seconds": float(row.get("duration_seconds") or (end - start)),
            "duration_label": _format_span(
                row.get("duration_seconds") or (end - start)
            ),
        })
    events.sort(key=lambda event: (event.get("start") or 0, event.get("kind") or ""))
    return events


def format_focus_timeline_markdown(events: list[dict] | None = None, **query) -> str:
    """Render the focus timeline as Markdown — the easiest paste-into-LLM format.

    Pass an event list or the same keyword filters as build_focus_timeline()
    (since/until/limit). Example prompt to use with the output:
    "Here is my Ableton activity with breaks. Where do I lose focus?"
    """
    if events is None:
        events = build_focus_timeline(**query)
    lines = [
        "# Ableton focus timeline",
        "",
        "Sessions are tracked work. BREAKs are gaps between work; PAUSEs are "
        "idle spans inside one session (no mouse/keyboard input while the "
        "session stayed open).",
        "Times are local. Durations: sessions show active time, breaks/pauses show idle length.",
        "",
    ]
    if not events:
        lines.append("_No sessions or pauses recorded yet._")
        return "\n".join(lines) + "\n"
    for event in events:
        if event.get("kind") == "pause":
            if event.get("pause_kind") == PAUSE_KIND_WITHIN:
                project = (
                    event.get("prev_project")
                    or event.get("next_project")
                    or "—"
                )
                lines.append(
                    f"- PAUSE {event.get('start_local')} → {event.get('end_local')} "
                    f"({event.get('duration_label')}, reason={event.get('reason')}) "
                    f"| during: {project}"
                )
            else:
                lines.append(
                    f"- BREAK {event.get('start_local')} → {event.get('end_local')} "
                    f"({event.get('duration_label')}, reason={event.get('reason')}) "
                    f"| before: {event.get('prev_project') or '—'} "
                    f"| after: {event.get('next_project') or '—'}"
                )
        else:
            open_mark = " (ongoing)" if event.get("open") else ""
            lines.append(
                f"- WORK {event.get('start_local')} → {event.get('end_local')} "
                f"({event.get('active_label')}) "
                f"| project: {event.get('project')}{open_mark}"
            )
    lines.append("")
    work_count = sum(1 for event in events if event.get("kind") == "session")
    breaks = [e for e in events if e.get("kind") == "pause"
              and e.get("pause_kind") != PAUSE_KIND_WITHIN]
    within = [e for e in events if e.get("kind") == "pause"
              and e.get("pause_kind") == PAUSE_KIND_WITHIN]
    total_breaks = sum(float(e.get("duration_seconds") or 0) for e in breaks)
    total_within = sum(float(e.get("duration_seconds") or 0) for e in within)
    lines.append(
        f"Summary: {work_count} work sessions, {len(breaks)} breaks "
        f"({_format_span(total_breaks)}), {len(within)} in-session pauses "
        f"({_format_span(total_within)})."
    )
    return "\n".join(lines) + "\n"


def get_condense_gap_seconds() -> float:
    """Read the user-configured session condense gap from DB, or fallback to default."""
    try:
        if DB_PATH.exists():
            with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = 'session_condense_gap_minutes'"
                ).fetchone()
                if row:
                    return float(row["value"]) * 60
    except sqlite3.OperationalError as e:
        if 'no such table' not in str(e):
            print(f"[{_ts()}] Warning: failed to read session_condense_gap_minutes: {e}")
    return DEFAULT_CONDENSE_GAP_MINUTES * 60


def condense_recent_sessions(
    rows,
    max_gap_seconds: float | None = None,
):
    """Merge adjacent same-project fragments separated by short pauses."""
    if max_gap_seconds is None:
        max_gap_seconds = get_condense_gap_seconds()
    condensed = []

    for raw_row in rows:
        row = dict(raw_row)
        row["active_seconds"] = float(row.get("active_seconds") or 0)
        row["start_time"] = float(row.get("start_time") or 0)
        row_id = row.get("id")
        row["session_ids"] = [int(row_id)] if row_id is not None else []

        if condensed:
            current = condensed[-1]
            gap_seconds = current["start_time"] - session_end_time(row)
            same_project = current["project_name"] == row["project_name"]
            if same_project and gap_seconds < max_gap_seconds:
                current["start_time"] = min(current["start_time"], row["start_time"])
                current["active_seconds"] += row["active_seconds"]
                current["last_seen_time"] = max(
                    session_end_time(current),
                    session_end_time(row),
                )
                current["session_ids"].extend(row["session_ids"])
                if row.get("end_time") is None:
                    current["end_time"] = None
                continue

        condensed.append(row)

    return condensed


def allocate_session_activity(
    start_time: float,
    end_time: float,
    active_seconds: float,
) -> list[tuple[str, int, float]]:
    """Split a session's active time across the hour/day buckets it spans."""
    active = max(float(active_seconds or 0), 0.0)
    start = float(start_time or 0)
    end = max(float(end_time or start), start)

    if active <= 0:
        return []

    start_dt = datetime.fromtimestamp(start).replace(minute=0, second=0, microsecond=0)
    if end <= start:
        return [(start_dt.date().isoformat(), start_dt.hour, active)]

    allocations = []
    span_seconds = end - start
    cursor = start

    while cursor < end:
        cursor_dt = datetime.fromtimestamp(cursor)
        next_hour_dt = cursor_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        next_boundary = min(end, next_hour_dt.timestamp())
        slice_seconds = max(next_boundary - cursor, 0.0)
        if slice_seconds > 0:
            allocated = active * (slice_seconds / span_seconds)
            allocations.append((cursor_dt.date().isoformat(), cursor_dt.hour, allocated))
        cursor = next_boundary

    return allocations


def build_activity_rollups(rows) -> tuple[dict[str, float], dict[tuple[str, int], float]]:
    daily = defaultdict(float)
    hourly = defaultdict(float)

    for row in rows:
        start_time = row["start_time"]
        end_time = row["end_time"] or row["last_seen_time"] or row["start_time"]
        active_seconds = row["active_seconds"] or 0

        for day_key, hour, seconds in allocate_session_activity(start_time, end_time, active_seconds):
            daily[day_key] += seconds
            hourly[(day_key, hour)] += seconds

    return daily, hourly


def day_seconds(target_day: date) -> float:
    return _all_daily_totals().get(target_day.isoformat(), 0.0)


_ROLLUP_CACHE = {}


def _all_daily_totals() -> dict[str, float]:
    if not DB_PATH.exists():
        return {}
    revisions=[]
    for path in (DB_PATH,Path(str(DB_PATH)+'-wal')):
        try:
            stat=path.stat()
            revisions.append((stat.st_ino,stat.st_mtime_ns,stat.st_size))
        except FileNotFoundError:
            revisions.append(None)
    key=(str(DB_PATH),tuple(revisions),date.today(),time.tzname)
    if _ROLLUP_CACHE.get('key')==key:
        return dict(_ROLLUP_CACHE['daily'])
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        rows = load_activity_rows(conn)
    daily, _ = build_activity_rollups(rows)
    _ROLLUP_CACHE.update(key=key,daily=dict(daily))
    return daily


def compute_streak_days(
    daily_totals: dict[str, float], today: date | None = None
) -> int:
    """Consecutive days with activity ending today (or yesterday right after midnight)."""
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    active_days = {day for day, seconds in daily_totals.items() if seconds > 0}
    streak = 0
    cursor = None
    if today.isoformat() in active_days:
        cursor = today
    elif yesterday.isoformat() in active_days:
        cursor = yesterday
    while cursor and cursor.isoformat() in active_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def streak_days(today: date | None = None) -> int:
    """Current streak from the DB."""
    return compute_streak_days(_all_daily_totals(), today)


def production_run_days(today: date | None = None) -> int:
    """Use the dashboard's 15-minute qualifying-day rule for menu and reminders."""
    totals = {day: seconds for day, seconds in _all_daily_totals().items() if seconds >= 900}
    return compute_streak_days(totals, today)


def yesterday_seconds() -> float:
    """Tracked seconds for yesterday."""
    return day_seconds((date.today() - timedelta(days=1)))


def week_seconds(target_date: date | None = None) -> float:
    """Tracked seconds for the current tracking week (respects week_start_weekday setting)."""
    if not DB_PATH.exists():
        return 0.0
    target_date = target_date or date.today()
    raw = get_app_setting("week_start_weekday")
    week_start_weekday = int(raw) if raw is not None else 4
    week_start = target_date - timedelta(
        days=(target_date.weekday() - week_start_weekday) % 7
    )
    week_end = week_start + timedelta(days=7)  # exclusive upper bound

    daily = _all_daily_totals()
    week_days = {(week_start + timedelta(days=i)).isoformat() for i in range(7)}
    return sum(daily.get(day, 0.0) for day in week_days)


def get_app_setting(key: str, default: str | None = None) -> str | None:
    """Read an app_setting from the DB (re-exports from dashboard for convenience)."""
    from dashboard import get_app_setting as _gas

    return _gas(key, default)


def _live_pid() -> int | None:
    try:
        from AppKit import NSRunningApplication
        apps=NSRunningApplication.runningApplicationsWithBundleIdentifier_('com.ableton.live')
        return int(apps[0].processIdentifier()) if len(apps)==1 else None
    except ImportError:
        try:
            result=subprocess.run(['pgrep','-x','Live'],capture_output=True,text=True,timeout=1)
            pids=result.stdout.strip().splitlines()
            return int(pids[0]) if result.returncode==0 and len(pids)==1 else None
        except (ValueError,OSError,subprocess.SubprocessError):
            return None


def is_ableton_running() -> bool:
    return _live_pid() is not None


_error_state: dict[str, str | None] = {}
_ERROR_STATE_LOCK = threading.Lock()


def _log_transient_error(
    channel: str,
    message: str | None,
    fail_label: str,
    recover_label: str,
    suffix: str = "",
):
    """De-duplicate repeated subprocess error logs across polls.

    Only prints when the message changes from the previous one for `channel`,
    so a persistent failure logs once and a recovery logs once.
    """
    with _ERROR_STATE_LOCK:
        if message == _error_state.get(channel):
            return
        _error_state[channel] = message
    if message:
        print(f"[{_ts()}] {fail_label}: {message}{suffix}")
    else:
        print(f"[{_ts()}] {recover_label}")


@dataclass(frozen=True)
class TrackerStatus:
    state: str = STATE_ABLETON_CLOSED
    paused: bool = False
    running: bool = False
    project_name: str | None = None
    resume_hint_project: str | None = None
    hid_idle_seconds: float = 0.0
    audio_active: bool = False
    audio_idle_seconds: float = float("inf")
    idle_paused: bool = False
    checked_at: float = 0.0
    idle_pause_started_at: float | None = None
    reason: str = "closed"
    message: str = "Ableton not running"
    evidence_source: str = "unavailable"
    cutoff: float | None = None
    policy_version: int = POLICY_VERSION
    verified_until: float = 0.0
    source_error: str | None = None


def parse_osc_playing(data: bytes) -> bool | None:
    import struct
    def read_string(offset):
        end=data.index(b'\0',offset)
        value=data[offset:end]
        padded=(end+4)&~3
        if padded>len(data) or any(data[end:padded]):
            raise ValueError('Invalid OSC padding')
        return value,padded
    try:
        address,pos=read_string(0)
        tags,pos=read_string(pos)
        if address!=b'/live/song/get/is_playing': return None
        if tags in (b',T',b',F') and len(data)==pos:
            return tags==b',T'
        if tags==b',i' and len(data)==pos+4:
            value=struct.unpack('>i',data[pos:])[0]
            return bool(value) if value in (0,1) else None
    except (ValueError,IndexError):
        pass
    return None


def is_ableton_playing_osc() -> bool | None:
    """Optional read-only OSC query. The response port belongs to one client."""
    import socket
    msg=b'/live/song/get/is_playing\0\0\0'+b',\0\0\0'
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(('127.0.0.1',11001))
            sock.settimeout(0.15)
            sock.sendto(msg,('127.0.0.1',11000))
            deadline=time.monotonic()+0.15
            while time.monotonic()<deadline:
                data,peer=sock.recvfrom(1024)
                if peer!=('127.0.0.1',11000): continue
                parsed=parse_osc_playing(data)
                if parsed is not None: return parsed
        except (OSError,ValueError):
            pass
    return None


def get_live_window_titles() -> list[str]:
    """Return visible Ableton Live window titles.

    Logs (once) when System Events refuses — without that, an Accessibility
    permission failure looks identical to "Ableton has no open windows".
    """
    script = """
tell application "System Events"
    if not (exists process "Live") then return ""
    if (count of windows of process "Live") = 0 then return ""
    set AppleScript's text item delimiters to linefeed
    return (name of every window of process "Live") as text
end tell
"""
    try:
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as e:
        _log_transient_error(
            "title", str(e), "window-title lookup failed", "window-title lookup recovered"
        )
        return []

    if r.returncode != 0:
        err = r.stderr.strip()
        hint = ""
        if "1719" in err or "assistive access" in err.lower() or "-25211" in err:
            hint = (
                "  → Grant Accessibility to this Python in System Settings → "
                "Privacy & Security → Accessibility "
                f"({sys.executable})"
            )
        _log_transient_error(
            "title",
            err,
            "osascript error reading Live windows",
            "window-title lookup recovered",
            suffix=hint,
        )
        return []

    _log_transient_error(
        "title", None, "window-title lookup failed", "window-title lookup recovered"
    )
    return [title.strip() for title in r.stdout.splitlines() if title.strip()]


def parse_project_title(title: str) -> str | None:
    """Extract a real Live set name from a window title.

    Live sometimes exposes bare window titles like "My Song" or "Untitled"
    instead of the fuller "My Song - Ableton Live 12 Suite" form. We accept
    both, while still rejecting obvious dialogs and plugin/editor windows.
    """
    raw = (title or "").strip()
    if not raw:
        return None

    if LIVE_TITLE_SUFFIX_RE.search(raw):
        name = LIVE_TITLE_SUFFIX_RE.sub("", raw).strip()
    else:
        # Live set names come from filenames, so "/" is a strong signal this is
        # a plugin/editor window rather than a project.
        if "/" in raw:
            return None
        name = raw

    name = _BRACKET_RE.sub("", name).strip()
    name = _PAREN_RE.sub("", name).strip()
    name = _ALS_SUFFIX_RE.sub("", name).strip()
    name = _WHITESPACE_RE.sub(" ", name).strip(" -–—")

    if not name or name.lower() in {"", "ableton live", "live"}:
        return None

    lowered = name.lower()
    if any(r.match(lowered) for r in _NON_PROJECT_TITLE_RES):
        return None

    return name


def get_project_name(current_project: str | None = None, titles: list[str] | None = None) -> str | None:
    """Resolve the current Ableton project while ignoring transient windows.

    Returns None if no reliable project title is visible; callers should keep
    the current session alive rather than rotating to a guess.
    """
    candidates: list[str] = []
    distinct: dict[str, str] = {}
    distinct_live: dict[str, str] = {}
    for title in (get_live_window_titles() if titles is None else titles):
        parsed = parse_project_title(title)
        if not parsed:
            continue
        candidates.append(parsed)
        key = parsed.casefold()
        distinct.setdefault(key, parsed)
        if LIVE_TITLE_SUFFIX_RE.search(title):
            distinct_live.setdefault(key, parsed)

    if not candidates:
        return None

    if current_project:
        # Draft-to-saved conversion: when tracking an Untitled session,
        # prefer a single non-Untitled candidate as a reliable rename
        # (the user saved the draft with a real name). Check this
        # before the "still visible" branch because both titles may
        # appear briefly while Live is switching window titles.
        if is_untitled_project_name(current_project):
            non_untitled_live = {
                k: v for k, v in distinct_live.items()
                if not is_untitled_project_name(v)
            }
            if len(non_untitled_live) == 1:
                return next(iter(non_untitled_live.values()))
            non_untitled_distinct = {
                k: v for k, v in distinct.items()
                if not is_untitled_project_name(v)
            }
            if len(non_untitled_distinct) == 1:
                return next(iter(non_untitled_distinct.values()))
            if non_untitled_distinct or non_untitled_live:
                return None

        if current_project in candidates:
            return current_project

        # A single canonical Live title is a reliable project switch.
        if len(distinct_live) == 1:
            return next(iter(distinct_live.values()))
        if len(distinct_live) > 1:
            return None

        # When Live is only exposing bare titles, accept a single distinct
        # parsed candidate as a real project switch. This fixes the common case
        # where the main project window title is just the set name.
        if len(distinct) == 1:
            return next(iter(distinct.values()))

        # Otherwise keep the current session alive rather than rotating to a
        # new bare title guess just because the visible windows are transient.
        return None

    if len(distinct_live) == 1:
        return next(iter(distinct_live.values()))
    if len(distinct_live) > 1:
        return None
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def acquire_writer_lock(db_path=None):
    path = (db_path or DB_PATH).with_suffix('.writer.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


class Tracker:
    def __init__(self, writer_lock=True, observer=None):
        self._writer_lock = acquire_writer_lock() if writer_lock else None
        if writer_lock and self._writer_lock is None:
            raise RuntimeError("Another tracker already owns this database")
        self.observer = observer
        self._deadline = None
        self._previous_observation = None
        self._input_barrier = float('-inf')
        self._last_project_at = None
        self._lost_observability_at = None
        self._last_play_at = None
        self._play_mode = False
        # Provisional trip to another app: last credited moment before leaving,
        # and the moment Live came back (counts as attendance like an input).
        self._away_since = None
        self._return_at = None
        self._interval_id = None
        self._pause_interval_id = None
        self._interval_start_wall = None
        self._interval_credit = 0.0
        self._interval_reason = 'working'
        self._interval_source = 'direct'
        self._status = TrackerStatus()
        self._status_payload = None
        self._needs_new_input = False
        self._pending_start = None
        self.session_id = None
        self.project_name = None
        self.last_tick = None
        self.last_tick_mono = None
        self.resume_hint_project = None
        self.next_cleanup_at = 0.0
        self.last_audio_active = 0.0
        self.last_running = False
        self.last_hid_idle = 0.0
        self.last_audio_is_active = False
        self.last_audio_idle = float("inf")
        self.last_checked_at = 0.0
        self.last_state = STATE_ABLETON_CLOSED
        self._consecutive_failures = 0
        self._last_error: str | None = None
        # Pending pause: set when a session closes, finalized when the
        # next session starts. Survives only in memory; restarts fall
        # back to the latest closed session end_time in _start().
        self._pending_pause_start: float | None = None
        self._pending_pause_project: str | None = None
        self._pending_pause_reason: str = PAUSE_REASON_UNKNOWN
        # Open in-session idle span: no input for 60s+ while the session
        # stays open (audio kept it alive). Finalized when input resumes
        # or the session closes. _within_last_end is a watermark so a new
        # span never reaches back across an already-recorded interval.
        self._within_pause_start: float | None = None
        self._within_pause_project: str | None = None
        self._within_pause_session_id: int | None = None
        self._within_last_end: float | None = None

        # --- One-time initialization (unified entry point) ---
        setup_db()
        stale = close_stale_open_sessions()
        if stale:
            print(f"[{_ts()}] closed {stale} stale open session{'s' if stale != 1 else ''}")
        self.maybe_run_cleanup(force=True)

    def _rename(self, new_project: str):
        if self.session_id is None:
            self.project_name = new_project
            return
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
            conn.execute(
                "UPDATE sessions SET project_name=? WHERE id=? AND end_time IS NULL",
                (new_project, self.session_id),
            )
            conn.commit()
        old = self.project_name
        self.project_name = new_project
        print(f"[{_ts()}] ↻  {old} → {new_project}")

    def _finalize_within_pause(self, now: float, end: float | None = None) -> None:
        """Record the open in-session idle span, if it is a real pause."""
        start = self._within_pause_start
        if start is None:
            return
        project = self._within_pause_project
        session_id = self._within_pause_session_id
        self._within_pause_start = None
        self._within_pause_project = None
        self._within_pause_session_id = None
        end_ts = now if end is None else min(max(float(end), start), now)
        self._within_last_end = end_ts
        if end_ts - start < MIN_PAUSE_SECONDS:
            return
        listening = (
            self.last_audio_active and self.last_audio_active >= start
        )
        record_pause(
            start, end_ts,
            reason=(PAUSE_REASON_NO_INPUT_LISTENING if listening
                    else PAUSE_REASON_NO_INPUT),
            prev_project=project, next_project=project,
            kind=PAUSE_KIND_WITHIN, session_id=session_id,
        )

    def _discard_within_pause(self) -> None:
        """Drop the open in-session span without recording (idle probe blind)."""
        self._within_pause_start = None
        self._within_pause_project = None
        self._within_pause_session_id = None

    def _within_pause_update(self, now: float, hid_idle_seconds: float) -> None:
        """Open/extend/finalize the in-session idle span for this poll.

        Opens when input has been idle 60s+ while a session is open;
        finalizes when input resumes (end ≈ last-input time). Spans are
        approximate to fallback poll granularity (±5s) — precise enough for focus
        review, and active_seconds accounting is left untouched.
        """
        if self.session_id is None:
            # Orphaned by an external close (_tick rowcount 0): finalize
            # what we observed rather than leaking the open span.
            if self._within_pause_start is not None:
                self._finalize_within_pause(now, end=now)
            return
        try:
            hid = float(hid_idle_seconds)
        except (TypeError, ValueError):
            self._discard_within_pause()
            return
        if hid != hid or hid == float("inf"):
            # Idle probe blind — don't fabricate precision.
            self._discard_within_pause()
            return
        if hid >= MIN_PAUSE_SECONDS:
            if self._within_pause_start is None:
                start = now - hid
                if self._within_last_end is not None:
                    start = max(start, self._within_last_end)
                self._within_pause_start = start
                self._within_pause_project = self.project_name
                self._within_pause_session_id = self.session_id
            return
        if self._within_pause_start is not None:
            self._finalize_within_pause(now, end=now - hid)

    def _finalize_pending_pause(self, next_project: str, now: float) -> None:
        """Record the gap between the last close and this start, if real."""
        # Any pre-start idle is either recorded as a between-pause here or
        # negligible: move the within watermark so a later in-session span
        # never reaches back across this boundary.
        if self._within_last_end is None or now > self._within_last_end:
            self._within_last_end = now
        pause_start = self._pending_pause_start
        prev_project = self._pending_pause_project
        reason = self._pending_pause_reason or PAUSE_REASON_UNKNOWN
        if pause_start is not None:
            self._pending_pause_start = None
            self._pending_pause_project = None
            self._pending_pause_reason = PAUSE_REASON_UNKNOWN
            record_pause(
                pause_start, now, reason=reason,
                prev_project=prev_project, next_project=next_project,
            )
            return
        # No in-memory close (process restart, crash, or first run after
        # history exists): derive the gap from the latest closed session.
        try:
            with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT project_name,
                           COALESCE(end_time, last_seen_time, start_time) AS end_ts
                    FROM sessions
                    WHERE COALESCE(end_time, last_seen_time, start_time) < ?
                    ORDER BY COALESCE(end_time, last_seen_time, start_time) DESC
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
        except Exception:
            return
        if row:
            record_pause(
                float(row["end_ts"] or 0), now,
                reason=PAUSE_REASON_APP_RESTART,
                prev_project=row["project_name"],
                next_project=next_project,
            )

    def _start(self, project: str, at=None, mono=None):
        now = time.time() if at is None else at
        now_mono = time.monotonic() if mono is None else mono
        pause_id=self._pause_interval_id
        self._finalize_pending_pause(project, now)
        if self._pending_start is None:
            self._pending_start=(uuid.uuid4().hex,project,now,now_mono)
        token,project,now,now_mono=self._pending_start
        # Historical estimates stay separate; covered rows retain exact idle gaps.
        gap_limit=get_condense_gap_seconds()
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing=conn.execute('SELECT id FROM sessions WHERE start_token=?',(token,)).fetchone()
            if existing:
                sid=existing[0]
            else:
                recent=conn.execute('''SELECT id FROM sessions s WHERE project_name=?
                    AND policy_version=? AND end_time IS NOT NULL
                    AND ?-end_time>=0 AND ?-end_time<?
                    AND EXISTS(SELECT 1 FROM activity_intervals a WHERE a.session_id=s.id
                               AND a.credited_seconds=0)
                    ORDER BY end_time DESC LIMIT 1''',
                    (project,POLICY_VERSION,now,now,gap_limit)).fetchone()
                if recent:
                    sid=recent[0]
                    conn.execute('UPDATE sessions SET last_seen_time=?,end_time=NULL,start_token=? WHERE id=?',
                                 (now,token,sid))
                else:
                    cur=conn.execute('INSERT INTO sessions(project_name,start_time,last_seen_time,policy_version,start_token) VALUES(?,?,?,?,?)',
                                     (project,now,now,POLICY_VERSION,token))
                    sid=cur.lastrowid
            if pause_id:
                conn.execute('UPDATE activity_intervals SET end_time=MAX(start_time,?) WHERE id=?', (now,pause_id))
            conn.commit()
        self._pending_start=None
        self.session_id, self.project_name = sid, project
        self.last_tick, self.last_tick_mono = now, now_mono
        self._new_interval(now)
        self.resume_hint_project = None
        print(f"[{_ts()}] ▶  {project}")

    def _new_interval(self, wall):
        self._interval_id = uuid.uuid4().hex
        self._interval_start_wall = wall
        self._interval_credit = 0.0

    def _tick(self, at=None, mono=None, until=None):
        if self.session_id is None:
            return
        wall = time.time() if at is None else at
        current_mono = time.monotonic() if mono is None else mono
        end_mono = min(current_mono, until) if until is not None else current_mono
        if self._deadline is not None:
            end_mono = min(end_mono, self._deadline)
        if self._previous_observation is not None:
            end_mono = min(end_mono,self._previous_observation.mono+EVIDENCE_TTL)
        # Direct callers have no observations; never grant an unbounded final gap.
        if self._previous_observation is None:
            end_mono = min(end_mono, self.last_tick_mono + INPUT_ALLOWANCE)
        elapsed = max(0.0, end_mono - self.last_tick_mono)
        if elapsed <= 0:
            return
        end_wall = self.last_tick + elapsed
        target = self._interval_credit + elapsed
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT end_time FROM sessions WHERE id=?', (self.session_id,)).fetchone()
            if row is None or row[0] is not None:
                conn.rollback()
                self.session_id = self.project_name = None
                self.last_tick = self.last_tick_mono = None
                self._needs_new_input = True
                return
            old = conn.execute('SELECT credited_seconds FROM activity_intervals WHERE id=?', (self._interval_id,)).fetchone()
            stored = old[0] if old else 0.0
            delta = max(0.0, target - stored)
            conn.execute('''INSERT INTO activity_intervals
                (id,session_id,start_time,end_time,credited_seconds,reason,evidence_source)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                end_time=excluded.end_time,credited_seconds=excluded.credited_seconds''',
                (self._interval_id,self.session_id,self._interval_start_wall,end_wall,target,
                 self._interval_reason,self._interval_source))
            conn.execute('UPDATE sessions SET active_seconds=active_seconds+?,last_seen_time=? WHERE id=? AND end_time IS NULL',
                         (delta,end_wall,self.session_id))
            conn.commit()
        # Only advance after commit. Stable interval IDs reconcile ambiguous commits.
        self._interval_credit = target
        self.last_tick, self.last_tick_mono = end_wall, end_mono

    def _close(self, preserve_resume_hint=False, reason=PAUSE_REASON_UNKNOWN, at=None, mono=None):
        if self.session_id is None:
            if not preserve_resume_hint:
                self.resume_hint_project = None
            return
        self._tick(at=at, mono=mono)
        if self.session_id is None:
            return
        now = self.last_tick
        project, sid = self.project_name, self.session_id
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
            cur = conn.execute('UPDATE sessions SET last_seen_time=?,end_time=? WHERE id=? AND end_time IS NULL',
                               (now,now,sid))
            row = conn.execute('SELECT active_seconds FROM sessions WHERE id=?',(sid,)).fetchone()
            discarded = bool(row and (row[0]<5 or (not preserve_resume_hint and is_untitled_project_name(project))))
            if discarded:
                conn.execute('DELETE FROM sessions WHERE id=?',(sid,))
            else:
                pause_id=f'pause-{sid}-{float(now).hex()}'
                conn.execute('INSERT OR IGNORE INTO activity_intervals(id,session_id,start_time,end_time,credited_seconds,reason,evidence_source) VALUES(?,?,?,?,0,?,?)',
                             (pause_id,sid,now,now,reason,self._interval_source))
            conn.commit()
        if not discarded:
            self._pause_interval_id=pause_id
        if self._pending_pause_start is None:
            self._pending_pause_start = now
            self._pending_pause_project = project
            self._pending_pause_reason = reason
        if discarded:
            self._discard_within_pause()
        else:
            self._finalize_within_pause(now,end=now)
        self.resume_hint_project = project if preserve_resume_hint else None
        self.session_id = self.project_name = None
        self.last_tick = self.last_tick_mono = None
        self._away_since = self._return_at = None
        self._interval_id = None
        print(f"[{_ts()}] ■  {project} ({reason})")

    def status(self) -> TrackerStatus:
        # Frozen snapshot: menu reads never wait behind detector/network operations.
        return self._status

    def _publish_status(self, o, reason, cutoff=None):
        messages = {
            'working': f"Working on: {self.project_name}",
            'listening': "Listening: Live is playing",
            'away': f"Away: counts if back in Live within {max(0, int((cutoff or o.mono)-o.mono))}s",
            'idle': (f'Paused: no playback for {int(PLAY_ALLOWANCE//60)}m' if self._play_mode
                     else f'Paused: no activity for {int(INPUT_ALLOWANCE//60)}m'),
            'listening_limit': 'Paused: listening limit reached',
            'background': 'Paused: another app is active',
            'locked': 'Paused: Mac locked or asleep',
            'manual': 'Paused',
            'unverified': 'Paused: cannot verify activity',
            'no_project': 'Ableton open (no verified project)',
            'awaiting_input': ('Paused: press play in Live to resume' if self._play_mode
                               else 'Paused: interact with Live to resume'),
            'closed': 'Ableton not running',
        }
        counting = self.session_id is not None and reason in ('working','listening','away')
        state = (STATE_TRACKING if counting else STATE_PAUSED if reason=='manual'
                 else STATE_ABLETON_CLOSED if not o.pid else STATE_ABLETON_OPEN
                 if reason=='no_project' else STATE_IDLE_PAUSED)
        idle = max(0.0,o.mono-o.input_at) if o.input_at is not None else 0.0
        cutoff_wall = o.wall + cutoff-o.mono if cutoff is not None else None
        verified = o.wall + min(EVIDENCE_TTL,max(0,cutoff-o.mono),max(0,(self._deadline or o.mono)-o.mono)) if counting and cutoff is not None else o.wall
        status = TrackerStatus(state=state,paused=reason=='manual',running=bool(o.pid),
            project_name=self.project_name,resume_hint_project=self.resume_hint_project,
            hid_idle_seconds=idle,audio_active=o.audio is True,
            audio_idle_seconds=max(0,o.mono-o.audio_at) if o.audio_at is not None else 0.0,
            idle_paused=state==STATE_IDLE_PAUSED,checked_at=o.wall,
            idle_pause_started_at=self._pending_pause_start if state==STATE_IDLE_PAUSED else None,
            reason=reason,message=messages[reason],evidence_source=o.source+('/'+o.audio_source if reason=='listening' and o.audio_source else ''),cutoff=cutoff_wall,
            verified_until=verified,source_error=o.error)
        from dataclasses import asdict
        payload=asdict(status)
        # Paused snapshots do not create recurring heartbeat writes.
        if not counting:
            payload.pop('checked_at',None); payload.pop('verified_until',None)
            payload.pop('hid_idle_seconds',None); payload.pop('audio_idle_seconds',None)
        encoded=json.dumps(payload,sort_keys=True,allow_nan=False)
        if encoded!=self._status_payload:
            with closing(sqlite3.connect(DB_PATH,timeout=10)) as conn:
                conn.execute('INSERT INTO tracker_status(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(encoded,))
                conn.commit()
            if self._status.reason != reason:
                print(f"[{_ts()}] {messages[reason]}" + (f" ({o.error})" if o.error else ''))
            self._status_payload=encoded
        self._status=status
        self.last_state=state
        self.last_running=bool(o.pid)
        self.last_hid_idle=idle
        self.last_checked_at=o.wall

    def poll_once(self, paused=False, observation=None):
        if observation is None:
            if self.observer is None:
                from activity_observer import NativeObserver
                self.observer=NativeObserver()
            observation=self.observer.snapshot(self.project_name,paused=paused)
        o=observation
        if not all(math.isfinite(x) for x in (o.wall,o.mono)):
            raise ValueError('Non-finite observation clock')
        prev=self._previous_observation
        if prev and o.mono < prev.mono:
            return  # delayed/out-of-order detector response
        old_session=self.session_id
        gap=bool(prev and o.mono-prev.mono>EVIDENCE_TTL)
        changed_pid=bool(prev and prev.pid!=o.pid)
        # Settle old evidence before new input can extend it. Never bridge a missed heartbeat.
        if old_session is not None and prev:
            end=min(o.mono,prev.mono+EVIDENCE_TTL,self._deadline or o.mono)
            if o.boundary is not None:
                end=min(end,max(prev.mono,o.boundary))
            self._tick(at=o.wall,mono=end)
            if self.session_id is None:  # externally closed/deleted
                self._input_barrier=max(self._input_barrier,o.mono)
        if changed_pid:
            self._last_project_at=None
        if (gap or changed_pid) and self.session_id is not None:
            self._close(preserve_resume_hint=not changed_pid,reason='unverified',at=o.wall,mono=self.last_tick_mono)
        # Policy v3: Live need not be in front. Other apps and background playback
        # count; only lock/sleep, quit, manual pause and inactivity stop the session.
        blocked=paused or not o.pid or o.locked or gap or changed_pid
        if blocked:
            barrier=(o.boundary if o.boundary is not None else
                     prev.mono+EVIDENCE_TTL if gap and not (paused or not o.pid or o.locked or changed_pid) else o.mono)
            self._input_barrier=max(self._input_barrier,barrier)
            self._needs_new_input=True
        reason=('manual' if paused else 'closed' if not o.pid else 'locked' if o.locked
                else 'unverified' if changed_pid or (gap and (o.input_at is None or o.input_at<=self._input_barrier)) else None)
        if o.observable:
            self._lost_observability_at=None
        elif self._lost_observability_at is None:
            self._lost_observability_at=o.mono
        project=o.project
        if project is not None:
            self._last_project_at=o.project_at if o.project_at is not None else o.mono
        elif self._last_project_at is not None and o.mono < self._last_project_at+PROJECT_GRACE:
            # Unreadable or ambiguous titles: same Live process, so keep the known set.
            project=self.project_name if self.session_id is not None else self.resume_hint_project
        if not project and reason is None:
            reason='unverified' if self.session_id is not None or self.resume_hint_project else 'no_project'
        input_at=(o.input_at if o.input_at is not None and math.isfinite(o.input_at)
                  and self._input_barrier<o.input_at<=o.mono else None)
        if (self._return_at is not None and self._input_barrier<self._return_at<=o.mono
                and (input_at is None or self._return_at>input_at)):
            input_at=self._return_at
        fresh_input=input_at is not None
        # Policy v5: the play button is the work signal. Counts while Live plays, in
        # front or not, and for PLAY_ALLOWANCE after it stops. Input alone counts only
        # when play state is unknown (AbletonOSC off), so tracking never goes dark.
        audio_known=(o.audio is not None and o.audio_at is not None and 0<=o.mono-o.audio_at<EVIDENCE_TTL)
        audio_fresh=audio_known and o.audio is True
        if audio_fresh and (self._last_play_at is None or o.audio_at>self._last_play_at):
            self._last_play_at=o.audio_at
        self._play_mode=audio_known
        # Play from before a lock, sleep or manual pause never bridges across it.
        play_at=(self._last_play_at if self._last_play_at is not None
                 and self._last_play_at>self._input_barrier else None)
        play_deadline=play_at+PLAY_ALLOWANCE if play_at is not None else float('-inf')
        input_deadline=input_at+INPUT_ALLOWANCE if fresh_input and not audio_known else float('-inf')
        evidence=play_at is not None or input_deadline>float('-inf')
        if reason is None and not evidence:
            reason=self._status.reason if self._status.reason in ('idle','listening_limit','unverified') else 'awaiting_input'
        cutoff=None
        attendance_cutoff=None
        if evidence:
            attendance_cutoff=max(input_deadline,play_deadline)
            cutoff=attendance_cutoff
            project_expired=False
            if o.project is None and self._last_project_at is not None:
                cutoff=min(cutoff,self._last_project_at+PROJECT_GRACE)
                project_expired=o.mono>=self._last_project_at+PROJECT_GRACE
            if not o.observable:
                cutoff=min(cutoff,self._lost_observability_at+EVIDENCE_TTL)
            if reason is None:
                if o.mono>=cutoff:
                    reason='unverified' if not o.observable or project_expired else 'idle'
                else:
                    reason='listening' if audio_fresh and not (fresh_input and o.mono<input_at+INPUT_ALLOWANCE) else 'working'
        self._deadline=cutoff
        active=reason in ('working','listening')
        if active:
            if self.session_id is not None and self.last_tick_mono < o.mono-0.000001:
                self._close(preserve_resume_hint=True,reason='idle',at=o.wall,mono=self.last_tick_mono)
            if self.session_id is not None and project!=self.project_name:
                if is_untitled_project_name(self.project_name) and not is_untitled_project_name(project):
                    self._rename(project)
                else:
                    self._close(reason=PAUSE_REASON_PROJECT_SWITCH,at=o.wall,mono=self.last_tick_mono)
            if self.session_id is None:
                if old_session is not None and self._needs_new_input:
                    active=False; reason='awaiting_input'
                else:
                    self._start(project,at=o.wall,mono=o.mono)
            if active:
                self._needs_new_input=False
                interval_source=o.source+('/'+o.audio_source if reason=='listening' and o.audio_source else '')
                if (self._interval_reason!=reason or self._interval_source!=interval_source
                    or (self.last_tick is not None and abs(o.wall-self.last_tick-(o.mono-self.last_tick_mono))>1)):
                    self._new_interval(o.wall)
                    self.last_tick=o.wall
                self._interval_reason,self._interval_source=reason,interval_source
                self._within_pause_update(o.wall,max(0,o.mono-input_at) if fresh_input else 0)
        else:
            # No final catch-up: the previous snapshot was settled above.
            if self.session_id is not None:
                self._close(preserve_resume_hint=bool(o.pid) and not changed_pid,
                            reason=reason,at=o.wall,mono=self.last_tick_mono)
            if not o.pid:
                self.resume_hint_project=None
            if reason in ('idle','listening_limit','unverified','no_project'):
                self._input_barrier=max(self._input_barrier,input_at or o.input_at or float('-inf'))
                self._needs_new_input=True
        if o.audio is True:
            self.last_audio_active=o.wall
        self._previous_observation=o
        self._publish_status(o,reason,attendance_cutoff if active else None)

    def shutdown(self):
        try:
            if self.session_id is not None:
                self._close(preserve_resume_hint=True,reason=PAUSE_REASON_APP_RESTART)
            if self._previous_observation is not None and self._status.state==STATE_TRACKING:
                self._publish_status(self._previous_observation,'unverified')
        finally:
            if self.observer is not None:
                self.observer.close()
            if self._writer_lock is not None:
                self._writer_lock.close()
                self._writer_lock=None

    def maybe_run_cleanup(self, force: bool = False):
        now = time.time()
        if not force and now < self.next_cleanup_at:
            return

        result = cleanup_phantom_sessions()
        deleted = result.get("deleted", 0)
        if deleted:
            print(f"[{_ts()}] cleaned {deleted} phantom session{'s' if deleted != 1 else ''}")

        result = cleanup_untitled_sessions()
        deleted = result.get("deleted", 0)
        if deleted:
            print(f"[{_ts()}] cleaned {deleted} unsaved session{'s' if deleted != 1 else ''}")

        self.next_cleanup_at = now + CLEANUP_INTERVAL


    def run(self, stop_event: threading.Event | None = None, wake_interval: float = POLL_INTERVAL, _lock: threading.Lock | None = None):
        """Sole public loop entry point.

        Standalone (stop_event=None): registers OS signal handlers, runs forever.
        Threaded (stop_event + _lock provided): loops until the event is set; checks
        PAUSE_FILE each cycle and uses the lock for thread-safe access when the menu
        bar UI may call poll_now() concurrently.  Tracks consecutive failures with
        exponential backoff for the menu bar error display.
        """
        if stop_event is None:
            print(f"Ableton Tracker  |  poll every {POLL_INTERVAL}s  |  db: {DB_PATH}")
            print("Ctrl+C to stop\n")

            def shutdown(sig, frame):
                print("\nStopping tracker…")
                self.shutdown()
                sys.exit(0)

            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)

        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                paused = PAUSE_FILE.exists()
                if _lock is not None:
                    with _lock:
                        self.poll_once(paused=paused)

                else:
                    self.poll_once(paused=paused)

                self._consecutive_failures = 0
                self._last_error = None
            except Exception as e:
                self._consecutive_failures += 1
                self._last_error = str(e)
                if _lock is not None:
                    print(f"tracker error ({self._consecutive_failures}): {e}", file=sys.stderr)
                    if self._consecutive_failures >= TRACKER_MAX_RETRIES:
                        print("tracker: repeated failure, retrying with bounded backoff", file=sys.stderr)
                else:
                    print(f"[{_ts()}] error: {e}")
            wait = wake_interval
            observer=self.observer
            if observer is not None and observer.started:
                if self._status.reason in ('closed','background'):
                    wait=30.0
                if self._deadline is not None and self.session_id is not None:
                    wait=min(wait,max(0.1,self._deadline-time.monotonic()))
            if self._consecutive_failures>0:
                wait=min(2**min(self._consecutive_failures,6),TRACKER_MAX_BACKOFF)
            if observer is not None:
                observer.wake.wait(wait)
                observer.wake.clear()
            elif stop_event:
                stop_event.wait(wait)
            else:
                time.sleep(wait)
        self.shutdown()


if __name__ == "__main__":
    Tracker().run()
