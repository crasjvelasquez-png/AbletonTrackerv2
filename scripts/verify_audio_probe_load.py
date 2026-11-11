#!/usr/bin/env python3
"""End-to-end verification script for persistent audio-probe helper."""

import argparse
import atexit
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import tracker

# Isolate SQLite database to temporary test directory
TEMP_DIR = Path(tempfile.mkdtemp(prefix="tracker-probe-test-"))
atexit.register(shutil.rmtree, TEMP_DIR, True)
tracker.DB_PATH = TEMP_DIR / "tracker.sqlite"

FAKE_HELPER_CODE = """#!/usr/bin/env python3
import json
import os
import sys
import threading
import time
from pathlib import Path

# Append launch metadata to launch log
log_path = os.environ.get("FAKE_PROBE_LOG", str(Path(__file__).with_name("launch.log")))
try:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "argv": sys.argv}) + "\\n")
except Exception:
    pass

# Background thread reads stdin until EOF (parent death) then exits
def _watch_stdin():
    try:
        if sys.stdin is not None:
            while True:
                chunk = sys.stdin.read(1024)
                if not chunk:
                    break
    except Exception:
        pass
    os._exit(0)

threading.Thread(target=_watch_stdin, daemon=True).start()

# Helper mode behaviors
mode = os.environ.get("FAKE_PROBE_MODE", "active")
if mode == "active":
    while True:
        sys.stdout.write("active rms=0.1 peak=0.2 samples=48000\\n")
        sys.stdout.flush()
        time.sleep(0.2)
elif mode == "hang":
    while True:
        time.sleep(1)
elif mode == "die":
    sys.stdout.write("active rms=0.1 peak=0.2 samples=48000\\n")
    sys.stdout.flush()
    sys.exit(1)
else:
    sys.exit(0)
"""


def setup_fake_helper(temp_dir: Path) -> tuple[Path, Path]:
    helper_path = temp_dir / "fake_audio_probe"
    launch_log = temp_dir / "launch.log"
    helper_path.write_text(FAKE_HELPER_CODE, encoding="utf-8")
    helper_path.chmod(0o755)
    os.environ["FAKE_PROBE_LOG"] = str(launch_log)
    return helper_path, launch_log


