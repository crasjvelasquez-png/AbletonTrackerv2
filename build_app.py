#!/usr/bin/env python3
"""Build the self-contained-source Tracker.app macOS bundle."""

import getpass
import os
import plistlib
import secrets
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw


APP_DIR = Path(__file__).resolve().parent
DIST_DIR = Path(os.environ.get("TRACKER_DIST_DIR") or APP_DIR / "dist")
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

# macOS keeps Accessibility, Audio Capture and Notification grants against the
# app's designated requirement.  Ad-hoc signatures pin that to the cdhash, so
# every rebuild silently revoked Accessibility and stopped tracking.  A local
# self-signed identity, created once and reused, keeps the requirement stable:
# `identifier "com.abletontracker.tracker" and certificate leaf = H"..."`.
SIGNING_DIR = Path(os.environ.get("TRACKER_SIGNING_DIR") or Path.home() / ".ableton_tracker" / "signing")
SIGNING_KEYCHAIN = SIGNING_DIR / "tracker-signing.keychain-db"
SIGNING_NAME = "Ableton Tracker Local Signing"
# Login-keychain item holding the signing keychain's random password. A non-default
# signing dir gets its own item so experiments never overwrite the real one.
SIGNING_SERVICE = ("com.abletontracker.signing" if "TRACKER_SIGNING_DIR" not in os.environ
                   else f"com.abletontracker.signing:{SIGNING_DIR}")
# LibreSSL writes PKCS#12 that `security import` accepts; OpenSSL 3+ does not.
SYSTEM_OPENSSL = "/usr/bin/openssl"
ERR_SEC_ITEM_NOT_FOUND = 44  # `security find-generic-password` exit status for a missing item


def _security(command: str) -> None:
    """Run one password-bearing `security` command via stdin, keeping secrets out of argv.

    One command per call: `security -i` exits with the status of its last command
    only, so batching would hide an earlier failure.
    """
    result = subprocess.run(["security", "-i"], input=command + "\n", capture_output=True, text=True)
    failed = [line for line in result.stderr.splitlines() if ": returned " in line]
    if result.returncode or failed:
        raise RuntimeError(f"security {command.split()[0]} failed: "
                           + (failed[-1] if failed else result.stderr.strip() or str(result.returncode)))


