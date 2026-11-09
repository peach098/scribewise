"""Payment handlers of the control bot (specs/015-telegram-stars-payments).

- ``pre_checkout_query``: the last point where a charge can still be refused (research P3).
  :func:`scribe.payments.checkout.precheckout_decision` runs under a 5 s timeout; any error or
  timeout declines (nothing is charged). The reason is a localized ``error_message``.
- ``successful_payment``: :func:`scribe.payments.credit.credit_payment` in one transaction,
  committed, then ``pay.thanks`` for a first payment only (a renewal is silent, FR-006). Access
  is granted by the new ``subscriptions`` row; ``access_reconcile`` lifts a suspension.
- ``refunded_payment``: a refund made by Telegram or the operator is applied once
  (:func:`scribe.payments.credit.apply_refund`) and the person gets ``pay.refunded``.
- ``pay:offer``: the same screen as the **Subscription** button (``apply:subinfo``).
- ``subscription`` updates (``canceled``, ``active``, ``failed``): changes made inside Telegram
  are followed by :func:`scribe.payments.lifecycle.on_subscription_update` (research P5).
- ``pay:cancel`` (asks to confirm), ``pay:cancel:yes``, ``pay:cancel:no`` and ``pay:resume``:
  each acts only on the presser's own running subscription, found by their Telegram id (a payer
  may have no account); anything else answers ``pay.stale`` with the current view. Telegram is
  called first; on :class:`StarsApiError` the person gets ``pay.try_again`` and nothing changes.
- ``/account`` (:func:`account_handler`, registered with the settings router so it answers in the
  same states): the access status and the Stars subscription, with the buttons that fit.

These updates reach their handlers for anyone, with or without an account: ``pre_checkout_query``
has no gate, and the access gate lets payment service messages and ``pay:`` buttons through
(research P7). Logs hold kinds, reasons and (for credited payments) amounts only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.types import BotSubscriptionUpdated, CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import subscription_callback
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import Payment, PaymentSource, StarsCancelSource, StarsSubState, User
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, language_for_code, normalize_lang, t
from scribe.logging import get_logger
from scribe.payments import checkout, credit, lifecycle, repo
from scribe.payments.offers import price_text
from scribe.payments.telegram import StarsApi, StarsApiError
from scribe.payments.views import account_view, day
from scribe.telegram.control_bot.render import button, keyboard

log = get_logger("scribe.payments")

PRECHECKOUT_TIMEOUT_S = 5  # Telegram allows 10 s; stay well inside it (research P3)


# --- pre-checkout -------------------------------------------------------------------------------


async def _decide(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    query: PreCheckoutQuery,
    now: datetime,
) -> checkout.Accept | checkout.Decline:
    async with session_factory() as session:
        return await checkout.precheckout_decision(
            session,
            settings,
            payer_id=query.from_user.id,
            payload=query.invoice_payload,
            currency=query.currency,
            total_amount=query.total_amount,
            now=now,
        )


async def pre_checkout_handler(
    query: PreCheckoutQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: Clock = utcnow,
) -> None:
    """Answer within Telegram's limit: accept, or decline with a reason the person can read."""
    try:
        decision = await asyncio.wait_for(
            _decide(session_factory, settings, query, clock()), PRECHECKOUT_TIMEOUT_S
        )
    except Exception as exc:  # a timeout or a DB error declines; nothing is charged
        log.error("stars_precheckout", result="error", error=type(exc).__name__)
        decision = checkout.Decline(checkout.DeclineReason.error)
    if isinstance(decision, checkout.Accept):
        log.info("stars_precheckout", result="accepted")
        await query.answer(ok=True)
        return
    log.info("stars_precheckout", result="declined", reason=decision.reason.value)
    lang = await _payer_language(session_factory, query)
    await query.answer(ok=False, error_message=t(f"pay.declined_{decision.reason.value}", lang))


