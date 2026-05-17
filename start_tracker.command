#!/bin/bash
# Compatibility launcher for older dashboard help text.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_ID="com.abletontracker.menubar"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_ID.plist"
DATA_DIR="$HOME/.ableton_tracker"
TRACKER_LOG="$DATA_DIR/tracker.log"
PID_FILE="$DATA_DIR/tracker.pid"
PYTHON="$(command -v python3)"

mkdir -p "$DATA_DIR"

echo "Starting Ableton Tracker..."
echo ""

if pgrep -f "$DIR/menubar.py" >/dev/null 2>&1; then
    echo "Menu bar tracker is already running."
elif [ -f "$PLIST_PATH" ]; then
    echo "Mode: LaunchAgent / menu bar"
    launchctl load "$PLIST_PATH" 2>/dev/null || true
    sleep 1
    if pgrep -f "$DIR/menubar.py" >/dev/null 2>&1; then
        echo "Started menu bar tracker."
    else
        echo "LaunchAgent is installed but did not start. Run ./restart.command for a full relaunch."
    fi
else
    echo "Mode: standalone tracker.py"
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Standalone tracker is already running (PID $(cat "$PID_FILE"))."
    else
        nohup "$PYTHON" "$DIR/tracker.py" >> "$TRACKER_LOG" 2>&1 &
        PID=$!
        echo "$PID" > "$PID_FILE"
        echo "Started standalone tracker.py (PID $PID)."
    fi
fi

echo ""
echo "Data: ~/.ableton_tracker/sessions.db"
echo "Log:  $DATA_DIR/menubar.log"
echo ""
read -rp "Press Enter to close..."
