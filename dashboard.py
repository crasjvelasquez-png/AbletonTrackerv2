#!/usr/bin/env python3
"""Ableton Tracker Dashboard — local web server."""

import os
import sqlite3
import json
import hashlib
import time
import traceback
import webbrowser
import threading
import gzip
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from tracker import (
    allocate_session_activity,
    build_activity_rollups,
    cleanup_phantom_sessions,
    cleanup_untitled_sessions,
    condense_recent_sessions,
    count_phantom_sessions,
    is_ableton_running,
    session_end_time,
    get_condense_gap_seconds,
)

DB_PATH = Path.home() / ".ableton_tracker" / "sessions.db"
MAX_REQUEST_SIZE = 1_048_576  # 1 MB
TEMPLATE_PATH = Path(__file__).with_name("templates") / "dashboard.html"
TEMPLATES_DIR = Path(__file__).with_name("templates").resolve()
STATIC_DIR = Path(__file__).with_name("static").resolve()
STATIC_CONTENT_TYPES = {
    ".js":   "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".json": "application/json; charset=utf-8",
}
PORT = 7421
UNTITLED_NAMES = {"untitled", "untitled project"}
MAX_CUSTOM_CATEGORIES = 12
CONFIG_CACHE_TTL_SECONDS = 60
RECENT_SESSION_PAGE_SIZE = 60
CONFIG_CACHE: dict[str, tuple[float, object]] = {}
PROJECT_STATUS_OPTIONS = {
    "idea": "Idea",
    "needs_work": "Needs Work",
    "in_progress": "In Progress",
    "finishing": "Finishing",
    "final_touches": "Final Touches",
    "finished": "Finished",
    "paused": "Paused",
    "abandoned": "Abandoned",
}
PROJECT_TYPE_OPTIONS = {
    "personal": "Personal",
    "client": "Client",
    "other": "Other",
}
PROJECT_PRIORITY_OPTIONS = {
    "low": "Low",
    "normal": "Normal",
    "high": "High",
}
PROJECT_TASK_STATUS_OPTIONS = {"open", "done"}
PROJECT_TASK_PRIORITY_OPTIONS = {"low", "normal", "high"}
PROJECT_TASK_PRIORITY_ORDER_SQL = "CASE pt.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 WHEN 'low' THEN 2 ELSE 1 END"
PLANNER_GOAL_TYPE_OPTIONS = {
    "sessions_per_week",
    "hours_per_week",
    "projects_finished_per_period",
    "touch_active_project_every_n_days",
}
PLANNER_GOAL_PERIOD_OPTIONS = {"week", "month"}
PLANNER_GOAL_SCOPE_OPTIONS = {"all", "project_type", "category", "project"}
ACTIVE_PROJECT_STATUSES = {"idea", "in_progress", "finishing", "final_touches"}
LEGACY_CATEGORY_KEYS = [
    "c4milo",
    "production",
    "mixing",
    "mastering",
    "instrumentation",
]
WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]
DEFAULT_WEEK_START_WEEKDAY = 4  # Friday
PRODUCTION_RUN_QUALIFYING_SECONDS = 15 * 60


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        yield conn
    finally:
        conn.close()


def calculate_production_run(daily_totals: dict, today: date) -> dict:
    """Return qualifying-day run state without mutating recorded activity."""
    qualifying_seconds = PRODUCTION_RUN_QUALIFYING_SECONDS

    def seconds_for(day_value: date) -> float:
        return float(daily_totals.get(day_value.isoformat(), 0) or 0)

    qualifying_days = set()
    for day_key, seconds in daily_totals.items():
        day_value = date.fromisoformat(day_key)
        if day_value <= today and float(seconds or 0) >= qualifying_seconds:
            qualifying_days.add(day_value)

    best_days = 0
    current_best = 0
    previous_day = None
    for day_value in sorted(qualifying_days):
        if previous_day is not None and day_value == previous_day + timedelta(days=1):
            current_best += 1
        else:
            current_best = 1
        best_days = max(best_days, current_best)
        previous_day = day_value

    cursor_day = today if today in qualifying_days else today - timedelta(days=1)
    current_days = 0
    while cursor_day in qualifying_days:
        current_days += 1
        cursor_day -= timedelta(days=1)

    today_seconds = seconds_for(today)
    today_qualifies = today_seconds >= qualifying_seconds
    recent_days = []
    for offset in range(6, -1, -1):
        day_value = today - timedelta(days=offset)
        seconds = seconds_for(day_value)
        qualifies = seconds >= qualifying_seconds
        if qualifies:
            state = "completed"
        elif day_value == today and seconds > 0:
            state = "partial"
        elif day_value == today:
            state = "today"
        else:
            state = "missed"
        recent_days.append({
            "day": day_value.isoformat(),
            "seconds": seconds,
            "qualifies": qualifies,
            "is_today": day_value == today,
            "state": state,
        })

    return {
        "current_days": current_days,
        "best_days": best_days,
        "qualifying_seconds": qualifying_seconds,
        "today_seconds": today_seconds,
        "today_qualifies": today_qualifies,
        "today_progress_ratio": min(today_seconds / qualifying_seconds, 1.0),
        "remaining_seconds": max(qualifying_seconds - today_seconds, 0.0),
        "recent_days": recent_days,
    }


# ─────────────────────────────────────────────────────────────
#  Mutations
# ─────────────────────────────────────────────────────────────

def clear_all_sessions() -> dict:
    if not DB_PATH.exists():
        return {"ok": True, "deleted": 0}
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE end_time IS NOT NULL")
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount}


def clear_unsaved_projects() -> dict:
    return cleanup_untitled_sessions()


def clear_phantom_sessions() -> dict:
    return cleanup_phantom_sessions()


CONSOLIDATE_RETURN_WINDOW_SECONDS = 30 * 60      # must return to same project within 30 min (vibe-checks)
CONSOLIDATE_MAX_SINGLE_INTERVENING_SECONDS = 15 * 60  # each vibe-check project must be ≤ 15 min
CONSOLIDATE_MAX_TOTAL_INTERVENING_SECONDS = 20 * 60   # total intervening time must be ≤ 20 min
CONSOLIDATE_SAME_PROJECT_IDLE_SECONDS = 90 * 60    # idle/break gap with no intervening projects


def consolidate_sessions() -> dict:
    """Merge same-day same-project fragments separated by short gaps.

    Only closed rows are considered; active sessions are never touched.
    Two merge modes:

    1. Same-project idle gaps (no intervening rows): merges across
       gaps ≤ CONSOLIDATE_SAME_PROJECT_IDLE_SECONDS (default 90 min).

    2. Vibe-check interruptions (intervening other-project rows between
       same-project fragments): merges when gap ≤ RETURN_WINDOW (30 min),
       each intervening fragment ≤ MAX_SINGLE_INTERVENING (15 min),
       and total intervening time ≤ MAX_TOTAL_INTERVENING (20 min).

    Chaining: after a merge the accumulated end_time is used for
    subsequent gap comparisons so multi-fragment chains merge correctly.
    """
    if not DB_PATH.exists():
        return {"ok": True, "merged": 0, "deleted": 0}

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, project_name, start_time, last_seen_time, end_time,
                   active_seconds, notes, todos_json
            FROM sessions
            WHERE end_time IS NOT NULL
            ORDER BY start_time ASC, id ASC
            """
        ).fetchall()

        if len(rows) < 2:
            return {"ok": True, "merged": 0, "deleted": 0}

        # last index per project (index into rows list)
        last_idx_by_project: dict[str, int] = {}
        absorb: dict[int, int] = {}  # absorbed_id -> survivor_id
        survivors: dict[int, dict] = {}  # survivor_id -> accumulated row state
        merged_count = 0

        for i, row in enumerate(rows):
            pid = int(row["id"])
            project = row["project_name"]
            start_ts = float(row["start_time"] or 0)
            end_ts = float(row["end_time"] or 0)
            last_seen_ts = float(row["last_seen_time"] or 0)
            active = float(row["active_seconds"] or 0)
            notes = (row["notes"] or "").strip()
            todos_raw = (row["todos_json"] or "[]")

            if project not in last_idx_by_project:
                last_idx_by_project[project] = i
                continue

            prev_idx = last_idx_by_project[project]

            # Resolve survivor chain to find the true survivor
            survivor_id = int(rows[prev_idx]["id"])
            while survivor_id in absorb:
                survivor_id = absorb[survivor_id]

            # Use accumulated survivor state for end/start times if available
            if survivor_id in survivors:
                survivor_state = survivors[survivor_id]
                prev_end_ts = survivor_state["end_time"]
                prev_start_ts = survivor_state["start_time"]
            else:
                prev_end_ts = float(rows[prev_idx]["end_time"] or 0)
                prev_start_ts = float(rows[prev_idx]["start_time"] or 0)

            # Must be same calendar day (local time)
            prev_day = datetime.fromtimestamp(prev_start_ts).date()
            cur_day = datetime.fromtimestamp(start_ts).date()
            if prev_day != cur_day:
                last_idx_by_project[project] = i
                continue

            gap_seconds = start_ts - prev_end_ts
            if gap_seconds <= 0:
                last_idx_by_project[project] = i
                continue

            # Determine merge mode based on whether there are *unabsorbed* intervening rows
            intervening_rows = [
                j for j in range(prev_idx + 1, i)
                if int(rows[j]["id"]) not in absorb
            ]

            if not intervening_rows:
                # Mode 1: same-project idle gap — no intervening projects
                if gap_seconds > CONSOLIDATE_SAME_PROJECT_IDLE_SECONDS:
                    last_idx_by_project[project] = i
                    continue
            else:
                # Mode 2: vibe-check interruption
                if gap_seconds > CONSOLIDATE_RETURN_WINDOW_SECONDS:
                    last_idx_by_project[project] = i
                    continue

                total_intervening = 0.0
                ok_to_merge = True
                for j in intervening_rows:
                    intervening = rows[j]
                    if intervening["project_name"] == project:
                        ok_to_merge = False
                        break
                    int_active = float(intervening["active_seconds"] or 0)
                    if int_active > CONSOLIDATE_MAX_SINGLE_INTERVENING_SECONDS:
                        ok_to_merge = False
                        break
                    total_intervening += int_active

                if not ok_to_merge or total_intervening > CONSOLIDATE_MAX_TOTAL_INTERVENING_SECONDS:
                    last_idx_by_project[project] = i
                    continue

            # Merge this row into the survivor
            if survivor_id not in survivors:
                survivors[survivor_id] = {
                    "start_time": prev_start_ts,
                    "end_time": max(prev_end_ts, end_ts),
                    "last_seen_time": max(
                        float(row["last_seen_time"] or 0) if int(rows[prev_idx]["id"]) == survivor_id else 0,
                        last_seen_ts,
                    ),
                    "active_seconds": float(rows[prev_idx]["active_seconds"] or 0),
                    "notes_list": [(rows[prev_idx]["notes"] or "").strip()],
                    "todos_map": {},
                }
                # Parse previous todos
                prev_todos = _parse_todos_json(rows[prev_idx]["todos_json"] or "[]")
                for t in prev_todos:
                    survivors[survivor_id]["todos_map"][t["text"]] = t["done"]

            state = survivors[survivor_id]
            state["end_time"] = max(state["end_time"], end_ts)
            state["last_seen_time"] = max(state["last_seen_time"], last_seen_ts)
            state["active_seconds"] += active
            if notes:
                state["notes_list"].append(notes)
            for t in _parse_todos_json(todos_raw):
                state["todos_map"][t["text"]] = t["done"]

            absorb[pid] = survivor_id
            merged_count += 1
            # Keep last_idx_by_project pointing to the earliest fragment so
            # chains continue through the accumulated survivor state.

        if not survivors:
            return {"ok": True, "merged": 0, "deleted": 0}

        # Write survivors and delete absorbed rows
        for survivor_id, state in survivors.items():
            merged_notes = " | ".join(
                n for n in state["notes_list"] if n
            )
            merged_todos = [
                {"text": txt, "done": done}
                for txt, done in state["todos_map"].items()
            ]
            merged_todos_json = json.dumps(merged_todos, separators=(",", ":"))
            conn.execute(
                """
                UPDATE sessions
                SET end_time = ?, last_seen_time = ?, active_seconds = ?,
                    notes = ?, todos_json = ?
                WHERE id = ?
                """,
                (
                    state["end_time"],
                    state["last_seen_time"],
                    state["active_seconds"],
                    merged_notes,
                    merged_todos_json,
                    survivor_id,
                ),
            )

        deleted_ids = list(absorb.keys())
        placeholders = ",".join("?" * len(deleted_ids))
        conn.execute(
            f"DELETE FROM sessions WHERE id IN ({placeholders})",
            deleted_ids,
        )
        deleted_count = conn.execute(
            f"SELECT changes()"
        ).fetchone()[0]
        conn.commit()

        return {
            "ok": True,
            "merged": merged_count,
            "deleted": deleted_count,
        }


def delete_sessions(session_ids) -> dict:
    if not isinstance(session_ids, list) or not session_ids:
        return {"error": "session_ids required"}

    clean_ids: list[int] = []
    for raw in session_ids:
        try:
            clean_ids.append(int(raw))
        except (TypeError, ValueError):
            return {"error": "invalid session id"}

    if not DB_PATH.exists():
        return {"ok": True, "deleted": 0, "skipped_live": 0}

    placeholders = ",".join("?" * len(clean_ids))
    with db_connection() as conn:
        live_rows = conn.execute(
            f"SELECT id FROM sessions WHERE id IN ({placeholders}) AND end_time IS NULL",
            clean_ids,
        ).fetchall()
        live_ids = {row[0] for row in live_rows}
        deletable = [i for i in clean_ids if i not in live_ids]

        deleted = 0
        if deletable:
            del_placeholders = ",".join("?" * len(deletable))
            cur = conn.execute(
                f"DELETE FROM sessions WHERE id IN ({del_placeholders}) AND end_time IS NOT NULL",
                deletable,
            )
            conn.commit()
            deleted = cur.rowcount

        return {"ok": True, "deleted": deleted, "skipped_live": len(live_ids)}


MAX_NOTES_LENGTH = 500


def _normalize_todos(todos) -> list[dict]:
    if not isinstance(todos, list):
        return []
    normalized = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        normalized.append({"text": text, "done": bool(item.get("done"))})
    return normalized


def _parse_todos_json(value) -> list[dict]:
    if not value:
        return []
    try:
        return _normalize_todos(json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def set_session_notes(session_id, notes: str, todo_notes: str = "", todos_json=None) -> dict:
    """Save notes and to-dos for a single session."""
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return {"error": "invalid session_id"}

    if not DB_PATH.exists():
        return {"error": "no data yet"}

    trimmed = (notes or "").strip()[:MAX_NOTES_LENGTH]

    with db_connection() as conn:
        ensure_sessions_notes_column(conn)
        if todos_json is not None:
            todos = _normalize_todos(todos_json)
            trimmed_todo_notes = " | ".join(todo["text"] for todo in todos)
            todos_text = json.dumps(todos, separators=(",", ":"))
            cur = conn.execute(
                "UPDATE sessions SET notes = ?, todo_notes = ?, todos_json = ? WHERE id = ?",
                (trimmed, trimmed_todo_notes, todos_text, sid),
            )
            conn.commit()
            if cur.rowcount == 0:
                return {"error": "session not found"}
            return {
                "ok": True,
                "session_id": sid,
                "notes": trimmed,
                "todo_notes": trimmed_todo_notes,
                "todos": todos,
            }
        elif todo_notes:
            trimmed_todo = (todo_notes or "").strip()
            cur = conn.execute(
                "UPDATE sessions SET notes = ?, todo_notes = ? WHERE id = ?",
                (trimmed, trimmed_todo, sid),
            )
            conn.commit()
            if cur.rowcount == 0:
                return {"error": "session not found"}
            return {
                "ok": True,
                "session_id": sid,
                "notes": trimmed,
                "todo_notes": trimmed_todo,
            }
        else:
            cur = conn.execute(
                "UPDATE sessions SET notes = ? WHERE id = ?",
                (trimmed, sid),
            )
            conn.commit()
            if cur.rowcount == 0:
                return {"error": "session not found"}
            return {
                "ok": True,
                "session_id": sid,
                "notes": trimmed,
            }


def clear_session_notes() -> dict:
    if not DB_PATH.exists():
        return {"ok": True, "updated": 0}
    with db_connection() as conn:
        ensure_sessions_notes_column(conn)
        cur = conn.execute("UPDATE sessions SET notes = '', todo_notes = '', todos_json = '[]'")
        conn.commit()
        return {"ok": True, "updated": cur.rowcount}


def get_last_session_todos(project_name: str) -> dict:
    project = (project_name or "").strip()
    if not project or not DB_PATH.exists():
        return {"project_name": project, "todos": []}
    with db_connection() as conn:
        ensure_sessions_notes_column(conn)
        conn.row_factory = sqlite3.Row
        
        canonical_row = conn.execute("SELECT canonical_name FROM project_aliases WHERE alias_name = ?", (project,)).fetchone()
        canonical_project = canonical_row[0] if canonical_row else project
        aliases = conn.execute("SELECT alias_name FROM project_aliases WHERE canonical_name = ?", (canonical_project,)).fetchall()
        names = [canonical_project] + [r[0] for r in aliases]
        placeholders = ",".join("?" * len(names))
        
        row = conn.execute(
            f"""
            SELECT COALESCE(pa.canonical_name, s.project_name) AS project_name, s.todos_json
            FROM sessions s
            LEFT JOIN project_aliases pa ON s.project_name = pa.alias_name
            WHERE s.project_name IN ({placeholders})
            ORDER BY s.start_time DESC, s.id DESC
            LIMIT 1
            """,
            names,
        ).fetchone()
    if not row:
        return {"project_name": project, "todos": []}
    return {"project_name": row["project_name"], "todos": _parse_todos_json(row["todos_json"])}


def get_session_notes_entry(session_id, project_name: str = "") -> dict:
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return {"error": "invalid session_id"}

    if not DB_PATH.exists():
        return {"error": "no data yet"}

    project = (project_name or "").strip()
    with db_connection() as conn:
        ensure_sessions_notes_column(conn)
        conn.row_factory = sqlite3.Row
        current = conn.execute(
            """
            SELECT id, project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json
            FROM sessions
            WHERE id = ?
            """,
            (sid,),
        ).fetchone()
        if not current:
            return {"error": "session not found"}
        if not project:
            project = current["project_name"]

        canonical_row = conn.execute("SELECT canonical_name FROM project_aliases WHERE alias_name = ?", (project,)).fetchone()
        canonical_project = canonical_row[0] if canonical_row else project
        aliases = conn.execute("SELECT alias_name FROM project_aliases WHERE canonical_name = ?", (canonical_project,)).fetchall()
        names = [canonical_project] + [r[0] for r in aliases]
        placeholders = ",".join("?" * len(names))
        
        rows = conn.execute(
            f"""
            SELECT s.id, COALESCE(pa.canonical_name, s.project_name) AS project_name, s.start_time, s.last_seen_time, s.end_time, s.active_seconds, s.notes, s.todos_json
            FROM sessions s
            LEFT JOIN project_aliases pa ON s.project_name = pa.alias_name
            WHERE s.project_name IN ({placeholders})
            ORDER BY s.start_time DESC, s.id DESC
            """,
            names,
        ).fetchall()

    index = next((i for i, row in enumerate(rows) if int(row["id"]) == sid), None)
    if index is None:
        return {"error": "session not found for project"}

    def serialize(row):
        return {
            "id": int(row["id"]),
            "project_name": row["project_name"],
            "start_time": float(row["start_time"] or 0),
            "last_seen_time": float(row["last_seen_time"] or 0),
            "end_time": float(row["end_time"]) if row["end_time"] is not None else None,
            "active_seconds": float(row["active_seconds"] or 0),
            "notes": (row["notes"] or "").strip(),
            "todos": _parse_todos_json(row["todos_json"]),
        }

    return {
        "ok": True,
        "session": serialize(rows[index]),
        "previous_session_id": int(rows[index + 1]["id"]) if index + 1 < len(rows) else None,
        "next_session_id": int(rows[index - 1]["id"]) if index > 0 else None,
    }


def ensure_project_category_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_categories (
            project_name TEXT PRIMARY KEY,
            category_key TEXT NOT NULL,
            updated_at   INTEGER NOT NULL
        )
        """
    )


def ensure_artists_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artists (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            email      TEXT NOT NULL DEFAULT '',
            phone      TEXT NOT NULL DEFAULT '',
            instagram  TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )


def ensure_project_metadata_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_metadata (
            project_name TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT '',
            type         TEXT NOT NULL DEFAULT '',
            priority     TEXT NOT NULL DEFAULT '',
            due_date     TEXT NOT NULL DEFAULT '',
            hard_deadline TEXT NOT NULL DEFAULT '',
            turn_in_date TEXT NOT NULL DEFAULT '',
            artist_id    TEXT NOT NULL DEFAULT '',
            progress_percent INTEGER NOT NULL DEFAULT 0,
            pinned       INTEGER NOT NULL DEFAULT 0,
            project_note TEXT NOT NULL DEFAULT '',
            board_order  INTEGER NOT NULL DEFAULT 0,
            completed_at INTEGER NOT NULL DEFAULT 0,
            prior_status TEXT NOT NULL DEFAULT '',
            prior_progress_percent INTEGER NOT NULL DEFAULT 0,
            updated_at   INTEGER NOT NULL
        )
        """
    )
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(project_metadata)").fetchall()
    }
    metadata_columns = {
        "display_name": "TEXT NOT NULL DEFAULT ''",
        "priority": "TEXT NOT NULL DEFAULT ''",
        "due_date": "TEXT NOT NULL DEFAULT ''",
        "hard_deadline": "TEXT NOT NULL DEFAULT ''",
        "turn_in_date": "TEXT NOT NULL DEFAULT ''",
        "artist_id": "TEXT NOT NULL DEFAULT ''",
        "progress_percent": "INTEGER NOT NULL DEFAULT 0",
        "pinned": "INTEGER NOT NULL DEFAULT 0",
        "project_note": "TEXT NOT NULL DEFAULT ''",
        "board_order": "INTEGER NOT NULL DEFAULT 0",
        "completed_at": "INTEGER NOT NULL DEFAULT 0",
        "prior_status": "TEXT NOT NULL DEFAULT ''",
        "prior_progress_percent": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in metadata_columns.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE project_metadata ADD COLUMN {column} {definition}")


def ensure_project_tasks_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            title        TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'open',
            priority     TEXT NOT NULL DEFAULT 'normal',
            due_date     TEXT NOT NULL DEFAULT '',
            waiting      INTEGER NOT NULL DEFAULT 0,
            quick        INTEGER NOT NULL DEFAULT 0,
            label        TEXT NOT NULL DEFAULT '',
            completed_at INTEGER,
            sort_order   INTEGER NOT NULL DEFAULT 0,
            created_at   INTEGER NOT NULL,
            updated_at   INTEGER NOT NULL
        )
        """
    )
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(project_tasks)").fetchall()
    }
    task_columns = {
        "waiting": "INTEGER NOT NULL DEFAULT 0",
        "quick": "INTEGER NOT NULL DEFAULT 0",
        "label": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in task_columns.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE project_tasks ADD COLUMN {column} {definition}")


def ensure_project_folders_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_folders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
            status        TEXT NOT NULL DEFAULT '',
            type          TEXT NOT NULL DEFAULT '',
            priority      TEXT NOT NULL DEFAULT '',
            due_date      TEXT NOT NULL DEFAULT '',
            hard_deadline TEXT NOT NULL DEFAULT '',
            turn_in_date  TEXT NOT NULL DEFAULT '',
            note          TEXT NOT NULL DEFAULT '',
            board_order   INTEGER NOT NULL DEFAULT 0,
            created_at    INTEGER NOT NULL,
            updated_at    INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_folder_members (
            project_name TEXT PRIMARY KEY,
            folder_id    INTEGER NOT NULL REFERENCES project_folders(id) ON DELETE CASCADE,
            sort_order   INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_folder_members_folder ON project_folder_members(folder_id, sort_order)")


def ensure_planner_goals_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS planner_goals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_type    TEXT NOT NULL,
            target_value REAL NOT NULL,
            period       TEXT NOT NULL,
            scope_type   TEXT NOT NULL,
            scope_value  TEXT NOT NULL DEFAULT '',
            active       INTEGER NOT NULL DEFAULT 1,
            created_at   INTEGER NOT NULL,
            updated_at   INTEGER NOT NULL
        )
        """
    )


def ensure_category_definitions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS category_definitions (
            key         TEXT PRIMARY KEY,
            label       TEXT NOT NULL,
            color       TEXT NOT NULL,
            updated_at  INTEGER NOT NULL
        )
        """
    )
    columns = [
        row[1]
        for row in conn.execute("PRAGMA table_info(category_definitions)").fetchall()
    ]
    expected_columns = ["key", "label", "color", "updated_at"]
    if columns != expected_columns:
        try:
            conn.execute("BEGIN")
            conn.execute("ALTER TABLE category_definitions RENAME TO category_definitions_legacy")
            conn.execute(
                """
                CREATE TABLE category_definitions (
                    key         TEXT PRIMARY KEY,
                    label       TEXT NOT NULL,
                    color       TEXT NOT NULL,
                    updated_at  INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO category_definitions (key, label, color, updated_at)
                SELECT key, label, color, updated_at
                FROM category_definitions_legacy
                """
            )
            conn.execute("DROP TABLE category_definitions_legacy")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def ensure_daily_metrics_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_metrics (
            metric_date      TEXT PRIMARY KEY,
            goal_hours       REAL,
            updated_at       INTEGER NOT NULL
        )
        """
    )


def ensure_app_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  INTEGER NOT NULL
        )
        """
    )


def get_app_setting(key: str, default: str | None = None) -> str | None:
    if not DB_PATH.exists():
        return default
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def _cache_get(key: str):
    entry = CONFIG_CACHE.get(key)
    if not entry:
        return None
    cached_at, value = entry
    if time.monotonic() - cached_at > CONFIG_CACHE_TTL_SECONDS:
        CONFIG_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value) -> None:
    CONFIG_CACHE[key] = (time.monotonic(), value)


def _cache_clear(key: str | None = None) -> None:
    if key is None:
        CONFIG_CACHE.clear()
    else:
        CONFIG_CACHE.pop(key, None)


def set_app_setting(key: str, value: str) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, strftime('%s', 'now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value),
        )
        conn.commit()
    _cache_clear("all_app_settings")


def get_all_app_settings() -> dict[str, str]:
    if not DB_PATH.exists():
        return {}
    cache_key = "all_app_settings"
    cached = _cache_get(cache_key)
    if cached is not None:
        return dict(cached)
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT key, value FROM app_settings ORDER BY key"
        ).fetchall()
        settings = {row["key"]: row["value"] for row in rows}
        _cache_set(cache_key, settings)
        return settings


def ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_project_name ON sessions(project_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_start_time ON sessions(start_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_end_time ON sessions(end_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_categories_key ON project_categories(category_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_metadata_status ON project_metadata(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_metadata_type ON project_metadata(type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_tasks_project_name ON project_tasks(project_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_tasks_status ON project_tasks(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_planner_goals_active ON planner_goals(active)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_planner_goals_type ON planner_goals(goal_type)"
    )


def ensure_project_aliases_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_aliases (
            alias_name     TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL
        )
        """
    )


def run_schema_migrations(conn: sqlite3.Connection) -> None:
    ensure_artists_table(conn)
    ensure_category_definitions_table(conn)
    ensure_project_category_table(conn)
    ensure_project_metadata_table(conn)
    ensure_project_tasks_table(conn)
    ensure_project_folders_tables(conn)
    ensure_planner_goals_table(conn)
    ensure_daily_metrics_table(conn)
    ensure_app_settings_table(conn)
    ensure_project_aliases_table(conn)
    ensure_sessions_notes_column(conn)
    ensure_indexes(conn)
    purge_legacy_categories(conn)


def ensure_sessions_notes_column(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "notes" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN notes TEXT DEFAULT ''")
    if "todo_notes" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN todo_notes TEXT DEFAULT ''")
    if "todos_json" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN todos_json TEXT DEFAULT '[]'")
    conn.execute("UPDATE sessions SET notes = COALESCE(notes, '') WHERE notes IS NULL")
    conn.execute("UPDATE sessions SET todo_notes = COALESCE(todo_notes, '') WHERE todo_notes IS NULL")
    conn.execute("UPDATE sessions SET todos_json = COALESCE(todos_json, '[]') WHERE todos_json IS NULL")
    conn.commit()


def purge_legacy_categories(conn: sqlite3.Connection) -> None:
    placeholders = ",".join("?" * len(LEGACY_CATEGORY_KEYS))
    conn.execute(
        f"DELETE FROM project_categories WHERE category_key IN ({placeholders})",
        LEGACY_CATEGORY_KEYS,
    )
    conn.execute(
        f"DELETE FROM category_definitions WHERE key IN ({placeholders})",
        LEGACY_CATEGORY_KEYS,
    )
    conn.commit()


def get_category_options(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT category.key,
               category.label,
               category.color,
               COUNT(project.project_name) AS assignment_count
        FROM category_definitions
        AS category
        LEFT JOIN project_categories AS project
          ON project.category_key = category.key
        GROUP BY category.key, category.label, category.color
        ORDER BY LOWER(category.label) ASC
        """
    ).fetchall()
    return [
        {
            "key": row["key"],
            "label": row["label"],
            "color": row["color"],
            "assignment_count": row["assignment_count"],
        }
        for row in rows
    ]


def get_category_maps(conn: sqlite3.Connection) -> tuple[list[dict], dict[str, dict]]:
    options = get_category_options(conn)
    return options, {option["key"]: option for option in options}


def get_project_categories(conn: sqlite3.Connection) -> dict[str, dict]:
    _, category_by_key = get_category_maps(conn)
    rows = conn.execute(
        """
        SELECT project_name, category_key
        FROM project_categories
        """
    ).fetchall()
    categories = {}
    for row in rows:
        category = category_by_key.get(row["category_key"])
        if not category:
            continue
        categories[row["project_name"]] = {
            "key": category["key"],
            "label": category["label"],
            "color": category["color"],
        }
    return categories


def get_project_status_options(conn: sqlite3.Connection) -> dict[str, str]:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'project_status_options'").fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            pass
    return PROJECT_STATUS_OPTIONS


def get_project_board_lane_order(conn: sqlite3.Connection) -> list[str]:
    """Return the saved lane order normalized against current status options.

    The order always includes "" (Unsorted) plus every configured status key.
    Saved positions for removed/unknown keys are discarded; newly configured
    statuses not yet in the saved order are appended in their configured order.
    """
    statuses = get_project_status_options(conn)
    current_keys = [""] + list(statuses.keys())
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'project_board_lane_order'"
    ).fetchone()
    saved: list[str] = []
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, list):
                saved = [str(s) for s in parsed]
        except Exception:
            saved = []
    current_set = set(current_keys)
    normalized: list[str] = []
    seen: set[str] = set()
    for status in saved:
        if status in current_set and status not in seen:
            normalized.append(status)
            seen.add(status)
    for status in current_keys:
        if status not in seen:
            normalized.append(status)
            seen.add(status)
    return normalized


def save_project_board_lane_order(ordered_statuses: list[str]) -> dict:
    """Validate and persist the lane order for the project board."""
    if not DB_PATH.exists():
        return {"error": "No data"}

    if not isinstance(ordered_statuses, list) or len(ordered_statuses) == 0:
        return {"error": "ordered_statuses must be a non-empty list"}

    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        statuses = get_project_status_options(conn)
        current_keys = [""] + list(statuses.keys())

        submitted = [str(s) for s in ordered_statuses]
        submitted_set = set(submitted)
        current_set = set(current_keys)

        if len(submitted) != len(submitted_set):
            return {"error": "ordered_statuses contains duplicate values"}

        if submitted_set != current_set:
            missing = current_set - submitted_set
            extra = submitted_set - current_set
            if missing:
                return {"error": f"ordered_statuses is missing lanes: {', '.join(sorted(missing))}"}
            if extra:
                return {"error": f"ordered_statuses contains unknown lanes: {', '.join(sorted(extra))}"}

        if len(submitted) != len(current_keys):
            return {"error": "ordered_statuses must contain exactly the current lanes"}

        set_app_setting("project_board_lane_order", json.dumps(submitted))
    return {"ok": True, "ordered_statuses": submitted}


def get_project_type_options(conn: sqlite3.Connection) -> dict[str, str]:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'project_type_options'").fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            pass
    return PROJECT_TYPE_OPTIONS


def get_project_metadata(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT project_name, display_name, status, type, priority, due_date, hard_deadline, turn_in_date, artist_id, progress_percent, pinned, project_note, board_order, completed_at, prior_status, prior_progress_percent
        FROM project_metadata
        """
    ).fetchall()
    metadata = {}
    status_options = get_project_status_options(conn)
    type_options = get_project_type_options(conn)
    for row in rows:
        status = row["status"] if row["status"] in status_options else ""
        project_type = row["type"] if row["type"] in type_options else ""
        priority = row["priority"] if row["priority"] in PROJECT_PRIORITY_OPTIONS else ""
        due_date = row["due_date"] if _is_valid_date_string(row["due_date"]) else ""
        hard_deadline = row["hard_deadline"] if _is_valid_date_string(row["hard_deadline"]) else ""
        turn_in_date = row["turn_in_date"] if _is_valid_date_string(row["turn_in_date"]) else ""
        metadata[row["project_name"]] = {
            "display_name": (row["display_name"] or "").strip() or row["project_name"],
            "status": status,
            "status_label": status_options.get(status, ""),
            "type": project_type,
            "type_label": type_options.get(project_type, ""),
            "priority": priority,
            "priority_label": PROJECT_PRIORITY_OPTIONS.get(priority, ""),
            "due_date": due_date,
            "hard_deadline": hard_deadline,
            "turn_in_date": turn_in_date,
            "artist_id": row["artist_id"] or "",
            "progress_percent": row["progress_percent"] or 0,
            "pinned": bool(row["pinned"]),
            "project_note": row["project_note"] or "",
            "board_order": row["board_order"] or 0,
            "completed_at": row["completed_at"] or 0,
            "prior_status": row["prior_status"] or "",
            "prior_progress_percent": row["prior_progress_percent"] or 0,
        }
    return metadata


def get_artists(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, email, phone, instagram, created_at, updated_at
        FROM artists
        ORDER BY name COLLATE NOCASE ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def create_artist(conn: sqlite3.Connection, artist_id: str, name: str, email: str = "", phone: str = "", instagram: str = "") -> dict:
    normalized_name = name.strip()
    if not normalized_name:
        return {"error": "Artist name is required."}
    
    conn.execute(
        """
        INSERT INTO artists (id, name, email, phone, instagram, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
        """,
        (artist_id, normalized_name, email.strip(), phone.strip(), instagram.strip())
    )
    conn.commit()
    return {"ok": True, "id": artist_id, "name": normalized_name}


def update_artist(conn: sqlite3.Connection, artist_id: str, name: str, email: str = "", phone: str = "", instagram: str = "") -> dict:
    normalized_name = name.strip()
    if not normalized_name:
        return {"error": "Artist name is required."}

    cur = conn.execute(
        """
        UPDATE artists
        SET name = ?, email = ?, phone = ?, instagram = ?, updated_at = strftime('%s', 'now')
        WHERE id = ?
        """,
        (normalized_name, email.strip(), phone.strip(), instagram.strip(), artist_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        return {"error": "Artist not found."}
    return {"ok": True, "id": artist_id, "name": normalized_name}


def delete_artist(conn: sqlite3.Connection, artist_id: str) -> dict:
    cur = conn.execute("DELETE FROM artists WHERE id = ?", (artist_id,))
    conn.commit()
    if cur.rowcount == 0:
        return {"error": "Artist not found."}
    
    # Remove artist_id from projects
    conn.execute("UPDATE project_metadata SET artist_id = '' WHERE artist_id = ?", (artist_id,))
    conn.commit()
    
    return {"ok": True, "deleted": cur.rowcount}


def _is_valid_date_string(value: str | None) -> bool:
    normalized = (value or "").strip()
    if not normalized:
        return True
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _normalize_project_metadata_fields(
    status: str | None,
    project_type: str | None,
    priority: str | None = None,
    due_date: str | None = "",
    hard_deadline: str | None = "",
    turn_in_date: str | None = "",
    artist_id: str | None = "",
    progress_percent: int | None = 0,
    pinned: bool | int | str | None = False,
    project_note: str | None = "",
    status_options: dict[str, str] | None = None,
    type_options: dict[str, str] | None = None,
) -> tuple[str, str, str, str, str, str, str, int, bool, str] | dict:
    status_options = status_options or PROJECT_STATUS_OPTIONS
    type_options = type_options or PROJECT_TYPE_OPTIONS

    normalized_status = (status or "").strip().lower()
    normalized_type = (project_type or "").strip().lower()
    normalized_priority = (priority or "").strip().lower()
    normalized_due_date = (due_date or "").strip()
    normalized_hard_deadline = (hard_deadline or "").strip()
    normalized_turn_in_date = (turn_in_date or "").strip()
    normalized_artist_id = (artist_id or "").strip()
    normalized_project_note = (project_note or "").strip()
    if isinstance(pinned, str):
        normalized_pinned = pinned.strip().lower() in {"1", "true", "yes", "on"}
    else:
        normalized_pinned = bool(pinned)

    try:
        normalized_progress = int(progress_percent or 0)
        normalized_progress = max(0, min(100, normalized_progress))
    except (ValueError, TypeError):
        normalized_progress = 0

    if normalized_status and normalized_status not in status_options:
        return {"error": "Unknown project status."}
    if normalized_type and normalized_type not in type_options:
        return {"error": "Unknown project type."}
    if normalized_priority and normalized_priority not in PROJECT_PRIORITY_OPTIONS:
        return {"error": "Unknown project priority."}
    for value in (normalized_due_date, normalized_hard_deadline, normalized_turn_in_date):
        if not _is_valid_date_string(value):
            return {"error": "Project dates must be empty or YYYY-MM-DD."}

    return (
        normalized_status,
        normalized_type,
        normalized_priority,
        normalized_due_date,
        normalized_hard_deadline,
        normalized_turn_in_date,
        normalized_artist_id,
        normalized_progress,
        normalized_pinned,
        normalized_project_note,
    )


def _project_deadline_summary(metadata: dict, today: date | None = None) -> dict:
    today = today or date.today()
    due_date = metadata.get("due_date", "")
    hard_deadline = metadata.get("hard_deadline", "")
    turn_in_date = metadata.get("turn_in_date", "")
    deadline_date = hard_deadline or due_date
    reasons = []

    if turn_in_date:
        return {
            "deadline_state": "delivered",
            "deadline_label": "Delivered",
            "deadline_reasons": [f"Turned in {turn_in_date}"],
        }
    if not deadline_date:
        return {
            "deadline_state": "",
            "deadline_label": "",
            "deadline_reasons": [],
        }

    parsed_deadline = datetime.strptime(deadline_date, "%Y-%m-%d").date()
    days_until = (parsed_deadline - today).days
    label_source = "Hard deadline" if hard_deadline else "Due date"
    reasons.append(f"{label_source} {deadline_date}")

    if days_until < 0:
        reasons.append(f"{abs(days_until)} day{'s' if abs(days_until) != 1 else ''} overdue")
        return {
            "deadline_state": "overdue",
            "deadline_label": "Overdue",
            "deadline_reasons": reasons,
        }
    if days_until <= 3:
        if days_until == 0:
            reasons.append("Due today")
        else:
            reasons.append(f"Due in {days_until} day{'s' if days_until != 1 else ''}")
        return {
            "deadline_state": "due_soon",
            "deadline_label": "Due Soon",
            "deadline_reasons": reasons,
        }

    reasons.append(f"Due in {days_until} days")
    return {
        "deadline_state": "upcoming",
        "deadline_label": "Upcoming",
        "deadline_reasons": reasons,
    }


def _format_project_task(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "project_name": row["project_name"],
        "title": row["title"],
        "status": row["status"],
        "priority": row["priority"],
        "due_date": row["due_date"] or "",
        "waiting": bool(row["waiting"]),
        "quick": bool(row["quick"]),
        "label": row["label"] or "",
        "completed_at": row["completed_at"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _normalize_project_task_fields(
    project_name: str | None,
    title: str | None,
    status: str | None = "open",
    priority: str | None = "normal",
    due_date: str | None = "",
    waiting=False,
    quick=False,
    label: str | None = "",
    sort_order=0,
) -> tuple[str, str, str, str, str, int, int, str, int] | dict:
    normalized_project = (project_name or "").strip()
    normalized_title = (title or "").strip()
    normalized_status = (status or "open").strip().lower()
    normalized_priority = (priority or "normal").strip().lower()
    normalized_due_date = (due_date or "").strip()
    normalized_label = " ".join((label or "").strip().split())

    def normalize_flag(value) -> int:
        if isinstance(value, str):
            return 1 if value.strip().lower() in {"1", "true", "yes", "on"} else 0
        return 1 if bool(value) else 0

    normalized_waiting = normalize_flag(waiting)
    normalized_quick = normalize_flag(quick)

    if not normalized_project:
        return {"error": "Project name is required."}
    if not normalized_title:
        return {"error": "Task title is required."}
    if normalized_status not in PROJECT_TASK_STATUS_OPTIONS:
        return {"error": "Unknown task status."}
    if normalized_priority not in PROJECT_TASK_PRIORITY_OPTIONS:
        return {"error": "Unknown task priority."}
    if normalized_due_date:
        try:
            parsed_due_date = datetime.strptime(normalized_due_date, "%Y-%m-%d")
        except ValueError:
            return {"error": "Task due date must be a valid YYYY-MM-DD date."}
        if parsed_due_date.strftime("%Y-%m-%d") != normalized_due_date:
            return {"error": "Task due date must be a valid YYYY-MM-DD date."}
    if len(normalized_label) > 32:
        return {"error": "Task label must be 32 characters or fewer."}
    try:
        normalized_sort_order = int(sort_order or 0)
    except (TypeError, ValueError):
        return {"error": "Task sort order must be a number."}

    return (
        normalized_project,
        normalized_title,
        normalized_status,
        normalized_priority,
        normalized_due_date,
        normalized_waiting,
        normalized_quick,
        normalized_label,
        normalized_sort_order,
    )


def get_project_tasks(conn: sqlite3.Connection, project_name: str | None = None) -> list[dict]:
    if project_name is None:
        rows = conn.execute(
            f"""
            SELECT pt.id, COALESCE(pa.canonical_name, pt.project_name) AS project_name, pt.title, pt.status, pt.priority, pt.due_date, pt.waiting, pt.quick, pt.label,
                   pt.completed_at, pt.sort_order, pt.created_at, pt.updated_at
            FROM project_tasks pt
            LEFT JOIN project_aliases pa ON pt.project_name = pa.alias_name
            ORDER BY LOWER(COALESCE(pa.canonical_name, pt.project_name)) ASC,
                     CASE pt.status WHEN 'open' THEN 0 ELSE 1 END ASC,
                     {PROJECT_TASK_PRIORITY_ORDER_SQL} ASC,
                     pt.sort_order ASC, pt.created_at ASC, pt.id ASC
            """
        ).fetchall()
    else:
        project_name = (project_name or "").strip()
        canonical_row = conn.execute("SELECT canonical_name FROM project_aliases WHERE alias_name = ?", (project_name,)).fetchone()
        canonical_project = canonical_row[0] if canonical_row else project_name
        aliases = conn.execute("SELECT alias_name FROM project_aliases WHERE canonical_name = ?", (canonical_project,)).fetchall()
        names = [canonical_project] + [r[0] for r in aliases]
        placeholders = ",".join("?" * len(names))

        rows = conn.execute(
            f"""
            SELECT pt.id, COALESCE(pa.canonical_name, pt.project_name) AS project_name, pt.title, pt.status, pt.priority, pt.due_date, pt.waiting, pt.quick, pt.label,
                   pt.completed_at, pt.sort_order, pt.created_at, pt.updated_at
            FROM project_tasks pt
            LEFT JOIN project_aliases pa ON pt.project_name = pa.alias_name
            WHERE pt.project_name IN ({placeholders})
            ORDER BY CASE pt.status WHEN 'open' THEN 0 ELSE 1 END ASC,
                     {PROJECT_TASK_PRIORITY_ORDER_SQL} ASC,
                     pt.sort_order ASC, pt.created_at ASC, pt.id ASC
            """,
            names,
        ).fetchall()
    return [_format_project_task(row) for row in rows]


def get_project_tasks_response(project_name: str | None) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    normalized_project = (project_name or "").strip()
    if not normalized_project:
        return {"error": "Project name is required."}
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        return {
            "ok": True,
            "project_name": normalized_project,
            "tasks": get_project_tasks(conn, normalized_project),
        }


def get_project_tasks_by_project(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    tasks_by_project: dict[str, list[dict]] = {}
    for task in get_project_tasks(conn):
        tasks_by_project.setdefault(task["project_name"], []).append(task)
    return tasks_by_project


def create_project_task(
    project_name: str | None,
    title: str | None,
    priority: str | None = "normal",
    due_date: str | None = "",
    sort_order=0,
    waiting=False,
    quick=False,
    label: str | None = "",
) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}

    normalized = _normalize_project_task_fields(
        project_name, title, "open", priority, due_date, waiting, quick, label, sort_order
    )
    if isinstance(normalized, dict):
        return normalized
    normalized_project, normalized_title, status, normalized_priority, normalized_due_date, normalized_waiting, normalized_quick, normalized_label, normalized_sort_order = normalized

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            INSERT INTO project_tasks (
                project_name, title, status, priority, due_date, waiting, quick, label,
                completed_at, sort_order, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, strftime('%s', 'now'), strftime('%s', 'now'))
            """,
            (
                normalized_project,
                normalized_title,
                status,
                normalized_priority,
                normalized_due_date,
                normalized_waiting,
                normalized_quick,
                normalized_label,
                normalized_sort_order,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, project_name, title, status, priority, due_date, waiting, quick, label,
                   completed_at, sort_order, created_at, updated_at
            FROM project_tasks
            WHERE id = ?
            """,
            (cur.lastrowid,),
        ).fetchone()
        return {"ok": True, "task": _format_project_task(row)}


def update_project_task(task_id, fields: dict) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    try:
        normalized_id = int(task_id)
    except (TypeError, ValueError):
        return {"error": "Task id is required."}

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            """
            SELECT id, project_name, title, status, priority, due_date, waiting, quick, label,
                   completed_at, sort_order, created_at, updated_at
            FROM project_tasks
            WHERE id = ?
            """,
            (normalized_id,),
        ).fetchone()
        if not existing:
            return {"error": "Task not found."}

        project_name = fields.get("project_name", existing["project_name"])
        title = fields.get("title", existing["title"])
        status = fields.get("status", existing["status"])
        priority = fields.get("priority", existing["priority"])
        due_date = fields.get("due_date", existing["due_date"])
        waiting = fields.get("waiting", existing["waiting"])
        quick = fields.get("quick", existing["quick"])
        label = fields.get("label", existing["label"])
        sort_order = fields.get("sort_order", existing["sort_order"])
        normalized = _normalize_project_task_fields(
            project_name, title, status, priority, due_date, waiting, quick, label, sort_order
        )
        if isinstance(normalized, dict):
            return normalized
        normalized_project, normalized_title, normalized_status, normalized_priority, normalized_due_date, normalized_waiting, normalized_quick, normalized_label, normalized_sort_order = normalized
        completed_at_sql = (
            "COALESCE(completed_at, strftime('%s', 'now'))"
            if normalized_status == "done"
            else "NULL"
        )

        conn.execute(
            f"""
            UPDATE project_tasks
            SET project_name = ?,
                title = ?,
                status = ?,
                priority = ?,
                due_date = ?,
                waiting = ?,
                quick = ?,
                label = ?,
                completed_at = {completed_at_sql},
                sort_order = ?,
                updated_at = strftime('%s', 'now')
            WHERE id = ?
            """,
            (
                normalized_project,
                normalized_title,
                normalized_status,
                normalized_priority,
                normalized_due_date,
                normalized_waiting,
                normalized_quick,
                normalized_label,
                normalized_sort_order,
                normalized_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, project_name, title, status, priority, due_date, waiting, quick, label,
                   completed_at, sort_order, created_at, updated_at
            FROM project_tasks
            WHERE id = ?
            """,
            (normalized_id,),
        ).fetchone()
        return {"ok": True, "task": _format_project_task(row)}


def delete_project_task(task_id) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    try:
        normalized_id = int(task_id)
    except (TypeError, ValueError):
        return {"error": "Task id is required."}

    with db_connection() as conn:
        cur = conn.execute("DELETE FROM project_tasks WHERE id = ?", (normalized_id,))
        conn.commit()
        if cur.rowcount < 1:
            return {"error": "Task not found."}
        return {"ok": True, "deleted": cur.rowcount, "id": normalized_id}


def _format_planner_goal(row: sqlite3.Row, progress: dict | None = None) -> dict:
    goal = {
        "id": int(row["id"]),
        "goal_type": row["goal_type"],
        "target_value": row["target_value"],
        "period": row["period"],
        "scope_type": row["scope_type"],
        "scope_value": row["scope_value"] or "",
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if progress is not None:
        goal["progress"] = progress
    return goal


def _normalize_planner_goal_fields(
    goal_type: str | None,
    target_value,
    period: str | None,
    scope_type: str | None,
    scope_value: str | None = "",
    active=True,
) -> tuple[str, float, str, str, str, int] | dict:
    normalized_type = (goal_type or "").strip().lower()
    normalized_period = (period or "").strip().lower()
    normalized_scope_type = (scope_type or "all").strip().lower()
    normalized_scope_value = (scope_value or "").strip()

    if normalized_type not in PLANNER_GOAL_TYPE_OPTIONS:
        return {"error": "Unknown planner goal type."}
    if normalized_period not in PLANNER_GOAL_PERIOD_OPTIONS:
        return {"error": "Unknown planner goal period."}
    if normalized_scope_type not in PLANNER_GOAL_SCOPE_OPTIONS:
        return {"error": "Unknown planner goal scope."}
    if normalized_scope_type == "all":
        normalized_scope_value = ""
    elif not normalized_scope_value:
        return {"error": "Planner goal scope value is required."}

    try:
        normalized_target = float(target_value)
    except (TypeError, ValueError):
        return {"error": "Planner goal target must be a number."}
    if normalized_target <= 0:
        return {"error": "Planner goal target must be greater than zero."}
    if normalized_type in {
        "sessions_per_week",
        "projects_finished_per_period",
        "touch_active_project_every_n_days",
    }:
        if normalized_target != int(normalized_target):
            return {"error": "Planner goal target must be a whole number."}
        normalized_target = float(int(normalized_target))

    if isinstance(active, str):
        normalized_active = 0 if active.strip().lower() in {"0", "false", "no", "off"} else 1
    else:
        normalized_active = 1 if bool(active) else 0
    return (
        normalized_type,
        normalized_target,
        normalized_period,
        normalized_scope_type,
        normalized_scope_value,
        normalized_active,
    )


def _planner_goal_period_range(period: str, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    if period == "month":
        start = today.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return start, end
    return get_week_range(today)


def _scope_matches_project(project_name: str, scope_type: str, scope_value: str, metadata: dict, categories: dict) -> bool:
    if scope_type == "all":
        return True
    if scope_type == "project":
        return project_name == scope_value
    if scope_type == "project_type":
        return metadata.get(project_name, {}).get("type", "") == scope_value
    if scope_type == "category":
        category = categories.get(project_name)
        return bool(category and category.get("key") == scope_value)
    return False


def _count_sessions_in_range(
    conn: sqlite3.Connection,
    start_date: date,
    end_date: date,
    scope_type: str,
    scope_value: str,
    metadata: dict,
    categories: dict,
) -> int:
    range_start = datetime.combine(start_date, datetime.min.time()).timestamp()
    range_end = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).timestamp()
    rows = conn.execute(
        """
        SELECT project_name
        FROM sessions
        WHERE active_seconds > 0
          AND start_time >= ?
          AND start_time < ?
        """,
        (range_start, range_end),
    ).fetchall()
    return sum(
        1
        for row in rows
        if _scope_matches_project(row["project_name"], scope_type, scope_value, metadata, categories)
    )


def _sum_scoped_seconds_in_range(
    conn: sqlite3.Connection,
    start_date: date,
    end_date: date,
    scope_type: str,
    scope_value: str,
    metadata: dict,
    categories: dict,
) -> int:
    range_start = datetime.combine(start_date, datetime.min.time()).timestamp()
    range_end = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).timestamp()
    rows = conn.execute(
        """
        SELECT project_name, start_time, last_seen_time, end_time, active_seconds
        FROM sessions
        WHERE active_seconds > 0
          AND start_time < ?
          AND COALESCE(end_time, last_seen_time, start_time) >= ?
        """,
        (range_end, range_start),
    ).fetchall()
    total = 0.0
    for row in rows:
        if not _scope_matches_project(row["project_name"], scope_type, scope_value, metadata, categories):
            continue
        end_time = row["end_time"] or row["last_seen_time"] or row["start_time"]
        for day_key, _hour, seconds in allocate_session_activity(
            row["start_time"],
            end_time,
            row["active_seconds"],
        ):
            if start_date <= date.fromisoformat(day_key) <= end_date:
                total += seconds
    return round(total)


def _count_finished_projects(
    conn: sqlite3.Connection,
    start_date: date,
    end_date: date,
    scope_type: str,
    scope_value: str,
    metadata: dict,
    categories: dict,
) -> int:
    rows = conn.execute(
        """
        SELECT project_name, status, updated_at, turn_in_date
        FROM project_metadata
        WHERE status = 'finished'
        """
    ).fetchall()
    total = 0
    for row in rows:
        project_name = row["project_name"]
        if not _scope_matches_project(project_name, scope_type, scope_value, metadata, categories):
            continue
        turn_in_date = row["turn_in_date"] or ""
        if turn_in_date:
            finished_date = datetime.strptime(turn_in_date, "%Y-%m-%d").date()
            if start_date <= finished_date <= end_date:
                total += 1
            continue
        # The metadata table does not yet have a dedicated finished_at timestamp.
        # Count current finished projects so the v1 goal remains useful until
        # status transition history exists.
        total += 1
    return total


def _count_touched_active_projects(
    conn: sqlite3.Connection,
    days: int,
    scope_type: str,
    scope_value: str,
    metadata: dict,
    categories: dict,
    today: date | None = None,
) -> tuple[int, int]:
    today = today or date.today()
    cutoff = datetime.combine(today - timedelta(days=days - 1), datetime.min.time()).timestamp()
    placeholders = ",".join("?" * len(ACTIVE_PROJECT_STATUSES))
    rows = conn.execute(
        f"""
        SELECT s.project_name,
               MAX(COALESCE(s.end_time, s.last_seen_time, s.start_time)) AS last_seen
        FROM sessions s
        JOIN project_metadata pm ON pm.project_name = s.project_name
        WHERE s.active_seconds > 0
          AND pm.status IN ({placeholders})
        GROUP BY s.project_name
        """,
        tuple(ACTIVE_PROJECT_STATUSES),
    ).fetchall()
    active_projects = [
        row
        for row in rows
        if _scope_matches_project(row["project_name"], scope_type, scope_value, metadata, categories)
    ]
    touched = sum(1 for row in active_projects if float(row["last_seen"] or 0) >= cutoff)
    return touched, len(active_projects)


def compute_planner_goal_progress(
    conn: sqlite3.Connection,
    goal: dict,
    metadata: dict | None = None,
    categories: dict | None = None,
    today: date | None = None,
) -> dict:
    metadata = metadata if metadata is not None else get_project_metadata(conn)
    categories = categories if categories is not None else get_project_categories(conn)
    today = today or date.today()
    start_date, end_date = _planner_goal_period_range(goal["period"], today)
    goal_type = goal["goal_type"]
    target = float(goal["target_value"])
    current = 0.0
    unit = "count"
    label = ""
    total_active_projects = None

    if goal_type == "sessions_per_week":
        current = float(_count_sessions_in_range(
            conn, start_date, end_date, goal["scope_type"], goal["scope_value"], metadata, categories
        ))
        unit = "sessions"
        label = "Sessions this period"
    elif goal_type == "hours_per_week":
        seconds = _sum_scoped_seconds_in_range(
            conn, start_date, end_date, goal["scope_type"], goal["scope_value"], metadata, categories
        )
        current = round(seconds / 3600, 2)
        unit = "hours"
        label = "Hours this period"
    elif goal_type == "projects_finished_per_period":
        current = float(_count_finished_projects(
            conn, start_date, end_date, goal["scope_type"], goal["scope_value"], metadata, categories
        ))
        unit = "projects"
        label = "Finished projects this period"
    elif goal_type == "touch_active_project_every_n_days":
        days = int(target)
        touched, total_active_projects = _count_touched_active_projects(
            conn, days, goal["scope_type"], goal["scope_value"], metadata, categories, today
        )
        current = float(touched)
        target = float(total_active_projects)
        unit = "projects"
        label = f"Active projects touched in {days} days"

    remaining = max(target - current, 0.0)
    percent = 100.0 if target <= 0 else min(100.0, round((current / target) * 100, 1))
    return {
        "current_value": current,
        "target_value": target,
        "remaining_value": remaining,
        "percent": percent,
        "unit": unit,
        "label": label,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_active_projects": total_active_projects,
    }


def get_planner_goals(conn: sqlite3.Connection, include_inactive: bool = True) -> list[dict]:
    query = """
        SELECT id, goal_type, target_value, period, scope_type, scope_value,
               active, created_at, updated_at
        FROM planner_goals
    """
    params = ()
    if not include_inactive:
        query += " WHERE active = ?"
        params = (1,)
    query += " ORDER BY active DESC, created_at ASC, id ASC"
    rows = conn.execute(query, params).fetchall()
    metadata = get_project_metadata(conn)
    categories = get_project_categories(conn)
    goals = []
    for row in rows:
        base = _format_planner_goal(row)
        goals.append(_format_planner_goal(row, compute_planner_goal_progress(conn, base, metadata, categories)))
    return goals


def get_planner_goals_response() -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        return {"ok": True, "goals": get_planner_goals(conn)}


def create_planner_goal(
    goal_type: str | None,
    target_value,
    period: str | None = "week",
    scope_type: str | None = "all",
    scope_value: str | None = "",
    active=True,
) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    normalized = _normalize_planner_goal_fields(
        goal_type, target_value, period, scope_type, scope_value, active
    )
    if isinstance(normalized, dict):
        return normalized
    normalized_type, normalized_target, normalized_period, normalized_scope_type, normalized_scope_value, normalized_active = normalized

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            INSERT INTO planner_goals (
                goal_type, target_value, period, scope_type, scope_value,
                active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
            """,
            (
                normalized_type,
                normalized_target,
                normalized_period,
                normalized_scope_type,
                normalized_scope_value,
                normalized_active,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, goal_type, target_value, period, scope_type, scope_value,
                   active, created_at, updated_at
            FROM planner_goals
            WHERE id = ?
            """,
            (cur.lastrowid,),
        ).fetchone()
        goal = _format_planner_goal(row)
        return {"ok": True, "goal": _format_planner_goal(row, compute_planner_goal_progress(conn, goal))}


def update_planner_goal(goal_id, fields: dict) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    try:
        normalized_id = int(goal_id)
    except (TypeError, ValueError):
        return {"error": "Planner goal id is required."}

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            """
            SELECT id, goal_type, target_value, period, scope_type, scope_value,
                   active, created_at, updated_at
            FROM planner_goals
            WHERE id = ?
            """,
            (normalized_id,),
        ).fetchone()
        if not existing:
            return {"error": "Planner goal not found."}

        normalized = _normalize_planner_goal_fields(
            fields.get("goal_type", existing["goal_type"]),
            fields.get("target_value", existing["target_value"]),
            fields.get("period", existing["period"]),
            fields.get("scope_type", existing["scope_type"]),
            fields.get("scope_value", existing["scope_value"]),
            fields.get("active", bool(existing["active"])),
        )
        if isinstance(normalized, dict):
            return normalized
        normalized_type, normalized_target, normalized_period, normalized_scope_type, normalized_scope_value, normalized_active = normalized

        conn.execute(
            """
            UPDATE planner_goals
            SET goal_type = ?,
                target_value = ?,
                period = ?,
                scope_type = ?,
                scope_value = ?,
                active = ?,
                updated_at = strftime('%s', 'now')
            WHERE id = ?
            """,
            (
                normalized_type,
                normalized_target,
                normalized_period,
                normalized_scope_type,
                normalized_scope_value,
                normalized_active,
                normalized_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, goal_type, target_value, period, scope_type, scope_value,
                   active, created_at, updated_at
            FROM planner_goals
            WHERE id = ?
            """,
            (normalized_id,),
        ).fetchone()
        goal = _format_planner_goal(row)
        return {"ok": True, "goal": _format_planner_goal(row, compute_planner_goal_progress(conn, goal))}


def delete_planner_goal(goal_id) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    try:
        normalized_id = int(goal_id)
    except (TypeError, ValueError):
        return {"error": "Planner goal id is required."}

    with db_connection() as conn:
        cur = conn.execute("DELETE FROM planner_goals WHERE id = ?", (normalized_id,))
        conn.commit()
        if cur.rowcount < 1:
            return {"error": "Planner goal not found."}
        return {"ok": True, "deleted": cur.rowcount, "id": normalized_id}


def get_project_folders(conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        with db_connection() as owned_conn:
            return get_project_folders(owned_conn)
    conn.row_factory = sqlite3.Row
    folders = [dict(row) for row in conn.execute("SELECT * FROM project_folders ORDER BY board_order, id").fetchall()]
    members = conn.execute(
        "SELECT folder_id, project_name, sort_order FROM project_folder_members ORDER BY folder_id, sort_order, project_name COLLATE NOCASE"
    ).fetchall()
    by_folder: dict[int, list[dict]] = {}
    for row in members:
        by_folder.setdefault(row["folder_id"], []).append({"project_name": row["project_name"], "sort_order": row["sort_order"]})
    for folder in folders:
        folder["members"] = by_folder.get(folder["id"], [])
        folder["member_count"] = len(folder["members"])
        folder["is_folder"] = True
        folder["project_name"] = folder["name"]
        folder["project_note"] = folder["note"]
        folder["pinned"] = False
        folder["project_tasks"] = get_project_tasks(conn, f"__folder__{folder['id']}")
    return folders


def save_project_folder(payload: dict) -> dict:
    try:
        folder_id = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        return {"error": "Invalid folder id."}
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"error": "Folder name is required."}
    if len(name) > 180:
        return {"error": "Folder name must be 180 characters or fewer."}
    with db_connection() as conn:
        statuses = get_project_status_options(conn)
        types = get_project_type_options(conn)
        status = str(payload.get("status") or "").strip()
        folder_type = str(payload.get("type") or "").strip()
        priority = str(payload.get("priority") or "").strip()
        dates = [str(payload.get(key) or "").strip() for key in ("due_date", "hard_deadline", "turn_in_date")]
        if status and status not in statuses:
            return {"error": "Unknown project status."}
        if folder_type and folder_type not in types:
            return {"error": "Unknown project type."}
        if priority and priority not in PROJECT_PRIORITY_OPTIONS:
            return {"error": "Unknown project priority."}
        try:
            for value in dates:
                if value:
                    datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return {"error": "Folder dates must be empty or YYYY-MM-DD."}
        note = str(payload.get("note") or "").strip()
        if len(note) > 3000:
            return {"error": "Folder notes must be 3000 characters or fewer."}
        now = int(time.time())
        try:
            if folder_id:
                cur = conn.execute(
                    "UPDATE project_folders SET name=?, status=?, type=?, priority=?, due_date=?, hard_deadline=?, turn_in_date=?, note=?, updated_at=? WHERE id=?",
                    (name, status, folder_type, priority, *dates, note, now, folder_id),
                )
                if cur.rowcount < 1:
                    return {"error": "Folder not found."}
            else:
                cur = conn.execute(
                    "INSERT INTO project_folders(name,status,type,priority,due_date,hard_deadline,turn_in_date,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (name, status, folder_type, priority, *dates, note, now, now),
                )
                folder_id = cur.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            return {"error": "A folder with that name already exists."}
        folder = next(item for item in get_project_folders(conn) if item["id"] == folder_id)
        return {"ok": True, "folder": folder}


def set_project_folder_members(folder_id: object, projects: object) -> dict:
    try:
        normalized_id = int(folder_id)
    except (TypeError, ValueError):
        return {"error": "Folder id is required."}
    if not isinstance(projects, list):
        return {"error": "projects must be a list."}
    names = [str(name).strip() for name in projects]
    if any(not name for name in names) or len(names) != len(set(names)):
        return {"error": "projects must contain unique project names."}
    with db_connection() as conn:
        if not conn.execute("SELECT 1 FROM project_folders WHERE id=?", (normalized_id,)).fetchone():
            return {"error": "Folder not found."}
        known = {row[0] for row in conn.execute("SELECT DISTINCT project_name FROM sessions UNION SELECT project_name FROM project_metadata")}
        if any(name not in known for name in names):
            return {"error": "Unknown project."}
        conn.execute("DELETE FROM project_folder_members WHERE folder_id=?", (normalized_id,))
        for order, name in enumerate(names, 1):
            conn.execute(
                "INSERT INTO project_folder_members(project_name,folder_id,sort_order) VALUES(?,?,?) ON CONFLICT(project_name) DO UPDATE SET folder_id=excluded.folder_id, sort_order=excluded.sort_order",
                (name, normalized_id, order),
            )
        conn.commit()
    return {"ok": True, "folder_id": normalized_id, "projects": names}


def delete_project_folder(folder_id: object) -> dict:
    try:
        normalized_id = int(folder_id)
    except (TypeError, ValueError):
        return {"error": "Folder id is required."}
    with db_connection() as conn:
        conn.execute("DELETE FROM project_folder_members WHERE folder_id=?", (normalized_id,))
        cur = conn.execute("DELETE FROM project_folders WHERE id=?", (normalized_id,))
        conn.execute("DELETE FROM project_tasks WHERE project_name=?", (f"__folder__{normalized_id}",))
        conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


def reorder_project_folder(folder_id: object, status: str | None, ordered_folders: object) -> dict:
    try:
        normalized_id = int(folder_id)
        ids = [int(value) for value in ordered_folders]
    except (TypeError, ValueError):
        return {"error": "Invalid folder order."}
    if normalized_id not in ids or len(ids) != len(set(ids)):
        return {"error": "Invalid folder order."}
    normalized_status = (status or "").strip()
    with db_connection() as conn:
        if normalized_status and normalized_status not in get_project_status_options(conn):
            return {"error": "Unknown project status."}
        for order, value in enumerate(ids, 1):
            conn.execute("UPDATE project_folders SET board_order=?, status=?, updated_at=? WHERE id=?", (order, normalized_status, int(time.time()), value))
        conn.commit()
    return {"ok": True}


def set_project_metadata(
    project_name: str,
    status: str | None,
    project_type: str | None,
    priority: str | None = None,
    due_date: str | None = None,
    hard_deadline: str | None = None,
    turn_in_date: str | None = None,
    artist_id: str | None = None,
    progress_percent: int | None = 0,
    pinned: bool | int | str | None = None,
    project_note: str | None = None,
    display_name: str | None = None,
) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}

    normalized_name = (project_name or "").strip()
    if not normalized_name:
        return {"error": "Project name is required."}

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            """
            SELECT display_name, status, type, priority, due_date, hard_deadline, turn_in_date, artist_id, progress_percent, pinned, project_note, board_order, completed_at, prior_status, prior_progress_percent
            FROM project_metadata
            WHERE project_name = ?
            """,
            (normalized_name,),
        ).fetchone()
        normalized_display_name = (
            (existing["display_name"] or "").strip() if display_name is None and existing
            else str(display_name or "").strip()
        )
        if not normalized_display_name or normalized_display_name == normalized_name:
            normalized_display_name = ""
        if len(normalized_display_name) > 180:
            return {"error": "Display name must be 180 characters or fewer."}
        normalized = _normalize_project_metadata_fields(
            existing["status"] if status is None and existing else status,
            existing["type"] if project_type is None and existing else project_type,
            existing["priority"] if priority is None and existing else priority,
            existing["due_date"] if due_date is None and existing else due_date,
            existing["hard_deadline"] if hard_deadline is None and existing else hard_deadline,
            existing["turn_in_date"] if turn_in_date is None and existing else turn_in_date,
            existing["artist_id"] if artist_id is None and existing else artist_id,
            existing["progress_percent"] if progress_percent is None and existing else progress_percent,
            existing["pinned"] if pinned is None and existing else pinned,
            existing["project_note"] if project_note is None and existing else project_note,
            get_project_status_options(conn),
            get_project_type_options(conn),
        )
        if isinstance(normalized, dict):
            return normalized
        (
            normalized_status,
            normalized_type,
            normalized_priority,
            normalized_due_date,
            normalized_hard_deadline,
            normalized_turn_in_date,
            normalized_artist_id,
            normalized_progress,
            normalized_pinned,
            normalized_project_note,
        ) = normalized

        if normalized_pinned:
            pinned_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM project_metadata
                WHERE pinned = 1 AND project_name != ?
                """,
                (normalized_name,),
            ).fetchone()[0]
            if pinned_count >= 3:
                return {"error": "You can pin up to 3 projects."}

        if not any(
            (
                normalized_status,
                normalized_type,
                normalized_priority,
                normalized_due_date,
                normalized_hard_deadline,
                normalized_turn_in_date,
                normalized_artist_id,
                normalized_progress,
                normalized_pinned,
                normalized_project_note,
                normalized_display_name,
                existing["board_order"] if existing else 0,
            )
        ):
            cur = conn.execute(
                "DELETE FROM project_metadata WHERE project_name = ?",
                (normalized_name,),
            )
            conn.commit()
            return {
                "ok": True,
                "deleted": cur.rowcount,
                "project_name": normalized_name,
                "metadata": {
                    "status": "",
                    "display_name": normalized_name,
                    "status_label": "",
                    "type": "",
                    "type_label": "",
                    "priority": "",
                    "priority_label": "",
                    "due_date": "",
                    "hard_deadline": "",
                    "turn_in_date": "",
                    "artist_id": "",
                    "progress_percent": 0,
                    "pinned": False,
                    "project_note": "",
                    "board_order": 0,
                },
            }

        completion_update = (normalized_status == "finished" and (not existing or existing["status"] != "finished"))
        completion_clear = (normalized_status != "finished" and existing and existing["status"] == "finished")
        completed_at = int(time.time()) if completion_update else (existing["completed_at"] if existing else 0)
        prior_status = (existing["status"] or "") if completion_update and existing else (existing["prior_status"] if existing else "")
        prior_progress = (existing["progress_percent"] or 0) if completion_update and existing else (existing["prior_progress_percent"] if existing else 0)
        if completion_update:
            normalized_progress = 100
        if completion_clear:
            completed_at = 0
            prior_status = ""
            prior_progress = 0

        conn.execute(
            """
            INSERT INTO project_metadata (
                project_name, display_name, status, type, priority, due_date, hard_deadline, turn_in_date, artist_id, progress_percent, pinned, project_note, completed_at, prior_status, prior_progress_percent, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            ON CONFLICT(project_name) DO UPDATE SET
                display_name = excluded.display_name,
                status = excluded.status,
                type = excluded.type,
                priority = excluded.priority,
                due_date = excluded.due_date,
                hard_deadline = excluded.hard_deadline,
                turn_in_date = excluded.turn_in_date,
                artist_id = excluded.artist_id,
                progress_percent = excluded.progress_percent,
                pinned = excluded.pinned,
                project_note = excluded.project_note,
                completed_at = excluded.completed_at,
                prior_status = excluded.prior_status,
                prior_progress_percent = excluded.prior_progress_percent,
                updated_at = excluded.updated_at
            """,
            (
                normalized_name,
                normalized_display_name,
                normalized_status,
                normalized_type,
                normalized_priority,
                normalized_due_date,
                normalized_hard_deadline,
                normalized_turn_in_date,
                normalized_artist_id,
                normalized_progress,
                1 if normalized_pinned else 0,
                normalized_project_note,
                completed_at,
                prior_status,
                prior_progress,
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "project_name": normalized_name,
            "metadata": {
                "display_name": normalized_display_name or normalized_name,
                "status": normalized_status,
                "status_label": get_project_status_options(conn).get(normalized_status, ""),
                "type": normalized_type,
                "type_label": get_project_type_options(conn).get(normalized_type, ""),
                "priority": normalized_priority,
                "priority_label": PROJECT_PRIORITY_OPTIONS.get(normalized_priority, ""),
                "due_date": normalized_due_date,
                "hard_deadline": normalized_hard_deadline,
                "turn_in_date": normalized_turn_in_date,
                "artist_id": normalized_artist_id,
                "progress_percent": normalized_progress,
                "pinned": normalized_pinned,
                "project_note": normalized_project_note,
                "board_order": existing["board_order"] if existing else 0,
                "completed_at": completed_at,
                "prior_status": prior_status,
                "prior_progress_percent": prior_progress,
            },
        }


def set_project_completion(project_name: str, completed: bool) -> dict:
    """Atomically complete or reopen a known project card."""
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    name = (project_name or "").strip()
    if not name:
        return {"error": "Project name is required."}
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        known = conn.execute(
            "SELECT 1 FROM sessions WHERE project_name = ? UNION SELECT 1 FROM project_metadata WHERE project_name = ? LIMIT 1",
            (name, name),
        ).fetchone()
        if not known:
            return {"error": "Unknown project."}
        row = conn.execute(
            "SELECT status, progress_percent, completed_at, prior_status, prior_progress_percent FROM project_metadata WHERE project_name = ?",
            (name,),
        ).fetchone()
        now = int(time.time())
        if completed:
            if row and row["status"] == "finished" and row["completed_at"]:
                return {"ok": True, "project_name": name, "metadata": get_project_metadata(conn).get(name, {})}
            prior_status = (row["status"] if row else "") or ""
            prior_progress = (row["progress_percent"] if row else 0) or 0
            conn.execute(
                """INSERT INTO project_metadata (project_name, status, progress_percent, completed_at, prior_status, prior_progress_percent, updated_at)
                   VALUES (?, 'finished', 100, ?, ?, ?, ?)
                   ON CONFLICT(project_name) DO UPDATE SET status='finished', progress_percent=100, completed_at=excluded.completed_at, prior_status=excluded.prior_status, prior_progress_percent=excluded.prior_progress_percent, updated_at=excluded.updated_at""",
                (name, now, prior_status, prior_progress, now),
            )
        else:
            if not row or row["status"] != "finished":
                return {"ok": True, "project_name": name, "metadata": get_project_metadata(conn).get(name, {})}
            restore_status = row["prior_status"] or "in_progress"
            restore_progress = row["prior_progress_percent"] if row["completed_at"] else row["progress_percent"]
            conn.execute(
                "UPDATE project_metadata SET status=?, progress_percent=?, completed_at=0, prior_status='', prior_progress_percent=0, updated_at=? WHERE project_name=?",
                (restore_status, restore_progress, now, name),
            )
        conn.commit()
        return {"ok": True, "project_name": name, "metadata": get_project_metadata(conn).get(name, {})}


def reorder_project_board(project_name: str, status: str | None, ordered_projects: object) -> dict:
    normalized_name = (project_name or "").strip()
    normalized_status = (status or "").strip()
    if not normalized_name:
        return {"error": "Project name is required."}
    if not isinstance(ordered_projects, list):
        return {"error": "ordered_projects must be a list."}
    names = [str(name).strip() for name in ordered_projects]
    if any(not name for name in names) or len(names) != len(set(names)):
        return {"error": "ordered_projects must contain unique project names."}
    if normalized_name not in names:
        return {"error": "Moved project is missing from ordered_projects."}

    with db_connection() as conn:
        statuses = get_project_status_options(conn)
        if normalized_status and normalized_status not in statuses:
            return {"error": "Unknown project status."}
        known = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT project_name FROM sessions UNION SELECT project_name FROM project_metadata"
            ).fetchall()
        }
        if any(name not in known for name in names):
            return {"error": "Unknown project in board order."}
        now = int(time.time())
        for position, name in enumerate(names, start=1):
            conn.execute(
                """
                INSERT INTO project_metadata (project_name, status, board_order, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_name) DO UPDATE SET
                    status = excluded.status,
                    board_order = excluded.board_order,
                    updated_at = excluded.updated_at
                """,
                (name, normalized_status, position, now),
            )
        conn.commit()
    return {"ok": True, "project_name": normalized_name, "status": normalized_status, "ordered_projects": names}


def set_project_category(project_name: str, category_key: str | None) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}

    normalized_name = (project_name or "").strip()
    if not normalized_name:
        return {"error": "Project name is required."}

    normalized_key = (category_key or "").strip().lower() or None

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _, category_by_key = get_category_maps(conn)
        if normalized_key is not None and normalized_key not in category_by_key:
            return {"error": "Unknown category."}
        if normalized_key is None:
            cur = conn.execute(
                "DELETE FROM project_categories WHERE project_name = ?",
                (normalized_name,),
            )
            conn.commit()
            return {"ok": True, "deleted": cur.rowcount, "project_name": normalized_name}

        conn.execute(
            """
            INSERT INTO project_categories (project_name, category_key, updated_at)
            VALUES (?, ?, strftime('%s', 'now'))
            ON CONFLICT(project_name) DO UPDATE SET
                category_key = excluded.category_key,
                updated_at = excluded.updated_at
            """,
            (normalized_name, normalized_key),
        )
        conn.commit()
        category = category_by_key[normalized_key]
        return {
            "ok": True,
            "project_name": normalized_name,
            "category": {
                "key": category["key"],
                "label": category["label"],
                "color": category["color"],
            },
        }


def normalize_category_key(label: str) -> str:
    collapsed = "".join(ch.lower() if ch.isalnum() else "-" for ch in label.strip())
    cleaned = "-".join(part for part in collapsed.split("-") if part)
    return cleaned[:36] or "category"


def normalize_hex_color(color: str) -> str | None:
    normalized = (color or "").strip().upper()
    if (
        len(normalized) != 7
        or not normalized.startswith("#")
        or any(ch not in "0123456789ABCDEF" for ch in normalized[1:])
    ):
        return None
    return normalized


def create_category(label: str, color: str) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}

    normalized_label = " ".join((label or "").strip().split())
    if not normalized_label:
        return {"error": "Category name is required."}
    if len(normalized_label) > 32:
        return {"error": "Category name must be 32 characters or less."}

    normalized_color = normalize_hex_color(color)
    if not normalized_color:
        return {"error": "Pick a valid hex color."}

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        custom_count = conn.execute(
            "SELECT COUNT(*) FROM category_definitions"
        ).fetchone()[0]
        if custom_count >= MAX_CUSTOM_CATEGORIES:
            return {"error": f"You can create up to {MAX_CUSTOM_CATEGORIES} custom categories."}

        existing_label = conn.execute(
            "SELECT key FROM category_definitions WHERE LOWER(label) = LOWER(?)",
            (normalized_label,),
        ).fetchone()
        if existing_label:
            return {"error": "A category with that name already exists."}

        base_key = f"custom-{normalize_category_key(normalized_label)}"
        category_key = base_key
        suffix = 2
        while conn.execute(
            "SELECT 1 FROM category_definitions WHERE key = ?",
            (category_key,),
        ).fetchone():
            category_key = f"{base_key}-{suffix}"
            suffix += 1

        conn.execute(
            """
            INSERT INTO category_definitions (key, label, color, updated_at)
            VALUES (?, ?, ?, strftime('%s', 'now'))
            """,
            (category_key, normalized_label, normalized_color),
        )
        conn.commit()
        return {
            "ok": True,
            "category": {
                "key": category_key,
                "label": normalized_label,
                "color": normalized_color,
            },
            "custom_category_limit": MAX_CUSTOM_CATEGORIES,
            "custom_category_count": custom_count + 1,
        }


def update_category(category_key: str, label: str, color: str) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}

    normalized_key = (category_key or "").strip().lower()
    normalized_label = " ".join((label or "").strip().split())
    if not normalized_key:
        return {"error": "Category key is required."}
    if not normalized_label:
        return {"error": "Category name is required."}
    if len(normalized_label) > 32:
        return {"error": "Category name must be 32 characters or less."}

    normalized_color = normalize_hex_color(color)
    if not normalized_color:
        return {"error": "Pick a valid hex color."}

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT key
            FROM category_definitions
            WHERE key = ?
            """,
            (normalized_key,),
        ).fetchone()
        if not row:
            return {"error": "Category not found."}

        existing_label = conn.execute(
            """
            SELECT key
            FROM category_definitions
            WHERE LOWER(label) = LOWER(?)
              AND key != ?
            """,
            (normalized_label, normalized_key),
        ).fetchone()
        if existing_label:
            return {"error": "A category with that name already exists."}

        conn.execute(
            """
            UPDATE category_definitions
            SET label = ?, color = ?, updated_at = strftime('%s', 'now')
            WHERE key = ?
            """,
            (normalized_label, normalized_color, normalized_key),
        )
        conn.commit()
        return {
            "ok": True,
            "category": {
                "key": normalized_key,
                "label": normalized_label,
                "color": normalized_color,
            },
        }


def delete_category(category_key: str) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}

    normalized_key = (category_key or "").strip().lower()
    if not normalized_key:
        return {"error": "Category key is required."}

    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT key, label, color
            FROM category_definitions
            WHERE key = ?
            """,
            (normalized_key,),
        ).fetchone()
        if not row:
            return {"error": "Category not found."}

        cleared = conn.execute(
            "DELETE FROM project_categories WHERE category_key = ?",
            (normalized_key,),
        ).rowcount
        conn.execute(
            "DELETE FROM category_definitions WHERE key = ?",
            (normalized_key,),
        )
        conn.commit()
        return {
            "ok": True,
            "deleted_category_key": normalized_key,
            "cleared_assignments": cleared,
            "category": {
                "key": row["key"],
                "label": row["label"],
                "color": row["color"],
            },
        }


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValueError("invalid date") from exc


def parse_month_key(value: str) -> date:
    try:
        return date.fromisoformat(f"{(value or '').strip()}-01")
    except ValueError as exc:
        raise ValueError("invalid month") from exc


def get_week_range(target_date: date, week_start_weekday: int | None = None) -> tuple[date, date]:
    if week_start_weekday is None:
        raw = get_app_setting("week_start_weekday")
        week_start_weekday = int(raw) if raw is not None else DEFAULT_WEEK_START_WEEKDAY
    week_start = target_date - timedelta(days=(target_date.weekday() - week_start_weekday) % 7)
    return week_start, week_start + timedelta(days=6)


def get_daily_progress_seconds(conn: sqlite3.Connection, target_date: date) -> int:
    day_start = datetime.combine(target_date, datetime.min.time()).timestamp()
    day_end = datetime.combine(target_date + timedelta(days=1), datetime.min.time()).timestamp()
    rows = conn.execute(
        """
        SELECT start_time, last_seen_time, end_time, active_seconds
        FROM sessions
        WHERE active_seconds > 0
          AND start_time < ?
          AND COALESCE(end_time, last_seen_time, start_time) >= ?
        """,
        (day_end, day_start),
    ).fetchall()
    target_key = target_date.isoformat()
    total_seconds = 0.0
    for row in rows:
        end_time = row["end_time"] or row["last_seen_time"] or row["start_time"]
        for day_key, _hour, seconds in allocate_session_activity(
            row["start_time"],
            end_time,
            row["active_seconds"],
        ):
            if day_key == target_key:
                total_seconds += seconds
    return round(total_seconds)


def get_range_progress_seconds(conn: sqlite3.Connection, start_date: date, end_date: date) -> int:
    range_start = datetime.combine(start_date, datetime.min.time()).timestamp()
    range_end = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).timestamp()
    rows = conn.execute(
        """
        SELECT start_time, last_seen_time, end_time, active_seconds
        FROM sessions
        WHERE active_seconds > 0
          AND start_time < ?
          AND COALESCE(end_time, last_seen_time, start_time) >= ?
        """,
        (range_end, range_start),
    ).fetchall()
    total_seconds = 0.0
    for row in rows:
        end_time = row["end_time"] or row["last_seen_time"] or row["start_time"]
        for day_key, _hour, seconds in allocate_session_activity(
            row["start_time"],
            end_time,
            row["active_seconds"],
        ):
            if start_date <= date.fromisoformat(day_key) <= end_date:
                total_seconds += seconds
    return round(total_seconds)


