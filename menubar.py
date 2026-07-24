#!/usr/bin/env python3
"""Ableton Tracker — menu bar app with embedded tracker daemon."""

import sys
import time
import signal
import threading
import subprocess
import os
from datetime import date, datetime
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
    is_ableton_running,
    streak_days,
    week_seconds,
    yesterday_seconds,
    Tracker,
)
from notifications import NotificationCoordinator, NotificationMessage

DASHBOARD_PORT = 7421
DASHBOARD_URL = f"http://localhost:{DASHBOARD_PORT}"
APP_DIR = Path(__file__).resolve().parent
DASHBOARD_WINDOW_SCRIPT = APP_DIR / "dashboard_window.py"
PAUSE_FILE = Path.home() / ".ableton_tracker" / "paused"
REFRESH_INTERVAL = 5
TRACKER_WAKE_INTERVAL = 5
NOTIFICATION_CHECK_INTERVAL = 5 * 60
NOTIFICATION_STATE_PATH = Path.home() / ".ableton_tracker" / "notification_state.json"


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


def fmt_goal_time(seconds: float, goal_hours: float | None) -> str:
    """Format time with optional goal suffix like '1¾/3h'."""
    frac = fmt_quarter(seconds)
    if goal_hours is not None and goal_hours > 0:
        g = f"{int(goal_hours)}h" if goal_hours == int(goal_hours) else f"{goal_hours}h"
        prefix = frac if frac else "0m"
        return f"{prefix}/{g}"
    return frac if frac else "0m"


def today_seconds() -> float:
    try:
        return day_seconds(date.today())
    except Exception:
        return 0


def _get_default_goals() -> tuple[float | None, float | None]:
    try:
        from dashboard import get_app_setting
        daily = get_app_setting("default_daily_goal_hours")
        weekly = get_app_setting("default_weekly_goal_hours")
        dh = float(daily) if daily else None
        wh = float(weekly) if weekly else None
        return dh, wh
    except Exception:
        return None, None


def _find_ableton_app() -> str | None:
    from glob import glob
    candidates = sorted(glob("/Applications/Ableton Live *.app"))
    if candidates:
        return candidates[-1]
    if Path("/Applications/Ableton Live.app").exists():
        return "/Applications/Ableton Live.app"
    return None


class TrackerThread(threading.Thread):
    """Runs the tracker loop in a background thread; respects the pause flag."""

    def __init__(self):
        super().__init__(daemon=True)
        self.tracker = Tracker()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self.tracker._consecutive_failures

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self.tracker._last_error

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
        self.tracker.run(
            stop_event=self._stop,
            wake_interval=TRACKER_WAKE_INTERVAL,
            _lock=self._lock,
        )


