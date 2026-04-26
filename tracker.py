#!/usr/bin/env python3
"""Ableton Live project time tracker daemon."""

import sqlite3
import subprocess
import time
import signal
import sys
import re
import tempfile
from collections import defaultdict
from contextlib import closing
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
# ~-40 dBFS — above noise floor / plugin residue, well below normal listening level.
AUDIO_LEVEL_ACTIVE_THRESHOLD = 0.01
CLEANUP_INTERVAL = 15 * 60  # seconds between background cleanup passes
SESSION_CONDENSE_GAP_SECONDS = 5 * 60
DB_PATH = Path.home() / ".ableton_tracker" / "sessions.db"
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


def setup_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
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
        conn.commit()


def is_phantom_session_name(name: str | None) -> bool:
    """Return True for closed rows that are clearly not real Live sets."""
    raw = (name or "").strip()
    if not raw:
        return False

    lowered = re.sub(r"\s+", " ", raw).strip().lower()
    if lowered in UNTITLED_NAMES:
        return False

    if any(re.match(pattern, lowered, flags=re.IGNORECASE) for pattern in NON_PROJECT_TITLE_PATTERNS):
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
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return len(_phantom_session_ids(conn))


def cleanup_phantom_sessions() -> dict:
    """Delete clearly invalid historical rows left by older builds."""
    if not DB_PATH.exists():
        return {"ok": True, "deleted": 0}

    with closing(sqlite3.connect(DB_PATH)) as conn:
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

    with closing(sqlite3.connect(DB_PATH)) as conn:
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

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT start_time, last_seen_time, end_time, active_seconds
            FROM sessions
            WHERE active_seconds > 0
            """
        ).fetchall()

    daily, _ = build_activity_rollups(rows)
    return daily.get(target_day.isoformat(), 0.0)


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


_last_idle_error = None


def _set_idle_error(message: str | None):
    global _last_idle_error
    if message == _last_idle_error:
        return
    _last_idle_error = message
    if message:
        print(f"[{_ts()}] HIDIdleTime read failed: {message}")
    else:
        print(f"[{_ts()}] HIDIdleTime read recovered")


def get_idle_seconds() -> float:
    """Seconds since last mouse/keyboard activity (macOS HIDIdleTime)."""
    try:
        r = subprocess.run(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception as e:
        _set_idle_error(f"subprocess: {e}")
        return 0.0
    if r.returncode != 0:
        _set_idle_error(f"ioreg exit {r.returncode}: {r.stderr.strip()[:200]}")
        return 0.0
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', r.stdout)
    if not m:
        _set_idle_error(f"no HIDIdleTime in {len(r.stdout)} bytes of ioreg output")
        return 0.0
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


_last_audio_probe_error = None


def _set_audio_probe_error(message: str | None):
    global _last_audio_probe_error
    if message == _last_audio_probe_error:
        return
    _last_audio_probe_error = message
    if message:
        print(f"[{_ts()}] audio-level probe unavailable: {message}")
    else:
        print(f"[{_ts()}] audio-level probe recovered")


def _system_audio_level_active() -> bool | None:
    """Sample actual macOS system output level using ScreenCaptureKit.

    This is intentionally level-based instead of a Live CPU/process proxy:
    a stopped transport or idle audio engine should read quiet, while real
    playback from Live (or any audible system output) keeps auto-idle awake.
    """
    swift = f"""
import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

final class AudioProbe: NSObject, SCStreamOutput {{
    var peak: Float = 0
    var sumOfSquares: Double = 0
    var sampleCount: Int = 0

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {{
        guard type == .audio, sampleBuffer.isValid else {{ return }}
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
        for buffer in buffers {{
            guard let data = buffer.mData else {{ continue }}
            let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
            let samples = data.bindMemory(to: Float.self, capacity: count)
            for index in 0..<count {{
                let sample = samples[index]
                let absSample = abs(sample)
                if absSample > peak {{ peak = absSample }}
                sumOfSquares += Double(sample) * Double(sample)
                sampleCount += 1
            }}
        }}
    }}
}}

