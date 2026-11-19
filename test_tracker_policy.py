"""Failure cases specified before implementation; all persistence uses temporary DBs.

Failure inventory: global/pointer input, unbounded loops, focus/lock/sleep, stale
sources, old project resumes, clock jumps, SQL retry/ambiguous commit, duplicate
writers, midnight gaps, evidence cleanup, malformed OSC, and probe cancellation.

Policy v2 (forgiving attendance) failure inventory:
- hands-off listening in Live is dropped after 30s although no audio probe exists
- hands-off time grows without bound when nobody returns to Live
- a short trip to another app (<3m) loses the time spent there
- a long trip to another app (>3m) is credited as work
- away time is credited progressively, so never returning still counts
- lock, quit or pause while away credits time after the departure
- returning focus after a long absence resumes without real interaction
- one failed/ambiguous window-title read ends a running session
- project memory survives forever when titles stay unreadable

Policy v3 (work happens outside Live too) failure inventory:
- using another app while a set is open stops the count (old 3m "away" rule)
- Live playing in the background is not counted
- playback is cut off by a fixed listening limit while it is still playing
- playback alone cannot resume a session after an idle pause
- no input anywhere and no playback still counts forever
- lock, quit or manual pause while Live is in the background keeps counting
- stale input from before sleep resumes a session after wake

Policy v5 (the play button is the work signal) failure inventory:
- mouse/keyboard input alone keeps counting while play detection works
- time keeps counting more than 3m after playback last stopped
- a stop shorter than 3m between plays loses or splits time
- continuous playback is cut off at 3m
- pressing play does not resume after a 3m stop
- play from before a lock, sleep or manual pause bridges across it
- play detection unavailable (AbletonOSC off) stops all tracking
- status says "Listening" while Live is stopped
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
                      observable=True, locked=False, audio=None,
                      audio_at=None, source='fixture')
        if changes.get('audio') is not None:
            fields['audio_at'] = 1000.0 + second
        fields.update(changes)
        self.t.poll_once(observation=tracker.Observation(**fields))
        return self.t.status()

    def run_until(self, end, **changes):
        for sec in range(0, end + 1, 5):
            self.poll(sec, **changes)

    def total(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            return c.execute('SELECT COALESCE(SUM(active_seconds),0) FROM sessions').fetchone()[0]

    def test_silence_stops_at_two_minutes_and_stays_flat(self):
        # v6: the 2m fallback tail is taken back to the last input with Live in front.
        for sec in range(0, 721, 5):
            self.poll(sec, input_at=1000 + min(sec, 60))
        self.assertEqual(self.total(), 60)
        self.assertEqual(self.t.status().reason, 'idle')

    def test_hands_off_listening_counts_without_playback_probe(self):
        self.run_until(100, audio=None)
        self.assertEqual(self.total(), 100)
        self.assertIsNotNone(self.t.session_id)

    def test_unavailable_audio_never_extends_input_allowance(self):
        self.run_until(720, audio=None)
        self.assertEqual(self.total(), 0)

    def test_playback_counts_with_no_time_limit(self):
        self.run_until(1800, audio=True)
        self.assertEqual(self.total(), 1800)
        self.assertEqual(self.t.status().reason, 'listening')

    def test_delayed_playback_probe_start_still_extends_at_input_allowance(self):
        from activity_observer import AUDIO_PROBE_AFTER
        first = None
        for sec in range(0, 901, 5):
            if sec - 1000.0 + self.last_input < AUDIO_PROBE_AFTER:
                self.poll(sec, audio=None, audio_at=None)
                continue
            if first is None:
                first = sec  # requested now; the result arrives next poll
                self.poll(sec, audio=None, audio_at=None)
                continue
            # Reported one poll late, measured 1.5s after the previous request.
            self.poll(sec, audio=True, audio_at=1000.0 + sec - 3.5)
        self.assertEqual(self.total(), 900)
        self.assertEqual(self.t.status().reason, 'listening')

    def test_listening_reason_is_visible(self):
        self.run_until(605, audio=True)
        self.assertEqual(self.t.status().reason, 'listening')
        self.assertIn('Listening', self.t.status().message)
        self.assertAlmostEqual(self.t.status().cutoff, self.base + 605 + tracker.PLAY_ALLOWANCE)

    def test_other_apps_count_while_live_is_open(self):
        self.run_until(10)
        for sec in range(15, 101, 5):
            self.poll(sec, foreground=False, input_at=1000+sec)
        self.assertEqual(self.total(), 100)
        self.assertEqual(self.t.status().reason, 'working')
        self.assertEqual(self.t.status().state, tracker.STATE_TRACKING)

    def test_long_time_in_other_apps_does_not_count_in_fallback(self):
        # v6: with play state unknown, only input with Live in front counts.
        self.run_until(10)
        for sec in range(15, 1201, 5):
            self.poll(sec, foreground=False, input_at=1000+sec)
        self.assertEqual(self.total(), 0)
        self.assertIsNone(self.t.session_id)

    def test_background_playback_counts_without_input(self):
        # v6: playback behind other apps counts for BACKGROUND_ALLOWANCE, then stops.
        for sec in range(0, 1201, 5):
            self.poll(sec, foreground=False, audio=True)
        self.assertEqual(self.total(), tracker.BACKGROUND_ALLOWANCE)
        self.assertEqual(self.t.status().reason, 'background')

    def test_background_without_input_or_playback_stops_at_two_minutes(self):
        for sec in range(0, 721, 5):
            self.poll(sec, foreground=False)
        self.assertEqual(self.total(), 0)
        self.assertIsNone(self.t.session_id)

    def test_playback_alone_resumes_after_idle(self):
        self.run_until(400)
        self.assertEqual(self.total(), 0)
        for sec in range(405, 501, 5):
            self.poll(sec, foreground=False, audio=True)
        self.assertIsNotNone(self.t.session_id)
        self.assertEqual(self.total(), 95)

    def test_lock_in_background_stops_counting(self):
        self.run_until(10)
        self.poll(15, foreground=False, input_at=1014)
        self.poll(20, foreground=False, locked=True, audio=True)
        self.assertIsNone(self.t.session_id)
        for sec in range(25, 101, 5):
            self.poll(sec, foreground=False, locked=True, audio=True)
        self.assertLessEqual(self.total(), 20)

    def test_quit_in_background_stops_counting(self):
        self.run_until(10)
        self.poll(15, foreground=False, input_at=1014)
        self.poll(20, pid=None, foreground=False, input_at=None, project=None)
        self.poll(60, pid=None, foreground=False, input_at=None, project=None)
        self.assertIsNone(self.t.session_id)
        self.assertLessEqual(self.total(), 20)

    def test_manual_pause_in_background_stops_counting(self):
        self.run_until(10)
        self.poll(15, foreground=False, input_at=1014)
        for sec in (20, 60):
            self.t.poll_once(paused=True, observation=tracker.Observation(
                wall=self.base+sec, mono=1000+sec, pid=42, foreground=False, audio=True,
                audio_at=1000+sec, source='fixture'))
        self.assertIsNone(self.t.session_id)
        self.assertLessEqual(self.total(), 20)

    def test_leaving_live_is_not_a_stop_boundary(self):
        self.run_until(10)
        self.poll(15, foreground=False, input_at=1014)
        self.assertEqual(self.total(), 15)
        self.assertIsNotNone(self.t.session_id)

    def test_pointer_only_motion_does_not_reset_attendance(self):
        self.run_until(720)
        self.assertEqual(self.total(),0)
        self.poll(725,input_at=1725)
        self.assertIsNotNone(self.t.session_id)

    def test_resolve_current_project_on_resume(self):
        self.run_until(305)
        self.poll(310,project='Song B',input_at=1310)
        self.assertEqual(self.t.project_name,'Song B')
        self.poll(315,project='Song B',input_at=1310)
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            rows=c.execute('SELECT project_name,active_seconds FROM sessions ORDER BY id').fetchall()
        self.assertEqual(rows,[('Song B',5)])  # v6: Song A's 2m tail was taken back

    def test_title_read_failure_keeps_current_project(self):
        self.run_until(10)
        for sec in range(15,121,5):
            self.poll(sec,project=None,project_at=1010,input_at=1000+sec)
        self.assertEqual(self.total(),120)
        self.assertEqual(self.t.project_name,'Song A')
        self.assertEqual(self.t.status().reason,'working')

    def test_project_loss_grace_is_not_renewed(self):
        self.run_until(10)
        for sec in range(15,401,5):
            self.poll(sec,project=None,project_at=1010,input_at=1000+sec,audio=True)
        self.assertEqual(self.total(),310)
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
        self.poll(100,input_at=1000,audio=False)
        self.assertEqual(self.total(),0)  # v6: lock trims back to the last input (1000)
        self.assertIsNone(self.t.session_id)
        self.poll(105,input_at=1105)
        self.assertIsNotNone(self.t.session_id)

    def test_stale_audio_expires(self):
        for sec in range(0,721,5):
            self.poll(sec,audio=True,audio_at=1000)
        self.assertEqual(self.total(),0)  # v6: taken back to the only play reading

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
            self.assertEqual(c.execute('SELECT SUM(credited_seconds) FROM activity_intervals').fetchone()[0],35)
            c.execute('DELETE FROM sessions')
            c.commit()
            self.assertEqual(c.execute('SELECT COUNT(*) FROM activity_intervals').fetchone()[0],0)

    def test_migrations_are_idempotent(self):
        self.run_until(35)
        tracker.setup_db(); tracker.setup_db()
        self.assertEqual(self.total(),35)

    def test_external_close_is_not_reopened(self):
        self.run_until(10)
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            c.execute('UPDATE sessions SET end_time=?',(self.base+10,)); c.commit()
        self.poll(15)
        self.assertIsNone(self.t.session_id)
        self.assertEqual(self.total(),10)

    def test_midnight_gap_is_not_spread(self):
        self.run_until(35)
        self.poll(90,input_at=1090)  # missed heartbeats end the session
        self.poll(95,input_at=1095)
        self.poll(100,input_at=1095)
        self.assertEqual(tracker.day_seconds(datetime.fromtimestamp(self.base).date()),45)
        self.assertEqual(tracker.day_seconds(datetime.fromtimestamp(self.base+90).date()),5)

    def test_second_writer_rejected_before_initialization(self):
        first=tracker.acquire_writer_lock(self.db.with_name("other.db"))
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(tracker.acquire_writer_lock(self.db.with_name("other.db")))
        finally: first.close()

    # Policy v5: playback drives the count whenever the play detector answers.
    def stopped(self, sec, **changes):
        return self.poll(sec, audio=False, audio_at=1000.0 + sec, **changes)

    def playing(self, sec, **changes):
        return self.poll(sec, audio=True, audio_at=1000.0 + sec, **changes)

    def test_v5_input_without_play_does_not_count(self):
        for sec in range(0, 601, 5):
            self.stopped(sec, input_at=1000 + sec)
        self.assertEqual(self.total(), 0)
        self.assertIsNone(self.t.session_id)
        self.assertIn('play', self.t.status().message.lower())

    def test_v5_stops_three_minutes_after_last_play(self):
        for sec in range(0, 61, 5):
            self.playing(sec)
        for sec in range(65, 901, 5):
            self.stopped(sec, input_at=1000 + sec)
        self.assertEqual(self.total(), 60 + tracker.PLAY_ALLOWANCE)
        self.assertEqual(self.t.status().reason, 'idle')
        self.assertIn('3m', self.t.status().message)

    def test_v5_short_stop_between_plays_is_one_session(self):
        for sec in range(0, 61, 5):
            self.playing(sec)
        for sec in range(65, 216, 5):
            self.stopped(sec)
        for sec in range(220, 301, 5):
            self.playing(sec)
        self.assertEqual(self.total(), 300)
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)

    def test_v5_continuous_playback_is_not_cut_off(self):
        for sec in range(0, 1201, 5):
            self.playing(sec)
        self.assertEqual(self.total(), 1200)
        self.assertEqual(self.t.status().reason, 'listening')

    def test_v5_play_resumes_after_stop(self):
        self.playing(0)
        for sec in range(5, 501, 5):
            self.stopped(sec, input_at=1000 + sec)
        self.assertEqual(self.total(), tracker.PLAY_ALLOWANCE)
        for sec in range(505, 601, 5):
            self.playing(sec)
        self.assertIsNotNone(self.t.session_id)
        self.assertEqual(self.total(), tracker.PLAY_ALLOWANCE + 95)

    def test_v5_play_before_lock_does_not_bridge(self):
        for sec in range(0, 31, 5):
            self.playing(sec)
        self.poll(35, locked=True, boundary=1032)
        self.stopped(60)
        self.stopped(65, input_at=1065)
        self.assertIsNone(self.t.session_id)
        self.assertEqual(self.total(), 30)  # v6: lock trims back to the last play

    def test_v5_manual_pause_does_not_bridge(self):
        for sec in range(0, 31, 5):
            self.playing(sec)
        self.t.poll_once(paused=True, observation=tracker.Observation(
            wall=self.base+35, mono=1035, pid=42, audio=False, audio_at=1035, source='fixture'))
        for sec in range(40, 101, 5):
            self.stopped(sec, input_at=1000 + sec)
        self.assertIsNone(self.t.session_id)
        self.assertLessEqual(self.total(), 35)

    def test_v5_unavailable_detector_falls_back_to_input(self):
        for sec in range(0, 101, 5):
            self.poll(sec, input_at=1000 + sec, audio=None, audio_at=None)
        self.assertEqual(self.total(), 100)
        self.assertEqual(self.t.status().reason, 'working')

    def test_v5_stopped_window_is_not_listening(self):
        self.playing(0)
        for sec in range(5, 31, 5):
            st = self.stopped(sec)
        self.assertEqual(st.reason, 'working')
        self.assertNotIn('Listening', st.message)


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
