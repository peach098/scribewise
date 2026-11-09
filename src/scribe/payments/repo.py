"""Payment queries (specs/015-telegram-stars-payments data-model.md).

Every function takes the caller's ``AsyncSession`` and never commits. Rows are keyed by the
payer's Telegram id. Nothing here reads or writes message content.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import (
    Payment,
    PaymentKind,
    PaymentOffer,
    StarsSubscription,
    StarsSubState,
    Subscription,
    SubscriptionSource,
)

RUNNING_STATES = (StarsSubState.active, StarsSubState.canceled)


async def running_for(session: AsyncSession, telegram_id: int) -> StarsSubscription | None:
    """The person's ``active`` or ``canceled`` Stars subscription (at most one, FR-007)."""
    result = await session.scalars(
        select(StarsSubscription).where(
            StarsSubscription.telegram_id == telegram_id,
            StarsSubscription.state.in_(RUNNING_STATES),
        )
    )
    return result.one_or_none()


async def any_running(session: AsyncSession) -> bool:
    """Whether any Stars subscription is running (``active`` or ``canceled``)."""
    found = await session.scalar(
        select(StarsSubscription.id).where(StarsSubscription.state.in_(RUNNING_STATES)).limit(1)
    )
    return found is not None


async def by_offer(session: AsyncSession, offer_id: uuid.UUID) -> StarsSubscription | None:
    """The Stars subscription a renewal payload or ``subscription`` update points to."""
    result = await session.scalars(
        select(StarsSubscription).where(StarsSubscription.offer_id == offer_id)
    )
    return result.one_or_none()


async def get_offer(session: AsyncSession, offer_id: uuid.UUID) -> PaymentOffer | None:
    return await session.get(PaymentOffer, offer_id)


async def by_charge(session: AsyncSession, charge_id: str) -> Payment | None:
    result = await session.scalars(select(Payment).where(Payment.charge_id == charge_id))
    return result.one_or_none()


async def renewing_payment_ids(session: AsyncSession, telegram_id: int) -> frozenset[uuid.UUID]:
    """Ids of the person's ``stars`` access periods whose Stars subscription is ``active``
    (renewing): these get the 1 h grace (research P5)."""
    return await renewing_payment_ids_bulk(session, [telegram_id])


async def renewing_payment_ids_bulk(
    session: AsyncSession, telegram_ids: Sequence[int]
) -> frozenset[uuid.UUID]:
    """:func:`renewing_payment_ids` of many people at once (payment ids are globally unique)."""
    if not telegram_ids:
        return frozenset()
    rows = await session.scalars(
        select(Subscription.payment_id)
        .join(Payment, Payment.id == Subscription.payment_id)
        .join(StarsSubscription, StarsSubscription.id == Payment.stars_subscription_id)
        .where(
            Subscription.telegram_id.in_(list(telegram_ids)),
            Subscription.source == SubscriptionSource.stars,
            StarsSubscription.state == StarsSubState.active,
        )
    )
    return frozenset(row for row in rows.all() if row is not None)


async def payments_for(
    session: AsyncSession, telegram_id: int, since: datetime | None = None
) -> list[Payment]:
    """The person's payments, newest first, optionally only those paid at or after ``since``."""
    query = select(Payment).where(Payment.telegram_id == telegram_id)
    if since is not None:
        query = query.where(Payment.paid_at >= since)
    result = await session.scalars(
        query.order_by(Payment.paid_at.desc(), Payment.created_at.desc())
    )
    return list(result.all())


async def payments_in_range(
    session: AsyncSession, start: datetime, end: datetime, *, telegram_id: int | None = None
) -> list[Payment]:
    """Payments with ``start <= paid_at < end``, newest first (``pay list``)."""
    query = select(Payment).where(Payment.paid_at >= start, Payment.paid_at < end)
    if telegram_id is not None:
        query = query.where(Payment.telegram_id == telegram_id)
    result = await session.scalars(
        query.order_by(Payment.paid_at.desc(), Payment.created_at.desc())
    )
    return list(result.all())


async def stars_subscriptions_for(
    session: AsyncSession, telegram_id: int
) -> list[StarsSubscription]:
    """Every Stars subscription of the person, newest first."""
    result = await session.scalars(
        select(StarsSubscription)
        .where(StarsSubscription.telegram_id == telegram_id)
        .order_by(StarsSubscription.started_at.desc())
    )
    return list(result.all())


async def paying_subscribers(session: AsyncSession) -> int:
    """Distinct people with a running Stars subscription (``access stats``)."""
    count = await session.scalar(
        select(func.count(func.distinct(StarsSubscription.telegram_id))).where(
            StarsSubscription.state.in_(RUNNING_STATES)
        )
    )
    return int(count or 0)


@dataclass(frozen=True, slots=True)
class PayStats:
    """Totals for ``pay stats`` (contracts/operator-cli.md)."""

    payments: int
    first: int
    renewal: int
    received: int
    refunded_count: int
    refunded: int
    active: int
    canceled_running: int
    ended_in_range: int

    @property
    def net(self) -> int:
        return self.received - self.refunded


async def stats(session: AsyncSession, start: datetime, end: datetime) -> PayStats:
    """Payments paid in ``[start, end)`` and, of those, the refunded ones; the Stars
    subscriptions running now and those that ended in the range."""
    rows = (
        await session.execute(
            select(Payment.kind, Payment.amount_stars, Payment.refunded_at).where(
                Payment.paid_at >= start, Payment.paid_at < end
            )
        )
    ).all()
    refunded_amounts = [amount for _, amount, refunded_at in rows if refunded_at is not None]
    state_rows = await session.execute(
        select(StarsSubscription.state, func.count()).group_by(StarsSubscription.state)
    )
    states = {state: count for state, count in state_rows}
    ended = await session.scalar(
        select(func.count())
        .select_from(StarsSubscription)
        .where(StarsSubscription.ended_at >= start, StarsSubscription.ended_at < end)
    )
    return PayStats(
        payments=len(rows),
        first=sum(1 for kind, _, _ in rows if kind is PaymentKind.first),
        renewal=sum(1 for kind, _, _ in rows if kind is PaymentKind.renewal),
        received=sum(amount for _, amount, _ in rows),
        refunded_count=len(refunded_amounts),
        refunded=sum(refunded_amounts),
        active=int(states.get(StarsSubState.active, 0)),
        canceled_running=int(states.get(StarsSubState.canceled, 0)),
        ended_in_range=int(ended or 0),
    )