def get_daily_target(date_value: str) -> dict:
    target_date = parse_iso_date(date_value)
    date_key = target_date.isoformat()

    if not DB_PATH.exists():
        return {
            "date": date_key,
            "goal_hours": None,
            "progress_seconds": 0,
            "has_target": False,
        }

    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT goal_hours
            FROM daily_metrics
            WHERE metric_date = ?
            """,
            (date_key,),
        ).fetchone()
        progress_seconds = get_daily_progress_seconds(conn, target_date)
        goal_hours = None
        if row and row["goal_hours"] is not None:
            goal_hours = round(float(row["goal_hours"]) * 10) / 10
        if goal_hours is None:
            default_raw = get_app_setting("default_daily_goal_hours")
            if default_raw is not None:
                try:
                    goal_hours = round(float(default_raw) * 10) / 10
                except (ValueError, TypeError):
                    goal_hours = None
        return {
            "date": date_key,
            "goal_hours": goal_hours,
            "progress_seconds": progress_seconds,
            "has_target": goal_hours is not None,
        }


def set_daily_target(date_value: str, goal_hours: object) -> dict:
    target_date = parse_iso_date(date_value)
    try:
        normalized_goal = round(float(goal_hours) * 10) / 10
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid goal") from exc
    if normalized_goal <= 0 or normalized_goal > 100:
        raise ValueError("invalid goal")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO daily_metrics (metric_date, goal_hours, updated_at)
            VALUES (?, ?, strftime('%s', 'now'))
            ON CONFLICT(metric_date) DO UPDATE SET
                goal_hours = excluded.goal_hours,
                updated_at = excluded.updated_at
            """,
            (target_date.isoformat(), normalized_goal),
        )
        conn.commit()
    set_app_setting("default_daily_goal_hours", str(normalized_goal))
    return get_daily_target(target_date.isoformat())


def get_weekly_target(date_value: str = "") -> dict:
    target_date = parse_iso_date(date_value) if date_value else date.today()
    week_start_weekday = int(get_app_setting("week_start_weekday") or DEFAULT_WEEK_START_WEEKDAY)
    week_start, week_end = get_week_range(target_date, week_start_weekday)
    start_key = week_start.isoformat()
    end_key = week_end.isoformat()

    default_raw = get_app_setting("default_weekly_goal_hours")
    goal_hours = None
    if default_raw is not None:
        try:
            goal_hours = round(float(default_raw) * 10) / 10
        except (ValueError, TypeError):
            goal_hours = None

    base = {
        "week_start": start_key,
        "week_end": end_key,
        "weekly_start_date": start_key,
        "weekly_end_date": end_key,
        "goal_hours": goal_hours or 0,
        "progress_seconds": 0,
        "has_target": goal_hours is not None and goal_hours > 0,
        "week_start_weekday": week_start_weekday,
        "week_start_weekday_name": WEEKDAY_NAMES[week_start_weekday],
    }

    if not DB_PATH.exists():
        return base

    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        reset_at = datetime.combine(week_end + timedelta(days=1), datetime.min.time())
        seconds_until_reset = max(0, int(round(reset_at.timestamp() - datetime.now().timestamp())))
        return {
            **base,
            "reset_at": reset_at.isoformat(),
            "seconds_until_reset": seconds_until_reset,
            "progress_seconds": get_range_progress_seconds(conn, week_start, week_end),
        }


def set_weekly_target(goal_hours: object) -> dict:
    try:
        normalized_goal = round(float(goal_hours) * 10) / 10
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid goal") from exc
    if normalized_goal <= 0 or normalized_goal > 168:
        raise ValueError("invalid goal")
    set_app_setting("default_weekly_goal_hours", str(normalized_goal))
    return get_weekly_target()


# ─────────────────────────────────────────────────────────────
#  Data layer
# ─────────────────────────────────────────────────────────────

def _compute_data_etag(month_value: str = "") -> str:
    """Lightweight hash of DB state — returns None if no DB."""
    if not DB_PATH.exists():
        return ""
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        s = conn.execute(
            "SELECT COUNT(*), MAX(rowid), MAX(COALESCE(last_seen_time, start_time)),"
            "  (SELECT COUNT(*) FROM sessions WHERE end_time IS NULL)"
            " FROM sessions"
        ).fetchone()
        sn = conn.execute(
            """
            SELECT COUNT(*), MAX(rowid),
                   GROUP_CONCAT(id || ':' || COALESCE(notes, '') || ':' || COALESCE(todo_notes, '') || ':' || COALESCE(todos_json, ''), '|')
            FROM (
                SELECT id, rowid, notes, todo_notes, todos_json
                FROM sessions
                ORDER BY id
            )
            """
        ).fetchone()
        cd = conn.execute("SELECT COUNT(*), MAX(rowid) FROM category_definitions").fetchone()
        pc = conn.execute("SELECT COUNT(*), MAX(rowid) FROM project_categories").fetchone()
        pm = conn.execute(
            """
            SELECT COUNT(*), MAX(rowid), MAX(updated_at),
                   GROUP_CONCAT(project_name || ':' || status || ':' || type || ':' || priority || ':' || due_date || ':' || hard_deadline || ':' || turn_in_date || ':' || pinned || ':' || project_note || ':' || board_order || ':' || completed_at || ':' || prior_status || ':' || prior_progress_percent, '|')
            FROM (
                SELECT rowid, project_name, status, type, priority, due_date, hard_deadline, turn_in_date, pinned, project_note, board_order, completed_at, prior_status, prior_progress_percent, updated_at
                FROM project_metadata
                ORDER BY project_name
            )
            """
        ).fetchone()
        pt = conn.execute(
            """
            SELECT COUNT(*), MAX(id), MAX(updated_at),
                   GROUP_CONCAT(id || ':' || project_name || ':' || status || ':' || priority || ':' || due_date || ':' || waiting || ':' || quick || ':' || label || ':' || updated_at, '|')
            FROM (
                SELECT id, project_name, status, priority, due_date, waiting, quick, label, updated_at
                FROM project_tasks
                ORDER BY id
            )
            """
        ).fetchone()
        pg = conn.execute(
            """
            SELECT COUNT(*), MAX(id), MAX(updated_at),
                   GROUP_CONCAT(id || ':' || goal_type || ':' || target_value || ':' || period || ':' || scope_type || ':' || scope_value || ':' || active || ':' || updated_at, '|')
            FROM (
                SELECT id, goal_type, target_value, period, scope_type, scope_value, active, updated_at
                FROM planner_goals
                ORDER BY id
            )
            """
        ).fetchone()
        pf = conn.execute("SELECT COUNT(*), MAX(id), MAX(updated_at), GROUP_CONCAT(id || ':' || name || ':' || status || ':' || board_order, '|') FROM project_folders").fetchone()
        pfm = conn.execute("SELECT COUNT(*), GROUP_CONCAT(project_name || ':' || folder_id || ':' || sort_order, '|') FROM project_folder_members").fetchone()
        dm = conn.execute("SELECT COUNT(*), MAX(rowid) FROM daily_metrics").fetchone()
        ast = conn.execute(
            "SELECT GROUP_CONCAT(key || ':' || value, '|') FROM (SELECT key, value FROM app_settings ORDER BY key)"
        ).fetchone()
        raw = f"{s}|{sn}|{cd}|{pc}|{pm}|{pt}|{pg}|{pf}|{pfm}|{dm}|{ast}|{month_value}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_project_list() -> list[str]:
    if not DB_PATH.exists():
        return []
    cache_key = "project_list"
    cached = _cache_get(cache_key)
    if cached is not None:
        return list(cached)
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT DISTINCT COALESCE(pa.canonical_name, s.project_name) AS project_name FROM sessions s LEFT JOIN project_aliases pa ON s.project_name = pa.alias_name WHERE s.project_name IS NOT NULL ORDER BY LOWER(COALESCE(pa.canonical_name, s.project_name)) ASC").fetchall()
        projects = [row["project_name"] for row in rows if row["project_name"]]
        _cache_set(cache_key, projects)
        return projects


