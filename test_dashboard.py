import os
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
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


if __name__ == "__main__":
    unittest.main()
