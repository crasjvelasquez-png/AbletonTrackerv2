#!/usr/bin/env python3
"""Native desktop Tracker window."""

from datetime import datetime
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


APP_DIR = Path(__file__).resolve().parent
DASHBOARD_PORT = 7421
DASHBOARD_URL = f"http://127.0.0.1:{DASHBOARD_PORT}"
LOG_DIR = Path.home() / ".ableton_tracker"
LOG_PATH = LOG_DIR / "dashboard.log"
WINDOW_LOCK_PATH = LOG_DIR / "dashboard-window.lock"
MENUBAR_LOCK_PATH = LOG_DIR / "tracker-app.lock"
TRACKER_BUNDLE_ID = "com.abletontracker.tracker"


def python_executable() -> str:
    """Return the headless CPython binary for background child processes."""
    return os.environ.get("ABLETON_TRACKER_PYTHON") or sys.executable


def find_bundle_launcher() -> Path | None:
    """Locate the compiled Tracker.app Mach-O launcher if built."""
    candidates = (
        APP_DIR.parent.parent / "MacOS" / "launcher",
        APP_DIR / "dist" / "Tracker.app" / "Contents" / "MacOS" / "launcher",
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def is_lock_held(path: Path) -> bool:
    """Return True if another process holds an exclusive flock on path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return True
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()
    return False


def acquire_window_lock(path: Path = WINDOW_LOCK_PATH):
    """Acquire the single-instance lock for the Tracker desktop window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def activate_existing_window() -> bool:
    """Bring an already-running Tracker.app window to the foreground."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application id "{TRACKER_BUNDLE_ID}"\nreopen\nactivate\nend tell',
            ],
            timeout=3,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return True
    except Exception:
        return False


def reexec_in_app_bundle_if_needed() -> None:
    """Re-exec inside Tracker.app/Contents/MacOS/launcher so Dock owns the window."""
    if (
        sys.platform != "darwin"
        or "unittest" in sys.modules
        or os.environ.get("ABLETON_TRACKER_IN_BUNDLE") == "1"
    ):
        return
    launcher = find_bundle_launcher()
    if launcher is None:
        return
    try:
        if Path(sys.executable).resolve() != launcher.resolve():
            os.environ["ABLETON_TRACKER_IN_BUNDLE"] = "1"
            os.execv(
                str(launcher),
                [str(launcher), str(Path(__file__).resolve()), *sys.argv[1:]],
            )
    except OSError as exc:
        log(f"Could not re-exec into bundle launcher: {exc}")


def ensure_menubar_if_needed() -> subprocess.Popen | None:
    """Ensure the background menu bar tracker daemon is running when launched from Dock."""
    if "unittest" in sys.modules or is_lock_held(MENUBAR_LOCK_PATH):
        return None
    menubar_script = APP_DIR / "menubar.py"
    if not menubar_script.exists():
        return None
    log("Menu bar tracker is not running; starting menubar.py in background")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = (LOG_DIR / "tracker.log").open("a", encoding="utf-8")
    env = os.environ.copy()
    env.pop("ABLETON_TRACKER_OPEN_WINDOW", None)
    env.pop("ABLETON_TRACKER_IN_BUNDLE", None)
    proc = subprocess.Popen(
        [python_executable(), str(menubar_script)],
        stdout=log_file,
        stderr=log_file,
        cwd=APP_DIR,
        env=env,
        start_new_session=True,
    )
    log_file.close()
    return proc


def configure_macos_app_icon() -> bool:
    """Configure the bundle artwork and Dock reopen behavior on macOS."""
    if sys.platform != "darwin":
        return False

    try:
        from AppKit import NSApplication, NSImage
        import Foundation
    except ImportError:
        log("AppKit unavailable; keeping the default application icon")
        return False

    try:
        import webview.platforms.cocoa as cocoa

        def _handle_reopen(self, app, has_visible):
            for win in app.windows():
                if win.isMiniaturized():
                    win.deminiaturize_(None)
                win.makeKeyAndOrderFront_(None)
            return Foundation.YES

        if not hasattr(
            cocoa.BrowserView.AppDelegate,
            "applicationShouldHandleReopen_hasVisibleWindows_",
        ):
            cocoa.BrowserView.AppDelegate.applicationShouldHandleReopen_hasVisibleWindows_ = (
                _handle_reopen
            )
    except Exception as exc:
        log(f"Could not register Dock reopen handler: {exc}")

    bundled_icon = APP_DIR.parent / "icon.icns"
    source_icon = APP_DIR / "dist" / "Tracker.icns"
    icon_path = bundled_icon if bundled_icon.exists() else source_icon
    if not icon_path.exists():
        log(f"Application icon not found: {icon_path}")
        return False

    icon = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
    if icon is None:
        log(f"Application icon could not be loaded: {icon_path}")
        return False

    NSApplication.sharedApplication().setApplicationIconImage_(icon)
    return True


def activate_current_app() -> None:
    """Bring the current Tracker window process to the foreground."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

        NSRunningApplication.currentApplication().activateWithOptions_(
            NSApplicationActivateIgnoringOtherApps
        )
    except Exception:
        pass


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def dashboard_ready(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{DASHBOARD_URL}/api/data", timeout=timeout):
            return True
    except Exception:
        return False


def start_dashboard_if_needed() -> subprocess.Popen | None:
    if dashboard_ready(timeout=0.5):
        log("Dashboard window reusing existing dashboard server")
        return None

    log("Dashboard window starting dashboard.py")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_PATH.open("a", encoding="utf-8")
    env = os.environ.copy()
    env["ABLETON_TRACKER_NO_BROWSER"] = "1"
    proc = subprocess.Popen(
        [python_executable(), str(APP_DIR / "dashboard.py")],
        stdout=log_file,
        stderr=log_file,
        cwd=APP_DIR,
        env=env,
    )
    log_file.close()

    for _ in range(20):
        if dashboard_ready(timeout=0.5):
            log("Dashboard server ready for embedded window")
            return proc
        if proc.poll() is not None:
            log(f"ERROR: dashboard.py exited early with code {proc.returncode}")
            return proc
        time.sleep(0.25)

    log("ERROR: dashboard.py did not become ready for embedded window")
    return proc


def run_window() -> None:
    if is_lock_held(WINDOW_LOCK_PATH):
        log("Tracker window is already open; activating existing window")
        activate_existing_window()
        return

    reexec_in_app_bundle_if_needed()

    window_lock = acquire_window_lock()
    if window_lock is None:
        log("Tracker window lock already held; activating existing window")
        activate_existing_window()
        return

    try:
        ensure_menubar_if_needed()
        server_proc = start_dashboard_if_needed()

        if not dashboard_ready(timeout=1.0):
            raise RuntimeError(f"Dashboard server is not responding at {DASHBOARD_URL}")

        try:
            import webview
        except ImportError as exc:
            log("ERROR: pywebview is not installed. Run: python3 -m pip install --user pywebview")
            raise RuntimeError("pywebview is not installed") from exc

        app_url = f"{DASHBOARD_URL}/#dashboard"
        log("Opening embedded Tracker window")
        configure_macos_app_icon()
        window = webview.create_window(
            "Tracker",
            app_url,
            width=1280,
            height=860,
            min_size=(900, 650),
        )
        window.events.shown += activate_current_app
        webview.start()

        if server_proc and server_proc.poll() is None:
            log("Dashboard window closed; stopping dashboard server started by this window")
            server_proc.terminate()
    finally:
        try:
            fcntl.flock(window_lock.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        window_lock.close()


def main() -> int:
    try:
        run_window()
        return 0
    except Exception as exc:
        log(f"ERROR: dashboard window failed: {exc}")
        print(f"Tracker window failed: {exc}", file=sys.stderr)
        print(f"See log: {LOG_PATH}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
