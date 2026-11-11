"""Observable evidence for the forgiving, finite attendance policy.

Only event categories and timestamps are retained, never key contents or audio.
"""
from dataclasses import dataclass

POLICY_VERSION = 2
INPUT_ALLOWANCE = 180.0      # hands-off time in Live (listening, thinking) that still counts
LISTENING_ALLOWANCE = 600.0  # with Live playback/audio detected
AWAY_ALLOWANCE = 180.0       # time in another app that counts if you come back to Live
PROJECT_GRACE = 300.0        # keep the current set while window titles are unreadable
EVIDENCE_TTL = 10.0
POLICY_TEXT = ('Counts Live interaction plus up to 3m of hands-off time. Live playback '
               'extends this to 10m after your last interaction. Time in another app '
               'counts if you return to Live within 3m; longer trips end the session '
               'when you left.')

@dataclass(frozen=True)
class Observation:
    wall: float
    mono: float
    pid: int | None = None
    foreground: bool = False
    project: str | None = None
    project_at: float | None = None
    input_at: float | None = None
    observable: bool = True
    locked: bool = False
    audio: bool | None = None
    audio_at: float | None = None
    source: str = 'unavailable'
    audio_source: str | None = None
    audio_started_at: float | None = None
    audio_finished_at: float | None = None
    boundary: float | None = None
    error: str | None = None
