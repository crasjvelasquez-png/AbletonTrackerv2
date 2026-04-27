#!/usr/bin/env python3
"""Ableton Tracker Dashboard — local web server."""

import os
import sqlite3
import json
import webbrowser
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from tracker import (
    allocate_session_activity,
    build_activity_rollups,
    cleanup_phantom_sessions,
    condense_recent_sessions,
    count_phantom_sessions,
    is_ableton_running,
)

DB_PATH = Path.home() / ".ableton_tracker" / "sessions.db"
PORT = 7421
UNTITLED_NAMES = {"untitled", "untitled project"}
MAX_CUSTOM_CATEGORIES = 12
LEGACY_CATEGORY_KEYS = [
    "c4milo",
    "production",
    "mixing",
    "mastering",
    "instrumentation",
]


# ─────────────────────────────────────────────────────────────
#  Mutations
# ─────────────────────────────────────────────────────────────

def clear_all_sessions() -> dict:
    if not DB_PATH.exists():
        return {"ok": True, "deleted": 0}
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM sessions WHERE end_time IS NOT NULL")
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount}


def clear_unsaved_projects() -> dict:
    if not DB_PATH.exists():
        return {"ok": True, "deleted": 0}
    placeholders = ",".join("?" * len(UNTITLED_NAMES))
    with sqlite3.connect(DB_PATH) as conn:
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


def clear_phantom_sessions() -> dict:
    return cleanup_phantom_sessions()


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
    with sqlite3.connect(DB_PATH) as conn:
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


def purge_legacy_categories(conn: sqlite3.Connection) -> None:
    ensure_category_definitions_table(conn)
    ensure_project_category_table(conn)
    placeholders = ",".join("?" * len(LEGACY_CATEGORY_KEYS))
    conn.execute(
        f"DELETE FROM project_categories WHERE category_key IN ({placeholders})",
        LEGACY_CATEGORY_KEYS,
    )
    conn.execute(
        f"DELETE FROM category_definitions WHERE key IN ({placeholders})",
        LEGACY_CATEGORY_KEYS,
    )


def get_category_options(conn: sqlite3.Connection) -> list[dict]:
    purge_legacy_categories(conn)
    ensure_project_category_table(conn)
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
    ensure_project_category_table(conn)
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

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        ensure_project_category_table(conn)
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

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        purge_legacy_categories(conn)
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

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        purge_legacy_categories(conn)
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

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        purge_legacy_categories(conn)
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


def get_friday_week_range(target_date: date) -> tuple[date, date]:
    week_start = target_date - timedelta(days=(target_date.weekday() - 4) % 7)
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
    start_key = start_date.isoformat()
    end_key = end_date.isoformat()
    total_seconds = 0.0
    for row in rows:
        end_time = row["end_time"] or row["last_seen_time"] or row["start_time"]
        for day_key, _hour, seconds in allocate_session_activity(
            row["start_time"],
            end_time,
            row["active_seconds"],
        ):
            if start_key <= day_key <= end_key:
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

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        ensure_daily_metrics_table(conn)
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
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        ensure_daily_metrics_table(conn)
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
    return get_daily_target(target_date.isoformat())


def get_weekly_target(date_value: str = "") -> dict:
    target_date = parse_iso_date(date_value) if date_value else date.today()
    week_start, week_end = get_friday_week_range(target_date)
    start_key = week_start.isoformat()
    end_key = week_end.isoformat()
    reset_at = datetime.combine(week_end + timedelta(days=1), datetime.min.time())
    seconds_until_reset = max(0, int(round(reset_at.timestamp() - datetime.now().timestamp())))

    base = {
        "week_start": start_key,
        "week_end": end_key,
        "weekly_start_date": start_key,
        "weekly_end_date": end_key,
        "reset_at": reset_at.isoformat(),
        "seconds_until_reset": seconds_until_reset,
        "goal_hours": 0,
        "progress_seconds": 0,
        "has_target": False,
    }

    if not DB_PATH.exists():
        return base

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return {
            **base,
            "progress_seconds": get_range_progress_seconds(conn, week_start, week_end),
        }

# ─────────────────────────────────────────────────────────────
#  Data layer
# ─────────────────────────────────────────────────────────────

