"""Scheduler pure parts (T021): job schedules and step dispatch, without a database."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any

import pytest

from scribe.scheduling.scheduler import Daily, Scheduler, Weekly, next_run


def test_daily_next_run_is_strictly_after() -> None:
    daily = Daily(at=time(3, 0))
    assert next_run(daily, datetime(2026, 10, 14, 2, 59, tzinfo=UTC)) == datetime(
        2026, 10, 14, 3, 0, tzinfo=UTC
    )
    assert next_run(daily, datetime(2026, 10, 14, 3, 0, tzinfo=UTC)) == datetime(
        2026, 10, 15, 3, 0, tzinfo=UTC
    )
    # Inputs in other time zones are normalised to UTC.
    plus3 = timezone(timedelta(hours=3))
    assert next_run(daily, datetime(2026, 10, 14, 5, 30, tzinfo=plus3)) == datetime(
        2026, 10, 14, 3, 0, tzinfo=UTC
    )  # 02:30 UTC
    assert next_run(daily, datetime(2026, 10, 14, 6, 30, tzinfo=plus3)) == datetime(
        2026, 10, 15, 3, 0, tzinfo=UTC
    )  # 03:30 UTC


def test_weekly_next_run() -> None:
    monday_4am = Weekly(weekday=0, at=time(4, 0))
    wednesday = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    assert next_run(monday_4am, wednesday) == datetime(2026, 10, 19, 4, 0, tzinfo=UTC)
    exactly = datetime(2026, 10, 19, 4, 0, tzinfo=UTC)
    assert next_run(monday_4am, exactly) == datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
    same_day_before = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)
    assert next_run(monday_4am, same_day_before) == exactly
    with pytest.raises(ValueError):
        Weekly(weekday=7, at=time(0, 0))


def _unused_session_factory() -> Any:
    def factory() -> Any:
        raise AssertionError("the database must not be touched")

    return factory


async def test_steps_without_handlers_do_not_touch_the_database() -> None:
    scheduler = Scheduler(_unused_session_factory(), tick_s=5)
    await scheduler.tick()
    assert scheduler.last_tick_at is not None


async def test_tick_runs_steps_in_order_and_isolates_failures() -> None:
    scheduler = Scheduler(_unused_session_factory(), tick_s=5)
    calls: list[str] = []

    def step(name: str, fail: bool = False) -> Any:
        async def run() -> None:
            calls.append(name)
            if fail:
                raise RuntimeError(name)

        return run

    scheduler._step_due_turns = step("due_turns", fail=True)  # type: ignore[method-assign]
    scheduler._step_reminders = step("reminders")  # type: ignore[method-assign]
    scheduler._step_expiries = step("expiries", fail=True)  # type: ignore[method-assign]
    scheduler._step_jobs = step("jobs")  # type: ignore[method-assign]
    await scheduler.tick()
    assert calls == ["due_turns", "reminders", "expiries", "jobs"]


async def test_no_claim_when_in_flight_capacity_is_used() -> None:
    scheduler = Scheduler(_unused_session_factory(), tick_s=5, max_in_flight_turns=10)

    async def handler(ids: list[Any]) -> None:
        return None

    scheduler.on_due_turns(handler)
    scheduler._turns_in_flight = 10
    await scheduler._step_due_turns()  # would raise if it opened a session
