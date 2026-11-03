"""next_allowed(): working hours, days and time zones, DST-safe (T026, research R4b)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from scribe.scheduling.working_hours import next_allowed

MON_FRI = [0, 1, 2, 3, 4]
NINE, SEVEN_PM = time(9, 0), time(19, 0)
MSK = ZoneInfo("Europe/Moscow")  # UTC+3, no DST
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")


def _local(tz: ZoneInfo, year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def test_inside_window_returns_same_instant() -> None:
    t = _local(MSK, 2026, 10, 14, 12, 30).astimezone(UTC)  # Wednesday 12:30
    assert next_allowed(t, "Europe/Moscow", MON_FRI, NINE, SEVEN_PM) == t


def test_window_bounds_start_inclusive_end_exclusive() -> None:
    start = _local(MSK, 2026, 10, 14, 9, 0)
    end = _local(MSK, 2026, 10, 14, 19, 0)
    assert next_allowed(start, MSK, MON_FRI, NINE, SEVEN_PM) == start
    assert next_allowed(end, MSK, MON_FRI, NINE, SEVEN_PM) == _local(MSK, 2026, 10, 15, 9, 0)


def test_next_allowed_outside_hours() -> None:
    # 22:00 on a Wednesday -> Thursday 09:00 local.
    t = _local(MSK, 2026, 10, 14, 22, 0)
    result = next_allowed(t, MSK, MON_FRI, NINE, SEVEN_PM)
    assert result == _local(MSK, 2026, 10, 15, 9, 0)
    assert result.tzinfo is not None and result.utcoffset() == UTC.utcoffset(None)

    # Early morning -> same day 09:00.
    early = _local(MSK, 2026, 10, 14, 6, 15)
    assert next_allowed(early, MSK, MON_FRI, NINE, SEVEN_PM) == _local(MSK, 2026, 10, 14, 9, 0)

    # Friday evening and the weekend -> Monday 09:00.
    friday = _local(MSK, 2026, 10, 16, 20, 0)
    saturday = _local(MSK, 2026, 10, 17, 12, 0)
    monday = _local(MSK, 2026, 10, 19, 9, 0)
    assert next_allowed(friday, MSK, MON_FRI, NINE, SEVEN_PM) == monday
    assert next_allowed(saturday, MSK, MON_FRI, NINE, SEVEN_PM) == monday

    # US DST ends on Sunday 2026-11-01: Friday 22:00 EDT (UTC-4) -> Monday 09:00 EST (UTC-5).
    friday_ny = _local(NY, 2026, 10, 30, 22, 0)
    result_ny = next_allowed(friday_ny, NY, MON_FRI, NINE, SEVEN_PM)
    assert result_ny == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    assert result_ny.astimezone(NY).hour == 9


def test_dst_gap_start_time_and_spring_forward() -> None:
    # Berlin springs forward on Sunday 2026-03-29 (02:00 -> 03:00). A window starting at 02:30
    # (which does not exist that day) opens at the first real instant after the gap.
    every_day = list(range(7))
    t = _local(BERLIN, 2026, 3, 29, 1, 0)
    result = next_allowed(t, BERLIN, every_day, time(2, 30), time(18, 0))
    assert result == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert result > t

    # Saturday evening before the change -> Monday 09:00 CEST (UTC+2).
    saturday = _local(BERLIN, 2026, 3, 28, 20, 0)
    assert next_allowed(saturday, BERLIN, MON_FRI, NINE, SEVEN_PM) == datetime(
        2026, 3, 30, 7, 0, tzinfo=UTC
    )


def test_single_working_day_wraps_to_next_week() -> None:
    wednesday_late = _local(MSK, 2026, 10, 14, 19, 30)
    assert next_allowed(wednesday_late, MSK, [2], NINE, SEVEN_PM) == _local(MSK, 2026, 10, 21, 9, 0)


def test_rejects_naive_datetimes_and_empty_days() -> None:
    with pytest.raises(ValueError):
        next_allowed(datetime(2026, 10, 14, 12, 0), MSK, MON_FRI, NINE, SEVEN_PM)
    with pytest.raises(ValueError):
        next_allowed(_local(MSK, 2026, 10, 14, 12, 0), MSK, [], NINE, SEVEN_PM)
