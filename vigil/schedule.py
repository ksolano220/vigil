"""Schedule math: when a job is due, and which windows it silently skipped."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta:
    """'90m', '2h30m', '1d' -> timedelta."""
    if isinstance(text, (int, float)):
        return timedelta(seconds=float(text))
    matches = _DURATION.findall(str(text).strip().lower())
    if not matches:
        raise ValueError(f"cannot parse duration: {text!r}")
    seconds = sum(float(value) * _UNITS[unit] for value, unit in matches)
    return timedelta(seconds=seconds)


def parse_clock(text: str) -> tuple[int, int]:
    """'09:15' -> (9, 15)."""
    hour, _, minute = str(text).partition(":")
    hour_i, minute_i = int(hour), int(minute or 0)
    if not (0 <= hour_i < 24 and 0 <= minute_i < 60):
        raise ValueError(f"cannot parse clock time: {text!r}")
    return hour_i, minute_i


def expected_windows(job, since: datetime, until: datetime) -> list[datetime]:
    """Every moment the job was supposed to run in (since, until]."""
    if job.at:
        return _clock_windows(job.at, since, until)
    return _interval_windows(job.every_delta, since, until)


def _interval_windows(every: timedelta, since: datetime, until: datetime) -> list[datetime]:
    if every.total_seconds() <= 0:
        return []
    windows, cursor = [], since + every
    # Guard against a long outage on a fast schedule producing a runaway list.
    while cursor <= until and len(windows) < 1000:
        windows.append(cursor)
        cursor += every
    return windows


def _clock_windows(times: list[str], since: datetime, until: datetime) -> list[datetime]:
    windows = []
    day = since.date()
    while day <= until.date():
        for text in times:
            hour, minute = parse_clock(text)
            moment = datetime.combine(day, datetime.min.time(), tzinfo=since.tzinfo)
            moment = moment.replace(hour=hour, minute=minute)
            if since < moment <= until:
                windows.append(moment)
        day += timedelta(days=1)
    return sorted(windows)


def next_due(job, last_run: datetime | None, now: datetime) -> datetime:
    """When the job is next expected to run."""
    anchor = last_run or now
    if job.at:
        upcoming = _clock_windows(job.at, anchor, anchor + timedelta(days=2))
        return upcoming[0] if upcoming else anchor + timedelta(days=1)
    return anchor + job.every_delta


def missed_windows(job, last_run: datetime | None, created_at: datetime, now: datetime) -> list[datetime]:
    """Windows that closed (window + grace) with no run to cover them."""
    since = last_run or created_at
    deadline = now - job.grace_delta
    return [w for w in expected_windows(job, since, now) if w <= deadline]
