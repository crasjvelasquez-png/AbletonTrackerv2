#!/bin/bash
# Restart Ableton Tracker so code changes take effect.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_ID="com.abletontracker.menubar"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_ID.plist"
DATA_DIR="$HOME/.ableton_tracker"
TRACKER_LOG="$DATA_DIR/tracker.log"
DASHBOARD_LOG="$DATA_DIR/dashboard.log"
PID_FILE="$DATA_DIR/tracker.pid"
PYTHON="$(command -v python3)"

mkdir -p "$DATA_DIR"

echo "Restarting Ableton Tracker..."
echo ""

# Stop the dashboard server if it's running so the next open uses fresh code.
pkill -f "dashboard.py" 2>/dev/null || true

# Wipe cached bytecode so Python re-reads source on next launch.
rm -rf "$DIR/__pycache__"

if [ -f "$PLIST_PATH" ]; then
    echo "Mode: LaunchAgent / menu bar"
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    pkill -f "menubar.py" 2>/dev/null || true
    sleep 1
    launchctl load "$PLIST_PATH"
    echo "Relaunched menu bar tracker."
    echo ""
    echo "Menu bar log:  $DATA_DIR/menubar.log"
else
    echo "Mode: standalone Tracker.app / menu bar"
    if [ -f "$PID_FILE" ]; then
        PID="$(cat "$PID_FILE")"
        kill "$PID" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    pkill -f "menubar.py" 2>/dev/null || true
    sleep 1
    "$PYTHON" "$DIR/build_app.py"
    open -gj "$DIR/dist/Tracker.app"
    echo "Relaunched Tracker.app menu bar."
    echo ""
    echo "Tracker log:   $TRACKER_LOG"
fi

echo "Dashboard log: $DASHBOARD_LOG"
echo ""
echo "If your dashboard was open, reopen it so it starts with fresh code."
echo ""
read -rp "Press Enter to close..."
