#!/usr/bin/env python3
"""Build self-contained-source Tracker.app and Planner.app macOS bundles."""

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw


APP_DIR = Path(__file__).resolve().parent
DIST_DIR = APP_DIR / "dist"
MASTER_SIZE = 1024
ICON_INSET = 92  # Matches the 82% visual footprint used by Latency.app.
SOURCE_FILES = (
    "dashboard.py",
    "dashboard_window.py",
    "menubar.py",
    "notifications.py",
    "tracker.py",
)
SOURCE_DIRS = ("templates", "static")

APPS = {
    "Tracker": {
        "bundle_id": "com.abletontracker.tracker",
        "accent": "#3B82F6",
        "ui_element": True,
        "entry": "menubar.py",
    },
    "Planner": {
        "bundle_id": "com.abletontracker.planner",
        "accent": "#8B5CF6",
        "ui_element": False,
        "entry": "dashboard_window.py planner",
    },
}


def build_icon(app_name: str, accent: str) -> Path:
    """Create a restrained generic icon and compile it to ICNS."""
    iconset = DIST_DIR / f"{app_name}.iconset"
    icns = DIST_DIR / f"{app_name}.icns"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    image = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = ICON_INSET
    draw.rounded_rectangle(
        (inset, inset, MASTER_SIZE - inset, MASTER_SIZE - inset),
        radius=210,
        fill=accent,
    )
    white = (255, 255, 255, 245)
    if app_name == "Tracker":
        widths = 54
        heights = (150, 280, 430, 280, 150)
        gap = 54
        total = len(heights) * widths + (len(heights) - 1) * gap
        x = (MASTER_SIZE - total) // 2
        for height in heights:
            y = (MASTER_SIZE - height) // 2
            draw.rounded_rectangle((x, y, x + widths, y + height), radius=27, fill=white)
            x += widths + gap
    else:
        for index, width in enumerate((360, 300, 230)):
            y = 350 + index * 145
            draw.ellipse((292, y - 24, 340, y + 24), fill=white)
            draw.rounded_rectangle((390, y - 18, 390 + width, y + 18), radius=18, fill=white)

    specs = (
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    )
    for size, filename in specs:
        image.resize((size, size), Image.Resampling.LANCZOS).save(iconset / filename)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    shutil.rmtree(iconset)
    image.save(DIST_DIR / f"{app_name}-icon.png")
    return icns


def copy_app_source(resources: Path) -> Path:
    embedded = resources / "app"
    embedded.mkdir(parents=True)
    for filename in SOURCE_FILES:
        shutil.copy2(APP_DIR / filename, embedded / filename)
    for dirname in SOURCE_DIRS:
        shutil.copytree(APP_DIR / dirname, embedded / dirname)
    return embedded


def build_bundle(app_name: str, config: dict[str, object], icns: Path) -> Path:
    bundle = DIST_DIR / f"{app_name}.app"
    if bundle.exists():
        shutil.rmtree(bundle)
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copy2(icns, resources / "icon.icns")
    copy_app_source(resources)

    contents.joinpath("Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>{app_name}</string>
<key>CFBundleDisplayName</key><string>{app_name}</string>
<key>CFBundleIdentifier</key><string>{config['bundle_id']}</string>
<key>CFBundleVersion</key><string>1.0</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleExecutable</key><string>launcher</string>
<key>CFBundleIconFile</key><string>icon.icns</string>
<key>LSMinimumSystemVersion</key><string>10.13</string>
<key>LSUIElement</key><{str(config['ui_element']).lower()}/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
""", encoding="utf-8")

    python_path = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    entry = str(config["entry"])
    open_window = ""
    if app_name == "Tracker":
        open_window = '''if [ "${1:-}" != "--background" ]; then
    export ABLETON_TRACKER_OPEN_WINDOW="1"
fi'''
    launcher = macos / "launcher"
    launcher.write_text(f"""#!/bin/bash
set -u
export PYTHONDONTWRITEBYTECODE=1
PYTHON="{python_path}"
APP_SOURCE="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
LOG_DIR="$HOME/.ableton_tracker"
mkdir -p "$LOG_DIR"
{open_window}
cd "$APP_SOURCE"
exec "$PYTHON" {entry} >> "$LOG_DIR/{app_name.lower()}.log" 2>&1
""", encoding="utf-8")
    launcher.chmod(0o755)
    return bundle


def main() -> None:
    DIST_DIR.mkdir(exist_ok=True)
    for app_name, config in APPS.items():
        icns = build_icon(app_name, str(config["accent"]))
        bundle = build_bundle(app_name, config, icns)
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
            check=True,
        )
        print(f"Built and signed {bundle}")


if __name__ == "__main__":
    main()
