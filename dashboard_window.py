#!/usr/bin/env python3
"""Native desktop window for the Tracker or Planner surface."""

import argparse
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DASHBOARD_PORT = 7421
DASHBOARD_URL = f"http://127.0.0.1:{DASHBOARD_PORT}"
LOG_DIR = Path.home() / ".ableton_tracker"
LOG_PATH = LOG_DIR / "dashboard.log"


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
        [sys.executable, str(APP_DIR / "dashboard.py")],
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


def run_window(app_mode: str = "tracker") -> None:
    server_proc = start_dashboard_if_needed()

    if not dashboard_ready(timeout=1.0):
        raise RuntimeError(f"Dashboard server is not responding at {DASHBOARD_URL}")

    try:
        import webview
    except ImportError as exc:
        log("ERROR: pywebview is not installed. Run: python3 -m pip install --user pywebview")
        raise RuntimeError("pywebview is not installed") from exc

    app_name = "Planner" if app_mode == "planner" else "Tracker"
    app_url = f"{DASHBOARD_URL}/?app={app_mode}#{'planner' if app_mode == 'planner' else 'dashboard'}"
    log(f"Opening embedded {app_name} window")
    webview.create_window(
        app_name,
        app_url,
        width=1280,
        height=860,
        min_size=(900, 650),
    )
    webview.start()

    if server_proc and server_proc.poll() is None:
        log("Dashboard window closed; stopping dashboard server started by this window")
        server_proc.terminate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app_mode", nargs="?", choices=("tracker", "planner"), default="tracker")
    args = parser.parse_args()
    try:
        run_window(args.app_mode)
        return 0
    except Exception as exc:
        log(f"ERROR: dashboard window failed: {exc}")
        print(f"{args.app_mode.title()} window failed: {exc}", file=sys.stderr)
        print(f"See log: {LOG_PATH}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
