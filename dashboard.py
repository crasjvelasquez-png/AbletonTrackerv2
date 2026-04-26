#!/usr/bin/env python3
"""Ableton Tracker Dashboard — local web server."""

import sqlite3
import json
import webbrowser
import threading
from datetime import date, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

from tracker import (
    build_activity_rollups,
    cleanup_phantom_sessions,
    condense_recent_sessions,
    count_phantom_sessions,
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
                SELECT start_time, last_seen_time, end_time, active_seconds
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
            goal_week_start = today - timedelta(days=(today.weekday() - 4) % 7)
            goal_week_end   = goal_week_start + timedelta(days=6)
            month_start = today.replace(day=1).isoformat()

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

            project_rows = []
            for row in projects:
                project = dict(row)
                category = project_categories.get(project["project_name"])
                project["category_key"] = category["key"] if category else None
                project["category_label"] = category["label"] if category else None
                project["category_color"] = category["color"] if category else None
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
                    "week_seconds":   week_s,
                    "goal_week_start": goal_week_start.isoformat(),
                    "goal_week_end": goal_week_end.isoformat(),
                    "month_seconds":  month_s,
                    "month_project_count": month_project_count,
                    "project_count":  len(projects),
                    "streak_days":    streak,
                    "live_project":   live["project_name"] if live else None,
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
  --glass-highlight:linear-gradient(180deg, rgba(255,255,255,.28), rgba(255,255,255,0));
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
    --glass-highlight:linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,0));
    --control-bg:rgba(38,41,48,.82);
    --control-bg-strong:rgba(50,54,62,.96);
    --control-inner:rgba(18,20,25,.96);
    --row-border:rgba(235,235,245,.08);
    --bar-bg:rgba(235,235,245,.1);
    --toast-shadow:0 18px 40px rgba(0,0,0,.38);
    --modal-backdrop:rgba(0,0,0,.48);
    --shadow-1:0 1px 2px rgba(0,0,0,.2), 0 18px 42px rgba(0,0,0,.26);
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
  --glass-highlight:linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,0));
  --control-bg:rgba(38,41,48,.82);
  --control-bg-strong:rgba(50,54,62,.96);
  --control-inner:rgba(18,20,25,.96);
  --row-border:rgba(235,235,245,.08);
  --bar-bg:rgba(235,235,245,.1);
  --toast-shadow:0 18px 40px rgba(0,0,0,.38);
  --modal-backdrop:rgba(0,0,0,.48);
  --shadow-1:0 1px 2px rgba(0,0,0,.2), 0 18px 42px rgba(0,0,0,.26);
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
  backdrop-filter:saturate(180%) 
(18px);
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
.intro .sub{
  font-size:13px;color:var(--ink-4);margin-top:12px;
}
.intro .sub span{color:var(--ink-2)}