def get_project_list_recent() -> list[dict]:
    """Return canonical projects ordered by their most recent session."""
    if not DB_PATH.exists():
        return []
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT COALESCE(pa.canonical_name, s.project_name) AS project_name,
                   MAX(COALESCE(s.last_seen_time, s.start_time, 0)) AS last_active
            FROM sessions s
            LEFT JOIN project_aliases pa ON s.project_name = pa.alias_name
            WHERE s.project_name IS NOT NULL
              AND TRIM(s.project_name) != ''
            GROUP BY COALESCE(pa.canonical_name, s.project_name)
            ORDER BY last_active DESC, LOWER(project_name) ASC
            """
        ).fetchall()
        return [
            {"project_name": row["project_name"], "last_active": row["last_active"]}
            for row in rows
        ]


def get_project_aliases(project_name: str) -> dict:
    """Return alias information for a project (which projects are merged into it)."""
    if not DB_PATH.exists():
        return {"ok": True, "canonical_name": project_name, "aliases": []}
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        # Resolve to canonical if the given name is itself an alias
        canonical_row = conn.execute(
            "SELECT canonical_name FROM project_aliases WHERE alias_name = ?",
            (project_name,),
        ).fetchone()
        canonical = canonical_row[0] if canonical_row else project_name
        alias_rows = conn.execute(
            "SELECT alias_name FROM project_aliases WHERE canonical_name = ?",
            (canonical,),
        ).fetchall()
        return {
            "ok": True,
            "canonical_name": canonical,
            "aliases": [row[0] for row in alias_rows],
        }


def get_project_report(project_name: str) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data"}
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        aliases = conn.execute("SELECT alias_name FROM project_aliases WHERE canonical_name = ?", (project_name,)).fetchall()
        names = [project_name] + [row[0] for row in aliases]
        placeholders = ",".join("?" * len(names))
        
        rows = conn.execute(f"""
            SELECT id, COALESCE(pa.canonical_name, s.project_name) AS project_name, start_time, last_seen_time, end_time, active_seconds, notes, todo_notes, todos_json
            FROM sessions s
            LEFT JOIN project_aliases pa ON s.project_name = pa.alias_name
            WHERE s.project_name IN ({placeholders}) AND s.active_seconds > 0
            ORDER BY s.start_time DESC
        """, names).fetchall()
        
        if not rows:
            return {
                "project_name": project_name,
                "sessions": [],
                "total_duration": "00:00:00",
                "total_duration_seconds": 0,
                "session_count": 0
            }
        
        # Build per-session maps for condensed entries.
        raw_notes = {}
        raw_todo_notes = {}
        raw_todos = {}
        raw_start_times = {}
        raw_end_times = {}
        raw_last_seen_times = {}
        raw_entries = {}
        for r in rows:
            sid = int(r["id"])
            raw_notes[sid] = (r["notes"] or "").strip()
            raw_todo_notes[sid] = (r["todo_notes"] or "").strip()
            raw_todos[sid] = _parse_todos_json(r["todos_json"])
            raw_start_times[sid] = float(r["start_time"] or 0)
            raw_end_times[sid] = float(r["end_time"]) if r["end_time"] is not None else None
            raw_last_seen_times[sid] = float(r["last_seen_time"] or 0)
            start_ts = r["start_time"]
            end_ts = r["end_time"] if r["end_time"] is not None else r["last_seen_time"]
            start_dt = datetime.fromtimestamp(start_ts)
            end_dt = datetime.fromtimestamp(end_ts) if end_ts else start_dt
            raw_entries[sid] = {
                "id": sid,
                "date": start_dt.strftime("%Y-%m-%d"),
                "day": start_dt.strftime("%a"),
                "start_time": start_dt.strftime("%H:%M"),
                "end_time": end_dt.strftime("%H:%M"),
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
            }
            
        recent = condense_recent_sessions([dict(r) for r in rows])
        # Reverses order to chronological (oldest → newest)
        recent.reverse()
        
        sessions = []
        total_seconds = 0
        
        for row in recent:
            session_ids = row.get("session_ids", [])
            # Concatenate notes
            notes_list = []
            # session_ids is in newest->oldest order if derived from DESC start_time query?
            # Wait, condensation groups them. Let's make sure we sort IDs chronologically or just append.
            # condense_recent_sessions groups them. 
            for sid in session_ids:
                note = raw_notes.get(sid, "")
                if note:
                    notes_list.append(note)
            
            merged_notes = " | ".join(notes_list)
            
            start_dt = datetime.fromtimestamp(row["start_time"])
            end_ts = row["end_time"] if row["end_time"] is not None else row["last_seen_time"]
            end_dt = datetime.fromtimestamp(end_ts) if end_ts else start_dt
            
            dur_sec = int(row["active_seconds"])
            total_seconds += dur_sec
            
            h, rem = divmod(dur_sec, 3600)
            m, s = divmod(rem, 60)
            duration_str = f"{h:02d}:{m:02d}:{s:02d}"
            
            sessions.append({
                "date": start_dt.strftime("%Y-%m-%d"),
                "start_time": start_dt.strftime("%H:%M"),
                "end_time": end_dt.strftime("%H:%M"),
                "duration": duration_str,
                "duration_seconds": dur_sec,
                "notes": merged_notes,
                "session_ids": session_ids,
                "session_entries": [raw_entries[sid] for sid in session_ids if sid in raw_entries],
            })
            
        # Query project note for the canonical project
        project_note = ""
        metadata_row = conn.execute(
            "SELECT project_note FROM project_metadata WHERE project_name = ?",
            (project_name,),
        ).fetchone()
        if metadata_row:
            project_note = (metadata_row["project_note"] or "").strip()

        # Build per-session maps keyed by session id string
        session_notes_map = {}
        session_todo_notes_map = {}
        session_todos_map = {}
        session_start_times_map = {}
        session_end_times_map = {}
        session_last_seen_times_map = {}
        for sid in raw_notes:
            sid_str = str(sid)
            if raw_notes.get(sid):
                session_notes_map[sid_str] = raw_notes[sid]
            if raw_todo_notes.get(sid):
                session_todo_notes_map[sid_str] = raw_todo_notes[sid]
            todos = raw_todos.get(sid, [])
            if todos:
                session_todos_map[sid_str] = todos
            session_start_times_map[sid_str] = raw_start_times.get(sid, 0)
            session_end_times_map[sid_str] = raw_end_times.get(sid)
            session_last_seen_times_map[sid_str] = raw_last_seen_times.get(sid, 0)

        th, trem = divmod(total_seconds, 3600)
        tm, ts = divmod(trem, 60)
        
        return {
            "project_name": project_name,
            "sessions": sessions,
            "total_duration": f"{th:02d}:{tm:02d}:{ts:02d}",
            "total_duration_seconds": total_seconds,
            "session_count": len(sessions),
            "project_note": project_note,
            "notes": session_notes_map,
            "todo_notes": session_todo_notes_map,
            "todos": session_todos_map,
            "start_times": session_start_times_map,
            "end_times": session_end_times_map,
            "last_seen_times": session_last_seen_times_map,
        }


def get_stats(month_value: str = "", recent_before: float | None = None) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    try:
        with db_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            category_options, _ = get_category_maps(conn)
            project_categories = get_project_categories(conn)
            project_metadata = get_project_metadata(conn)
            project_tasks = get_project_tasks_by_project(conn)
            planner_goals = get_planner_goals(conn)
            artists = get_artists(conn)

            activity_rows = conn.execute("""
                SELECT COALESCE(pa.canonical_name, s.project_name) AS project_name,
                       s.start_time, s.last_seen_time, s.end_time, s.active_seconds
                FROM sessions s
                LEFT JOIN project_aliases pa ON s.project_name = pa.alias_name
                WHERE s.active_seconds > 0
            """).fetchall()
            daily_totals, hourly_totals = build_activity_rollups(activity_rows)

            projects = conn.execute("""
                SELECT COALESCE(pa.canonical_name, s.project_name) AS project_name,
                       SUM(s.active_seconds)  AS total_seconds,
                       COUNT(*)             AS session_count,
                       MIN(s.start_time)      AS first_seen,
                       MAX(COALESCE(s.end_time, s.last_seen_time, s.start_time)) AS last_seen,
                       AVG(s.active_seconds)  AS avg_seconds
                FROM   sessions s
                LEFT JOIN project_aliases pa ON s.project_name = pa.alias_name
                WHERE  s.active_seconds > 0
                GROUP  BY COALESCE(pa.canonical_name, s.project_name)
                ORDER  BY total_seconds DESC
            """).fetchall()

            today = date.today()
            selected_month_start = parse_month_key(month_value) if month_value else today.replace(day=1)
            current_month_start = today.replace(day=1)
            if selected_month_start > current_month_start:
                selected_month_start = current_month_start
            selected_month_end = (
                selected_month_start.replace(day=28) + timedelta(days=4)
            ).replace(day=1) - timedelta(days=1)
            selected_month_key = selected_month_start.strftime("%Y-%m")
            previous_month_end = selected_month_start - timedelta(days=1)
            previous_month_start = previous_month_end.replace(day=1)
            last_year_ago = (today - timedelta(days=364)).isoformat()
            year_daily = [
                {"day": day, "total_seconds": total_seconds}
                for day, total_seconds in sorted(daily_totals.items())
                if day >= last_year_ago
            ]

            year_hourly = [
                {"day": day, "hour": hour, "active_seconds": active_seconds}
                for (day, hour), active_seconds in sorted(hourly_totals.items())
                if day >= last_year_ago
            ]

            recent_params: tuple[float, ...] = ()
            recent_before_clause = ""
            if recent_before and recent_before > 0:
                recent_before_clause = "AND start_time < ?"
                recent_params = (recent_before,)

            recent = conn.execute(f"""
                SELECT id, project_name, start_time, last_seen_time, end_time, active_seconds, notes, todo_notes, todos_json
                FROM   sessions
                WHERE  (active_seconds >= 5 OR end_time IS NULL)
                  {recent_before_clause}
                ORDER  BY start_time DESC
                LIMIT  240
            """, recent_params).fetchall()
            # Build notes, start-time, end-time, and last-seen maps from raw rows before condensation
            raw_notes = {}
            raw_todo_notes = {}
            raw_todos = {}
            raw_start_times = {}
            raw_end_times = {}
            raw_last_seen_times = {}
            raw_active_seconds = {}
            for r in recent:
                sid = int(r["id"])
                raw_notes[sid] = (r["notes"] or "").strip()
                raw_todo_notes[sid] = (r["todo_notes"] or "").strip()
                raw_todos[sid] = _parse_todos_json(r["todos_json"])
                raw_start_times[sid] = float(r["start_time"] or 0)
                raw_end_times[sid] = float(r["end_time"]) if r["end_time"] is not None else None
                raw_last_seen_times[sid] = float(r["last_seen_time"] or 0)
                raw_active_seconds[sid] = float(r["active_seconds"] or 0)
            condensed_recent = condense_recent_sessions(recent)
            recent_has_more = len(condensed_recent) > RECENT_SESSION_PAGE_SIZE
            recent = condensed_recent[:RECENT_SESSION_PAGE_SIZE]

            today_str   = today.isoformat()
            week_start_weekday = int(get_app_setting("week_start_weekday") or DEFAULT_WEEK_START_WEEKDAY)
            goal_week_start, goal_week_end = get_week_range(today, week_start_weekday)
            month_start_key = selected_month_start.isoformat()
            month_end_key = selected_month_end.isoformat()
            previous_month_start_key = previous_month_start.isoformat()
            previous_month_end_key = previous_month_end.isoformat()

            today_session_seconds = []
            today_project_names = set()
            month_per_project = {}
            previous_month_per_project = {}
            for row in activity_rows:
                row_today_seconds = 0.0
                row_month_seconds = 0.0
                row_previous_month_seconds = 0.0
                end_time = row["end_time"] or row["last_seen_time"] or row["start_time"]
                for day_key, _hour, seconds in allocate_session_activity(
                    row["start_time"],
                    end_time,
                    row["active_seconds"],
                ):
                    if day_key == today_str:
                        row_today_seconds += seconds
                    if month_start_key <= day_key <= month_end_key:
                        row_month_seconds += seconds
                    if previous_month_start_key <= day_key <= previous_month_end_key:
                        row_previous_month_seconds += seconds
                if row_today_seconds > 0:
                    today_session_seconds.append(row_today_seconds)
                    today_project_names.add(row["project_name"])
                if row_month_seconds > 0:
                    project_name = row["project_name"]
                    month_per_project[project_name] = month_per_project.get(project_name, 0.0) + row_month_seconds
                if row_previous_month_seconds > 0:
                    project_name = row["project_name"]
                    previous_month_per_project[project_name] = (
                        previous_month_per_project.get(project_name, 0.0) + row_previous_month_seconds
                    )

            today_session_count = len(today_session_seconds)
            today_avg_session_seconds = (
                sum(today_session_seconds) / today_session_count
                if today_session_count
                else 0
            )

            def scalar(q, *p):
                return conn.execute(q, p).fetchone()[0] or 0

            total_s  = scalar("SELECT COALESCE(SUM(active_seconds),0) FROM sessions")
            today_s  = daily_totals.get(today_str, 0)
            week_s   = sum(
                seconds
                for day_key, seconds in daily_totals.items()
                if goal_week_start.isoformat() <= day_key <= goal_week_end.isoformat()
            )
            month_s  = sum(month_per_project.values())
            month_project_count = len(month_per_project)
            closed_session_count = scalar("SELECT COUNT(*) FROM sessions WHERE end_time IS NOT NULL")

            placeholders = ",".join("?" * len(UNTITLED_NAMES))
            unsaved_closed_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sessions
                WHERE end_time IS NOT NULL
                  AND LOWER(TRIM(project_name)) IN ({placeholders})
                """,
                tuple(UNTITLED_NAMES),
            ).fetchone()[0] or 0
            phantom_closed_count = count_phantom_sessions()

            # Streak: consecutive days with activity ending today (or yesterday, right after midnight)
            active_days = {day for day, seconds in daily_totals.items() if seconds > 0}
            streak = 0
            cursor_day = None
            if today.isoformat() in active_days:
                cursor_day = today
            elif (today - timedelta(days=1)).isoformat() in active_days:
                cursor_day = today - timedelta(days=1)

            while cursor_day and cursor_day.isoformat() in active_days:
                streak += 1
                cursor_day -= timedelta(days=1)

            production_run = calculate_production_run(daily_totals, today)

            now_ts = time.time()

            # Currently active session (end_time IS NULL)
            live = conn.execute("""
                SELECT COALESCE(pa.canonical_name, s.project_name) AS project_name,
                       s.start_time, s.last_seen_time, s.active_seconds
                FROM sessions s
                LEFT JOIN project_aliases pa ON s.project_name = pa.alias_name
                WHERE end_time IS NULL ORDER BY start_time DESC LIMIT 1
            """).fetchone()
            live_duration_seconds = 0.0
            live_start_time = None
            ableton_has_project = False
            if live:
                live_start_time = float(live["start_time"] or 0)
                live_last_seen = float(live["last_seen_time"] or live_start_time or now_ts)
                live_active_seconds = float(live["active_seconds"] or 0)
                live_duration_seconds = max(
                    live_active_seconds + max(0.0, now_ts - live_last_seen),
                    0.0,
                )
                ableton_has_project = bool((live["project_name"] or "").strip())
            ableton_running = True if ableton_has_project else is_ableton_running()

            display_names = {
                row["project_name"]: project_metadata.get(row["project_name"], {}).get(
                    "display_name", row["project_name"]
                )
                for row in projects
            }
            campaign_names = sorted(
                month_per_project,
                key=lambda name: (-month_per_project[name], display_names.get(name, name).casefold(), name.casefold()),
            )
            previous_campaign_names = sorted(
                previous_month_per_project,
                key=lambda name: (-previous_month_per_project[name], display_names.get(name, name).casefold(), name.casefold()),
            )
            month_ranks = {name: rank for rank, name in enumerate(campaign_names, 1)}
            previous_month_ranks = {name: rank for rank, name in enumerate(previous_campaign_names, 1)}

            project_rows = []
            for row in projects:
                project = dict(row)
                category = project_categories.get(project["project_name"])
                project["category_key"] = category["key"] if category else None
                project["category_label"] = category["label"] if category else None
                project["category_color"] = category["color"] if category else None
                metadata = project_metadata.get(project["project_name"], {})
                project["status"] = metadata.get("status", "")
                project["display_name"] = metadata.get("display_name", project["project_name"])
                project["status_label"] = metadata.get("status_label", "")
                project["type"] = metadata.get("type", "")
                project["type_label"] = metadata.get("type_label", "")
                project["priority"] = metadata.get("priority", "")
                project["priority_label"] = metadata.get("priority_label", "")
                project["due_date"] = metadata.get("due_date", "")
                project["hard_deadline"] = metadata.get("hard_deadline", "")
                project["turn_in_date"] = metadata.get("turn_in_date", "")
                project["artist_id"] = metadata.get("artist_id", "")
                project["progress_percent"] = metadata.get("progress_percent", 0)
                project["pinned"] = metadata.get("pinned", False)
                project["project_note"] = metadata.get("project_note", "")
                project["board_order"] = metadata.get("board_order", 0)
                project["completed_at"] = metadata.get("completed_at", 0)
                project["prior_status"] = metadata.get("prior_status", "")
                project["prior_progress_percent"] = metadata.get("prior_progress_percent", 0)
                project.update(_project_deadline_summary(metadata, today))
                project["project_tasks"] = project_tasks.get(project["project_name"], [])
                project["month_seconds"] = month_per_project.get(project["project_name"], 0)
                project["previous_month_seconds"] = previous_month_per_project.get(project["project_name"], 0)
                project["month_rank"] = month_ranks.get(project["project_name"])
                project["previous_month_rank"] = previous_month_ranks.get(project["project_name"])
                project["rank_delta"] = (
                    previous_month_ranks[project["project_name"]] - month_ranks[project["project_name"]]
                    if project["project_name"] in month_ranks and project["project_name"] in previous_month_ranks
                    else None
                )
                project["month_share_percent"] = (
                    project["month_seconds"] / month_s * 100.0 if month_s else 0.0
                )
                project["is_live_project"] = bool(
                    live and project["project_name"] == live["project_name"]
                )
                project_rows.append(project)

            recent_rows = []
            for row in recent:
                category = project_categories.get(row["project_name"])
                metadata = project_metadata.get(row["project_name"], {})
                deadline_summary = _project_deadline_summary(metadata, today)
                session_ids = row.get("session_ids", [])
                # Build per-session notes, start-times, end-times, and last-seen maps
                session_notes = {}
                session_todo_notes = {}
                session_todos = {}
                session_start_times = {}
                session_end_times = {}
                session_last_seen_times = {}
                session_active_seconds = {}
                for sid in session_ids:
                    note = raw_notes.get(sid, "")
                    if note:
                        session_notes[str(sid)] = note
                    todo_note = raw_todo_notes.get(sid, "")
                    if todo_note:
                        session_todo_notes[str(sid)] = todo_note
                    todos = raw_todos.get(sid, [])
                    if todos:
                        session_todos[str(sid)] = todos
                    session_start_times[str(sid)] = raw_start_times.get(sid, 0)
                    session_end_times[str(sid)] = raw_end_times.get(sid)
                    session_last_seen_times[str(sid)] = raw_last_seen_times.get(sid, 0)
                    session_active_seconds[str(sid)] = raw_active_seconds.get(sid, 0)
                recent_rows.append(
                    {
                        "project_name": row["project_name"],
                        "display_name": metadata.get("display_name", row["project_name"]),
                        "start_time": row["start_time"],
                        "end_time": row["end_time"],
                        "active_seconds": row["active_seconds"],
                        "session_ids": session_ids,
                        "session_notes": session_notes,
                        "session_todo_notes": session_todo_notes,
                        "session_todos": session_todos,
                        "session_start_times": session_start_times,
                        "session_end_times": session_end_times,
                        "session_last_seen_times": session_last_seen_times,
                        "session_active_seconds": session_active_seconds,
                        "category_key": category["key"] if category else None,
                        "category_label": category["label"] if category else None,
                        "category_color": category["color"] if category else None,
                        "status": metadata.get("status", ""),
                        "status_label": metadata.get("status_label", ""),
                        "type": metadata.get("type", ""),
                        "type_label": metadata.get("type_label", ""),
                        "priority": metadata.get("priority", ""),
                        "priority_label": metadata.get("priority_label", ""),
                        "due_date": metadata.get("due_date", ""),
                        "hard_deadline": metadata.get("hard_deadline", ""),
                        "turn_in_date": metadata.get("turn_in_date", ""),
                        "artist_id": metadata.get("artist_id", ""),
                        "progress_percent": metadata.get("progress_percent", 0),
                        "pinned": metadata.get("pinned", False),
                        "project_note": metadata.get("project_note", ""),
                        "board_order": metadata.get("board_order", 0),
                        "deadline_state": deadline_summary["deadline_state"],
                        "deadline_label": deadline_summary["deadline_label"],
                        "deadline_reasons": deadline_summary["deadline_reasons"],
                    }
                )

            return {
                "summary": {
                    "total_seconds":  total_s,
                    "today_seconds":  today_s,
                    "today_average_session_seconds": today_avg_session_seconds,
                    "today_session_count": today_session_count,
                    "today_project_count": len(today_project_names),
                    "week_seconds":   week_s,
                    "goal_week_start": goal_week_start.isoformat(),
                    "goal_week_end": goal_week_end.isoformat(),
                    "week_start_weekday": week_start_weekday,
                    "week_start_weekday_name": WEEKDAY_NAMES[week_start_weekday],
                    "week_end_weekday_name": WEEKDAY_NAMES[(week_start_weekday + 6) % 7],
                    "month_seconds":  month_s,
                    "month_project_count": month_project_count,
                    "selected_month": selected_month_key,
                    "selected_month_start": month_start_key,
                    "selected_month_end": month_end_key,
                    "selected_month_label": selected_month_start.strftime("%B %Y"),
                    "selected_month_is_current": selected_month_key == current_month_start.strftime("%Y-%m"),
                    "project_count":  len(projects),
                    "streak_days":    streak,
                    "production_run": production_run,
                    "live_project":   live["project_name"] if live else None,
                    "live_session_start_time": live_start_time,
                    "live_session_duration_seconds": live_duration_seconds,
                    "ableton_running": ableton_running,
                    "ableton_has_project": ableton_has_project,
                    "generated_at": now_ts,
                    "closed_session_count": closed_session_count,
                    "unsaved_closed_count": unsaved_closed_count,
                    "phantom_closed_count": phantom_closed_count,
                },
                "projects": project_rows,
                "project_folders": get_project_folders(conn),
                "year_daily": [dict(r) for r in year_daily],
                "year_hourly": [dict(r) for r in year_hourly],
                "recent": recent_rows,
                "recent_has_more": recent_has_more,
                "recent_oldest_start_time": min((row["start_time"] for row in recent_rows), default=None),
                "category_options": category_options,
                "project_status_options": list(get_project_status_options(conn).items()),
                "project_type_options": list(get_project_type_options(conn).items()),
                "planner_goals": planner_goals,
                "artists": artists,
                "custom_category_limit": MAX_CUSTOM_CATEGORIES,
                "custom_category_count": len(category_options),
                "project_board_lane_order": get_project_board_lane_order(conn),
            }
    except sqlite3.Error as e:
        return {"error": str(e)}
    except Exception:
        traceback.print_exc()
        raise


# ─────────────────────────────────────────────────────────────
#  HTML template
# ─────────────────────────────────────────────────────────────

def _load_html() -> str:
    if TEMPLATE_PATH.exists():
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    return HTML

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ableton Tracker</title>
<script>
(() => {
  try {
    const theme = localStorage.getItem('ableton_tracker_theme');
    if (theme === 'dark' || theme === 'light') {
      document.documentElement.dataset.theme = theme;
    }
  } catch (e) {}
})();
</script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  color-scheme:light;
  --bg:#f5f5f7;
  --bg-elev:rgba(255,255,255,.88);
  --surface:rgba(255,255,255,.72);
  --surface-2:rgba(255,255,255,.94);
  --surface-hover:rgba(60,60,67,.06);
  --border:rgba(60,60,67,.12);
  --border-strong:rgba(60,60,67,.18);
  --ink:#1d1d1f;
  --ink-2:#3a3a3c;
  --ink-3:#6e6e73;
  --ink-4:#8e8e93;
  --accent:#007aff;
  --accent-soft:rgba(0,122,255,.10);
  --accent-line:rgba(0,122,255,.18);
  --live:#34c759;
  --danger:#ff3b30;
  --danger-soft:rgba(255,59,48,.08);
  --page-wash:linear-gradient(180deg, rgba(255,255,255,.84), rgba(255,255,255,0) 220px);
  --page-glow-a:radial-gradient(900px 500px at 0% 0%, rgba(0,122,255,.05), transparent 60%);
  --page-glow-b:radial-gradient(700px 420px at 100% 0%, rgba(90,200,250,.07), transparent 52%);
  --header-bg:rgba(245,245,247,.78);
  --logo-bg:linear-gradient(180deg, #ffffff, #f2f7ff);
  --logo-border:rgba(0,122,255,.12);
  --logo-shadow:0 10px 24px rgba(0,122,255,.08);
  --glass-highlight:linear-gradient(180deg, rgba(255,255,255,.182), rgba(255,255,255,0));
  --control-bg:rgba(255,255,255,.8);
  --control-bg-strong:#fff;
  --control-inner:rgba(255,255,255,.96);
  --row-border:rgba(60,60,67,.08);
  --bar-bg:rgba(60,60,67,.08);
  --toast-shadow:0 18px 40px rgba(15,23,42,.12);
  --modal-backdrop:rgba(15,23,42,.22);

  --font-display:-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif;
  --font-body:-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
  --font-mono:"SF Mono", SFMono-Regular, ui-monospace, Menlo, monospace;

  --radius:22px;
  --radius-sm:14px;
  --shadow-1:0 1px 2px rgba(15,23,42,.04), 0 18px 40px rgba(15,23,42,.06);
  --shadow-card-hover:0 12px 24px rgba(15,23,42,.10), 0 28px 58px rgba(255,255,255,.0845);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --bg:#0b0d10;
    --bg-elev:rgba(24,26,31,.9);
    --surface:rgba(28,31,36,.72);
    --surface-2:rgba(37,40,46,.94);
    --surface-hover:rgba(235,235,245,.07);
    --border:rgba(235,235,245,.12);
    --border-strong:rgba(235,235,245,.2);
    --ink:#f5f5f7;
    --ink-2:#d7d7dc;
    --ink-3:#aaaab3;
    --ink-4:#7f808a;
    --accent:#0a84ff;
    --accent-soft:rgba(10,132,255,.16);
    --accent-line:rgba(10,132,255,.28);
    --live:#30d158;
    --danger:#ff453a;
    --danger-soft:rgba(255,69,58,.14);
    --page-wash:linear-gradient(180deg, rgba(22,24,29,.9), rgba(11,13,16,0) 240px);
    --page-glow-a:radial-gradient(900px 500px at 0% 0%, rgba(10,132,255,.14), transparent 60%);
    --page-glow-b:radial-gradient(700px 420px at 100% 0%, rgba(100,210,255,.1), transparent 52%);
    --header-bg:rgba(11,13,16,.78);
    --logo-bg:linear-gradient(180deg, rgba(41,45,54,.94), rgba(21,24,30,.94));
    --logo-border:rgba(10,132,255,.26);
    --logo-shadow:0 10px 24px rgba(0,0,0,.24);
    --glass-highlight:linear-gradient(180deg, rgba(255,255,255,.0416), rgba(255,255,255,0));
    --control-bg:rgba(38,41,48,.82);
    --control-bg-strong:rgba(50,54,62,.96);
    --control-inner:rgba(18,20,25,.96);
    --row-border:rgba(235,235,245,.08);
    --bar-bg:rgba(235,235,245,.1);
    --toast-shadow:0 18px 40px rgba(0,0,0,.38);
    --modal-backdrop:rgba(0,0,0,.48);
    --shadow-1:0 1px 2px rgba(0,0,0,.2), 0 18px 42px rgba(0,0,0,.26);
    --shadow-card-hover:0 16px 30px rgba(0,0,0,.34), 0 30px 62px rgba(255,255,255,.117);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#0b0d10;
  --bg-elev:rgba(24,26,31,.9);
  --surface:rgba(28,31,36,.72);
  --surface-2:rgba(37,40,46,.94);
  --surface-hover:rgba(235,235,245,.07);
  --border:rgba(235,235,245,.12);
  --border-strong:rgba(235,235,245,.2);
  --ink:#f5f5f7;
  --ink-2:#d7d7dc;
  --ink-3:#aaaab3;
  --ink-4:#7f808a;
  --accent:#0a84ff;
  --accent-soft:rgba(10,132,255,.16);
  --accent-line:rgba(10,132,255,.28);
  --live:#30d158;
  --danger:#ff453a;
  --danger-soft:rgba(255,69,58,.14);
  --page-wash:linear-gradient(180deg, rgba(22,24,29,.9), rgba(11,13,16,0) 240px);
  --page-glow-a:radial-gradient(900px 500px at 0% 0%, rgba(10,132,255,.14), transparent 60%);
  --page-glow-b:radial-gradient(700px 420px at 100% 0%, rgba(100,210,255,.1), transparent 52%);
  --header-bg:rgba(11,13,16,.78);
  --logo-bg:linear-gradient(180deg, rgba(41,45,54,.94), rgba(21,24,30,.94));
  --logo-border:rgba(10,132,255,.26);
  --logo-shadow:0 10px 24px rgba(0,0,0,.24);
  --glass-highlight:linear-gradient(180deg, rgba(255,255,255,.0416), rgba(255,255,255,0));
  --control-bg:rgba(38,41,48,.82);
  --control-bg-strong:rgba(50,54,62,.96);
  --control-inner:rgba(18,20,25,.96);
  --row-border:rgba(235,235,245,.08);
  --bar-bg:rgba(235,235,245,.1);
  --toast-shadow:0 18px 40px rgba(0,0,0,.38);
  --modal-backdrop:rgba(0,0,0,.48);
  --shadow-1:0 1px 2px rgba(0,0,0,.2), 0 18px 42px rgba(0,0,0,.26);
  --shadow-card-hover:0 16px 30px rgba(0,0,0,.34), 0 30px 62px rgba(255,255,255,.117);
}
:root[data-theme="light"]{color-scheme:light}
html{font-size:15px}
body{
  font-family:var(--font-body);
  background:var(--bg);
  color:var(--ink);
  min-height:100vh;
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
  font-feature-settings:"ss01","cv11";
  letter-spacing:-0.01em;
  background-image:
    var(--page-wash),
    var(--page-glow-a),
    var(--page-glow-b);
  background-attachment:fixed;
}

header{
  position:sticky;top:0;z-index:20;
  background:var(--header-bg);
  backdrop-filter:saturate(180%) blur(18px);
  -webkit-backdrop-filter:saturate(180%) blur(18px);
  border-bottom:1px solid var(--border);
  padding:0 36px;height:68px;
  display:flex;align-items:center;justify-content:space-between;
}
.logo{display:flex;align-items:center;gap:14px}
.logo-icon{
  width:36px;height:36px;border-radius:12px;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  background:var(--logo-bg);
  border:1px solid var(--logo-border);
  box-shadow:var(--logo-shadow);
}
.logo-icon svg rect{fill:var(--accent)}
.logo-text{display:flex;flex-direction:column;line-height:1}
.logo-text h1{
  font-family:var(--font-display);font-weight:650;font-size:20px;
  color:var(--ink);letter-spacing:-.025em;
}
.logo-text h1 em{font-style:normal;color:var(--ink-3);font-weight:550}

.header-nav{
  display:inline-flex;align-items:center;gap:6px;
  padding:4px;border-radius:999px;
  background:var(--control-bg);border:1px solid var(--border);
}
.nav-tab{
  border:none;background:transparent;color:var(--ink-3);
  font-size:13px;font-weight:650;padding:9px 14px;border-radius:999px;cursor:pointer;
  transition:background .18s ease, color .18s ease, transform .18s ease;
}
.nav-tab:hover{color:var(--ink);background:var(--surface-hover)}
.nav-tab.is-active{
  color:var(--ink);background:var(--control-bg-strong);
  box-shadow:0 1px 2px rgba(15,23,42,.06);
}

