import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from notifications import NotificationCoordinator
from tracker import TrackerStatus


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

    # Failure modes covered below: early/late delivery, repeats after being
    # ignored, repeats across restarts, interrupting an active session,
    # wrong remaining time, missing/duplicate goal acknowledgment, snooze
    # firing early or from a stale day, skip not sticking, malformed action
    # files, lost retry after failed delivery, and midnight rollover.

    def invite(self, now, **overrides):
        values = {"today_seconds": 0, "daily_goal_hours": 2}
        values.update(overrides)
        return self.check(now, **values)

    def act(self, action, day, at):
        with self.coordinator.actions_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"action": action, "day": day, "at": at}) + "\n")

    def test_invitation_waits_for_8am_and_shows_target(self):
        self.assertEqual(self.invite(datetime(2026, 7, 23, 7, 59)), [])
        sent = self.invite(datetime(2026, 7, 23, 8))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].title, "Some time for music today?")
        self.assertEqual(sent[0].subtitle, "Today’s target: 2h")
        self.assertEqual(sent[0].category, "invite")
        self.assertEqual(sent[0].day, "2026-07-23")

    def test_ignored_invitation_is_not_repeated_even_after_restart(self):
        self.assertEqual(len(self.invite(datetime(2026, 7, 23, 8))), 1)
        self.assertEqual(self.invite(datetime(2026, 7, 23, 12)), [])
        self.coordinator = NotificationCoordinator(self.db_path, self.state_path)
        self.assertEqual(self.invite(datetime(2026, 7, 23, 15)), [])

    def test_new_day_gets_a_new_invitation(self):
        self.invite(datetime(2026, 7, 23, 8))
        self.assertEqual(len(self.invite(datetime(2026, 7, 24, 8, 1))), 1)

    def test_catch_up_until_9pm_only(self):
        self.assertEqual(len(self.invite(datetime(2026, 7, 23, 20, 59))), 1)
        self.assertEqual(self.invite(datetime(2026, 7, 24, 21)), [])

    def test_active_session_suppresses_that_days_invitation(self):
        now = datetime(2026, 7, 23, 8)
        status = TrackerStatus(state="tracking", checked_at=now.timestamp())
        self.assertEqual(self.invite(now, tracker_status=status), [])
        self.assertEqual(self.invite(now + timedelta(hours=2)), [])

    def test_partial_progress_shows_remaining_time(self):
        sent = self.invite(datetime(2026, 7, 23, 9), today_seconds=75 * 60)
        self.assertEqual(sent[0].subtitle, "45m left toward today’s target")

    def test_no_goal_invites_without_target(self):
        sent = self.invite(datetime(2026, 7, 23, 8), daily_goal_hours=None)
        self.assertEqual(sent[0].subtitle, "")
        self.assertEqual(sent[0].message, "Pick up a project or start something new.")

    def test_goal_reached_acknowledged_once_after_session(self):
        now = datetime(2026, 7, 23, 11)
        active = TrackerStatus(state="tracking", checked_at=now.timestamp())
        self.assertEqual(self.invite(now, today_seconds=7200, tracker_status=active), [])
        sent = self.invite(now, today_seconds=7200)
        self.assertEqual([m.title for m in sent], ["Daily target reached"])
        self.assertEqual(sent[0].message, "2h in Ableton today.")
        self.coordinator = NotificationCoordinator(self.db_path, self.state_path)
        self.assertEqual(self.invite(now + timedelta(hours=1), today_seconds=8000), [])

    def test_later_reminds_after_one_hour(self):
        self.invite(datetime(2026, 7, 23, 8))
        self.act("later", "2026-07-23", datetime(2026, 7, 23, 8, 5).timestamp())
        self.assertEqual(self.invite(datetime(2026, 7, 23, 9, 4)), [])
        self.coordinator = NotificationCoordinator(self.db_path, self.state_path)
        self.assertEqual(len(self.invite(datetime(2026, 7, 23, 9, 5))), 1)
        self.assertEqual(self.invite(datetime(2026, 7, 23, 10, 30)), [])
        self.act("later", "2026-07-23", datetime(2026, 7, 23, 10, 30).timestamp())
        self.assertEqual(len(self.invite(datetime(2026, 7, 23, 11, 31))), 1)

    def test_later_not_delivered_while_working_or_after_goal(self):
        self.invite(datetime(2026, 7, 23, 8))
        self.act("later", "2026-07-23", datetime(2026, 7, 23, 8).timestamp())
        now = datetime(2026, 7, 23, 9, 30)
        status = TrackerStatus(state="tracking", checked_at=now.timestamp())
        self.assertEqual(self.invite(now, tracker_status=status), [])
        self.assertEqual([m.title for m in self.invite(now, today_seconds=7200)],
                         ["Daily target reached"])

    def test_skip_and_open_end_the_day(self):
        for action in ("skip", "open", "dismiss"):
            with self.subTest(action=action):
                day = {"skip": 23, "open": 24, "dismiss": 25}[action]
                self.invite(datetime(2026, 7, day, 8))
                self.act("later", f"2026-07-{day}", datetime(2026, 7, day, 8).timestamp())
                self.act(action, f"2026-07-{day}", datetime(2026, 7, day, 8, 1).timestamp())
                self.assertEqual(self.invite(datetime(2026, 7, day, 10)), [])

    def test_stale_and_malformed_actions_are_ignored(self):
        self.act("later", "2026-07-22", datetime(2026, 7, 22, 20).timestamp())
        with self.coordinator.actions_path.open("a") as handle:
            handle.write("not json\n{\"action\": 5}\n")
        self.invite(datetime(2026, 7, 23, 8))
        self.assertEqual(self.invite(datetime(2026, 7, 23, 9, 30)), [])
        self.assertFalse(self.coordinator.actions_path.exists())

    def test_failed_delivery_retries(self):
        def fail(message):
            raise RuntimeError("test delivery failure")
        self.assertEqual(self.coordinator.check(now=datetime(2026, 7, 22, 8), today_seconds=0,
                         week_seconds=0, weekly_goal_hours=None, streak_days=0,
                         daily_goal_hours=2, deliver=fail), [])
        self.assertEqual(len(self.invite(datetime(2026, 7, 22, 8, 1))), 1)

    def test_retired_reminders_never_fire(self):
        now = datetime(2026, 7, 22, 10)
        status = TrackerStatus(idle_paused=True, checked_at=now.timestamp(),
                               idle_pause_started_at=now.timestamp() - 600)
        self.invite(datetime(2026, 7, 22, 8))
        for hour in (9, 18, 19, 20):
            self.assertEqual(self.invite(now.replace(hour=hour), streak_days=12,
                             week_seconds=0, weekly_goal_hours=40,
                             ableton_running=True, tracker_status=status), [])

    def test_pause_requires_five_continuous_minutes_and_once_per_pause(self):
        now = datetime(2026, 7, 22, 7)
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


if __name__ == "__main__":
    unittest.main()
