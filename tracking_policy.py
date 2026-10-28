"""Observable evidence for the strict, finite attendance policy.

Only event categories and timestamps are retained, never key contents or audio.
"""
from dataclasses import dataclass

POLICY_VERSION = 1
INPUT_ALLOWANCE = 30.0
LISTENING_ALLOWANCE = 120.0
EVIDENCE_TTL = 10.0
POLICY_TEXT = ('Counts Live interaction plus up to 30s of inactivity. Live playback '
               'extends this to 2m after your last interaction. Switching apps pauses. '
               'Long hands-off performances pause after 2m without new interaction.')

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