.cards{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;margin-bottom:28px}
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
  padding:22px 24px 20px;
  position:relative;overflow:hidden;
  transition:border-color .2s ease, background .2s ease, transform .2s ease;
}
.card::after{
  content:"";position:absolute;inset:0;pointer-events:none;
  background:var(--glass-highlight);
}
.card:hover{border-color:var(--border-strong);background:var(--surface-2)}
.card-label{
  font-size:13px;font-weight:600;color:var(--ink-3);margin-bottom:14px;
}
.card-value{
  font-family:var(--font-display);font-weight:680;
  font-size:40px;line-height:1;letter-spacing:-.05em;color:var(--ink);
  font-variant-numeric:tabular-nums;
}
.card-value.accent em{font-style:normal;font-weight:550;color:var(--ink-3)}
.card-value .unit{
  font-family:var(--font-body);font-size:15px;font-weight:500;
  color:var(--ink-4);margin-left:4px;
}
.card-sub{
  font-size:13px;color:var(--ink-4);margin-top:10px;
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
}
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
.goal-shell{display:grid;gap:16px}
.goal-range{
  font-size:12px;color:var(--ink-4);font-weight:600;
}
.goal-ring-wrap{display:grid;place-items:center}
.goal-ring{
  width:min(220px,100%);aspect-ratio:1;border-radius:50%;
  --goal-progress:0deg;
  --goal-progress-mid:0deg;
  --goal-progress-hot:0deg;
  --goal-track:rgba(77,97,126,.14);
  --goal-dial:conic-gradient(from -90deg, #ffe96d 0deg, #ffc246 var(--goal-progress-mid), #ff7a42 var(--goal-progress-hot), #ef3a38 var(--goal-progress), var(--goal-track) var(--goal-progress) 360deg);
  padding:9px;position:relative;
  background:var(--goal-dial);
  box-shadow:0 18px 44px rgba(239,58,56,.13), 0 8px 24px rgba(255,194,70,.1);
  isolation:isolate;
}
.goal-ring::before{
  content:"";position:absolute;inset:-18px;border-radius:inherit;
  background:var(--goal-dial);
  filter:blur(24px);opacity:.34;z-index:-1;
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
  display:grid;gap:10px;
}
.goal-input-row{
  display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;
}
.goal-label{
  display:grid;gap:4px;font-size:12px;color:var(--ink-4);
}
.goal-label strong{
  font-size:13px;color:var(--ink-2);font-weight:700;
}
.goal-input-wrap{
  display:inline-flex;align-items:center;gap:8px;
  padding:10px 12px;border-radius:16px;
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
.target-card .goal-shell{
  grid-template-columns:minmax(126px,150px) minmax(0,1fr);
  align-items:center;gap:12px 16px;
}
.target-card .goal-range{
  grid-column:1 / -1;
}
.target-card .goal-ring{
  width:min(150px,100%);
  padding:9px;
}
.target-card .goal-ring-core{
  padding:13px;
}
.target-card .goal-kicker{
  font-size:10px;
}
.target-card .goal-value{
  font-size:30px;
}
.target-card .goal-value small{
  display:block;
  margin-top:4px;
}
.target-card .goal-status{
  font-size:11px;padding:6px 9px;
}
.target-card .goal-controls{
  gap:9px;
}
.target-card .goal-input-wrap{
  padding:8px 10px;
}
.target-card .goal-input{
  width:56px;
  font-size:21px;
}
.target-card .goal-presets{
  gap:6px;
}
.target-card .goal-chip{
  padding:7px 10px;
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
  min-height:240px;display:grid;grid-template-columns:minmax(160px,220px) 1fr;
  gap:20px;align-items:center;
}
.donut-chart{
  width:min(200px,100%);aspect-ratio:1;border-radius:50%;margin:0 auto;
  position:relative;box-shadow:var(--logo-shadow);
}
.donut-hole{
  position:absolute;inset:22%;border-radius:50%;
  background:var(--control-inner);border:1px solid var(--border);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  text-align:center;padding:10px;
}
.donut-total{
  font-family:var(--font-display);font-size:24px;font-weight:680;
  color:var(--ink);letter-spacing:-.03em;
}
.donut-caption{
  font-size:12px;color:var(--ink-4);margin-top:4px;
}
.donut-legend{display:grid;gap:10px;align-content:center}
.legend-item{
  display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;
  font-size:12px;color:var(--ink-2);
}
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
  .target-card .goal-shell{grid-template-columns:1fr}
  .target-card .goal-ring{width:min(200px,100%)}
}
@media(max-width:640px){
  header{padding:0 20px;height:62px}
  .header-right{gap:10px;flex-wrap:wrap;justify-content:flex-end}
  .header-nav{order:2;width:100%;justify-content:center}
  .page{padding:28px 20px 60px}
  .section-head{flex-wrap:wrap}
  .card-value{font-size:34px}
  .daily-bars{gap:4px}
  .mark{gap:8px;flex-direction:column;align-items:flex-start}
  .heatmap-meta{grid-template-columns:1fr}
  .heatmap-controls{width:100%;justify-content:space-between;justify-self:stretch}
  .heatmap-stage{min-height:316px;padding:16px 12px 16px}
  .heatmap-stage-head,
  .heatmap-stage-values{gap:6px}
  .heatmap-day-head strong,
  .heatmap-day-chip strong{font-size:11px}
  .field-grid{grid-template-columns:1fr}
  .color-control{grid-template-columns:auto 1fr}
  .color-picker-button{grid-column:1 / -1}
  .color-presets{grid-template-columns:repeat(4,minmax(0,1fr))}
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
      <div class="sub" id="pageSubtitle">Live · <span id="introDate"></span></div>
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

function updateHeaderForView() {
  const isSettings = activeView === 'settings';
  document.getElementById('navDashboard').classList.toggle('is-active', !isSettings);
  document.getElementById('navSettings').classList.toggle('is-active', isSettings);
  document.getElementById('pageTitle').innerHTML = isSettings
    ? 'Workspace <em>settings</em>'
    : 'Studio <em>dashboard</em>';
  document.getElementById('pageSubtitle').innerHTML = isSettings
    ? 'Manage categories, colors, and personal organization'
    : 'Live · <span id="introDate"></span>';
  if (!isSettings) {
    document.getElementById('introDate').textContent =
      new Date().toLocaleDateString('en-US', { weekday:'long', month:'long', day:'numeric' });
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

function startOfFridayWeek(base) {
  return addDays(base, -((base.getDay() - 5 + 7) % 7));
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

function render(data) {
  if (data.error) {
    document.getElementById('app').innerHTML =
      `<div class="empty"><p>${escapeHtml(data.error)}</p><small>run start_tracker.command</small></div>`;
    return;
  }
  syncCategoryOptions(data.category_options);
  const { summary, projects, year_daily = [], year_hourly = [], recent } = data;

  if (activeView === 'settings') {
    renderSettings(data);
    return;
  }

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

  document.getElementById('app').innerHTML = `
    <div class="cards">
      <div class="card">
        <div class="card-label">This Month</div>
        <div class="card-value accent">${fmt.dur(summary.month_seconds)}</div>
        <div class="card-sub">${summary.month_project_count} project${summary.month_project_count !== 1 ? 's' : ''} · this month</div>
      </div>
      <div class="card">
        <div class="card-label">Streak</div>
        <div class="card-value">${summary.streak_days}<span class="unit">day${summary.streak_days !== 1 ? 's' : ''}</span></div>
        <div class="card-sub">consecutive</div>
      </div>
    </div>

    <div class="rings-row">
      <div class="chart-card target-card">
        <div class="section-head">
          <h3 class="section-title">Daily <em>target</em></h3>
          <span class="section-meta">Today: <strong>${fmt.dur(summary.today_seconds || 0)}</strong></span>
        </div>
        <div class="chart-wrap"><div id="dailyGoalCard"></div></div>
      </div>
      <div class="chart-card target-card">
        <div class="section-head">
          <h3 class="section-title">Weekly <em>target</em></h3>
          <span class="section-meta">This Week: <strong>${fmt.dur(summary.week_seconds || 0)}</strong></span>
        </div>
        <div class="chart-wrap"><div id="weeklyGoalCard"></div></div>
      </div>
    </div>

    <div class="chart-card chart-card-wide">
      <div class="section-head">
        <h3 class="section-title">Activity <em>calendar</em></h3>
        <span class="section-meta">Friday to Thursday · browse week by week</span>
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
  let cursor = new Date(startOfFridayWeek(start));
  const lastWeekStart = startOfFridayWeek(today);

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
    mount.innerHTML = '<div class="chart-empty">No Friday-to-Thursday weeks to show yet.</div>';
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
      <span>${formatHoursNumber(totalHours)}h in this Friday-to-Thursday window</span>
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
      : 'No activity in this week yet. Use the arrows to browse older Friday-to-Thursday windows.';

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
  presets,
}) {
  const mount = document.getElementById(mountId);
  if (!mount) return;

  const completedHours = Math.round(((completedSeconds || 0) / 3600) * 10) / 10;

  function paint(goalHours) {
    const safeGoalHours = clamp(Math.round(goalHours * 10) / 10, 1, 100);
    const progress = completedHours / safeGoalHours;
    const progressClamped = clamp(progress, 0, 1);
    const progressDegrees = Math.round(progressClamped * 360);
    const progressMidDegrees = Math.round(progressDegrees * 0.48);
    const progressHotDegrees = Math.round(progressDegrees * 0.78);
    const percent = Math.round(progress * 100);
    const percentLabel = `${Math.min(percent, 999)}%`;
    const helperMarkup = helperLabel ? `<span>${helperLabel}</span>` : '';

    mount.innerHTML = `
      <div class="goal-shell">
        <div class="goal-range">${rangeLabel}</div>
        <div class="goal-ring-wrap">
          <div class="goal-ring ${progress >= 1 ? 'is-complete' : ''}" style="--goal-progress:${progressDegrees}deg;--goal-progress-mid:${progressMidDegrees}deg;--goal-progress-hot:${progressHotDegrees}deg" aria-label="${percentLabel} complete">
            <div class="goal-ring-core">
              <div class="goal-value">${percentLabel}</div>
            </div>
          </div>
        </div>
        <div class="goal-controls">
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
    rangeLabel: `${shortRange(summary.goal_week_start, summary.goal_week_end)} · resets Friday`,
    goalLabel: 'Weekly goal',
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

  const entries = [...buckets.values()]
    .filter(entry => entry.total_seconds > 0)
    .sort((a, b) => b.total_seconds - a.total_seconds);

  if (entries.length === 0) {
    mount.innerHTML = '<div class="chart-empty">No category data yet</div>';
    return;
  }

  const totalSeconds = entries.reduce((sum, entry) => sum + entry.total_seconds, 0) || 1;
  let start = 0;
  const segments = entries.map(entry => {
    const share = entry.total_seconds / totalSeconds;
    const end = start + (share * 100);
    const segment = `${entry.color} ${start}% ${end}%`;
    start = end;
    return segment;
  });
  const totalHours = Math.round((totalSeconds / 3600) * 10) / 10;

  mount.innerHTML = `
    <div class="donut-layout">
      <div class="donut-chart" style="background:conic-gradient(${segments.join(', ')})">
        <div class="donut-hole">
          <div class="donut-total">${totalHours}h</div>
          <div class="donut-caption">All categories</div>
        </div>
      </div>
      <div class="donut-legend">
        ${entries.map(entry => {
          const hours = Math.round((entry.total_seconds / 3600) * 10) / 10;
          return `
            <div class="legend-item" title="${escapeHtml(entry.label)}">
              <span class="legend-swatch" style="background:${entry.color}"></span>
              <span class="legend-name">${escapeHtml(entry.label)}</span>
              <span class="legend-value">${hours}h</span>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;
}

load();
setInterval(load, 60_000);
</script>
</body>
</html>"""


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
        if self.path.startswith("/api/data"):
            self._json(get_stats())
        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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
