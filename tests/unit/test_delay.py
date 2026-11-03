"""pick_delay(): uniform within the inclusive range (T026, FR-016); re-planning, requeue and
read-mark rules for waiting turns."""

from __future__ import annotations

import random
from datetime import UTC, datetime, time

import pytest

from scribe.db.models import User
from scribe.drafting import composer
from scribe.intake.waiting import (
    pick_delay,
    readable_up_to,
    replanned_due_at,
    requeued_due_at,
)
from scribe.sending import send


def test_delay_is_random_within_range() -> None:
    draws = [pick_delay(120, 600) for _ in range(200)]
    assert all(120 <= d <= 600 for d in draws)
    assert len(set(draws)) > 1
    assert all(isinstance(d, int) for d in draws)


def test_fixed_delay_when_min_equals_max() -> None:
    assert {pick_delay(300, 300) for _ in range(20)} == {300}
    assert pick_delay(0, 0) == 0


def test_bounds_are_inclusive_with_injected_rng() -> None:
    rng = random.Random(7)
    draws = {pick_delay(1, 3, rng) for _ in range(500)}
    assert draws == {1, 2, 3}


@pytest.mark.parametrize(("low", "high"), [(-1, 5), (10, 5)])
def test_invalid_ranges(low: int, high: int) -> None:
    with pytest.raises(ValueError):
        pick_delay(low, high)


# --- re-planning and requeue rules (Phase 4 review) ---------------------------------------------


def _user() -> User:
    return User(
        timezone="Europe/Berlin",
        working_days=[1, 2, 3, 4, 5],
        work_start=time(9, 0),
        work_end=time(19, 0),
    )


def test_replan_keeps_an_explicit_read_now_even_after_the_planned_time() -> None:
    arrived = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # 22:00 Berlin
    read_now_at = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)  # after arrived + delay
    now = datetime(2026, 10, 14, 21, 30, tzinfo=UTC)
    morning = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    assert replanned_due_at(arrived, 300, read_now_at, _user(), now) == morning
    assert replanned_due_at(arrived, 300, read_now_at, _user(), now, read_now=True) == read_now_at


def test_requeue_respects_working_hours_unless_read() -> None:
    night = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)  # 03:00 Berlin
    morning = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    assert requeued_due_at(None, _user(), night, read_now=False) == morning
    assert requeued_due_at(night, _user(), night, read_now=False) == night
    assert requeued_due_at(None, _user(), night, read_now=True) == night


def test_readable_up_to_stops_before_a_waiting_message() -> None:
    assert readable_up_to(12, None) == 12
    assert readable_up_to(12, 11) == 10
    assert readable_up_to(9, 11) == 9


def test_reply_to_key_matches_between_composer_and_send() -> None:
    assert composer.REPLY_TO_KEY == send.REPLY_TO_KEY
    assert composer.REPLY_TO_KEY in composer.SYSTEM_OUTPUT_KEYS
