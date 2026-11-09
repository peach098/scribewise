"""``/paysupport`` and ``/terms`` (specs/015-telegram-stars-payments research P11, FR-012).

Telegram asks bots that take payments to answer both commands, so they work for anyone in the
private chat, even without access (the gate allows them) and even with payments off.

- ``/paysupport``: explains how payments work and asks for one message of 1 to 1000 characters
  (``PaySupportStates.describe``). The text is forwarded as **plain text** (``parse_mode=None``)
  to every operator with the sender's id, ``@username`` and the charge ids of their payments
  from the last 90 days. It is **not stored**: the only trace is the FSM state and, for the
  10-minute cooldown, ``last_support_at`` in the FSM data. Other input gets the request again;
  ``/cancel`` leaves. With no operator configured the person gets the subscription contact.
- ``/terms``: a static text with the price (or "prices not set").

Logs hold no text, id or username.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import operator_languages
from scribe.clock import Clock, utcnow
from scribe.config import Settings, payments_available
from scribe.i18n import DEFAULT_LANG, Lang, t
from scribe.payments import repo
from scribe.payments.offers import price_text
from scribe.payments.states import PaySupportStates

MAX_SUPPORT_CHARS = 1000
SUPPORT_COOLDOWN = timedelta(minutes=10)
PAYMENT_WINDOW = timedelta(days=90)


def _contact_text(settings: Settings, lang: str) -> str:
    if settings.subscription_contact:
        return t("pay.support_contact", lang, contact=settings.subscription_contact)
    return t("pay.support_contact_none", lang)


async def paysupport_command(
    message: Message, state: FSMContext, settings: Settings, lang: Lang, clock: Clock = utcnow
) -> None:
    if not settings.operator_telegram_ids:
        await state.clear()
        await message.answer(_contact_text(settings, lang))
        return
    last = (await state.get_data()).get("last_support_at")
    try:
        recent = isinstance(last, str) and clock() - datetime.fromisoformat(last) < SUPPORT_COOLDOWN
    except ValueError:
        recent = False
    if recent:
        await message.answer(t("pay.support_wait", lang))
        return
    await state.set_state(PaySupportStates.describe)
    price = (
        price_text(settings.stars_price)
        if payments_available(settings) and settings.stars_price
        else ""
    )
    price_line = t("pay.support_price_line", lang, price=price) if price else ""
    await message.answer(t("pay.support_intro", lang, price_line=price_line))


async def support_text_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    sender = message.from_user
    text = message.text or ""
    if sender is None or not 1 <= len(text) <= MAX_SUPPORT_CHARS:
        await message.answer(t("pay.support_again", lang))
        return
    now = clock()
    async with session_factory() as session:
        payments = await repo.payments_for(session, sender.id, since=now - PAYMENT_WINDOW)
    lines = [f"{p.charge_id} {p.paid_at:%Y-%m-%d} {p.amount_stars}" for p in payments]
    languages = await operator_languages(session_factory, settings.operator_telegram_ids)
    sent = 0
    for operator_id in settings.operator_telegram_ids:
        op_lang = languages.get(operator_id, DEFAULT_LANG)
        who = (
            f"@{sender.username}"
            if sender.username
            else t("access.apply_operator_no_username", op_lang)
        )
        body = t(
            "pay.support_operator",
            op_lang,
            who=who,
            telegram_id=sender.id,
            payments="\n".join(lines) if lines else t("pay.support_none", op_lang),
            text=text,
        )
        try:
            await bot.send_message(operator_id, body, parse_mode=None)  # plain text, never parsed
            sent += 1
        except Exception:  # a blocked operator never stops the others
            continue
    await state.clear()
    if sent == 0:
        await message.answer(_contact_text(settings, lang))
        return
    await state.update_data(last_support_at=now.isoformat())  # the 10-minute cooldown
    await message.answer(t("pay.support_sent", lang))


async def support_other_input(message: Message, lang: Lang) -> None:
    """A photo, a sticker, another command or text that is too long: ask again."""
    await message.answer(t("pay.support_again", lang))


async def support_cancel(message: Message, state: FSMContext, lang: Lang) -> None:
    data = await state.get_data()
    await state.clear()
    if "last_support_at" in data:
        await state.update_data(last_support_at=data["last_support_at"])
    await message.answer(t("pay.support_cancelled", lang))


async def terms_command(message: Message, settings: Settings, lang: Lang) -> None:
    if payments_available(settings) and settings.stars_price is not None:
        await message.answer(t("pay.terms", lang, price=price_text(settings.stars_price)))
    else:
        await message.answer(t("pay.terms_no_price", lang))


def create_router() -> Router:
    """Registered next to the payments router, ahead of the flows that take free text."""
    router = Router(name="payments_support")
    router.message.register(terms_command, Command("terms"))
    in_flow = StateFilter(PaySupportStates.describe)
    router.message.register(support_cancel, Command("cancel"), in_flow)
    router.message.register(
        paysupport_command, Command("paysupport"), StateFilter(None, PaySupportStates.describe)
    )
    router.message.register(support_text_input, in_flow, F.text & ~F.text.startswith("/"))
    router.message.register(support_other_input, in_flow)
    return router


__all__ = ["create_router", "paysupport_command", "terms_command"]
