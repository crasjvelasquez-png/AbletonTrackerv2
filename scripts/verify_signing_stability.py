"""E2E check: rebuilding Tracker.app must not revoke macOS privacy grants.

macOS stores Accessibility / Audio Capture / Notification grants against the
app's *designated requirement* (DR).  An ad-hoc signature's DR is its cdhash,
which changes on every build, so each `restart.command` silently revoked
Accessibility and the tracker stopped recording ("Accessibility permission is
unavailable").  This script builds the app twice into a scratch dist directory
and proves that the second build satisfies the DR recorded from the first --
the same check TCC performs when the rebuilt app asks for Accessibility.

Failure modes this guards against:
  1. Build falls back to ad-hoc signing -> DR is `cdhash H"..."`.
  2. Signing identity is recreated between builds -> leaf hash changes.
  3. Nested code (notification helper) signed with another identity.
  4. Signing keychain left in the user's keychain search list after a build.
  5. Signature invalid on disk (`codesign --verify --deep --strict` fails).
  6. Signing keychain locked (e.g. after reboot) and not unlocked by the build.
  7. A build that would sign with a different identity (lost keychain, ad-hoc
     fallback) silently replaces the installed, already-granted app.

Writes a JSON artifact to ~/.ableton_tracker/verification/ and exits non-zero
on any failure.  Does not touch dist/ or the running app.

    python3 scripts/verify_signing_stability.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = Path.home() / ".ableton_tracker" / "verification"
BUNDLE_ID = "com.abletontracker.tracker"


def run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def search_list() -> list[str]:
    out = run("security", "list-keychains", "-d", "user").stdout
    return [line.strip().strip('"') for line in out.splitlines() if line.strip()]


def designated(path: Path) -> str:
    out = run("codesign", "-d", "-r-", str(path))
    for line in (out.stdout + out.stderr).splitlines():
        if line.lstrip("# ").startswith("designated =>"):
            return line.split("=>", 1)[1].strip()
    return ""


def cdhash(path: Path) -> str:
    out = run("codesign", "-dvvv", str(path))
    for line in out.stderr.splitlines():
        if line.startswith("CDHash="):
            return line.split("=", 1)[1]
    return ""


def satisfies(path: Path, requirement: str, scratch: Path) -> bool:
    req = scratch / "req.bin"
    compiled = run("csreq", "-r-", "-b", str(req), input=requirement)
    if compiled.returncode:
        return False
    return run("codesign", "--verify", "-R", str(req), str(path)).returncode == 0


def build(dist: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, TRACKER_DIST_DIR=str(dist))
    return run(sys.executable, str(APP_DIR / "build_app.py"), env=env, cwd=APP_DIR)


def main() -> int:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))

    before = search_list()
    with tempfile.TemporaryDirectory(prefix="tracker-sign-") as tmp:
        scratch = Path(tmp)
        builds = []
        for n in (1, 2):
            dist = scratch / f"dist{n}"
            result = build(dist)
            tail = (result.stderr or result.stdout).strip().splitlines()
            check(f"build {n} succeeds", result.returncode == 0, tail[-1] if tail else "")
            app = dist / "Tracker.app"
            builds.append({
                "app": app,
                "dr": designated(app),
                "cdhash": cdhash(app),
                "helper_dr": designated(app / "Contents/Resources/Tracker Notifications.app"),
                "log": result.stdout[-2000:],
            })
            if n == 1:
                # Simulate a reboot between builds: the signing keychain is locked.
                keychain = Path.home() / ".ableton_tracker" / "signing" / "tracker-signing.keychain-db"
                if keychain.exists():
                    run("security", "lock-keychain", str(keychain))

        first, second = builds
        # Failure mode 7: a build that would sign differently (here: a brand-new identity
        # in a scratch signing dir) must refuse to replace a stably signed app.
        other_dir = scratch / "other signing dir"
        env = dict(os.environ, TRACKER_DIST_DIR=str(scratch / "dist2"),
                   TRACKER_SIGNING_DIR=str(other_dir))
        refused = run(sys.executable, str(APP_DIR / "build_app.py"), env=env, cwd=APP_DIR)
        # build_app namespaces the password item by signing dir; remove the scratch one.
        run("security", "delete-generic-password", "-s", f"com.abletontracker.signing:{other_dir}")
        run("security", "delete-keychain", str(other_dir / "tracker-signing.keychain-db"))
        tail = (refused.stdout + refused.stderr).strip().splitlines()
        check("build refuses to replace app with a different signature",
              refused.returncode != 0 and designated(second["app"]) == second["dr"],
              tail[-1] if tail else "")
        check("DR is not cdhash-based", bool(first["dr"]) and not first["dr"].startswith("cdhash"), first["dr"])
        check("DR names the bundle id", f'identifier "{BUNDLE_ID}"' in first["dr"])
        check("DR identical across rebuilds", first["dr"] == second["dr"], second["dr"])
        check("rebuild satisfies first build's DR (TCC check)",
              satisfies(second["app"], first["dr"], scratch))
        leaf = first["dr"].split("certificate", 1)[-1]
        check("notification helper uses the same identity", leaf in second["helper_dr"], second["helper_dr"])
        strict = run("codesign", "--verify", "--deep", "--strict", str(second["app"]))
        check("signature valid on disk (deep, strict)", strict.returncode == 0, strict.stderr.strip()[-200:])
        check("keychain search list restored", search_list() == before, " ".join(search_list()))

        installed = APP_DIR / "dist" / "Tracker.app"
        installed_dr = designated(installed) if installed.exists() else ""
        artifact = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "designated_requirement": first["dr"],
            "cdhashes": [first["cdhash"], second["cdhash"]],
            "installed_app_dr": installed_dr,
            "installed_app_matches": installed_dr == first["dr"],
            "checks": checks,
            "passed": all(c["ok"] for c in checks),
        }

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_DIR / f"signing-stability-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"\nInstalled dist/Tracker.app DR: {installed_dr or '(missing)'}"
          f"{'  [matches]' if artifact['installed_app_matches'] else '  [differs: re-run ./restart.command]'}")
    print(f"Artifact: {path}")
    return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
