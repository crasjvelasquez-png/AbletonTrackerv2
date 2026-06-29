import os
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
from unittest.mock import patch

import tracker


class TrackerPauseResumeTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        tracker.setup_db()

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_resume_uses_last_known_project_when_titles_are_transient(self):
        title_state = {"mode": "project"}

        def fake_get_project_name(current_project=None):
            if title_state["mode"] == "project":
                return "Real Project"
            return None

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", side_effect=fake_get_project_name):
            t = tracker.Tracker()

            t.poll_once(paused=False)
            first_session = t.session_id
            self.assertEqual(t.project_name, "Real Project")

            title_state["mode"] = "transient"
            t.poll_once(paused=True)

            self.assertIsNone(t.session_id)
            self.assertEqual(t.resume_hint_project, "Real Project")

            t.poll_once(paused=False)

            self.assertNotEqual(t.session_id, first_session)
            self.assertEqual(t.project_name, "Real Project")
            self.assertIsNone(t.resume_hint_project)
            self.assertEqual(t.status().state, tracker.STATE_TRACKING)

    def test_stops_tracking_cleanly_when_live_is_not_running(self):
        with patch.object(tracker, "is_ableton_running", return_value=False):
            t = tracker.Tracker()
            t.resume_hint_project = "Real Project"
            t.poll_once(paused=False)
            self.assertIsNone(t.resume_hint_project)
            self.assertEqual(t.status().state, tracker.STATE_ABLETON_CLOSED)

    def test_tick_uses_monotonic_elapsed_instead_of_wall_clock_gap(self):
        t = tracker.Tracker()

        with patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t._start("Real Project")

        with patch.object(tracker.time, "time", return_value=1100.0), \
             patch.object(tracker.time, "monotonic", return_value=20.0):
            t._tick()

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            active = conn.execute(
                "SELECT active_seconds FROM sessions WHERE id=?", (t.session_id,)
            ).fetchone()[0]

        self.assertEqual(active, 10.0)

    def test_tick_skips_database_write_when_elapsed_is_zero(self):
        t = tracker.Tracker()

        with patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t._start("Real Project")

        with patch.object(tracker.sqlite3, "connect") as connect, \
             patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t._tick()

        connect.assert_not_called()

    def test_idle_pause_requires_audio_to_be_quiet(self):
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=True), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker, "get_project_name", return_value="Real Project"):
            t = tracker.Tracker()
            t.poll_once(paused=False)

        self.assertIsNotNone(t.session_id)
        self.assertFalse(t.status().idle_paused)
        self.assertTrue(t.status().audio_active)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

    def test_active_input_does_not_probe_audio(self):
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active") as audio_active, \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", return_value="Real Project"):
            t = tracker.Tracker()
            t.poll_once(paused=False)

        audio_active.assert_not_called()
        self.assertIsNotNone(t.session_id)
        self.assertFalse(t.status().idle_paused)
        self.assertFalse(t.status().audio_active)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

    def test_idle_pause_closes_when_mouse_and_audio_are_idle(self):
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker.time, "time", return_value=1000.0):
            t = tracker.Tracker()
            t._start("Real Project")
            t.last_audio_active = 900.0
            t.poll_once(paused=False)

        self.assertIsNone(t.session_id)
        self.assertEqual(t.resume_hint_project, "Real Project")
        self.assertTrue(t.status().idle_paused)
        self.assertEqual(t.status().state, tracker.STATE_IDLE_PAUSED)

    def test_unavailable_audio_probe_does_not_idle_pause_open_session(self):
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=None), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker, "get_project_name", return_value="Real Project"):
            t = tracker.Tracker()
            t._start("Real Project")
            t.poll_once(paused=False)

        self.assertIsNotNone(t.session_id)
        self.assertFalse(t.status().idle_paused)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

    def test_close_adds_final_elapsed_since_last_tick(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t._start("Real Project")

        with patch.object(tracker.time, "time", return_value=1050.0), \
             patch.object(tracker.time, "monotonic", return_value=25.0):
            t._close()

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT active_seconds, end_time FROM sessions WHERE project_name=?",
                ("Real Project",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 15.0)
        self.assertIsNotNone(row[1])

    def test_tick_does_not_update_externally_closed_session(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t._start("Real Project")

        sid = t.session_id

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE sessions SET end_time=? WHERE id=?", (1005.0, sid)
            )
            conn.commit()

        with patch.object(tracker.time, "time", return_value=1020.0), \
             patch.object(tracker.time, "monotonic", return_value=20.0):
            t._tick()

        self.assertIsNone(t.session_id)

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT end_time, active_seconds FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], 0.0)

    def test_wake_from_sleep_grace_suppresses_idle_pause_one_cycle(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0):
            t._start("Real Project")

        t.last_checked_at = 1000.0

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker.time, "time", return_value=1200.0):
            t.last_audio_active = 900.0
            t.poll_once(paused=False)

        self.assertIsNotNone(t.session_id)
        self.assertFalse(t.status().idle_paused)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

    def test_mouse_movement_resumes_after_idle_pause(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0):
            t._start("Real Project")

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker.time, "time", return_value=1100.0):
            t.last_audio_active = 1000.0
            t.poll_once(paused=False)

        self.assertIsNone(t.session_id)
        self.assertEqual(t.resume_hint_project, "Real Project")

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", return_value=None), \
             patch.object(tracker.time, "time", return_value=1110.0):
            t.poll_once(paused=False)

        self.assertIsNotNone(t.session_id)
        self.assertEqual(t.project_name, "Real Project")
        self.assertFalse(t.status().idle_paused)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

    def test_idle_resume_on_mouse_activity_does_not_discard_short_row(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0):
            t._start("Real Project")

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker.time, "time", return_value=1100.0):
            t.last_audio_active = 1000.0
            t.poll_once(paused=False)

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", return_value="Real Project") as get_project_name, \
             patch.object(tracker.time, "time", return_value=1110.0):
            t.poll_once(paused=False)

        get_project_name.assert_not_called()
        self.assertIsNotNone(t.session_id)
        self.assertEqual(t.project_name, "Real Project")
        self.assertIsNone(t.resume_hint_project)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                """
                SELECT end_time IS NULL, active_seconds
                FROM sessions
                WHERE id=?
                """,
                (t.session_id,),
            ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertGreater(row[1], 0)

    def test_audio_resumes_after_idle_pause_without_mouse_activity(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0):
            t._start("Real Project")

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker.time, "time", return_value=1100.0):
            t.last_audio_active = 1000.0
            t.poll_once(paused=False)

        self.assertIsNone(t.session_id)
        self.assertEqual(t.resume_hint_project, "Real Project")

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=True), \
             patch.object(tracker, "get_idle_seconds", return_value=60), \
             patch.object(tracker, "get_project_name", return_value=None), \
             patch.object(tracker.time, "time", return_value=1110.0):
            t.poll_once(paused=False)

        self.assertIsNotNone(t.session_id)
        self.assertEqual(t.project_name, "Real Project")
        self.assertTrue(t.status().audio_active)
        self.assertFalse(t.status().idle_paused)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

    def test_repeated_idle_paused_polls_preserve_resume_hint(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0):
            t._start("Real Project")

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker.time, "time", return_value=1100.0):
            t.last_audio_active = 1000.0
            t.poll_once(paused=False)

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=45), \
             patch.object(tracker.time, "time", return_value=1115.0):
            t.poll_once(paused=False)

        self.assertIsNone(t.session_id)
        self.assertEqual(t.resume_hint_project, "Real Project")
        self.assertTrue(t.status().idle_paused)
        self.assertEqual(t.status().state, tracker.STATE_IDLE_PAUSED)

    def test_manual_pause_preserves_resume_hint_and_resume_clears_it(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0):
            t._start("Real Project")

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker.time, "time", return_value=1010.0):
            t.poll_once(paused=True)

        self.assertIsNone(t.session_id)
        self.assertEqual(t.resume_hint_project, "Real Project")
        self.assertTrue(t.status().paused)
        self.assertEqual(t.status().state, tracker.STATE_PAUSED)

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active") as audio_active, \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", return_value=None), \
             patch.object(tracker.time, "time", return_value=1020.0):
            t.poll_once(paused=False)

        audio_active.assert_not_called()
        self.assertIsNotNone(t.session_id)
        self.assertEqual(t.project_name, "Real Project")
        self.assertIsNone(t.resume_hint_project)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

    def test_resume_hint_is_cleared_even_without_open_session_when_live_quits(self):
        t = tracker.Tracker()
        t.resume_hint_project = "Real Project"

        with patch.object(tracker, "is_ableton_running", return_value=False):
            t.poll_once(paused=False)

        self.assertIsNone(t.resume_hint_project)
        self.assertEqual(t.status().state, tracker.STATE_ABLETON_CLOSED)

    def test_idle_with_no_known_project_reports_ableton_open(self):
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker.time, "time", return_value=1000.0):
            t = tracker.Tracker()
            t.poll_once(paused=False)

        self.assertIsNone(t.session_id)
        self.assertIsNone(t.resume_hint_project)
        self.assertFalse(t.status().idle_paused)
        self.assertEqual(t.status().state, tracker.STATE_ABLETON_OPEN)

    def test_project_switch_closes_old_session_and_tracks_new_state(self):
        project_state = {"name": "First Project"}

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active") as first_audio_active, \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", side_effect=lambda current_project=None: project_state["name"]), \
             patch.object(tracker.time, "time", return_value=1000.0):
            t = tracker.Tracker()
            t.poll_once(paused=False)
            first_session = t.session_id
        first_audio_active.assert_not_called()

        project_state["name"] = "Second Project"
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active") as second_audio_active, \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", side_effect=lambda current_project=None: project_state["name"]), \
             patch.object(tracker.time, "time", return_value=1040.0):
            t.poll_once(paused=False)
        second_audio_active.assert_not_called()

        self.assertNotEqual(t.session_id, first_session)
        self.assertEqual(t.project_name, "Second Project")
        self.assertIsNone(t.resume_hint_project)
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)


