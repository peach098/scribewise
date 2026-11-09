"""Subscription offers (specs/015-telegram-stars-payments research P1, P2, P12).

An offer is a ``payment_offers`` row plus an invoice link made for it. The invoice payload is
``sub1:<offer id>``: bot-made, 41 bytes, no personal data. Every renewal and every
``subscription`` update repeats it, so the offer is also how a Stars subscription is found.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.access.policy import AccessKind, AccessStatus
from scribe.config import Settings, payments_available
from scribe.db.models import PaymentOffer, StarsSubscription
from scribe.i18n import format_day_month_year, t
from scribe.logging import get_logger
from scribe.payments import repo
from scribe.payments.telegram import StarsApi

log = get_logger("scribe.payments")

PAYLOAD_PREFIX = "sub1:"
MAX_PAYLOAD_BYTES = 128  # Telegram's limit for an invoice payload


def make_payload(offer_id: uuid.UUID) -> str:
    return f"{PAYLOAD_PREFIX}{offer_id}"


def parse_payload(payload: str) -> uuid.UUID | None:
    """The offer id of ``sub1:<uuid>``; ``None`` for anything else (strict, research P2)."""
    if len(payload.encode()) > MAX_PAYLOAD_BYTES or not payload.startswith(PAYLOAD_PREFIX):
        return None
    raw = payload[len(PAYLOAD_PREFIX) :]
    try:
        offer_id = uuid.UUID(raw)
    except ValueError:
        return None
    return offer_id if str(offer_id) == raw else None  # one canonical spelling only


# --- making an offer ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Offer:
    offer_id: uuid.UUID
    url: str  # the invoice link
    price: int


@dataclass(frozen=True, slots=True)
class Running:
    """The person already has a running Stars subscription: show the manage view instead."""

    stars: StarsSubscription


@dataclass(frozen=True, slots=True)
class Unavailable:
    """Payments are not available (off, no price, gate off): today's placeholder."""


def price_text(price: int) -> str:
    return f"{price} ⭐"


async def make_offer(
    session: AsyncSession,
    api: StarsApi,
    settings: Settings,
    telegram_id: int,
    *,
    lang: str,
    now: datetime,
) -> Offer | Running | Unavailable:
    """Create an invoice link and its ``payment_offers`` row; the caller commits. The link is
    made first, so a Telegram error (:class:`StarsApiError`) leaves no row behind."""
    price = settings.stars_price
    if not payments_available(settings) or price is None:
        return Unavailable()
    running = await repo.running_for(session, telegram_id)
    if running is not None:
        return Running(running)
    offer_id = uuid.uuid4()
    url = await api.create_subscription_link(
        payload=make_payload(offer_id),
        price=price,
        title=t("pay.invoice_title", lang),
        description=t("pay.invoice_description", lang),
        label=t("pay.invoice_label", lang),
    )
    session.add(
        PaymentOffer(id=offer_id, telegram_id=telegram_id, price_stars=price, created_at=now)
    )
    await session.flush()
    log.info("stars_offer_created")
    return Offer(offer_id, url, price)


def offer_view(
    offer: Offer,
    lang: str,
    *,
    access: AccessStatus | None = None,
    tz: str = "UTC",
) -> tuple[str, InlineKeyboardMarkup]:
    """``pay.offer`` plus, for someone who already has access, one line saying so, and the URL
    button that opens the invoice (contracts/control-chat.md "Offer")."""
    text = t("pay.offer", lang, price=price_text(offer.price))
    if access is not None and access.kind in (AccessKind.beta, AccessKind.beta_and_subscribed):
        text = f"{text}\n\n{t('pay.offer_has_beta', lang)}"
    elif access is not None and access.kind is AccessKind.subscribed and access.until is not None:
        day = format_day_month_year(access.until.astimezone(ZoneInfo(tz)).date(), lang)
        text = f"{text}\n\n{t('pay.offer_has_sub', lang, date=day)}"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("pay.btn_subscribe", lang, price=price_text(offer.price)), url=offer.url
                )
            ]
        ]
    )
    return text, markup
