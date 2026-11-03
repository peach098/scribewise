"""Pacing, backoff and Telegram error classes (specs/011-release-notes research R7, R8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage

from scribe.release_notes.sender import PER_CHAT_GAP_S, Outcome, Pacer, backoff, classify

METHOD = SendMessage(chat_id=1, text="x")


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


async def test_overall_rate() -> None:
    clock = _Clock()
    pacer = Pacer(10, clock=clock, sleep=clock.sleep)
    for chat_id in range(50):
        await pacer.wait(chat_id)
    assert sum(clock.sleeps) >= 4.9 - 1e-6
    assert sum(clock.sleeps) < 5.0


async def test_no_wait_when_time_has_passed() -> None:
    clock = _Clock()
    pacer = Pacer(10, clock=clock, sleep=clock.sleep)
    await pacer.wait(1)
    clock.now += timedelta(seconds=5)
    await pacer.wait(2)
    assert clock.sleeps == []


async def test_same_chat_spacing() -> None:
    clock = _Clock()
    pacer = Pacer(25, clock=clock, sleep=clock.sleep)
    await pacer.wait(7)
    start = clock.now
    await pacer.wait(7)
    assert (clock.now - start).total_seconds() >= PER_CHAT_GAP_S
    await pacer.wait(8)  # another chat only waits for the overall rate
    assert clock.sleeps[-1] == pytest.approx(1 / 25)


def test_backoff_steps() -> None:
    assert [backoff(n) for n in range(1, 6)] == [
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=10),
        timedelta(minutes=30),
        timedelta(minutes=30),
    ]


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (TelegramRetryAfter(METHOD, "flood", retry_after=3), Outcome.retry_after),
        (TelegramForbiddenError(METHOD, "bot was blocked by the user"), Outcome.blocked),
        (TelegramForbiddenError(METHOD, "user is deactivated"), Outcome.blocked),
        (TelegramBadRequest(METHOD, "Bad Request: chat not found"), Outcome.blocked),
        (TelegramNotFound(METHOD, "Not Found"), Outcome.blocked),
        (TelegramBadRequest(METHOD, "Bad Request: message is too long"), Outcome.failed),
        (TelegramNetworkError(METHOD, "timeout"), Outcome.retryable),
        (TelegramServerError(METHOD, "Bad Gateway"), Outcome.retryable),
        (TelegramAPIError(METHOD, "something else"), Outcome.retryable),
        (OSError("connection reset"), Outcome.retryable),
    ],
)
def test_classify(exc: Exception, outcome: Outcome) -> None:
    assert classify(exc) is outcome


async def test_shared_pacer_serializes_concurrent_senders() -> None:
    """Issue #46: the grammatical gender job shares the release notes pacer; two senders
    waiting at once still get one budget: consecutive sends at least 1/rate apart."""
    import asyncio

    clock = _Clock()

    async def yielding_sleep(seconds: float) -> None:
        # Virtual time like a real event loop: each sleeper wakes at its own deadline, and
        # the other sender runs meanwhile.
        wake = clock.now + timedelta(seconds=seconds)
        await asyncio.sleep(0)
        clock.now = max(clock.now, wake)

    pacer = Pacer(10, clock=clock, sleep=yielding_sleep)
    sent: list[datetime] = []

    async def sender(offset: int) -> None:
        for chat_id in range(10):
            await pacer.wait(offset + chat_id)
            sent.append(clock.now)
            await asyncio.sleep(0)

    await asyncio.gather(sender(0), sender(1000))
    gaps = [(b - a).total_seconds() for a, b in zip(sent, sent[1:], strict=False)]
    assert len(sent) == 20 and min(gaps) >= 0.1 - 1e-6


async def test_one_chats_gap_does_not_block_another_chat() -> None:
    """Issue #46 review: a sender waiting out chat X's gap must not hold the shared pacer, so a
    send to chat Y that starts meanwhile goes first (real time: about PER_CHAT_GAP_S)."""
    import asyncio

    def now() -> datetime:
        return datetime.now(UTC)

    pacer = Pacer(10, clock=now, sleep=asyncio.sleep)
    start = now()
    await pacer.wait(1)  # chat X
    sent: dict[str, datetime] = {}

    async def again_x() -> None:
        await pacer.wait(1)  # waits PER_CHAT_GAP_S
        sent["x"] = now()

    async def other_y() -> None:
        await asyncio.sleep(0.05)  # starts while X waits out its gap
        await pacer.wait(2)
        sent["y"] = now()

    await asyncio.gather(again_x(), other_y())
    assert sent["y"] - start < timedelta(seconds=0.5)
    assert sent["x"] - start >= timedelta(seconds=PER_CHAT_GAP_S)


async def test_a_clock_that_does_not_move_cannot_make_it_spin() -> None:
    """Issue #46 review: the per-chat wait is computed once, so a frozen injected clock (as in
    tests) returns instead of looping."""
    import asyncio

    frozen = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    pacer = Pacer(10, clock=lambda: frozen, sleep=sleep)
    await asyncio.wait_for(pacer.wait(1), timeout=1)
    await asyncio.wait_for(pacer.wait(1), timeout=1)
    assert sleeps and sleeps[0] == pytest.approx(PER_CHAT_GAP_S)
