#!/usr/bin/env python3
"""Build the self-contained-source Tracker.app macOS bundle."""

import shutil
import subprocess
import sys
import struct
import plistlib
from io import BytesIO
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
    "projects.py",
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
}


def build_icon(app_name: str, accent: str) -> Path:
    """Create a restrained generic icon as a PNG-backed ICNS file."""
    icns = DIST_DIR / f"{app_name}.icns"

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

    # ICNS supports PNG payloads directly. Building the chunks here avoids
    # iconutil, which rejects valid icon sets on some current macOS releases.
    chunks = []
    for size, chunk_type in (
        (16, b"icp4"),
        (32, b"icp5"),
        (64, b"icp6"),
        (128, b"ic07"),
        (256, b"ic08"),
        (512, b"ic09"),
        (1024, b"ic10"),
    ):
        png = BytesIO()
        image.resize((size, size), Image.Resampling.LANCZOS).save(png, format="PNG")
        payload = png.getvalue()
        chunks.append(chunk_type + struct.pack(">I", len(payload) + 8) + payload)
    body = b"".join(chunks)
    icns.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
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


def build_notification_sender(bundle: Path, icns: Path) -> None:
    """Bundle a signed native UserNotifications sender under Tracker's own icon."""
    helper = bundle / "Contents" / "Resources" / "Tracker Notifications.app"
    contents = helper / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    shutil.copy2(icns, contents / "Resources" / "icon.icns")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({
            "CFBundleName": "Tracker", "CFBundleDisplayName": "Tracker",
            "CFBundleIdentifier": "com.abletontracker.notifications",
            "CFBundleExecutable": "tracker-notify", "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0",
            "CFBundleIconFile": "icon.icns", "LSUIElement": True,
            "LSMinimumSystemVersion": "10.14",
        }, handle)
    subprocess.run(["xcrun", "swiftc", "-O", str(APP_DIR / "notification_sender.swift"),
                    "-o", str(contents / "MacOS" / "tracker-notify")], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(helper)], check=True)


def main() -> None:
    DIST_DIR.mkdir(exist_ok=True)
    for app_name, config in APPS.items():
        icns = build_icon(app_name, str(config["accent"]))
        bundle = build_bundle(app_name, config, icns)
        build_notification_sender(bundle, icns)
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
            check=True,
        )
        # Register the embedded sender so macOS can resolve its bundle and artwork.
        registrar = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
        if registrar.exists():
            subprocess.run([str(registrar), "-f", str(bundle / "Contents" / "Resources" / "Tracker Notifications.app")], check=True)
        print(f"Built and signed {bundle}")


if __name__ == "__main__":
    main()
