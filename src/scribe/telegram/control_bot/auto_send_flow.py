"""Auto-send confirmations in the control chat (specs/014, contracts/control-chat.md §3).

``autooffer:<work_chat_id>:on|later|never`` answers the advisor's offer (``sending/auto_send.py``,
FR-015..FR-017): ``on`` only asks for the same confirmation (``autoconfirm:<id>:offer``),
``later`` snoozes the offer for 30 days, ``never`` stops offering for the chat.
``autowarn:<work_chat_id>:keep`` dismisses the suggestion to turn auto-send off (the other button
is ``chatauto:<id>:off``).

``autoconfirm:<work_chat_id>:manual|offer|no`` answers the confirmation that precedes turning
auto-send on for one chat. ``manual`` is the confirmation shown by ``chatauto:<id>:on`` in
``/chats`` (``settings.py``), ``offer`` the one shown by the offer's "Turn on" (the advisor,
specs/014 US2); ``no`` cancels. Nothing is changed before the user presses the yes button, and
the callback names exactly one chat, so a single press can never turn auto-send on for more than
one chat (FR-002). The chat must be active (``autosend.chat_not_active`` otherwise); a repeated
confirmation changes nothing more than the first, and one shown before the setting last changed
(``work_chats.auto_send_changed_at`` later than the message date) is refused
(``autosend.confirm_stale``) so an old button cannot undo the user's turning auto-send off.

The router carries :class:`OwnershipMiddleware`, so a confirmation naming another user's chat is
refused before the handler runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.db.models import ActivityKind, AutoSendOfferState, AutoSendSource, User, WorkChat
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, LocalizedError, t
from scribe.logging import get_logger
from scribe.sending.auto_send import OFFER_SNOOZE, set_chat_auto_send, window_label
from scribe.telegram.control_bot.ownership import OwnershipMiddleware
from scribe.telegram.control_bot.render import (
    InvalidCallbackData,
    button,
    format_callback,
    keyboard,
    parse_callback,
)

log = get_logger("scribe.control_bot")

NO = "no"
ON, LATER, NEVER, KEEP = "on", "later", "never", "keep"


def confirm_text(chat: WorkChat, user: User) -> str:
    """The confirmation of turning auto-send on for ``chat`` (``autosend.confirm``)."""
    lang = user.language
    return t(
        "autosend.confirm",
        lang,
        chat=chat.title or t("common.chat", lang),
        window=window_label(user.auto_send_window_s, lang),
    )


def confirm_keyboard(
    work_chat_id: uuid.UUID, source: AutoSendSource, lang: str
) -> InlineKeyboardMarkup:
    """Yes carries the source (``manual`` from /chats, ``offer`` from the offer); Cancel is
    ``autoconfirm:<id>:no``."""
    return keyboard(
        [
            [
                button(
                    t("btn.autosend_confirm_yes", lang),
                    format_callback("autoconfirm", work_chat_id, source.value),
                ),
                button(t("btn.cancel", lang), format_callback("autoconfirm", work_chat_id, NO)),
            ]
        ]
    )


async def on_autoconfirm(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        data = parse_callback(callback.data)
        work_chat_id = uuid.UUID(data.entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("alert.chat_not_found", lang))
        return
    if data.extra == NO:
        await callback.answer()
        await _show(callback, bot, user, t("autosend.confirm_cancelled", lang))
        return
    if data.extra not in {source.value for source in AutoSendSource}:
        await callback.answer(t("alert.chat_not_found", lang))
        return
    source = AutoSendSource(data.extra)
    async with session_factory() as session:
        before = await chats_repo.get_work_chat(session, user.id, work_chat_id)
        was_on = before is not None and before.auto_send
        if (
            not was_on
            and before is not None
            and _confirmation_is_stale(callback, before.auto_send_changed_at)
        ):
            await callback.answer(t("autosend.confirm_stale", lang), show_alert=True)
            await _drop_buttons(callback, bot)
            return
        try:
            await set_chat_auto_send(
                session,
                user_id=user.id,
                work_chat_id=work_chat_id,
                on=True,
                source=source,
                now=clock(),
            )
        except LocalizedError as exc:  # not an active chat (disabled or excluded since asked)
            await session.rollback()
            await callback.answer(exc.text(lang))
            return
        if source == AutoSendSource.offer and not was_on:
            await _record_answer(session, user.id, work_chat_id, ON, clock())
        await session.commit()
        chat = await chats_repo.get_work_chat(session, user.id, work_chat_id)
        current = await users_repo.get_user(session, user.id) or user
    await callback.answer()
    title = (chat.title if chat is not None else "") or t("common.chat", current.language)
    await _show(callback, bot, current, t("autosend.enabled", current.language, chat=title))


def _confirmation_is_stale(callback: CallbackQuery, changed_at: datetime | None) -> bool:
    """The setting changed after the confirmation was shown (the user turned auto-send off since):
    the old "Yes, turn on" must not turn it back on. An inaccessible (old) message has date 0."""
    message = callback.message
    if message is None or changed_at is None:
        return False
    shown_at = message.date
    return not isinstance(shown_at, datetime) or changed_at > shown_at


async def _drop_buttons(callback: CallbackQuery, bot: Bot) -> None:
    message = callback.message
    if message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=message.message_id, reply_markup=None
        )
    except Exception as exc:  # too old, deleted, ...
        log.info("auto_send_confirm_edit_failed", error=type(exc).__name__)


async def _show(
    callback: CallbackQuery,
    bot: Bot,
    user: User,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Replace the message (and its buttons) with ``text`` and ``markup``; a new message when it
    cannot be edited."""
    message = callback.message
    if message is not None:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup,
            )
            return
        except Exception as exc:  # too old, deleted, ...
            log.info("auto_send_confirm_edit_failed", error=type(exc).__name__)
    await bot.send_message(user.telegram_control_chat_id, text, reply_markup=markup)


