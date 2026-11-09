"""Crediting a payment exactly once (specs/015-telegram-stars-payments research P4, P8, FR-004,
FR-005, SC-002).

:func:`credit_payment` is the only way a Stars charge becomes access. It is called by the
``successful_payment`` handler and by the ``stars_reconcile`` backfill, so a payment polling
lost is still credited. The exactly-once key is Telegram's ``telegram_payment_charge_id``
(``payments.charge_id`` is unique and inserted with ``ON CONFLICT DO NOTHING``).

In the caller's transaction it records the payment, the person's Stars subscription (a first
payment creates it, a renewal moves its ``period_end`` and revives one that ended as ``expired``
or ``failed``: Telegram has charged the person) and the access period as an ordinary
``subscriptions`` row with ``source='stars'``, so the specs/005 gate, reconcile job and
``/my_data`` work unchanged. The caller commits, then sends the notice. A payment whose payload
is not ours, or that fits no subscription, is still recorded with a 30-day period
(:class:`Unmatched`): Telegram has charged the person, so it is never dropped silently.

Periods follow Telegram's expiry date, which is authoritative (:func:`first_period`,
:func:`next_period`). Logs carry kinds and amounts only (research P14).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.access import repo as access_repo
from scribe.db.models import (
    AccessEventKind,
    Payment,
    PaymentKind,
    PaymentSource,
    StarsCancelSource,
    StarsEndReason,
    StarsSubscription,
    StarsSubState,
    Subscription,
    SubscriptionSource,
)
from scribe.logging import get_logger
from scribe.payments import repo
from scribe.payments.offers import parse_payload
from scribe.payments.telegram import SUBSCRIPTION_PERIOD_S

log = get_logger("scribe.payments")

SUBSCRIPTION_PERIOD = timedelta(seconds=SUBSCRIPTION_PERIOD_S)
STARS_ACTOR = "system"
REVIVABLE = (StarsEndReason.expired, StarsEndReason.failed)  # data-model "State transitions"


# --- period math --------------------------------------------------------------------------------


def _period(start: datetime, expires_at: datetime | None) -> tuple[datetime, datetime]:
    end = start + SUBSCRIPTION_PERIOD if expires_at is None else expires_at
    if end <= start:
        raise ValueError("the subscription expiry is not after the period start")
    return start, end


def first_period(paid_at: datetime, expires_at: datetime | None) -> tuple[datetime, datetime]:
    """``[paid_at, expires_at)``; without Telegram's expiry (a backfill) ``paid_at + 30 days``."""
    return _period(paid_at, expires_at)


def next_period(
    prev_end: datetime, paid_at: datetime, expires_at: datetime | None
) -> tuple[datetime, datetime]:
    """A renewal starts at the later of the payment and the previous period's end, so a renewal
    confirmed late leaves no gap and an early one no overlap."""
    return _period(max(paid_at, prev_end), expires_at)


# --- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Credited:
    payment: Payment
    stars: StarsSubscription
    first: bool  # a first payment (the person is told); a renewal is silent (FR-006)


@dataclass(frozen=True, slots=True)
class AlreadyCredited:
    pass


@dataclass(frozen=True, slots=True)
class Unmatched:
    """Recorded and given a 30-day period, but tied to no Stars subscription."""

    payment: Payment


@dataclass(frozen=True, slots=True)
class Refunded:
    payment: Payment
    period_ended: bool  # the refund took away an access period


@dataclass(frozen=True, slots=True)
class AlreadyRefunded:
    pass


@dataclass(frozen=True, slots=True)
class NotFound:
    pass


# --- crediting ----------------------------------------------------------------------------------


async def credit_payment(
    session: AsyncSession,
    *,
    payer_id: int,
    charge_id: str,
    amount: int,
    payload: str,
    paid_at: datetime,
    expires_at: datetime | None,
    is_first: bool,
    via: PaymentSource,
    now: datetime,
) -> Credited | AlreadyCredited | Unmatched:
    """Credit one charge. Idempotent on ``charge_id``; the caller commits."""
    if await repo.by_charge(session, charge_id) is not None:
        log.info("stars_payment_duplicate", via=via.value)
        return AlreadyCredited()

    offer_id = parse_payload(payload)
    offer = await repo.get_offer(session, offer_id) if offer_id is not None else None
    stars = await repo.by_offer(session, offer.id) if offer is not None else None
    # A first payment for an offer that already has a subscription is a renewal (defensive).
    first = is_first and stars is None
    unmatched: str | None = None
    if offer is None:
        unmatched = "unknown_payload"
    elif first:
        if await repo.running_for(session, payer_id) is not None:
            unmatched = "already_running"
    elif stars is None or stars.telegram_id != payer_id:
        unmatched = "no_subscription"

    if unmatched is None and not first:
        assert stars is not None
        start, end = _safe_period(paid_at, expires_at, prev_end=stars.period_end)
    else:
        start, end = _safe_period(paid_at, expires_at)

    payment = await session.scalar(
        insert(Payment)
        .values(
            id=uuid.uuid4(),
            telegram_id=payer_id,
            charge_id=charge_id,
            amount_stars=amount,
            kind=PaymentKind.first if is_first else PaymentKind.renewal,
            paid_at=paid_at,
            period_start=start,
            period_end=end,
            credited_via=via,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[Payment.charge_id])
        .returning(Payment)
    )
    if payment is None:  # a concurrent delivery won the race
        log.info("stars_payment_duplicate", via=via.value)
        return AlreadyCredited()

    event = AccessEventKind.payment_received if is_first else AccessEventKind.payment_renewed
    if unmatched is not None or offer is None:
        log.warning("stars_payment_unmatched", reason=unmatched, via=via.value)
        await _grant_period(session, payment, event, now)
        return Unmatched(payment)

    if first:
        stars = StarsSubscription(
            id=uuid.uuid4(),
            telegram_id=payer_id,
            offer_id=offer.id,
            first_charge_id=charge_id,
            price_stars=amount,
            started_at=paid_at,
            period_end=end,
            state=StarsSubState.active,
            created_at=now,
        )
        session.add(stars)
        offer.used_at = now
    else:
        assert stars is not None
        _revive_if_ended(stars, now)
        stars.period_end = max(stars.period_end, end)
    await session.flush()
    payment.stars_subscription_id = stars.id
    await _grant_period(session, payment, event, now)
    log.info(
        "stars_payment_credited",
        kind=payment.kind.value,
        via=via.value,
        amount=amount,
    )
    return Credited(payment, stars, first)


