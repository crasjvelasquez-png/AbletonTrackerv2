#!/usr/bin/env python3
"""Ableton Tracker — menu bar app with embedded tracker daemon."""

import sys
import time
import signal
import threading
import subprocess
import os
import fcntl
import math
import struct
import zlib
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
    production_run_days,
    week_seconds,
    yesterday_seconds,
    Tracker,
)
from notifications import NotificationCoordinator, NotificationMessage
from projects import ProjectCatalog, open_project

DASHBOARD_PORT = 7421
DASHBOARD_URL = f"http://localhost:{DASHBOARD_PORT}"
APP_DIR = Path(__file__).resolve().parent
DASHBOARD_WINDOW_SCRIPT = APP_DIR / "dashboard_window.py"
PAUSE_FILE = Path.home() / ".ableton_tracker" / "paused"
REFRESH_INTERVAL = 5
TRACKER_WAKE_INTERVAL = 5
NOTIFICATION_CHECK_INTERVAL = 30
NOTIFICATION_STATE_PATH = Path.home() / ".ableton_tracker" / "notification_state.json"
INSTANCE_LOCK_PATH = Path.home() / ".ableton_tracker" / "tracker-app.lock"
BADGE_DIR = Path.home() / ".ableton_tracker" / "menubar-badges"
BADGE_PX = 40  # rumps sizes status icons to 20pt; draw at 2x.
BADGE_STEPS = 24
BADGE_ACCENT = (164, 178, 246)
BADGE_GLYPHS = {"tracking": "●", "open": "◐", "idle": "○"}