.header-right{display:flex;align-items:center;gap:14px}
.theme-toggle{
  width:38px;height:38px;border-radius:999px;
  border:1px solid var(--border);
  background:var(--control-bg);
  color:var(--ink-2);
  display:grid;place-items:center;cursor:pointer;
  transition:background .18s ease, border-color .18s ease, color .18s ease, transform .18s ease;
}
.theme-toggle:hover{background:var(--control-bg-strong);border-color:var(--border-strong);color:var(--ink)}
.theme-toggle:active{transform:translateY(1px)}
.theme-toggle svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.theme-toggle .sun{display:none}
:root[data-theme="dark"] .theme-toggle .moon{display:none}
:root[data-theme="dark"] .theme-toggle .sun{display:block}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]) .theme-toggle .moon{display:none}
  :root:not([data-theme="light"]) .theme-toggle .sun{display:block}
}
.live-badge{
  display:inline-flex;align-items:center;gap:8px;
  background:rgba(52,199,89,.10);
  border:1px solid rgba(52,199,89,.18);
  color:var(--live);
  padding:7px 12px;border-radius:999px;
  font-size:12px;font-weight:600;
}
.live-badge .dot{
  width:6px;height:6px;border-radius:50%;
  background:var(--live);box-shadow:0 0 8px rgba(52,199,89,.5);
  animation:pulse 1.6s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.55;transform:scale(.88)}}
.updated{
  font-size:12px;color:var(--ink-4);
}

.page{max-width:1200px;margin:0 auto;padding:40px 36px 80px;position:relative;z-index:1}

.intro{margin-bottom:32px;display:flex;justify-content:space-between;align-items:flex-end;gap:24px;flex-wrap:wrap}
.intro h2{
  font-family:var(--font-display);font-weight:650;
  font-size:clamp(32px,4vw,46px);line-height:1.05;letter-spacing:-.04em;
  color:var(--ink);
}
.intro h2 em{font-style:normal;color:var(--ink-3);font-weight:500}
.page-title-dashboard{
  color:var(--ink);
  font-weight:780;
}
:root[data-theme="dark"] .page-title-dashboard{
  color:#fff;
}
.intro .sub{
  font-size:13px;color:var(--ink-4);margin-top:12px;
}
.intro .sub > span:not(.session-presence){color:var(--ink-2)}
.session-presence{
  display:inline-flex;align-items:center;gap:12px;
  flex-wrap:nowrap;
}
.session-status{
  display:grid;grid-template-columns:max-content 24px;align-items:center;column-gap:4px;
  color:var(--ink-2);
}
.session-status__text{
  color:inherit;
  white-space:nowrap;
}
.session-status__indicator{
  display:inline-flex;align-items:center;gap:4px;
  min-width:24px;
  justify-content:flex-start;
}
.session-status__dot{
  width:10px;height:10px;border-radius:999px;
  display:inline-block;flex:0 0 auto;
  background:#ff453a;
  box-shadow:0 0 0 1px rgba(0,0,0,.06), 0 0 10px rgba(255,69,58,.35);
}
.session-status__indicator--off .session-status__dot{
  background:#7d7d84;
  box-shadow:none;
}
.session-status__indicator--booting{
  min-width:24px;
}
.session-status__indicator--booting .session-status__dot{
  background:#ffffff;
  box-shadow:0 0 0 1px rgba(0,0,0,.14), 0 0 12px rgba(255,255,255,.28);
  animation:bootBlink 0.545s ease-in-out infinite;
}
.session-status__timer{
  display:grid;grid-template-columns:44px 72px;align-items:center;column-gap:10px;
  min-width:126px;
  padding:4px 10px;
  border:1px solid var(--border);
  border-radius:999px;
  background:color-mix(in srgb, var(--surface) 82%, transparent);
  color:var(--ink-2);
}
.session-status__timer-label{
  font-size:11px;
  color:var(--ink-4);
  text-transform:uppercase;
  letter-spacing:.08em;
}
.session-status__timer-value{
  font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;
  font-size:12px;
  font-variant-numeric:tabular-nums;
  text-align:right;
  white-space:nowrap;
}
.session-status__timer[data-state="live"]{
  color:var(--ink);
  border-color:color-mix(in srgb, #30d158 22%, var(--border));
  background:color-mix(in srgb, #30d158 12%, var(--surface));
}
.session-status__timer[data-state="booting"]{
  border-color:color-mix(in srgb, #ffffff 20%, var(--border));
}
.session-status__timer[data-state="off"] .session-status__timer-value{
  color:var(--ink-4);
}
@media (max-width: 640px){
  .session-presence{
    display:grid;
    grid-template-columns:1fr;
    row-gap:8px;
    align-items:flex-start;
  }
  .session-status__timer{
    width:100%;
    max-width:220px;
  }
}
@keyframes bootBlink{
  0%, 18%, 100%{opacity:1;transform:scale(1)}
  50%{opacity:.18;transform:scale(.82)}
}

.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:28px}
@media (max-width: 1100px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
.card,
.chart-card,
.table-card{
  background:var(--surface);
  border:1px solid var(--border);
  backdrop-filter:saturate(180%) blur(20px);
  -webkit-backdrop-filter:saturate(180%) blur(20px);
  box-shadow:var(--shadow-1);
}
.card{
  border-radius:var(--radius);
  min-height:203px;
  padding:28px 22px 24px;
  position:relative;overflow:hidden;
  display:flex;flex-direction:column;align-items:flex-start;
  transform:translateZ(0);
  transform-origin:center top;
  will-change:transform;
  transition:border-color .22s ease, background .22s ease, box-shadow .26s ease, transform .26s cubic-bezier(.2,.8,.2,1);
}
.card::after{
  content:"";position:absolute;inset:0;pointer-events:none;
  background:var(--glass-highlight);
  opacity:.75;
  transition:opacity .24s ease;
}
.card:hover{
  border-color:var(--border-strong);
  background:var(--surface-2);
  box-shadow:var(--shadow-card-hover);
  transform:scale(1.025);
  z-index:2;
}
.card:hover::after{opacity:1}
.card-label{
  font-size:12px;font-weight:650;color:var(--ink-3);margin-bottom:18px;
  text-transform:uppercase;letter-spacing:.04em;
}
.card-value{
  font-family:var(--font-display);font-weight:680;
  font-size:34px;line-height:1.12;letter-spacing:-.03em;color:var(--ink);
  font-variant-numeric:tabular-nums;
}
.card-value--text{
  font-size:23px;line-height:1.2;letter-spacing:-.02em;
  white-space:normal;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  max-width:100%;
}
.proj-dot{
  display:inline-block;width:8px;height:8px;border-radius:50%;
  margin-right:6px;vertical-align:middle;
}
.card-value.accent em{font-style:normal;font-weight:550;color:var(--ink-3)}
.card-value .unit{
  font-family:var(--font-body);font-size:13px;font-weight:500;
  color:var(--ink-4);margin-left:3px;
}
.card-sub{
  font-size:12px;color:var(--ink-4);margin-top:12px;
  line-height:1.45;
}
.card-chip{
  display:inline-flex;align-items:center;
  margin-top:18px;
  font-size:11px;font-weight:650;
  padding:4px 9px;border-radius:999px;
  color:var(--ink-3);
  background:rgba(120,120,128,.12);
  font-variant-numeric:tabular-nums;
  letter-spacing:.01em;
}
.card-sub:last-child{margin-top:auto;padding-top:12px}
.card-chip--good{
  color:#2c8a4a;background:rgba(48,164,85,.14);
}
@media (prefers-color-scheme: dark){
  .card-chip--good{color:#5fd97e;background:rgba(48,164,85,.18)}
}
:root[data-theme="dark"] .card-chip--good{color:#5fd97e;background:rgba(48,164,85,.18)}
.card-chip--record{
  color:var(--accent);background:var(--accent-soft);
}

.chart-row{display:grid;grid-template-columns:1.4fr 1fr;gap:14px;margin-bottom:28px;align-items:start}
.rings-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:28px;align-items:stretch}
.chart-card{
  border-radius:var(--radius);
  padding:24px 26px 20px;
}
.chart-card-wide{margin-bottom:28px}
.chart-wrap{position:relative;min-height:240px}
.target-stack{
  display:grid;grid-template-rows:repeat(2,minmax(0,1fr));gap:14px;
  align-self:stretch;
}
.target-card{
  display:grid;grid-template-rows:auto 1fr;
  min-height:410px;
  padding:28px 30px 26px;
  position:relative;overflow:hidden;
  transform-origin:center top;
  backdrop-filter:none;
  -webkit-backdrop-filter:none;
  transition:border-color .22s ease, background .22s ease, box-shadow .26s ease, transform .26s cubic-bezier(.2,.8,.2,1);
}
.target-card::after{
  content:"";position:absolute;inset:0;pointer-events:none;
  background:var(--glass-highlight);
  opacity:.72;
  transition:opacity .24s ease;
}
.target-card:hover{
  border-color:var(--border-strong);
  background:var(--surface-2);
  box-shadow:var(--shadow-card-hover);
  transform:scale(1.02);
  z-index:2;
}
.target-card:hover::after{opacity:1}
.target-card .chart-wrap{
  min-height:0;height:100%;
}
.chart-empty{
  height:240px;display:flex;align-items:center;justify-content:center;
  color:var(--ink-4);font-size:13px;
  border:1px dashed var(--border);border-radius:16px;
}
.heatmap-shell{display:grid;gap:16px}
.heatmap-meta{
  display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:14px;
}
.heatmap-summary{
  display:grid;gap:6px;justify-items:start;min-width:0;
  font-size:12px;color:var(--ink-4);
}
.heatmap-summary strong{
  display:block;
  font-size:13px;color:var(--ink-2);font-weight:650;
}
.heatmap-summary span{
  display:block;min-width:0;
}
.heatmap-controls{
  display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:nowrap;
  justify-self:end;white-space:nowrap;
}
.heatmap-week-label{
  font-size:12px;color:var(--ink-3);font-weight:700;
}
.heatmap-nav{
  display:inline-flex;align-items:center;gap:8px;
}
.heatmap-nav-btn{
  width:34px;height:34px;border-radius:999px;border:1px solid rgba(45,70,39,.12);
  background:var(--control-bg-strong);color:var(--ink-2);display:grid;place-items:center;
  box-shadow:0 10px 24px rgba(34,57,31,.08);
  transition:transform .16s ease, box-shadow .16s ease, border-color .16s ease, background .16s ease;
}
.heatmap-nav-btn:hover{
  transform:translateY(-1px);
  border-color:rgba(45,70,39,.18);
  box-shadow:0 14px 28px rgba(34,57,31,.12);
}
.heatmap-nav-btn:disabled{
  opacity:.45;cursor:not-allowed;transform:none;box-shadow:none;
}
.heatmap-scale{
  display:inline-flex;align-items:center;gap:8px;
  font-size:11px;color:var(--ink-4);
  padding:8px 12px;border-radius:999px;
  background:rgba(62,94,53,.08);
  flex:0 0 auto;
}
.heatmap-scale-row{display:inline-flex;align-items:center;gap:5px}
.heatmap-swatch{
  width:14px;height:14px;border-radius:999px;border:none;
  box-shadow:0 0 12px rgba(255,188,48,.18);
}
.heatmap-stage{
  padding:16px 16px 18px;overflow:hidden;border-radius:30px;
  background:
    radial-gradient(circle at 50% 10%, rgba(255,255,255,.06), transparent 40%),
    linear-gradient(180deg, rgba(90,128,78,.98) 0%, rgba(70,102,60,.98) 100%);
  box-shadow:inset 0 0 0 1px rgba(19,42,21,.14), 0 20px 42px rgba(28,51,31,.16);
}
.heatmap-grid-wrap{
  display:flex;gap:0;
}
.heatmap-yaxis{
  display:flex;flex-direction:column;width:34px;flex-shrink:0;
  padding-top:38px;gap:1px;
}
.heatmap-yaxis-label{
  height:13px;font-size:10px;line-height:13px;font-weight:600;
  text-align:right;padding-right:7px;letter-spacing:.02em;
  color:rgba(244,247,239,.92);white-space:nowrap;
  text-shadow:0 1px 2px rgba(19,42,21,.45);
}
.heatmap-grid-body{
  flex:1;min-width:0;display:flex;flex-direction:column;gap:4px;
}
.heatmap-grid-headers{
  display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;
}
.heatmap-day-head{
  text-align:center;color:rgba(244,247,239,.92);padding-bottom:2px;
}
.heatmap-day-head strong{
  display:block;font-size:11px;font-weight:700;letter-spacing:.03em;
}
.heatmap-day-head span{
  display:block;font-size:9px;opacity:.7;margin-top:2px;
}
.heatmap-day-head .day-total{
  display:block;font-size:9px;opacity:.55;margin-top:1px;
}
.heatmap-day-head.is-today strong{
  color:rgba(255,247,214,1);
}
.heatmap-grid-rows{
  position:relative;
  display:flex;flex-direction:column;gap:0;
  overflow:hidden;border-radius:8px;
  isolation:isolate;
  background-image:
    linear-gradient(to right, rgba(244,247,239,.08) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(244,247,239,.06) 1px, transparent 1px);
  background-size:calc(100%/7) 100%, 100% calc(100%/24);
  background-position:0 0, 0 0;
}
.heatmap-canvas{
  position:absolute;inset:0;
  width:100%;height:100%;
  pointer-events:none;z-index:0;
  display:block;
}
.heatmap-grid-row{
  display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:0;
  position:relative;z-index:1;
}
.heatmap-cell{
  height:13px;background:transparent;
  transition:background-color .1s;
}
.heatmap-cell:hover{background:rgba(255,255,255,.08);cursor:default}
.heatmap-foot{
  font-size:12px;color:var(--ink-4);
}
.goal-shell{
  height:100%;
  display:grid;
  grid-template-columns:minmax(170px,200px) minmax(0,1fr);
  gap:22px;
  align-items:center;
}
.goal-range{
  font-size:12px;
  color:var(--ink-4);
  font-weight:700;
  letter-spacing:.01em;
}
.goal-ring-wrap{
  display:grid;place-items:center;
  align-self:stretch;
  padding:8px 0;
}
.goal-ring{
  width:min(220px,100%);aspect-ratio:1;border-radius:50%;
  --goal-progress:0deg;
  --goal-progress-mid:120deg;
  --goal-progress-hot:240deg;
  --goal-cold:#1677ff;
  --goal-cool:#33c7ff;
  --goal-warm:#ffc247;
  --goal-hot:#ff3b30;
  --goal-track:rgba(120,120,128,.18);
  --goal-dial:conic-gradient(from -90deg, var(--goal-cold) 0deg, var(--goal-cool) var(--goal-progress-mid), var(--goal-warm) var(--goal-progress-hot), var(--goal-hot) var(--goal-progress), var(--goal-track) var(--goal-progress) 360deg);
  padding:9px;position:relative;
  background:var(--goal-dial);
  box-shadow:0 18px 44px rgba(22,119,255,.12), 0 8px 24px rgba(255,59,48,.09);
  isolation:isolate;
}
.goal-ring.is-zero{
  --goal-dial:conic-gradient(from -90deg, var(--goal-track) 0deg 360deg);
}
.goal-ring::before{
  display:none;
}
.goal-ring.has-overflow::before{
  content:"";display:block;position:absolute;inset:0;border-radius:inherit;
  background:conic-gradient(from -90deg, var(--goal-hot) 0deg, var(--goal-hot) var(--goal-overflow), transparent var(--goal-overflow) 360deg);
  opacity:.82;
  pointer-events:none;
  mask-image:radial-gradient(circle, transparent calc(100% - 19px), #000 calc(100% - 18px));
  -webkit-mask-image:radial-gradient(circle, transparent calc(100% - 19px), #000 calc(100% - 18px));
  filter:drop-shadow(0 0 8px color-mix(in srgb, var(--goal-hot) 45%, transparent));
}
.goal-ring::after{
  content:"";position:absolute;inset:8px;border-radius:inherit;
  background:linear-gradient(145deg, rgba(255,255,255,.2), rgba(255,255,255,0) 42%);
  opacity:.4;pointer-events:none;
}
.goal-ring.is-complete{
  --goal-track:rgba(239,58,56,.18);
}
.goal-ring-core{
  width:100%;height:100%;border-radius:50%;
  background:color-mix(in srgb, var(--control-inner) 88%, transparent);
  border:1px solid var(--border);
  backdrop-filter:blur(18px) saturate(150%);
  -webkit-backdrop-filter:blur(18px) saturate(150%);
  display:flex;align-items:center;justify-content:center;
  text-align:center;padding:18px;
}
.goal-kicker{
  display:none;
}
.goal-value{
  font-family:var(--font-display);font-size:42px;font-weight:720;
  line-height:1;color:var(--ink);letter-spacing:-.06em;
  font-variant-numeric:tabular-nums;
}
.goal-value small{
  font-family:var(--font-body);font-size:.5em;font-weight:750;color:var(--ink-3);
  letter-spacing:-.04em;margin-left:1px;
}
.goal-status{
  display:none;
}
.goal-controls{
  display:grid;
  gap:16px;
  align-content:center;
  padding:18px;
  border:1px solid var(--border);
  border-radius:22px;
  background:color-mix(in srgb, var(--control-bg) 72%, transparent);
}
.goal-input-row{
  display:grid;grid-template-columns:1fr auto;gap:14px;align-items:center;
}
.goal-label{
  display:grid;gap:6px;font-size:12px;color:var(--ink-4);
}
.goal-label strong{
  font-size:15px;color:var(--ink-2);font-weight:760;
}
.goal-input-wrap{
  display:inline-flex;align-items:center;gap:8px;
  padding:9px 12px;border-radius:18px;
  background:var(--control-bg);border:1px solid var(--border);
}
.goal-input{
  width:64px;border:none;background:transparent;
  font-family:var(--font-display);font-size:24px;font-weight:680;
  color:var(--ink);letter-spacing:-.04em;font-variant-numeric:tabular-nums;
}
.goal-input:focus{outline:none}
.goal-unit{
  font-size:12px;color:var(--ink-4);font-weight:600;
}
.goal-presets{display:flex;gap:8px;flex-wrap:wrap}
.goal-chip{
  border:none;border-radius:999px;padding:8px 12px;cursor:pointer;
  background:var(--control-bg);border:1px solid var(--border);color:var(--ink-2);
  font-size:12px;font-weight:600;transition:all .18s ease;
}
.goal-chip:hover{background:var(--control-bg-strong);border-color:var(--border-strong);color:var(--ink)}
.target-card .goal-ring{
  width:min(176px,100%);
  padding:10px;
}
.target-card .goal-ring-core{
  padding:16px;
}
.target-card .goal-kicker{
  font-size:10px;
}
.target-card .goal-value{
  font-size:34px;
}
.target-card .goal-value small{
  display:block;
  margin-top:4px;
}
.target-card .goal-status{
  font-size:11px;padding:6px 9px;
}
.target-card .goal-controls{
  gap:16px;
}
.target-card .goal-input-wrap{
  padding:9px 11px;
}
.target-card .goal-input{
  width:48px;
  font-size:18px;
}
.target-card .goal-unit{
  font-size:11px;
}
.target-card .goal-presets{
  gap:6px;
}
.target-card .goal-chip{
  padding:7px 11px;
}
.target-card .section-head{
  align-items:flex-start;
  margin-bottom:24px;
  gap:20px;
}
.target-card .section-title{
  font-weight:780;
  padding-top:7px;
}
.target-card .section-title em{
  color:var(--ink);
  font-weight:780;
}
.target-card .section-meta{
  display:grid;
  gap:4px;
  justify-items:start;
  text-align:left;
  min-width:190px;
  padding:11px 14px 12px;
  border:1px solid var(--border);
  border-radius:20px;
  background:color-mix(in srgb, var(--control-bg) 76%, transparent);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08);
  font-size:12px;
  font-weight:700;
  color:var(--ink-2);
  letter-spacing:.01em;
  font-variant-numeric:tabular-nums;
}
.target-card .section-meta strong{
  display:block;
  margin-top:1px;
  font-family:var(--font-display);
  font-size:25px;
  line-height:1;
  letter-spacing:-.045em;
  color:var(--ink);
  font-weight:780;
}
.section-meta-note{
  display:block;
  font-family:var(--font-body);
  font-size:12px;
  line-height:1.25;
  letter-spacing:.01em;
  color:var(--ink-4);
  font-weight:700;
  margin-top:2px;
}
.daily-chart{
  height:240px;display:grid;grid-template-rows:1fr auto;gap:12px;
}
.daily-bars{
  height:100%;display:grid;grid-template-columns:repeat(30,minmax(0,1fr));
  align-items:end;gap:6px;padding-right:2px;border-bottom:1px solid var(--border);
}
.daily-col{
  height:100%;display:grid;grid-template-rows:1fr auto;align-items:end;gap:8px;min-width:0;
}
.daily-bar{
  width:100%;min-height:0;border-radius:4px 4px 0 0;
  background:rgba(0,122,255,.18);
  transition:background .18s ease, opacity .18s ease;
}
.daily-col:hover .daily-bar{background:rgba(0,122,255,.3)}
.daily-bar.today{background:var(--accent)}
.daily-label{
  min-height:24px;text-align:center;
  font-size:11px;line-height:1.2;color:var(--ink-4);
}
.donut-layout{
  min-height:240px;display:grid;grid-template-columns:1fr minmax(160px,220px);
  gap:20px;align-items:center;
}
.bar-column{
  display:flex;flex-direction:column;gap:10px;
}
.donut-chart{
  width:100%;height:28px;border-radius:6px;display:flex;overflow:hidden;
  position:relative;box-shadow:var(--logo-shadow);
}
.bar-segment{
  height:100%;transition:opacity .22s ease, filter .22s ease;
  cursor:pointer;
}
.bar-segment.is-muted{opacity:.35}
.donut-total{
  font-family:var(--font-display);font-size:24px;font-weight:680;
  color:var(--ink);letter-spacing:-.03em;
}
.donut-caption{
  font-size:12px;color:var(--ink-4);margin-top:2px;
}
.donut-legend{display:grid;gap:10px;align-content:center}
.legend-item{
  display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;
  font-size:12px;color:var(--ink-2);
  padding:6px 8px;border-radius:10px;position:relative;overflow:hidden;
  transition:background .18s ease, transform .18s ease, opacity .18s ease;
}
.legend-item:hover{background:var(--surface-hover)}
.legend-item.is-active{background:var(--surface-hover);transform:translateY(-1px)}
.legend-item.is-muted{opacity:.35}
.legend-swatch{
  width:10px;height:10px;border-radius:4px;display:inline-block;
}
.legend-name{
  min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.legend-value{
  font-family:var(--font-mono);font-size:11px;color:var(--ink-3);font-variant-numeric:tabular-nums;
}

.section-head{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:20px;gap:16px;
}
.section-title{
  font-family:var(--font-display);font-weight:650;
  font-size:22px;line-height:1.1;letter-spacing:-.025em;color:var(--ink);
}
.section-title em{font-style:normal;color:var(--ink-3);font-weight:500}
.section-meta{
  font-size:12px;color:var(--ink-4);
}

.btn{
  font-family:var(--font-body);font-size:13px;font-weight:600;
  color:var(--ink-2);background:var(--control-bg);
  border:1px solid var(--border);
  padding:9px 14px;border-radius:999px;cursor:pointer;
  display:inline-flex;align-items:center;gap:8px;
  transition:all .18s ease;
}
.btn:hover{color:var(--ink);border-color:var(--border-strong);background:var(--control-bg-strong)}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.danger{color:var(--danger);border-color:rgba(255,59,48,.18)}
.btn.danger:hover{background:var(--danger-soft);border-color:rgba(255,59,48,.28);color:var(--danger)}
.btn .x{font-size:13px;line-height:1;opacity:.8}

.toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);
  background:var(--bg-elev);border:1px solid var(--border-strong);
  color:var(--ink);padding:12px 18px;border-radius:12px;
  font-size:13px;box-shadow:var(--toast-shadow);
  opacity:0;pointer-events:none;z-index:100;
  transition:opacity .2s ease, transform .25s cubic-bezier(.2,.8,.3,1);
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

.modal-backdrop{
  position:fixed;inset:0;background:var(--modal-backdrop);backdrop-filter:blur(10px);
  display:none;align-items:center;justify-content:center;z-index:90;
}
.modal-backdrop.show{display:flex}
.modal{
  background:var(--bg-elev);border:1px solid var(--border-strong);
  border-radius:var(--radius);padding:28px 30px 24px;max-width:440px;width:calc(100% - 40px);
  box-shadow:var(--toast-shadow);
}
.modal h3{
  font-family:var(--font-display);font-size:24px;font-weight:650;
  letter-spacing:-.02em;color:var(--ink);margin-bottom:10px;
}
.modal h3 em{font-style:normal;color:var(--ink-3);font-weight:500}
.modal p{
  font-size:14px;color:var(--ink-2);line-height:1.55;margin-bottom:20px;
}
.modal p code{
  font-family:var(--font-mono);font-size:12.5px;color:var(--ink);
  background:rgba(120,120,128,.12);padding:1px 6px;border-radius:6px;
  border:1px solid var(--border);
}
.modal-actions{display:flex;gap:10px;justify-content:flex-end}
.category-modal{
  max-width:520px;
}
.category-modal-head{
  display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
  margin-bottom:20px;
}
.category-modal-kicker{
  font-size:12px;color:var(--ink-4);margin-top:6px;
}
.category-close{
  width:34px;height:34px;border-radius:999px;border:1px solid var(--border);
  background:var(--control-bg);color:var(--ink-2);display:grid;place-items:center;
  cursor:pointer;font-size:18px;line-height:1;flex-shrink:0;
}
.category-close:hover{background:var(--control-bg-strong);color:var(--ink)}
.category-list{display:grid;gap:10px}
.category-option{
  width:100%;border:1px solid var(--border);background:var(--control-bg);
  border-radius:16px;padding:14px 16px;display:grid;grid-template-columns:auto 1fr auto;
  align-items:center;gap:12px;cursor:pointer;text-align:left;
  transition:border-color .18s ease, background .18s ease, transform .18s ease;
}
.category-option:hover{
  background:var(--control-bg-strong);border-color:var(--border-strong);transform:translateY(-1px);
}
.category-option.is-active{
  border-color:var(--category-color, var(--accent));
  box-shadow:0 0 0 3px color-mix(in srgb, var(--category-color, var(--accent)) 16%, transparent);
}
.category-option-swatch{
  width:14px;height:14px;border-radius:999px;background:var(--category-color, var(--accent));
  box-shadow:0 0 0 4px color-mix(in srgb, var(--category-color, var(--accent)) 18%, transparent);
}
.category-option-label{
  font-size:14px;font-weight:650;color:var(--ink);
}
.category-option-note{
  font-size:12px;color:var(--ink-4);
}
.category-option-check{
  font-size:12px;font-weight:700;color:var(--category-color, var(--accent));
  opacity:0;
}
.category-option.is-active .category-option-check{opacity:1}
.category-modal-foot{
  margin-top:18px;display:flex;justify-content:flex-end;
}

.settings-shell{display:grid;gap:28px}
.settings-grid{
  display:grid;grid-template-columns:minmax(0,1.1fr) minmax(320px,.9fr);gap:18px;
}
.settings-stack{display:grid;gap:18px}
.settings-card{
  border-radius:var(--radius);
  padding:24px 26px;
}
.settings-copy{
  font-size:14px;line-height:1.6;color:var(--ink-3);max-width:62ch;
}
.settings-kpis{
  display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;
}
.settings-kpi{
  padding:16px 18px;border-radius:18px;
  background:var(--control-bg);border:1px solid var(--border);
}
.settings-kpi-label{
  font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-4);font-weight:700;
}
.settings-kpi-value{
  margin-top:8px;font-family:var(--font-display);font-size:26px;font-weight:680;
  color:var(--ink);letter-spacing:-.04em;
}
.category-form{
  display:grid;gap:14px;
}
.field-grid{
  display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;align-items:end;
}
.field{
  display:grid;gap:8px;
}
.field-label{
  font-size:12px;font-weight:650;color:var(--ink-3);
}
.text-input{
  width:100%;border:1px solid var(--border);background:var(--control-bg);
  border-radius:16px;padding:14px 16px;font-size:14px;color:var(--ink);
  transition:border-color .18s ease, background .18s ease, box-shadow .18s ease;
}
.text-input:focus,
.color-picker-button:focus-within{
  outline:none;border-color:var(--accent);
  box-shadow:0 0 0 4px color-mix(in srgb, var(--accent) 16%, transparent);
  background:var(--control-bg-strong);
}
.color-field{
  display:grid;gap:10px;
}
.color-control{
  display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:center;
  padding:10px 12px;border-radius:18px;border:1px solid var(--border);background:var(--control-bg);
}
.color-preview{
  width:40px;height:40px;border-radius:14px;background:var(--color-value, var(--accent));
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.55), 0 10px 24px color-mix(in srgb, var(--color-value, var(--accent)) 22%, transparent);
}
.color-summary{
  display:grid;gap:3px;min-width:0;
}
.color-summary-label{
  font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-4);font-weight:700;
}
.color-summary-value{
  font-family:var(--font-mono);font-size:13px;color:var(--ink);letter-spacing:.02em;
}
.color-picker-button{
  position:relative;display:inline-flex;align-items:center;justify-content:center;
  min-width:88px;min-height:40px;padding:0 14px;border-radius:999px;
  border:1px solid var(--border);background:var(--control-bg-strong);cursor:pointer;
  font-size:12px;font-weight:700;color:var(--ink-2);
  transition:border-color .18s ease, background .18s ease, color .18s ease, box-shadow .18s ease;
}
.color-picker-button:hover{
  color:var(--ink);border-color:var(--border-strong);
}
.color-input{
  position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;
}
.color-presets{
  display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;
}
.color-preset{
  height:30px;border-radius:10px;border:1px solid transparent;background:var(--color-value);
  cursor:pointer;box-shadow:inset 0 0 0 1px rgba(255,255,255,.36);
  transition:transform .16s ease, border-color .16s ease, box-shadow .16s ease;
}
.color-preset:hover{
  transform:translateY(-1px);
}
.color-preset.is-active{
  border-color:color-mix(in srgb, var(--color-value) 72%, black 8%);
  box-shadow:0 0 0 3px color-mix(in srgb, var(--color-value) 24%, transparent), inset 0 0 0 1px rgba(255,255,255,.65);
}
.color-preset:disabled{
  cursor:not-allowed;opacity:.45;transform:none;box-shadow:none;
}
.field-note{
  font-size:12px;color:var(--ink-4);
}
.settings-actions{display:flex;justify-content:flex-start}
.category-library{
  display:grid;gap:12px;
}
.category-library-grid{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;
}
.category-library-card{
  display:grid;gap:10px;padding:16px;border-radius:18px;
  background:var(--control-bg);border:1px solid var(--border);
}
.category-library-card.is-editing{
  background:var(--control-bg-strong);border-color:var(--border-strong);
}
.category-library-head{
  display:flex;align-items:center;justify-content:space-between;gap:12px;
}
.category-library-title{
  display:flex;align-items:center;gap:10px;min-width:0;
}
.category-library-name{
  font-size:14px;font-weight:650;color:var(--ink);
}
.category-library-type{
  font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-4);
}
.category-library-swatch{
  width:14px;height:14px;border-radius:999px;background:var(--category-color, var(--accent));
  box-shadow:0 0 0 5px color-mix(in srgb, var(--category-color, var(--accent)) 16%, transparent);
}
.category-library-code{
  font-family:var(--font-mono);font-size:12px;color:var(--ink-4);
}
.category-library-meta{
  display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
  font-size:12px;color:var(--ink-4);
}
.category-library-edit-toggle[aria-expanded="true"]{
  color:var(--ink);background:var(--control-bg-strong);border-color:var(--border-strong);
}
.category-library-actions{
  display:flex;gap:8px;flex-wrap:wrap;
}
.btn.small{
  padding:7px 11px;font-size:12px;
}
.btn.subtle{
  color:var(--ink-3);
}
.btn.subtle:hover{
  color:var(--ink);background:var(--control-bg-strong);
}
.category-editor{
  display:grid;gap:12px;padding:14px;border-radius:16px;
  background:var(--surface-hover);border:1px solid var(--border);
}
.category-editor[hidden]{
  display:none;
}
.category-editor-head{
  display:flex;align-items:center;justify-content:space-between;gap:12px;
}
.category-editor-title{
  font-size:13px;font-weight:700;color:var(--ink);
}
.category-editor-actions{
  display:flex;gap:8px;flex-wrap:wrap;
}

.table-card{
  border-radius:var(--radius);
  padding:24px 26px;margin-bottom:24px;
}
.recent-stats{
  display:grid;
  grid-template-columns:repeat(3,minmax(0,1fr));
  gap:1px;
  overflow:hidden;
  margin:-4px 0 20px;
  border:1px solid var(--border);
  border-radius:18px;
  background:var(--border);
}
.recent-stat{
  min-width:0;
  padding:15px 17px 16px;
  background:color-mix(in srgb, var(--control-bg) 70%, transparent);
}
.recent-stat-label{
  display:block;
  font-size:11px;
  font-weight:750;
  color:var(--ink-4);
  letter-spacing:.055em;
  text-transform:uppercase;
}
.recent-stat-value{
  display:block;
  margin-top:7px;
  font-family:var(--font-display);
  font-size:25px;
  line-height:1.05;
  font-weight:720;
  color:var(--ink);
  font-variant-numeric:tabular-nums;
  letter-spacing:-.035em;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
}
.recent-stat-note{
  display:block;
  margin-top:6px;
  font-size:12px;
  line-height:1.35;
  color:var(--ink-4);
}
.table-scroll{
  max-height:min(52vh, 540px);
  overflow:auto;
  margin:0 -6px -6px;
  padding:0 6px 6px;
  scrollbar-gutter:stable;
}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{
  text-align:left;
  font-size:12px;font-weight:600;color:var(--ink-3);
  padding:0 14px 14px;border-bottom:1px solid var(--border);
}
th:last-child{text-align:right}
.table-scroll thead th{
  position:sticky;
  top:0;
  z-index:1;
  background:var(--surface);
}
td{
  padding:14px;border-bottom:1px solid var(--row-border);
  vertical-align:middle;color:var(--ink-2);
}
tbody tr:last-child td{border-bottom:none}
tbody tr{transition:background .15s ease}
tbody tr:hover td{background:var(--surface-hover)}
td.num{text-align:right;font-family:var(--font-mono);font-variant-numeric:tabular-nums;color:var(--ink-2);font-size:12.5px}
td.dur{text-align:right;font-family:var(--font-mono);font-weight:600;color:var(--ink);font-size:13px;font-variant-numeric:tabular-nums}
td.row-action{width:36px;padding:8px 10px 8px 4px;text-align:right}
.row-del{
  display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;border-radius:6px;
  border:1px solid transparent;background:transparent;
  color:var(--danger);font-size:15px;line-height:1;cursor:pointer;
  opacity:.55;transition:opacity .15s ease, background .15s ease, border-color .15s ease;
}
tbody tr:hover .row-del{opacity:1}
.row-del:hover{background:var(--danger-soft);border-color:rgba(255,59,48,.28)}
.row-del:disabled{opacity:.2;cursor:not-allowed}

.proj-badge{
  display:inline-flex;align-items:center;gap:8px;
  color:var(--ink);font-weight:600;letter-spacing:-.01em;font-size:13.5px;
  max-width:280px;
}
.proj-badge .tick{
  width:7px;height:7px;border-radius:50%;
  background:var(--proj-color, rgba(0,122,255,.55));flex-shrink:0;
}
.proj-badge.untitled .tick{background:var(--danger);opacity:.7}
.proj-badge.untitled .name{color:var(--ink-3);font-style:italic}
.proj-badge-wrap{
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
}
.category-pill{
  border:1px solid color-mix(in srgb, var(--category-color, var(--border-strong)) 28%, transparent);
  background:color-mix(in srgb, var(--category-color, var(--control-bg)) 14%, var(--control-bg));
  color:var(--ink);border-radius:14px;padding:7px 12px;cursor:pointer;
  display:inline-flex;align-items:center;gap:8px;font-size:12px;font-weight:650;
  white-space:nowrap;transition:transform .16s ease, border-color .16s ease, background .16s ease;
  min-width:120px;min-height:36px;
}
.category-pill:hover{
  transform:translateY(-1px);
  border-color:color-mix(in srgb, var(--category-color, var(--border-strong)) 44%, transparent);
}
.category-pill-swatch{
  width:9px;height:9px;border-radius:999px;background:var(--category-color, var(--ink-4));flex-shrink:0;
}
.category-pill.empty{
  --category-color:#8E8E93;
  color:var(--ink-3);
  background:color-mix(in srgb, var(--category-color) 10%, var(--control-bg));
  border-color:color-mix(in srgb, var(--category-color) 28%, transparent);
  min-width:0;
}
.category-pill.empty .category-pill-swatch{background:var(--category-color)}

.rank{
  font-family:var(--font-mono);color:var(--ink-4);font-size:11px;
  font-weight:500;width:22px;text-align:right;padding-right:6px;
}

.bar-bg{
  background:var(--bar-bg);border-radius:999px;height:5px;width:140px;
  overflow:hidden;display:inline-block;vertical-align:middle;
}
.bar-fill{
  height:100%;background:linear-gradient(90deg, rgba(0,122,255,.45), rgba(0,122,255,.95));
  border-radius:999px;transition:width .6s cubic-bezier(.2,.8,.3,1);
}

.active-tag{
  display:inline-flex;align-items:center;gap:6px;
  font-size:12px;color:var(--live);font-weight:600;
}
.active-tag .dot{width:5px;height:5px;border-radius:50%;background:var(--live);box-shadow:0 0 6px rgba(52,199,89,.45)}

.empty{text-align:center;padding:56px 20px}
.empty p{
  font-family:var(--font-display);font-size:20px;font-weight:650;
  color:var(--ink-2);margin-bottom:6px;
}
.empty small{font-size:13px;color:var(--ink-4)}

.mark{
  margin-top:40px;padding-top:24px;border-top:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;
  font-size:12px;color:var(--ink-4);
}
.mark em{font-family:var(--font-display);font-style:normal;color:var(--ink-3)}

@media(max-width:960px){
  .cards{grid-template-columns:repeat(2,1fr)}
  .chart-row{grid-template-columns:1fr}
  .rings-row{grid-template-columns:1fr}
  .settings-grid{grid-template-columns:1fr}
  .settings-kpis{grid-template-columns:1fr}
  .bar-bg{display:none}
  .donut-layout{grid-template-columns:1fr}
  .goal-input-row{grid-template-columns:1fr}
  .target-card .goal-shell{grid-template-columns:minmax(170px,220px) minmax(0,1fr)}
  .target-card .goal-ring{width:min(188px,100%)}
}
@media(max-width:640px){
  header{padding:0 20px;height:62px}
  .header-right{gap:10px;flex-wrap:wrap;justify-content:flex-end}
  .header-nav{order:2;width:100%;justify-content:center}
  .page{padding:28px 20px 60px}
  .section-head{flex-wrap:wrap}
  .target-card{min-height:auto;padding:24px 20px 22px}
  .target-card .section-head{display:grid;grid-template-columns:1fr;margin-bottom:22px}
  .target-card .section-title{padding-top:0}
  .target-card .section-meta{width:100%;min-width:0}
  .target-card .goal-shell{grid-template-columns:1fr;gap:18px}
  .target-card .goal-ring{width:min(210px,100%)}
  .target-card .goal-controls{padding:16px}
  .card{min-height:190px;padding:24px 18px 20px}
  .card-value{font-size:32px}
  .card-value--text{font-size:21px}
  .daily-bars{gap:4px}
  .mark{gap:8px;flex-direction:column;align-items:flex-start}
  .heatmap-meta{grid-template-columns:1fr}
  .heatmap-controls{width:100%;justify-content:space-between;justify-self:stretch}
  .heatmap-stage{min-height:316px;padding:16px 12px 16px}
  .heatmap-stage-head,
  .heatmap-stage-values{gap:6px}
  .heatmap-day-head strong,
  .heatmap-day-chip strong{font-size:11px}
  .recent-stats{grid-template-columns:1fr}
  .recent-stat-value{font-size:23px}
  .field-grid{grid-template-columns:1fr}
  .color-control{grid-template-columns:auto 1fr}
  .color-picker-button{grid-column:1 / -1}
  .color-presets{grid-template-columns:repeat(4,minmax(0,1fr))}
}
@media (prefers-reduced-motion: reduce){
  .card,
  .target-card{transition:border-color .2s ease, background .2s ease, box-shadow .2s ease}
  .card:hover,
  .target-card:hover{transform:none}
}
</style>
</head>
<body>
<header>
  <div class="logo">
    <div class="logo-icon">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <rect x="1"  y="8" width="2" height="4"  rx="1"/>
        <rect x="5"  y="5" width="2" height="10" rx="1"/>
        <rect x="9"  y="2" width="2" height="16" rx="1"/>
        <rect x="13" y="5" width="2" height="10" rx="1"/>
        <rect x="17" y="8" width="2" height="4"  rx="1"/>
      </svg>
    </div>
    <div class="logo-text">
      <h1>Ableton <em>Tracker</em></h1>
    </div>
  </div>
  <div class="header-right">
    <div class="header-nav" role="tablist" aria-label="Pages">
      <button class="nav-tab is-active" id="navDashboard" type="button" data-view-tab="dashboard">Dashboard</button>
      <button class="nav-tab" id="navSettings" type="button" data-view-tab="settings">Settings</button>
    </div>
    <button class="theme-toggle" id="themeToggle" type="button" aria-label="Switch theme" title="Switch theme">
      <svg class="moon" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M21 14.7A8 8 0 0 1 9.3 3 7 7 0 1 0 21 14.7Z"/>
      </svg>
      <svg class="sun" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="4"/>
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>
      </svg>
    </button>
    <div id="liveBadge" style="display:none" class="live-badge">
      <span class="dot"></span>
      <span id="liveProject">Recording</span>
    </div>
    <span class="updated" id="updatedAt">Loading…</span>
  </div>
</header>

<div class="page">
  <div class="intro">
    <div>
      <h2 id="pageTitle">Dashboard </h2>
      <div class="sub" id="pageSubtitle">
        <span class="session-presence">
          <span class="session-status" id="sessionStatus" data-state="live">
            <span class="session-status__text" id="sessionStatusText">Live</span>
            <span class="session-status__indicator session-status__indicator--live" id="sessionStatusIndicator" aria-hidden="true">
              <span class="session-status__dot"></span>
            </span>
          </span>
          <span class="session-status__timer" id="sessionTimer" data-state="live" aria-live="polite">
            <span class="session-status__timer-label" id="sessionTimerLabel">Session</span>
            <span class="session-status__timer-value" id="sessionTimerValue">00:00:00</span>
          </span>
        </span>
        · <span id="introDate"></span> · <span id="introTime"></span>
      </div>
    </div>
  </div>
  <div id="app"><div class="empty"><p>Loading data…</p><small>one moment</small></div></div>
  <div class="mark">
    <span>Ableton Tracker · <em>v1</em></span>
    <span id="markDb">local</span>
  </div>
</div>

<!-- Confirm modal -->
<div class="modal-backdrop" id="modal">
  <div class="modal" role="dialog" aria-modal="true">
    <h3 id="modalTitle">Confirm</h3>
    <p id="modalBody"></p>
    <div class="modal-actions">
      <button class="btn" id="modalCancel">Cancel</button>
      <button class="btn danger" id="modalConfirm"><span class="x">×</span> Confirm</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="categoryModal">
  <div class="modal category-modal" role="dialog" aria-modal="true" aria-labelledby="categoryModalTitle">
    <div class="category-modal-head">
      <div>
        <h3 id="categoryModalTitle">Set category</h3>
        <div class="category-modal-kicker" id="categoryModalProject"></div>
      </div>
      <button class="category-close" id="categoryModalClose" type="button" aria-label="Close category picker">×</button>
    </div>
    <div class="category-list" id="categoryModalList"></div>
    <div class="category-modal-foot">
      <button class="btn" id="categoryModalClear" type="button">Clear category</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const PROJECT_COLORS = [
  '#007aff','#5ac8fa','#64d2ff','#5e5ce6','#34c759',
  '#ff9f0a','#ff375f','#8e8e93','#aeaeb2','#c7c7cc'
];
const UNTITLED = new Set(['untitled','untitled project','']);
const isUntitled = n => UNTITLED.has((n || '').trim().toLowerCase());
const WEEKLY_GOAL_STORAGE_KEY = 'ableton_tracker_weekly_goal_hours';
const WEEK_START_DAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const DEFAULT_WEEK_START_BACKEND = 4;
let WEEK_START_BACKEND_DAY = DEFAULT_WEEK_START_BACKEND;
const DAILY_GOAL_STORAGE_KEY = 'ableton_tracker_daily_goal_hours';
const THEME_STORAGE_KEY = 'ableton_tracker_theme';
const CUSTOM_CATEGORY_LIMIT_FALLBACK = 12;
const CATEGORY_COLOR_PRESETS = [
  '#FF6B6B', '#FF9F43', '#FFD166', '#7BD389',
  '#3EC1D3', '#4D96FF', '#7C5CFF', '#C77DFF',
  '#F15BB5', '#6D6875', '#A3A380', '#F28482',
];
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
let latestDashboardData = null;
let activeView = window.location.hash === '#settings' ? 'settings' : 'dashboard';
let CATEGORY_OPTIONS = [];
let CATEGORY_BY_KEY = {};
let sessionTimerSnapshot = { state: 'off', baseSeconds: 0, syncedAtMs: 0 };

function sessionSubtitleMarkup() {
  return `
    <span class="session-presence">
      <span class="session-status" id="sessionStatus" data-state="live">
        <span class="session-status__text" id="sessionStatusText">Live</span>
        <span class="session-status__indicator session-status__indicator--live" id="sessionStatusIndicator" aria-hidden="true">
          <span class="session-status__dot"></span>
        </span>
      </span>
      <span class="session-status__timer" id="sessionTimer" data-state="live" aria-live="polite">
        <span class="session-status__timer-label" id="sessionTimerLabel">Session</span>
        <span class="session-status__timer-value" id="sessionTimerValue">00:00:00</span>
      </span>
    </span>
    · <span id="introDate"></span> · <span id="introTime"></span>
  `;
}

function activeTheme() {
  const stored = localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === 'light' || stored === 'dark') return stored;
  return systemTheme.matches ? 'dark' : 'light';
}