class AudioQuietSignalTests(unittest.TestCase):
    def test_audio_level_probe_parses_quiet_and_active(self):
        self.assertTrue(tracker._parse_audio_level_probe("active 0.02\n"))
        self.assertFalse(tracker._parse_audio_level_probe("quiet 0.0\n"))
        self.assertIsNone(tracker._parse_audio_level_probe("unavailable samples=0\n"))
        self.assertIsNone(tracker._parse_audio_level_probe("permission denied\n"))

    def test_audio_active_uses_level_probe_result(self):
        with patch.object(tracker, "_live_pid", return_value=123), \
             patch.object(tracker, "is_ableton_playing_osc", return_value=None), \
             patch.object(tracker, "_system_audio_level_active", return_value=False):
            self.assertFalse(tracker.is_audio_active())

    def test_audio_active_preserves_unavailable_probe_result(self):
        with patch.object(tracker, "_live_pid", return_value=123), \
             patch.object(tracker, "is_ableton_playing_osc", return_value=None), \
             patch.object(tracker, "_system_audio_level_active", return_value=None):
            self.assertIsNone(tracker.is_audio_active())


class ParseProjectTitleTests(unittest.TestCase):
    def test_accepts_bare_project_titles(self):
        self.assertEqual(tracker.parse_project_title("My Song"), "My Song")
        self.assertEqual(tracker.parse_project_title("Untitled"), "Untitled")

    def test_rejects_plugin_like_bare_titles_with_slashes(self):
        self.assertIsNone(tracker.parse_project_title("VocAlign 6 Pro AU/14-harmony 3 R"))


