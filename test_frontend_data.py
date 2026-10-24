"""Dashboard additions: deterministic, temporary-database coverage written before implementation."""
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import dashboard
import tracker


class FrontendDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sessions.db'
        self.patchers = [patch.object(module, 'DB_PATH', self.path) for module in (dashboard, tracker)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard.CONFIG_CACHE.clear()
        self.addCleanup(dashboard.CONFIG_CACHE.clear)
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(self.path)) as conn:
            dashboard.run_schema_migrations(conn)

    def add_session(self, name, day, seconds, *, hour=10, open_session=False):
        start = datetime.combine(day, datetime.min.time()).replace(hour=hour).timestamp()
        with closing(tracker.sqlite3.connect(self.path)) as conn:
            conn.execute('INSERT INTO sessions(project_name,start_time,last_seen_time,end_time,active_seconds) VALUES(?,?,?,?,?)',
                         (name, start, start + seconds, None if open_session else start + seconds, seconds))
            conn.commit()

    def test_review_uses_custom_week_start_and_equal_complete_days(self):
        today = date.today()
        start = today - timedelta(days=3)
        dashboard.set_app_setting('week_start_weekday', str(start.weekday()))
        self.add_session('Top', start, 1800)
        self.add_session('Top', start + timedelta(days=1), 3600)
        self.add_session('Today', today, 7200)
        self.add_session('Prior', start - timedelta(days=7), 900)
        self.add_session('Prior incomplete day', today - timedelta(days=7), 8000)
        review = dashboard.get_stats()['summary']['weekly_review']
        self.assertEqual(review['week_start'], start.isoformat())
        self.assertEqual(review['total_seconds'], 12600)
        self.assertEqual(review['completed_days'], 3)
        self.assertEqual(review['comparison_seconds'], 5400)
        self.assertEqual(review['previous_comparison_seconds'], 900)
        self.assertEqual(review['top_project']['project_name'], 'Today')
        self.assertEqual(review['best_day']['day'], today.isoformat())

    def test_first_week_day_has_no_completed_day_comparison(self):
        dashboard.set_app_setting('week_start_weekday', str(date.today().weekday()))
        review = dashboard.get_stats()['summary']['weekly_review']
        self.assertEqual(review['completed_days'], 0)
        self.assertEqual(review['total_seconds'], 0)
        self.assertIsNone(review['top_project'])
        self.assertIsNone(review['best_day'])
        self.assertEqual(review['previous_comparison_seconds'], 0)

    def test_monday_zero_is_respected(self):
        dashboard.set_app_setting('week_start_weekday', '0')
        review = dashboard.get_stats()['summary']['weekly_review']
        self.assertEqual(date.fromisoformat(review['week_start']).weekday(), 0)

    def test_live_timer_exposes_only_recorded_time_even_when_stale_or_paused(self):
        self.add_session('Open', date.today() - timedelta(days=1), 123, open_session=True)
        for paused in (False, True):
            if paused:
                (self.path.parent / 'paused').touch()
            summary = dashboard.get_stats()['summary']
            self.assertEqual(summary['live_session_recorded_seconds'], 123)
            self.assertFalse(summary['tracking_active'])
            self.assertIsInstance(summary['live_session_last_seen_time'], float)
        with closing(tracker.sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT active_seconds FROM sessions').fetchone()[0], 123)


if __name__ == '__main__':
    unittest.main()