function updateThemeToggle() {
  const theme = activeTheme();
  const next = theme === 'dark' ? 'light' : 'dark';
  const btn = document.getElementById('themeToggle');
  btn.setAttribute('aria-label', `Switch to ${next} mode`);
  btn.title = `Switch to ${next} mode`;
}

function applyStoredTheme() {
  const stored = localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === 'light' || stored === 'dark') {
    document.documentElement.dataset.theme = stored;
  } else {
    delete document.documentElement.dataset.theme;
  }
  updateThemeToggle();
}

function toggleTheme() {
  localStorage.setItem(THEME_STORAGE_KEY, activeTheme() === 'dark' ? 'light' : 'dark');
  applyStoredTheme();
}

function syncCategoryOptions(options) {
  CATEGORY_OPTIONS = Array.isArray(options) ? options : [];
  CATEGORY_BY_KEY = Object.fromEntries(CATEGORY_OPTIONS.map(option => [option.key, option]));
}

function normalizeHexColor(value) {
  const text = String(value || '').trim().toUpperCase();
  return /^#[0-9A-F]{6}$/.test(text) ? text : null;
}

function renderColorField({ inputId = '', value = '#7C5CFF', disabled = false, showPresets = true } = {}) {
  const safeValue = normalizeHexColor(value) || '#7C5CFF';
  return `
    <div class="color-field" data-color-field>
      <div class="color-control">
        <span class="color-preview" data-color-preview style="--color-value:${safeValue}"></span>
        <div class="color-summary">
          <span class="color-summary-label">Current color</span>
          <strong class="color-summary-value" data-color-value>${safeValue}</strong>
        </div>
        <label class="color-picker-button">
          <input
            class="color-input"
            ${inputId ? `id="${inputId}"` : ''}
            name="color"
            type="color"
            value="${safeValue}"
            ${disabled ? 'disabled' : ''}
          >
          <span>Browse</span>
        </label>
      </div>
      ${showPresets ? `<div class="color-presets" role="list" aria-label="Suggested colors">
        ${CATEGORY_COLOR_PRESETS.map(color => `
          <button
            class="color-preset ${color === safeValue ? 'is-active' : ''}"
            type="button"
            data-color-preset="${color}"
            aria-label="Use ${color}"
            style="--color-value:${color}"
            ${disabled ? 'disabled' : ''}
          ></button>
        `).join('')}
      </div>` : ''}
    </div>
  `;
}

function bindColorField(root) {
  const field = root?.matches?.('[data-color-field]') ? root : root?.querySelector?.('[data-color-field]');
  if (!field) return;
  const input = field.querySelector('input[name="color"]');
  const preview = field.querySelector('[data-color-preview]');
  const valueLabel = field.querySelector('[data-color-value]');
  const presets = Array.from(field.querySelectorAll('[data-color-preset]'));
  if (!input || !preview || !valueLabel) return;

  const sync = nextValue => {
    const color = normalizeHexColor(nextValue) || '#7C5CFF';
    input.value = color;
    preview.style.setProperty('--color-value', color);
    valueLabel.textContent = color;
    presets.forEach(button => {
      button.classList.toggle('is-active', button.dataset.colorPreset === color);
    });
  };

  input.addEventListener('input', event => sync(event.target.value));
  presets.forEach(button => {
    button.addEventListener('click', () => sync(button.dataset.colorPreset));
  });
  sync(input.value);
}

function setIntroDates() {
  const now = new Date();
  const introDate = document.getElementById('introDate');
  const introTime = document.getElementById('introTime');
  if (introDate) {
    introDate.textContent = now.toLocaleDateString('en-US', {
      weekday: 'long',
      month: 'long',
      day: 'numeric',
    });
  }
  if (introTime) {
    introTime.textContent = now.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
    });
  }
}

function tickSessionTimer() {
  const timer = document.getElementById('sessionTimer');
  const value = document.getElementById('sessionTimerValue');
  if (!timer || !value) return;

  const state = sessionTimerSnapshot.state || 'off';
  timer.dataset.state = state;

  if (state === 'live') {
    const elapsed = Math.max(0, (Date.now() - sessionTimerSnapshot.syncedAtMs) / 1000);
    value.textContent = formatSessionClock((sessionTimerSnapshot.baseSeconds || 0) + elapsed);
    return;
  }

  value.textContent = state === 'booting' ? 'Starting…' : '--:--:--';
}

function updateHeaderForView() {
  const isSettings = activeView === 'settings';
  document.getElementById('navDashboard').classList.toggle('is-active', !isSettings);
  document.getElementById('navSettings').classList.toggle('is-active', isSettings);
  document.getElementById('pageTitle').innerHTML = isSettings
    ? 'Workspace <em>settings</em>'
    : '<strong class="page-title-dashboard">Dashboard</strong>';
  document.getElementById('pageSubtitle').innerHTML = isSettings
    ? 'Manage categories, colors, and personal organization'
    : sessionSubtitleMarkup();
  if (!isSettings) {
    setIntroDates();
    updateSessionStatus(latestDashboardData?.summary);
  }
}

function setActiveView(nextView, { pushHash = true } = {}) {
  activeView = nextView === 'settings' ? 'settings' : 'dashboard';
  if (pushHash) {
    const nextHash = activeView === 'settings' ? '#settings' : '#dashboard';
    if (window.location.hash !== nextHash) window.location.hash = nextHash;
  }
  updateHeaderForView();
  if (latestDashboardData) render(latestDashboardData);
}

document.getElementById('themeToggle').addEventListener('click', toggleTheme);
systemTheme.addEventListener('change', () => {
  if (!localStorage.getItem(THEME_STORAGE_KEY)) updateThemeToggle();
});
applyStoredTheme();
document.getElementById('navDashboard').addEventListener('click', () => setActiveView('dashboard'));
document.getElementById('navSettings').addEventListener('click', () => setActiveView('settings'));
window.addEventListener('hashchange', () => {
  const nextView = window.location.hash === '#settings' ? 'settings' : 'dashboard';
  if (nextView !== activeView) setActiveView(nextView, { pushHash: false });
});
updateHeaderForView();

const fmt = {
  dur(s) {
    if (!s || s < 60) return s > 0 ? '<1m' : '0m';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  },
  date(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleDateString('en-US', { month:'short', day:'numeric' });
  },
  datetime(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString('en-US', {
      month:'short', day:'numeric', hour:'numeric', minute:'2-digit'
    });
  },
  hrs(s) { return Math.round((s || 0) / 360) / 10; }
};

function formatSessionClock(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

function escapeHtml(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function getCategoryMeta(categoryKey) {
  return categoryKey ? CATEGORY_BY_KEY[categoryKey] || null : null;
}

function projectColor(project, index = 0) {
  return project?.category_color || getCategoryMeta(project?.category_key)?.color || PROJECT_COLORS[index % PROJECT_COLORS.length];
}

function projectChartColor(index, total) {
  const safeTotal = Math.max(total, 1);
  const t = safeTotal === 1 ? 0 : index / (safeTotal - 1);
  return `hsl(210 ${Math.round(78 - (t * 22))}% ${Math.round(28 + (t * 42))}%)`;
}

function projectBadge(project, index = 0) {
  const untitled = isUntitled(project.project_name);
  return `
    <span class="proj-badge ${untitled ? 'untitled' : ''}" style="--proj-color:${projectColor(project, index)}">
      <span class="tick"></span>
      <span class="name">${escapeHtml(project.project_name)}</span>
    </span>
  `;
}

function categoryPill(project) {
  const category = getCategoryMeta(project.category_key);
  const encodedProject = encodeURIComponent(project.project_name);
  if (!category) {
    return `
      <button
        class="category-pill empty"
        type="button"
        data-category-trigger="true"
        data-project-name="${encodedProject}"
        data-category-key=""
      >
        <span class="category-pill-swatch"></span>
        <span>Set category</span>
      </button>
    `;
  }
  return `
    <button
      class="category-pill"
      type="button"
      style="--category-color:${category.color}"
      data-category-trigger="true"
      data-project-name="${encodedProject}"
      data-category-key="${category.key}"
    >
      <span class="category-pill-swatch"></span>
      <span>${escapeHtml(category.label)}</span>
    </button>
  `;
}

function localDateKey(d) {
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function dateFromKey(key) {
  const [year, month, day] = String(key).split('-').map(Number);
  return new Date(year, month - 1, day, 12);
}

function addDays(base, amount) {
  const d = new Date(base);
  d.setDate(d.getDate() + amount);
  return d;
}

function startOfWeek(base) {
  const jsDay = (WEEK_START_BACKEND_DAY + 1) % 7;
  return addDays(base, -((base.getDay() - jsDay + 7) % 7));
}

function weekStartDayName() {
  return WEEK_START_DAY_NAMES[WEEK_START_BACKEND_DAY] || 'Friday';
}

function weekEndDayName() {
  return WEEK_START_DAY_NAMES[(WEEK_START_BACKEND_DAY + 6) % 7] || 'Thursday';
}

function shortDate(key) {
  return dateFromKey(key).toLocaleDateString('en-US', { month:'short', day:'numeric' });
}

function shortRange(startKey, endKey) {
  return `${shortDate(startKey)} - ${shortDate(endKey)}`;
}

function formatHoursNumber(hours) {
  const rounded = Math.round(hours * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function mixColor(a, b, t) {
  return a.map((value, index) => Math.round(value + ((b[index] - value) * t)));
}

function colorStops(stops, t) {
  if (t <= 0) return stops[0];
  if (t >= 1) return stops[stops.length - 1];
  const scaled = t * (stops.length - 1);
  const index = Math.floor(scaled);
  const localT = scaled - index;
  return mixColor(stops[index], stops[index + 1], localT);
}

function rgbString(parts) {
  return `rgb(${parts[0]}, ${parts[1]}, ${parts[2]})`;
}

function rgbaString(parts, alpha) {
  return `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, ${alpha})`;
}

function heatmapColorParts(value, maxValue) {
  const t = Math.pow(value / Math.max(maxValue, 1), 1.5);
  return colorStops([
    [245, 228, 82],
    [255, 211, 74],
    [255, 177, 56],
    [255, 136, 56],
    [255, 92, 74],
    [239, 58, 56],
  ], t);
}

function heatmapColor(value, maxValue) {
  if (value <= 0) return 'rgba(27, 48, 26, 0.18)';
  return rgbString(heatmapColorParts(value, maxValue));
}

function paintHeatmapCanvas(canvas, container, days, activityByDayHour) {
  if (!canvas || !container) return;
  const rect = container.getBoundingClientRect();
  const W = rect.width;
  const H = rect.height;
  if (W <= 0 || H <= 0) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width  = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  canvas.style.width  = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const cw = W / 7;
  const ch = H / 24;
  // Tighter radius + faster falloff = sharper, less hazy cells.
  const radius = Math.max(cw, ch) * 1.05;

  for (let col = 0; col < days.length; col++) {
    const day = days[col];
    if (!day.inRange) continue;
    for (let h = 0; h < 24; h++) {
      const secs = Math.min(activityByDayHour[`${day.key}_${h}`] || 0, 3600);
      if (secs <= 0) continue;
      const row = 23 - h;
      const cx  = col * cw + cw / 2;
      const cy  = row * ch + ch / 2;
      const intensity = Math.pow(secs / 3600, 0.4);
      const [r, g, b] = heatmapColorParts(secs, 3600);
      const g2 = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
      g2.addColorStop(0,    `rgba(${r},${g},${b},${Math.min(1, 1.1 * intensity)})`);
      g2.addColorStop(0.45, `rgba(${r},${g},${b},${0.55 * intensity})`);
      g2.addColorStop(0.8,  `rgba(${r},${g},${b},${0.1 * intensity})`);
      g2.addColorStop(1,    `rgba(${r},${g},${b},0)`);
      ctx.fillStyle = g2;
      ctx.fillRect(cx - radius, cy - radius, radius * 2, radius * 2);
    }
  }
}

function heatmapGlowColor(value, maxValue) {
  if (value <= 0) return 'transparent';
  return rgbaString(colorStops([
    [255, 229, 109],
    [255, 206, 88],
    [255, 163, 69],
    [255, 114, 66],
    [245, 72, 62],
  ], Math.pow(value / Math.max(maxValue, 1), 1.5)), 0.52);
}

function getStoredGoalHours(storageKey, fallbackHours) {
  const raw = Number(localStorage.getItem(storageKey));
  return Number.isFinite(raw) && raw > 0 ? raw : fallbackHours;
}

function setStoredGoalHours(storageKey, hours) {
  localStorage.setItem(storageKey, String(hours));
}

// ── Toast ──
let toastTimer = null;
function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2400);
}

// ── Confirm ──
function confirmDialog({ title, body, confirmLabel = 'Confirm' }) {
  return new Promise(resolve => {
    const modal = document.getElementById('modal');
    document.getElementById('modalTitle').innerHTML = title;
    document.getElementById('modalBody').innerHTML  = body;
    const confirmBtn = document.getElementById('modalConfirm');
    const cancelBtn  = document.getElementById('modalCancel');
    confirmBtn.innerHTML = `<span class="x">×</span> ${confirmLabel}`;
    modal.classList.add('show');
    const close = (v) => {
      modal.classList.remove('show');
      confirmBtn.removeEventListener('click', onYes);
      cancelBtn.removeEventListener('click', onNo);
      modal.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      resolve(v);
    };
    const onYes = () => close(true);
    const onNo  = () => close(false);
    const onBackdrop = e => { if (e.target === modal) close(false); };
    const onKey = e => { if (e.key === 'Escape') close(false); if (e.key === 'Enter') close(true); };
    confirmBtn.addEventListener('click', onYes);
    cancelBtn.addEventListener('click', onNo);
    modal.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
    confirmBtn.focus();
  });
}

async function postAction(path){
  const res = await fetch(path, { method:'POST' });
  if (!res.ok) throw new Error('request failed');
  return res.json();
}

async function postJson(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || 'request failed');
  return data;
}

function bindCategoryTriggers() {
  document.querySelectorAll('[data-category-trigger]').forEach(button => {
    button.addEventListener('click', () => {
      openCategoryPicker({
        projectName: decodeURIComponent(button.dataset.projectName || ''),
        categoryKey: button.dataset.categoryKey || null,
      });
    });
  });
}

function openCategoryPicker({ projectName, categoryKey }) {
  const modal = document.getElementById('categoryModal');
  const title = document.getElementById('categoryModalProject');
  const list = document.getElementById('categoryModalList');
  const closeBtn = document.getElementById('categoryModalClose');
  const clearBtn = document.getElementById('categoryModalClear');

  title.textContent = projectName;
  list.innerHTML = CATEGORY_OPTIONS.map(option => `
    <button
      class="category-option ${option.key === categoryKey ? 'is-active' : ''}"
      type="button"
      style="--category-color:${option.color}"
      data-category-value="${option.key}"
    >
      <span class="category-option-swatch"></span>
      <span>
        <span class="category-option-label">${escapeHtml(option.label)}</span>
        <span class="category-option-note">${escapeHtml(option.color)}</span>
      </span>
      <span class="category-option-check">Selected</span>
    </button>
  `).join('');

  const close = () => {
    modal.classList.remove('show');
    closeBtn.removeEventListener('click', onClose);
    clearBtn.removeEventListener('click', onClear);
    modal.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onKey);
    list.querySelectorAll('[data-category-value]').forEach(button => {
      button.removeEventListener('click', onSelect);
    });
  };

  const save = async (nextKey) => {
    try {
      const result = await postJson('/api/project-category', {
        project_name: projectName,
        category_key: nextKey,
      });
      close();
      const message = result.category
        ? `${projectName} → ${result.category.label}`
        : `Cleared category for ${projectName}`;
      toast(message);
      load();
    } catch (error) {
      toast(error.message || 'Failed to save category');
    }
  };

  const onClose = () => close();
  const onClear = () => save(null);
  const onBackdrop = event => { if (event.target === modal) close(); };
  const onKey = event => { if (event.key === 'Escape') close(); };
  const onSelect = event => save(event.currentTarget.dataset.categoryValue || null);

  modal.classList.add('show');
  closeBtn.addEventListener('click', onClose);
  clearBtn.addEventListener('click', onClear);
  modal.addEventListener('click', onBackdrop);
  document.addEventListener('keydown', onKey);
  list.querySelectorAll('[data-category-value]').forEach(button => {
    button.addEventListener('click', onSelect);
  });
  list.querySelector('[data-category-value]')?.focus();
}

function renderSettings(data) {
  const categoryOptions = data.category_options || [];
  const customLimit = data.custom_category_limit || CUSTOM_CATEGORY_LIMIT_FALLBACK;
  const customCount = data.custom_category_count || 0;
  const remaining = Math.max(customLimit - customCount, 0);

  document.getElementById('app').innerHTML = `
    <div class="settings-shell">
      <div class="chart-card settings-card">
        <div class="section-head">
          <h3 class="section-title">Custom <em>categories</em></h3>
          <span class="section-meta">${customCount}/${customLimit} used</span>
        </div>
        <p class="settings-copy">
          Create up to ${customLimit} personal categories with your own color palette.
          Anything you add here becomes available in the project category picker right away.
        </p>
      </div>

      <div class="settings-grid">
        <div class="chart-card settings-card">
          <div class="section-head">
            <h3 class="section-title">Create a <em>category</em></h3>
            <span class="section-meta">${remaining} slot${remaining === 1 ? '' : 's'} left</span>
          </div>
          <form class="category-form" id="categoryCreateForm">
            <div class="field-grid">
              <label class="field">
                <span class="field-label">Category name</span>
                <input
                  class="text-input"
                  id="categoryNameInput"
                  name="label"
                  type="text"
                  maxlength="32"
                  placeholder="Ex. Sound Design"
                  ${remaining === 0 ? 'disabled' : ''}
                  required
                >
              </label>
              <label class="field">
                <span class="field-label">Color</span>
                ${renderColorField({ inputId: 'categoryColorInput', value: '#7C5CFF', disabled: remaining === 0 })}
              </label>
            </div>
            <div class="field-note">Pick a color from the palette or open the browser picker for something custom.</div>
            <div class="settings-actions">
              <button class="btn" type="submit" ${remaining === 0 ? 'disabled' : ''}>Save category</button>
            </div>
          </form>
        </div>

        <div class="settings-stack">
          <div class="chart-card settings-card">
            <div class="section-head">
              <h3 class="section-title">At a <em>glance</em></h3>
            </div>
            <div class="settings-kpis">
              <div class="settings-kpi">
                <div class="settings-kpi-label">Categories</div>
                <div class="settings-kpi-value">${customCount}</div>
              </div>
              <div class="settings-kpi">
                <div class="settings-kpi-label">Available</div>
                <div class="settings-kpi-value">${remaining}</div>
              </div>
              <div class="settings-kpi">
                <div class="settings-kpi-label">Limit</div>
                <div class="settings-kpi-value">${customLimit}</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="table-card">
        <div class="section-head">
          <h3 class="section-title">Category <em>library</em></h3>
          <span class="section-meta">${categoryOptions.length} total</span>
        </div>
        <div class="category-library">
          <div class="category-library-grid">
            ${categoryOptions.map(option => `
              <div class="category-library-card" data-category-card="${option.key}">
                <div class="category-library-head">
                  <div class="category-library-title">
                    <span class="category-library-swatch" style="--category-color:${option.color}"></span>
                    <span class="category-library-name">${escapeHtml(option.label)}</span>
                  </div>
                </div>
                <div class="category-library-meta">
                  <span>${option.assignment_count || 0} assigned project${option.assignment_count === 1 ? '' : 's'}</span>
                  <div class="category-library-actions">
                    <button class="btn subtle small" type="button" data-category-edit="${option.key}">Edit</button>
                    <button class="btn danger small" type="button" data-category-delete="${option.key}" data-category-label="${escapeHtml(option.label)}" data-category-assignments="${option.assignment_count || 0}">Delete</button>
                  </div>
                </div>
                <form class="category-editor" data-category-editor="${option.key}" hidden>
                  <div class="category-editor-head">
                    <span class="category-editor-title">Edit category</span>
                    <button class="btn subtle small" type="button" data-category-cancel="${option.key}">Cancel</button>
                  </div>
                  <div class="field-grid">
                    <label class="field">
                      <span class="field-label">Category name</span>
                      <input
                        class="text-input"
                        name="label"
                        type="text"
                        maxlength="32"
                        value="${escapeHtml(option.label)}"
                        required
                      >
                    </label>
                    <label class="field">
                      <span class="field-label">Color</span>
                      ${renderColorField({ value: option.color, showPresets: false })}
                    </label>
                  </div>
                  <div class="category-editor-actions">
                    <button class="btn small" type="submit">Save changes</button>
                    <button class="btn danger small" type="button" data-category-delete="${option.key}" data-category-label="${escapeHtml(option.label)}" data-category-assignments="${option.assignment_count || 0}">Delete</button>
                  </div>
                </form>
              </div>
            `).join('')}
          </div>
        </div>
      </div>
    </div>
  `;

  const form = document.getElementById('categoryCreateForm');
  if (!form) return;
  bindColorField(form);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const nameInput = document.getElementById('categoryNameInput');
    const colorInput = document.getElementById('categoryColorInput');
    const submitButton = form.querySelector('button[type="submit"]');
    const label = nameInput.value.trim();
    const color = colorInput.value;
    if (!label) {
      toast('Add a category name first');
      nameInput.focus();
      return;
    }
    submitButton.disabled = true;
    try {
      const result = await postJson('/api/category-options', { label, color });
      toast(`Created ${result.category.label}`);
      form.reset();
      colorInput.value = '#7C5CFF';
      bindColorField(form);
      await load();
    } catch (error) {
      toast(error.message || 'Failed to create category');
    } finally {
      submitButton.disabled = remaining === 0;
    }
  });

  const toggleEditor = (key, open) => {
    document.querySelectorAll('[data-category-editor]').forEach(editor => {
      const isOpen = editor.dataset.categoryEditor === key && open;
      editor.hidden = !isOpen;
      editor.closest('[data-category-card]')?.classList.toggle('is-editing', isOpen);
    });
    document.querySelectorAll('[data-category-edit]').forEach(button => {
      const isOpen = button.dataset.categoryEdit === key && open;
      button.setAttribute('aria-expanded', String(isOpen));
      button.textContent = isOpen ? 'Close' : 'Edit';
    });
  };

  document.querySelectorAll('[data-category-edit]').forEach(button => {
    button.addEventListener('click', () => {
      const editor = document.querySelector(`[data-category-editor="${button.dataset.categoryEdit}"]`);
      const shouldOpen = !editor || editor.hidden;
      toggleEditor(button.dataset.categoryEdit, shouldOpen);
      if (!shouldOpen) return;
      document
        .querySelector(`[data-category-editor="${button.dataset.categoryEdit}"] input[name="label"]`)
        ?.focus();
    });
  });

  document.querySelectorAll('[data-category-cancel]').forEach(button => {
    button.addEventListener('click', () => toggleEditor(button.dataset.categoryCancel, false));
  });

  document.querySelectorAll('[data-category-editor]').forEach(editor => {
    bindColorField(editor);
    editor.addEventListener('submit', async event => {
      event.preventDefault();
      const key = editor.dataset.categoryEditor;
      const submitButton = editor.querySelector('button[type="submit"]');
      const label = editor.querySelector('input[name="label"]').value.trim();
      const color = editor.querySelector('input[name="color"]').value;
      submitButton.disabled = true;
      try {
        const result = await postJson('/api/category-options/update', { key, label, color });
        toast(`Updated ${result.category.label}`);
        await load();
      } catch (error) {
        toast(error.message || 'Failed to update category');
      } finally {
        submitButton.disabled = false;
      }
    });
  });

  document.querySelectorAll('[data-category-delete]').forEach(button => {
    button.addEventListener('click', async () => {
      const key = button.dataset.categoryDelete;
      const label = button.dataset.categoryLabel || 'this category';
      const assignments = Number(button.dataset.categoryAssignments || '0');
      const ok = await confirmDialog({
        title: 'Delete custom <em>category</em>?',
        body: assignments > 0
          ? `Deletes <code>${label}</code> and clears it from ${assignments} assigned project${assignments === 1 ? '' : 's'}.`
          : `Deletes <code>${label}</code> from your category library.`,
        confirmLabel: 'Delete category',
      });
      if (!ok) return;
      button.disabled = true;
      try {
        const result = await postJson('/api/category-options/delete', { key });
        toast(
          result.cleared_assignments > 0
            ? `Deleted ${result.category.label} and cleared ${result.cleared_assignments} project${result.cleared_assignments === 1 ? '' : 's'}`
            : `Deleted ${result.category.label}`
        );
        await load();
      } catch (error) {
        toast(error.message || 'Failed to delete category');
        button.disabled = false;
      }
    });
  });
}

