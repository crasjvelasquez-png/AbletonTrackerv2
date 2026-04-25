#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if ! python3 -c "import PIL" 2>/dev/null; then
    echo "Installing Pillow..."
    python3 -m pip install --user Pillow
fi

python3 "$DIR/build_app.py"
echo ""
read -rp "Press Enter to close..."
