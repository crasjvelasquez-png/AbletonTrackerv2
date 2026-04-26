#!/usr/bin/env python3
"""Ableton Tracker — menu bar app with embedded tracker daemon."""

import sys
import time
import signal
import threading
import subprocess
import webbrowser
from datetime import date
from pathlib import Path

import rumps

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from tracker import (
    STATE_ABLETON_CLOSED,
    STATE_ABLETON_OPEN,
    STATE_IDLE_PAUSED,
    STATE_PAUSED,
    STATE_TRACKING,
    day_seconds,
    setup_db,
    Tracker,
    close_stale_open_sessions,
)

DASHBOARD_PORT = 7421
DASHBOARD_URL = f"http://localhost:{DASHBOARD_PORT}"
APP_DIR = Path(__file__).resolve().parent
PAUSE_FILE = Path.home() / ".ableton_tracker" / "paused"
REFRESH_INTERVAL = 5
TRACKER_WAKE_INTERVAL = 5


def fmt_dur(seconds: float) -> str:
    s = int(seconds or 0)
    if s < 60:
        return "0m"
    h, m = divmod(s // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


_QUARTER_GLYPH = {0: "", 1: "¼", 2: "½", 3: "¾"}


def fmt_quarter(seconds: float) -> str:
    """Round seconds to the nearest quarter hour, render as e.g. '1¾'. Empty if zero."""
    quarters = round((seconds or 0) / 900)
    if quarters == 0:
        return ""
    whole, frac = divmod(quarters, 4)
    if whole and frac:
        return f"{whole}{_QUARTER_GLYPH[frac]}"
    if whole:
        return str(whole)
    return _QUARTER_GLYPH[frac]


def today_seconds() -> float:
    try:
        return day_seconds(date.today())
    except Exception:
        return 0


class TrackerThread(threading.Thread):
    """Runs the tracker loop; respects a pause flag."""

    def __init__(self):
        super().__init__(daemon=True)
        self.tracker = Tracker()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        setup_db()
        stale = close_stale_open_sessions()
        if stale:
            print(f"closed {stale} stale open session{'s' if stale != 1 else ''}")
        self.tracker.maybe_run_cleanup(force=True)

    def stop(self):
        self._stop.set()
        with self._lock:
            self.tracker._close()

    def poll_now(self, paused: bool):
        with self._lock:
            self.tracker.poll_once(paused=paused)

    def current_project(self) -> str | None:
        with self._lock:
            return self.tracker.project_name

    def status(self):
        with self._lock:
            return self.tracker.status()

    def run(self):
        while not self._stop.is_set():
            try:
                self.poll_now(paused=PAUSE_FILE.exists())
                self.tracker.maybe_run_cleanup()
            except Exception as e:
                print(f"tracker error: {e}", file=sys.stderr)
            self._stop.wait(TRACKER_WAKE_INTERVAL)


class DashboardProcess:
    """Lazily spawns the dashboard HTTP server as a subprocess."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def open(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [sys.executable, str(APP_DIR / "dashboard.py")],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=APP_DIR,
            )
            time.sleep(0.8)  # let server bind before browser hits it
        webbrowser.open(DASHBOARD_URL)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class AbletonTrackerApp(rumps.App):
    def __init__(self):
        super().__init__("●", quit_button=None)
        self.icon = None
        self.tracker_thread = TrackerThread()
        self.dashboard = DashboardProcess()

        self.status_item = rumps.MenuItem("Idle")
        self.status_item.set_callback(None)
        self.today_item = rumps.MenuItem("Today: 0m")
        self.today_item.set_callback(None)
        self.pause_item = rumps.MenuItem(
            "Pause tracking" if not PAUSE_FILE.exists() else "Resume tracking",
            callback=self.toggle_pause,
        )

        self.menu = [
            self.status_item,
            self.today_item,
            None,
            rumps.MenuItem("Open dashboard", callback=self.open_dashboard),
            self.pause_item,
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        self.tracker_thread.start()
        self.refresh_timer = rumps.Timer(self._refresh, REFRESH_INTERVAL)
        self.refresh_timer.start()
        self._refresh(None)

    def _refresh(self, _):
        paused = PAUSE_FILE.exists()
        self.pause_item.title = "Resume tracking" if paused else "Pause tracking"
        status = self.tracker_thread.status()
        today = today_seconds()
        frac = fmt_quarter(today)
        suffix = f" {frac}" if frac else ""

        if status.state == STATE_PAUSED:
            self.title = f"⏸{suffix}"
            self.status_item.title = "Paused"
        elif status.state == STATE_IDLE_PAUSED:
            self.title = f"⏸{suffix}"
            idle_seconds = int(status.hid_idle_seconds)
            if idle_seconds < 60:
                idle_label = f"{idle_seconds}s"
            else:
                idle_label = f"{max(1, idle_seconds // 60)}m"
            project = status.resume_hint_project or "last project"
            self.status_item.title = f"Paused: idle {idle_label} - {project}"
        elif status.state == STATE_TRACKING and status.project_name:
            self.title = f"●{suffix}"
            self.status_item.title = f"Recording: {status.project_name}"
        elif status.state == STATE_ABLETON_OPEN:
            self.title = f"◐{suffix}"
            self.status_item.title = "Ableton open (no project)"
        elif status.state == STATE_ABLETON_CLOSED:
            self.title = f"○{suffix}"
            self.status_item.title = "Ableton not running"
        else:
            self.title = f"○{suffix}"
            self.status_item.title = "Idle"

        self.today_item.title = f"Today: {fmt_dur(today)}"

    def open_dashboard(self, _):
        self.dashboard.open()

    def toggle_pause(self, sender):
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
            sender.title = "Pause tracking"
            self.tracker_thread.poll_now(paused=False)
        else:
            PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            PAUSE_FILE.touch()
            sender.title = "Resume tracking"
            self.tracker_thread.poll_now(paused=True)
        self._refresh(None)

    def quit_app(self, _):
        self.tracker_thread.stop()
        self.dashboard.stop()
        rumps.quit_application()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    AbletonTrackerApp().run()