class DashboardProcess:
    """Keeps the dashboard server warm and opens the embedded window on demand."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.server_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def prewarm(self):
        if "unittest" in sys.modules:
            return
        threading.Thread(target=self.start_server, daemon=True).start()

    def start_server(self):
        with self._lock:
            if self.server_proc is not None and self.server_proc.poll() is None:
                return
            env = os.environ.copy()
            env["ABLETON_TRACKER_NO_BROWSER"] = "1"
            self.server_proc = subprocess.Popen(
                [sys.executable, str(APP_DIR / "dashboard.py")],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=APP_DIR,
                env=env,
            )

    def open(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [sys.executable, str(DASHBOARD_WINDOW_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=APP_DIR,
            )

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.server_proc and self.server_proc.poll() is None:
            self.server_proc.terminate()


class AbletonTrackerApp(rumps.App):
    def __init__(self):
        super().__init__("●", quit_button=None)
        self.icon = None
        self.tracker_thread = TrackerThread()
        self.dashboard = DashboardProcess()
        self.notification_coordinator = NotificationCoordinator(
            db_path=Path.home() / ".ableton_tracker" / "sessions.db",
            state_path=NOTIFICATION_STATE_PATH,
            enabled="unittest" not in sys.modules,
        )
        self._next_notification_check = 0.0

        self.open_ableton_item = rumps.MenuItem(
            "Open Ableton", callback=self.open_ableton
        )
        self.status_item = rumps.MenuItem("Idle")
        self.status_item.set_callback(None)
        self.today_item = rumps.MenuItem("Today: 0m")
        self.today_item.set_callback(None)
        self.yesterday_item = rumps.MenuItem("Yesterday: —")
        self.yesterday_item.set_callback(None)
        self.week_item = rumps.MenuItem("This week: 0m")
        self.week_item.set_callback(None)
        self.streak_item = rumps.MenuItem("Streak: —")
        self.streak_item.set_callback(None)
        self.pause_item = rumps.MenuItem(
            "Pause tracking" if not PAUSE_FILE.exists() else "Resume tracking",
            callback=self.toggle_pause,
            key="p",
        )

        self.menu = [
            self.open_ableton_item,
            None,
            self.status_item,
            self.today_item,
            self.yesterday_item,
            self.week_item,
            self.streak_item,
            None,
            rumps.MenuItem("Open Dashboard", callback=self.open_dashboard, key="d"),
            self.pause_item,
            None,
            rumps.MenuItem("Quit", callback=self.quit_app, key="q"),
        ]

        self.tracker_thread.start()
        self.dashboard.prewarm()
        self.refresh_timer = rumps.Timer(self._refresh, REFRESH_INTERVAL)
        self.refresh_timer.start()
        self._refresh(None)

    def _refresh(self, _):
        paused = PAUSE_FILE.exists()
        self.pause_item.title = "Resume tracking" if paused else "Pause tracking"

        daily_goal, weekly_goal = _get_default_goals()
        today = today_seconds()
        yesterday = yesterday_seconds()
        streak = 0
        week = 0.0
        try:
            streak = streak_days()
            week = week_seconds()
        except Exception as e:
            print(f"[refresh] streak/week error: {e}", file=sys.stderr)

        self._check_notifications(
            today=today,
            week=week,
            weekly_goal=weekly_goal,
            streak=streak,
        )

        failures = self.tracker_thread.consecutive_failures
        last_error = self.tracker_thread.last_error
        if failures > 0:
            self.title = self._build_title("⚠", today, daily_goal, streak, active=False)
            trunc = (last_error or "unknown")[:60]
            self.status_item.title = f"Error ({failures}): {trunc}"
            self.today_item.title = f"Today: {fmt_goal_time(today, daily_goal)}"
            self.yesterday_item.title = f"Yesterday: {fmt_dur(yesterday)}"
            self.week_item.title = f"This week: {fmt_goal_time(week, weekly_goal)}"
            self._update_streak(streak)
            self._update_open_ableton(False)
            return

        status = self.tracker_thread.status()
        ableton_up = status.state not in (STATE_ABLETON_CLOSED,)

        if status.state == STATE_PAUSED:
            self.title = self._build_title("○", today, daily_goal, streak, active=False)
            self.status_item.title = "Paused"
        elif status.state == STATE_IDLE_PAUSED:
            self.title = self._build_title("○", today, daily_goal, streak, active=False)
            idle_seconds = int(status.hid_idle_seconds)
            if idle_seconds < 60:
                idle_label = f"{idle_seconds}s"
            else:
                idle_label = f"{max(1, idle_seconds // 60)}m"
            project = status.resume_hint_project or "last project"
            self.status_item.title = f"Idle {idle_label} — {project}"
        elif status.state == STATE_TRACKING and status.project_name:
            self.title = self._build_title("●", today, daily_goal, streak, active=True)
            self.status_item.title = f"Working on: {status.project_name}"
        elif status.state == STATE_ABLETON_OPEN:
            self.title = self._build_title("◐", today, daily_goal, streak, active=False)
            self.status_item.title = "Ableton open (no project)"
        elif status.state == STATE_ABLETON_CLOSED:
            self.title = self._build_title("○", today, daily_goal, streak, active=False)
            self.status_item.title = "Ableton not running"
        else:
            self.title = self._build_title("○", today, daily_goal, streak, active=False)
            self.status_item.title = "Idle"

        self.today_item.title = f"Today: {fmt_goal_time(today, daily_goal)}"
        self.yesterday_item.title = f"Yesterday: {fmt_dur(yesterday)}"
        self.week_item.title = f"This week: {fmt_goal_time(week, weekly_goal)}"
        self._update_streak(streak)
        self._update_open_ableton(not ableton_up)

    def _check_notifications(
        self,
        *,
        today: float,
        week: float,
        weekly_goal: float | None,
        streak: int,
    ) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic < self._next_notification_check:
            return
        self._next_notification_check = now_monotonic + NOTIFICATION_CHECK_INTERVAL
        try:
            self.notification_coordinator.check(
                now=datetime.now(),
                today_seconds=today,
                week_seconds=week,
                weekly_goal_hours=weekly_goal,
                streak_days=streak,
                deliver=self._deliver_notification,
            )
        except Exception as exc:
            print(f"[notifications] check error: {exc}", file=sys.stderr)

    @staticmethod
    def _deliver_notification(notification: NotificationMessage) -> None:
        rumps.notification(
            notification.title,
            notification.subtitle,
            notification.message,
            sound=False,
        )

    def _build_title(
        self, icon: str, seconds: float, goal: float | None, streak: int, active: bool
    ) -> str:
        time_str = fmt_goal_time(seconds, goal)
        parts = [icon, time_str]
        if goal is not None and goal > 0 and (seconds or 0) >= goal * 3600:
            parts.append("✓")
        if streak > 0:
            parts.append(f"💿{streak}")
        return " ".join(parts)

    def _update_streak(self, streak: int):
        if streak > 0:
            day_word = "day" if streak == 1 else "days"
            self.streak_item.title = f"💿 Streak: {streak} {day_word}"
        else:
            self.streak_item.title = "Streak: —"

    def _update_open_ableton(self, show: bool):
        if show:
            self.open_ableton_item.title = "Open Ableton"
            self.open_ableton_item.set_callback(self.open_ableton)
        else:
            self.open_ableton_item.title = "Ableton is running"
            self.open_ableton_item.set_callback(None)

    def open_ableton(self, _):
        app_path = _find_ableton_app()
        if app_path:
            subprocess.Popen(
                ["open", "-a", app_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["open", "-a", "Ableton Live"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

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
