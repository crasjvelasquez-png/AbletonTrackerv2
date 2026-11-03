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
from projects import PINNED_FOLDERS_KEY, TEMPLATES_KEY, list_templates, normalize_pinned_folders, normalize_templates
from activity_store import ensure_activity_schema, load_activity_rows, read_tracking_status
from tracking_policy import POLICY_TEXT

from tracker import (
    allocate_session_activity,
    build_activity_rollups,
    build_focus_timeline,
    cleanup_phantom_sessions,
    cleanup_untitled_sessions,
    condense_recent_sessions,
    count_phantom_sessions,
    ensure_pauses_table as _ensure_pauses_table,
    format_focus_timeline_markdown,
    get_condense_gap_seconds,
    get_pauses as _tracker_get_pauses,
    is_ableton_running,
    rebuild_pauses_from_sessions as _rebuild_pauses,
    session_end_time,
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
                   active_seconds, notes, todos_json, policy_version
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
            if row['policy_version'] != rows[prev_idx]['policy_version']:
                last_idx_by_project[project] = i
                continue

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

        for absorbed_id, survivor_id in absorb.items():
            while survivor_id in absorb:
                survivor_id = absorb[survivor_id]
            conn.execute('UPDATE activity_intervals SET session_id=? WHERE session_id=?',
                         (survivor_id,absorbed_id))
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
        row = conn.execute(
            "SELECT project_name, todos_json FROM sessions WHERE project_name = ? "
            "ORDER BY start_time DESC, id DESC LIMIT 1",
            (project,),
        ).fetchone()
    if not row:
        return {"project_name": project, "todos": []}
    return {"project_name": row["project_name"], "todos": _parse_todos_json(row["todos_json"])}


def get_session_notes_entry(session_id, project_name: str = "") -> dict:
    raw_session_id = str(session_id or "").strip()
    sid = None
    if raw_session_id:
        try:
            sid = int(raw_session_id)
        except (TypeError, ValueError):
            return {"error": "invalid session_id"}
    if not DB_PATH.exists():
        return {"error": "no data yet"}

    project = (project_name or "").strip()
    if sid is None and not project:
        return {"error": "session_id or project required"}
    with db_connection() as conn:
        ensure_sessions_notes_column(conn)
        conn.row_factory = sqlite3.Row
        if sid is not None:
            current = conn.execute(
                "SELECT project_name FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if not current:
                return {"error": "session not found"}
            if not project:
                project = current["project_name"]
        rows = conn.execute(
            "SELECT id, project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json "
            "FROM sessions WHERE project_name = ? ORDER BY start_time DESC, id DESC",
            (project,),
        ).fetchall()
    if sid is None:
        if not rows:
            return {"error": "session not found for project"}
        sid = int(rows[0]["id"])
    index = next((i for i, row in enumerate(rows) if int(row["id"]) == sid), None)
    if index is None:
        return {"error": "session not found for project"}

    def serialize(row):
        return {
            "id": int(row["id"]), "project_name": row["project_name"],
            "start_time": float(row["start_time"] or 0),
            "last_seen_time": float(row["last_seen_time"] or 0),
            "end_time": float(row["end_time"]) if row["end_time"] is not None else None,
            "active_seconds": float(row["active_seconds"] or 0),
            "notes": (row["notes"] or "").strip(),
            "todos": _parse_todos_json(row["todos_json"]),
        }

    return {
        "ok": True, "session": serialize(rows[index]),
        "previous_session_id": int(rows[index + 1]["id"]) if index + 1 < len(rows) else None,
        "next_session_id": int(rows[index - 1]["id"]) if index > 0 else None,
        "history": [serialize(row) for row in rows],
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
    if key == PINNED_FOLDERS_KEY:
        value = json.dumps(normalize_pinned_folders(value), ensure_ascii=False)
    elif key == TEMPLATES_KEY:
        value = json.dumps(normalize_templates(value), ensure_ascii=False)
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


def ensure_pauses_table(conn: sqlite3.Connection) -> None:
    """Dashboard-side migration for the tracker-owned pauses table."""
    _ensure_pauses_table(conn)


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
        "CREATE INDEX IF NOT EXISTS idx_pauses_start ON pauses(pause_start)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pauses_end ON pauses(pause_end)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_categories_key ON project_categories(category_key)"
    )



def run_schema_migrations(conn: sqlite3.Connection) -> None:
    ensure_category_definitions_table(conn)
    ensure_project_category_table(conn)
    ensure_daily_metrics_table(conn)
    ensure_app_settings_table(conn)
    ensure_pauses_table(conn)
    ensure_sessions_notes_column(conn)
    ensure_activity_schema(conn)
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
    rows = load_activity_rows(conn)
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
    rows = load_activity_rows(conn)
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
    """Lightweight hash of tracker data state — returns empty if no DB."""
    if not DB_PATH.exists():
        return ""
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        sessions = conn.execute(
            "SELECT COUNT(*), MAX(rowid), MAX(COALESCE(last_seen_time, start_time)), "
            "(SELECT COUNT(*) FROM sessions WHERE end_time IS NULL) FROM sessions"
        ).fetchone()
        notes = conn.execute(
            "SELECT COUNT(*), MAX(rowid), GROUP_CONCAT(id || ':' || COALESCE(notes, '') || ':' || "
            "COALESCE(todo_notes, '') || ':' || COALESCE(todos_json, ''), '|') FROM "
            "(SELECT id, rowid, notes, todo_notes, todos_json FROM sessions ORDER BY id)"
        ).fetchone()
        categories = conn.execute("SELECT COUNT(*), MAX(rowid) FROM category_definitions").fetchone()
        assignments = conn.execute("SELECT COUNT(*), MAX(rowid) FROM project_categories").fetchone()
        daily_metrics = conn.execute("SELECT COUNT(*), MAX(rowid) FROM daily_metrics").fetchone()
        settings = conn.execute(
            "SELECT GROUP_CONCAT(key || ':' || value, '|') FROM "
            "(SELECT key, value FROM app_settings ORDER BY key)"
        ).fetchone()
        decision = read_tracking_status(conn,time.time())
        raw = f"{decision}|{sessions}|{notes}|{categories}|{assignments}|{daily_metrics}|{settings}|{month_value}|{(DB_PATH.parent / 'paused').exists()}|{int(time.time() // 30)}"
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
        rows = conn.execute(
            "SELECT DISTINCT project_name FROM sessions WHERE project_name IS NOT NULL "
            "ORDER BY LOWER(project_name) ASC"
        ).fetchall()
        projects = [row["project_name"] for row in rows if row["project_name"]]
        _cache_set(cache_key, projects)
        return projects


def get_project_list_recent() -> list[dict]:
    """Return tracker projects ordered by their most recent session."""
    if not DB_PATH.exists():
        return []
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT project_name, MAX(COALESCE(last_seen_time, start_time, 0)) AS last_active "
            "FROM sessions WHERE project_name IS NOT NULL AND TRIM(project_name) != '' "
            "GROUP BY project_name ORDER BY last_active DESC, LOWER(project_name) ASC"
        ).fetchall()
        return [{"project_name": row["project_name"], "last_active": row["last_active"]} for row in rows]



def get_project_report(project_name: str) -> dict:
    if not DB_PATH.exists():
        return {"error": "No data"}
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, project_name, start_time, last_seen_time, end_time, active_seconds, "
            "notes, todo_notes, todos_json FROM sessions "
            "WHERE project_name = ? AND active_seconds > 0 ORDER BY start_time DESC",
            (project_name,),
        ).fetchall()
        
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
                "active_seconds": float(r["active_seconds"] or 0),
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
            "notes": session_notes_map,
            "todo_notes": session_todo_notes_map,
            "todos": session_todos_map,
            "start_times": session_start_times_map,
            "end_times": session_end_times_map,
            "last_seen_times": session_last_seen_times_map,
        }


