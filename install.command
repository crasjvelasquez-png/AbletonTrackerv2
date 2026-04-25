#!/bin/bash
# Install Ableton Tracker as a LaunchAgent that starts at login.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_ID="com.abletontracker.menubar"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_ID.plist"
LOG_DIR="$HOME/.ableton_tracker"
PYTHON="$(command -v python3)"

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

echo "Installing Ableton Tracker..."
echo "  Python:    $PYTHON"
echo "  App dir:   $DIR"
echo "  LaunchAgent: $PLIST_PATH"
echo ""

# Ensure rumps is installed
if ! "$PYTHON" -c "import rumps" 2>/dev/null; then
    echo "Installing required package: rumps"
    "$PYTHON" -m pip install --user rumps
fi

# Stop any old instance
launchctl unload "$PLIST_PATH" 2>/dev/null || true
pkill -f "menubar.py" 2>/dev/null || true
# Clean up legacy background tracker started by start_tracker.command
if [ -f "$LOG_DIR/tracker.pid" ]; then
    kill "$(cat "$LOG_DIR/tracker.pid")" 2>/dev/null || true
    rm -f "$LOG_DIR/tracker.pid"
fi

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$PLIST_ID</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$DIR/menubar.py</string>
    </array>
    <key>WorkingDirectory</key><string>$DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/menubar.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/menubar.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

launchctl load "$PLIST_PATH"

echo "Installed. The ● icon should appear in your menu bar within a few seconds."
echo "It will auto-start every time you log in."
echo ""
echo "Logs: $LOG_DIR/menubar.log"
echo "Uninstall: run uninstall.command"
echo ""
read -rp "Press Enter to close..."
