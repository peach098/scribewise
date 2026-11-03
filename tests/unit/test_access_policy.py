"""Pure access policy (005 research Q3, data-model "Derived: AccessStatus"). Synthetic values."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from scribe.access.policy import AccessKind, AccessStatus, is_operator, status, subscription_active
from scribe.config import Settings
from scribe.db.models import BetaEntry, Subscription

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
TELEGRAM_ID = 900000001
OPERATOR_ID = 900000099


def _beta() -> BetaEntry:
    return BetaEntry(telegram_id=TELEGRAM_ID, added_by="operator", added_at=NOW - timedelta(days=1))


def _sub(
    starts_at: datetime, ends_at: datetime, *, cancelled_at: datetime | None = None
) -> Subscription:
    return Subscription(
        telegram_id=TELEGRAM_ID,
        starts_at=starts_at,
        ends_at=ends_at,
        cancelled_at=cancelled_at,
        granted_by="operator",
    )


def _active(days: int = 30) -> Subscription:
    return _sub(NOW - timedelta(days=1), NOW + timedelta(days=days))


def test_no_rows_is_none() -> None:
    result = status(None, [], is_operator=False, now=NOW)
    assert result == AccessStatus(AccessKind.none, None)
    assert result.has_access is False


def test_beta_entry_is_beta() -> None:
    result = status(_beta(), [], is_operator=False, now=NOW)
    assert result == AccessStatus(AccessKind.beta, None)
    assert result.has_access is True


def test_active_subscription_is_subscribed_until_its_end() -> None:
    sub = _active()
    result = status(None, [sub], is_operator=False, now=NOW)
    assert result == AccessStatus(AccessKind.subscribed, sub.ends_at)
    assert result.has_access is True


def test_beta_and_active_subscription() -> None:
    sub = _active()
    result = status(_beta(), [sub], is_operator=False, now=NOW)
    assert result == AccessStatus(AccessKind.beta_and_subscribed, sub.ends_at)


def test_operator_has_access_without_rows() -> None:
    result = status(None, [], is_operator=True, now=NOW)
    assert result.kind is AccessKind.operator
    assert result.has_access is True


def test_operator_wins_over_rows() -> None:
    assert status(_beta(), [_active()], is_operator=True, now=NOW).kind is AccessKind.operator


def test_starts_now_is_active() -> None:
    sub = _sub(NOW, NOW + timedelta(days=30))
    assert subscription_active(sub, NOW) is True
    assert status(None, [sub], is_operator=False, now=NOW).kind is AccessKind.subscribed


def test_ends_now_is_not_active() -> None:
    sub = _sub(NOW - timedelta(days=30), NOW)
    assert subscription_active(sub, NOW) is False
    assert status(None, [sub], is_operator=False, now=NOW) == AccessStatus(AccessKind.none, None)


def test_cancelled_is_not_active() -> None:
    sub = _sub(NOW - timedelta(days=1), NOW + timedelta(days=30), cancelled_at=NOW)
    assert subscription_active(sub, NOW) is False
    assert status(None, [sub], is_operator=False, now=NOW).kind is AccessKind.none


def test_future_start_is_not_active() -> None:
    sub = _sub(NOW + timedelta(seconds=1), NOW + timedelta(days=30))
    assert subscription_active(sub, NOW) is False
    assert status(None, [sub], is_operator=False, now=NOW).kind is AccessKind.none


def test_until_is_latest_end_of_active_rows() -> None:
    first = _sub(NOW - timedelta(days=10), NOW + timedelta(days=20))
    second = _sub(NOW - timedelta(days=5), NOW + timedelta(days=50))
    chained = _sub(NOW + timedelta(days=50), NOW + timedelta(days=80))  # scheduled, not active
    ended = _sub(NOW - timedelta(days=90), NOW - timedelta(days=60))
    result = status(None, [first, chained, second, ended], is_operator=False, now=NOW)
    assert result == AccessStatus(AccessKind.subscribed, second.ends_at)


def test_beta_with_only_inactive_subscriptions_is_beta() -> None:
    ended = _sub(NOW - timedelta(days=90), NOW - timedelta(days=60))
    assert status(_beta(), [ended], is_operator=False, now=NOW) == AccessStatus(
        AccessKind.beta, None
    )


@pytest.mark.parametrize(("telegram_id", "expected"), [(OPERATOR_ID, True), (TELEGRAM_ID, False)])
def test_is_operator(telegram_id: int, expected: bool) -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        master_key=base64.b64encode(b"\x00" * 32).decode(),
        bot_token="placeholder",
        tg_api_id=1,
        tg_api_hash="placeholder",
        operator_telegram_ids=[OPERATOR_ID],
    )
    assert is_operator(telegram_id, settings) is expected
