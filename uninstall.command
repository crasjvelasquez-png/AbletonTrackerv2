#!/bin/bash
# Stop and uninstall the Ableton Tracker LaunchAgent.
# Your data (sessions.db) is preserved.

PLIST_ID="com.abletontracker.menubar"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_ID.plist"

echo "Uninstalling Ableton Tracker..."
launchctl unload "$PLIST_PATH" 2>/dev/null || true
rm -f "$PLIST_PATH"
pkill -f "menubar.py" 2>/dev/null || true
pkill -f "dashboard.py" 2>/dev/null || true

echo "Done. Your data is still at ~/.ableton_tracker/sessions.db"
echo "Re-run install.command to turn tracking back on."
echo ""
read -rp "Press Enter to close..."