async function clearRecent(){
  const ok = await confirmDialog({
    title: 'Clear <em>all logs?</em>',
    body:  'Permanently deletes closed sessions from your history. If Ableton is recording right now, the live session is preserved. This cannot be undone.',
    confirmLabel: 'Clear logs',
  });
  if (!ok) return;
  try {
    const r = await postAction('/api/clear-recent');
    toast(`Cleared ${r.deleted} closed session${r.deleted === 1 ? '' : 's'}`);
    load();
  } catch(e){ toast('Failed to clear logs'); }
}

async function clearUnsaved(){
  const ok = await confirmDialog({
    title: 'Remove <em>unsaved</em> projects?',
    body:  'Deletes closed sessions logged against <code>Untitled</code> or <code>Untitled Project</code>. If an unsaved draft is recording right now, it is preserved until that session ends.',
    confirmLabel: 'Remove drafts',
  });
  if (!ok) return;
  try {
    const r = await postAction('/api/clear-unsaved');
    toast(r.deleted === 0
      ? 'No unsaved sessions found'
      : `Removed ${r.deleted} unsaved session${r.deleted === 1 ? '' : 's'}`);
    load();
  } catch(e){ toast('Failed to remove drafts'); }
}

function bindRowDeleteTriggers(){
  document.querySelectorAll('.row-del').forEach(btn => {
    if (btn.disabled) return;
    btn.addEventListener('click', () => {
      const ids = (btn.dataset.sessionIds || '')
        .split(',')
        .map(s => parseInt(s, 10))
        .filter(n => Number.isFinite(n));
      const projectName = decodeURIComponent(btn.dataset.projectName || '');
      deleteRecentEntry(ids, projectName);
    });
  });
}

async function deleteRecentEntry(sessionIds, projectName){
  if (!sessionIds.length) return;
  const ok = await confirmDialog({
    title: 'Delete <em>this entry?</em>',
    body: `Permanently removes ${sessionIds.length === 1 ? 'this session' : `the ${sessionIds.length} merged sessions`} for <code>${escapeHtml(projectName || 'this project')}</code> from your history. This cannot be undone.`,
    confirmLabel: 'Delete entry',
  });
  if (!ok) return;
  try {
    const r = await postJson('/api/delete-session', { session_ids: sessionIds });
    if (r.skipped_live) {
      toast('Live session preserved');
    } else {
      toast(`Deleted ${r.deleted} session${r.deleted === 1 ? '' : 's'}`);
    }
    load();
  } catch(e){ toast('Failed to delete entry'); }
}

async function clearPhantoms(){
  const ok = await confirmDialog({
    title: 'Remove phantom <em>sessions</em>?',
    body:  'Deletes closed rows that were clearly captured from export dialogs or plugin windows instead of real Live sets.',
    confirmLabel: 'Clean phantoms',
  });
  if (!ok) return;
  try {
    const r = await postAction('/api/clear-phantoms');
    toast(r.deleted === 0
      ? 'No phantom sessions found'
      : `Removed ${r.deleted} phantom session${r.deleted === 1 ? '' : 's'}`);
    load();
  } catch(e){ toast('Failed to clean phantom sessions'); }
}

async function load() {
  try {
    const res  = await fetch('/api/data');
    const data = await res.json();
    latestDashboardData = data;
    render(data);
    document.getElementById('updatedAt').textContent =
      'Synced ' + new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  } catch(e) {
    console.error(e);
  }
}

function lastMonthPace(yearDaily, today = new Date()) {
  if (!Array.isArray(yearDaily) || yearDaily.length === 0) return null;
  const totals = new Map();
  for (const row of yearDaily) {
    if (row && row.day) totals.set(row.day, row.total_seconds || 0);
  }
  const y = today.getFullYear();
  const m = today.getMonth();
  const dayOfMonth = today.getDate();
  const iso = (d) => {
    const yy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yy}-${mm}-${dd}`;
  };
  let thisSum = 0;
  for (let d = 1; d <= dayOfMonth; d++) thisSum += totals.get(iso(new Date(y, m, d))) || 0;
  const lastMonthEnd = new Date(y, m, 0).getDate();
  const cap = Math.min(dayOfMonth, lastMonthEnd);
  let lastSum = 0;
  for (let d = 1; d <= cap; d++) lastSum += totals.get(iso(new Date(y, m - 1, d))) || 0;
  if (lastSum <= 0) return null;
  const pct = ((thisSum - lastSum) / lastSum) * 100;
  return { pct: Math.round(pct), positive: pct >= 0 };
}

function longestStreak(yearDaily) {
  if (!Array.isArray(yearDaily) || yearDaily.length === 0) return 0;
  const sorted = yearDaily
    .filter(r => r && r.day && (r.total_seconds || 0) > 0)
    .map(r => r.day)
    .sort();
  if (sorted.length === 0) return 0;
  const dayMs = 86400000;
  let best = 1;
  let cur = 1;
  let prev = new Date(sorted[0] + 'T00:00:00');
  for (let i = 1; i < sorted.length; i++) {
    const d = new Date(sorted[i] + 'T00:00:00');
    const gap = Math.round((d - prev) / dayMs);
    if (gap === 1) cur += 1;
    else if (gap > 1) cur = 1;
    if (cur > best) best = cur;
    prev = d;
  }
  return best;
}

function render(data) {
  if (data.error) {
    document.getElementById('app').innerHTML =
      `<div class="empty"><p>${escapeHtml(data.error)}</p><small>run start_tracker.command</small></div>`;
    return;
  }
  syncCategoryOptions(data.category_options);
  const { summary, projects, year_daily = [], year_hourly = [], recent } = data;
  if (summary.week_start_weekday != null) {
    WEEK_START_BACKEND_DAY = summary.week_start_weekday;
  }

  if (activeView === 'settings') {
    renderSettings(data);
    return;
  }

  updateSessionStatus(summary);

  if (summary.live_project) {
    document.getElementById('liveBadge').style.display = 'inline-flex';
    document.getElementById('liveProject').textContent = summary.live_project;
  } else {
    document.getElementById('liveBadge').style.display = 'none';
  }

  const maxProj = projects[0]?.total_seconds || 1;
  const unsavedCount = summary.unsaved_closed_count || 0;
  const closedSessionCount = summary.closed_session_count || 0;
  const phantomCount = summary.phantom_closed_count || 0;
  const todaySessionCount = summary.today_session_count || 0;
  const todayProjectCount = summary.today_project_count || 0;

  const pace = lastMonthPace(year_daily);
  let paceChip;
  if (!pace) {
    paceChip = `<div class="card-chip">— vs last month</div>`;
  } else {
    const sign = pace.pct > 0 ? '+' : '';
    const cls = pace.positive ? ' card-chip--good' : '';
    paceChip = `<div class="card-chip${cls}">${sign}${pace.pct}% vs last month</div>`;
  }

  const best = longestStreak(year_daily);
  const cur = summary.streak_days || 0;
  let streakChip;
  if (best === 0) {
    streakChip = `<div class="card-chip">Best: —</div>`;
  } else if (cur >= best && cur > 0) {
    streakChip = `<div class="card-chip card-chip--record">New record</div>`;
  } else {
    streakChip = `<div class="card-chip">Best: ${best} day${best !== 1 ? 's' : ''}</div>`;
  }

  // Top project this month (uses month_seconds field added server-side)
  const topProject = [...projects]
    .filter(p => (p.month_seconds || 0) > 0)
    .sort((a, b) => (b.month_seconds || 0) - (a.month_seconds || 0))[0];
  const topProjectShare = topProject && summary.month_seconds > 0
    ? Math.round((topProject.month_seconds / summary.month_seconds) * 100)
    : 0;

  // Best day this month from year_daily
  const now = new Date();
  const monthPrefix = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-`;
  const bestDay = (year_daily || [])
    .filter(r => r && r.day && r.day.startsWith(monthPrefix) && (r.total_seconds || 0) > 0)
    .reduce((acc, r) => (!acc || r.total_seconds > acc.total_seconds ? r : acc), null);
  let bestDayValue, bestDayChip, bestDaySub;
  if (!bestDay) {
    bestDayValue = '—';
    bestDayChip = `<div class="card-chip">No activity yet</div>`;
    bestDaySub = 'this month';
  } else {
    const bdDate = new Date(bestDay.day + 'T00:00:00');
    const bdLabel = bdDate.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
    const todayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    bestDayValue = fmt.dur(bestDay.total_seconds);
    bestDaySub = bdLabel;
    bestDayChip = bestDay.day === todayKey
      ? `<div class="card-chip card-chip--record">Today's the one</div>`
      : `<div class="card-chip">Best day this month</div>`;
  }

  document.getElementById('app').innerHTML = `
    <div class="cards">
      <div class="card">
        <div class="card-label">Top Project</div>
        ${topProject
          ? `<div class="card-value card-value--text" title="${escapeHtml(topProject.project_name)}">${escapeHtml(topProject.project_name)}</div>
             <div class="card-chip">${fmt.dur(topProject.month_seconds)} · ${topProjectShare}% of month</div>
             <div class="card-sub"><span class="proj-dot" style="background:${projectColor(topProject, 0)}"></span>${topProject.category_label || 'Uncategorized'}</div>`
          : `<div class="card-value">—</div>
             <div class="card-chip">No projects yet</div>
             <div class="card-sub">this month</div>`}
      </div>
      <div class="card">
        <div class="card-label">Best Day</div>
        <div class="card-value">${bestDayValue}</div>
        ${bestDayChip}
        <div class="card-sub">${bestDaySub}</div>
      </div>
      <div class="card">
        <div class="card-label">This Month</div>
        <div class="card-value accent">${fmt.dur(summary.month_seconds)}</div>
        ${paceChip}
        <div class="card-sub">${summary.month_project_count} project${summary.month_project_count !== 1 ? 's' : ''} · this month</div>
      </div>
      <div class="card">
        <div class="card-label">Streak</div>
        <div class="card-value">${summary.streak_days}<span class="unit">day${summary.streak_days !== 1 ? 's' : ''}</span></div>
        ${streakChip}
        <div class="card-sub">consecutive</div>
      </div>
    </div>

    <div class="rings-row">
      <div class="chart-card target-card">
        <div class="section-head">
          <h3 class="section-title">Daily <em>Target</em></h3>
          <div class="section-meta">
            <span>Today</span>
            <strong>${fmt.dur(summary.today_seconds || 0)}</strong>
            <span class="section-meta-note" id="dailyGoalRemaining"></span>
          </div>
        </div>
        <div class="chart-wrap"><div id="dailyGoalCard"></div></div>
      </div>
      <div class="chart-card target-card">
          <div class="section-head">
            <h3 class="section-title">Weekly <em>Target</em></h3>
            <div class="section-meta">
            <span>${weekStartDayName()}</span>
            <strong>${fmt.dur(summary.week_seconds || 0)}</strong>
            <span class="section-meta-note" id="weeklyGoalRemaining"></span>
          </div>
        </div>
        <div class="chart-wrap"><div id="weeklyGoalCard"></div></div>
      </div>
    </div>

    <div class="chart-card chart-card-wide">
      <div class="section-head">
        <h3 class="section-title">Activity <em>calendar</em></h3>
        <span class="section-meta">${weekStartDayName()} to ${weekEndDayName()} · browse week by week</span>
      </div>
      <div class="chart-wrap"><div id="activityHeatmap"></div></div>
    </div>

    <div class="chart-card chart-card-wide">
      <div class="section-head">
        <h3 class="section-title">By <em>category</em></h3>
        <span class="section-meta">hours per category</span>
      </div>
      <div class="chart-wrap"><div id="categoryChart"></div></div>
    </div>

    <div class="chart-card chart-card-wide">
      <div class="section-head">
        <h3 class="section-title">By <em>project</em></h3>
        <span class="section-meta">top 10 · hours</span>
      </div>
      <div class="chart-wrap"><div id="projectChart"></div></div>
    </div>

    <div class="table-card">
      <div class="section-head">
        <h3 class="section-title">Projects</h3>
        <button class="btn danger" id="btnClearUnsaved" ${unsavedCount === 0 ? 'disabled' : ''}>
          <span class="x">×</span> Clear unsaved${unsavedCount > 0 ? ` · ${unsavedCount}` : ''}
        </button>
      </div>
      ${projects.length === 0
        ? '<div class="empty"><p>No projects yet</p><small>waiting for a live session</small></div>'
        : `<div class="table-scroll"><table>
            <thead><tr>
              <th style="width:40px">#</th>
              <th>Project</th>
              <th>Category</th>
              <th style="text-align:right">Total</th>
              <th style="text-align:right">Sessions</th>
              <th style="text-align:right">Avg</th>
              <th style="text-align:right">Last active</th>
              <th style="text-align:right"></th>
            </tr></thead>
            <tbody>
              ${projects.map((p,i) => {
                return `
                <tr>
                  <td><span class="rank">${String(i+1).padStart(2,'0')}</span></td>
                  <td>
                    <div class="proj-badge-wrap">
                      ${projectBadge(p, i)}
                    </div>
                  </td>
                  <td>${categoryPill(p)}</td>
                  <td class="dur">${fmt.dur(p.total_seconds)}</td>
                  <td class="num">${p.session_count}</td>
                  <td class="num">${fmt.dur(p.avg_seconds)}</td>
                  <td class="num">${fmt.date(p.last_seen)}</td>
                  <td style="text-align:right;padding-right:14px">
                    <div class="bar-bg">
                      <div class="bar-fill" style="width:${Math.round(p.total_seconds/maxProj*100)}%;background:linear-gradient(90deg, color-mix(in srgb, ${projectColor(p, i)} 50%, white), ${projectColor(p, i)})"></div>
                    </div>
                  </td>
                </tr>`;
              }).join('')}
            </tbody>
          </table></div>`}
    </div>

    <div class="table-card">
      <div class="section-head">
        <h3 class="section-title">Recent <em>entries</em></h3>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button class="btn danger" id="btnClearPhantoms" ${phantomCount === 0 ? 'disabled' : ''}>
            <span class="x">×</span> Clean phantom logs${phantomCount > 0 ? ` · ${phantomCount}` : ''}
          </button>
          <button class="btn danger" id="btnClearRecent" ${closedSessionCount === 0 ? 'disabled' : ''}>
            <span class="x">×</span> Clear logs
          </button>
        </div>
      </div>
      <div class="recent-stats" aria-label="Today's session stats">
        <div class="recent-stat">
          <span class="recent-stat-label">Avg session</span>
          <strong class="recent-stat-value">${fmt.dur(summary.today_average_session_seconds || 0)}</strong>
          <span class="recent-stat-note">across today</span>
        </div>
        <div class="recent-stat">
          <span class="recent-stat-label">Sessions worked</span>
          <strong class="recent-stat-value">${todaySessionCount}</strong>
          <span class="recent-stat-note">today</span>
        </div>
        <div class="recent-stat">
          <span class="recent-stat-label">Projects touched</span>
          <strong class="recent-stat-value">${todayProjectCount}</strong>
          <span class="recent-stat-note">distinct ${todayProjectCount === 1 ? 'project' : 'projects'}</span>
        </div>
      </div>
      ${recent.length === 0
        ? '<div class="empty"><p>No sessions logged</p><small>start Ableton to begin</small></div>'
        : `<div class="table-scroll"><table>
            <thead><tr>
              <th>Project</th><th>Started</th><th>Ended</th><th style="text-align:right">Duration</th><th aria-label="Delete"></th>
            </tr></thead>
            <tbody>
              ${recent.map(s => {
                const ids = (s.session_ids || []).join(',');
                const isLive = !s.end_time;
                const delTitle = isLive
                  ? 'Cannot delete the live session'
                  : 'Delete this entry';
                return `
                <tr>
                  <td>
                    <div class="proj-badge-wrap">
                      ${projectBadge(s)}
                      ${s.category_key ? categoryPill(s) : ''}
                    </div>
                  </td>
                  <td class="num" style="text-align:left">${fmt.datetime(s.start_time)}</td>
                  <td class="num" style="text-align:left">${s.end_time
                    ? fmt.datetime(s.end_time)
                    : '<span class="active-tag"><span class="dot"></span>active now</span>'}</td>
                  <td class="dur">${fmt.dur(s.active_seconds)}</td>
                  <td class="row-action">
                    <button class="row-del" type="button"
                      data-session-ids="${ids}"
                      data-project-name="${encodeURIComponent(s.project_name || '')}"
                      title="${delTitle}"
                      ${isLive || !ids ? 'disabled' : ''}>×</button>
                  </td>
                </tr>`;
              }).join('')}
            </tbody>
          </table></div>`}
    </div>
  `;

  const br = document.getElementById('btnClearRecent');
  const bp = document.getElementById('btnClearPhantoms');
  const bu = document.getElementById('btnClearUnsaved');
  if (br) br.addEventListener('click', clearRecent);
  if (bp) bp.addEventListener('click', clearPhantoms);
  if (bu) bu.addEventListener('click', clearUnsaved);
  bindCategoryTriggers();
  bindRowDeleteTriggers();

  renderActivityHeatmap(year_daily, year_hourly);
  renderWeeklyGoal(summary);
  renderDailyGoal(summary);
  renderProjectChart(projects);
  renderCategoryChart(projects);
}

function updateSessionStatus(summary) {
  const status = document.getElementById('sessionStatus');
  const label = document.getElementById('sessionStatusText');
  const indicator = document.getElementById('sessionStatusIndicator');
  const timer = document.getElementById('sessionTimer');
  const timerLabel = document.getElementById('sessionTimerLabel');
  if (!status || !label || !indicator || !timer || !timerLabel) return;

  let state = 'off';
  let text = 'Off';
  let indicatorHtml = '<span class="session-status__dot"></span>';
  let timerLabelText = 'Session';
  let timerBaseSeconds = 0;

  if (summary?.ableton_has_project) {
    state = 'live';
    text = 'Live';
    indicator.className = 'session-status__indicator session-status__indicator--live';
    indicatorHtml = '<span class="session-status__dot"></span>';
    timerBaseSeconds = Number(summary.live_session_duration_seconds) || 0;
  } else if (summary?.ableton_running) {
    state = 'booting';
    text = 'Booting up';
    indicator.className = 'session-status__indicator session-status__indicator--booting';
    indicatorHtml = '<span class="session-status__dot"></span>';
    timerLabelText = 'Status';
  } else {
    state = 'off';
    text = 'Off';
    indicator.className = 'session-status__indicator session-status__indicator--off';
    indicatorHtml = '<span class="session-status__dot"></span>';
    timerLabelText = 'Status';
  }

  sessionTimerSnapshot = {
    state,
    baseSeconds: timerBaseSeconds,
    syncedAtMs: Date.now(),
  };
  status.dataset.state = state;
  label.textContent = text;
  indicator.innerHTML = indicatorHtml;
  timer.dataset.state = state;
  timerLabel.textContent = timerLabelText;
  tickSessionTimer();
}

