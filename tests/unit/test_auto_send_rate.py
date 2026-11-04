"""specs/014 FR-015, FR-018: ``should_offer`` decides from the chat's 30-day figures alone. Plain
objects, no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from scribe.db.models import AutoSendOfferState, User, WorkChat
from scribe.db.repos.decisions import ChatRate
from scribe.sending.auto_send import should_offer

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _offer(unchanged: int, total: int, *, chat: dict[str, Any] | None = None, **user: Any) -> bool:
    work_chat = WorkChat(auto_send=False, auto_send_offer=AutoSendOfferState.none)
    for key, value in (chat or {}).items():
        setattr(work_chat, key, value)
    stored = User(auto_send_last_offer_at=None)
    for key, value in user.items():
        setattr(stored, key, value)
    return should_offer(ChatRate(unchanged, total), work_chat, stored, NOW)


def test_us2_thirteen_of_twenty_is_offered() -> None:
    assert _offer(13, 20)


def test_us2_exactly_sixty_percent_is_not() -> None:
    assert not _offer(12, 20)


def test_us2_below_twenty_decisions_is_not() -> None:
    assert not _offer(19, 19)


def test_us2_auto_send_already_on_is_not() -> None:
    assert not _offer(21, 30, chat={"auto_send": True})


def test_us2_opted_out_is_not() -> None:
    assert not _offer(20, 20, chat={"auto_send_offer": AutoSendOfferState.opted_out})


def test_us2_snoozed_until_the_future_is_not_and_the_past_is() -> None:
    assert not _offer(15, 20, chat={"auto_send_offer_not_before": NOW + timedelta(days=1)})
    assert _offer(15, 20, chat={"auto_send_offer_not_before": NOW - timedelta(seconds=1)})


def test_us2_another_offer_within_24_hours_is_not() -> None:
    assert not _offer(15, 20, auto_send_last_offer_at=NOW - timedelta(hours=23))
    assert _offer(15, 20, auto_send_last_offer_at=NOW - timedelta(hours=25))
