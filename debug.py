#!/usr/bin/env python3
"""Pre-flight diagnostic for Ableton Tracker.

Runs every check needed to confirm the menu bar app will work,
so you don't have to discover issues after installing the LaunchAgent.
"""

import os
import re
import sys
import socket
import sqlite3
import subprocess
from pathlib import Path

from tracker import get_live_window_titles, parse_project_title

APP_DIR = Path(__file__).resolve().parent
OK, WARN, FAIL = "✅", "⚠️ ", "❌"
results = []
CHECKS = []


def check(name):
    def deco(fn):
        def wrapped():
            try:
                status, msg = fn()
            except Exception as e:
                status, msg = FAIL, f"exception: {e}"
            results.append((status, name, msg))
            print(f"{status}  {name}\n    {msg}\n")
        CHECKS.append(wrapped)
        return wrapped
    return deco


@check("Python version")
def _():
    v = sys.version_info
    if v < (3, 9):
        return FAIL, f"Python {v.major}.{v.minor} too old (need 3.9+)"
    return OK, f"Python {v.major}.{v.minor}.{v.micro} at {sys.executable}"


@check("rumps (menu bar library)")
def _():
    try:
        import rumps  # noqa
        return OK, f"rumps {rumps.__version__} installed"
    except ImportError:
        return FAIL, "Not installed. Run: pip3 install --user rumps"


@check("pyobjc (required by rumps)")
def _():
    try:
        import AppKit  # noqa
        return OK, "PyObjC AppKit bindings available"
    except ImportError:
        return WARN, "PyObjC not found. Usually bundled with rumps; if menu bar fails, run: pip3 install --user pyobjc-framework-Cocoa"


@check("Accessibility permission (AppleScript window titles)")
def _():
    script = 'tell application "System Events" to return name of first process'
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        if "1002" in r.stderr or "not allowed" in r.stderr.lower():
            return FAIL, (
                "Accessibility permission DENIED.\n"
                "    Fix: System Settings → Privacy & Security → Accessibility → enable Terminal\n"
                "    (and later, once installed, enable /usr/bin/python3 there too if prompted)"
            )
        return WARN, f"AppleScript error: {r.stderr.strip()[:200]}"
    return OK, "System Events scripting works"


@check("Ableton process detection")
def _():
    r = subprocess.run(["pgrep", "-x", "Live"], capture_output=True, text=True)
    if r.returncode == 0:
        pids = r.stdout.strip().replace("\n", ", ")
        return OK, f"Ableton Live running (PID {pids})"
    return WARN, "Ableton not running right now — can't test window-title parsing live. That's fine, other checks still apply."


@check("Ableton window-title parse (Live 11 & 12 compatibility)")
def _():
    samples = {
        # Live 11 typical formats
        "MyTrack [edited] - Ableton Live 11 Suite": "MyTrack",
        "Cool Song - Ableton Live 11 Standard": "Cool Song",
        # Live 12 typical formats
        "MyTrack.als - Ableton Live 12 Suite": "MyTrack",
        "Beat Sketch [edited] - Ableton Live 12 Suite": "Beat Sketch",
        "DemoProject.als - Ableton Live 12 Trial": "DemoProject",
        # Edge cases
        "Untitled - Ableton Live 12 Suite": "Untitled",
        "Project (v2) [edited] - Ableton Live 11 Suite": "Project",
        "Export Audio...": None,
        "Export Audio/Video... - Ableton Live 12 Suite": None,
        "VocAlign 6 Pro AU/14-harmony 3 R": None,
    }

    fails = []
    for title, expected in samples.items():
        got = parse_project_title(title)
        if got != expected:
            fails.append(f"'{title}' → '{got}' (expected '{expected}')")

    if fails:
        return FAIL, "Title parse mismatches:\n    " + "\n    ".join(fails)

    # Try the real frontmost title if Ableton is running
    live = subprocess.run(["pgrep", "-x", "Live"], capture_output=True).returncode == 0
    live_info = ""
    if live:
        titles = get_live_window_titles()
        if titles:
            live_info = "\n    Visible Live windows:\n    " + "\n    ".join(
                f"'{title}' → {parse_project_title(title)!r}" for title in titles[:4]
            )
    return OK, f"All {len(samples)} Live title variants parse correctly{live_info}"


@check("Idle detection (HIDIdleTime)")
def _():
    r = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=3)
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', r.stdout)
    if not m:
        return FAIL, "Could not read HIDIdleTime from ioreg — idle pausing will not work"
    secs = int(m.group(1)) / 1_000_000_000
    return OK, f"Idle time readable ({secs:.1f}s since last input). 10-minute idle pause will work."


@check("Dashboard port (7421) available")
def _():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 7421))
        s.close()
        return OK, "Port 7421 free"
    except OSError:
        return WARN, "Port 7421 already in use (probably a previous dashboard). Not a blocker — restart will reclaim it."


@check("Database read/write")
def _():
    db = Path.home() / ".ableton_tracker" / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (x INT)")
        conn.execute("INSERT INTO _probe VALUES (1)")
        conn.execute("DROP TABLE _probe")
        conn.commit()
    exists = db.exists()
    return OK, f"DB writable at {db} ({'existing' if exists else 'new'})"


@check("LaunchAgent directory")
def _():
    d = Path.home() / "Library" / "LaunchAgents"
    d.mkdir(parents=True, exist_ok=True)
    test = d / ".probe"
    test.write_text("x")
    test.unlink()
    return OK, f"{d} writable"


@check("Existing LaunchAgent status")
def _():
    plist = Path.home() / "Library" / "LaunchAgents" / "com.abletontracker.menubar.plist"
    if not plist.exists():
        return OK, "Not installed yet. Run install.command after this passes."
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    loaded = "com.abletontracker.menubar" in r.stdout
    return OK, f"Plist present. Loaded: {loaded}"


@check("menubar.py imports cleanly")
def _():
    r = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, '{APP_DIR}'); import menubar"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return FAIL, f"Import failed:\n    {r.stderr.strip()}"
    return OK, "menubar.py and all its imports load successfully"


def main():
    print("\n══ Ableton Tracker Pre-Flight Diagnostic ══\n")
    for fn in CHECKS:
        fn()

    fails = sum(1 for s, *_ in results if s == FAIL)
    warns = sum(1 for s, *_ in results if s == WARN)
    oks = sum(1 for s, *_ in results if s == OK)

    print("─" * 56)
    print(f"Summary: {oks} ok · {warns} warning · {fails} failure")
    if fails:
        print("\n❌ Do NOT install yet — fix the failures above.")
        sys.exit(1)
    elif warns:
        print("\n⚠️  Warnings are non-blocking. install.command should work.")
    else:
        print("\n✅ All green. Run install.command to set it up.")


if __name__ == "__main__":
    main()
