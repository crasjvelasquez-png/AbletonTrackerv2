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

# Replace the pre-standalone LaunchAgent with one that opens Tracker.app at login.
# Using `open` keeps the app in its bundle context, which rumps needs to present
# the status-bar menu reliably.
launchctl unload "$PLIST_PATH" 2>/dev/null || true
rm -f "$PLIST_PATH"
# Clean up legacy background tracker started by start_tracker.command
if [ -f "$LOG_DIR/tracker.pid" ]; then
    kill "$(cat "$LOG_DIR/tracker.pid")" 2>/dev/null || true
    rm -f "$LOG_DIR/tracker.pid"
fi

"$PYTHON" "$DIR/build_app.py"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>$PLIST_ID</string>
    <key>ProgramArguments</key><array>
        <string>/usr/bin/open</string>
        <string>-gj</string>
        <string>$DIR/dist/Tracker.app</string>
    </array>
    <key>RunAtLoad</key><true/>
</dict></plist>
EOF

pkill -f "menubar.py" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "Built Tracker.app and Planner.app. Tracker is now open and will start at login."
echo ""
echo "Apps: $DIR/dist"
echo "Log:  $LOG_DIR/tracker.log"
echo ""
read -rp "Press Enter to close..."