async def _record_answer(
    session: AsyncSession, user_id: uuid.UUID, work_chat_id: uuid.UUID, answer: str, now: datetime
) -> None:
    await activity_repo.record(
        session,
        user_id,
        ActivityKind.auto_send_offer_answered,
        at=now,
        meta={"work_chat_id": str(work_chat_id), "answer": answer},
    )
    log.info(
        "auto_send_offer_answered",
        user_id=str(user_id),
        work_chat_id=str(work_chat_id),
        answer=answer,
    )


async def on_autooffer(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``autooffer:<work_chat_id>:on|later|never`` (contracts/control-chat.md §2). An answer to an
    old offer still applies; the figures are not recomputed."""
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        data = parse_callback(callback.data)
        work_chat_id = uuid.UUID(data.entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("alert.chat_not_found", lang))
        return
    if data.extra not in (ON, LATER, NEVER):
        await callback.answer()
        return
    now = clock()
    async with session_factory() as session:
        chat = (
            await session.execute(
                select(WorkChat)
                .where(WorkChat.user_id == user.id, WorkChat.id == work_chat_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if chat is None:
            await callback.answer(t("alert.chat_not_found", lang))
            return
        title = chat.title or t("common.chat", user.language)
        confirm: str | None = None
        if data.extra == ON:
            # Nothing changes yet: the same confirmation as in /chats; "Yes" records the answer.
            if not chat.enabled or chat.excluded:
                await callback.answer(t("autosend.chat_not_active", user.language))
                return
            confirm = None if chat.auto_send else confirm_text(chat, user)
        elif data.extra == LATER:
            chat.auto_send_offer_not_before = now + OFFER_SNOOZE
            await _record_answer(session, user.id, work_chat_id, LATER, now)
        else:
            chat.auto_send_offer = AutoSendOfferState.opted_out
            await _record_answer(session, user.id, work_chat_id, NEVER, now)
        await session.commit()
    await callback.answer()
    if data.extra == ON:
        if confirm is None:  # already on
            await _show(callback, bot, user, t("autosend.enabled", user.language, chat=title))
            return
        await _show(
            callback, bot, user, confirm,
            confirm_keyboard(work_chat_id, AutoSendSource.offer, user.language),
        )  # fmt: skip
        return
    key = "autosend.offer_later" if data.extra == LATER else "autosend.offer_never"
    await _show(callback, bot, user, t(key, user.language))


async def on_autowarn(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    lang: Lang,
) -> None:
    """``autowarn:<work_chat_id>:keep``: dismiss the suggestion to turn auto-send off; only the
    message changes (the other button is ``chatauto:<id>:off``)."""
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        data = parse_callback(callback.data)
    except InvalidCallbackData:
        await callback.answer()
        return
    await callback.answer()
    if data.extra == KEEP:
        await _show(callback, bot, user, t("autosend.warn_kept", user.language))


def create_router() -> Router:
    router = Router(name="auto_send")
    router.callback_query.middleware(OwnershipMiddleware())
    router.callback_query.register(on_autoconfirm, F.data.regexp(r"^autoconfirm:"))
    router.callback_query.register(on_autooffer, F.data.regexp(r"^autooffer:"))
    router.callback_query.register(on_autowarn, F.data.regexp(r"^autowarn:"))
    return router
