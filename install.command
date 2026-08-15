#!/bin/bash
# Build and open the standalone Tracker and Planner apps.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_ID="com.abletontracker.menubar"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_ID.plist"
LOG_DIR="$HOME/.ableton_tracker"
PYTHON="$(command -v python3)"

mkdir -p "$LOG_DIR"

echo "Installing Ableton Tracker..."
echo "  Python:    $PYTHON"
echo "  App dir:   $DIR"
echo ""

# Ensure runtime packages are installed
if ! "$PYTHON" -c "import rumps" 2>/dev/null; then
    echo "Installing required package: rumps"
    "$PYTHON" -m pip install --user rumps
fi

if ! "$PYTHON" -c "import webview" 2>/dev/null; then
    echo "Installing required package: pywebview"
    "$PYTHON" -m pip install --user pywebview
fi

# Remove the pre-standalone LaunchAgent so it cannot create a second menu bar.
launchctl unload "$PLIST_PATH" 2>/dev/null || true
rm -f "$PLIST_PATH"
# Clean up legacy background tracker started by start_tracker.command
if [ -f "$LOG_DIR/tracker.pid" ]; then
    kill "$(cat "$LOG_DIR/tracker.pid")" 2>/dev/null || true
    rm -f "$LOG_DIR/tracker.pid"
fi

"$PYTHON" "$DIR/build_app.py"
open "$DIR/dist/Tracker.app"

echo "Built Tracker.app and Planner.app. Tracker is now open."
echo ""
echo "Apps: $DIR/dist"
echo "Log:  $LOG_DIR/tracker.log"
echo ""
read -rp "Press Enter to close..."
