"""Native notification rules for the Ableton Tracker menu-bar app."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Callable

from tracker import allocate_session_activity


MORNING_NOTIFICATION_HOUR = 9
LATE_NOTIFICATION_HOUR = 17
STREAK_NOTIFICATION_HOURS = (12, 18, 23)
MINIMUM_PACE_GAP_SECONDS = 15 * 60
DEFAULT_WEEK_START_WEEKDAY = 4  # Friday


@dataclass(frozen=True)
class NotificationMessage:
    event_keys: tuple[str, ...]
    title: str
    subtitle: str
    message: str


def _format_gap(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _join_labels(labels: list[str], limit: int = 3) -> str:
    visible = labels[:limit]
    body = " · ".join(visible)
    remaining = len(labels) - len(visible)
    if remaining > 0:
        body += f" · +{remaining} more"
    return body


class NotificationCoordinator:
    """Evaluates notification rules and persists sent-event deduplication."""

    def __init__(
        self,
        db_path: Path,
        state_path: Path,
        *,
        enabled: bool = True,
    ):
        self.db_path = Path(db_path)
        self.state_path = Path(state_path)
        self.enabled = enabled
        self._sent = self._load_state()
        self._paused_since: datetime | None = None
        self._pause_token: str | None = None

    def check(
        self,
        *,
        now: datetime,
        today_seconds: float,
        week_seconds: float,
        weekly_goal_hours: float | None,
        streak_days: int,
        deliver: Callable[[NotificationMessage], None],
        daily_goal_hours: float | None = None,
        pause_token: str | None = None,
        ableton_running: bool = False,
    ) -> list[NotificationMessage]:
        """Deliver eligible, unsent notifications and return what was delivered."""
        if not self.enabled:
            return []

        candidates: list[NotificationMessage] = []
        for candidate in (
            self._daily_goal_candidate(now, today_seconds, daily_goal_hours),
            self._paused_candidate(now, pause_token, ableton_running),
            self._streak_candidate(now, today_seconds, streak_days),
            self._recap_candidate(now),
        ):
            if candidate:
                candidates.append(candidate)

        if now.hour >= LATE_NOTIFICATION_HOUR:
            pace = self._weekly_pace_candidate(now, week_seconds, weekly_goal_hours)
            if pace:
                candidates.append(pace)

        delivered: list[NotificationMessage] = []
        for candidate in candidates:
            if all(key in self._sent for key in candidate.event_keys):
                continue
            try:
                deliver(candidate)
            except Exception as exc:
                print(f"[notifications] delivery error: {exc}")
                continue
            self._mark_sent(candidate.event_keys, now)
            delivered.append(candidate)
        return delivered


    def _streak_candidate(
        self, now: datetime, today_seconds: float, streak_days: int
    ) -> NotificationMessage | None:
        event_key = f"streak-risk:{now.date().isoformat()}:{now.hour}"
        if (
            now.hour not in STREAK_NOTIFICATION_HOURS
            or event_key in self._sent
            or today_seconds > 0
            or streak_days <= 0
        ):
            return None
        return NotificationMessage(
            (event_key,),
            f"Your {streak_days}-day streak is at risk 🔥",
            "A little Ableton time keeps it alive.",
            "Open a project before midnight and keep the streak going.",
        )

    def _daily_goal_candidate(self, now, seconds, goal_hours):
        key = f"daily-goal:{now.date().isoformat()}"
        if key in self._sent or not goal_hours or goal_hours <= 0 or seconds < goal_hours * 3600:
            return None
        return NotificationMessage(
            (key,), "Daily goal complete 🎉", f"{_format_gap(seconds)} making music today.",
            "You reached your daily Ableton goal.",
        )

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

    def _recap_candidate(self, now):
        # Use the completed week, including a catch-up when the app next opens.
        if now.hour < MORNING_NOTIFICATION_HOUR or not self.db_path.exists():
            return None
        end = now.date() - timedelta(days=(now.weekday() - self._week_start_weekday()) % 7)
        start = end - timedelta(days=7)
        key = f"weekly-recap:{start.isoformat()}"
        if key in self._sent:
            return None
        totals = defaultdict(float)
        try:
            with closing(sqlite3.connect(self.db_path, timeout=3)) as conn:
                conn.row_factory = sqlite3.Row
                tables = self._table_names(conn)
                aliases = dict(conn.execute("SELECT alias_name, canonical_name FROM project_aliases")) if "project_aliases" in tables else {}
                labels = dict(conn.execute("SELECT project_name, display_name FROM project_metadata")) if "project_metadata" in tables else {}
                rows = conn.execute(
                    """SELECT project_name, start_time, end_time, last_seen_time, active_seconds
                       FROM sessions WHERE active_seconds > 0 AND start_time < ?
                       AND COALESCE(end_time, last_seen_time, start_time) >= ?""",
                    (datetime.combine(end, datetime_time.min).timestamp(),
                     datetime.combine(start, datetime_time.min).timestamp()),
                ).fetchall()
            for row in rows:
                project = aliases.get(row["project_name"], row["project_name"])
                for day, _, seconds in allocate_session_activity(
                    row["start_time"], row["end_time"] or row["last_seen_time"] or row["start_time"], row["active_seconds"]
                ):
                    if start.isoformat() <= day < end.isoformat():
                        totals[project] += seconds
        except sqlite3.Error as exc:
            print(f"[notifications] recap query error: {exc}")
            return None
        if not totals:
            return None
        top = max(totals, key=totals.get)
        count = len(totals)
        return NotificationMessage(
            (key,), "Your weekly recap", f"{start:%b %d} – {end - timedelta(days=1):%b %d}",
            f"{_format_gap(sum(totals.values()))} across {count} project{'s' if count != 1 else ''}. "
            f"Most time: {labels.get(top) or top}.",
        )

    def _weekly_pace_candidate(
        self,
        now: datetime,
        week_seconds: float,
        weekly_goal_hours: float | None,
    ) -> NotificationMessage | None:
        if weekly_goal_hours is None or weekly_goal_hours <= 0:
            return None

        week_start_weekday = self._week_start_weekday()
        week_start_date = now.date() - timedelta(
            days=(now.weekday() - week_start_weekday) % 7
        )
        week_start = datetime.combine(week_start_date, datetime_time.min)
        elapsed_seconds = max(
            0.0, min((now - week_start).total_seconds(), 7 * 86400)
        )
        expected_seconds = (
            weekly_goal_hours * 3600 * (elapsed_seconds / (7 * 86400))
        )
        gap = expected_seconds - max(0.0, week_seconds)
        event_key = f"weekly-pace:{now.date().isoformat()}"
        if event_key in self._sent or gap < MINIMUM_PACE_GAP_SECONDS:
            return None
        return NotificationMessage(
            (event_key,),
            "Weekly goal check-in",
            f"You’re {_format_gap(gap)} behind pace.",
            "A focused session today can close the gap.",
        )


    def _week_start_weekday(self) -> int:
        if not self.db_path.exists():
            return DEFAULT_WEEK_START_WEEKDAY
        try:
            with closing(sqlite3.connect(self.db_path, timeout=3)) as conn:
                if "app_settings" not in self._table_names(conn):
                    return DEFAULT_WEEK_START_WEEKDAY
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = 'week_start_weekday'"
                ).fetchone()
                value = int(row[0]) if row else DEFAULT_WEEK_START_WEEKDAY
                return value if 0 <= value <= 6 else DEFAULT_WEEK_START_WEEKDAY
        except (sqlite3.Error, TypeError, ValueError):
            return DEFAULT_WEEK_START_WEEKDAY

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    def _load_state(self) -> dict[str, str]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            sent = payload.get("sent", {})
            if isinstance(sent, dict):
                return {str(key): str(value) for key, value in sent.items()}
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _mark_sent(self, event_keys: tuple[str, ...], now: datetime) -> None:
        timestamp = now.isoformat(timespec="seconds")
        for event_key in event_keys:
            self._sent[event_key] = timestamp
        if len(self._sent) > 500:
            self._sent = dict(list(self._sent.items())[-400:])
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.state_path.with_name(
                f".{self.state_path.name}.{os.getpid()}.tmp"
            )
            temp_path.write_text(
                json.dumps({"sent": self._sent}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temp_path, self.state_path)
        except OSError as exc:
            print(f"[notifications] state save error: {exc}")
