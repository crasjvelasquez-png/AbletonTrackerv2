"""Policy v6 failure inventory, specified before implementation; temporary DBs only.

- the 3m tail after the last play is kept when you never come back
- input in another app during the tail is kept as work
- a short stop between plays is trimmed or split
- the trim leaves session and evidence totals out of sync
- a lock keeps the tail; a manual pause loses it
- with play state unknown, input in other apps counts
- playback behind other apps counts forever, or a short trip away stops it
"""
import contextlib, pathlib, sqlite3, tempfile, unittest
from datetime import datetime
from unittest.mock import patch
import tracker

class PolicyV6Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / 'sessions.db'
        p = patch.object(tracker, 'DB_PATH', self.db); p.start(); self.addCleanup(p.stop)
        self.t = tracker.Tracker(writer_lock=False); self.addCleanup(self.t.shutdown)
        self.base = datetime(2026, 10, 26, 12, 0).timestamp()
    def poll(self, sec, audio=None, input_at=1000.0, foreground=True, **kw):
        fields = dict(wall=self.base+sec, mono=1000.0+sec, pid=42, foreground=foreground,
                      project='Song A', project_at=1000.0+sec, input_at=input_at, observable=True,
                      locked=False, audio=audio, audio_at=None if audio is None else 1000.0+sec,
                      source='fixture')
        fields.update(kw)
        self.t.poll_once(observation=tracker.Observation(**fields)); return self.t.status()
    def total(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            return c.execute('SELECT COALESCE(SUM(active_seconds),0) FROM sessions').fetchone()[0]
    def sessions(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            return c.execute('SELECT active_seconds,start_time,end_time FROM sessions ORDER BY id').fetchall()

    def test_tail_trimmed_to_last_play(self):
        # Trims tail back to last playback after idle cutoff and sets status reason to idle.
        for sec in range(0, 60 + 1, 5):
            self.poll(sec, audio=True)
        for sec in range(65, 900 + 1, 5):
            status = self.poll(sec, audio=False)
        self.assertEqual(self.total(), 60)
        self.assertEqual(status.reason, 'idle')

    def test_tail_trimmed_to_last_input_in_live(self):
        # While playback is stopped, user input in Live extends the session tail up to the last Live input.
        for sec in range(0, 60 + 1, 5):
            self.poll(sec, audio=True)
        for sec in range(65, 900 + 1, 5):
            input_at = 1000.0 + sec if sec <= 120 else 1120.0
            self.poll(sec, audio=False, input_at=input_at)
        self.assertEqual(self.total(), 120)

    def test_input_in_other_app_not_kept_in_tail(self):
        # Input in other applications while Live is stopped is not credited or kept in the session tail.
        for sec in range(0, 60 + 1, 5):
            self.poll(sec, audio=True, foreground=True)
        for sec in range(65, 900 + 1, 5):
            self.poll(sec, audio=False, foreground=False, input_at=1000.0 + sec)
        self.assertEqual(self.total(), 60)

    def test_trim_keeps_db_consistent(self):
        # Tail trimming preserves consistency between activity_intervals and the session row in SQLite.
        for sec in range(0, 60 + 1, 5):
            self.poll(sec, audio=True)
        for sec in range(65, 900 + 1, 5):
            self.poll(sec, audio=False)
        total = self.total()
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            credited = c.execute('SELECT SUM(credited_seconds) FROM activity_intervals').fetchone()[0]
        self.assertEqual(credited, total)
        sess = self.sessions()
        self.assertEqual(len(sess), 1)
        active_seconds, start_time, end_time = sess[0]
        self.assertEqual(end_time, self.base + 60)
        self.assertEqual(active_seconds, 60)

    def test_short_stop_not_trimmed(self):
        # Stops shorter than PLAY_ALLOWANCE between playback periods are bridged without trimming.
        for sec in range(0, 60 + 1, 5):
            self.poll(sec, audio=True)
        for sec in range(65, 215 + 1, 5):
            self.poll(sec, audio=False)
        for sec in range(220, 300 + 1, 5):
            self.poll(sec, audio=True)
        self.assertEqual(self.total(), 300)
        self.assertEqual(len(self.sessions()), 1)

    def test_lock_trims_tail(self):
        # Screen lock trims unconfirmed stopped time back to the last active playback.
        for sec in range(0, 30 + 1, 5):
            self.poll(sec, audio=True)
        for sec in range(35, 90 + 1, 5):
            self.poll(sec, audio=False)
        self.poll(95, locked=True, audio=None, boundary=1093)
        self.assertEqual(self.total(), 30)

    def test_manual_pause_not_trimmed(self):
        # Manual pause preserves elapsed stopped time without trimming the tail.
        for sec in range(0, 30 + 1, 5):
            self.poll(sec, audio=True)
        for sec in range(35, 90 + 1, 5):
            self.poll(sec, audio=False)
        self.t.poll_once(paused=True, observation=tracker.Observation(wall=self.base+95, mono=1095, pid=42, audio=False, audio_at=1095, source='fixture'))
        self.assertGreaterEqual(self.total(), 90)

    def test_fallback_counts_input_with_live_in_front(self):
        # When play state is unknown, continuous input with Live in front counts as working.
        for sec in range(0, 100 + 1, 5):
            status = self.poll(sec, audio=None, input_at=1000.0 + sec, foreground=True)
        self.assertEqual(self.total(), 100)
        self.assertEqual(status.reason, 'working')

    def test_fallback_ignores_input_in_other_apps(self):
        # When play state is unknown, user input in background apps is ignored and yields 0 credit.
        self.poll(0, audio=None, foreground=True, input_at=1000.0)
        for sec in range(5, 600 + 1, 5):
            self.poll(sec, audio=None, foreground=False, input_at=1000.0 + sec)
        self.assertEqual(self.total(), 0)

    def test_background_playback_stops_after_allowance(self):
        # Playback in background stops crediting after BACKGROUND_ALLOWANCE with reason background.
        for sec in range(0, 900 + 1, 5):
            status = self.poll(sec, audio=True, foreground=False)
        self.assertEqual(self.total(), tracker.BACKGROUND_ALLOWANCE)
        self.assertEqual(status.reason, 'background')
        self.assertIn('another app', status.message)

    def test_background_playback_resumes_when_live_in_front(self):
        # Bringing Live to the foreground resumes active session credit after background allowance was exhausted.
        for sec in range(0, 900 + 1, 5):
            self.poll(sec, audio=True, foreground=False)
        for sec in range(905, 960 + 1, 5):
            self.poll(sec, audio=True, foreground=True)
        self.assertIsNotNone(self.t.session_id)
        self.assertEqual(self.total(), 300 + 55)

    def test_short_background_trip_keeps_counting(self):
        # A background excursion shorter than BACKGROUND_ALLOWANCE continues tracking in one session.
        for sec in range(0, 600 + 1, 5):
            foreground = not (100 <= sec <= 340)
            self.poll(sec, audio=True, foreground=foreground)
        self.assertEqual(self.total(), 600)
        self.assertEqual(len(self.sessions()), 1)

    def test_front_visit_resets_background_timer(self):
        # Bringing Live to the foreground resets the background allowance timer.
        for sec in range(0, 10 + 1, 5):
            self.poll(sec, audio=True, foreground=True)
        for sec in range(15, 250 + 1, 5):
            self.poll(sec, audio=True, foreground=False)
        for sec in range(255, 260 + 1, 5):
            self.poll(sec, audio=True, foreground=True)
        for sec in range(265, 500 + 1, 5):
            self.poll(sec, audio=True, foreground=False)
        self.assertEqual(self.total(), 500)
        self.assertEqual(len(self.sessions()), 1)

if __name__ == '__main__':
    unittest.main()
