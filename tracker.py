#!/usr/bin/env python3
"""Ableton Live project time tracker daemon."""

import sqlite3
import subprocess
import time
import signal
import sys
import re
import tempfile
import threading
from collections import defaultdict
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

STATE_TRACKING = "tracking"
STATE_IDLE_PAUSED = "idle_paused"
STATE_PAUSED = "paused"
STATE_ABLETON_OPEN = "ableton_open"
STATE_ABLETON_CLOSED = "ableton_closed"

POLL_INTERVAL = 30  # seconds between checks
IDLE_THRESHOLD = 30  # seconds of HID + audio idle before tracking pauses
AUDIO_POLL_TIMEOUT = 3  # seconds before giving up on the audio probe subprocess
AUDIO_LEVEL_POLL_SECONDS = 1.0
# -70 dBFS peak (≈0.000316 linear) — low enough to clear plugin white-noise
# residue without missing real playback. RMS uses the same floor; the probe
# treats audio as active when either peak or RMS crosses it.
AUDIO_LEVEL_ACTIVE_THRESHOLD = 0.000316
CLEANUP_INTERVAL = 15 * 60  # seconds between background cleanup passes
SESSION_CONDENSE_GAP_SECONDS = 5 * 60
TRACKER_MAX_RETRIES = 5
TRACKER_MAX_BACKOFF = 60
DB_PATH = Path.home() / ".ableton_tracker" / "sessions.db"
PAUSE_FILE = Path.home() / ".ableton_tracker" / "paused"
UNTITLED_NAMES = {"untitled", "untitled project"}
LIVE_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(Ableton\s*)?Live\s*\d*\s*(Suite|Standard|Lite|Intro|Trial)?\s*$",
    flags=re.IGNORECASE,
)
NON_PROJECT_TITLE_PATTERNS = [
    r"^export audio(?:\/video)?(?:\.\.\.)?$",
    r"^exporting\b",
    r"^rendering\b",
    r"^preferences$",
    r"^settings$",
    r"^open live set$",
    r"^save live set$",
    r"^save live set as$",
    r"^collect all and save$",
    r"^manage files$",
]
_NON_PROJECT_TITLE_RES = [re.compile(p, re.IGNORECASE) for p in NON_PROJECT_TITLE_PATTERNS]
_WHITESPACE_RE = re.compile(r"\s+")
_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_ALS_SUFFIX_RE = re.compile(r"\.als\s*$", re.IGNORECASE)


def setup_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name   TEXT    NOT NULL,
                start_time     REAL    NOT NULL,
                last_seen_time REAL,
                end_time       REAL,
                active_seconds REAL    DEFAULT 0
            )
        """)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "last_seen_time" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_seen_time REAL")
        conn.execute("""
            UPDATE sessions
            SET last_seen_time = COALESCE(last_seen_time, end_time, start_time)
            WHERE last_seen_time IS NULL
        """)
        if "notes" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN notes TEXT DEFAULT ''")
        if "todo_notes" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN todo_notes TEXT DEFAULT ''")
        if "todos_json" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN todos_json TEXT DEFAULT '[]'")
        conn.execute("""
            UPDATE sessions SET notes = COALESCE(notes, '') WHERE notes IS NULL
        """)
        conn.execute("""
            UPDATE sessions SET todo_notes = COALESCE(todo_notes, '') WHERE todo_notes IS NULL
        """)
        conn.execute("""
            UPDATE sessions SET todos_json = COALESCE(todos_json, '[]') WHERE todos_json IS NULL
        """)
        conn.commit()


def is_phantom_session_name(name: str | None) -> bool:
    """Return True for closed rows that are clearly not real Live sets."""
    raw = (name or "").strip()
    if not raw:
        return False

    lowered = _WHITESPACE_RE.sub(" ", raw).strip().lower()
    if lowered in UNTITLED_NAMES:
        return False

    if any(r.match(lowered) for r in _NON_PROJECT_TITLE_RES):
        return True

    # Slash characters cannot appear in Live set filenames on macOS, but
    # plugin/editor windows often use them in their titles.
    if "/" in raw:
        return True

    return False


def _phantom_session_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT id, project_name
        FROM sessions
        WHERE end_time IS NOT NULL
        """
    ).fetchall()
    return [row[0] for row in rows if is_phantom_session_name(row[1])]


