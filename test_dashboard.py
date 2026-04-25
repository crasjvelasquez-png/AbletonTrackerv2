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

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_set_project_category_persists_and_is_returned_by_stats(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Real Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        result = dashboard.set_project_category("Real Song", "production")
        self.assertTrue(result["ok"])
        self.assertEqual(result["category"]["label"], "Production")

        stats = dashboard.get_stats()
        self.assertEqual(stats["projects"][0]["category_key"], "production")
        self.assertEqual(stats["projects"][0]["category_label"], "Production")
        self.assertEqual(stats["projects"][0]["category_color"], "#00A6FF")
        self.assertEqual(stats["recent"][0]["category_key"], "production")

    def test_set_project_category_none_clears_existing_assignment(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Real Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        dashboard.set_project_category("Real Song", "mixing")
        cleared = dashboard.set_project_category("Real Song", None)

        self.assertTrue(cleared["ok"])
        stats = dashboard.get_stats()
        self.assertIsNone(stats["projects"][0]["category_key"])
        self.assertIsNone(stats["recent"][0]["category_key"])


class DashboardRolloverTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()

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


if __name__ == "__main__":
    unittest.main()