def get_stats() -> dict:
    if not DB_PATH.exists():
        return {"error": "No data yet — start the tracker first."}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            category_options, _ = get_category_maps(conn)
            project_categories = get_project_categories(conn)

            activity_rows = conn.execute("""
                SELECT project_name, start_time, last_seen_time, end_time, active_seconds
                FROM sessions
                WHERE active_seconds > 0
            """).fetchall()
            daily_totals, hourly_totals = build_activity_rollups(activity_rows)

            projects = conn.execute("""
                SELECT project_name,
                       SUM(active_seconds)  AS total_seconds,
                       COUNT(*)             AS session_count,
                       MIN(start_time)      AS first_seen,
                       MAX(COALESCE(end_time, last_seen_time, start_time)) AS last_seen,
                       AVG(active_seconds)  AS avg_seconds
                FROM   sessions
                WHERE  active_seconds > 0
                GROUP  BY project_name
                ORDER  BY total_seconds DESC
            """).fetchall()

            today = date.today()
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

            recent = conn.execute("""
                SELECT id, project_name, start_time, last_seen_time, end_time, active_seconds
                FROM   sessions
                WHERE  active_seconds >= 5 OR end_time IS NULL
                ORDER  BY start_time DESC
                LIMIT  240
            """).fetchall()
            recent = condense_recent_sessions(recent)[:60]

            today_str   = today.isoformat()
            goal_week_start, goal_week_end = get_friday_week_range(today)
            month_start = today.replace(day=1).isoformat()

            today_session_seconds = []
            today_project_names = set()
            for row in activity_rows:
                row_today_seconds = 0.0
                end_time = row["end_time"] or row["last_seen_time"] or row["start_time"]
                for day_key, _hour, seconds in allocate_session_activity(
                    row["start_time"],
                    end_time,
                    row["active_seconds"],
                ):
                    if day_key == today_str:
                        row_today_seconds += seconds
                if row_today_seconds > 0:
                    today_session_seconds.append(row_today_seconds)
                    today_project_names.add(row["project_name"])

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
            month_s  = sum(
                seconds
                for day_key, seconds in daily_totals.items()
                if day_key >= month_start
            )
            month_project_count = scalar(
                """
                SELECT COUNT(DISTINCT project_name)
                FROM sessions
                WHERE active_seconds > 0
                  AND date(start_time,'unixepoch','localtime') >= ?
                """,
                month_start,
            )
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

            # Currently active session (end_time IS NULL)
            live = conn.execute("""
                SELECT project_name FROM sessions
                WHERE end_time IS NULL ORDER BY start_time DESC LIMIT 1
            """).fetchone()

            month_per_project = dict(conn.execute(
                """
                SELECT project_name, COALESCE(SUM(active_seconds), 0) AS month_seconds
                FROM sessions
                WHERE active_seconds > 0
                  AND date(start_time, 'unixepoch', 'localtime') >= ?
                GROUP BY project_name
                """,
                (month_start,),
            ).fetchall())

            project_rows = []
            for row in projects:
                project = dict(row)
                category = project_categories.get(project["project_name"])
                project["category_key"] = category["key"] if category else None
                project["category_label"] = category["label"] if category else None
                project["category_color"] = category["color"] if category else None
                project["month_seconds"] = month_per_project.get(project["project_name"], 0)
                project_rows.append(project)

            recent_rows = []
            for row in recent:
                category = project_categories.get(row["project_name"])
                recent_rows.append(
                    {
                        "project_name": row["project_name"],
                        "start_time": row["start_time"],
                        "end_time": row["end_time"],
                        "active_seconds": row["active_seconds"],
                        "session_ids": row.get("session_ids", []),
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
                    "month_seconds":  month_s,
                    "month_project_count": month_project_count,
                    "project_count":  len(projects),
                    "streak_days":    streak,
                    "live_project":   live["project_name"] if live else None,
                    "ableton_running": is_ableton_running(),
                    "closed_session_count": closed_session_count,
                    "unsaved_closed_count": unsaved_closed_count,
                    "phantom_closed_count": phantom_closed_count,
                },
                "projects": project_rows,
                "year_daily": [dict(r) for r in year_daily],
                "year_hourly": [dict(r) for r in year_hourly],
                "recent": recent_rows,
                "category_options": category_options,
                "custom_category_limit": MAX_CUSTOM_CATEGORIES,
                "custom_category_count": len(category_options),
            }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
#  HTML template
# ─────────────────────────────────────────────────────────────

TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
_HTML_CACHE: str | None = None


def _load_html() -> str:
    global _HTML_CACHE
    if os.environ.get("ABLETON_TRACKER_DEV") == "1":
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    if _HTML_CACHE is None:
        _HTML_CACHE = TEMPLATE_PATH.read_text(encoding="utf-8")
    return _HTML_CACHE


# ─────────────────────────────────────────────────────────────
#  HTTP server
# ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence access logs

    def _request_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            self._json(get_stats())
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
        elif self.path == "/api/delete-session":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
                return
            result = delete_sessions(payload.get("session_ids") or [])
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/project-category":
            try:
                payload = self._request_json()
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, status=400)
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
            result = delete_category(payload.get("key", ""))
            self._json(result, status=200 if result.get("ok") else 400)
        elif self.path == "/api/daily-target":
            try:
                payload = self._request_json()
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
        else:
            self._json({"error": "not found"}, status=404)


def main():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Dashboard running at {url}")
    print("Ctrl+C to stop\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