def count_phantom_sessions() -> int:
    if not DB_PATH.exists():
        return 0
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        return len(_phantom_session_ids(conn))


def cleanup_phantom_sessions() -> dict:
    """Delete clearly invalid historical rows left by older builds."""
    if not DB_PATH.exists():
        return {"ok": True, "deleted": 0}

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        ids = _phantom_session_ids(conn)
        if not ids:
            return {"ok": True, "deleted": 0}

        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", ids)
        conn.commit()
        return {"ok": True, "deleted": len(ids)}


def close_stale_open_sessions() -> int:
    """Close leftover open rows from a previous tracker instance."""
    if not DB_PATH.exists():
        return 0

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        rows = conn.execute(
            """
            SELECT id, COALESCE(last_seen_time, start_time)
            FROM sessions
            WHERE end_time IS NULL
            """
        ).fetchall()
        if not rows:
            return 0

        conn.executemany(
            """
            UPDATE sessions
            SET last_seen_time = COALESCE(last_seen_time, ?),
                end_time = ?
            WHERE id = ?
            """,
            [(ts, ts, row_id) for row_id, ts in rows],
        )
        conn.commit()
        return len(rows)


def session_end_time(session) -> float:
    """Best-known session boundary for closed or still-open rows."""
    return float(
        session.get("end_time")
        or session.get("last_seen_time")
        or session.get("start_time")
        or 0
    )


def condense_recent_sessions(
    rows,
    max_gap_seconds: float = SESSION_CONDENSE_GAP_SECONDS,
):
    """Merge adjacent same-project fragments separated by short pauses."""
    condensed = []

    for raw_row in rows:
        row = dict(raw_row)
        row["active_seconds"] = float(row.get("active_seconds") or 0)
        row["start_time"] = float(row.get("start_time") or 0)
        row_id = row.get("id")
        row["session_ids"] = [int(row_id)] if row_id is not None else []

        if condensed:
            current = condensed[-1]
            gap_seconds = current["start_time"] - session_end_time(row)
            same_project = current["project_name"] == row["project_name"]
            if same_project and gap_seconds < max_gap_seconds:
                current["start_time"] = min(current["start_time"], row["start_time"])
                current["active_seconds"] += row["active_seconds"]
                current["last_seen_time"] = max(
                    session_end_time(current),
                    session_end_time(row),
                )
                current["session_ids"].extend(row["session_ids"])
                if row.get("end_time") is None:
                    current["end_time"] = None
                continue

        condensed.append(row)

    return condensed


def allocate_session_activity(
    start_time: float,
    end_time: float,
    active_seconds: float,
) -> list[tuple[str, int, float]]:
    """Split a session's active time across the hour/day buckets it spans."""
    active = max(float(active_seconds or 0), 0.0)
    start = float(start_time or 0)
    end = max(float(end_time or start), start)

    if active <= 0:
        return []

    start_dt = datetime.fromtimestamp(start).replace(minute=0, second=0, microsecond=0)
    if end <= start:
        return [(start_dt.date().isoformat(), start_dt.hour, active)]

    allocations = []
    span_seconds = end - start
    cursor = start

    while cursor < end:
        cursor_dt = datetime.fromtimestamp(cursor)
        next_hour_dt = cursor_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        next_boundary = min(end, next_hour_dt.timestamp())
        slice_seconds = max(next_boundary - cursor, 0.0)
        if slice_seconds > 0:
            allocated = active * (slice_seconds / span_seconds)
            allocations.append((cursor_dt.date().isoformat(), cursor_dt.hour, allocated))
        cursor = next_boundary

    return allocations


def build_activity_rollups(rows) -> tuple[dict[str, float], dict[tuple[str, int], float]]:
    daily = defaultdict(float)
    hourly = defaultdict(float)

    for row in rows:
        start_time = row["start_time"]
        end_time = row["end_time"] or row["last_seen_time"] or row["start_time"]
        active_seconds = row["active_seconds"] or 0

        for day_key, hour, seconds in allocate_session_activity(start_time, end_time, active_seconds):
            daily[day_key] += seconds
            hourly[(day_key, hour)] += seconds

    return daily, hourly