async def apply_refund(
    session: AsyncSession, *, charge_id: str, by: str, now: datetime
) -> Refunded | AlreadyRefunded | NotFound:
    """The database part of a refund (research P8), idempotent on the charge id; the caller
    commits. The payment is marked refunded, the access period it bought is cancelled (so
    access ends now unless another source remains) and the Stars subscription, if still
    running, ends as ``refunded``. ``by`` is ``operator:<note or cli>`` or ``telegram``."""
    payment = await session.scalar(
        select(Payment)
        .where(Payment.charge_id == charge_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None:
        return NotFound()
    if payment.refunded_at is not None:
        return AlreadyRefunded()
    payment.refunded_at = now
    payment.refunded_by = by
    access = await session.scalar(
        select(Subscription).where(Subscription.payment_id == payment.id).with_for_update()
    )
    period_ended = access is not None and access.cancelled_at is None
    if access is not None and period_ended:
        access.cancelled_at = now
    stars = (
        await session.get(StarsSubscription, payment.stars_subscription_id)
        if payment.stars_subscription_id is not None
        else None
    )
    if stars is not None and stars.state is not StarsSubState.ended:
        log.info("stars_subscription_state", from_=stars.state.value, to="ended", by="refund")
        stars.state = StarsSubState.ended
        stars.canceled_at = stars.canceled_at or now
        stars.canceled_by = StarsCancelSource.refund
        stars.ended_at = now
        stars.ended_reason = StarsEndReason.refunded
    await access_repo.record_event(
        session,
        payment.telegram_id,
        AccessEventKind.payment_refunded,
        actor="operator" if by.startswith("operator") else STARS_ACTOR,
        meta={"charge_id": payment.charge_id, "amount": payment.amount_stars, "by": by},
        now=now,
    )
    log.info("stars_refund", result="ok", amount=payment.amount_stars)
    return Refunded(payment, period_ended)


def _safe_period(
    paid_at: datetime, expires_at: datetime | None, *, prev_end: datetime | None = None
) -> tuple[datetime, datetime]:
    """The period, or a plain 30 days when Telegram's expiry is not after its start: the person
    has paid, so the payment is still credited."""
    try:
        if prev_end is not None:
            return next_period(prev_end, paid_at, expires_at)
        return first_period(paid_at, expires_at)
    except ValueError:
        start = max(paid_at, prev_end) if prev_end is not None else paid_at
        return start, start + SUBSCRIPTION_PERIOD


def _revive_if_ended(stars: StarsSubscription, now: datetime) -> None:
    """A renewal arrived for a subscription ended as ``expired`` or ``failed``: Telegram has
    charged the person, so it runs again (data-model.md "State transitions")."""
    if stars.state is StarsSubState.ended and stars.ended_reason in REVIVABLE:
        log.info("stars_subscription_state", from_="ended", to="active", by="late_renewal")
        stars.state = StarsSubState.active
        stars.ended_at = None
        stars.ended_reason = None
        stars.canceled_at = None
        stars.canceled_by = None


async def _grant_period(
    session: AsyncSession, payment: Payment, event: AccessEventKind, now: datetime
) -> None:
    session.add(
        Subscription(
            telegram_id=payment.telegram_id,
            starts_at=payment.period_start,
            ends_at=payment.period_end,
            granted_by="stars",
            source=SubscriptionSource.stars,
            payment_id=payment.id,
        )
    )
    await session.flush()
    await access_repo.record_event(
        session,
        payment.telegram_id,
        event,
        actor=STARS_ACTOR,
        meta={"charge_id": payment.charge_id, "amount": payment.amount_stars},
        now=now,
    )


__all__ = [
    "AlreadyCredited",
    "AlreadyRefunded",
    "Credited",
    "NotFound",
    "Refunded",
    "SUBSCRIPTION_PERIOD",
    "Unmatched",
    "apply_refund",
    "credit_payment",
    "first_period",
    "next_period",
]