def render_badge_png(state: str, progress: float) -> bytes:
    """Draw the minimal status badge: a thin goal ring, filled clockwise from 12."""
    progress = min(max(progress or 0.0, 0.0), 1.0)
    color = BADGE_ACCENT if state == "tracking" else (0, 0, 0)
    arc_alpha = 0.55 if state == "idle" else 1.0
    center, radius, half_stroke, dot = BADGE_PX / 2, 12.0, 1.5, 3.0
    sub = 4

    def alpha_at(x: float, y: float) -> float:
        dx, dy = x - center, y - center
        dist = math.hypot(dx, dy)
        if abs(dist - radius) <= half_stroke:
            angle = math.atan2(dx, -dy) % math.tau
            return arc_alpha if angle < progress * math.tau else 0.28
        if state == "tracking" and dist <= dot:
            return 1.0
        return 0.0

    rows = bytearray()
    for py in range(BADGE_PX):
        rows.append(0)
        for px in range(BADGE_PX):
            total = sum(
                alpha_at(px + (i + 0.5) / sub, py + (j + 0.5) / sub)
                for i in range(sub) for j in range(sub)
            )
            rows += bytes((*color, round(255 * total / (sub * sub))))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", BADGE_PX, BADGE_PX, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def acquire_instance_lock(path: Path = INSTANCE_LOCK_PATH):
    """Hold an exclusive lock for the lifetime of the Tracker menu-bar app."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


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
        self._stop_event = threading.Event()
        if sys.platform == 'darwin' and 'unittest' not in sys.modules:
            from activity_observer import NativeObserver
            self.tracker.observer = NativeObserver()
            self.tracker.observer.start()

    @property
    def consecutive_failures(self) -> int:
        return self.tracker._consecutive_failures

    @property
    def last_error(self) -> str | None:
        return self.tracker._last_error

    def stop(self):
        self._stop_event.set()
        if self.tracker.observer is not None:
            self.tracker.observer.wake.set()
        if self.is_alive():
            self.join(timeout=5)
        if not self.is_alive():
            self.tracker.shutdown()

    def poll_now(self, paused: bool):
        with self._lock:
            self.tracker.poll_once(paused=paused)

    def current_project(self) -> str | None:
        return self.tracker.status().project_name

    def status(self):
        return self.tracker.status()

    def run(self):
        self.tracker.run(
            stop_event=self._stop_event,
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
                [sys.executable, str(DASHBOARD_WINDOW_SCRIPT), "tracker"],
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
        # The manual pause control is hidden; do not strand an old paused run.
        try:
            PAUSE_FILE.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[startup] could not clear manual pause: {exc}", file=sys.stderr)
        from tracker import DB_PATH
        self.project_catalog = ProjectCatalog(DB_PATH)
        self._next_project_refresh = 0.0
        self._project_snapshot = None
        self.open_project_item = rumps.MenuItem("Open Project")
        self.open_project_item.add(rumps.MenuItem("Loading projects…"))
        self.open_project_item.add(None)
        self.open_project_item.add(rumps.MenuItem("Customize…", callback=self.customize_projects))

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
        self.menu = [
            self.open_ableton_item,
            self.status_item,
            self.today_item,
            self.week_item,
            self.streak_item,
            None,
            self.open_project_item,
            rumps.MenuItem("Open Tracker", callback=self.open_dashboard),
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]
        self.panel = None
        self._show_open_ableton = False
        # rumps creates its native status item immediately before this event.
        if hasattr(rumps, "events"):
            rumps.events.before_start.register(self._install_panel)

        self.tracker_thread.start()
        self.dashboard.prewarm()
        if os.environ.get("ABLETON_TRACKER_OPEN_WINDOW") == "1":
            self.dashboard.open()
        self.refresh_timer = rumps.Timer(self._refresh, REFRESH_INTERVAL)
        self.refresh_timer.start()
        self._refresh(None)

    def _refresh(self, _):
        self._refresh_projects()

        daily_goal, weekly_goal = _get_default_goals()
        from tracker import DB_PATH
        revision = []
        for path in (DB_PATH, Path(str(DB_PATH)+'-wal')):
            try:
                stat = path.stat()
                revision.append((stat.st_mtime_ns,stat.st_size))
            except FileNotFoundError:
                revision.append(None)
        key = (tuple(revision),date.today(),time.tzname)
        if getattr(self,'_totals_key',None) != key or 'unittest' in sys.modules:
            today = today_seconds()
            yesterday = yesterday_seconds()
            streak,week = 0,0.0
            try:
                streak = production_run_days()
                week = week_seconds()
            except Exception as e:
                print(f"[refresh] streak/week error: {e}",file=sys.stderr)
            self._totals = today,yesterday,streak,week
            self._totals_key = key
        today,yesterday,streak,week = self._totals

        self._check_notifications(
            today=today,
            week=week,
            weekly_goal=weekly_goal,
            daily_goal=daily_goal,
            streak=streak,
        )

        failures = self.tracker_thread.consecutive_failures
        last_error = self.tracker_thread.last_error
        if failures > 0:
            self.icon, self._badge_key = None, None
            self.title = f"⚠ {fmt_goal_time(today, None)}"
            trunc = (last_error or "unknown")[:60]
            self.status_item.title = f"Error ({failures}): {trunc}"
            self.today_item.title = f"Today: {fmt_goal_time(today, daily_goal)}"
            self.yesterday_item.title = f"Yesterday: {fmt_dur(yesterday)}"
            self.week_item.title = f"This week: {fmt_goal_time(week, weekly_goal)}"
            self._update_streak(streak)
            self._update_open_ableton(False)
            self._sync_panel("error")
            return

        status = self.tracker_thread.status()
        ableton_up = status.state not in (STATE_ABLETON_CLOSED,)

        if status.state == STATE_PAUSED:
            self.title = self._build_title("idle", today, daily_goal, streak, active=False)
            self.status_item.title = "Paused"
        elif status.state == STATE_IDLE_PAUSED:
            self.title = self._build_title("idle", today, daily_goal, streak, active=False)
            idle_seconds = int(status.hid_idle_seconds)
            if idle_seconds < 60:
                idle_label = f"{idle_seconds}s"
            else:
                idle_label = f"{max(1, idle_seconds // 60)}m"
            project = status.resume_hint_project or "last project"
            self.status_item.title = f"Idle {idle_label} — {project}"
        elif status.state == STATE_TRACKING and status.project_name:
            self.title = self._build_title("tracking", today, daily_goal, streak, active=True)
            self.status_item.title = f"Working on: {status.project_name}"
        elif status.state == STATE_ABLETON_OPEN:
            self.title = self._build_title("open", today, daily_goal, streak, active=False)
            self.status_item.title = "Ableton open (no project)"
        elif status.state == STATE_ABLETON_CLOSED:
            self.title = self._build_title("idle", today, daily_goal, streak, active=False)
            self.status_item.title = "Ableton not running"
        else:
            self.title = self._build_title("idle", today, daily_goal, streak, active=False)
            self.status_item.title = "Idle"
        if status.checked_at and status.message:
            self.status_item.title = status.message

        self.today_item.title = f"Today: {fmt_goal_time(today, daily_goal)}"
        self.yesterday_item.title = f"Yesterday: {fmt_dur(yesterday)}"
        self.week_item.title = f"This week: {fmt_goal_time(week, weekly_goal)}"
        self._update_streak(streak)
        self._update_open_ableton(not ableton_up)
        self._sync_panel("tracking" if status.state == STATE_TRACKING else "idle")

    def _install_panel(self):
        """Attach presentation only after rumps has initialized the native menu."""
        try:
            from menubar_panel import MenuBarPanel
            self.panel = MenuBarPanel.attach(
                self._nsapp.nsstatusitem,
                self.open_project_item._menu,
                lambda: self._refresh(None),
                {
                    "tracker": lambda: self.open_dashboard(None),
                    "ableton": lambda: self.open_ableton(None),
                    "quit": lambda: self.quit_app(None),
                },
            )
            self._refresh(None)
        except Exception as exc:
            if self.panel is not None:
                self.panel.dispose()
                self.panel = None
            print(f"[panel] using native menu fallback: {exc}", file=sys.stderr)

    def _sync_panel(self, state):
        if self.panel is None:
            return
        try:
            self.panel.update(
                status=self.status_item.title,
                today=self.today_item.title,
                week=self.week_item.title,
                run=self.streak_item.title,
                show_open_ableton=self._show_open_ableton,
                state=state,
            )
        except Exception as exc:
            self.panel.dispose()
            self.panel = None
            print(f"[panel] using native menu fallback: {exc}", file=sys.stderr)

    def _check_notifications(
        self,
        *,
        today: float,
        week: float,
        weekly_goal: float | None,
        streak: int,
        daily_goal: float | None = None,
    ) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic < self._next_notification_check:
            return
        self._next_notification_check = now_monotonic + NOTIFICATION_CHECK_INTERVAL
        try:
            try:
                pause_token = str(PAUSE_FILE.stat().st_mtime_ns)
            except FileNotFoundError:
                pause_token = None
            status = self.tracker_thread.status()
            self.notification_coordinator.check(
                now=datetime.now(),
                today_seconds=today,
                week_seconds=week,
                weekly_goal_hours=weekly_goal,
                daily_goal_hours=daily_goal,
                pause_token=pause_token,
                ableton_running=status.running and self.tracker_thread.consecutive_failures == 0,
                tracker_status=status,
                streak_days=streak,
                deliver=self._deliver_notification,
            )
        except Exception as exc:
            print(f"[notifications] check error: {exc}", file=sys.stderr)

    @staticmethod
    def _deliver_notification(notification: NotificationMessage) -> None:
        packaged = APP_DIR.parent / "Tracker Notifications.app" / "Contents" / "MacOS" / "tracker-notify"
        development = APP_DIR / "dist" / "Tracker.app" / "Contents" / "Resources" / "Tracker Notifications.app" / "Contents" / "MacOS" / "tracker-notify"
        sender = packaged if packaged.exists() else development
        if not sender.exists():
            raise RuntimeError("Build Tracker.app to enable Tracker notifications")
        args = [str(sender), notification.title, notification.subtitle, notification.message]
        if notification.category:
            args += [notification.category, notification.day]
        result = subprocess.run(args,
                                timeout=15, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"Tracker notification sender exited {result.returncode}")

    def _build_title(
        self, state: str, seconds: float, goal: float | None, streak: int, active: bool
    ) -> str:
        time_str = fmt_goal_time(seconds, None)
        progress = seconds / (goal * 3600) if goal and goal > 0 else 1.0
        if self._set_badge(state, progress):
            return time_str
        return f"{BADGE_GLYPHS[state]} {time_str}"

    def _set_badge(self, state: str, progress: float) -> bool:
        """Swap the status icon only when the drawn badge changes; False means use glyphs."""
        key = (state, round(min(max(progress, 0.0), 1.0) * BADGE_STEPS))
        if getattr(self, "_badge_key", None) == key:
            return True
        path = BADGE_DIR / f"{key[0]}-{key[1]}-{bytes(BADGE_ACCENT).hex()}.png"
        try:
            if not path.exists():
                BADGE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(render_badge_png(key[0], key[1] / BADGE_STEPS))
                tmp.replace(path)
            self.template = state != "tracking"
            self.icon = str(path)
        except Exception as exc:
            print(f"[badge] falling back to glyph title: {exc}", file=sys.stderr)
            self._badge_key = None
            self.icon = None
            return False
        self._badge_key = key
        return True

    def _update_streak(self, streak: int):
        if streak > 0:
            day_word = "day" if streak == 1 else "days"
            self.streak_item.title = f"Streak: {streak} {day_word}"
        else:
            self.streak_item.title = "Streak: —"

    def _update_open_ableton(self, show: bool):
        self._show_open_ableton = show
        native_item = getattr(self.open_ableton_item, "_menuitem", None)
        if native_item is not None:
            native_item.setHidden_(not show)

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

    def customize_projects(self, _):
        # Always reaches Projects, even when the embedded dashboard is open.
        import webbrowser
        self.dashboard.start_server()
        webbrowser.open(f"{DASHBOARD_URL}/#settings-projects")

    def _refresh_projects(self):
        now = time.monotonic()
        if now >= self._next_project_refresh:
            self._next_project_refresh = now + 30
            self.project_catalog.refresh_async()
        snapshot = self.project_catalog.take_snapshot()
        if snapshot is None or snapshot == self._project_snapshot:
            return
        self._project_snapshot = snapshot
        menu = self.open_project_item
        menu.clear()
        # Explicit keys avoid collisions with headings or identically named sets.
        def add(parent, key, title, path=None):
            item = rumps.MenuItem(title, callback=(lambda _: self._open_project(path)) if path else None)
            parent[key] = item
        menu.add(rumps.MenuItem("New from Template"))
        for index, template in enumerate(snapshot.get("templates", [])):
            add(menu, f"template-{index}", template["name"], template["path"])
        if not snapshot.get("templates"):
            menu["template-hint"] = rumps.MenuItem("Choose templates in Customize…", callback=self.customize_projects)
        menu.add(None)
        menu.add(rumps.MenuItem("Recent"))
        for index, project in enumerate(snapshot["recent"]):
            add(menu, f"recent-{index}", project["name"] if project["path"] else project["name"] + " (not found)", project["path"])
        if not snapshot["recent"]:
            menu.add(rumps.MenuItem("No saved projects tracked yet"))
        menu.add(None)
        menu.add(rumps.MenuItem("Pinned Folders"))
        for index, folder in enumerate(snapshot["folders"]):
            item = rumps.MenuItem(folder["label"] + (" (missing)" if folder["missing"] else ""))
            menu[f"folder-{index}"] = item
            if folder["missing"]:
                continue
            for project_index, project in enumerate(folder["projects"]):
                label = os.path.relpath(project["path"], folder["path"])
                add(item, f"set-{project_index}", label, project["path"])
            if not folder["projects"]:
                item.add(rumps.MenuItem("No Live sets found"))
            if folder["more"]:
                item.add(rumps.MenuItem("Showing the 25 newest sets"))
            if folder["warning"]:
                item.add(rumps.MenuItem(folder["warning"]))
        if not snapshot["folders"]:
            menu.add(rumps.MenuItem("Pin folders in Customize…"))
        if snapshot.get("warning"):
            menu.add(rumps.MenuItem(snapshot["warning"]))
        menu.add(None)
        menu.add(rumps.MenuItem("Customize…", callback=self.customize_projects))

    def _open_project(self, path):
        try:
            open_project(path, _find_ableton_app())
        except Exception as exc:
            rumps.alert(title="Could not open project", message=str(exc))

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
        if self.panel is not None:
            self.panel.dispose()
        self.tracker_thread.stop()
        self.dashboard.stop()
        rumps.quit_application()


if __name__ == "__main__":
    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        print("Tracker is already running; ignoring duplicate launch.")
        sys.exit(0)
    app = AbletonTrackerApp()
    signal.signal(signal.SIGTERM, lambda *args: app.quit_app(None))
    try:
        app.run()
    finally:
        app.tracker_thread.stop()
        app.dashboard.stop()
