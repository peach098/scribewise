"""The pre-checkout decision (specs/015-telegram-stars-payments research P3, FR-003, SC-003).

Telegram asks the bot to accept or decline a charge before it happens, and the answer must come
within 10 s. :func:`precheckout_decision` uses one DB read pass and no other network call; the
handler wraps it in a 5 s timeout and declines on expiry. A declined check never charges.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from scribe.config import Settings, payments_available
from scribe.db.models import PaymentOffer, StarsSubscription, StarsSubState
from scribe.payments import repo
from scribe.payments.offers import parse_payload

OFFER_TTL = timedelta(hours=1)  # FR-003: an offer is valid for a first payment for 1 hour
STARS_CURRENCY = "XTR"


class DeclineReason(enum.StrEnum):
    """Each member has an ``error_message`` text ``pay.declined_<reason>``."""

    off = "off"  # payments turned off
    price = "price"  # the amount differs from the offer or the current price
    expired = "expired"  # unknown or old offer
    running = "running"  # already has a running Stars subscription
    error = "error"  # wrong currency, internal error or timeout


@dataclass(frozen=True, slots=True)
class Accept:
    pass


@dataclass(frozen=True, slots=True)
class Decline:
    reason: DeclineReason


def decide(
    settings: Settings,
    *,
    payer_id: int,
    currency: str,
    total_amount: int,
    now: datetime,
    offer: PaymentOffer | None,
    linked: StarsSubscription | None,
    payer_running: StarsSubscription | None,
) -> Accept | Decline:
    """The decision table over plain rows: ``offer`` is the row the payload names, ``linked``
    the Stars subscription created from that offer, ``payer_running`` the payer's own running
    subscription."""
    if currency != STARS_CURRENCY:
        return Decline(DeclineReason.error)
    if offer is None:
        return Decline(DeclineReason.expired)

    if linked is not None:
        # Renewal path: a renewal repeats the first payment's payload. Accept it for the
        # payer's own running subscription whatever the settings say (Telegram charges it
        # anyway; a lost `subscription` re-enable update must not block it). Any other use
        # of a used offer is not a first payment.
        if linked.telegram_id != payer_id:
            return Decline(DeclineReason.running)
        if linked.state in (StarsSubState.active, StarsSubState.canceled):
            return Accept()
        return Decline(DeclineReason.expired)

    # First-payment path.
    if not payments_available(settings):
        return Decline(DeclineReason.off)
    if now - offer.created_at > OFFER_TTL:
        return Decline(DeclineReason.expired)
    if total_amount != offer.price_stars or offer.price_stars != settings.stars_price:
        return Decline(DeclineReason.price)
    if payer_running is not None:
        return Decline(DeclineReason.running)
    return Accept()


async def precheckout_decision(
    session: AsyncSession,
    settings: Settings,
    *,
    payer_id: int,
    payload: str,
    currency: str,
    total_amount: int,
    now: datetime,
) -> Accept | Decline:
    """Load the rows the payload names and apply :func:`decide`. Pure apart from reads."""
    offer_id = parse_payload(payload)
    offer = await repo.get_offer(session, offer_id) if offer_id is not None else None
    linked = await repo.by_offer(session, offer.id) if offer is not None else None
    running = await repo.running_for(session, payer_id) if offer is not None else None
    return decide(
        settings,
        payer_id=payer_id,
        currency=currency,
        total_amount=total_amount,
        now=now,
        offer=offer,
        linked=linked,
        payer_running=running,
    )
