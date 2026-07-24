import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from notifications import NotificationCoordinator


class NotificationCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "sessions.db"
        self.state_path = root / "notification_state.json"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    last_seen_time REAL,
                    end_time REAL,
                    active_seconds REAL DEFAULT 0
                );
                CREATE TABLE project_aliases (
                    alias_name TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL
                );
                CREATE TABLE project_metadata (
                    project_name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    due_date TEXT NOT NULL DEFAULT '',
                    hard_deadline TEXT NOT NULL DEFAULT '',
                    turn_in_date TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE project_tasks (
                    id INTEGER PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    due_date TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
        self.coordinator = NotificationCoordinator(
            self.db_path, self.state_path, enabled=True
        )
        self.delivered = []

    def tearDown(self):
        self.tempdir.cleanup()

    def check(self, now, **overrides):
        values = {
            "today_seconds": 3600,
            "week_seconds": 8 * 3600,
            "weekly_goal_hours": 10,
            "streak_days": 4,
        }
        values.update(overrides)
        return self.coordinator.check(
            now=now,
            deliver=self.delivered.append,
            **values,
        )

    def test_deadline_banner_combines_project_and_task_due_tomorrow(self):
        now = datetime(2026, 7, 23, 10, 0)
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO project_metadata
                    (project_name, display_name, status, due_date)
                VALUES ('internal.als', 'Midnight Mix', 'in_progress', ?)
                """,
                (tomorrow,),
            )
            conn.execute(
                """
                INSERT INTO project_tasks
                    (project_name, title, status, due_date)
                VALUES ('internal.als', 'Print stems', 'open', ?)
                """,
                (tomorrow,),
            )

        sent = self.check(now)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].title, "Tomorrow’s deadlines")
        self.assertIn("Project: Midnight Mix", sent[0].message)
        self.assertIn("Midnight Mix: Print stems", sent[0].message)

    def test_quiet_project_banner_uses_display_name_and_seven_day_threshold(self):
        now = datetime(2026, 7, 23, 10, 0)
        last_seen = now - timedelta(days=8)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO project_metadata
                    (project_name, display_name, status)
                VALUES ('song.als', 'Blue Hour', 'in_progress')
                """
            )
            conn.execute(
                """
                INSERT INTO sessions
                    (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES ('song.als', ?, ?, ?, 3600)
                """,
                (
                    last_seen.timestamp() - 3600,
                    last_seen.timestamp(),
                    last_seen.timestamp(),
                ),
            )

        sent = self.check(now)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].title, "Project gone quiet")
        self.assertIn("Blue Hour (8d)", sent[0].message)

    def test_late_streak_and_weekly_pace_banners(self):
        now = datetime(2026, 7, 23, 17, 30)

        sent = self.check(
            now,
            today_seconds=0,
            week_seconds=0,
            weekly_goal_hours=7,
            streak_days=12,
        )

        self.assertEqual(
            [message.title for message in sent],
            [
                "Your 12-day streak is at risk 🔥",
                "Weekly goal check-in",
            ],
        )
        self.assertIn("behind pace", sent[1].subtitle)

    def test_notifications_are_deduplicated_across_coordinator_instances(self):
        now = datetime(2026, 7, 23, 17, 30)
        first = self.check(
            now,
            today_seconds=0,
            week_seconds=0,
            weekly_goal_hours=7,
            streak_days=3,
        )
        reloaded = NotificationCoordinator(
            self.db_path, self.state_path, enabled=True
        )
        second = reloaded.check(
            now=now,
            today_seconds=0,
            week_seconds=0,
            weekly_goal_hours=7,
            streak_days=3,
            deliver=self.delivered.append,
        )

        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])

    def test_no_notifications_before_morning_window(self):
        sent = self.check(
            datetime(2026, 7, 23, 8, 59),
            today_seconds=0,
            week_seconds=0,
            weekly_goal_hours=7,
            streak_days=3,
        )

        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
