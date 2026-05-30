#!/usr/bin/env python3
"""Build AbletonTrackerDashboard.app with a rounded-corner icon.

Requires: icon_source.png (or .jpg/.jpeg/.webp) in the same folder.
Produces: AbletonTrackerDashboard.app — drag this to your Dock.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

APP_DIR   = Path(__file__).resolve().parent
APP_NAME  = "AbletonTrackerDashboard"
APP_BUNDLE = APP_DIR / f"{APP_NAME}.app"
CORNER_RADIUS_RATIO = 0.225  # Apple's squircle uses ~22.5% of side length
ICON_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def find_source() -> Path:
    for ext in ("png", "jpg", "jpeg", "webp", "PNG", "JPG", "JPEG"):
        p = APP_DIR / f"icon_source.{ext}"
        if p.exists():
            return p
    print("ERROR: No icon_source.png (or .jpg/.jpeg/.webp) found in", APP_DIR)
    print("       Save your logo image as icon_source.png next to this script.")
    sys.exit(1)


def trim_background(img: Image.Image, tolerance: int = 20) -> Image.Image:
    # Trim near-white (or near-transparent) border from around the icon content.
    rgba = img.convert("RGBA")
    r, g, b, a = rgba.split()
    # Treat pixels as "background" if nearly white OR nearly transparent.
    rgb = Image.merge("RGB", (r, g, b))
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, bg).convert("L")
    # Combine color-diff with alpha so transparent pixels also count as background.
    mask = ImageChops.lighter(diff, a)
    bbox = mask.point(lambda v: 255 if v > tolerance else 0).getbbox()
    return rgba.crop(bbox) if bbox else rgba


def make_rounded_square(src: Path, size: int) -> Image.Image:
    img = Image.open(src).convert("RGBA")
    img = trim_background(img)
    # Pad to square (don't crop content) so the full icon fits.
    w, h = img.size
    side = max(w, h)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - w) // 2, (side - h) // 2))
    img = square.resize((size, size), Image.LANCZOS)
    img = img.resize((size, size), Image.LANCZOS)

    # Rounded-corner mask
    mask = Image.new("L", (size, size), 0)
    radius = int(size * CORNER_RADIUS_RATIO)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (size, size)], radius, fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask=mask)
    return out


def build_iconset(src: Path) -> Path:
    iconset = APP_DIR / f"{APP_NAME}.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    # iconutil requires specific filenames
    spec = [
        (16,  "icon_16x16.png"),
        (32,  "icon_16x16@2x.png"),
        (32,  "icon_32x32.png"),
        (64,  "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024,"icon_512x512@2x.png"),
    ]
    for size, name in spec:
        make_rounded_square(src, size).save(iconset / name, "PNG")

    icns = APP_DIR / f"{APP_NAME}.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    shutil.rmtree(iconset)
    return icns


def build_bundle(icns: Path):
    if APP_BUNDLE.exists():
        shutil.rmtree(APP_BUNDLE)
    contents  = APP_BUNDLE / "Contents"
    macos_dir = contents / "MacOS"
    res_dir   = contents / "Resources"
    macos_dir.mkdir(parents=True)
    res_dir.mkdir(parents=True)

    shutil.copy(icns, res_dir / "icon.icns")

    (contents / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Ableton Tracker</string>
    <key>CFBundleDisplayName</key><string>Ableton Tracker</string>
    <key>CFBundleIdentifier</key><string>com.abletontracker.dashboard</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleSignature</key><string>????</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIconFile</key><string>icon.icns</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
    <key>LSUIElement</key><false/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
""")

    # Resolve python3 at build time — Dock-launched apps have a stripped PATH
    # and `command -v python3` there often finds nothing (or Apple's stub that
    # prompts to install Command Line Tools).
    python_path = sys.executable or shutil.which("python3") or "/usr/bin/python3"

    launcher = macos_dir / "launcher"
    launcher.write_text(f"""#!/bin/bash
# Launch the embedded Ableton Tracker dashboard window.
set -u
APP_DIR="{APP_DIR}"
PYTHON="{python_path}"
LOG_DIR="$HOME/.ableton_tracker"
LOG="$LOG_DIR/dashboard.log"
mkdir -p "$LOG_DIR"

log() {{ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }}

log "Dashboard app launched"

if [ ! -x "$PYTHON" ]; then
    log "ERROR: Python not found at $PYTHON"
    osascript -e "display dialog \\"Ableton Tracker: Python not found at $PYTHON. Re-run build_app.command.\\" buttons {{\\"OK\\"}} default button 1 with icon stop"
    exit 1
fi

if ! "$PYTHON" -c "import webview" >/dev/null 2>&1; then
    log "ERROR: pywebview is not installed for $PYTHON"
    osascript -e "display dialog \\"Ableton Tracker: pywebview is not installed. Run build_app.command again or install it with: $PYTHON -m pip install --user pywebview\\" buttons {{\\"OK\\"}} default button 1 with icon stop"
    exit 1
fi

log "Opening embedded dashboard window"
exec "$PYTHON" "$APP_DIR/dashboard_window.py" >> "$LOG" 2>&1
""")
    launcher.chmod(0o755)


def main():
    src = find_source()
    print(f"Source image: {src.name}")
    icns = build_iconset(src)
    print(f"Built icon:   {icns.name}")
    build_bundle(icns)
    print(f"Built app:    {APP_BUNDLE.name}")
    print()
    print("Drag AbletonTrackerDashboard.app to your Dock.")
    print("Clicking it opens the dashboard in a standalone app window.")


if __name__ == "__main__":
    main()