def day_seconds(target_day: date) -> float:
    """Return tracked seconds allocated to the given local calendar day."""
    if not DB_PATH.exists():
        return 0.0

    day_start = datetime.combine(target_day, datetime.min.time()).timestamp()
    day_end = datetime.combine(target_day + timedelta(days=1), datetime.min.time()).timestamp()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT start_time, last_seen_time, end_time, active_seconds
            FROM sessions
            WHERE active_seconds > 0
              AND start_time < ?
              AND COALESCE(end_time, last_seen_time, start_time) >= ?
            """,
            (day_end, day_start),
        ).fetchall()

    daily, _ = build_activity_rollups(rows)
    return daily.get(target_day.isoformat(), 0.0)


def _all_daily_totals() -> dict[str, float]:
    """Return {iso_date: total_active_seconds} for all tracked days."""
    if not DB_PATH.exists():
        return {}
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT start_time, last_seen_time, end_time, active_seconds
            FROM sessions
            WHERE active_seconds > 0
            """
        ).fetchall()
    daily, _ = build_activity_rollups(rows)
    return daily


def compute_streak_days(
    daily_totals: dict[str, float], today: date | None = None
) -> int:
    """Consecutive days with activity ending today (or yesterday right after midnight)."""
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    active_days = {day for day, seconds in daily_totals.items() if seconds > 0}
    streak = 0
    cursor = None
    if today.isoformat() in active_days:
        cursor = today
    elif yesterday.isoformat() in active_days:
        cursor = yesterday
    while cursor and cursor.isoformat() in active_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def streak_days(today: date | None = None) -> int:
    """Current streak from the DB."""
    return compute_streak_days(_all_daily_totals(), today)


def yesterday_seconds() -> float:
    """Tracked seconds for yesterday."""
    return day_seconds((date.today() - timedelta(days=1)))


def week_seconds(target_date: date | None = None) -> float:
    """Tracked seconds for the current tracking week (respects week_start_weekday setting)."""
    if not DB_PATH.exists():
        return 0.0
    target_date = target_date or date.today()
    raw = get_app_setting("week_start_weekday")
    week_start_weekday = int(raw) if raw is not None else 4
    week_start = target_date - timedelta(
        days=(target_date.weekday() - week_start_weekday) % 7
    )
    week_end = week_start + timedelta(days=7)  # exclusive upper bound

    week_start_ts = datetime.combine(week_start, datetime.min.time()).timestamp()
    week_end_ts = datetime.combine(week_end, datetime.min.time()).timestamp()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT start_time, last_seen_time, end_time, active_seconds
            FROM sessions
            WHERE active_seconds > 0
              AND start_time < ?
              AND COALESCE(end_time, last_seen_time, start_time) >= ?
            """,
            (week_end_ts, week_start_ts),
        ).fetchall()

    daily, _ = build_activity_rollups(rows)
    week_days = {(week_start + timedelta(days=i)).isoformat() for i in range(7)}
    return sum(daily.get(day, 0.0) for day in week_days)


def get_app_setting(key: str, default: str | None = None) -> str | None:
    """Read an app_setting from the DB (re-exports from dashboard for convenience)."""
    from dashboard import get_app_setting as _gas

    return _gas(key, default)


def _live_pid() -> int | None:
    r = subprocess.run(["pgrep", "-x", "Live"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def is_ableton_running() -> bool:
    return _live_pid() is not None


_error_state: dict[str, str | None] = {}
_ERROR_STATE_LOCK = threading.Lock()


def _log_transient_error(
    channel: str,
    message: str | None,
    fail_label: str,
    recover_label: str,
    suffix: str = "",
):
    """De-duplicate repeated subprocess error logs across polls.

    Only prints when the message changes from the previous one for `channel`,
    so a persistent failure logs once and a recovery logs once.
    """
    with _ERROR_STATE_LOCK:
        if message == _error_state.get(channel):
            return
        _error_state[channel] = message
    if message:
        print(f"[{_ts()}] {fail_label}: {message}{suffix}")
    else:
        print(f"[{_ts()}] {recover_label}")


def _set_idle_error(message: str | None):
    _log_transient_error(
        "idle", message, "HIDIdleTime read failed", "HIDIdleTime read recovered"
    )


def get_idle_seconds() -> float:
    """Seconds since last mouse/keyboard activity (macOS HIDIdleTime)."""
    try:
        r = subprocess.run(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception as e:
        _set_idle_error(f"subprocess: {e}")
        return float("inf")
    if r.returncode != 0:
        _set_idle_error(f"ioreg exit {r.returncode}: {r.stderr.strip()[:200]}")
        return float("inf")
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', r.stdout)
    if not m:
        _set_idle_error(f"no HIDIdleTime in {len(r.stdout)} bytes of ioreg output")
        return float("inf")
    _set_idle_error(None)
    return int(m.group(1)) / 1_000_000_000  # ns → s


@dataclass(frozen=True)
class TrackerStatus:
    state: str = STATE_ABLETON_CLOSED
    paused: bool = False
    running: bool = False
    project_name: str | None = None
    resume_hint_project: str | None = None
    hid_idle_seconds: float = 0.0
    audio_active: bool = False
    audio_idle_seconds: float = float("inf")
    idle_paused: bool = False
    checked_at: float = 0.0


def _parse_audio_level_probe(output: str) -> bool | None:
    for line in (output or "").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "active":
            return True
        if parts[0] == "quiet":
            return False
        if parts[0] == "unavailable":
            return None
    return None


def _set_audio_probe_error(message: str | None):
    _log_transient_error(
        "audio_probe",
        message,
        "audio-level probe unavailable",
        "audio-level probe recovered",
    )


_AUDIO_PROBE_SOURCE = """
import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

