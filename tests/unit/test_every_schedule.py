"""``Every`` interval schedule for scheduler jobs (specs/003-jira-commitments T005, research J8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scribe.scheduling.scheduler import Every, next_run, previous_run

T = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)


def test_next_after_adds_the_interval() -> None:
    assert Every(timedelta(minutes=5)).next_after(T) == T + timedelta(minutes=5)


def test_next_run_uses_the_interval() -> None:
    assert next_run(Every(timedelta(seconds=300)), T) == T + timedelta(minutes=5)


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-1)])
def test_non_positive_interval_is_rejected(interval: timedelta) -> None:
    with pytest.raises(ValueError):
        Every(interval)


def test_previous_run_subtracts_the_interval() -> None:
    """Startup requeue (``ensure_jobs``) needs the previous run of an ``Every`` job (specs/005)."""
    assert previous_run(Every(timedelta(seconds=60)), T) == T - timedelta(seconds=60)
