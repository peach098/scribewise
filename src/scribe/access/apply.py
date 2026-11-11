"""Beta applications in the control bot (specs/005-beta-subscription-access US5, research Q12,
Q13; contracts/control-chat.md "Beta application flow").

- The "no access" reply (:func:`no_access_reply`, used by the gate and the send-time refusal)
  offers **Join the beta** (``apply:start``) and **Subscription** (``apply:subinfo``), unless
  the person has a pending application or one rejected less than 30 days ago: then it says so
  instead, with no buttons.
- **Join the beta** asks for the use case (``ApplyStates.use_case``). A text of 10–1000
  characters is kept in the FSM data only and shown back with the consent note and **Send
  application** / **Edit** / **Cancel** (``ApplyStates.review``). **Send application** stores
  the application (consent given now), confirms, and sends it to every operator as plain text
  with **Approve** / **Reject** (``betaapp:approve:<id>``, ``betaapp:reject:<id>``).
- A plugin may replace the two buttons (:data:`OfferProvider`, docs/plugins.md): it returns
  the button rows of the "no access" reply for the person; the pending or recently-rejected
  notice is unchanged.
- **Subscription** sends a placeholder (plus the subscription contact, if set) and stores
  nothing.
- The decision buttons answer only operators; anyone else gets a silent answer. The first
  decision wins: :func:`notify_decision` (shared with ``scribe.admin``) edits every operator
  copy to show it and tells the applicant.

An applicant has no ``users`` row (FR-002): the handlers work from ``from_user`` and the
``lang`` the user-context middleware derived from the Telegram language. The gate lets
``apply:`` buttons and the messages of this flow through for people without access, without
counting them against the refusal cooldown. Logs hold no ids, usernames or use cases.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import applications, policy, repo
from scribe.access.applications import ApplyBlock, BlockKind, Decision
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import BetaApplication, BetaApplicationStatus, User
from scribe.i18n import DEFAULT_LANG, Lang, format_day_month_year, normalize_lang, t
from scribe.logging import get_logger
from scribe.telegram.control_bot.render import (
    InvalidCallbackData,
    button,
    format_callback,
    keyboard,
    parse_callback,
)

log = get_logger("scribe.access")

APPLY_PREFIX = "apply:"  # the applicant's buttons; the gate lets them through
DECISION_ACTION = "betaapp"


# A plugin's buttons for someone without access (or at a usage limit): (Telegram id, language,
# session) -> button rows. An empty list means no buttons.
OfferProvider = Callable[[int, str, AsyncSession], Awaitable[list[list[InlineKeyboardButton]]]]


class ApplyStates(StatesGroup):
    use_case = State()
    review = State()


# --- the "no access" reply -------------------------------------------------------------------


def offer_keyboard(lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button(t("btn.apply_join", lang), "apply:start"),
                button(t("btn.apply_subscription", lang), "apply:subinfo"),
            ]
        ]
    )


def _day(moment: datetime, lang: str) -> str:
    return format_day_month_year(moment.astimezone(UTC).date(), lang)


def block_text(block: ApplyBlock, lang: str) -> str:
    if block.kind is BlockKind.pending or block.until is None:
        return t("access.apply_pending", lang)
    return t("access.apply_rejected_recently", lang, date=_day(block.until, lang))


async def offer_rows(
    offer: OfferProvider | None, session: AsyncSession, telegram_id: int, lang: str
) -> InlineKeyboardMarkup | None:
    """The plugin's buttons for ``telegram_id`` (None: no provider or no rows)."""
    if offer is None:
        return None
    rows = await offer(telegram_id, lang, session)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def no_access_reply(
    session: AsyncSession,
    telegram_id: int,
    *,
    has_account: bool,
    lang: str,
    now: datetime,
    offer: OfferProvider | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """``access.refused`` (no account) or ``access.ended_reply`` with the two buttons (or the
    ``offer`` provider's rows instead); the pending or recently-rejected notice without
    buttons instead when applying is blocked."""
    block = await applications.can_apply(session, telegram_id, now)
    if block is not None:
        return block_text(block, lang), None
    key = "access.ended_reply" if has_account else "access.refused"
    if offer is not None:
        return t(key, lang), await offer_rows(offer, session, telegram_id, lang)
    return t(key, lang), offer_keyboard(lang)


# --- texts for operators ---------------------------------------------------------------------


def operator_text(application: BetaApplication, lang: str) -> str:
    who = (
        f"@{application.username}"
        if application.username
        else t("access.apply_operator_no_username", lang)
    )
    text = t(
        "access.apply_operator",
        lang,
        who=who,
        telegram_id=application.telegram_id,
        language=application.language,
        use_case=application.use_case or "",
    )
    if application.status == BetaApplicationStatus.pending or application.decided_at is None:
        return text
    by = (
        str(application.decided_by)
        if application.decided_by is not None
        else t("access.apply_operator_by_cli", lang)
    )
    when = f"{application.decided_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    key = (
        "access.apply_operator_approved"
        if application.status == BetaApplicationStatus.approved
        else "access.apply_operator_rejected"
    )
    return f"{text}\n\n{t(key, lang, by=by, when=when)}"