final class AudioProbe: NSObject, SCStreamOutput {
    var peak: Float = 0
    var sumOfSquares: Double = 0
    var sampleCount: Int = 0

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        var blockBuffer: CMBlockBuffer?
        var audioBufferList = AudioBufferList()
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: 0,
            blockBufferOut: &blockBuffer
        )

        let buffers = UnsafeMutableAudioBufferListPointer(&audioBufferList)
        for buffer in buffers {
            guard let data = buffer.mData else { continue }
            let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
            let samples = data.bindMemory(to: Float.self, capacity: count)
            for index in 0..<count {
                let sample = samples[index]
                let absSample = abs(sample)
                if absSample > peak { peak = absSample }
                sumOfSquares += Double(sample) * Double(sample)
                sampleCount += 1
            }
        }
    }
}

let args = CommandLine.arguments
let pollSeconds = args.count > 1 ? (Double(args[1]) ?? 1.0) : 1.0
let activeThreshold = args.count > 2 ? (Double(args[2]) ?? 0.01) : 0.01

let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
guard let display = content.displays.first else {
    print("quiet 0")
    exit(0)
}

let filter = SCContentFilter(display: display, excludingWindows: [])
let config = SCStreamConfiguration()
config.capturesAudio = true
config.excludesCurrentProcessAudio = true
config.width = 2
config.height = 2
config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

