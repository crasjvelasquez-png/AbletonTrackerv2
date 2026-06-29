#!/usr/bin/env python3
"""Build AbletonTrackerDashboard.app with a correctly padded macOS icon.

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
MASTER_SIZE = 1024
ARTWORK_SCALE = 0.80
ICON_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def find_source() -> Path:
    for ext in ("png", "jpg", "jpeg", "webp", "PNG", "JPG", "JPEG"):
        p = APP_DIR / f"icon_source.{ext}"
        if p.exists():
            return p
    print("ERROR: No icon_source.png (or .jpg/.jpeg/.webp) found in", APP_DIR)
    print("       Save your logo image as icon_source.png next to this script.")
    sys.exit(1)


def isolate_existing_container(img: Image.Image) -> Image.Image:
    """Mask away the source canvas outside its existing rounded container."""
    rgba = img.convert("RGBA")
    mask = Image.new("L", rgba.size, 0)
    radius = round(min(rgba.size) * 0.225)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, rgba.width - 1, rgba.height - 1), radius=radius, fill=255
    )
    rgba.putalpha(ImageChops.multiply(rgba.getchannel("A"), mask))
    return rgba


def build_master(src: Path) -> Image.Image:
    img = isolate_existing_container(Image.open(src))
    bbox = img.getchannel("A").point(lambda value: 255 if value > 8 else 0).getbbox()
    if bbox:
        img = img.crop(bbox)

    # The artwork already contains its rounded-square container. Preserve it and
    # fit it proportionally inside a transparent square instead of adding another.
    w, h = img.size
    target = round(MASTER_SIZE * ARTWORK_SCALE)
    ratio = min(target / w, target / h)
    fitted = img.resize((round(w * ratio), round(h * ratio)), Image.LANCZOS)
    master = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    master.alpha_composite(
        fitted,
        ((MASTER_SIZE - fitted.width) // 2, (MASTER_SIZE - fitted.height) // 2),
    )
    return master


def build_iconset(src: Path) -> Path:
    iconset = APP_DIR / f"{APP_NAME}.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    master = build_master(src)
    master.save(APP_DIR / "icon_master_1024.png", "PNG")

    # iconutil requires specific filenames.
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
        master.resize((size, size), Image.LANCZOS).save(iconset / name, "PNG")

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