def decision_keyboard(application_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button(
                    t("btn.application_approve", lang),
                    format_callback(DECISION_ACTION, "approve", application_id),
                ),
                button(
                    t("btn.reject", lang),
                    format_callback(DECISION_ACTION, "reject", application_id),
                ),
            ]
        ]
    )


async def operator_languages(
    session_factory: async_sessionmaker[AsyncSession], ids: Iterable[int]
) -> dict[int, Lang]:
    """Each operator's stored language (``users.language``); English without an account."""
    wanted = list(set(ids))
    if not wanted:
        return {}
    async with session_factory() as session:
        rows = await session.execute(
            select(User.telegram_control_chat_id, User.language).where(
                User.telegram_control_chat_id.in_(wanted)
            )
        )
        stored = {chat_id: normalize_lang(language) for chat_id, language in rows}
    return {chat_id: stored.get(chat_id, DEFAULT_LANG) for chat_id in wanted}


async def send_to_operators(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    application: BetaApplication,
) -> int:
    """Send the application to every operator and remember the copies; how many were sent.
    With no operator configured it waits for ``beta requests`` (Edge Cases)."""
    langs = await operator_languages(session_factory, settings.operator_telegram_ids)
    sent: list[list[int]] = []
    for chat_id in settings.operator_telegram_ids:
        lang = langs.get(chat_id, DEFAULT_LANG)
        try:
            message = await bot.send_message(
                chat_id,
                operator_text(application, lang),
                reply_markup=decision_keyboard(application.id, lang),
                parse_mode=None,  # plain text: the use case is never interpreted
            )
        except Exception as exc:  # blocked bot, network: the other operators still get it
            log.warning("application_operator_send_failed", error=type(exc).__name__)
            continue
        sent.append([chat_id, message.message_id])
    if sent:
        async with session_factory() as session:
            await applications.add_operator_messages(session, application.id, sent)
            await session.commit()
        application.operator_messages = [*application.operator_messages, *sent]
    return len(sent)


async def _edit_operator_copy(
    bot: Bot, application: BetaApplication, chat_id: int, message_id: int, lang: str
) -> None:
    try:
        await bot.edit_message_text(
            operator_text(application, lang),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
            parse_mode=None,
        )
    except Exception as exc:  # deleted message, unchanged text: nothing else depends on it
        log.warning("application_operator_edit_failed", error=type(exc).__name__)


