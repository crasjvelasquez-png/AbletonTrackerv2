#!/bin/bash
# Launch the Ableton Tracker dashboard in your browser.

DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill any previous dashboard on this port
lsof -ti:7421 | xargs kill -9 2>/dev/null

echo "Starting dashboard at http://localhost:7421"
echo "Ctrl+C to stop"
echo ""
python3 "$DIR/dashboard.py"