def get_pauses(since: float | None = None, until: float | None = None, limit: int = 500, kind: str | None = None) -> list[dict]:
    """Dashboard-side read of recorded pauses (tracker-owned table)."""
    if not DB_PATH.exists():
        return []
    with db_connection() as conn:
        ensure_pauses_table(conn)
        conn.commit()
    return _tracker_get_pauses(since=since, until=until, limit=limit, kind=kind)


def rebuild_pause_history(reason: str = "unknown") -> dict:
    """Backfill pauses from session gaps. Idempotent; safe on real data."""
    if not DB_PATH.exists():
        return {"ok": True, "created": 0, "skipped": 0}
    with db_connection() as conn:
        ensure_pauses_table(conn)
        conn.commit()
    return _rebuild_pauses(reason=reason)


def get_focus_timeline(
    days: int = 7,
    limit: int = 500,
    since: float | None = None,
    until: float | None = None,
) -> dict:
    """LLM-ready work + break timeline for focus analysis (not a UI feature).

    Returns {"events", "timeline_markdown", "session_count", "pause_count",
    "total_pause_seconds", "within_pause_count", "total_within_pause_seconds"}.
    pause_count/total_pause_seconds cover BREAKs between work;
    within_pause_count/total_within_pause_seconds cover PAUSEs inside one
    session. Times are local. Paste timeline_markdown into any LLM with:
    "Here is my Ableton activity with breaks. Where do I lose focus?"
    """
    if not DB_PATH.exists():
        return {
            "events": [],
            "timeline_markdown": (
                "# Ableton focus timeline\n\n_No sessions or pauses recorded yet._\n"
            ),
            "session_count": 0,
            "pause_count": 0,
            "total_pause_seconds": 0.0,
            "within_pause_count": 0,
            "total_within_pause_seconds": 0.0,
        }
    if since is None and days and days > 0:
        since = time.time() - float(days) * 86400
    with db_connection() as conn:
        ensure_pauses_table(conn)
        conn.commit()
    events = build_focus_timeline(since=since, until=until, limit=limit)
    between_seconds = sum(
        float(event.get("duration_seconds") or 0)
        for event in events
        if event.get("kind") == "pause"
        and event.get("pause_kind") != "within"
    )
    within_seconds = sum(
        float(event.get("duration_seconds") or 0)
        for event in events
        if event.get("kind") == "pause"
        and event.get("pause_kind") == "within"
    )
    return {
        "events": events,
        "timeline_markdown": format_focus_timeline_markdown(events),
        "session_count": sum(1 for event in events if event.get("kind") == "session"),
        "pause_count": sum(
            1 for event in events
            if event.get("kind") == "pause"
            and event.get("pause_kind") != "within"
        ),
        "total_pause_seconds": between_seconds,
        "within_pause_count": sum(
            1 for event in events
            if event.get("kind") == "pause"
            and event.get("pause_kind") == "within"
        ),
        "total_within_pause_seconds": within_seconds,
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

            activity_rows = load_activity_rows(conn)
            daily_totals, hourly_totals = build_activity_rollups(activity_rows)

            projects = conn.execute("""
                SELECT project_name,
                       SUM(active_seconds) AS total_seconds,
                       COUNT(*) AS session_count,
                       MIN(start_time) AS first_seen,
                       MAX(COALESCE(end_time, last_seen_time, start_time)) AS last_seen,
                       AVG(active_seconds) AS avg_seconds
                FROM sessions
                WHERE active_seconds > 0
                GROUP BY project_name
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

            today_by_session = {}
            today_project_names = set()
            month_per_project = {}
            previous_month_per_project = {}
            week_per_project = {}
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
                    if goal_week_start.isoformat() <= day_key <= today_str:
                        name = row["project_name"]
                        week_per_project[name] = week_per_project.get(name, 0.0) + seconds
                    if month_start_key <= day_key <= month_end_key:
                        row_month_seconds += seconds
                    if previous_month_start_key <= day_key <= previous_month_end_key:
                        row_previous_month_seconds += seconds
                if row_today_seconds > 0:
                    today_by_session[row["id"]] = today_by_session.get(row["id"],0) + row_today_seconds
                    today_project_names.add(row["project_name"])
                if row_month_seconds > 0:
                    project_name = row["project_name"]
                    month_per_project[project_name] = month_per_project.get(project_name, 0.0) + row_month_seconds
                if row_previous_month_seconds > 0:
                    project_name = row["project_name"]
                    previous_month_per_project[project_name] = (
                        previous_month_per_project.get(project_name, 0.0) + row_previous_month_seconds
                    )

            today_session_seconds = list(today_by_session.values())
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

            # Compare equal, completed weekdays. Today's incomplete day is excluded
            # from both comparison periods, but remains in the current week total.
            completed_days = (today - goal_week_start).days
            comparison_seconds = sum(
                daily_totals.get((goal_week_start + timedelta(days=offset)).isoformat(), 0)
                for offset in range(completed_days)
            )
            previous_comparison_seconds = sum(
                daily_totals.get((goal_week_start + timedelta(days=offset - 7)).isoformat(), 0)
                for offset in range(completed_days)
            )
            week_days = [
                {"day": day_key, "total_seconds": seconds}
                for day_key, seconds in daily_totals.items()
                if goal_week_start.isoformat() <= day_key <= today_str and seconds > 0
            ]
            best_day = min(week_days, key=lambda row: (-row["total_seconds"], row["day"])) if week_days else None
            top_name = min(week_per_project, key=lambda name: (-week_per_project[name], name.casefold())) if week_per_project else None
            weekly_review = {
                "week_start": goal_week_start.isoformat(),
                "week_end": goal_week_end.isoformat(),
                "total_seconds": sum(week_per_project.values()),
                "top_project": {"project_name": top_name, "total_seconds": week_per_project[top_name]} if top_name else None,
                "best_day": best_day,
                "completed_days": completed_days,
                "comparison_seconds": comparison_seconds,
                "previous_comparison_seconds": previous_comparison_seconds,
            }

            now_ts = time.time()

            # Currently active session (end_time IS NULL)
            live = conn.execute("""
                SELECT project_name, start_time, last_seen_time, active_seconds
                FROM sessions
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
                    live_active_seconds,
                    0.0,
                )
                ableton_has_project = bool((live["project_name"] or "").strip())
            policy_status = read_tracking_status(conn,now_ts)
            ableton_running = bool(policy_status.get('running')) if policy_status else (True if ableton_has_project else is_ableton_running())
            tracking_paused = (DB_PATH.parent / "paused").exists()
            tracking_active = bool(live and not tracking_paused and
                (policy_status.get('state')=='tracking' if policy_status else 0<=now_ts-live_last_seen<=10))

            campaign_names = sorted(month_per_project, key=lambda name: (-month_per_project[name], name.casefold()))
            previous_campaign_names = sorted(previous_month_per_project, key=lambda name: (-previous_month_per_project[name], name.casefold()))
            month_ranks = {name: rank for rank, name in enumerate(campaign_names, 1)}
            previous_month_ranks = {name: rank for rank, name in enumerate(previous_campaign_names, 1)}

            project_rows = []
            for row in projects:
                project = dict(row)
                category = project_categories.get(project["project_name"])
                project["category_key"] = category["key"] if category else None
                project["category_label"] = category["label"] if category else None
                project["category_color"] = category["color"] if category else None
                project["display_name"] = project["project_name"]
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
                        "display_name": row["project_name"],
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
                    "live_session_recorded_seconds": max(float(live["active_seconds"] or 0), 0) if live else 0,
                    "live_session_last_seen_time": float(live["last_seen_time"] or 0) if live else None,
                    "weekly_review": weekly_review,
                    "tracking_paused": tracking_paused,
                    "tracking_active": tracking_active,
                    "tracking_status": policy_status,
                    "tracking_policy": POLICY_TEXT,
                    "historical_precision": "Older sessions use estimated hour/day allocation.",
                    "ableton_running": ableton_running,
                    "ableton_has_project": ableton_has_project,
                    "generated_at": now_ts,
                    "closed_session_count": closed_session_count,
                    "unsaved_closed_count": unsaved_closed_count,
                    "phantom_closed_count": phantom_closed_count,
                },
                "projects": project_rows,
                "year_daily": [dict(r) for r in year_daily],
                "year_hourly": [dict(r) for r in year_hourly],
                "recent": recent_rows,
                "recent_has_more": recent_has_more,
                "recent_oldest_start_time": min((row["start_time"] for row in recent_rows), default=None),
                "category_options": category_options,
                "custom_category_limit": MAX_CUSTOM_CATEGORIES,
                "custom_category_count": len(category_options),
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
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # Static files are cached for an hour. Version the local assets so a
        # source update cannot mix a fresh template with stale CSS/JavaScript.
        for asset in ("js/settings.js", "js/dashboard.js", "css/dashboard.css"):
            path = STATIC_DIR / asset
            if path.is_file():
                html = html.replace(f'"/static/{asset}"', f'"/static/{asset}?v={path.stat().st_mtime_ns}"')
        return html
    raise FileNotFoundError(f"Dashboard template missing: {TEMPLATE_PATH}")


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
        elif parsed.path == "/api/project-templates":
            self._json({"available": list_templates()})
        elif parsed.path == "/api/project-list":
            sort_value = parse_qs(parsed.query).get("sort", [""])[0]
            self._json(get_project_list_recent() if sort_value == "recent" else get_project_list())
        elif parsed.path == "/api/last-session-todos":
            project = parse_qs(parsed.query).get("project", [""])[0]
            self._json(get_last_session_todos(project))
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
        elif parsed.path == "/api/focus-timeline":
            query = parse_qs(parsed.query)
            try:
                days = int(query.get("days", ["7"])[0] or 7)
            except ValueError:
                self._json({"error": "days must be an integer"}, status=400)
                return
            try:
                limit = int(query.get("limit", ["500"])[0] or 500)
            except ValueError:
                self._json({"error": "limit must be an integer"}, status=400)
                return
            self._json(get_focus_timeline(days=days, limit=limit))
        elif parsed.path == "/api/focus-timeline/download":
            query = parse_qs(parsed.query)
            try:
                days = int(query.get("days", ["7"])[0] or 7)
            except ValueError:
                self._json({"error": "days must be an integer"}, status=400)
                return
            fmt = query.get("format", ["markdown"])[0].lower()
            result = get_focus_timeline(days=days)
            if fmt == "json":
                content_str = json.dumps(result["events"], indent=2)
                content_type = "application/json"
                ext = "json"
            else:
                content_str = result["timeline_markdown"]
                content_type = "text/markdown"
                ext = "md"
            body = content_str.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="focus_timeline.{ext}"',
            )
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
        elif self.path == "/api/rebuild-pauses":
            self._json(rebuild_pause_history())
        elif self.path == "/api/consolidate-sessions":
            self._json(consolidate_sessions())
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
            try:
                set_app_setting(key, str(value))
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
                return
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
