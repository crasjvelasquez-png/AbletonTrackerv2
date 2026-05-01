import os
import sys
import tempfile
import unittest
import threading
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

# Provide a stable rumps mock module so menubar.py can import cleanly.
class _RumpsApp:
    def __init__(self, *args, **kwargs):
        pass

    def run(self):
        pass

class _RumpsMenuItem:
    def __init__(self, title, callback=None):
        self.title = title
        self.callback = callback

    def set_callback(self, callback):
        self.callback = callback

class _RumpsTimer:
    def __init__(self, callback, interval):
        self.callback = callback
        self.interval = interval

    def start(self):
        pass

    def stop(self):
        pass

class _RumpsModule:
    App = _RumpsApp
    MenuItem = _RumpsMenuItem
    Timer = _RumpsTimer

    @staticmethod
    def quit_application():
        pass

    def __getattr__(self, name):
        raise AttributeError(name)

sys.modules["rumps"] = _RumpsModule()

import menubar
from menubar import (
    fmt_dur,
    fmt_quarter,
    today_seconds,
    TrackerThread,
    DashboardProcess,
    AbletonTrackerApp,
    PAUSE_FILE,
    TRACKER_WAKE_INTERVAL,
    DASHBOARD_URL,
    APP_DIR,
)
import tracker


# ---------------------------------------------------------------------------
# fmt_dur / fmt_quarter — pure functions
# ---------------------------------------------------------------------------

class FmtDurTests(unittest.TestCase):
    def test_zero_seconds(self):
        self.assertEqual(fmt_dur(0), "0m")

    def test_less_than_60_seconds(self):
        self.assertEqual(fmt_dur(30), "0m")
        self.assertEqual(fmt_dur(59), "0m")

    def test_exactly_1_minute(self):
        self.assertEqual(fmt_dur(60), "1m")

    def test_minutes_only(self):
        self.assertEqual(fmt_dur(120), "2m")
        self.assertEqual(fmt_dur(3540), "59m")

    def test_hours_and_minutes(self):
        self.assertEqual(fmt_dur(3600), "1h 0m")
        self.assertEqual(fmt_dur(3660), "1h 1m")
        self.assertEqual(fmt_dur(7200), "2h 0m")
        self.assertEqual(fmt_dur(9000), "2h 30m")

    def test_none_input(self):
        self.assertEqual(fmt_dur(None), "0m")


class FmtQuarterTests(unittest.TestCase):
    def test_zero_seconds(self):
        self.assertEqual(fmt_quarter(0), "")

    def test_below_one_quarter(self):
        self.assertEqual(fmt_quarter(100), "")
        self.assertEqual(fmt_quarter(449), "")

    def test_one_quarter_at_15_min(self):
        self.assertEqual(fmt_quarter(900), "\u00bc")

    def test_half_at_30_min(self):
        self.assertEqual(fmt_quarter(1800), "\u00bd")

    def test_three_quarters_at_45_min(self):
        self.assertEqual(fmt_quarter(2700), "\u00be")

    def test_one_hour(self):
        self.assertEqual(fmt_quarter(3600), "1")

    def test_one_and_half_hours(self):
        self.assertEqual(fmt_quarter(5400), "1\u00bd")

    def test_one_and_three_quarters(self):
        self.assertEqual(fmt_quarter(6300), "1\u00be")

    def test_two_hours(self):
        self.assertEqual(fmt_quarter(7200), "2")

    def test_two_and_a_quarter(self):
        self.assertEqual(fmt_quarter(8100), "2\u00bc")

    def test_banker_rounding_down_at_half_quarter(self):
        # 675 / 900 = 0.75 -> round(0.75) = 1 -> 1/4
        self.assertEqual(fmt_quarter(675), "\u00bc")

    def test_none_input(self):
        self.assertEqual(fmt_quarter(None), "")


# ---------------------------------------------------------------------------
# today_seconds
# ---------------------------------------------------------------------------

