#!/bin/bash
# Pre-flight diagnostic for Ableton Tracker.
# Verifies everything BEFORE you install the LaunchAgent.

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
/usr/bin/env python3 "$DIR/debug.py"
echo ""
read -rp "Press Enter to close..."
