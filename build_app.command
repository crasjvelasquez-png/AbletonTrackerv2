#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if ! python3 -c "import PIL" 2>/dev/null; then
    echo "Installing Pillow..."
    python3 -m pip install --user Pillow
fi

if ! python3 -c "import webview" 2>/dev/null; then
    echo "Installing pywebview..."
    python3 -m pip install --user pywebview
fi

python3 "$DIR/build_app.py"

echo
echo "Built standalone app bundles:"
echo "  $DIR/dist/Tracker.app"
echo "  $DIR/dist/Planner.app"
echo ""
read -rp "Press Enter to close..."
