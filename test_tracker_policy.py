"""Failure cases specified before implementation; all persistence uses temporary DBs.

Failure inventory: global/pointer input, unbounded loops, focus/lock/sleep, stale
sources, old project resumes, clock jumps, SQL retry/ambiguous commit, duplicate
writers, midnight gaps, evidence cleanup, malformed OSC, and probe cancellation.
"""
import contextlib
import dataclasses
import json
import pathlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import tracker


class PolicyReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / 'sessions.db'
        self.patch_db = patch.object(tracker, 'DB_PATH', self.db)
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        self.t = tracker.Tracker(writer_lock=False)
        self.addCleanup(self.t.shutdown)
        self.base = datetime(2026, 10, 26, 23, 59).timestamp()
        self.last_input = 1000.0

    def poll(self, second, **changes):
        fields = dict(wall=self.base + second, mono=1000.0 + second,
                      pid=42, foreground=True, project='Song A',
                      project_at=1000.0 + second, input_at=self.last_input,
                      observable=True, locked=False, audio=False,
                      audio_at=1000.0 + second, source='fixture')
        fields.update(changes)
        self.t.poll_once(observation=tracker.Observation(**fields))
        return self.t.status()

    def run_until(self, end, **changes):
        for sec in range(0, end + 1, 5):
            self.poll(sec, **changes)

    def total(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            return c.execute('SELECT COALESCE(SUM(active_seconds),0) FROM sessions').fetchone()[0]

    def test_silence_stops_at_thirty_seconds_and_stays_flat(self):
        self.run_until(90)
        self.assertEqual(self.total(), 30)
        self.assertEqual(self.t.status().reason, 'idle')

    def test_unavailable_audio_never_extends_input_allowance(self):
        self.run_until(90, audio=None)
        self.assertEqual(self.total(), 30)

    def test_loop_stops_at_two_minutes_without_audio_resume(self):
        self.run_until(180, audio=True)
        self.assertEqual(self.total(), 120)
        self.assertEqual(self.t.status().reason, 'listening_limit')

    def test_listening_reason_is_visible(self):
        self.run_until(35, audio=True)
        self.assertEqual(self.t.status().reason, 'listening')
        self.assertIn('Listening', self.t.status().message)
        self.assertAlmostEqual(self.t.status().cutoff, self.base + 120)

    def test_browser_activity_cannot_count_or_resume(self):
        self.run_until(10)
        for sec in range(15, 81, 5):
            self.poll(sec, foreground=False, input_at=1000+sec)
        self.assertLessEqual(self.total(),15)
        self.assertEqual(self.t.status().reason,'background')
        self.poll(85)  # focus alone, same old input
        self.assertIsNone(self.t.session_id)
        self.poll(90,input_at=1090)
        self.assertIsNotNone(self.t.session_id)

    def test_native_focus_event_stops_at_event_boundary(self):
        self.run_until(10)
        self.poll(15,foreground=False,boundary=1012)
        self.assertEqual(self.total(),12)

    def test_pointer_only_motion_does_not_reset_attendance(self):
        self.run_until(60)
        self.assertEqual(self.total(),30)
        self.poll(65,input_at=1065)
        self.assertIsNotNone(self.t.session_id)

    def test_resolve_current_project_on_resume(self):
        self.run_until(35)
        self.poll(40,project='Song B',input_at=1040)
        self.assertEqual(self.t.project_name,'Song B')
        self.poll(45,project='Song B',input_at=1040)
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            rows=c.execute('SELECT project_name,active_seconds FROM sessions ORDER BY id').fetchall()
        self.assertEqual(rows,[('Song A',30),('Song B',5)])

    def test_project_loss_grace_is_not_renewed(self):
        self.run_until(10)
        for sec in range(15,46,5):
            self.poll(sec,project=None,project_at=1010,input_at=1000+sec,audio=True)
        self.assertEqual(self.total(),20)
        self.assertEqual(self.t.status().reason,'unverified')

    def test_startup_without_project_does_not_guess(self):
        self.poll(0,project=None,project_at=None)
        self.assertIsNone(self.t.session_id)
        with patch.object(tracker,'get_live_window_titles',return_value=['Plugin Editor','Real Song']):
            self.assertIsNone(tracker.get_project_name())

    def test_unknown_input_expires_after_ten_seconds(self):
        self.run_until(10)
        for sec in range(15,46,5):
            self.poll(sec,observable=False,audio=True)
        self.assertLessEqual(self.total(),25)
        self.assertEqual(self.t.status().reason,'unverified')

    def test_sleep_and_wake_require_new_input(self):
        self.run_until(10)
        self.poll(15,locked=True,boundary=1012)
        self.poll(100,input_at=1000,audio=True)
        self.assertEqual(self.total(),12)
        self.assertIsNone(self.t.session_id)
        self.poll(105,input_at=1105)
        self.assertIsNotNone(self.t.session_id)

    def test_stale_audio_expires(self):
        for sec in range(0,61,5):
            self.poll(sec,audio=True,audio_at=1000)
        self.assertEqual(self.total(),30)

    def test_missed_poll_does_not_invent_work(self):
        self.run_until(10,audio=True)
        self.poll(300,audio=True)
        self.assertLessEqual(self.total(),20)
        self.assertIsNone(self.t.session_id)

    def test_wall_jump_uses_monotonic_duration(self):
        self.poll(0)
        self.poll(5,wall=self.base+3605,input_at=1005)
        self.poll(10,wall=self.base+3610,input_at=1010)
        self.assertEqual(self.total(),10)

    def test_failed_commit_retry_neither_loses_nor_duplicates_seconds(self):
        self.poll(0)
        real_connect=tracker.sqlite3.connect
        with patch.object(tracker.sqlite3,'connect',side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError): self.poll(5)
        self.poll(10)
        self.assertEqual(self.total(),10)
        self.poll(10)
        self.assertEqual(self.total(),10)

    def test_ambiguous_commit_is_idempotent(self):
        self.poll(0)
        real_connect=tracker.sqlite3.connect
        class Ambiguous:
            def __init__(self,*a,**kw): self.c=real_connect(*a,**kw)
            def __getattr__(self,name): return getattr(self.c,name)
            def commit(self):
                self.c.commit()
                raise sqlite3.OperationalError('commit reply lost')
        with patch.object(tracker.sqlite3,'connect',side_effect=Ambiguous):
            with self.assertRaises(sqlite3.OperationalError): self.poll(5)
        self.poll(10)
        self.assertEqual(self.total(),10)

    def test_evidence_equals_aggregate_and_removes_deleted_rows(self):
        self.run_until(35)
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT SUM(credited_seconds) FROM activity_intervals').fetchone()[0],30)
            c.execute('DELETE FROM sessions')
            c.commit()
            self.assertEqual(c.execute('SELECT COUNT(*) FROM activity_intervals').fetchone()[0],0)

    def test_migrations_are_idempotent(self):
        self.run_until(35)
        tracker.setup_db(); tracker.setup_db()
        self.assertEqual(self.total(),30)

    def test_external_close_is_not_reopened(self):
        self.run_until(10)
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            c.execute('UPDATE sessions SET end_time=?',(self.base+10,)); c.commit()
        self.poll(15)
        self.assertIsNone(self.t.session_id)
        self.assertEqual(self.total(),10)

    def test_midnight_gap_is_not_spread(self):
        self.run_until(35)
        self.poll(90,input_at=1090)
        self.poll(95,input_at=1090)
        self.assertEqual(tracker.day_seconds(datetime.fromtimestamp(self.base).date()),30)
        self.assertEqual(tracker.day_seconds(datetime.fromtimestamp(self.base+90).date()),5)

    def test_second_writer_rejected_before_initialization(self):
        first=tracker.acquire_writer_lock(self.db.with_name("other.db"))
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(tracker.acquire_writer_lock(self.db.with_name("other.db")))
        finally: first.close()


class OSCProtocolTests(unittest.TestCase):
    def test_boolean_and_integer_messages(self):
        import struct
        addr=b'/live/song/get/is_playing\0\0\0'
        for tags,payload,expected in [(b',T\0\0',b'',True),(b',F\0\0',b'',False),
                                       (b',i\0\0',struct.pack('>i',1),True),
                                       (b',i\0\0',struct.pack('>i',0),False)]:
            self.assertIs(tracker.parse_osc_playing(addr+tags+payload),expected)
        for packet in [b'garbage,T',addr+b',i\0\0',b'/wrong\0\0'+b',T\0\0']:
            self.assertIsNone(tracker.parse_osc_playing(packet))


if __name__=='__main__': unittest.main()