def _keychain_password() -> str | None:
    """The signing keychain's password, None only if the item truly does not exist."""
    result = subprocess.run(["security", "find-generic-password", "-s", SIGNING_SERVICE,
                             "-a", getpass.getuser(), "-w"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    if result.returncode == ERR_SEC_ITEM_NOT_FOUND:
        return None
    # Locked login keychain (SSH, screen locked) or a cancelled prompt: the identity
    # still exists, so never treat this as missing and replace it.
    raise RuntimeError(f"login keychain unavailable (security exit {result.returncode})")


def _identity_hash() -> str | None:
    out = subprocess.run(["security", "find-identity", "-p", "codesigning", str(SIGNING_KEYCHAIN)],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if f'"{SIGNING_NAME}"' in line and ")" in line:
            return line.split(")", 1)[1].split()[0]
    return None


def _create_signing_keychain() -> None:
    """Create the identity; on any failure leave nothing behind so the next build retries cleanly."""
    SIGNING_DIR.mkdir(parents=True, exist_ok=True)
    (SIGNING_DIR / "README.txt").write_text(
        "Do not delete. Tracker.app is signed with the identity in this keychain so macOS keeps\n"
        "its Accessibility grant across rebuilds. Deleting it forces a\n"
        "one-time re-grant; build_app.py refuses to replace an installed app until you opt in.\n",
        encoding="utf-8")
    password = secrets.token_hex(24)
    p12_password = secrets.token_hex(16)
    keychain = f'"{SIGNING_KEYCHAIN}"'
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "cert.cnf").write_text(f"""[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN={SIGNING_NAME}
[ext]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
""", encoding="utf-8")
            subprocess.run([SYSTEM_OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(tmp_dir / "key.pem"), "-out", str(tmp_dir / "cert.pem"),
                            "-days", "7300", "-config", str(tmp_dir / "cert.cnf")],
                           check=True, capture_output=True)
            subprocess.run([SYSTEM_OPENSSL, "pkcs12", "-export", "-inkey", str(tmp_dir / "key.pem"),
                            "-in", str(tmp_dir / "cert.pem"), "-out", str(tmp_dir / "id.p12"),
                            "-name", SIGNING_NAME, "-passout", "env:TRACKER_P12_PASSWORD"],
                           check=True, capture_output=True,
                           env=dict(os.environ, TRACKER_P12_PASSWORD=p12_password))
            _security(f"create-keychain -p {password} {keychain}")
            _security(f"set-keychain-settings {keychain}")  # no auto-lock timeout
            _security(f"unlock-keychain -p {password} {keychain}")
            _security(f'import "{tmp_dir / "id.p12"}" -k {keychain} -P {p12_password} -T /usr/bin/codesign')
            # Lets codesign use the key without a GUI "allow access" prompt.
            _security(f"set-key-partition-list -S apple-tool:,apple:,codesign: -s -k {password} {keychain}")
        # Stored last: a password item exists only for a complete identity.
        _security(f'add-generic-password -U -s "{SIGNING_SERVICE}" -a "{getpass.getuser()}" -w {password}')
    except BaseException:
        subprocess.run(["security", "delete-keychain", str(SIGNING_KEYCHAIN)], capture_output=True)
        SIGNING_KEYCHAIN.unlink(missing_ok=True)
        raise


def signing_identity() -> str:
    """Return the stable local signing identity's SHA-1, or "-" (ad hoc) if unavailable.

    Ad hoc is only a fallback for first-time builds: refuse_identity_change() stops it
    from replacing an installed, already-granted app.
    """
    try:
        password = _keychain_password()
        if SIGNING_KEYCHAIN.exists() and password is None:
            if os.environ.get("TRACKER_ALLOW_IDENTITY_CHANGE") != "1":
                raise RuntimeError(f"{SIGNING_KEYCHAIN} exists but its login-keychain password item "
                                   f"({SIGNING_SERVICE}) is missing")
            orphan = SIGNING_KEYCHAIN.with_name(f"orphaned-{int(time.time())}.keychain-db")
            SIGNING_KEYCHAIN.rename(orphan)
            print(f"WARNING: signing keychain password missing; moved old keychain to {orphan}.")
        if not SIGNING_KEYCHAIN.exists():
            print(f"Creating local code-signing identity in {SIGNING_KEYCHAIN} (one time).")
            print("  Re-grant Accessibility to Tracker once after this build; later rebuilds keep it.")
            _create_signing_keychain()
            password = _keychain_password()
        # Keychains lock after reboot; codesign then fails with errSecInternalComponent.
        _security(f'unlock-keychain -p {password} "{SIGNING_KEYCHAIN}"')
        identity = _identity_hash()
        if identity is None:
            raise RuntimeError(f'no "{SIGNING_NAME}" identity in {SIGNING_KEYCHAIN}')
        return identity
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"WARNING: stable signing unavailable ({exc}); falling back to ad-hoc signing.")
        return "-"


@contextmanager
def signing_keychain_searchable(identity: str):
    """codesign only finds identities in the search list; add ours for the build only."""
    if identity == "-":
        yield
        return
    listed = subprocess.run(["security", "list-keychains", "-d", "user"],
                            capture_output=True, text=True, check=True).stdout
    # Drop a stale entry left by a killed build so it is always removed afterwards.
    original = [line.strip().strip('"') for line in listed.splitlines()
                if line.strip() and line.strip().strip('"') != str(SIGNING_KEYCHAIN)]
    subprocess.run(["security", "list-keychains", "-d", "user", "-s", *original, str(SIGNING_KEYCHAIN)], check=True)
    try:
        yield
    finally:
        subprocess.run(["security", "list-keychains", "-d", "user", "-s", *original], check=True)


def expected_requirement(bundle_id: str, identity: str) -> str | None:
    """The DR a build will get; None for ad hoc, whose DR is a per-build cdhash."""
    if identity == "-":
        return None
    return f'identifier "{bundle_id}" and certificate leaf = H"{identity.lower()}"'


def refuse_identity_change(bundle: Path, bundle_id: str, identity: str) -> None:
    """Never silently replace an app whose privacy grants a different signature would revoke."""
    installed = designated_requirement(bundle) if bundle.exists() else ""
    if not installed or installed.startswith("cdhash"):
        return
    if installed == expected_requirement(bundle_id, identity):
        return
    if os.environ.get("TRACKER_ALLOW_IDENTITY_CHANGE") == "1":
        print("WARNING: signing identity changed; re-grant Accessibility to Tracker.")
        return
    raise SystemExit(
        f"\nREFUSING TO BUILD: this build would sign {bundle.name} differently than the installed copy.\n"
        f"  installed: {installed}\n"
        f"  this build: {expected_requirement(bundle_id, identity) or 'ad hoc (cdhash)'}\n"
        "Replacing it would silently revoke Accessibility, and tracking would stop.\n"
        f"Restore {SIGNING_DIR} (and its login-keychain password), or, to accept re-granting\n"
        "permissions, rerun with TRACKER_ALLOW_IDENTITY_CHANGE=1.\n")


def designated_requirement(path: Path) -> str:
    out = subprocess.run(["codesign", "-d", "-r-", str(path)], capture_output=True, text=True)
    for line in (out.stdout + out.stderr).splitlines():
        if line.startswith("designated =>") or line.startswith("# designated =>"):
            return line.split("=>", 1)[1].strip()
    return ""


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


def copy_app_source(resources: Path, identity: str) -> Path:
    embedded = resources / "app"
    embedded.mkdir(parents=True)
    for filename in SOURCE_FILES:
        shutil.copy2(APP_DIR / filename, embedded / filename)
    for dirname in SOURCE_DIRS:
        shutil.copytree(APP_DIR / dirname, embedded / dirname)
    return embedded


def build_bundle(app_name: str, config: dict[str, object], icns: Path, identity: str) -> Path:
    bundle = DIST_DIR / f"{app_name}.app"
    if bundle.exists():
        shutil.rmtree(bundle)
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copy2(icns, resources / "icon.icns")
    copy_app_source(resources, identity)

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


def build_notification_sender(bundle: Path, icns: Path, identity: str) -> None:
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
    subprocess.run(["codesign", "--force", "--sign", identity, str(helper)], check=True)


def main() -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    identity = signing_identity()
    with signing_keychain_searchable(identity):
        for app_name, config in APPS.items():
            refuse_identity_change(DIST_DIR / f"{app_name}.app", str(config["bundle_id"]), identity)
            icns = build_icon(app_name, str(config["accent"]))
            bundle = build_bundle(app_name, config, icns, identity)
            build_notification_sender(bundle, icns, identity)
            subprocess.run(
                ["codesign", "--force", "--deep", "--sign", identity, str(bundle)],
                check=True,
            )
            # Register both the embedded notification helper and the main app bundle.
            registrar = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
            if registrar.exists():
                subprocess.run([str(registrar), "-f", str(bundle / "Contents" / "Resources" / "Tracker Notifications.app")], check=True)
                subprocess.run([str(registrar), "-f", str(bundle)], check=True)
            requirement = designated_requirement(bundle)
            print(f"Built and signed {bundle}")
            print(f"  Designated requirement: {requirement}")
            if requirement.startswith("cdhash"):
                print("WARNING: ad-hoc signature. macOS will revoke Accessibility on every rebuild;")
                print("  re-grant Tracker in System Settings > Privacy & Security > Accessibility.")

if __name__ == "__main__":
    main()