let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
guard let display = content.displays.first else {{
    print("quiet 0")
    exit(0)
}}

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
try await Task.sleep(nanoseconds: UInt64({AUDIO_LEVEL_POLL_SECONDS} * 1_000_000_000))
try await stream.stopCapture()

let rms = probe.sampleCount > 0 ? sqrt(probe.sumOfSquares / Double(probe.sampleCount)) : 0
if probe.sampleCount == 0 {{
    print("unavailable samples=0")
}} else if rms >= Double({AUDIO_LEVEL_ACTIVE_THRESHOLD}) {{
    print("active rms=\\(rms) peak=\\(probe.peak) samples=\\(probe.sampleCount)")
}} else {{
    print("quiet rms=\\(rms) peak=\\(probe.peak) samples=\\(probe.sampleCount)")
}}
"""
    script_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".swift", delete=False) as script:
            script.write(swift)
            script_path = script.name
        r = subprocess.run(
            ["swift", script_path],
            capture_output=True,
            text=True,
            timeout=max(AUDIO_POLL_TIMEOUT, AUDIO_LEVEL_POLL_SECONDS + 4),
        )
    except Exception as e:
        _set_audio_probe_error(str(e))
        return None
    finally:
        if script_path:
            try:
                Path(script_path).unlink()
            except FileNotFoundError:
                pass

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


_last_title_error = None


def get_live_window_titles() -> list[str]:
    """Return visible Ableton Live window titles.

    Logs (once) when System Events refuses — without that, an Accessibility
    permission failure looks identical to "Ableton has no open windows".
    """
    global _last_title_error
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
    except Exception as e:
        if _last_title_error != str(e):
            print(f"[{_ts()}] window-title lookup failed: {e}")
            _last_title_error = str(e)
        return []

    if r.returncode != 0:
        err = r.stderr.strip()
        if _last_title_error != err:
            hint = ""
            if "1719" in err or "assistive access" in err.lower() or "-25211" in err:
                hint = (
                    "  → Grant Accessibility to this Python in System Settings → "
                    "Privacy & Security → Accessibility "
                    f"({sys.executable})"
                )
            print(f"[{_ts()}] osascript error reading Live windows: {err}{hint}")
            _last_title_error = err
        return []

    if _last_title_error is not None:
        print(f"[{_ts()}] window-title lookup recovered")
        _last_title_error = None
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

    name = re.sub(r"\s*\[[^\]]*\]", "", name).strip()
    name = re.sub(r"\s*\([^)]*\)", "", name).strip()
    name = re.sub(r"\.als\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+", " ", name).strip(" -–—")

    if not name or name.lower() in {"", "ableton live", "live"}:
        return None

    lowered = name.lower()
    if any(re.match(pattern, lowered, flags=re.IGNORECASE) for pattern in NON_PROJECT_TITLE_PATTERNS):
        return None

    return name


def get_project_name(current_project: str | None = None) -> str | None:
    """Resolve the current Ableton project while ignoring transient windows.

    Returns None if no reliable project title is visible; callers should keep
    the current session alive rather than rotating to a guess.
    """
    candidates = []
    distinct_candidates = []
    seen_candidates = set()
    distinct_live_title_candidates = []
    seen_live_title_candidates = set()
    for title in get_live_window_titles():
        parsed = parse_project_title(title)
        if parsed:
            candidates.append(parsed)
            key = parsed.casefold()
            if key not in seen_candidates:
                distinct_candidates.append(parsed)
                seen_candidates.add(key)
            if LIVE_TITLE_SUFFIX_RE.search(title):
                if key not in seen_live_title_candidates:
                    distinct_live_title_candidates.append(parsed)
                    seen_live_title_candidates.add(key)

    if not candidates:
        return None

    if current_project:
        for candidate in candidates:
            if candidate == current_project:
                return candidate

        # A single canonical Live title is a reliable project switch.
        if len(distinct_live_title_candidates) == 1:
            return distinct_live_title_candidates[0]

        if len(distinct_live_title_candidates) > 1:
            return None

        # When Live is only exposing bare titles, accept a single distinct
        # parsed candidate as a real project switch. This fixes the common case
        # where the main project window title is just the set name.
        if len(distinct_candidates) == 1:
            return distinct_candidates[0]

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
        self.last_paused = False
        self.last_running = False
        self.last_hid_idle = 0.0
        self.last_audio_is_active = False
        self.last_audio_idle = float("inf")
        self.last_idle_paused = False
        self.last_checked_at = 0.0
        self.last_state = STATE_ABLETON_CLOSED

    def _publish_state(self, state: str):
        self.last_state = state
        self.last_paused = state == STATE_PAUSED
        self.last_running = state != STATE_ABLETON_CLOSED
        self.last_idle_paused = state == STATE_IDLE_PAUSED

    def _start(self, project: str):
        now = time.time()
        with closing(sqlite3.connect(DB_PATH)) as conn:
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
        with closing(sqlite3.connect(DB_PATH)) as conn:
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
        with closing(sqlite3.connect(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT active_seconds FROM sessions WHERE id=?", (self.session_id,)
            ).fetchone()
            active = row[0] if row else 0
            if name in UNTITLED_NAMES or active < 5:
                # Drop untitled drafts and phantom sub-5s rows (title flickers).
                conn.execute("DELETE FROM sessions WHERE id=?", (self.session_id,))
                why = "unsaved" if name in UNTITLED_NAMES else "too short"
                print(f"[{_ts()}] ✗  {self.project_name} ({why} — discarded)")
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
        return TrackerStatus(
            state=self.last_state,
            paused=self.last_paused,
            running=self.last_running,
            project_name=self.project_name,
            resume_hint_project=self.resume_hint_project,
            hid_idle_seconds=self.last_hid_idle,
            audio_active=self.last_audio_is_active,
            audio_idle_seconds=self.last_audio_idle,
            idle_paused=self.last_idle_paused,
            checked_at=self.last_checked_at,
        )

    def poll_once(self, paused: bool = False):
        now = time.time()
        self.last_checked_at = now

        if paused:
            running = is_ableton_running()
            self.last_hid_idle = 0.0
            self.last_audio_is_active = False
            self.last_audio_idle = float("inf")
            self._close(preserve_resume_hint=True)
            self._publish_state(STATE_PAUSED)
            self.last_running = running
            return

        if not is_ableton_running():
            self.last_hid_idle = 0.0
            self.last_audio_is_active = False
            self.last_audio_idle = float("inf")
            self._close()
            self._publish_state(STATE_ABLETON_CLOSED)
            return

        self.last_hid_idle = get_idle_seconds()
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
            self._publish_state(
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
            self._publish_state(
                STATE_TRACKING if self.session_id is not None else STATE_ABLETON_OPEN
            )
            return

        if project != self.project_name:
            self._close()
            self._start(project)
        self._tick()
        self._publish_state(STATE_TRACKING)

    def maybe_run_cleanup(self, force: bool = False):
        now = time.time()
        if not force and now < self.next_cleanup_at:
            return

        result = cleanup_phantom_sessions()
        deleted = result.get("deleted", 0)
        if deleted:
            print(f"[{_ts()}] cleaned {deleted} phantom session{'s' if deleted != 1 else ''}")
        self.next_cleanup_at = now + CLEANUP_INTERVAL

    def run(self):
        setup_db()
        stale = close_stale_open_sessions()
        if stale:
            print(f"[{_ts()}] closed {stale} stale open session{'s' if stale != 1 else ''}")
        self.maybe_run_cleanup(force=True)
        print(f"Ableton Tracker  |  poll every {POLL_INTERVAL}s  |  db: {DB_PATH}")
        print("Ctrl+C to stop\n")

        def shutdown(sig, frame):
            print("\nStopping tracker…")
            self._close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        while True:
            try:
                self.poll_once()
                self.maybe_run_cleanup()
            except Exception as e:
                print(f"[{_ts()}] error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    Tracker().run()
