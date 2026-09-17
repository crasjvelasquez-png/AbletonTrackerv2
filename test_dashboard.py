import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from unittest.mock import patch

import dashboard
import tracker


class DashboardCategoryTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_set_project_category_persists_and_is_returned_by_stats(self):
        created = dashboard.create_category("Production", "#00a6ff")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Real Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        result = dashboard.set_project_category("Real Song", created["category"]["key"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["category"]["label"], "Production")

        stats = dashboard.get_stats()
        self.assertEqual(stats["projects"][0]["category_key"], created["category"]["key"])
        self.assertEqual(stats["projects"][0]["category_label"], "Production")
        self.assertEqual(stats["projects"][0]["category_color"], "#00A6FF")
        self.assertEqual(stats["recent"][0]["category_key"], created["category"]["key"])

    def test_set_project_category_none_clears_existing_assignment(self):
        created = dashboard.create_category("Mixing", "#8b5a2b")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Real Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        dashboard.set_project_category("Real Song", created["category"]["key"])
        cleared = dashboard.set_project_category("Real Song", None)

        self.assertTrue(cleared["ok"])
        stats = dashboard.get_stats()
        self.assertIsNone(stats["projects"][0]["category_key"])
        self.assertIsNone(stats["recent"][0]["category_key"])

    def test_create_category_adds_custom_option_and_allows_assignment(self):
        created = dashboard.create_category("Sound Design", "#7c5cff")

        self.assertTrue(created["ok"])
        self.assertEqual(created["category"]["label"], "Sound Design")
        self.assertEqual(created["category"]["color"], "#7C5CFF")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Custom Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        assigned = dashboard.set_project_category("Custom Song", created["category"]["key"])
        self.assertTrue(assigned["ok"])

        stats = dashboard.get_stats()
        option = next(
            item for item in stats["category_options"]
            if item["key"] == created["category"]["key"]
        )
        self.assertEqual(option["label"], created["category"]["label"])
        self.assertEqual(option["color"], created["category"]["color"])
        self.assertEqual(option["assignment_count"], 1)
        self.assertEqual(stats["custom_category_count"], 1)
        self.assertEqual(stats["projects"][0]["category_label"], "Sound Design")
        self.assertEqual(stats["projects"][0]["category_color"], "#7C5CFF")

    def test_update_category_changes_name_and_color(self):
        created = dashboard.create_category("Sound Design", "#7c5cff")

        updated = dashboard.update_category(
            created["category"]["key"],
            "Vocal Production",
            "#11aa88",
        )

        self.assertTrue(updated["ok"])
        self.assertEqual(updated["category"]["label"], "Vocal Production")
        self.assertEqual(updated["category"]["color"], "#11AA88")

        stats = dashboard.get_stats()
        option = next(
            item for item in stats["category_options"]
            if item["key"] == created["category"]["key"]
        )
        self.assertEqual(option["label"], "Vocal Production")
        self.assertEqual(option["color"], "#11AA88")

    def test_delete_category_clears_project_assignments(self):
        created = dashboard.create_category("Sound Design", "#7c5cff")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Custom Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        dashboard.set_project_category("Custom Song", created["category"]["key"])
        deleted = dashboard.delete_category(created["category"]["key"])

        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["cleared_assignments"], 1)

        stats = dashboard.get_stats()
        self.assertFalse(
            any(item["key"] == created["category"]["key"] for item in stats["category_options"])
        )
        self.assertIsNone(stats["projects"][0]["category_key"])

    def test_legacy_seeded_categories_are_purged(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.ensure_category_definitions_table(conn)
            dashboard.ensure_project_category_table(conn)
            conn.execute(
                """
                INSERT INTO category_definitions (key, label, color, updated_at)
                VALUES (?, ?, ?, 0)
                """,
                ("production", "Production", "#00A6FF"),
            )
            conn.execute(
                """
                INSERT INTO project_categories (project_name, category_key, updated_at)
                VALUES (?, ?, 0)
                """,
                ("Legacy Song", "production"),
            )
            conn.commit()
            dashboard.purge_legacy_categories(conn)

        stats = dashboard.get_stats()
        self.assertFalse(any(item["key"] == "production" for item in stats["category_options"]))
        self.assertEqual(stats["custom_category_count"], 0)

    def test_category_table_migration_removes_is_default_column(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute("DROP TABLE IF EXISTS category_definitions")
            conn.execute(
                """
                CREATE TABLE category_definitions (
                    key         TEXT PRIMARY KEY,
                    label       TEXT NOT NULL,
                    color       TEXT NOT NULL,
                    is_default  INTEGER NOT NULL DEFAULT 0,
                    updated_at  INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO category_definitions (key, label, color, is_default, updated_at)
                VALUES (?, ?, ?, ?, 0)
                """,
                ("custom-mixing", "Mixing", "#11AA88", 0),
            )
            conn.commit()

            dashboard.ensure_category_definitions_table(conn)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(category_definitions)").fetchall()]
            row = conn.execute(
                "SELECT key, label, color, updated_at FROM category_definitions WHERE key = ?",
                ("custom-mixing",),
            ).fetchone()

        self.assertEqual(columns, ["key", "label", "color", "updated_at"])
        self.assertEqual(tuple(row), ("custom-mixing", "Mixing", "#11AA88", 0))






class DashboardWeeklyTargetTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_friday_week_range_uses_previous_friday_through_thursday(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 24), week_start_weekday=4),
            (date(2026, 4, 24), date(2026, 4, 30)),
        )
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27), week_start_weekday=4),
            (date(2026, 4, 24), date(2026, 4, 30)),
        )
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 30), week_start_weekday=4),
            (date(2026, 4, 24), date(2026, 4, 30)),
        )
        self.assertEqual(
            dashboard.get_week_range(date(2026, 5, 1), week_start_weekday=4),
            (date(2026, 5, 1), date(2026, 5, 7)),
        )

    def test_weekly_target_aggregates_friday_to_thursday_progress(self):
        friday_start = datetime(2026, 4, 24, 10, 0).timestamp()
        friday_end = datetime(2026, 4, 24, 12, 0).timestamp()
        thursday_start = datetime(2026, 4, 30, 13, 0).timestamp()
        thursday_end = datetime(2026, 4, 30, 14, 0).timestamp()
        next_friday_start = datetime(2026, 5, 1, 10, 0).timestamp()
        next_friday_end = datetime(2026, 5, 1, 12, 0).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("Friday", friday_start, friday_end, friday_end, 7200.0),
                    ("Thursday", thursday_start, thursday_end, thursday_end, 3600.0),
                    ("Next Friday", next_friday_start, next_friday_end, next_friday_end, 7200.0),
                ],
            )
            conn.commit()

        target = dashboard.get_weekly_target("2026-04-27")

        self.assertEqual(target["week_start"], "2026-04-24")
        self.assertEqual(target["week_end"], "2026-04-30")
        self.assertEqual(target["weekly_start_date"], "2026-04-24")
        self.assertEqual(target["weekly_end_date"], "2026-04-30")
        self.assertEqual(target["progress_seconds"], 10800)
        self.assertEqual(target["reset_at"], "2026-05-01T00:00:00")
        self.assertGreaterEqual(target["seconds_until_reset"], 0)

    def test_weekly_target_is_independent_from_daily_goals(self):
        friday_start = datetime(2026, 4, 24, 10, 0).timestamp()
        friday_end = datetime(2026, 4, 24, 12, 0).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Friday", friday_start, friday_end, friday_end, 7200.0),
            )
            conn.commit()

        baseline = dashboard.get_weekly_target("2026-04-27")

        dashboard.set_daily_target("2026-04-24", 2.5)
        dashboard.set_daily_target("2026-04-30", 3.5)
        dashboard.set_daily_target("2026-04-27", 8.0)

        after_daily_changes = dashboard.get_weekly_target("2026-04-27")

        self.assertEqual(after_daily_changes["progress_seconds"], baseline["progress_seconds"])
        self.assertEqual(after_daily_changes["goal_hours"], baseline["goal_hours"])
        self.assertFalse(after_daily_changes["has_target"])
        self.assertNotIn("goal_day_count", after_daily_changes)

    # ── configurable week start ───────────────────────────────────

    def test_week_range_defaults_to_friday(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27)),
            (date(2026, 4, 24), date(2026, 4, 30)),
        )

    def test_week_range_monday_start(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27), week_start_weekday=0),
            (date(2026, 4, 27), date(2026, 5, 3)),
        )

    def test_week_range_sunday_start(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27), week_start_weekday=6),
            (date(2026, 4, 26), date(2026, 5, 2)),
        )

    def test_week_range_saturday_start(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27), week_start_weekday=5),
            (date(2026, 4, 25), date(2026, 5, 1)),
        )

    def test_app_settings_set_and_get(self):
        dashboard.set_app_setting("week_start_weekday", "2")
        self.assertEqual(dashboard.get_app_setting("week_start_weekday"), "2")

    def test_app_settings_default_when_missing(self):
        self.assertEqual(dashboard.get_app_setting("nonexistent", "pancakes"), "pancakes")

    def test_app_settings_none_default(self):
        self.assertIsNone(dashboard.get_app_setting("never_set"))

    def test_get_all_app_settings_returns_dict(self):
        dashboard.set_app_setting("a", "1")
        dashboard.set_app_setting("b", "2")
        settings = dashboard.get_all_app_settings()
        self.assertEqual(settings.get("a"), "1")
        self.assertEqual(settings.get("b"), "2")

    def test_session_notes_migration_adds_todo_notes_column(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
        self.assertIn("notes", columns)
        self.assertIn("todo_notes", columns)

    def test_set_session_notes_saves_worked_on_and_todos(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Notes Project", 100.0, 200.0, 200.0, 100.0),
            )
            session_id = cur.lastrowid
            conn.commit()

        result = dashboard.set_session_notes(session_id, "Built drums", "Bounce stems")

        self.assertTrue(result["ok"])
        self.assertEqual(result["notes"], "Built drums")
        self.assertEqual(result["todo_notes"], "Bounce stems")
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT notes, todo_notes FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "Built drums")
        self.assertEqual(row[1], "Bounce stems")

    def test_clear_session_notes_preserves_sessions(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todo_notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Notes Project",
                    100.0,
                    200.0,
                    200.0,
                    100.0,
                    "Built drums",
                    "Bounce stems",
                    '[{"text":"Bounce stems","done":false}]',
                ),
            )
            session_id = cur.lastrowid
            conn.commit()

        result = dashboard.clear_session_notes()

        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 1)
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT project_name, active_seconds, notes, todo_notes, todos_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "Notes Project")
        self.assertEqual(row[1], 100.0)
        self.assertEqual(row[2], "")
        self.assertEqual(row[3], "")
        self.assertEqual(row[4], "[]")

    def test_recent_payload_includes_session_todo_notes(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todo_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Notes Project", 100.0, 200.0, 200.0, 100.0, "Built drums", "Bounce stems"),
            )
            session_id = cur.lastrowid
            conn.commit()

        stats = dashboard.get_stats()
        recent = stats["recent"][0]

        self.assertEqual(recent["session_notes"][str(session_id)], "Built drums")
        self.assertEqual(recent["session_todo_notes"][str(session_id)], "Bounce stems")

    def test_todos_json_migration_adds_todos_json_column(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
        self.assertIn("todos_json", columns)

    def test_set_session_notes_saves_structured_todos(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Notes Project", 100.0, 200.0, 200.0, 100.0),
            )
            session_id = cur.lastrowid
            conn.commit()

        result = dashboard.set_session_notes(
            session_id, "Built drums", "",
            [{"text": "Bounce stems", "done": False}, {"text": "Export mix", "done": True}]
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["todo_notes"], "Bounce stems | Export mix")
        self.assertEqual(len(result["todos"]), 2)
        self.assertEqual(result["todos"][0]["text"], "Bounce stems")
        self.assertFalse(result["todos"][0]["done"])
        self.assertTrue(result["todos"][1]["done"])
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT notes, todo_notes, todos_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "Built drums")
        self.assertEqual(row[1], "Bounce stems | Export mix")
        self.assertIn("Bounce stems", row[2])

    def test_get_last_session_todos_returns_todos_for_project(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 100.0, 200.0, 200.0, 100.0,
                 '[{"text":"Fix bass","done":false}]'),
            )
            conn.commit()

        result = dashboard.get_last_session_todos("Alpha")
        self.assertEqual(len(result["todos"]), 1)
        self.assertEqual(result["todos"][0]["text"], "Fix bass")
        self.assertEqual(result["project_name"], "Alpha")

    def test_get_last_session_todos_empty_when_none(self):
        result = dashboard.get_last_session_todos("Nonexistent")
        self.assertEqual(result["todos"], [])

    def test_get_last_session_todos_filters_by_project(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 100.0, 200.0, 200.0, 100.0,
                 '[{"text":"Alpha task","done":false}]'),
            )
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Beta", 150.0, 250.0, 250.0, 100.0,
                 '[{"text":"Beta task","done":false}]'),
            )
            conn.commit()

        alpha = dashboard.get_last_session_todos("Alpha")
        beta = dashboard.get_last_session_todos("Beta")
        self.assertEqual(alpha["todos"][0]["text"], "Alpha task")
        self.assertEqual(beta["todos"][0]["text"], "Beta task")

    def test_get_session_notes_entry_returns_project_neighbors(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            first = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 100.0, 160.0, 160.0, 60.0, "First", '[{"text":"First task","done":false}]'),
            ).lastrowid
            second = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 200.0, 260.0, 260.0, 60.0, "Second", "[]"),
            ).lastrowid
            third = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 300.0, 360.0, 360.0, 60.0, "Third", "[]"),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Beta", 400.0, 460.0, 460.0, 60.0, "Beta", "[]"),
            )
            conn.commit()

        result = dashboard.get_session_notes_entry(second, "Alpha")

        self.assertTrue(result["ok"])
        self.assertEqual(result["session"]["id"], second)
        self.assertEqual(result["session"]["notes"], "Second")
        self.assertEqual(result["previous_session_id"], first)
        self.assertEqual(result["next_session_id"], third)
        self.assertEqual([entry["id"] for entry in result["history"]], [third, second, first])

        latest = dashboard.get_session_notes_entry("", "Alpha")
        self.assertTrue(latest["ok"])
        self.assertEqual(latest["session"]["id"], third)
        self.assertEqual([entry["id"] for entry in latest["history"]], [third, second, first])

    def test_recent_payload_includes_session_todos(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Notes Project", 100.0, 200.0, 200.0, 100.0,
                 '[{"text":"Mix drums","done":true}]'),
            )
            session_id = cur.lastrowid
            conn.commit()

        stats = dashboard.get_stats()
        recent = stats["recent"][0]

        self.assertIn(str(session_id), recent["session_todos"])
        self.assertEqual(recent["session_todos"][str(session_id)][0]["text"], "Mix drums")
        self.assertTrue(recent["session_todos"][str(session_id)][0]["done"])

    def test_weekly_target_includes_week_start_weekday(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Test", datetime(2026, 4, 24, 10, 0).timestamp(),
                 datetime(2026, 4, 24, 12, 0).timestamp(),
                 datetime(2026, 4, 24, 12, 0).timestamp(), 7200.0),
            )
            conn.commit()

        target = dashboard.get_weekly_target("2026-04-27")
        self.assertEqual(target["week_start_weekday"], 4)
        self.assertEqual(target["week_start_weekday_name"], "Friday")

    def test_weekly_target_respects_custom_week_start(self):
        dashboard.set_app_setting("week_start_weekday", "0")  # Monday
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Test", datetime(2026, 4, 27, 10, 0).timestamp(),
                 datetime(2026, 4, 27, 12, 0).timestamp(),
                 datetime(2026, 4, 27, 12, 0).timestamp(), 7200.0),
            )
            conn.commit()

        target = dashboard.get_weekly_target("2026-04-27")
        self.assertEqual(target["week_start"], "2026-04-27")
        self.assertEqual(target["week_end"], "2026-05-03")
        self.assertEqual(target["week_start_weekday"], 0)
        self.assertEqual(target["week_start_weekday_name"], "Monday")


class DashboardRolloverTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_cross_midnight_session_counts_toward_new_day_and_hour(self):
        start_ts = datetime(2026, 4, 24, 23, 59).timestamp()
        last_seen_ts = datetime(2026, 4, 25, 0, 3).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Late Session", start_ts, last_seen_ts, None, 240.0),
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 25)

        with patch.object(dashboard, "date", FrozenDate):
            stats = dashboard.get_stats()

        self.assertEqual(stats["summary"]["today_seconds"], 180.0)
        self.assertEqual(stats["summary"]["week_seconds"], 240.0)

        daily = {row["day"]: row["total_seconds"] for row in stats["year_daily"]}
        self.assertEqual(daily["2026-04-24"], 60.0)
        self.assertEqual(daily["2026-04-25"], 180.0)

        hourly = {
            (row["day"], row["hour"]): row["active_seconds"]
            for row in stats["year_hourly"]
        }
        self.assertEqual(hourly[("2026-04-24", 23)], 60.0)
        self.assertEqual(hourly[("2026-04-25", 0)], 180.0)
        self.assertEqual(stats["summary"]["streak_days"], 2)

    def test_streak_does_not_reset_right_after_midnight_without_activity_yet(self):
        session_1_start_ts = datetime(2026, 4, 23, 12, 0).timestamp()
        session_1_end_ts = datetime(2026, 4, 23, 12, 10).timestamp()
        session_2_start_ts = datetime(2026, 4, 24, 12, 0).timestamp()
        session_2_end_ts = datetime(2026, 4, 24, 12, 10).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Day 1", session_1_start_ts, session_1_end_ts, session_1_end_ts, 600.0),
            )
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Day 2", session_2_start_ts, session_2_end_ts, session_2_end_ts, 600.0),
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 25)

        with patch.object(dashboard, "date", FrozenDate):
            stats = dashboard.get_stats()

        self.assertEqual(stats["summary"]["streak_days"], 2)

    def test_production_run_requires_fifteen_minutes_and_keeps_midnight_grace(self):
        today = date(2026, 4, 25)
        result = dashboard.calculate_production_run(
            {
                "2026-04-21": 900.0,
                "2026-04-22": 1200.0,
                "2026-04-23": 899.0,
                "2026-04-24": 900.0,
                "2026-04-25": 450.0,
            },
            today,
        )

        self.assertEqual(result["current_days"], 1)
        self.assertEqual(result["best_days"], 2)
        self.assertEqual(result["qualifying_seconds"], 900)
        self.assertEqual(result["today_seconds"], 450.0)
        self.assertFalse(result["today_qualifies"])
        self.assertEqual(result["today_progress_ratio"], 0.5)
        self.assertEqual(result["remaining_seconds"], 450.0)
        self.assertEqual(len(result["recent_days"]), 7)
        self.assertEqual(result["recent_days"][-1]["state"], "partial")

    def test_production_run_exact_threshold_advances_and_sets_record(self):
        result = dashboard.calculate_production_run(
            {
                "2026-04-23": 900.0,
                "2026-04-24": 901.0,
                "2026-04-25": 900.0,
            },
            date(2026, 4, 25),
        )

        self.assertEqual(result["current_days"], 3)
        self.assertEqual(result["best_days"], 3)
        self.assertTrue(result["today_qualifies"])
        self.assertEqual(result["today_progress_ratio"], 1.0)
        self.assertEqual(result["remaining_seconds"], 0.0)

    def test_production_run_empty_and_broken_history_are_neutral(self):
        empty = dashboard.calculate_production_run({}, date(2026, 4, 25))
        broken = dashboard.calculate_production_run(
            {"2026-04-20": 900.0, "2026-04-22": 900.0, "2026-04-26": 900.0},
            date(2026, 4, 25),
        )

        self.assertEqual(empty["current_days"], 0)
        self.assertEqual(empty["best_days"], 0)
        self.assertEqual(empty["remaining_seconds"], 900.0)
        self.assertEqual(broken["current_days"], 0)
        self.assertEqual(broken["best_days"], 1)

    def test_cross_midnight_fragments_qualify_independently_for_production_run(self):
        start_ts = datetime(2026, 4, 24, 23, 40).timestamp()
        end_ts = datetime(2026, 4, 25, 0, 20).timestamp()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Boundary", start_ts, end_ts, end_ts, 1800.0),
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 25)

        with patch.object(dashboard, "date", FrozenDate):
            run = dashboard.get_stats()["summary"]["production_run"]

        self.assertEqual(run["today_seconds"], 900.0)
        self.assertTrue(run["today_qualifies"])
        self.assertEqual(run["current_days"], 2)

    def test_today_reflection_stats_use_only_todays_allocated_time(self):
        late_start_ts = datetime(2026, 4, 24, 23, 50).timestamp()
        late_end_ts = datetime(2026, 4, 25, 0, 10).timestamp()
        today_1_start_ts = datetime(2026, 4, 25, 10, 0).timestamp()
        today_1_end_ts = datetime(2026, 4, 25, 10, 20).timestamp()
        today_2_start_ts = datetime(2026, 4, 25, 13, 0).timestamp()
        today_2_end_ts = datetime(2026, 4, 25, 13, 15).timestamp()
        yesterday_start_ts = datetime(2026, 4, 24, 14, 0).timestamp()
        yesterday_end_ts = datetime(2026, 4, 24, 14, 30).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("Song A", late_start_ts, late_end_ts, late_end_ts, 1200.0),
                    ("Song A", today_1_start_ts, today_1_end_ts, today_1_end_ts, 1200.0),
                    ("Song B", today_2_start_ts, today_2_end_ts, today_2_end_ts, 900.0),
                    ("Yesterday", yesterday_start_ts, yesterday_end_ts, yesterday_end_ts, 1800.0),
                ],
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 25)

        with patch.object(dashboard, "date", FrozenDate):
            stats = dashboard.get_stats()

        self.assertEqual(stats["summary"]["today_seconds"], 2700.0)
        self.assertEqual(stats["summary"]["today_session_count"], 3)
        self.assertEqual(stats["summary"]["today_project_count"], 2)
        self.assertEqual(stats["summary"]["today_average_session_seconds"], 900.0)

    def test_selected_month_uses_allocated_time_within_that_month(self):
        april_start_ts = datetime(2026, 4, 15, 10, 0).timestamp()
        april_end_ts = datetime(2026, 4, 15, 12, 0).timestamp()
        crossing_start_ts = datetime(2026, 4, 30, 23, 30).timestamp()
        crossing_end_ts = datetime(2026, 5, 1, 0, 30).timestamp()
        may_start_ts = datetime(2026, 5, 3, 9, 0).timestamp()
        may_end_ts = datetime(2026, 5, 3, 10, 0).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("April Song", april_start_ts, april_end_ts, april_end_ts, 7200.0),
                    ("Boundary Song", crossing_start_ts, crossing_end_ts, crossing_end_ts, 3600.0),
                    ("May Song", may_start_ts, may_end_ts, may_end_ts, 3600.0),
                ],
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 5, 10)

        with patch.object(dashboard, "date", FrozenDate):
            april_stats = dashboard.get_stats("2026-04")
            may_stats = dashboard.get_stats("2026-05")

        april_projects = {row["project_name"]: row["month_seconds"] for row in april_stats["projects"]}
        may_projects = {row["project_name"]: row["month_seconds"] for row in may_stats["projects"]}

        self.assertEqual(april_stats["summary"]["selected_month"], "2026-04")
        self.assertEqual(april_stats["summary"]["month_seconds"], 9000.0)
        self.assertEqual(april_stats["summary"]["month_project_count"], 2)
        self.assertEqual(april_projects["April Song"], 7200.0)
        self.assertEqual(april_projects["Boundary Song"], 1800.0)
        self.assertEqual(may_stats["summary"]["selected_month"], "2026-05")
        self.assertEqual(may_stats["summary"]["month_seconds"], 5400.0)
        self.assertEqual(may_projects["Boundary Song"], 1800.0)
        self.assertEqual(may_projects["May Song"], 3600.0)

    def test_monthly_campaign_ranks_previous_month_shares_and_live_project(self):
        def ts(month, day, hour):
            return datetime(2026, month, day, hour, 0).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("Alpha", ts(4, 2, 10), ts(4, 2, 12), ts(4, 2, 12), 7200.0),
                    ("Beta", ts(4, 3, 10), ts(4, 3, 11), ts(4, 3, 11), 3600.0),
                    ("Alpha", ts(5, 2, 10), ts(5, 2, 11), ts(5, 2, 11), 3600.0),
                    ("Alpha", ts(5, 3, 10), ts(5, 3, 11), ts(5, 3, 11), 3600.0),
                    ("Beta", ts(5, 4, 10), ts(5, 4, 13), ts(5, 4, 13), 10800.0),
                    ("Gamma", ts(5, 5, 10), ts(5, 5, 11), ts(5, 5, 11), 3600.0),
                    ("Delta", ts(5, 6, 10), ts(5, 6, 11), ts(5, 6, 11), 3600.0),
                    ("Beta", ts(5, 10, 10), ts(5, 10, 10), None, 60.0),
                ],
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 5, 10)

        with patch.object(dashboard, "date", FrozenDate), patch.object(dashboard.time, "time", return_value=ts(5, 10, 10)):
            stats = dashboard.get_stats("2026-05")

        rows = {row["project_name"]: row for row in stats["projects"]}
        self.assertEqual(rows["Alpha"]["month_seconds"], 7200.0)
        self.assertEqual(rows["Alpha"]["previous_month_seconds"], 7200.0)
        self.assertEqual(rows["Alpha"]["month_rank"], 2)
        self.assertEqual(rows["Alpha"]["previous_month_rank"], 1)
        self.assertEqual(rows["Alpha"]["rank_delta"], -1)
        self.assertEqual(rows["Beta"]["month_rank"], 1)
        self.assertEqual(rows["Beta"]["previous_month_rank"], 2)
        self.assertEqual(rows["Beta"]["rank_delta"], 1)
        self.assertTrue(rows["Beta"]["is_live_project"])
        self.assertIsNone(rows["Gamma"]["previous_month_rank"])
        self.assertIsNone(rows["Gamma"]["rank_delta"])
        self.assertEqual(rows["Delta"]["month_rank"], 3)
        self.assertEqual(rows["Gamma"]["month_rank"], 4)
        campaign_rows = [row for row in rows.values() if row["month_rank"] is not None]
        self.assertAlmostEqual(sum(row["month_share_percent"] for row in campaign_rows), 100.0)


class ConsolidateSessionsTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def _insert(self, project, start_ts, end_ts, active, notes="", todos_json="[]"):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (project, start_ts, end_ts, end_ts, active, notes, todos_json),
            )
            conn.commit()
            return cur.lastrowid

    def _row_count(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def _all_rows(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.row_factory = tracker.sqlite3.Row
            return conn.execute(
                "SELECT * FROM sessions ORDER BY start_time ASC"
            ).fetchall()

    def test_merges_short_interruption(self):
        """A(X) → short B(Y) → A(X): merge X fragments."""
        x1_end = 100.0 + 900.0
        y1_start = x1_end
        y1_end = y1_start + 300.0
        x2_start = y1_end
        x2_end = x2_start + 1500.0

        self._insert("X", 100.0, x1_end, 900.0)
        self._insert("Y", y1_start, y1_end, 300.0)
        self._insert("X", x2_start, x2_end, 1500.0)

        self.assertEqual(self._row_count(), 3)
        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self._row_count(), 2)

        rows = self._all_rows()
        projects = [r["project_name"] for r in rows]
        self.assertIn("X", projects)
        self.assertIn("Y", projects)

        x_row = next(r for r in rows if r["project_name"] == "X")
        self.assertAlmostEqual(x_row["active_seconds"], 2400.0)
        self.assertAlmostEqual(x_row["start_time"], 100.0)
        self.assertAlmostEqual(x_row["end_time"], x2_end)

    def test_does_not_merge_long_single_interruption(self):
        """A(X) → long B(Y) > 15 min → A(X): don't merge."""
        x1_end = 100.0 + 900.0
        y1_start = x1_end
        y1_end = y1_start + 1200.0  # 20 min > 15 min
        x2_start = y1_end
        x2_end = x2_start + 1500.0

        self._insert("X", 100.0, x1_end, 900.0)
        self._insert("Y", y1_start, y1_end, 1200.0)
        self._insert("X", x2_start, x2_end, 1500.0)

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(self._row_count(), 3)

    def test_does_not_merge_across_midnight(self):
        """Day 1 X → day 2 X: don't merge."""
        ts1 = datetime(2026, 6, 15, 23, 50).timestamp()
        ts2 = datetime(2026, 6, 15, 23, 55).timestamp()
        ts3 = datetime(2026, 6, 16, 0, 0).timestamp()
        ts4 = datetime(2026, 6, 16, 0, 5).timestamp()
        ts5 = datetime(2026, 6, 16, 0, 10).timestamp()

        self._insert("X", ts1, ts2, 300.0)
        self._insert("Y", ts3, ts4, 300.0)
        self._insert("X", ts4, ts5, 300.0)

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)
        self.assertEqual(self._row_count(), 3)

    def test_skips_active_sessions(self):
        """Active sessions are never merged or deleted."""
        x1_end = 100.0 + 900.0
        y1_start = x1_end
        y1_end = y1_start + 300.0

        self._insert("X", 100.0, x1_end, 900.0)
        self._insert("Y", y1_start, y1_end, 300.0)
        # Insert an active session (end_time IS NULL)
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, active_seconds)
                VALUES (?, ?, ?, ?)
                """,
                ("X", y1_end, y1_end, 0.0),
            )
            conn.commit()

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)
        # X1 is closed, Y is closed, X2 is active — no merges
        rows = self._all_rows()
        self.assertEqual(len(rows), 3)

    def test_does_not_merge_return_window_exceeded(self):
        """Gap > 30 min: don't merge."""
        x1_end = 100.0 + 600.0
        y1_start = x1_end
        y1_end = y1_start + 300.0
        x2_start = y1_end + (31 * 60)  # 31 min later

        self._insert("X", 100.0, x1_end, 600.0)
        self._insert("Y", y1_start, y1_end, 300.0)
        # Need another X to check — but gap > 30 min, so won't merge
        self._insert("X", x2_start, x2_start + 600.0, 600.0)

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)
        self.assertEqual(self._row_count(), 3)

    def test_does_not_merge_total_intervening_exceeded(self):
        """Two intervening projects totaling > 20 min: don't merge."""
        x1_end = 100.0 + 300.0
        y1_start = x1_end
        y1_end = y1_start + 660.0  # 11 min each
        z1_start = y1_end
        z1_end = z1_start + 660.0  # 11 min each, total 22 min > 20 min
        x2_start = z1_end
        x2_end = x2_start + 600.0

        self._insert("X", 100.0, x1_end, 300.0)
        self._insert("Y", y1_start, y1_end, 660.0)
        self._insert("Z", z1_start, z1_end, 660.0)
        self._insert("X", x2_start, x2_end, 600.0)

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)
        self.assertEqual(self._row_count(), 4)

    def test_preserves_notes_and_todos(self):
        """Merged notes concatenate, todos deduplicate with latest done."""
        x1_end = 100.0 + 900.0
        y1_start = x1_end
        y1_end = y1_start + 300.0
        x2_start = y1_end
        x2_end = x2_start + 1500.0

        self._insert("X", 100.0, x1_end, 900.0,
                     notes="Drums",
                     todos_json='[{"text":"Kick","done":false}]')
        self._insert("Y", y1_start, y1_end, 300.0,
                     notes="Check ref")
        self._insert("X", x2_start, x2_end, 1500.0,
                     notes="Bass",
                     todos_json='[{"text":"Kick","done":true},{"text":"Snare","done":false}]')

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 1)

        rows = self._all_rows()
        self.assertEqual(len(rows), 2)
        x_row = next(r for r in rows if r["project_name"] == "X")
        self.assertIn("Drums", x_row["notes"])
        self.assertIn("Bass", x_row["notes"])

        todos = json.loads(x_row["todos_json"] or "[]")
        todos_by_text = {t["text"]: t["done"] for t in todos}
        self.assertTrue(todos_by_text.get("Kick"))
        self.assertFalse(todos_by_text.get("Snare"))

    def test_preserves_total_active_seconds(self):
        """Total active_seconds across all rows should be unchanged."""
        x1_end = 100.0 + 900.0
        y1_start = x1_end
        y1_end = y1_start + 300.0
        x2_start = y1_end
        x2_end = x2_start + 1500.0

        self._insert("X", 100.0, x1_end, 900.0)
        self._insert("Y", y1_start, y1_end, 300.0)
        self._insert("X", x2_start, x2_end, 1500.0)

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            before = conn.execute("SELECT COALESCE(SUM(active_seconds),0) FROM sessions").fetchone()[0]

        dashboard.consolidate_sessions()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            after = conn.execute("SELECT COALESCE(SUM(active_seconds),0) FROM sessions").fetchone()[0]

        self.assertAlmostEqual(before, after)
        self.assertAlmostEqual(before, 2700.0)

    def test_empty_db_returns_ok(self):
        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)
        self.assertEqual(result["deleted"], 0)

    def test_single_row_no_merge(self):
        self._insert("X", 100.0, 200.0, 100.0)
        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)
        self.assertEqual(result["deleted"], 0)

    def test_different_projects_only_no_merge(self):
        self._insert("X", 100.0, 200.0, 100.0)
        self._insert("Y", 200.0, 300.0, 100.0)
        self._insert("Z", 300.0, 400.0, 100.0)
        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)

    def test_merges_same_project_idle_gap(self):
        """Same project, no intervening rows, gap ≤ 90 min → merge."""
        x1_end = 100.0 + 900.0
        gap = 50 * 60  # 50 min idle
        x2_start = x1_end + gap
        x2_end = x2_start + 600.0

        self._insert("X", 100.0, x1_end, 900.0)
        self._insert("X", x2_start, x2_end, 600.0)

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self._row_count(), 1)

        row = self._all_rows()[0]
        self.assertAlmostEqual(row["start_time"], 100.0)
        self.assertAlmostEqual(row["end_time"], x2_end)
        self.assertAlmostEqual(row["active_seconds"], 1500.0)

    def test_does_not_merge_same_project_idle_gap_exceeded(self):
        """Same project, no intervening rows, gap > 90 min → don't merge."""
        x1_end = 100.0 + 900.0
        gap = 95 * 60  # 95 min idle
        x2_start = x1_end + gap
        x2_end = x2_start + 600.0

        self._insert("X", 100.0, x1_end, 900.0)
        self._insert("X", x2_start, x2_end, 600.0)

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)
        self.assertEqual(self._row_count(), 2)

    def test_chains_multiple_same_project_fragments(self):
        """5 same-project fragments with idle gaps ≤ 90 min → 1 row."""
        base = 100.0
        t = base
        for i in range(5):
            dur = 600.0
            t_end = t + dur
            self._insert("X", t, t_end, dur)
            t = t_end + (10 * 60)  # 10 min gap between each

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 4)
        self.assertEqual(result["deleted"], 4)
        self.assertEqual(self._row_count(), 1)

        row = self._all_rows()[0]
        self.assertAlmostEqual(row["start_time"], 100.0)
        self.assertAlmostEqual(row["active_seconds"], 3000.0)

    def test_chains_mixed_idle_and_vibe_check(self):
        """Chain with idle gaps + one short vibe-check interruption."""
        x1_end = 100.0 + 600.0
        y1_start = x1_end
        y1_end = y1_start + 300.0  # 5 min vibe check
        x2_start = y1_end
        x2_end = x2_start + 600.0

        self._insert("X", 100.0, x1_end, 600.0)
        self._insert("Y", y1_start, y1_end, 300.0)
        self._insert("X", x2_start, x2_end, 600.0)

        result = dashboard.consolidate_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 1)
        self.assertEqual(self._row_count(), 2)

        rows = self._all_rows()
        x_row = next(r for r in rows if r["project_name"] == "X")
        self.assertAlmostEqual(x_row["active_seconds"], 1200.0)
        self.assertAlmostEqual(x_row["start_time"], 100.0)
        self.assertAlmostEqual(x_row["end_time"], x2_end)



class DashboardGamificationTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (tracker.Path(__file__).parent / "templates" / "dashboard.html").read_text()
        cls.render_source = cls.source.split("function render(data) {", 1)[1].split("function updateSessionStatus", 1)[0]

    def test_weekly_pace_is_the_only_primary_weekly_mount(self):
        self.assertEqual(self.render_source.count('id="weeklyGoalCard"'), 1)
        self.assertIn("Weekly <em>pace</em>", self.render_source)
        self.assertNotIn("Today's <em>Required</em>", self.render_source)
        self.assertNotIn("Weekly <em>Quest</em>", self.render_source)
        self.assertIn("/api/weekly-target", self.source)
        self.assertIn('id="weeklyTargetPrev"', self.render_source)
        self.assertIn('id="weeklyTargetNext"', self.render_source)
        self.assertIn("model.progressText", self.source)
        self.assertIn('aria-label="Weekly pace progress"', self.source)
        self.assertNotIn("quest-checkpoint", self.source)
        self.assertNotIn("Next checkpoint", self.source)

    def test_production_run_contract_and_reduced_motion_are_present(self):
        self.assertIn("summary.production_run", self.render_source)
        self.assertIn("Production Run", self.render_source)
        self.assertIn("data-detail=\"production-run\"", self.render_source)
        self.assertNotIn("Current Streak", self.render_source)
        self.assertNotIn("Personal record", self.render_source)
        self.assertIn("Personal record", self.source)
        self.assertIn("to your record", self.source)
        self.assertIn("@media(prefers-reduced-motion:reduce)", self.source)
        self.assertIn("run-node.is-newly-qualified", self.source)

    def test_monthly_project_preview_leads_to_full_ranking(self):
        self.assertIn("Projects <em>this month</em>", self.render_source)
        self.assertIn("project.month_rank", self.render_source)
        self.assertIn("project.month_share_percent", self.render_source)
        self.assertIn("project.is_live_project", self.render_source)
        self.assertIn("campaignRows.slice(0, 3)", self.render_source)
        self.assertIn("View full project ranking", self.render_source)
        self.assertIn('id="projectsMonthNavSlot"', self.render_source)
        self.assertNotIn("campaign-move", self.render_source)
        self.assertNotIn("category_label || 'Uncategorized'", self.render_source)
        self.assertNotIn('data-detail="top-project"', self.render_source)
        self.assertNotIn('id="categoryChart"', self.render_source)
        self.assertNotIn('<h3 class="section-title">Projects</h3>', self.render_source)
        recent = self.render_source.index("View session history")
        load_older = self.render_source.index("Load older entries")
        self.assertGreater(load_older, recent)

    def test_overview_orders_weekly_pace_streak_and_project_preview(self):
        weekly = self.render_source.index("Weekly <em>pace</em>")
        streak = self.render_source.index("Production Run")
        projects = self.render_source.index("Projects <em>this month</em>")
        history = self.render_source.index("View session history")
        self.assertLess(weekly, streak)
        self.assertLess(streak, projects)
        self.assertLess(projects, history)






class FocusTimelineTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def _insert_session(self, name, start, end, active=100.0):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, start, end, end, active),
            )
            conn.commit()

    def test_migrations_create_pauses_table(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='pauses'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_focus_timeline_interleaves_sessions_and_pauses(self):
        self._insert_session("Song A", 1000.0, 1100.0)
        self._insert_session("Song B", 1500.0, 1600.0)
        rebuilt = dashboard.rebuild_pause_history()
        self.assertEqual(rebuilt["created"], 1)

        result = dashboard.get_focus_timeline(days=0)
        self.assertEqual(result["session_count"], 2)
        self.assertEqual(result["pause_count"], 1)
        self.assertEqual(result["total_pause_seconds"], 400.0)
        kinds = [event["kind"] for event in result["events"]]
        self.assertEqual(kinds, ["session", "pause", "session"])
        self.assertIn("BREAK", result["timeline_markdown"])
        self.assertIn("Song A", result["timeline_markdown"])
        self.assertIn("Song B", result["timeline_markdown"])

    def test_focus_timeline_empty_db_returns_empty_markdown(self):
        result = dashboard.get_focus_timeline(days=0)
        self.assertEqual(result["events"], [])
        self.assertIn("No sessions or pauses", result["timeline_markdown"])

    def test_focus_timeline_counts_within_pauses_separately(self):
        self._insert_session("Song A", 1000.0, 1600.0, active=500.0)
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.ensure_pauses_table(conn)
            conn.commit()
        tracker.record_pause(
            1100.0, 1200.0, reason="no_input_listening",
            prev_project="Song A", next_project="Song A",
            kind="within", session_id=1,
        )
        result = dashboard.get_focus_timeline(days=0)
        self.assertEqual(result["session_count"], 1)
        self.assertEqual(result["pause_count"], 0)
        self.assertEqual(result["within_pause_count"], 1)
        self.assertEqual(result["total_within_pause_seconds"], 100.0)
        self.assertEqual(result["total_pause_seconds"], 0.0)
        self.assertIn("- PAUSE", result["timeline_markdown"])
        self.assertIn("during: Song A", result["timeline_markdown"])

    def test_get_pauses_filter_by_kind(self):
        self._insert_session("Song A", 1000.0, 1100.0)
        self._insert_session("Song B", 1500.0, 1600.0)
        dashboard.rebuild_pause_history()
        tracker.record_pause(
            1025.0, 1090.0, reason="no_input",
            prev_project="Song A", next_project="Song A",
            kind="within", session_id=1,
        )
        self.assertEqual(len(dashboard.get_pauses()), 2)
        self.assertEqual(len(dashboard.get_pauses(kind="within")), 1)
        self.assertEqual(len(dashboard.get_pauses(kind="between")), 1)


if __name__ == "__main__":
    unittest.main()
