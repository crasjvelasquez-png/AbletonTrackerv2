"""Native notification rules for the Ableton Tracker menu-bar app."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Callable


MORNING_NOTIFICATION_HOUR = 9
LATE_NOTIFICATION_HOUR = 17
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

    def check(
        self,
        *,
        now: datetime,
        today_seconds: float,
        week_seconds: float,
        weekly_goal_hours: float | None,
        streak_days: int,
        deliver: Callable[[NotificationMessage], None],
    ) -> list[NotificationMessage]:
        """Deliver eligible, unsent notifications and return what was delivered."""
        if not self.enabled or now.hour < MORNING_NOTIFICATION_HOUR:
            return []

        candidates: list[NotificationMessage] = []
        deadline = self._deadline_candidate(now.date())
        if deadline:
            candidates.append(deadline)

        quiet = self._quiet_candidate(now)
        if quiet:
            candidates.append(quiet)

        if now.hour >= LATE_NOTIFICATION_HOUR:
            streak = self._streak_candidate(now.date(), today_seconds, streak_days)
            if streak:
                candidates.append(streak)

            pace = self._weekly_pace_candidate(
                now, week_seconds, weekly_goal_hours
            )
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

    def _deadline_candidate(self, today: date) -> NotificationMessage | None:
        tomorrow = (today + timedelta(days=1)).isoformat()
        event_key = f"deadline:{tomorrow}"
        if event_key in self._sent:
            return None

        labels = self._due_tomorrow_labels(tomorrow)
        if not labels:
            return None
        count = len(labels)
        return NotificationMessage(
            (event_key,),
            "Tomorrow’s deadlines",
            f"{count} item{'s' if count != 1 else ''} due",
            _join_labels(labels),
        )

    def _quiet_candidate(self, now: datetime) -> NotificationMessage | None:
        quiet_projects = self._quiet_projects(now)
        unsent = [
            project
            for project in quiet_projects
            if project["event_key"] not in self._sent
        ]
        if not unsent:
            return None

        labels = [
            f"{project['name']} ({project['days']}d)"
            for project in unsent
        ]
        count = len(labels)
        return NotificationMessage(
            tuple(project["event_key"] for project in unsent),
            "Project gone quiet" if count == 1 else "Projects gone quiet",
            f"No Ableton activity for {min(project['days'] for project in unsent)}+ days",
            _join_labels(labels),
        )

    def _streak_candidate(
        self, today: date, today_seconds: float, streak_days: int
    ) -> NotificationMessage | None:
        event_key = f"streak-risk:{today.isoformat()}"
        if (
            event_key in self._sent
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

    def _due_tomorrow_labels(self, tomorrow: str) -> list[str]:
        if not self.db_path.exists():
            return []
        labels: list[str] = []
        try:
            with sqlite3.connect(self.db_path, timeout=3) as conn:
                conn.row_factory = sqlite3.Row
                tables = self._table_names(conn)
                if "project_metadata" in tables:
                    rows = conn.execute(
                        """
                        SELECT project_name, display_name
                        FROM project_metadata
                        WHERE status NOT IN ('finished', 'abandoned')
                          AND ? IN (due_date, hard_deadline, turn_in_date)
                        ORDER BY LOWER(COALESCE(NULLIF(display_name, ''), project_name))
                        """,
                        (tomorrow,),
                    ).fetchall()
                    labels.extend(
                        f"Project: {row['display_name'] or row['project_name']}"
                        for row in rows
                    )
                if "project_tasks" in tables:
                    metadata_join = (
                        "LEFT JOIN project_metadata pm ON pm.project_name = pt.project_name"
                        if "project_metadata" in tables
                        else ""
                    )
                    display_expression = (
                        "COALESCE(NULLIF(pm.display_name, ''), pt.project_name)"
                        if metadata_join
                        else "pt.project_name"
                    )
                    rows = conn.execute(
                        f"""
                        SELECT pt.title, {display_expression} AS project_label
                        FROM project_tasks pt
                        {metadata_join}
                        WHERE pt.status = 'open' AND pt.due_date = ?
                        ORDER BY LOWER(project_label), LOWER(pt.title)
                        """,
                        (tomorrow,),
                    ).fetchall()
                    labels.extend(
                        f"{row['project_label']}: {row['title']}" for row in rows
                    )
        except sqlite3.Error as exc:
            print(f"[notifications] deadline query error: {exc}")
            return []
        return list(dict.fromkeys(labels))

    def _quiet_projects(self, now: datetime) -> list[dict]:
        if not self.db_path.exists():
            return []
        try:
            with sqlite3.connect(self.db_path, timeout=3) as conn:
                conn.row_factory = sqlite3.Row
                tables = self._table_names(conn)
                required = {"sessions", "project_metadata"}
                if not required.issubset(tables):
                    return []
                alias_join = (
                    "LEFT JOIN project_aliases pa ON pa.alias_name = s.project_name"
                    if "project_aliases" in tables
                    else ""
                )
                project_expression = (
                    "COALESCE(pa.canonical_name, s.project_name)"
                    if alias_join
                    else "s.project_name"
                )
                rows = conn.execute(
                    f"""
                    SELECT pm.project_name,
                           COALESCE(NULLIF(pm.display_name, ''), pm.project_name) AS display_name,
                           activity.last_seen
                    FROM project_metadata pm
                    JOIN (
                        SELECT {project_expression} AS project_name,
                               MAX(COALESCE(s.end_time, s.last_seen_time, s.start_time)) AS last_seen
                        FROM sessions s
                        {alias_join}
                        WHERE s.active_seconds > 0
                        GROUP BY {project_expression}
                    ) activity ON activity.project_name = pm.project_name
                    WHERE pm.status != ''
                      AND pm.status NOT IN ('finished', 'abandoned', 'paused')
                    ORDER BY activity.last_seen ASC
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            print(f"[notifications] quiet-project query error: {exc}")
            return []

        projects = []
        for row in rows:
            last_seen = datetime.fromtimestamp(float(row["last_seen"]))
            days = (now.date() - last_seen.date()).days
            if days < 7:
                continue
            threshold = 14 if days >= 14 else 7
            projects.append(
                {
                    "name": row["display_name"],
                    "days": days,
                    "event_key": (
                        f"quiet:{row['project_name']}:{int(row['last_seen'])}:{threshold}"
                    ),
                }
            )
        return projects

    def _week_start_weekday(self) -> int:
        if not self.db_path.exists():
            return DEFAULT_WEEK_START_WEEKDAY
        try:
            with sqlite3.connect(self.db_path, timeout=3) as conn:
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