async def notify_decision(
    bot: Bot, session_factory: async_sessionmaker[AsyncSession], decision: Decision
) -> None:
    """After a committed decision: show it on every operator copy (no buttons) and, when it
    changed something, tell the applicant in the application's language."""
    application = decision.application
    copies = [
        (int(chat_id), int(message_id)) for chat_id, message_id in application.operator_messages
    ]
    langs = await operator_languages(session_factory, (chat_id for chat_id, _ in copies))
    for chat_id, message_id in copies:
        await _edit_operator_copy(
            bot, application, chat_id, message_id, langs.get(chat_id, DEFAULT_LANG)
        )
    if not decision.changed or application.decided_at is None:
        return
    lang = normalize_lang(application.language)
    if application.status == BetaApplicationStatus.approved:
        text = t("access.apply_approved", lang)
    else:
        until = application.decided_at + applications.REAPPLY_AFTER
        text = t("access.apply_rejected", lang, date=_day(until, lang))
    try:
        await bot.send_message(application.telegram_id, text)
    except Exception as exc:  # the applicant blocked the bot: the decision stands
        log.warning("application_decision_notice_failed", error=type(exc).__name__)


# --- the applicant's handlers ------------------------------------------------------------------


def _chat_id(callback: CallbackQuery) -> int:
    return callback.message.chat.id if callback.message is not None else callback.from_user.id


async def _ask(bot: Bot, chat_id: int, state: FSMContext, lang: str) -> None:
    await state.set_state(ApplyStates.use_case)
    await state.set_data({})
    await bot.send_message(
        chat_id,
        t("access.apply_ask", lang),
        reply_markup=keyboard([[button(t("btn.cancel", lang), "apply:cancel")]]),
    )


async def start_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    await callback.answer()
    if not settings.access_gate:
        return  # the buttons only come from gate replies
    sender = callback.from_user
    now = clock()
    async with session_factory() as session:
        status = await repo.access_status(session, settings, sender.id, sender.username, now=now)
        block = None if status.has_access else await applications.can_apply(session, sender.id, now)
        await session.commit()  # a username beta entry may have been linked
    chat_id = _chat_id(callback)
    if status.has_access:
        await bot.send_message(chat_id, t("access.apply_has_access", lang))
    elif block is not None:
        await bot.send_message(chat_id, block_text(block, lang))
    else:
        await _ask(bot, chat_id, state, lang)


async def use_case_input(message: Message, state: FSMContext, lang: Lang) -> None:
    text = (message.text or "").strip()
    if len(text) < applications.MIN_USE_CASE:
        await message.answer(t("access.apply_too_short", lang, n=applications.MIN_USE_CASE))
        return
    if len(text) > applications.MAX_USE_CASE:
        await message.answer(t("access.apply_too_long", lang, n=applications.MAX_USE_CASE))
        return
    await state.update_data(use_case=text)
    await state.set_state(ApplyStates.review)
    review = t("access.apply_review", lang, use_case=text)
    await message.answer(
        f"{review}\n\n{t('access.apply_consent', lang)}",
        parse_mode=None,  # the person's own text, shown back as is
        reply_markup=keyboard(
            [
                [button(t("btn.apply_send", lang), "apply:send")],
                [
                    button(t("btn.edit", lang), "apply:edit"),
                    button(t("btn.cancel", lang), "apply:cancel"),
                ],
            ]
        ),
    )


async def not_text_input(message: Message, lang: Lang) -> None:
    await message.answer(t("access.apply_text_only", lang))


async def edit_callback(
    callback: CallbackQuery, bot: Bot, state: FSMContext, settings: Settings, lang: Lang
) -> None:
    await callback.answer()
    if settings.access_gate and await state.get_state() in ApplyStates.__state_names__:
        await _ask(bot, _chat_id(callback), state, lang)


async def cancel_callback(callback: CallbackQuery, bot: Bot, state: FSMContext, lang: Lang) -> None:
    await callback.answer()
    if await state.get_state() in ApplyStates.__state_names__:
        await state.clear()
        await bot.send_message(_chat_id(callback), t("access.apply_cancelled", lang))


async def cancel_command(message: Message, state: FSMContext, lang: Lang) -> None:
    await state.clear()
    await message.answer(t("access.apply_cancelled", lang))


