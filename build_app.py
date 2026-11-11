#!/usr/bin/env python3
"""Build the self-contained-source Tracker.app macOS bundle."""

import plistlib
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
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
    "menubar_panel.py",
    "notifications.py",
    "projects.py",
    "songs.py",
    "tracker.py",
    "tracking_policy.py",
    "activity_store.py",
    "activity_observer.py",
    "audio_probe.swift",
)
SOURCE_DIRS = ("templates", "static")

APPS = {
    "Tracker": {
        "bundle_id": "com.abletontracker.tracker",
        "accent": "#3B82F6",
        "ui_element": False,
        "entry": "dashboard_window.py",
    },
}


def resolve_python_dylib() -> str:
    """Resolve the shared CPython library path for in-process bundle execution."""
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    ldlib = sysconfig.get_config_var("LDLIBRARY") or ""
    if libdir and ldlib:
        candidate = Path(libdir) / ldlib
        if candidate.exists():
            return str(candidate.resolve())
    framework_lib = Path(sys.prefix) / "Python"
    if framework_lib.exists():
        return str(framework_lib.resolve())
    raise FileNotFoundError(f"Could not locate CPython shared library under {sys.prefix}")


def build_launcher(macos: Path, python_path: str, python_dylib: str, entry: str) -> Path:
    """Compile a native Mach-O bundle launcher so macOS Dock binds to Tracker.app."""
    launcher = macos / "launcher"
    c_source = f"""#include <dlfcn.h>
#include <fcntl.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *PYTHON_PATH = "{python_path}";
static const char *PYTHON_DYLIB = "{python_dylib}";
static const char *ENTRY_SCRIPT = "{entry}";

typedef int (*Py_BytesMain_t)(int, char **);

static void redirect_logs(const char *log_name) {{
    const char *home = getenv("HOME");
    if (!home || !home[0]) return;
    char dir_path[PATH_MAX];
    snprintf(dir_path, sizeof(dir_path), "%s/.ableton_tracker", home);
    mkdir(dir_path, 0755);
    char log_path[PATH_MAX];
    snprintf(log_path, sizeof(log_path), "%s/%s", dir_path, log_name);
    int fd = open(log_path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) {{
        dup2(fd, STDOUT_FILENO);
        dup2(fd, STDERR_FILENO);
        if (fd > STDERR_FILENO) close(fd);
    }}
}}

int main(int argc, char **argv) {{
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("ABLETON_TRACKER_PYTHON", PYTHON_PATH, 1);

    char exec_buf[PATH_MAX];
    uint32_t exec_size = sizeof(exec_buf);
    if (_NSGetExecutablePath(exec_buf, &exec_size) != 0) {{
        fprintf(stderr, "Failed to resolve executable path\\n");
        return 1;
    }}
    char real_exec[PATH_MAX];
    if (!realpath(exec_buf, real_exec)) {{
        strncpy(real_exec, exec_buf, sizeof(real_exec) - 1);
        real_exec[sizeof(real_exec) - 1] = '\\0';
    }}
    char exec_copy[PATH_MAX];
    strncpy(exec_copy, real_exec, sizeof(exec_copy) - 1);
    exec_copy[sizeof(exec_copy) - 1] = '\\0';
    char *macos_dir = dirname(exec_copy);

    char rel_app[PATH_MAX];
    snprintf(rel_app, sizeof(rel_app), "%s/../Resources/app", macos_dir);
    char app_source[PATH_MAX];
    if (!realpath(rel_app, app_source)) {{
        strncpy(app_source, rel_app, sizeof(app_source) - 1);
        app_source[sizeof(app_source) - 1] = '\\0';
    }}

    if (argc >= 2 && strcmp(argv[1], "--background") == 0) {{
        redirect_logs("tracker.log");
        if (chdir(app_source) != 0) {{
            perror("chdir failed");
            return 1;
        }}
        char *bg_argv[] = {{ (char *)PYTHON_PATH, "menubar.py", NULL }};
        execv(PYTHON_PATH, bg_argv);
        perror("execv failed");
        return 1;
    }}

    redirect_logs("dashboard.log");
    setenv("ABLETON_TRACKER_IN_BUNDLE", "1", 1);

    void *handle = dlopen(PYTHON_DYLIB, RTLD_LAZY | RTLD_GLOBAL);
    if (!handle) {{
        fprintf(stderr, "dlopen(%s) failed: %s\\n", PYTHON_DYLIB, dlerror());
        return 1;
    }}
    Py_BytesMain_t py_main = (Py_BytesMain_t)dlsym(handle, "Py_BytesMain");
    if (!py_main) {{
        fprintf(stderr, "dlsym(Py_BytesMain) failed: %s\\n", dlerror());
        return 1;
    }}

    if (argc >= 2 && strncmp(argv[1], "-psn_", 5) != 0) {{
        return py_main(argc, argv);
    }}

    if (chdir(app_source) != 0) {{
        perror("chdir failed");
        return 1;
    }}
    char script_path[PATH_MAX];
    snprintf(script_path, sizeof(script_path), "%s/%s", app_source, ENTRY_SCRIPT);
    char *win_argv[] = {{ real_exec, script_path, NULL }};
    return py_main(2, win_argv);
}}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".c", encoding="utf-8", delete=False) as tmp:
        tmp.write(c_source)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            ["xcrun", "clang", "-O2", str(tmp_path), "-o", str(launcher)],
            check=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    launcher.chmod(0o755)
    return launcher


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
    subprocess.run(["xcrun","swiftc","-O",str(APP_DIR / "audio_probe.swift"),
                    "-o",str(embedded / "audio_probe")],check=True)
    subprocess.run(["codesign","--force","--sign","-",str(embedded / "audio_probe")],check=True)
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
    python_dylib = resolve_python_dylib()
    entry = str(config["entry"])
    build_launcher(macos, python_path, python_dylib, entry)
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
        # Register both the embedded notification helper and the main app bundle.
        registrar = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
        if registrar.exists():
            subprocess.run([str(registrar), "-f", str(bundle / "Contents" / "Resources" / "Tracker Notifications.app")], check=True)
            subprocess.run([str(registrar), "-f", str(bundle)], check=True)
        print(f"Built and signed {bundle}")


if __name__ == "__main__":
    main()