class TodaySecondsTests(unittest.TestCase):
    def test_returns_day_seconds_for_today(self):
        frozen = date(2026, 5, 1)

        class FakeDate:
            @staticmethod
            def today():
                return frozen

        with patch.object(menubar, "day_seconds", return_value=3600.0) as mock_ds, \
             patch.object(menubar, "date", FakeDate):
            result = today_seconds()
        mock_ds.assert_called_once_with(frozen)
        self.assertEqual(result, 3600.0)

    def test_returns_zero_on_exception(self):
        with patch.object(menubar, "day_seconds", side_effect=RuntimeError("boom")):
            result = today_seconds()
        self.assertEqual(result, 0)


# ---------------------------------------------------------------------------
# TrackerThread — __init__ and method delegation
# ---------------------------------------------------------------------------

class TrackerThreadInitTests(unittest.TestCase):
    """TrackerThread.__init__ creates a Tracker instance.  We prevent the
    real Tracker.__init__ from touching the DB by mocking its side effects."""

    def setUp(self):
        self._patchers = [
            patch.object(tracker, "setup_db"),
            patch.object(tracker, "close_stale_open_sessions", return_value=0),
            patch.object(tracker.Tracker, "maybe_run_cleanup"),
            patch.object(tracker, "_ensure_audio_probe_binary", return_value="/fake/probe"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()

    def test_init_creates_daemon_thread_with_tracker(self):
        thread = TrackerThread()
        self.assertTrue(thread.daemon)
        self.assertIsInstance(thread.tracker, tracker.Tracker)

    def test_init_sets_lock_and_stop_event(self):
        thread = TrackerThread()
        self.assertIsInstance(thread._lock, type(threading.Lock()))
        self.assertIsInstance(thread._stop, type(threading.Event()))
        self.assertFalse(thread._stop.is_set())


class TrackerThreadMethodsTests(unittest.TestCase):
    def setUp(self):
        for p in [
            patch.object(tracker, "setup_db"),
            patch.object(tracker, "close_stale_open_sessions", return_value=0),
            patch.object(tracker.Tracker, "maybe_run_cleanup"),
            patch.object(tracker, "_ensure_audio_probe_binary", return_value="/fake/probe"),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.thread = TrackerThread()
        # Fully replace the real tracker with a mock so we control responses.
        self.thread.tracker = MagicMock()

    def test_poll_now_paused_delegates(self):
        self.thread.poll_now(paused=True)
        self.thread.tracker.poll_once.assert_called_once_with(paused=True)

    def test_poll_now_not_paused_delegates(self):
        self.thread.poll_now(paused=False)
        self.thread.tracker.poll_once.assert_called_once_with(paused=False)

    def test_stop_sets_event_and_calls_close(self):
        self.thread.tracker._close = MagicMock()
        self.thread.stop()
        self.assertTrue(self.thread._stop.is_set())
        self.thread.tracker._close.assert_called_once()

    def test_current_project_returns_name(self):
        self.thread.tracker.project_name = "My Song"
        self.assertEqual(self.thread.current_project(), "My Song")

    def test_current_project_returns_none(self):
        self.thread.tracker.project_name = None
        self.assertIsNone(self.thread.current_project())

    def test_status_returns_tracker_status(self):
        fake_status = MagicMock()
        self.thread.tracker.status = MagicMock(return_value=fake_status)
        self.assertEqual(self.thread.status(), fake_status)

    def test_consecutive_failures_delegates_to_tracker(self):
        self.thread.tracker._consecutive_failures = 5
        self.assertEqual(self.thread.consecutive_failures, 5)

    def test_last_error_delegates_to_tracker(self):
        self.thread.tracker._last_error = "uh oh"
        self.assertEqual(self.thread.last_error, "uh oh")


class TrackerThreadRunTests(unittest.TestCase):
    def setUp(self):
        for p in [
            patch.object(tracker, "setup_db"),
            patch.object(tracker, "close_stale_open_sessions", return_value=0),
            patch.object(tracker.Tracker, "maybe_run_cleanup"),
            patch.object(tracker, "_ensure_audio_probe_binary", return_value="/fake/probe"),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.thread = TrackerThread()
        self.thread.tracker = MagicMock()

    def test_run_delegates_to_tracker_run(self):
        self.thread.run()
        self.thread.tracker.run.assert_called_once_with(
            stop_event=self.thread._stop,
            wake_interval=TRACKER_WAKE_INTERVAL,
            _lock=self.thread._lock,
        )


# ---------------------------------------------------------------------------
# DashboardProcess
# ---------------------------------------------------------------------------

class DashboardProcessTests(unittest.TestCase):
    def setUp(self):
        self.dp = DashboardProcess()

    def test_open_spawns_when_none_exists(self):
        with patch.object(menubar.subprocess, "Popen") as mock_popen, \
             patch.object(menubar.webbrowser, "open") as mock_browser:
            mock_popen.return_value.poll.return_value = None
            self.dp.open()
        mock_popen.assert_called_once()
        mock_browser.assert_called_once_with(DASHBOARD_URL)

    def test_open_reuses_running_process(self):
        proc = MagicMock()
        proc.poll.return_value = None
        self.dp.proc = proc
        with patch.object(menubar.subprocess, "Popen") as mock_popen, \
             patch.object(menubar.webbrowser, "open") as mock_browser:
            self.dp.open()
        mock_popen.assert_not_called()
        mock_browser.assert_called_once_with(DASHBOARD_URL)

    def test_open_restarts_dead_process(self):
        proc = MagicMock()
        proc.poll.return_value = 0
        self.dp.proc = proc
        with patch.object(menubar.subprocess, "Popen") as mock_popen, \
             patch.object(menubar.webbrowser, "open") as mock_browser:
            self.dp.open()
        mock_popen.assert_called_once()
        mock_browser.assert_called_once_with(DASHBOARD_URL)

    def test_stop_terminates_running(self):
        proc = MagicMock()
        proc.poll.return_value = None
        self.dp.proc = proc
        self.dp.stop()
        proc.terminate.assert_called_once()

    def test_stop_ignores_none_proc(self):
        self.dp.proc = None
        self.dp.stop()

    def test_stop_ignores_already_dead(self):
        proc = MagicMock()
        proc.poll.return_value = 0
        self.dp.proc = proc
        self.dp.stop()
        proc.terminate.assert_not_called()

    def test_open_passes_correct_args(self):
        with patch.object(menubar.subprocess, "Popen") as mock_popen, \
             patch.object(menubar.webbrowser, "open"):
            self.dp.open()
        args = mock_popen.call_args[0][0]
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[1], str(APP_DIR / "dashboard.py"))
        self.assertEqual(mock_popen.call_args[1]["cwd"], APP_DIR)


# ---------------------------------------------------------------------------
# AbletonTrackerApp tests
# ---------------------------------------------------------------------------

class _AppTestBase(unittest.TestCase):
    """Shared setUp / tearDown for AbletonTrackerApp tests."""

    def setUp(self):
        # Prevent Tracker.__init__ from hitting the real DB / filesystem.
        self._patchers = [
            patch.object(tracker, "setup_db"),
            patch.object(tracker, "close_stale_open_sessions", return_value=0),
            patch.object(tracker.Tracker, "maybe_run_cleanup"),
            patch.object(tracker, "_ensure_audio_probe_binary", return_value="/fake/probe"),
        ]
        for p in self._patchers:
            p.start()

        # Don't actually launch a thread.
        self._thread_start = patch.object(TrackerThread, "start")
        self._thread_start.start()

        # Don't create real rumps Timer objects.
        self._timer_patch = patch.object(menubar.rumps, "Timer")
        self._timer_patch.start()

    def tearDown(self):
        self._timer_patch.stop()
        self._thread_start.stop()
        for p in reversed(self._patchers):
            p.stop()


class AbletonTrackerAppPauseTests(_AppTestBase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pause_dir = Path(self._tmpdir.name) / ".ableton_tracker"
        self._pause_file = self._pause_dir / "paused"
        self._pause_patch = patch.object(menubar, "PAUSE_FILE", self._pause_file)
        self._pause_patch.start()
        super().setUp()
        self.app = AbletonTrackerApp()
        self.app._refresh = MagicMock()

    def tearDown(self):
        super().tearDown()
        self._pause_patch.stop()
        self._tmpdir.cleanup()

    def test_toggle_pause_creates_pause_file(self):
        self.assertFalse(self._pause_file.exists())
        sender = MagicMock()
        self.app.toggle_pause(sender)
        self.assertTrue(self._pause_file.exists())
        self.app._refresh.assert_called_once_with(None)

    def test_toggle_pause_removes_pause_file(self):
        self._pause_dir.mkdir(parents=True, exist_ok=True)
        self._pause_file.touch()
        sender = MagicMock()
        self.app.toggle_pause(sender)
        self.assertFalse(self._pause_file.exists())
        self.app._refresh.assert_called_once_with(None)

    def test_toggle_pause_calls_poll_now_paused_then_unpaused(self):
        self.app.tracker_thread.poll_now = MagicMock()
        sender = MagicMock()

        # First toggle: not paused → pause it
        self.app.toggle_pause(sender)
        self.app.tracker_thread.poll_now.assert_called_once_with(paused=True)

        # Second toggle: paused → unpause
        self.app.tracker_thread.poll_now.reset_mock()
        self.app.toggle_pause(sender)
        self.app.tracker_thread.poll_now.assert_called_once_with(paused=False)


class AbletonTrackerAppRefreshTests(_AppTestBase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pause_dir = Path(self._tmpdir.name) / ".ableton_tracker"
        self._pause_file = self._pause_dir / "paused"
        self._pause_patch = patch.object(menubar, "PAUSE_FILE", self._pause_file)
        self._pause_patch.start()
        super().setUp()
        self.app = AbletonTrackerApp()
        # Replace status on the tracker_thread with a mock so we can control responses.
        self.app.tracker_thread.status = MagicMock()
        # Set error state attributes on the underlying tracker (read-only properties).
        self.app.tracker_thread.tracker._consecutive_failures = 0
        self.app.tracker_thread.tracker._last_error = None

    def tearDown(self):
        super().tearDown()
        self._pause_patch.stop()
        self._tmpdir.cleanup()

    def _make_status(self, state, project_name=None, resume_hint_project=None,
                     hid_idle_seconds=0.0):
        s = tracker.TrackerStatus()
        return replace(s,
                       state=state,
                       project_name=project_name,
                       resume_hint_project=resume_hint_project,
                       hid_idle_seconds=hid_idle_seconds)

    def _touch_pause(self):
        self._pause_dir.mkdir(parents=True, exist_ok=True)
        self._pause_file.touch()

    def test_refresh_shows_paused(self):
        self._touch_pause()
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_PAUSED)
        with patch.object(menubar, "day_seconds", return_value=0):
            self.app._refresh(None)
        self.assertEqual(self.app.pause_item.title, "Resume tracking")
        self.assertEqual(self.app.status_item.title, "Paused")

    def test_refresh_shows_idle_paused(self):
        self._touch_pause()
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_IDLE_PAUSED,
            resume_hint_project="My Song",
            hid_idle_seconds=45)
        with patch.object(menubar, "day_seconds", return_value=0):
            self.app._refresh(None)
        self.assertIn("Paused: idle", self.app.status_item.title)
        self.assertIn("My Song", self.app.status_item.title)

    def test_refresh_idle_paused_seconds_label(self):
        self._touch_pause()
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_IDLE_PAUSED,
            resume_hint_project="Song",
            hid_idle_seconds=120)
        with patch.object(menubar, "day_seconds", return_value=0):
            self.app._refresh(None)
        self.assertIn("2m", self.app.status_item.title)

    def test_refresh_shows_tracking(self):
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_TRACKING, project_name="Real Song")
        with patch.object(menubar, "day_seconds", return_value=0):
            self.app._refresh(None)
        self.assertIn("Real Song", self.app.status_item.title)

    def test_refresh_shows_ableton_open(self):
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_ABLETON_OPEN)
        with patch.object(menubar, "day_seconds", return_value=0):
            self.app._refresh(None)
        self.assertIn("Ableton open", self.app.status_item.title)

    def test_refresh_shows_ableton_closed(self):
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_ABLETON_CLOSED)
        with patch.object(menubar, "day_seconds", return_value=0):
            self.app._refresh(None)
        self.assertIn("Ableton not running", self.app.status_item.title)

    def test_refresh_shows_error(self):
        self.app.tracker_thread.tracker._consecutive_failures = 3
        self.app.tracker_thread.tracker._last_error = "Something broke badly"
        with patch.object(menubar, "day_seconds", return_value=1800):
            self.app._refresh(None)
        self.assertEqual(self.app.title, "\u26a0")
        self.assertIn("Error (3): Something broke badly", self.app.status_item.title)
        self.assertIn("Today: 30m", self.app.today_item.title)

    def test_refresh_today_item_formatted(self):
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_TRACKING, project_name="Song")
        with patch.object(menubar, "day_seconds", return_value=3660):
            self.app._refresh(None)
        self.assertIn("1h 1m", self.app.today_item.title)

    def test_refresh_title_includes_quarter(self):
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_TRACKING, project_name="Song")
        with patch.object(menubar, "day_seconds", return_value=5400):
            self.app._refresh(None)
        self.assertIn("1\u00bd", self.app.title)

    def test_refresh_title_no_quarter_when_zero(self):
        self.app.tracker_thread.status.return_value = self._make_status(
            tracker.STATE_TRACKING, project_name="Song")
        with patch.object(menubar, "day_seconds", return_value=0):
            self.app._refresh(None)
        self.assertNotIn("\u00bc", self.app.title)
        self.assertNotIn("\u00bd", self.app.title)
        self.assertNotIn("\u00be", self.app.title)


