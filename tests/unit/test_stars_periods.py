"""Period math of a Stars payment (specs/015 research P4, FR-005, SC-004). Pure functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scribe.payments.credit import SUBSCRIPTION_PERIOD, first_period, next_period

PAID = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
EXPIRES = PAID + timedelta(days=30)


def test_first_payment_runs_from_payment_to_telegrams_expiry() -> None:
    assert first_period(PAID, EXPIRES) == (PAID, EXPIRES)


def test_backfill_without_expiry_uses_thirty_days() -> None:
    assert timedelta(days=30) == SUBSCRIPTION_PERIOD
    assert first_period(PAID, None) == (PAID, PAID + timedelta(days=30))


def test_renewal_starts_at_the_previous_end() -> None:
    prev_end = EXPIRES
    paid = prev_end - timedelta(minutes=5)  # Telegram charges slightly before the end
    expires = prev_end + timedelta(days=30)
    assert next_period(prev_end, paid, expires) == (prev_end, expires)


def test_late_renewal_starts_at_payment_and_ends_at_telegrams_expiry() -> None:
    prev_end = EXPIRES
    paid = prev_end + timedelta(minutes=20)
    expires = prev_end + timedelta(days=30, minutes=20)
    assert next_period(prev_end, paid, expires) == (paid, expires)


def test_renewal_without_expiry_uses_thirty_days_from_its_start() -> None:
    prev_end = EXPIRES
    paid = prev_end + timedelta(hours=2)
    assert next_period(prev_end, paid, None) == (paid, paid + timedelta(days=30))


def test_expiry_not_after_the_start_is_refused() -> None:
    with pytest.raises(ValueError):
        first_period(PAID, PAID)
    with pytest.raises(ValueError):
        first_period(PAID, PAID - timedelta(days=1))
    with pytest.raises(ValueError):
        next_period(EXPIRES, EXPIRES + timedelta(hours=1), EXPIRES + timedelta(minutes=30))