async def _payer_language(
    session_factory: async_sessionmaker[AsyncSession], query: PreCheckoutQuery
) -> Lang:
    """The stored language, else the default for the payer's Telegram language."""
    try:
        async with session_factory() as session:
            user = await users_repo.get_by_control_chat(session, query.from_user.id)
    except Exception:  # the decline must still be answered
        user = None
    if user is not None:
        return normalize_lang(user.language)
    return language_for_code(query.from_user.language_code)


# --- successful payment -------------------------------------------------------------------------


async def successful_payment_handler(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    user: User | None,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    payment = message.successful_payment
    sender = message.from_user
    if payment is None or sender is None or payment.currency != checkout.STARS_CURRENCY:
        return
    expires_at = (
        datetime.fromtimestamp(payment.subscription_expiration_date, UTC)
        if payment.subscription_expiration_date is not None
        else None
    )
    async with session_factory() as session:
        result = await credit.credit_payment(
            session,
            payer_id=sender.id,
            charge_id=payment.telegram_payment_charge_id,
            amount=payment.total_amount,
            payload=payment.invoice_payload,
            paid_at=message.date,
            expires_at=expires_at,
            is_first=bool(payment.is_first_recurring),
            via=PaymentSource.update,
            now=clock(),
        )
        await session.commit()
    if isinstance(result, credit.Credited) and result.first:
        await message.answer(thanks_text(result.stars.period_end, payment.total_amount, user, lang))


async def refunded_payment_handler(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    user: User | None,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    refund = message.refunded_payment
    if refund is None:
        return
    async with session_factory() as session:
        result = await credit.apply_refund(
            session, charge_id=refund.telegram_payment_charge_id, by="telegram", now=clock()
        )
        await session.commit()
    if isinstance(result, credit.Refunded):
        await message.answer(refunded_text(result.payment, user, lang))


def refunded_text(payment: Payment, user: User | None, lang: str) -> str:
    """``pay.refunded`` for a refunded payment, dated in the person's time zone."""
    tz = user.timezone if user is not None else "UTC"
    return t(
        "pay.refunded",
        lang,
        price=price_text(payment.amount_stars),
        date=day(payment.paid_at, tz, lang),
    )


def thanks_text(period_end: datetime, amount: int, user: User | None, lang: str) -> str:
    """``pay.thanks`` (+ ``pay.thanks_start`` for someone with no account yet)."""
    tz = user.timezone if user is not None else "UTC"
    text = t("pay.thanks", lang, date=day(period_end, tz, lang), price=price_text(amount))
    if user is None:
        text = f"{text}\n{t('pay.thanks_start', lang)}"
    return text


# --- changes made inside Telegram ----------------------------------------------------------------


async def subscription_update_handler(
    update: BotSubscriptionUpdated,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock = utcnow,
) -> None:
    async with session_factory() as session:
        await lifecycle.on_subscription_update(
            session,
            payer_id=update.user.id,
            payload=update.invoice_payload,
            state=update.state,
            now=clock(),
        )
        await session.commit()


# --- /account and the cancel / resume buttons ---------------------------------------------


def _chat_id(callback: CallbackQuery) -> int:
    return callback.message.chat.id if callback.message is not None else callback.from_user.id


async def account_handler(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: User | None,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    sender = message.from_user
    if sender is None:
        return
    async with session_factory() as session:
        text, markup = await account_view(
            session, settings, sender.id, user=user, lang=lang, now=clock()
        )
    await message.answer(text, reply_markup=markup)


async def _stale(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: User | None,
    lang: Lang,
    now: datetime,
) -> None:
    """The button no longer fits: say so and show the current account."""
    async with session_factory() as session:
        text, markup = await account_view(
            session, settings, callback.from_user.id, user=user, lang=lang, now=now
        )
    await bot.send_message(
        _chat_id(callback), f"{t('pay.stale', lang)}\n\n{text}", reply_markup=markup
    )


async def cancel_callback(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: User | None,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``pay:cancel``: ask first (the confirmation names the date access lasts until)."""
    await callback.answer()
    now = clock()
    async with session_factory() as session:
        stars = await repo.running_for(session, callback.from_user.id)
    if stars is None or stars.state is not StarsSubState.active:
        await _stale(callback, bot, session_factory, settings, user, lang, now)
        return
    tz = user.timezone if user is not None else "UTC"
    await bot.send_message(
        _chat_id(callback),
        t("pay.cancel_confirm", lang, date=day(stars.period_end, tz, lang)),
        reply_markup=keyboard(
            [
                [
                    button(t("btn.pay_cancel_yes", lang), "pay:cancel:yes"),
                    button(t("btn.pay_cancel_no", lang), "pay:cancel:no"),
                ]
            ]
        ),
    )


async def cancel_yes_callback(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: User | None,
    lang: Lang,
    stars_api: StarsApi | None = None,
    clock: Clock = utcnow,
) -> None:
    await callback.answer()
    now = clock()
    tz = user.timezone if user is not None else "UTC"
    async with session_factory() as session:
        try:
            if stars_api is None:
                raise StarsApiError("payments are not wired")
            result = await lifecycle.cancel_renewal(
                session,
                stars_api,
                telegram_id=callback.from_user.id,
                by=StarsCancelSource.user_bot,
                now=now,
            )
        except StarsApiError as exc:
            await session.rollback()
            log.warning("stars_subscription_state", result="error", action="cancel", error=str(exc))
            await bot.send_message(_chat_id(callback), t("pay.try_again", lang))
            return
        await session.commit()
    if isinstance(result, lifecycle.Canceled):
        text = t("pay.canceled", lang, date=day(result.stars.period_end, tz, lang))
        await bot.send_message(_chat_id(callback), text)
    else:
        await _stale(callback, bot, session_factory, settings, user, lang, now)


async def cancel_no_callback(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: User | None,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """**Keep**: show the account again."""
    await callback.answer()
    async with session_factory() as session:
        text, markup = await account_view(
            session, settings, callback.from_user.id, user=user, lang=lang, now=clock()
        )
    await bot.send_message(_chat_id(callback), text, reply_markup=markup)


async def resume_callback(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: User | None,
    lang: Lang,
    stars_api: StarsApi | None = None,
    clock: Clock = utcnow,
) -> None:
    await callback.answer()
    now = clock()
    tz = user.timezone if user is not None else "UTC"
    async with session_factory() as session:
        try:
            if stars_api is None:
                raise StarsApiError("payments are not wired")
            result = await lifecycle.resume_renewal(
                session, stars_api, telegram_id=callback.from_user.id, now=now
            )
        except StarsApiError as exc:
            await session.rollback()
            log.warning("stars_subscription_state", result="error", action="resume", error=str(exc))
            await bot.send_message(_chat_id(callback), t("pay.try_again", lang))
            return
        await session.commit()
    if isinstance(result, lifecycle.Resumed):
        text = t(
            "pay.resumed",
            lang,
            date=day(result.stars.period_end, tz, lang),
            price=price_text(result.stars.price_stars),
        )
        await bot.send_message(_chat_id(callback), text)
    else:
        await _stale(callback, bot, session_factory, settings, user, lang, now)


def create_router() -> Router:
    """Registered right after the application router (before the setup and settings routers).
    ``/account`` is registered by the settings router (:func:`account_handler`)."""
    router = Router(name="payments")
    router.pre_checkout_query.register(pre_checkout_handler)
    router.subscription.register(subscription_update_handler)
    router.message.register(successful_payment_handler, F.successful_payment)
    router.message.register(refunded_payment_handler, F.refunded_payment)
    router.callback_query.register(subscription_callback, F.data == "pay:offer")
    router.callback_query.register(cancel_callback, F.data == "pay:cancel")
    router.callback_query.register(cancel_yes_callback, F.data == "pay:cancel:yes")
    router.callback_query.register(cancel_no_callback, F.data == "pay:cancel:no")
    router.callback_query.register(resume_callback, F.data == "pay:resume")
    return router


__all__ = [
    "account_handler",
    "create_router",
    "pre_checkout_handler",
    "refunded_payment_handler",
    "refunded_text",
    "subscription_update_handler",
    "successful_payment_handler",
    "thanks_text",
]