class AbletonTrackerAppQuitTests(_AppTestBase):
    def setUp(self):
        super().setUp()
        self.app = AbletonTrackerApp()

    def test_quit_app_stops_tracker_thread(self):
        self.app.tracker_thread.stop = MagicMock()
        self.app.quit_app(None)
        self.app.tracker_thread.stop.assert_called_once()

    def test_quit_app_stops_dashboard(self):
        self.app.tracker_thread.stop = MagicMock()
        self.app.dashboard.stop = MagicMock()
        self.app.quit_app(None)
        self.app.dashboard.stop.assert_called_once()

    def test_quit_app_calls_rumps_quit(self):
        self.app.tracker_thread.stop = MagicMock()
        self.app.dashboard.stop = MagicMock()
        with patch.object(menubar.rumps, "quit_application") as mock_quit:
            self.app.quit_app(None)
        mock_quit.assert_called_once()


class AbletonTrackerAppInitTests(_AppTestBase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pause_dir = Path(self._tmpdir.name) / ".ableton_tracker"
        self._pause_file = self._pause_dir / "paused"
        self._pause_patch = patch.object(menubar, "PAUSE_FILE", self._pause_file)
        self._pause_patch.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._pause_patch.stop()
        self._tmpdir.cleanup()

    def test_init_resume_tracking_when_paused(self):
        self._pause_dir.mkdir(parents=True, exist_ok=True)
        self._pause_file.touch()
        app = AbletonTrackerApp()
        self.assertEqual(app.pause_item.title, "Resume tracking")

    def test_init_pause_tracking_when_not_paused(self):
        app = AbletonTrackerApp()
        self.assertEqual(app.pause_item.title, "Pause tracking")


if __name__ == "__main__":
    unittest.main()
