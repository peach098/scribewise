"""Working-hours arithmetic (research R4b). Pure and DST-safe via ``zoneinfo``.

A working window is ``[start, end)`` local time on each day in ``working_days`` (0 = Monday).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

_MAX_DAYS_AHEAD = 8  # a full week plus today


def next_allowed(
    t: datetime,
    tz: str | ZoneInfo,
    working_days: Collection[int],
    start: time,
    end: time,
) -> datetime:
    """``t`` if it falls inside a working window, else the start of the next window (UTC).

    Window starts that fall into a DST gap (a local time that does not exist that day) resolve
    to the first real instant after the gap.
    """
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("next_allowed needs a timezone-aware datetime")
    if not working_days:
        raise ValueError("working_days must not be empty")
    if start >= end:
        raise ValueError("start must be earlier than end")
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    days = set(working_days)
    moment = t.astimezone(UTC)
    local = moment.astimezone(zone)

    for offset in range(_MAX_DAYS_AHEAD):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        # Round-trip through UTC normalises non-existent (gap) local times.
        window_start = datetime.combine(day, start, tzinfo=zone).astimezone(UTC)
        window_end = datetime.combine(day, end, tzinfo=zone).astimezone(UTC)
        if moment < window_start:
            return window_start
        if moment < window_end:
            return t.astimezone(UTC)
    raise AssertionError("unreachable: a working day occurs within a week")