class GetProjectNameTests(unittest.TestCase):
    def test_switches_to_single_bare_title_when_current_project_changes(self):
        with patch.object(tracker, "get_live_window_titles", return_value=["New Song"]):
            self.assertEqual(tracker.get_project_name("Old Song"), "New Song")

    def test_switches_to_single_live_window_title_when_current_project_changes(self):
        with patch.object(
            tracker,
            "get_live_window_titles",
            return_value=["New Song - Ableton Live 12 Suite"],
        ):
            self.assertEqual(tracker.get_project_name("Old Song"), "New Song")

    def test_keeps_current_project_when_current_title_is_still_visible(self):
        with patch.object(
            tracker,
            "get_live_window_titles",
            return_value=["Old Song", "Plugin Window"],
        ):
            self.assertEqual(tracker.get_project_name("Old Song"), "Old Song")

    def test_refuses_to_guess_when_multiple_new_bare_titles_are_visible(self):
        with patch.object(
            tracker,
            "get_live_window_titles",
            return_value=["New Song", "Plugin Window"],
        ):
            self.assertIsNone(tracker.get_project_name("Old Song"))


class PhantomCleanupTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        tracker.setup_db()

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_cleanup_removes_only_closed_phantom_sessions(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, 1, 1, ?, ?)
                """,
                [
                    ("Export Audio...", 2, 61),
                    ("VocAlign 6 Pro AU/14-harmony 3 R", 2, 61),
                    ("Real Song", 2, 61),
                    ("Untitled", 2, 61),
                    ("Export Audio...", None, 0),
                ],
            )
            conn.commit()

        self.assertEqual(tracker.count_phantom_sessions(), 2)
        result = tracker.cleanup_phantom_sessions()

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(tracker.count_phantom_sessions(), 0)

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT project_name FROM sessions ORDER BY id"
                ).fetchall()
            ]

        self.assertEqual(names, ["Real Song", "Untitled", "Export Audio..."])

    def test_close_stale_open_sessions_finishes_leftover_rows(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("Real Song", 10, 40, None, 30),
                    ("Another Song", 50, None, None, 0),
                    ("Closed Song", 70, 90, 90, 20),
                ],
            )
            conn.commit()

        closed = tracker.close_stale_open_sessions()
        self.assertEqual(closed, 2)

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            rows = conn.execute(
                """
                SELECT project_name, last_seen_time, end_time
                FROM sessions
                ORDER BY id
                """
            ).fetchall()

        self.assertEqual(rows, [
            ("Real Song", 40.0, 40.0),
            ("Another Song", 50.0, 50.0),
            ("Closed Song", 90.0, 90.0),
        ])


class SessionCondenseTests(unittest.TestCase):
    def test_condenses_adjacent_same_project_fragments_under_five_minutes(self):
        rows = [
            {
                "project_name": "Real Song",
                "start_time": 400.0,
                "last_seen_time": 520.0,
                "end_time": 520.0,
                "active_seconds": 120.0,
            },
            {
                "project_name": "Real Song",
                "start_time": 240.0,
                "last_seen_time": 360.0,
                "end_time": 360.0,
                "active_seconds": 120.0,
            },
            {
                "project_name": "Another Song",
                "start_time": 120.0,
                "last_seen_time": 180.0,
                "end_time": 180.0,
                "active_seconds": 60.0,
            },
        ]

        condensed = tracker.condense_recent_sessions(rows)

        self.assertEqual(len(condensed), 2)
        self.assertEqual(condensed[0]["project_name"], "Real Song")
        self.assertEqual(condensed[0]["start_time"], 240.0)
        self.assertEqual(condensed[0]["end_time"], 520.0)
        self.assertEqual(condensed[0]["active_seconds"], 240.0)

    def test_does_not_condense_exactly_fifteen_minute_gap(self):
        rows = [
            {
                "project_name": "Real Song",
                "start_time": 1000.0,
                "last_seen_time": 1060.0,
                "end_time": 1060.0,
                "active_seconds": 60.0,
            },
            {
                "project_name": "Real Song",
                "start_time": 100.0,
                "last_seen_time": 100.0,
                "end_time": 100.0,
                "active_seconds": 60.0,
            },
        ]

        condensed = tracker.condense_recent_sessions(rows)

        self.assertEqual(len(condensed), 2)


class DaySecondsTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        tracker.setup_db()

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_day_seconds_allocates_cross_midnight_activity_to_next_day(self):
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

        self.assertEqual(tracker.day_seconds(date(2026, 4, 24)), 60.0)
        self.assertEqual(tracker.day_seconds(date(2026, 4, 25)), 180.0)


class StartResumeTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        tracker.setup_db()

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_start_resumes_recent_same_project_session(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Real Song", 100.0, 220.0, 220.0, 120.0),
            )
            session_id = cur.lastrowid
            conn.commit()

        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=400.0):
            t._start("Real Song")

        self.assertEqual(t.session_id, session_id)

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            rows = conn.execute(
                """
                SELECT id, project_name, start_time, last_seen_time, end_time, active_seconds
                FROM sessions
                ORDER BY id
                """
            ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (session_id, "Real Song", 100.0, 400.0, None, 120.0))

    def test_start_creates_new_row_when_gap_is_fifteen_minutes_or_more(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Real Song", 100.0, 100.0, 100.0, 60.0),
            )
            conn.commit()

        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0):
            t._start("Real Song")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            rows = conn.execute(
                """
                SELECT project_name, start_time, last_seen_time, end_time, active_seconds
                FROM sessions
                ORDER BY id
                """
            ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], ("Real Song", 1000.0, 1000.0, None, 0.0))


class UntitledSessionTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        tracker.setup_db()

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_is_untitled_project_name(self):
        self.assertTrue(tracker.is_untitled_project_name("Untitled"))
        self.assertTrue(tracker.is_untitled_project_name("untitled"))
        self.assertTrue(tracker.is_untitled_project_name("  Untitled  "))
        self.assertTrue(tracker.is_untitled_project_name("Untitled Project"))
        self.assertFalse(tracker.is_untitled_project_name("Real Song"))
        self.assertFalse(tracker.is_untitled_project_name(""))
        self.assertFalse(tracker.is_untitled_project_name(None))

    def test_cleanup_removes_closed_untitled_sessions(self):
        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, 1, 1, ?, ?)
                """,
                [
                    ("Untitled", 2, 61),
                    ("Untitled Project", 2, 30),
                    ("Real Song", 2, 120),
                    ("Another Untitled", 2, 30),
                ],
            )
            conn.commit()

        result = tracker.cleanup_untitled_sessions()
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["ok"], True)

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT project_name FROM sessions ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual(names, ["Real Song", "Another Untitled"])

    def test_cleanup_preserves_open_untitled_sessions(self):
        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, 1, 1, ?, ?)
                """,
                [
                    ("Untitled", None, 0),
                    ("Untitled Project", None, 0),
                    ("Untitled", 2, 61),
                ],
            )
            conn.commit()

        result = tracker.cleanup_untitled_sessions()
        self.assertEqual(result["deleted"], 1)

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT project_name FROM sessions ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual(names, ["Untitled", "Untitled Project"])

    def test_untitled_to_saved_renames_session(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t._start("Untitled")
        untitled_id = t.session_id
        self.assertEqual(t.project_name, "Untitled")

        with patch.object(tracker.time, "time", return_value=1010.0), \
             patch.object(tracker.time, "monotonic", return_value=20.0):
            t._rename("Real Song")

        self.assertEqual(t.session_id, untitled_id)
        self.assertEqual(t.project_name, "Real Song")

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT project_name, active_seconds FROM sessions WHERE id=?",
                (untitled_id,),
            ).fetchone()
        self.assertEqual(row[0], "Real Song")

    def test_poll_once_renames_when_untitled_becomes_saved(self):
        project_state = {"name": "Untitled"}

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", side_effect=lambda cp=None: project_state["name"]), \
             patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t = tracker.Tracker()
            t.poll_once(paused=False)
        untitled_id = t.session_id
        self.assertEqual(t.project_name, "Untitled")

        project_state["name"] = "Real Song"
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", side_effect=lambda cp=None: project_state["name"]), \
             patch.object(tracker.time, "time", return_value=1010.0), \
             patch.object(tracker.time, "monotonic", return_value=20.0):
            t.poll_once(paused=False)

        self.assertEqual(t.session_id, untitled_id)
        self.assertEqual(t.project_name, "Real Song")
        self.assertEqual(t.status().state, tracker.STATE_TRACKING)

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT project_name FROM sessions WHERE id=?",
                (untitled_id,),
            ).fetchone()
        self.assertEqual(row[0], "Real Song")

    def test_close_deletes_untitled_when_not_preserving_hint(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t._start("Untitled")
        with patch.object(tracker.time, "time", return_value=1100.0), \
             patch.object(tracker.time, "monotonic", return_value=110.0):
            t._tick()
        sid = t.session_id

        with patch.object(tracker.time, "time", return_value=1120.0), \
             patch.object(tracker.time, "monotonic", return_value=130.0):
            t._close(preserve_resume_hint=False)

        self.assertIsNone(t.session_id)
        self.assertIsNone(t.resume_hint_project)

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        self.assertIsNone(row)

    def test_close_preserves_untitled_with_resume_hint(self):
        t = tracker.Tracker()
        with patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t._start("Untitled")
        with patch.object(tracker.time, "time", return_value=1100.0), \
             patch.object(tracker.time, "monotonic", return_value=110.0):
            t._tick()
        sid = t.session_id

        with patch.object(tracker.time, "time", return_value=1120.0), \
             patch.object(tracker.time, "monotonic", return_value=130.0):
            t._close(preserve_resume_hint=True)

        self.assertIsNone(t.session_id)
        self.assertEqual(t.resume_hint_project, "Untitled")

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id, end_time FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[1])

    def test_get_project_name_prefers_non_untitled_when_current_is_untitled(self):
        with patch.object(
            tracker,
            "get_live_window_titles",
            return_value=["Untitled", "Real Song - Ableton Live 12 Suite"],
        ):
            result = tracker.get_project_name("Untitled")
        self.assertEqual(result, "Real Song")

    def test_get_project_name_returns_none_when_multiple_saved_titles(self):
        with patch.object(
            tracker,
            "get_live_window_titles",
            return_value=["Untitled", "Real Song - Ableton Live 12 Suite", "Other Song - Ableton Live 12 Suite"],
        ):
            result = tracker.get_project_name("Untitled")
        self.assertIsNone(result)

    def test_get_project_name_keeps_untitled_if_no_saved_candidates(self):
        with patch.object(
            tracker,
            "get_live_window_titles",
            return_value=["Untitled"],
        ):
            result = tracker.get_project_name("Untitled")
        self.assertEqual(result, "Untitled")

    def test_poll_once_does_not_rename_when_multiple_saved_titles_visible(self):
        project_state = {"name": "Untitled"}

        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_project_name", side_effect=lambda cp=None: project_state["name"]), \
             patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t = tracker.Tracker()
            t.poll_once(paused=False)
        untitled_id = t.session_id

        project_state["name"] = None
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "is_audio_active", return_value=False), \
             patch.object(tracker, "get_project_name", side_effect=lambda cp=None: project_state["name"]), \
             patch.object(tracker.time, "time", return_value=1010.0), \
             patch.object(tracker.time, "monotonic", return_value=20.0):
            t.poll_once(paused=False)

        self.assertEqual(t.session_id, untitled_id)
        self.assertEqual(t.project_name, "Untitled")

    def test_ableton_closed_deletes_untitled_through_poll(self):
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "get_idle_seconds", return_value=0), \
             patch.object(tracker, "get_project_name", return_value="Untitled"), \
             patch.object(tracker.time, "time", return_value=1000.0), \
             patch.object(tracker.time, "monotonic", return_value=10.0):
            t = tracker.Tracker()
            t.poll_once(paused=False)
        sid = t.session_id

        self.assertEqual(t.project_name, "Untitled")

        with patch.object(tracker, "is_ableton_running", return_value=False), \
             patch.object(tracker.time, "time", return_value=1100.0), \
             patch.object(tracker.time, "monotonic", return_value=110.0):
            t.poll_once(paused=False)

        self.assertIsNone(t.session_id)
        self.assertEqual(t.status().state, tracker.STATE_ABLETON_CLOSED)

        with closing(tracker.sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