function renderActivityHeatmap(yearDaily, yearHourly) {
  const mount = document.getElementById('activityHeatmap');
  if (!mount) return;

  const activityByDay = {};
  yearDaily.forEach(d => {
    activityByDay[d.day] = (d.total_seconds || 0) / 3600;
  });

  const activityByDayHour = {};
  (yearHourly || []).forEach(r => {
    activityByDayHour[`${r.day}_${r.hour}`] = r.active_seconds;
  });

  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const start = addDays(today, -364);
  const todayKey = localDateKey(today);
  const weeks = [];
  let cursor = new Date(startOfWeek(start));
  const lastWeekStart = startOfWeek(today);

  while (cursor <= lastWeekStart) {
    const days = [];
    for (let index = 0; index < 7; index++) {
      const day = addDays(cursor, index);
      const key = localDateKey(day);
      const inRange = day >= start && day <= today;
      const isUpcoming = day > today;
      const value = inRange ? Math.round((activityByDay[key] || 0) * 10) / 10 : 0;
      days.push({ index, key, date: day, inRange, isUpcoming, value, isToday: key === todayKey });
    }
    if (days.some(d => d.inRange)) {
      weeks.push({ startKey: localDateKey(days[0].date), endKey: localDateKey(days[6].date), days });
    }
    cursor = addDays(cursor, 7);
  }

  if (weeks.length === 0) {
    mount.innerHTML = `<div class="chart-empty">No ${weekStartDayName()}-to-${weekEndDayName()} weeks to show yet.</div>`;
    return;
  }

  let currentWeekIndex = weeks.length - 1;

  const hourLabel = h => {
    if (h === 0)  return '12am';
    if (h === 12) return '12pm';
    return h < 12 ? `${h}am` : `${h - 12}pm`;
  };

  // Y axis: 24 labels top-to-bottom (h=23 → h=0), text only every 3 hours
  const yAxisHTML = Array.from({ length: 24 }, (_, i) => {
    const h = 23 - i;
    const show = h % 3 === 0;
    return `<div class="heatmap-yaxis-label">${show ? hourLabel(h) : ''}</div>`;
  }).join('');

  mount.innerHTML = `
    <div class="heatmap-shell">
      <div class="heatmap-meta">
        <div class="heatmap-summary" id="activityHeatmapSummary"></div>
        <div class="heatmap-controls">
          <div class="heatmap-scale">
            <span>0 min</span>
            <div class="heatmap-scale-row">
              ${[0, 0.2, 0.45, 0.7, 1].map(level => `
                <span class="heatmap-swatch" style="background:${heatmapColor(level * 3600, 3600)}"></span>
              `).join('')}
            </div>
            <span>60 min</span>
          </div>
          <div class="heatmap-nav">
            <button class="heatmap-nav-btn" id="heatmapPrevWeek" type="button" aria-label="Show previous week">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M8.75 2.5L4.25 7l4.5 4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
            <button class="heatmap-nav-btn" id="heatmapNextWeek" type="button" aria-label="Show next week">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M5.25 2.5L9.75 7l-4.5 4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
          </div>
        </div>
      </div>
      <div class="heatmap-week-label" id="activityHeatmapWeekLabel"></div>
      <div class="heatmap-stage" id="activityHeatmapStage"></div>
      <div class="heatmap-foot" id="activityHeatmapFoot"></div>
    </div>
  `;

  const summaryEl  = mount.querySelector('#activityHeatmapSummary');
  const weekLabelEl = mount.querySelector('#activityHeatmapWeekLabel');
  const stageEl    = mount.querySelector('#activityHeatmapStage');
  const footEl     = mount.querySelector('#activityHeatmapFoot');
  const prevBtn    = mount.querySelector('#heatmapPrevWeek');
  const nextBtn    = mount.querySelector('#heatmapNextWeek');
  let heatmapResizeObserver = null;

  function renderWeek() {
    const week = weeks[currentWeekIndex];
    const visibleDays = week.days.filter(d => d.inRange);
    const activeDays  = visibleDays.filter(d => d.value > 0).length;
    const totalHours  = Math.round(visibleDays.reduce((s, d) => s + d.value, 0) * 10) / 10;
    const peakDay     = visibleDays.reduce((best, d) => (
      d.value > 0 && (!best || d.value > best.value) ? d : best
    ), null);

    summaryEl.innerHTML = `
      <strong>${activeDays} active day${activeDays === 1 ? '' : 's'}</strong>
      <span>${formatHoursNumber(totalHours)}h in this ${weekStartDayName()}-to-${weekEndDayName()} window</span>
    `;
    weekLabelEl.textContent = `${shortRange(week.startKey, week.endKey)}${currentWeekIndex === weeks.length - 1 ? ' · current week' : ''}`;

    const headersHTML = week.days.map(day => {
      const cls = ['heatmap-day-head', day.isToday ? 'is-today' : ''].filter(Boolean).join(' ');
      const dailyTotal = day.isUpcoming ? '' : day.value > 0 ? `${formatHoursNumber(day.value)}h` : '';
      return `
        <div class="${cls}">
          <strong>${day.date.toLocaleDateString('en-US', { weekday:'short' })}</strong>
          <span>${day.date.toLocaleDateString('en-US', { month:'short', day:'numeric' })}</span>
          <span class="day-total">${dailyTotal}</span>
        </div>
      `;
    }).join('');

    // Rows: h=23 at top → h=0 at bottom (night at top, morning at bottom)
    const rowsHTML = Array.from({ length: 24 }, (_, i) => {
      const h = 23 - i;
      const cells = week.days.map(day => {
        if (!day.inRange) return `<div class="heatmap-cell"></div>`;
        const secs = Math.min(activityByDayHour[`${day.key}_${h}`] || 0, 3600);
        const mins = Math.round(secs / 60);
        const label = day.date.toLocaleDateString('en-US', { weekday:'short', month:'short', day:'numeric' });
        const title = secs > 0 ? `${label} ${hourLabel(h)}: ${mins}m active` : `${label} ${hourLabel(h)}: no activity`;
        return `<div class="heatmap-cell" title="${escapeHtml(title)}"></div>`;
      }).join('');
      return `<div class="heatmap-grid-row">${cells}</div>`;
    }).join('');

    stageEl.innerHTML = `
      <div class="heatmap-grid-wrap">
        <div class="heatmap-yaxis">${yAxisHTML}</div>
        <div class="heatmap-grid-body">
          <div class="heatmap-grid-headers">${headersHTML}</div>
          <div class="heatmap-grid-rows">
            <canvas class="heatmap-canvas"></canvas>
            ${rowsHTML}
          </div>
        </div>
      </div>
    `;

    const canvas = stageEl.querySelector('.heatmap-canvas');
    const gridRows = stageEl.querySelector('.heatmap-grid-rows');
    const paint = () => paintHeatmapCanvas(canvas, gridRows, week.days, activityByDayHour);
    requestAnimationFrame(paint);
    if (heatmapResizeObserver) heatmapResizeObserver.disconnect();
    heatmapResizeObserver = new ResizeObserver(paint);
    heatmapResizeObserver.observe(gridRows);

    footEl.textContent = peakDay
      ? `Peak this week: ${peakDay.date.toLocaleDateString('en-US', { weekday:'short', month:'short', day:'numeric' })} with ${formatHoursNumber(peakDay.value)}h.`
      : `No activity in this week yet. Use the arrows to browse older ${weekStartDayName()}-to-${weekEndDayName()} windows.`;

    prevBtn.disabled = currentWeekIndex === 0;
    nextBtn.disabled = currentWeekIndex === weeks.length - 1;
  }

  prevBtn.addEventListener('click', () => {
    if (currentWeekIndex === 0) return;
    currentWeekIndex -= 1;
    renderWeek();
  });
  nextBtn.addEventListener('click', () => {
    if (currentWeekIndex === weeks.length - 1) return;
    currentWeekIndex += 1;
    renderWeek();
  });

  renderWeek();
}

function renderGoalCard({
  mountId,
  storageKey,
  fallbackGoalHours,
  completedSeconds,
  rangeLabel,
  goalLabel,
  helperLabel,
  remainingId,
  presets,
}) {
  const mount = document.getElementById(mountId);
  if (!mount) return;

  const completedHours = Math.round(((completedSeconds || 0) / 3600) * 10) / 10;
  const remainingMount = remainingId ? document.getElementById(remainingId) : null;
  const coldColor = '#1677ff';
  const coolColor = '#33c7ff';
  const warmColor = '#ffc247';
  const hotColor = '#ff3b30';

  function lerpColor(a, b, amount) {
    const parse = color => [1, 3, 5].map(start => parseInt(color.slice(start, start + 2), 16));
    const from = parse(a);
    const to = parse(b);
    return '#' + from.map((part, index) => {
      const value = Math.round(part + (to[index] - part) * amount);
      return value.toString(16).padStart(2, '0');
    }).join('');
  }

  function ringColorStops(progressClamped) {
    if (progressClamped <= 1 / 3) {
      const endColor = lerpColor(coldColor, coolColor, progressClamped * 3);
      return { cool: endColor, warm: endColor, hot: endColor };
    }
    if (progressClamped <= 2 / 3) {
      const endColor = lerpColor(coolColor, warmColor, (progressClamped - 1 / 3) * 3);
      return { cool: coolColor, warm: endColor, hot: endColor };
    }
    const endColor = lerpColor(warmColor, hotColor, (progressClamped - 2 / 3) * 3);
    return { cool: coolColor, warm: warmColor, hot: endColor };
  }

  function remainingGoalLabel(goalHours) {
    const goalSeconds = goalHours * 3600;
    const deltaSeconds = Math.round(goalSeconds - (completedSeconds || 0));
    if (deltaSeconds <= 0) {
      const overSeconds = Math.abs(deltaSeconds);
      return overSeconds > 0
        ? `Goal met · ${fmt.dur(overSeconds)} extra`
        : 'Goal met right on target';
    }
    return `${fmt.dur(deltaSeconds)} left to goal`;
  }

  function paint(goalHours) {
    const safeGoalHours = clamp(Math.round(goalHours * 10) / 10, 1, 100);
    const progress = completedHours / safeGoalHours;
    const progressClamped = clamp(progress, 0, 1);
    const progressDegrees = Math.round(progressClamped * 360);
    const overflowDegrees = Math.round(clamp(progress - 1, 0, 1) * 360);
    const ringClasses = [
      'goal-ring',
      progressDegrees === 0 ? 'is-zero' : '',
      progressDegrees >= 360 ? 'is-complete' : '',
      overflowDegrees > 0 ? 'has-overflow' : '',
    ].filter(Boolean).join(' ');
    const progressMidDegrees = Math.min(progressDegrees, 120);
    const progressHotDegrees = Math.min(progressDegrees, 240);
    const ringStops = ringColorStops(progressClamped);
    const percent = Math.round(progress * 100);
    const percentLabel = `${Math.min(percent, 999)}%`;
    const helperMarkup = helperLabel ? `<span>${helperLabel}</span>` : '';
    if (remainingMount) {
      remainingMount.textContent = remainingGoalLabel(safeGoalHours);
    }

    mount.innerHTML = `
      <div class="goal-shell">
        <div class="goal-ring-wrap">
          <div class="${ringClasses}" style="--goal-progress:${progressDegrees}deg;--goal-overflow:${overflowDegrees}deg;--goal-progress-mid:${progressMidDegrees}deg;--goal-progress-hot:${progressHotDegrees}deg;--goal-cool:${ringStops.cool};--goal-warm:${ringStops.warm};--goal-hot:${ringStops.hot}" aria-label="${percentLabel} complete">
            <div class="goal-ring-core">
              <div class="goal-value">${percentLabel}</div>
            </div>
          </div>
        </div>
        <div class="goal-controls">
          <div class="goal-range">${rangeLabel}</div>
          <div class="goal-input-row">
            <label class="goal-label" for="${mountId}Input">
              <strong>${goalLabel}</strong>
              ${helperMarkup}
            </label>
            <div class="goal-input-wrap">
              <input class="goal-input" id="${mountId}Input" type="number" min="1" max="100" step="0.5" value="${safeGoalHours}">
              <span class="goal-unit">hours</span>
            </div>
          </div>
          <div class="goal-presets">
            ${presets.map(hours => `
              <button class="goal-chip" type="button" data-goal-hours="${hours}">${hours}h</button>
            `).join('')}
          </div>
        </div>
      </div>
    `;

    const input = mount.querySelector(`#${mountId}Input`);
    if (input) {
      const commit = () => {
        const nextValue = clamp(Number(input.value) || safeGoalHours, 1, 100);
        setStoredGoalHours(storageKey, nextValue);
        paint(nextValue);
      };
      input.addEventListener('change', commit);
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') commit();
      });
    }

    mount.querySelectorAll('[data-goal-hours]').forEach(button => {
      button.addEventListener('click', () => {
        const nextValue = Number(button.dataset.goalHours);
        setStoredGoalHours(storageKey, nextValue);
        paint(nextValue);
      });
    });
  }

  paint(getStoredGoalHours(storageKey, fallbackGoalHours));
}

function renderWeeklyGoal(summary) {
  renderGoalCard({
    mountId: 'weeklyGoalCard',
    storageKey: WEEKLY_GOAL_STORAGE_KEY,
    fallbackGoalHours: 12,
    completedSeconds: summary.week_seconds || 0,
    rangeLabel: `${shortRange(summary.goal_week_start, summary.goal_week_end)} · resets ${weekStartDayName()}`,
    goalLabel: 'Weekly goal',
    remainingId: 'weeklyGoalRemaining',
    presets: [10, 20, 30, 40],
  });
}

function renderDailyGoal(summary) {
  const todayLabel = new Date().toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'short',
    day: 'numeric',
  });
  renderGoalCard({
    mountId: 'dailyGoalCard',
    storageKey: DAILY_GOAL_STORAGE_KEY,
    fallbackGoalHours: 3,
    completedSeconds: summary.today_seconds || 0,
    rangeLabel: `${todayLabel} · resets tomorrow`,
    goalLabel: 'Daily goal',
    remainingId: 'dailyGoalRemaining',
    presets: [1, 2, 3, 5],
  });
}

function renderProjectChart(projects) {
  const mount = document.getElementById('projectChart');
  if (!mount) return;
  if (projects.length === 0) {
    mount.innerHTML = '<div class="chart-empty">No project data yet</div>';
    return;
  }

  const top = projects.slice(0, 10);
  const totalSeconds = top.reduce((sum, project) => sum + (project.total_seconds || 0), 0) || 1;
  const chartColors = top.map((_, index) => projectChartColor(index, top.length));
  let start = 0;
  const segments = top.map((project, index) => {
    const share = (project.total_seconds || 0) / totalSeconds;
    const end = start + (share * 100);
    const segment = `${chartColors[index]} ${start}% ${end}%`;
    start = end;
    return segment;
  });
  const totalHours = Math.round((totalSeconds / 3600) * 10) / 10;

  mount.innerHTML = `
    <div class="donut-layout">
      <div class="donut-chart" style="background:conic-gradient(${segments.join(', ')})">
        <div class="donut-hole">
          <div class="donut-total">${totalHours}h</div>
          <div class="donut-caption">Top 10 total</div>
        </div>
      </div>
      <div class="donut-legend">
        ${top.map((project, index) => {
          const hours = Math.round((project.total_seconds / 3600) * 10) / 10;
          return `
            <div class="legend-item" title="${escapeHtml(project.project_name)}">
              <span class="legend-swatch" style="background:${chartColors[index]}"></span>
              <span class="legend-name">${escapeHtml(project.project_name)}</span>
              <span class="legend-value">${hours}h</span>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;
}

function renderCategoryChart(projects) {
  const mount = document.getElementById('categoryChart');
  if (!mount) return;

  const buckets = new Map();
  projects.forEach(project => {
    const seconds = project.total_seconds || 0;
    if (seconds <= 0) return;
    const key = project.category_key || '__uncategorized';
    const meta = project.category_key ? CATEGORY_BY_KEY[project.category_key] : null;
    const label = meta?.label || project.category_label || 'Uncategorized';
    const color = meta?.color || project.category_color || '#8E8E93';
    if (!buckets.has(key)) {
      buckets.set(key, { key, label, color, total_seconds: 0 });
    }
    buckets.get(key).total_seconds += seconds;
  });

  let entries = [...buckets.values()]
    .filter(entry => entry.total_seconds > 0)
    .sort((a, b) => b.total_seconds - a.total_seconds);

  if (entries.length === 0) {
    mount.innerHTML = '<div class="chart-empty">No category data yet</div>';
    return;
  }

  if (entries.length > 6) {
    const topEntries = entries.slice(0, 5);
    const otherSeconds = entries.slice(5).reduce((sum, entry) => sum + entry.total_seconds, 0);
    if (otherSeconds > 0) {
      topEntries.push({
        key: '__other',
        label: 'Other',
        color: '#8E8E93',
        total_seconds: otherSeconds,
      });
    }
    entries = topEntries;
  }

  const totalSeconds = entries.reduce((sum, entry) => sum + entry.total_seconds, 0) || 1;
  let startPercent = 0;
  const chartEntries = entries.map((entry, index) => {
    const percent = (entry.total_seconds / totalSeconds) * 100;
    const endPercent = index === entries.length - 1 ? 100 : startPercent + percent;
    const chartEntry = {
      ...entry,
      percent,
      start_percent: startPercent,
      end_percent: endPercent,
    };
    startPercent = endPercent;
    return chartEntry;
  });
  const totalHours = Math.round((totalSeconds / 3600) * 10) / 10;

  mount.innerHTML = `
    <div class="donut-layout">
      <div class="bar-column">
        <div class="bar-header">
          <div class="donut-total">${totalHours}h</div>
          <div class="donut-caption">All categories</div>
        </div>
        <div id="donutChart" class="donut-chart">
          ${chartEntries.map((entry, index) => `
            <div class="bar-segment" data-segment-index="${index}" style="width:${entry.percent.toFixed(3)}%;background:${entry.color}"></div>
          `).join('')}
        </div>
      </div>
      <div class="donut-legend">
        ${chartEntries.map((entry, index) => {
          const hours = Math.round((entry.total_seconds / 3600) * 10) / 10;
          return `
            <div class="legend-item" data-legend-index="${index}" title="${escapeHtml(entry.label)}">
              <span class="legend-swatch" style="background:${entry.color}"></span>
              <span class="legend-name">${escapeHtml(entry.label)}</span>
              <span class="legend-value">${hours}h</span>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;

  const chart = mount.querySelector('#donutChart');
  const segments = [...mount.querySelectorAll('.bar-segment')];
  const legendItems = [...mount.querySelectorAll('.legend-item')];
  if (!chart || legendItems.length === 0) return;

  const setHighlight = activeIndex => {
    const hasActive = Number.isInteger(activeIndex) && activeIndex >= 0 && activeIndex < legendItems.length;
    legendItems.forEach((item, index) => {
      item.classList.toggle('is-active', hasActive && index === activeIndex);
      item.classList.toggle('is-muted', hasActive && index !== activeIndex);
    });
    segments.forEach((seg, index) => {
      seg.classList.toggle('is-muted', hasActive && index !== activeIndex);
    });
    chart.classList.toggle('is-hovered', hasActive);
  };

  const findSegmentIndex = event => {
    const rect = chart.getBoundingClientRect();
    const x = event.clientX - rect.left;
    if (x < 0 || x > rect.width) return -1;
    const percent = (x / rect.width) * 100;
    return chartEntries.findIndex(entry =>
      percent >= entry.start_percent &&
      (percent < entry.end_percent || entry.end_percent === 100)
    );
  };

  legendItems.forEach(item => {
    const index = Number(item.dataset.legendIndex);
    item.addEventListener('mouseenter', () => setHighlight(index));
    item.addEventListener('mouseleave', () => setHighlight(-1));
  });

  segments.forEach(seg => {
    const index = Number(seg.dataset.segmentIndex);
    seg.addEventListener('mouseenter', () => setHighlight(index));
    seg.addEventListener('mouseleave', () => setHighlight(-1));
  });

  chart.addEventListener('mousemove', event => {
    const index = findSegmentIndex(event);
    setHighlight(index);
  });
  chart.addEventListener('mouseleave', () => setHighlight(-1));
}

load();
setInterval(load, 5_000);
setInterval(() => {
  if (activeView === 'dashboard') setIntroDates();
}, 30_000);
setInterval(tickSessionTimer, 1_000);
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
#  HTTP server
# ─────────────────────────────────────────────────────────────

def merge_projects(canonical_name: str, aliases: list) -> dict:
    canonical_name = (canonical_name or "").strip()
    if not canonical_name or not isinstance(aliases, list) or not aliases:
        return {"error": "Target project and at least one project to merge are required."}
    if not DB_PATH.exists():
        return {"error": "No data."}
    
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            normalized_aliases = [(alias or "").strip() for alias in aliases]
            if any(not alias for alias in normalized_aliases):
                return {"error": "Project names cannot be empty."}
            if canonical_name in normalized_aliases:
                return {"error": "A project cannot be merged into itself."}
            if len(set(normalized_aliases)) != len(normalized_aliases):
                return {"error": "Duplicate projects were selected."}

            names = [canonical_name, *normalized_aliases]
            placeholders = ",".join("?" for _ in names)
            existing = {
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT project_name FROM sessions WHERE project_name IN ({placeholders})",
                    names,
                ).fetchall()
            }
            missing = [name for name in names if name not in existing]
            if missing:
                return {"error": f"Project not found: {missing[0]}"}

            mapped = conn.execute(
                f"SELECT alias_name FROM project_aliases WHERE alias_name IN ({','.join('?' for _ in normalized_aliases)})",
                normalized_aliases,
            ).fetchall()
            if mapped:
                return {"error": f"Project is already merged: {mapped[0][0]}"}

            target_is_alias = conn.execute(
                "SELECT 1 FROM project_aliases WHERE alias_name = ?", (canonical_name,)
            ).fetchone()
            if target_is_alias:
                return {"error": "The destination project is already merged into another project."}

            conn.execute("BEGIN")
            for alias in normalized_aliases:
                conn.execute(
                    """
                    INSERT INTO project_aliases (alias_name, canonical_name)
                    VALUES (?, ?)
                    """,
                    (alias, canonical_name)
                )
            conn.commit()
            _cache_clear("project_list")
            return {
                "ok": True,
                "canonical_name": canonical_name,
                "aliases": normalized_aliases,
                "merged_count": len(normalized_aliases),
            }
        except Exception as e:
            conn.rollback()
            return {"error": str(e)}


def unmerge_project(alias_name: str) -> dict:
    """Remove a single alias, restoring it as an independent project."""
    alias_name = (alias_name or "").strip()
    if not alias_name:
        return {"error": "alias_name is required"}
    if not DB_PATH.exists():
        return {"error": "No data."}

    with db_connection() as conn:
        try:
            cur = conn.execute(
                "DELETE FROM project_aliases WHERE alias_name = ?",
                (alias_name,),
            )
            if cur.rowcount == 0:
                return {"error": "Alias not found"}
            conn.commit()
            _cache_clear("project_list")
            return {"ok": True, "removed": alias_name}
        except Exception as e:
            conn.rollback()
            return {"error": str(e)}


def unmerge_projects(alias_names: list) -> dict:
    """Atomically restore several aliases as independent projects."""
    if not isinstance(alias_names, list) or not alias_names:
        return {"error": "At least one alias is required."}
    normalized = [(name or "").strip() for name in alias_names]
    if any(not name for name in normalized) or len(set(normalized)) != len(normalized):
        return {"error": "Aliases must be unique, non-empty project names."}
    if not DB_PATH.exists():
        return {"error": "No data."}

    with db_connection() as conn:
        try:
            placeholders = ",".join("?" for _ in normalized)
            found = {
                row[0]
                for row in conn.execute(
                    f"SELECT alias_name FROM project_aliases WHERE alias_name IN ({placeholders})",
                    normalized,
                ).fetchall()
            }
            missing = [name for name in normalized if name not in found]
            if missing:
                return {"error": f"Alias not found: {missing[0]}"}
            conn.execute("BEGIN")
            conn.execute(
                f"DELETE FROM project_aliases WHERE alias_name IN ({placeholders})",
                normalized,
            )
            conn.commit()
            _cache_clear("project_list")
            return {"ok": True, "removed": normalized, "removed_count": len(normalized)}
        except Exception as e:
            conn.rollback()
            return {"error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence access logs

    def _request_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return None
        if length > MAX_REQUEST_SIZE:
            return None
        raw = self.rfile.read(length)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _json(self, payload, status=200, etag=None):
        body = json.dumps(payload).encode()
        accept_encoding = self.headers.get("Accept-Encoding", "")
        use_gzip = "gzip" in accept_encoding.lower() and len(body) > 1024 and status != 304
        if use_gzip:
            body = gzip.compress(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, base_dir: Path, relative: str) -> bool:
        relative = relative.lstrip("/")
        if not relative:
            self._json({"error": "not found"}, status=404)
            return True
        candidate = (base_dir / relative).resolve()
        try:
            candidate.relative_to(base_dir)
        except ValueError:
            self._json({"error": "forbidden"}, status=403)
            return True
        if not candidate.is_file():
            self._json({"error": "not found"}, status=404)
            return True
        ctype = STATIC_CONTENT_TYPES.get(candidate.suffix.lower())
        if not ctype:
            self._json({"error": "forbidden"}, status=403)
            return True
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/static/"):
            self._serve_static(STATIC_DIR, parsed.path[len("/static/"):])
            return
        if parsed.path.startswith("/partials/"):
            self._serve_static(TEMPLATES_DIR, parsed.path[len("/partials/"):])
            return
        if parsed.path == "/api/data":
            query = parse_qs(parsed.query)
            month_value = query.get("month", [""])[0]
            recent_before_value = query.get("recent_before", [""])[0]
            recent_before = None
            if recent_before_value:
                try:
                    recent_before = float(recent_before_value)
                except ValueError:
                    self._json({"error": "recent_before must be a timestamp"}, status=400)
                    return
            etag = _compute_data_etag(month_value)
            if_none_match = self.headers.get("If-None-Match", "")
            if etag and if_none_match == etag and recent_before is None:
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            try:
                self._json(get_stats(month_value, recent_before), etag=etag if recent_before is None else None)
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
        elif parsed.path == "/api/daily-target":
            date_value = parse_qs(parsed.query).get("date", [""])[0]
            try:
                self._json(get_daily_target(date_value))
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
        elif parsed.path in ("/api/weekly-target", "/api/get_weekly_target"):
            date_value = parse_qs(parsed.query).get("date", [""])[0]
            try:
                self._json(get_weekly_target(date_value))
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
        elif parsed.path == "/api/app-settings":
            self._json(get_all_app_settings())
        elif parsed.path == "/api/project-list":
            sort_value = parse_qs(parsed.query).get("sort", [""])[0]
            self._json(get_project_list_recent() if sort_value == "recent" else get_project_list())
        elif parsed.path == "/api/project-aliases":
            project = parse_qs(parsed.query).get("project", [""])[0]
            if not project:
                self._json({"error": "project required"}, status=400)
                return
            self._json(get_project_aliases(project))
        elif parsed.path == "/api/last-session-todos":
            project = parse_qs(parsed.query).get("project", [""])[0]
            self._json(get_last_session_todos(project))
        elif parsed.path == "/api/project-tasks":
            project = parse_qs(parsed.query).get("project", [""])[0]
            result = get_project_tasks_response(project)
            self._json(result, status=200 if result.get("ok") else 400)
        elif parsed.path == "/api/planner-goals":
            result = get_planner_goals_response()
            self._json(result, status=200 if result.get("ok") else 400)
        elif parsed.path == "/api/session-notes-entry":
            query = parse_qs(parsed.query)
            result = get_session_notes_entry(
                query.get("session_id", [""])[0],
                query.get("project", [""])[0],
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif parsed.path == "/api/project-report":
            project = parse_qs(parsed.query).get("project", [""])[0]
            if not project:
                self._json({"error": "project required"}, status=400)
                return
            self._json(get_project_report(project))
        elif parsed.path == "/api/project-report/download":
            project = parse_qs(parsed.query).get("project", [""])[0]
            fmt = parse_qs(parsed.query).get("format", ["text"])[0].lower()
            if not project:
                self._json({"error": "project required"}, status=400)
                return
            report = get_project_report(project)
            if "error" in report:
                self._json(report, status=400)
                return
                
            content = []
            if fmt == "csv":
                import csv
                import io
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["Date", "Start", "End", "Duration", "Notes"])
                for s in report["sessions"]:
                    writer.writerow([s["date"], s["start_time"], s["end_time"], s["duration"], s.get("notes") or ""])
                writer.writerow([])
                writer.writerow(["Total Duration", "", "", report["total_duration"], ""])
                writer.writerow(["Sessions", "", "", str(report["session_count"]), ""])
                content_str = output.getvalue()
                content_type = "text/csv"
                ext = "csv"
            else:
                content.append(f"Project: {report['project_name']}")
                content.append(f"Total Time: {report['total_duration']}")
                content.append(f"Total Sessions: {report['session_count']}")
                content.append("")
                for s in report["sessions"]:
                    content.append(f"{s['date']}  {s['start_time']} - {s['end_time']}  ({s['duration']})")
                    if s["notes"]:
                        content.append(f"  Notes: {s['notes']}")
                content_str = "\n".join(content)
                content_type = "text/plain"
                ext = "txt"
                
            body = content_str.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            safe_name = report['project_name'].replace(' ', '_').replace('"', '_')
            filename = f"{safe_name}_report.{ext}"
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)
        else:
            body = _load_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/clear-recent":
            self._json(clear_all_sessions())
        elif self.path == "/api/clear-unsaved":
            self._json(clear_unsaved_projects())
        elif self.path == "/api/clear-phantoms":
            self._json(clear_phantom_sessions())
        elif self.path == "/api/consolidate-sessions":
            self._json(consolidate_sessions())
        elif self.path == "/api/merge-projects":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = merge_projects(
                payload.get("canonical_name", ""),
                payload.get("aliases", [])
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/unmerge-project":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = unmerge_project(payload.get("alias_name", ""))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/unmerge-projects":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = unmerge_projects(payload.get("aliases", []))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/delete-session":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = delete_sessions(payload.get("session_ids") or [])
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/session-notes":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = set_session_notes(
                payload.get("session_id"),
                payload.get("notes", ""),
                payload.get("todo_notes", ""),
                payload.get("todos") if "todos" in payload else None,
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-category":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = set_project_category(
                payload.get("project_name", ""),
                payload.get("category_key"),
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-folders/save":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            result = save_project_folder(payload or {})
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-folders/members":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            result = set_project_folder_members((payload or {}).get("folder_id"), (payload or {}).get("projects"))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-folders/delete":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            result = delete_project_folder((payload or {}).get("folder_id"))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-folders/reorder":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            result = reorder_project_folder((payload or {}).get("folder_id"), (payload or {}).get("status"), (payload or {}).get("ordered_folders"))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-metadata":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = set_project_metadata(
                payload.get("project_name", ""),
                payload.get("status"),
                payload.get("type"),
                payload.get("priority"),
                payload.get("due_date"),
                payload.get("hard_deadline"),
                payload.get("turn_in_date"),
                payload.get("artist_id"),
                payload.get("progress_percent"),
                payload.get("pinned"),
                payload.get("project_note"),
                payload.get("display_name"),
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-completion":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None or not isinstance(payload.get("completed"), bool):
                self._json({"error": "project_name and boolean completed are required"}, status=400)
                return
            result = set_project_completion(payload.get("project_name", ""), payload["completed"])
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-board/reorder":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = reorder_project_board(
                payload.get("project_name", ""),
                payload.get("status"),
                payload.get("ordered_projects"),
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-board/lane-order":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = save_project_board_lane_order(
                payload.get("ordered_statuses", [])
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/artists":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            
            with db_connection() as conn:
                result = create_artist(
                    conn,
                    payload.get("id", ""),
                    payload.get("name", ""),
                    payload.get("email", ""),
                    payload.get("phone", ""),
                    payload.get("instagram", ""),
                )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/artists/update":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            
            with db_connection() as conn:
                result = update_artist(
                    conn,
                    payload.get("id", ""),
                    payload.get("name", ""),
                    payload.get("email", ""),
                    payload.get("phone", ""),
                    payload.get("instagram", ""),
                )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/artists/delete":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            
            with db_connection() as conn:
                result = delete_artist(conn, payload.get("id", ""))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-tasks":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = create_project_task(
                payload.get("project_name", ""),
                payload.get("title", ""),
                payload.get("priority", "normal"),
                payload.get("due_date", ""),
                payload.get("sort_order", 0),
                payload.get("waiting", False),
                payload.get("quick", False),
                payload.get("label", ""),
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-tasks/update":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = update_project_task(payload.get("id"), payload)
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-tasks/delete":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = delete_project_task(payload.get("id"))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/planner-goals":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = create_planner_goal(
                payload.get("goal_type"),
                payload.get("target_value"),
                payload.get("period", "week"),
                payload.get("scope_type", "all"),
                payload.get("scope_value", ""),
                payload.get("active", True),
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/planner-goals/update":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = update_planner_goal(payload.get("id"), payload)
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/planner-goals/delete":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = delete_planner_goal(payload.get("id"))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/category-options":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = create_category(
                payload.get("label", ""),
                payload.get("color", ""),
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/category-options/update":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = update_category(
                payload.get("key", ""),
                payload.get("label", ""),
                payload.get("color", ""),
            )
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/category-options/delete":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            result = delete_category(payload.get("key", ""))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/daily-target":
            try:
                payload = self._request_json()
                if payload is None:
                    self._json({"error": "request body is required"}, status=400)
                    return
                result = set_daily_target(
                    payload.get("date", ""),
                    payload.get("goal_hours"),
                )
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
                return
            self._json(result)
        elif self.path == "/api/weekly-target":
            try:
                payload = self._request_json()
                if payload is None:
                    self._json({"error": "request body is required"}, status=400)
                    return
                result = set_weekly_target(payload.get("goal_hours"))
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
                return
            self._json(result)
        elif self.path == "/api/app-settings":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            if payload is None:
                self._json({"error": "request body is required"}, status=400)
                return
            key = (payload.get("key") or "").strip()
            if not key:
                self._json({"error": "key is required"}, status=400)
                return
            value = payload.get("value", "")
            set_app_setting(key, str(value))
            self._json({"ok": True, "key": key, "value": value})
        else:
            self._json({"error": "not found"}, status=404)


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        run_schema_migrations(conn)
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Dashboard running at {url}")
    print("Ctrl+C to stop\n")
    if os.environ.get("ABLETON_TRACKER_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