def read_launches(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    lines = [l.strip() for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    launches = []
    for line in lines:
        try:
            launches.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return launches


def clear_launches(log_path: Path):
    if log_path.exists():
        log_path.write_text("", encoding="utf-8")


def assert_pid_gone(pid: int, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        stat = res.stdout.strip()
        if not stat:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, 0)
        res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        stat = res.stdout.strip()
        if "Z" in stat:
            raise AssertionError(f"Process {pid} is zombie (not reaped) after {timeout}s (stat={stat})")
        raise AssertionError(f"Process {pid} still running after {timeout}s (stat={stat})")
    except ProcessLookupError:
        return


def check_not_zombie(pid: int):
    res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    stat = res.stdout.strip()
    assert "Z" not in stat, f"Process {pid} is zombie (stat={stat})"


def scenario_reuse(fake_helper_path: Path, launch_log: Path) -> str:
    clear_launches(launch_log)
    os.environ["FAKE_PROBE_MODE"] = "active"
    try:
        for i in range(15):
            res = tracker._system_audio_level_active(4242)
            assert res is True, f"Call {i + 1} returned {res}, expected True"
            if i < 14:
                time.sleep(0.1)
        launches = read_launches(launch_log)
        assert len(launches) == 1, f"Expected exactly 1 launch logged, got {len(launches)}"
        return f"15 calls returned True, exactly 1 launch logged (helper pid {launches[0]['pid']})"
    finally:
        tracker.cancel_audio_probe()


def scenario_cancel(fake_helper_path: Path, launch_log: Path) -> str:
    clear_launches(launch_log)
    os.environ["FAKE_PROBE_MODE"] = "active"
    try:
        res1 = tracker._system_audio_level_active(4242)
        assert res1 is True, f"Initial call returned {res1}, expected True"
        launches1 = read_launches(launch_log)
        assert len(launches1) >= 1, f"Expected at least 1 launch logged, got {len(launches1)}"
        helper_pid = launches1[-1]["pid"]
        count_before = len(launches1)

        tracker.cancel_audio_probe()
        assert_pid_gone(helper_pid, timeout=2.0)

        res2 = tracker._system_audio_level_active(4242)
        assert res2 is True, f"Call after cancel returned {res2}, expected True"
        launches2 = read_launches(launch_log)
        assert len(launches2) == count_before + 1, f"Expected {count_before + 1} launches, got {len(launches2)}"
        return f"Helper pid {helper_pid} gone within 2s after cancel; relaunched (count {len(launches2)})"
    finally:
        tracker.cancel_audio_probe()


def scenario_pid_change(fake_helper_path: Path, launch_log: Path) -> str:
    clear_launches(launch_log)
    os.environ["FAKE_PROBE_MODE"] = "active"
    try:
        res1 = tracker._system_audio_level_active(4242)
        assert res1 is True, f"Call for pid 4242 returned {res1}, expected True"
        launches1 = read_launches(launch_log)
        assert len(launches1) >= 1, f"Expected at least 1 launch logged, got {len(launches1)}"
        old_pid = launches1[-1]["pid"]

        res2 = tracker._system_audio_level_active(4343)
        assert res2 is True, f"Call for pid 4343 returned {res2}, expected True"
        assert_pid_gone(old_pid, timeout=2.0)

        launches2 = read_launches(launch_log)
        assert len(launches2) >= 2, f"Expected at least 2 launches, got {len(launches2)}"
        new_launch = launches2[-1]
        assert len(new_launch["argv"]) > 3, f"Expected argv to contain pid, got {new_launch['argv']}"
        assert new_launch["argv"][4 - 1] == "4343", f"Expected argv[3]=='4343', got {new_launch['argv']}"
        return f"Old helper pid {old_pid} gone within 2s; new helper launched with argv[3]=='4343'"
    finally:
        tracker.cancel_audio_probe()


def scenario_hang(fake_helper_path: Path, launch_log: Path) -> str:
    clear_launches(launch_log)
    os.environ["FAKE_PROBE_MODE"] = "hang"
    try:
        t0 = time.monotonic()
        res = tracker._system_audio_level_active(4242)
        elapsed = time.monotonic() - t0
        timeout_limit = getattr(tracker, "AUDIO_POLL_TIMEOUT", 3.0) + 1.5
        assert res is None, f"Expected None on hang, got {res}"
        assert elapsed <= timeout_limit, f"Call took {elapsed:.2f}s, expected <= {timeout_limit:.2f}s"

        launches = read_launches(launch_log)
        assert len(launches) >= 1, "Expected launch logged for hang"
        helper_pid = launches[-1]["pid"]
        assert_pid_gone(helper_pid, timeout=2.0)
        return f"Hang returned None in {elapsed:.2f}s (<= {timeout_limit:.2f}s); helper pid {helper_pid} gone"
    finally:
        tracker.cancel_audio_probe()


def scenario_die(fake_helper_path: Path, launch_log: Path) -> str:
    clear_launches(launch_log)
    os.environ["FAKE_PROBE_MODE"] = "die"
    try:
        res1 = tracker._system_audio_level_active(4242)
        assert res1 is True, f"First call expected True, got {res1}"
        launches1 = read_launches(launch_log)
        assert len(launches1) >= 1, f"Expected launch logged, got {len(launches1)}"
        first_pid = launches1[-1]["pid"]

        time.sleep(0.5)

        res2 = tracker._system_audio_level_active(4242)
        assert res2 in (None, True), f"Second call expected None or True, got {res2}"
        launches2 = read_launches(launch_log)
        assert (
            len(launches2) > len(launches1)
            or (launches2 and launches2[-1]["pid"] != first_pid)
        ), f"Expected launch count increased or helper relaunched, had {len(launches1)}, now {len(launches2)}"

        for launch in launches2:
            check_not_zombie(launch["pid"])
        return f"First call True; second call returned {res2}; relaunched without zombie children"
    finally:
        tracker.cancel_audio_probe()


def scenario_cancel_event(fake_helper_path: Path, launch_log: Path) -> str:
    clear_launches(launch_log)
    os.environ["FAKE_PROBE_MODE"] = "hang"
    try:
        cancel_event = threading.Event()
        outcome = {}
        errs = []

        def target():
            try:
                outcome["res"] = tracker._system_audio_level_active(4242, cancel_event=cancel_event)
                outcome["finished_at"] = time.monotonic()
            except Exception as e:
                errs.append(e)

        t = threading.Thread(target=target)
        t.start()
        time.sleep(0.3)
        t_set = time.monotonic()
        cancel_event.set()
        t.join(timeout=2.0)
        assert not t.is_alive(), "Call did not return after cancel_event was set"
        if errs:
            raise errs[0]
        elapsed_after_set = outcome.get("finished_at", time.monotonic()) - t_set
        assert elapsed_after_set <= 1.0, f"Call returned {elapsed_after_set:.2f}s after cancel_event (expected <= 1.0s)"
        assert outcome.get("res") is None, f"Expected None on cancel_event, got {outcome.get('res')}"
        return f"Returned None {elapsed_after_set:.2f}s after cancel_event set (<= 1.0s)"
    finally:
        tracker.cancel_audio_probe()


def scenario_cancel_quiet(fake_helper_path: Path, launch_log: Path) -> str:
    # Normal cancellation (input resumed) must not be reported as a probe failure.
    clear_launches(launch_log)
    os.environ["FAKE_PROBE_MODE"] = "active"
    errors = []
    with patch.object(tracker, "_set_audio_probe_error", side_effect=errors.append):
        assert tracker._system_audio_level_active(4242) is True
        for _ in range(5):
            cancel_event = threading.Event()
            outcome = {}
            def call():
                outcome["res"] = tracker._system_audio_level_active(4242, cancel_event=cancel_event)
                outcome["after_cancel"] = cancel_event.is_set()
            t = threading.Thread(target=call)
            t.start()
            time.sleep(0.05)
            cancel_event.set()
            tracker.cancel_audio_probe()
            t.join(timeout=2.0)
            assert not t.is_alive(), "Call did not return after cancel"
            # A line that arrived before the cancel may legitimately return True.
            if outcome.get("after_cancel"):
                assert outcome.get("res") is None, f"Expected None after cancel, got {outcome.get('res')}"
    reported = [e for e in errors if e]
    assert not reported, f"Cancellation reported errors: {reported}"
    return "5 mid-wait cancellations returned None with no error logged"


def scenario_parent_exit(fake_helper_path: Path, launch_log: Path, repo_root: Path, temp_dir: Path) -> str:
    clear_launches(launch_log)
    os.environ["FAKE_PROBE_MODE"] = "active"
    try:
        child_code = f"""import sys, os, time, json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, {repr(str(repo_root))})
import tracker
tracker.DB_PATH = Path({repr(str(temp_dir / 'tracker.sqlite'))})

with patch.object(tracker, '_ensure_audio_probe_binary', return_value=Path({repr(str(fake_helper_path))})):
    tracker._system_audio_level_active(4242)
    log_file = Path({repr(str(launch_log))})
    if log_file.exists():
        lines = [l.strip() for l in log_file.read_text(encoding='utf-8').splitlines() if l.strip()]
        if lines:
            entry = json.loads(lines[-1])
            print(entry['pid'])
            sys.stdout.flush()
    os._exit(0)
"""
        proc = subprocess.run(
            [sys.executable, "-c", child_code],
            capture_output=True,
            text=True,
            timeout=10,
            env=dict(os.environ),
        )
        assert proc.returncode == 0, f"Child process failed (code {proc.returncode}): {proc.stderr}"
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        assert lines, f"Child process did not output helper pid. stderr: {proc.stderr}"
        helper_pid = int(lines[-1])
        assert_pid_gone(helper_pid, timeout=3.0)
        return f"Child process exited without cancel; helper pid {helper_pid} exited within 3s"
    finally:
        tracker.cancel_audio_probe()


def run_scenario(name: str, fn, *args, **kwargs) -> dict:
    try:
        details = fn(*args, **kwargs)
        return {"name": name, "passed": True, "details": str(details)}
    except AssertionError as e:
        return {"name": name, "passed": False, "details": f"AssertionError: {e}"}
    except Exception as e:
        return {"name": name, "passed": False, "details": f"{type(e).__name__}: {e}"}
    finally:
        try:
            tracker.cancel_audio_probe()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/tracker-audio-probe-load.json"),
        help="Path to write JSON artifact (default: /tmp/tracker-audio-probe-load.json)",
    )
    args = parser.parse_args()

    fake_helper_path, launch_log = setup_fake_helper(TEMP_DIR)
    scenarios_results = []

    with patch.object(tracker, "_ensure_audio_probe_binary", return_value=fake_helper_path):
        scenarios_results.append(
            run_scenario("reuse", scenario_reuse, fake_helper_path, launch_log)
        )
        scenarios_results.append(
            run_scenario("cancel", scenario_cancel, fake_helper_path, launch_log)
        )
        scenarios_results.append(
            run_scenario("pid_change", scenario_pid_change, fake_helper_path, launch_log)
        )
        scenarios_results.append(
            run_scenario("hang", scenario_hang, fake_helper_path, launch_log)
        )
        scenarios_results.append(
            run_scenario("die", scenario_die, fake_helper_path, launch_log)
        )
        scenarios_results.append(
            run_scenario("cancel_event", scenario_cancel_event, fake_helper_path, launch_log)
        )
        scenarios_results.append(
            run_scenario("cancel_quiet", scenario_cancel_quiet, fake_helper_path, launch_log)
        )
        scenarios_results.append(
            run_scenario("parent_exit", scenario_parent_exit, fake_helper_path, launch_log, REPO_ROOT, TEMP_DIR)
        )

    all_passed = all(s["passed"] for s in scenarios_results)
    artifact = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python": platform.python_version(),
        "scenarios": scenarios_results,
        "all_passed": all_passed,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(artifact, indent=2))

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