let probe = AudioProbe()
let stream = SCStream(filter: filter, configuration: config, delegate: nil)
try stream.addStreamOutput(probe, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio-probe"))
try await stream.startCapture()
try await Task.sleep(nanoseconds: UInt64(pollSeconds * 1_000_000_000))
try await stream.stopCapture()

let rms = probe.sampleCount > 0 ? sqrt(probe.sumOfSquares / Double(probe.sampleCount)) : 0
if probe.sampleCount == 0 {
    print("unavailable samples=0")
} else if rms >= activeThreshold || Double(probe.peak) >= activeThreshold {
    print("active rms=\\(rms) peak=\\(probe.peak) samples=\\(probe.sampleCount)")
} else {
    print("quiet rms=\\(rms) peak=\\(probe.peak) samples=\\(probe.sampleCount)")
}
"""

_AUDIO_PROBE_BINARY = DB_PATH.parent / "audio_probe"
_AUDIO_PROBE_BUILD_LOCK = threading.Lock()


def _ensure_audio_probe_binary() -> Path | None:
    """Compile the Swift audio probe to a cached binary if needed.

    Rebuilds when the binary is missing or older than this source file, so
    edits to _AUDIO_PROBE_SOURCE take effect on the next launch. Returns the
    binary path on success, None on compile failure (caller logs).
    """
    binary = _AUDIO_PROBE_BINARY
    tracker_module_mtime = Path(__file__).stat().st_mtime
    if binary.exists() and binary.stat().st_mtime >= tracker_module_mtime:
        return binary

    with _AUDIO_PROBE_BUILD_LOCK:
        if binary.exists() and binary.stat().st_mtime >= tracker_module_mtime:
            return binary
        binary.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".swift", delete=False) as src:
            src.write(_AUDIO_PROBE_SOURCE)
            src_path = src.name
        try:
            r = subprocess.run(
                ["swiftc", "-O", "-o", str(binary), src_path],
                capture_output=True, text=True, timeout=60,
            )
        except Exception as e:
            _set_audio_probe_error(f"swiftc: {e}")
            return None
        finally:
            with suppress(FileNotFoundError):
                Path(src_path).unlink()
        if r.returncode != 0:
            _set_audio_probe_error(
                f"swiftc exited {r.returncode}: {(r.stderr or '').strip()[:300]}"
            )
            return None
    return binary


def _system_audio_level_active() -> bool | None:
    """Sample actual macOS system output level using ScreenCaptureKit.

    This is intentionally level-based instead of a Live CPU/process proxy:
    a stopped transport or idle audio engine should read quiet, while real
    playback from Live (or any audible system output) keeps auto-idle awake.
    """
    binary = _ensure_audio_probe_binary()
    if binary is None:
        return None
    try:
        r = subprocess.run(
            [str(binary), str(AUDIO_LEVEL_POLL_SECONDS), str(AUDIO_LEVEL_ACTIVE_THRESHOLD)],
            capture_output=True,
            text=True,
            timeout=max(AUDIO_POLL_TIMEOUT, AUDIO_LEVEL_POLL_SECONDS + 4),
        )
    except (OSError, subprocess.SubprocessError) as e:
        _set_audio_probe_error(str(e))
        return None

    if r.returncode != 0:
        error_lines = (r.stderr or "").strip().splitlines()
        error = next(
            (
                line.strip()
                for line in error_lines
                if "TCC" in line
                or "permission" in line.lower()
                or "declined" in line.lower()
                or "SCStreamError" in line
            ),
            next(
                (
                    line.strip()
                    for line in error_lines
                    if "capture" in line.lower()
                ),
                error_lines[-1].strip() if error_lines else f"swift exited {r.returncode}",
            ),
        )
        _set_audio_probe_error(error)
        return None
    raw_line = next(
        (line.strip() for line in (r.stdout or "").splitlines() if line.strip()),
        "",
    )
    parsed = _parse_audio_level_probe(r.stdout)
    if parsed is None:
        if raw_line.startswith("unavailable"):
            _set_audio_probe_error(raw_line)
        else:
            _set_audio_probe_error("no audio level result")
        return None
    _set_audio_probe_error(None)
    if raw_line:
        print(f"[{_ts()}] audio probe: {raw_line}")
    return parsed


def is_audio_active() -> bool | None:
    """True when audible output is present while Ableton Live is open.

    Returns None when the audio probe ran but could not produce a reliable
    signal. That is different from quiet: treating an unavailable probe as
    silence can falsely pause long passive listens.
    """
    if _live_pid() is None:
        return False

    return _system_audio_level_active()


def get_live_window_titles() -> list[str]:
    """Return visible Ableton Live window titles.

    Logs (once) when System Events refuses — without that, an Accessibility
    permission failure looks identical to "Ableton has no open windows".
    """
    script = """
tell application "System Events"
    if not (exists process "Live") then return ""
    if (count of windows of process "Live") = 0 then return ""
    set AppleScript's text item delimiters to linefeed
    return (name of every window of process "Live") as text
end tell
"""
    try:
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as e:
        _log_transient_error(
            "title", str(e), "window-title lookup failed", "window-title lookup recovered"
        )
        return []

    if r.returncode != 0:
        err = r.stderr.strip()
        hint = ""
        if "1719" in err or "assistive access" in err.lower() or "-25211" in err:
            hint = (
                "  → Grant Accessibility to this Python in System Settings → "
                "Privacy & Security → Accessibility "
                f"({sys.executable})"
            )
        _log_transient_error(
            "title",
            err,
            "osascript error reading Live windows",
            "window-title lookup recovered",
            suffix=hint,
        )
        return []

    _log_transient_error(
        "title", None, "window-title lookup failed", "window-title lookup recovered"
    )
    return [title.strip() for title in r.stdout.splitlines() if title.strip()]


def parse_project_title(title: str) -> str | None:
    """Extract a real Live set name from a window title.

    Live sometimes exposes bare window titles like "My Song" or "Untitled"
    instead of the fuller "My Song - Ableton Live 12 Suite" form. We accept
    both, while still rejecting obvious dialogs and plugin/editor windows.
    """
    raw = (title or "").strip()
    if not raw:
        return None

    if LIVE_TITLE_SUFFIX_RE.search(raw):
        name = LIVE_TITLE_SUFFIX_RE.sub("", raw).strip()
    else:
        # Live set names come from filenames, so "/" is a strong signal this is
        # a plugin/editor window rather than a project.
        if "/" in raw:
            return None
        name = raw

    name = _BRACKET_RE.sub("", name).strip()
    name = _PAREN_RE.sub("", name).strip()
    name = _ALS_SUFFIX_RE.sub("", name).strip()
    name = _WHITESPACE_RE.sub(" ", name).strip(" -–—")

    if not name or name.lower() in {"", "ableton live", "live"}:
        return None

    lowered = name.lower()
    if any(r.match(lowered) for r in _NON_PROJECT_TITLE_RES):
        return None

    return name


def get_project_name(current_project: str | None = None) -> str | None:
    """Resolve the current Ableton project while ignoring transient windows.

    Returns None if no reliable project title is visible; callers should keep
    the current session alive rather than rotating to a guess.
    """
    candidates: list[str] = []
    distinct: dict[str, str] = {}
    distinct_live: dict[str, str] = {}
    for title in get_live_window_titles():
        parsed = parse_project_title(title)
        if not parsed:
            continue
        candidates.append(parsed)
        key = parsed.casefold()
        distinct.setdefault(key, parsed)
        if LIVE_TITLE_SUFFIX_RE.search(title):
            distinct_live.setdefault(key, parsed)

    if not candidates:
        return None

    if current_project:
        if current_project in candidates:
            return current_project

        # A single canonical Live title is a reliable project switch.
        if len(distinct_live) == 1:
            return next(iter(distinct_live.values()))
        if len(distinct_live) > 1:
            return None

        # When Live is only exposing bare titles, accept a single distinct
        # parsed candidate as a real project switch. This fixes the common case
        # where the main project window title is just the set name.
        if len(distinct) == 1:
            return next(iter(distinct.values()))

        # Otherwise keep the current session alive rather than rotating to a
        # new bare title guess just because the visible windows are transient.
        return None

    for candidate in candidates:
        if candidate.lower() not in UNTITLED_NAMES:
            return candidate

    return candidates[0]


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Tracker:
    def __init__(self):
        self.session_id = None
        self.project_name = None
        self.last_tick = None
        self.resume_hint_project = None
        self.next_cleanup_at = 0.0
        self.last_audio_active = 0.0
        self.last_running = False
        self.last_hid_idle = 0.0
        self.last_audio_is_active = False
        self.last_audio_idle = float("inf")
        self.last_checked_at = 0.0
        self.last_state = STATE_ABLETON_CLOSED
        self._consecutive_failures = 0
        self._last_error: str | None = None

        # --- One-time initialization (unified entry point) ---
        setup_db()
        stale = close_stale_open_sessions()
        if stale:
            print(f"[{_ts()}] closed {stale} stale open session{'s' if stale != 1 else ''}")
        self.maybe_run_cleanup(force=True)
        # Pay swiftc compile cost up front so the first audio probe doesn't stall a poll.
        if _ensure_audio_probe_binary() is None:
            print(f"[{_ts()}] audio probe unavailable at startup — will retry on first use")

    def _start(self, project: str):
        now = time.time()
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
            resumed_row = conn.execute(
                """
                SELECT id
                FROM sessions
                WHERE project_name = ?
                  AND end_time IS NOT NULL
                  AND (? - COALESCE(end_time, last_seen_time, start_time)) < ?
                ORDER BY COALESCE(end_time, last_seen_time, start_time) DESC
                LIMIT 1
                """,
                (project, now, SESSION_CONDENSE_GAP_SECONDS),
            ).fetchone()

            if resumed_row:
                conn.execute(
                    """
                    UPDATE sessions
                    SET last_seen_time=?, end_time=NULL
                    WHERE id=?
                    """,
                    (now, resumed_row[0]),
                )
                self.session_id = resumed_row[0]
            else:
                cur = conn.execute(
                    """
                    INSERT INTO sessions (project_name, start_time, last_seen_time)
                    VALUES (?, ?, ?)
                    """,
                    (project, now, now),
                )
                self.session_id = cur.lastrowid
            conn.commit()
        self.project_name = project
        self.last_tick = now
        self.resume_hint_project = None
        if resumed_row:
            print(f"[{_ts()}] ↺  {project}")
        else:
            print(f"[{_ts()}] ▶  {project}")

    def _tick(self):
        if self.session_id is None:
            return
        now = time.time()
        # Cap elapsed to 2× poll interval so computer sleep isn't counted
        elapsed = min(now - self.last_tick, POLL_INTERVAL * 2) if self.last_tick else POLL_INTERVAL
        self.last_tick = now
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
            cur = conn.execute(
                """
                UPDATE sessions
                SET last_seen_time=?, active_seconds=active_seconds+?
                WHERE id=?
                """,
                (now, elapsed, self.session_id),
            )
            conn.commit()
        if cur.rowcount == 0:
            self.session_id = None
            self.project_name = None
            self.last_tick = None

    def _close(self, preserve_resume_hint: bool = False):
        if self.session_id is None:
            if not preserve_resume_hint:
                self.resume_hint_project = None
            return
        now = time.time()
        name = (self.project_name or "").strip().lower()
        project_name = self.project_name
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
            row = conn.execute(
                "SELECT active_seconds FROM sessions WHERE id=?", (self.session_id,)
            ).fetchone()
            active = row[0] if row else 0
            if active < 5:
                # Drop phantom sub-5s rows (title flickers).
                conn.execute("DELETE FROM sessions WHERE id=?", (self.session_id,))
                print(f"[{_ts()}] ✗  {self.project_name} (too short — discarded)")
            else:
                conn.execute(
                    """
                    UPDATE sessions
                    SET last_seen_time=?, end_time=?
                    WHERE id=? AND end_time IS NULL
                    """,
                    (now, now, self.session_id),
                )
                print(f"[{_ts()}] ■  {self.project_name}")
            conn.commit()
        if preserve_resume_hint and project_name:
            self.resume_hint_project = project_name
        else:
            self.resume_hint_project = None
        self.session_id = None
        self.project_name = None
        self.last_tick = None

    def status(self) -> TrackerStatus:
        state = self.last_state
        return TrackerStatus(
            state=state,
            paused=state == STATE_PAUSED,
            running=self.last_running,
            project_name=self.project_name,
            resume_hint_project=self.resume_hint_project,
            hid_idle_seconds=self.last_hid_idle,
            audio_active=self.last_audio_is_active,
            audio_idle_seconds=self.last_audio_idle,
            idle_paused=state == STATE_IDLE_PAUSED,
            checked_at=self.last_checked_at,
        )

    def poll_once(self, paused: bool = False):
        now = time.time()
        self.last_checked_at = now

        if paused:
            self.last_running = is_ableton_running()
            self.last_hid_idle = 0.0
            self.last_audio_is_active = False
            self.last_audio_idle = float("inf")
            self._close(preserve_resume_hint=True)
            self.last_state = STATE_PAUSED
            return

        if not is_ableton_running():
            self.last_running = False
            self.last_hid_idle = 0.0
            self.last_audio_is_active = False
            self.last_audio_idle = float("inf")
            self._close()
            self.last_state = STATE_ABLETON_CLOSED
            return

        self.last_running = True

        self.last_hid_idle = get_idle_seconds()
        if self.last_hid_idle == float("inf"):
            self.last_audio_is_active = False
            self.last_audio_idle = float("inf")
            if self.session_id is None and self.resume_hint_project:
                self._start(self.resume_hint_project)
            self._tick()
            self.last_state = STATE_TRACKING if self.session_id is not None else STATE_ABLETON_OPEN
            return
        should_check_audio = (
            self.last_hid_idle >= IDLE_THRESHOLD
            or self.last_state == STATE_IDLE_PAUSED
        )
        if should_check_audio:
            audio_active = is_audio_active()
            audio_known = audio_active is not None
            self.last_audio_is_active = audio_active is True
            if audio_active is True:
                self.last_audio_active = now
        else:
            audio_known = True
            self.last_audio_is_active = False
        self.last_audio_idle = (
            now - self.last_audio_active if self.last_audio_active else float("inf")
        )
        idle_paused = should_check_audio and audio_known and (
            self.last_hid_idle >= IDLE_THRESHOLD
            and self.last_audio_idle >= IDLE_THRESHOLD
        )
        if should_check_audio:
            audio_idle_label = (
                "inf" if self.last_audio_idle == float("inf")
                else f"{int(self.last_audio_idle)}s"
            )
            audio_label = (
                str(self.last_audio_is_active) if audio_known else "unknown"
            )
            print(
                f"[{_ts()}] check  hid_idle={int(self.last_hid_idle)}s  "
                f"audio_active={audio_label}  "
                f"audio_idle={audio_idle_label}  "
                f"→ {'pause' if idle_paused else 'continue'}"
            )
        if idle_paused:
            if self.session_id is not None:
                print(
                    f"[{_ts()}] ⏸  {self.project_name} "
                    f"(idle {int(self.last_hid_idle)}s, audio quiet)"
                )
            self._close(preserve_resume_hint=True)
            self.last_state = (
                STATE_IDLE_PAUSED if self.resume_hint_project else STATE_ABLETON_OPEN
            )
            return

        project = get_project_name(self.project_name or self.resume_hint_project)
        if project is None:
            # Title lookup failed transiently — keep ticking the
            # current session rather than rotating to a guess.
            if self.session_id is None and self.resume_hint_project:
                self._start(self.resume_hint_project)
            self._tick()
            self.last_state = (
                STATE_TRACKING if self.session_id is not None else STATE_ABLETON_OPEN
            )
            return

        if project != self.project_name:
            self._close()
            self._start(project)
        self._tick()
        self.last_state = STATE_TRACKING

    def maybe_run_cleanup(self, force: bool = False):
        now = time.time()
        if not force and now < self.next_cleanup_at:
            return

        result = cleanup_phantom_sessions()
        deleted = result.get("deleted", 0)
        if deleted:
            print(f"[{_ts()}] cleaned {deleted} phantom session{'s' if deleted != 1 else ''}")
        self.next_cleanup_at = now + CLEANUP_INTERVAL


    def run(self, stop_event: threading.Event | None = None, wake_interval: float = POLL_INTERVAL, _lock: threading.Lock | None = None):
        """Sole public loop entry point.

        Standalone (stop_event=None): registers OS signal handlers, runs forever.
        Threaded (stop_event + _lock provided): loops until the event is set; checks
        PAUSE_FILE each cycle and uses the lock for thread-safe access when the menu
        bar UI may call poll_now() concurrently.  Tracks consecutive failures with
        exponential backoff for the menu bar error display.
        """
        if stop_event is None:
            print(f"Ableton Tracker  |  poll every {POLL_INTERVAL}s  |  db: {DB_PATH}")
            print("Ctrl+C to stop\n")

            def shutdown(sig, frame):
                print("\nStopping tracker…")
                self._close()
                sys.exit(0)

            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)

        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                paused = PAUSE_FILE.exists() if _lock is not None else False
                if _lock is not None:
                    with _lock:
                        self.poll_once(paused=paused)
                        self.maybe_run_cleanup()
                else:
                    self.poll_once(paused=paused)
                    self.maybe_run_cleanup()
                self._consecutive_failures = 0
                self._last_error = None
            except Exception as e:
                self._consecutive_failures += 1
                self._last_error = str(e)
                if _lock is not None:
                    print(f"tracker error ({self._consecutive_failures}): {e}", file=sys.stderr)
                    if self._consecutive_failures >= TRACKER_MAX_RETRIES:
                        print("tracker: max retries reached, polling suspended", file=sys.stderr)
                else:
                    print(f"[{_ts()}] error: {e}")
            wait = wake_interval
            if _lock is not None and self._consecutive_failures > 0:
                wait = min(2 ** self._consecutive_failures, TRACKER_MAX_BACKOFF)
            if stop_event:
                stop_event.wait(wait)
            else:
                time.sleep(wait)


if __name__ == "__main__":
    Tracker().run()