async def send_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    await callback.answer()
    use_case = (await state.get_data()).get("use_case")
    if (
        not settings.access_gate
        or await state.get_state() != ApplyStates.review.state
        or not isinstance(use_case, str)
    ):
        return  # a stale button: the flow ended
    sender = callback.from_user
    chat_id = _chat_id(callback)
    now = clock()
    async with session_factory() as session:
        block = await applications.can_apply(session, sender.id, now)
        application: BetaApplication | None = None
        if block is None:
            try:
                application = await applications.submit(
                    session,
                    telegram_id=sender.id,
                    username=sender.username,
                    use_case=use_case,
                    language=lang,
                    now=now,
                )
                await session.commit()
            except IntegrityError:  # sent twice at once: the other one is pending
                await session.rollback()
                block = ApplyBlock(BlockKind.pending)
    await state.clear()
    if application is None:
        await bot.send_message(chat_id, block_text(block or ApplyBlock(BlockKind.pending), lang))
        return
    log.info("application_submitted", operators=len(settings.operator_telegram_ids))
    await bot.send_message(chat_id, t("access.apply_sent", lang))
    await send_to_operators(bot, session_factory, settings, application)


async def subscription_callback(
    callback: CallbackQuery, bot: Bot, settings: Settings, lang: Lang
) -> None:
    await callback.answer()
    if not settings.access_gate:
        return
    text = t("access.subscription_placeholder", lang)
    if settings.subscription_contact:
        line = t(
            "access.subscription_line", lang, subscription_contact=settings.subscription_contact
        )
        text = f"{text}\n{line}"
    await bot.send_message(_chat_id(callback), text)


# --- the operator's decision -------------------------------------------------------------------


async def _from_operator(callback: CallbackQuery, settings: Settings) -> bool:
    return policy.is_operator(callback.from_user.id, settings)


async def decision_callback(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    try:
        parsed = parse_callback(callback.data)
        application_id = uuid.UUID(parsed.extra or "")
    except (InvalidCallbackData, ValueError):
        await callback.answer()
        return
    if parsed.entity_id not in ("approve", "reject"):
        await callback.answer()
        return
    async with session_factory() as session:
        decision = await applications.decide(
            session,
            application_id,
            approve=parsed.entity_id == "approve",
            operator_id=callback.from_user.id,
            now=clock(),
        )
        await session.commit()
    if decision is None:  # purged, or never existed
        await callback.answer(t("alert.not_found", lang))
        return
    await callback.answer()
    if decision.changed:
        log.info("application_decided", status=decision.application.status.value)
        await notify_decision(bot, session_factory, decision)
    elif callback.message is not None:  # decided already: only refresh this copy
        await _edit_operator_copy(
            bot, decision.application, callback.message.chat.id, callback.message.message_id, lang
        )


async def ignore_callback(callback: CallbackQuery) -> None:
    """A decision button pressed by someone who is not an operator."""
    await callback.answer()


def create_router() -> Router:
    """Registered before the setup and settings routers, so the text of this flow is never
    taken by their state-bound handlers."""
    router = Router(name="apply")
    router.callback_query.register(start_callback, F.data == "apply:start")
    router.callback_query.register(subscription_callback, F.data == "apply:subinfo")
    router.callback_query.register(edit_callback, F.data == "apply:edit")
    router.callback_query.register(cancel_callback, F.data == "apply:cancel")
    router.callback_query.register(send_callback, F.data == "apply:send")
    router.callback_query.register(
        decision_callback, F.data.startswith(f"{DECISION_ACTION}:"), _from_operator
    )
    router.callback_query.register(ignore_callback, F.data.startswith(f"{DECISION_ACTION}:"))
    router.callback_query.register(ignore_callback, F.data.startswith(APPLY_PREFIX))
    in_flow = StateFilter(ApplyStates)
    router.message.register(cancel_command, Command("cancel"), in_flow)
    router.message.register(use_case_input, in_flow, F.text & ~F.text.startswith("/"))
    router.message.register(not_text_input, in_flow, ~F.text)
    return router


__all__ = [
    "APPLY_PREFIX",
    "ApplyStates",
    "OfferProvider",
    "block_text",
    "create_router",
    "no_access_reply",
    "notify_decision",
    "offer_keyboard",
    "offer_rows",
    "operator_languages",
    "operator_text",
    "send_to_operators",
]
