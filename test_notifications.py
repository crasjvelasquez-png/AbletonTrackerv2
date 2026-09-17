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

    def test_late_streak_and_weekly_pace_banners(self):
        now = datetime(2026, 7, 23, 18, 0)

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
        now = datetime(2026, 7, 23, 18, 0)
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

    def test_streak_only_scheduled_hours_and_stops_after_activity(self):
        for hour in (11, 13, 17, 19, 22):
            self.assertEqual(self.check(datetime(2026, 7, 22, hour), today_seconds=0, weekly_goal_hours=None), [])
        for hour in (12, 18, 23):
            now = datetime(2026, 7, 22, hour)
            self.assertEqual(len(self.check(now, today_seconds=0, weekly_goal_hours=None)), 1)
            self.assertEqual(self.check(now, today_seconds=0, weekly_goal_hours=None), [])
        self.assertEqual(self.check(datetime(2026, 7, 23, 12), today_seconds=1, weekly_goal_hours=None), [])
        self.assertEqual(self.check(datetime(2026, 7, 23, 12), today_seconds=0, streak_days=0, weekly_goal_hours=None), [])

    def test_daily_goal_threshold_and_persistence(self):
        now = datetime(2026, 7, 22, 8)
        self.assertEqual(self.check(now, daily_goal_hours=2, today_seconds=7199), [])
        self.assertEqual(self.check(now, daily_goal_hours=0, today_seconds=7200), [])
        sent = self.check(now, daily_goal_hours=2, today_seconds=7200)
        self.assertEqual(sent[0].title, "Daily goal complete 🎉")
        self.coordinator = NotificationCoordinator(self.db_path, self.state_path)
        self.assertEqual(self.check(now, daily_goal_hours=2, today_seconds=8000), [])
        self.assertEqual(len(self.check(now + timedelta(days=1), daily_goal_hours=2, today_seconds=7200)), 1)

    def test_pause_requires_five_continuous_minutes_and_once_per_pause(self):
        now = datetime(2026, 7, 22, 10)
        def check(minutes, token="one", running=True):
            return self.check(now + timedelta(minutes=minutes), pause_token=token, ableton_running=running)
        self.assertEqual(check(0), [])
        self.assertEqual(check(4), [])
        self.assertEqual(check(5)[0].title, "Tracking is still paused")
        self.coordinator = NotificationCoordinator(self.db_path, self.state_path)
        self.assertEqual(check(6), [])
        self.assertEqual(check(11), [])
        self.assertEqual(check(12, None), [])
        self.assertEqual(check(13, "two"), [])
        self.assertEqual(check(17, "two", False), [])
        self.assertEqual(check(18, "two"), [])
        self.assertEqual(check(22, "two"), [])
        self.assertEqual(len(check(23, "two")), 1)

    def test_failed_delivery_retries(self):
        now = datetime(2026, 7, 22, 12)
        def fail(message):
            raise RuntimeError("test delivery failure")
        self.assertEqual(self.coordinator.check(now=now, today_seconds=0, week_seconds=0,
                         weekly_goal_hours=None, streak_days=2, deliver=fail), [])
        self.assertEqual(len(self.check(now, today_seconds=0, weekly_goal_hours=None)), 1)

    def test_recap_completed_custom_week_aliases_and_boundary_allocation(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                INSERT INTO app_settings VALUES ('week_start_weekday', '0', 0);
                CREATE TABLE project_aliases (alias_name TEXT, canonical_name TEXT);
                INSERT INTO project_aliases VALUES ('old', 'song');
                CREATE TABLE project_metadata (project_name TEXT, display_name TEXT);
                INSERT INTO project_metadata VALUES ('song', 'Blue Hour');
            """)
            for name, start, end, seconds in (
                ('old', datetime(2026, 7, 19, 23), datetime(2026, 7, 20, 1), 7200),
                ('song', datetime(2026, 7, 15, 10), datetime(2026, 7, 15, 11), 3600),
                ('new', datetime(2026, 7, 20, 10), datetime(2026, 7, 20, 11), 3600),
            ):
                conn.execute('INSERT INTO sessions (project_name,start_time,end_time,active_seconds) VALUES (?,?,?,?)',
                             (name, start.timestamp(), end.timestamp(), seconds))
        self.assertEqual(self.check(datetime(2026, 7, 20, 8, 59)), [])
        sent = self.check(datetime(2026, 7, 20, 9))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].message, '2h across 1 project. Most time: Blue Hour.')
        self.coordinator = NotificationCoordinator(self.db_path, self.state_path)
        self.assertEqual(self.check(datetime(2026, 7, 21, 9)), [])

    def test_empty_week_has_no_recap(self):
        self.assertEqual(self.check(datetime(2026, 7, 24, 9)), [])


if __name__ == "__main__":
    unittest.main()
