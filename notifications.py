"""Native notification rules for the Ableton Tracker menu-bar app."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from tracker import STATE_TRACKING, TrackerStatus


INVITE_HOUR = 8
INVITE_CUTOFF_HOUR = 21
SNOOZE_SECONDS = 3600
INVITE_TITLE = "Some time for music today?"
INVITE_MESSAGE = "Pick up a project or start something new."
# Notification actions that end the day's invitations.
ENDING_ACTIONS = {"skip", "open", "dismiss"}


@dataclass(frozen=True)
class NotificationMessage:
    event_keys: tuple[str, ...]
    title: str
    subtitle: str
    message: str
    category: str = ""
    day: str = ""


def _format_gap(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


class NotificationCoordinator:
    """Evaluates notification rules and persists daily invitation state.

    One neutral invitation at 8 a.m. toward the daily target. Ignoring it ends
    the day; only an explicit "Later" action schedules one more, an hour on.
    Actions arrive from the native sender as JSON lines in ``actions_path``.
    """

    def __init__(
        self,
        db_path: Path,
        state_path: Path,
        *,
        enabled: bool = True,
        actions_path: Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.state_path = Path(state_path)
        self.actions_path = Path(actions_path) if actions_path else self.state_path.with_name("notification_actions.jsonl")
        self.enabled = enabled
        self._sent, self._snooze = self._load_state()
        self._paused_since: datetime | None = None
        self._pause_token: str | None = None

    def check(
        self,
        *,
        now: datetime,
        today_seconds: float,
        deliver: Callable[[NotificationMessage], None],
        daily_goal_hours: float | None = None,
        pause_token: str | None = None,
        ableton_running: bool = False,
        tracker_status: TrackerStatus | None = None,
        **_retired,
    ) -> list[NotificationMessage]:
        """Deliver eligible, unsent notifications and return what was delivered.

        ``week_seconds``, ``weekly_goal_hours`` and ``streak_days`` are still
        accepted from callers but no longer drive any notification.
        """
        if not self.enabled:
            return []
        self._ingest_actions()

        day = now.date().isoformat()
        active = tracker_status is not None and tracker_status.state == STATE_TRACKING
        goal_seconds = daily_goal_hours * 3600 if daily_goal_hours and daily_goal_hours > 0 else None
        goal_met = goal_seconds is not None and today_seconds >= goal_seconds

        candidates: list[NotificationMessage] = []
        paused = self._paused_candidate(now, pause_token, ableton_running)
        if paused:
            candidates.append(paused)
        if not active and goal_met:
            key = f"daily-goal:{day}"
            if key not in self._sent:
                candidates.append(NotificationMessage(
                    (key,), "Daily target reached", "", f"{_format_gap(today_seconds)} in Ableton today."))
        invite = self._invite_candidate(now, day, active, goal_met, today_seconds, goal_seconds)
        if invite:
            candidates.append(invite)

        delivered: list[NotificationMessage] = []
        for candidate in candidates:
            if all(key in self._sent for key in candidate.event_keys):
                continue
            try:
                deliver(candidate)
            except Exception as exc:
                print(f"[notifications] delivery error: {exc}")
                continue
            if candidate.category == "invite" and self._snooze.get("day") == day:
                self._snooze = {}
            self._mark_sent(candidate.event_keys, now)
            delivered.append(candidate)
            break  # One banner per check; remaining events are deferred.
        return delivered

    def _invite_candidate(self, now, day, active, goal_met, today_seconds, goal_seconds):
        if not INVITE_HOUR <= now.hour < INVITE_CUTOFF_HOUR or f"ended:{day}" in self._sent:
            return None
        morning_key = f"invite:{day}"
        snooze_until = self._snooze.get("until") if self._snooze.get("day") == day else None
        if morning_key not in self._sent:
            key = morning_key
        elif snooze_until is not None and now.timestamp() >= snooze_until:
            key = f"later:{day}:{int(snooze_until)}"
        else:
            return None
        if active or goal_met:
            # Working or done: the invitation is moot, so let it lapse quietly.
            if key != morning_key:
                self._snooze = {}
            self._mark_sent((key,), now)
            return None
        if goal_seconds is None:
            subtitle = ""
        elif today_seconds > 0:
            subtitle = f"{_format_gap(goal_seconds - today_seconds)} left toward today’s target"
        else:
            subtitle = f"Today’s target: {_format_gap(goal_seconds)}"
        return NotificationMessage((key,), INVITE_TITLE, subtitle, INVITE_MESSAGE,
                                   category="invite", day=day)

    def _ingest_actions(self) -> None:
        processing = self.actions_path.with_name(f".{self.actions_path.name}.{os.getpid()}.processing")
        try:
            os.replace(self.actions_path, processing)
        except FileNotFoundError:
            return
        except OSError as exc:
            print(f"[notifications] action read error: {exc}")
            return
        try:
            lines = processing.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        finally:
            processing.unlink(missing_ok=True)
        changed = False
        for line in lines:
            try:
                entry = json.loads(line)
                action, day, at = str(entry["action"]), str(entry["day"]), float(entry["at"])
            except (ValueError, TypeError, KeyError):
                continue
            if action == "later":
                self._snooze = {"day": day, "until": at + SNOOZE_SECONDS}
            elif action in ENDING_ACTIONS:
                self._sent[f"ended:{day}"] = datetime.fromtimestamp(at).isoformat(timespec="seconds")
                if self._snooze.get("day") == day:
                    self._snooze = {}
            else:
                continue
            changed = True
        if changed:
            self._save_state()

    def _paused_candidate(self, now, pause_token, ableton_running):
        if pause_token != self._pause_token or not ableton_running or pause_token is None:
            self._paused_since = None
        self._pause_token = pause_token
        if pause_token is None or not ableton_running:
            return None
        if self._paused_since is None:
            self._paused_since = now
        if (now - self._paused_since).total_seconds() < 300:
            return None
        key = f"paused:{pause_token}"
        if key in self._sent:
            return None
        return NotificationMessage(
            (key,), "Tracking is still paused", "Ableton is open, but tracking is paused.",
            "Choose Resume tracking in the menu bar to record your time.",
        )

    def _load_state(self) -> tuple[dict[str, str], dict]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            sent = payload.get("sent", {})
            snooze = payload.get("snooze", {})
            sent = {str(k): str(v) for k, v in sent.items()} if isinstance(sent, dict) else {}
            if not (isinstance(snooze, dict) and isinstance(snooze.get("until"), (int, float))):
                snooze = {}
            return sent, snooze
        except (OSError, ValueError, TypeError, AttributeError):
            return {}, {}

    def _mark_sent(self, event_keys: tuple[str, ...], now: datetime) -> None:
        timestamp = now.isoformat(timespec="seconds")
        for event_key in event_keys:
            self._sent[event_key] = timestamp
        self._save_state()

    def _save_state(self) -> None:
        if len(self._sent) > 500:
            self._sent = dict(list(self._sent.items())[-400:])
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.state_path.with_name(
                f".{self.state_path.name}.{os.getpid()}.tmp"
            )
            temp_path.write_text(
                json.dumps({"sent": self._sent, "snooze": self._snooze}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temp_path, self.state_path)
        except OSError as exc:
            print(f"[notifications] state save error: {exc}")
