"""Observable evidence for the forgiving, finite attendance policy.

Only event categories and timestamps are retained, never key contents or audio.
"""
from dataclasses import dataclass

POLICY_VERSION = 6
# Work happens outside Live too: with a set open, input in any app counts, and
# Live playback counts whichever app is in front.
INPUT_ALLOWANCE = 120.0      # fallback only: hands-off time after input when play state is unknown
# The play button is the work signal: counts while Live plays and for this long
# after it stops. Input alone does not count while the play detector answers.
PLAY_ALLOWANCE = 180.0
# Playback with Live behind other apps this long counts as distraction, not work.
BACKGROUND_ALLOWANCE = 300.0
LISTENING_ALLOWANCE = INPUT_ALLOWANCE  # compatibility alias; playback itself has no limit
AWAY_ALLOWANCE = INPUT_ALLOWANCE       # compatibility alias; other apps no longer end a session
PROJECT_GRACE = 300.0        # keep the current set while window titles are unreadable
EVIDENCE_TTL = 10.0
POLICY_TEXT = ('Counts while Live is playing, and for 3m after it stops. If you do not come back, '
               'the time after you were last in Live is taken back. Mouse and keyboard alone do '
               'not count. Stops after 5m with Live behind other apps, and when the Mac locks or '
               'sleeps. If play state cannot be read (AbletonOSC off), counts input with Live in '
               'front plus 2m.')

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
