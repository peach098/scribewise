"""Auto-send confirmations in the control chat (specs/014, contracts/control-chat.md §3).

``autoconfirm:<work_chat_id>:manual|offer|no`` answers the confirmation that precedes turning
auto-send on for one chat. ``manual`` is the confirmation shown by ``chatauto:<id>:on`` in
``/chats`` (``settings.py``), ``offer`` the one shown by the offer's "Turn on" (the advisor,
specs/014 US2); ``no`` cancels. Nothing is changed before the user presses the yes button, and
the callback names exactly one chat, so a single press can never turn auto-send on for more than
one chat (FR-002). The chat must be active (``autosend.chat_not_active`` otherwise); a stale or
repeated confirmation changes nothing more than the first.

The router carries :class:`OwnershipMiddleware`, so a confirmation naming another user's chat is
refused before the handler runs.
"""

from __future__ import annotations

import uuid

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.db.models import AutoSendSource, User, WorkChat
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, LocalizedError, t
from scribe.logging import get_logger
from scribe.sending.auto_send import set_chat_auto_send, window_label
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
    async with session_factory() as session:
        try:
            await set_chat_auto_send(
                session,
                user_id=user.id,
                work_chat_id=work_chat_id,
                on=True,
                source=AutoSendSource(data.extra),
                now=clock(),
            )
        except LocalizedError as exc:  # not an active chat (disabled or excluded since asked)
            await session.rollback()
            await callback.answer(exc.text(lang))
            return
        await session.commit()
        chat = await chats_repo.get_work_chat(session, user.id, work_chat_id)
        current = await users_repo.get_user(session, user.id) or user
    await callback.answer()
    title = (chat.title if chat is not None else "") or t("common.chat", current.language)
    await _show(callback, bot, current, t("autosend.enabled", current.language, chat=title))


async def _show(callback: CallbackQuery, bot: Bot, user: User, text: str) -> None:
    """Replace the confirmation (and its buttons) with ``text``; a new message when it cannot be
    edited."""
    message = callback.message
    if message is not None:
        try:
            await bot.edit_message_text(
                text=text, chat_id=message.chat.id, message_id=message.message_id
            )
            return
        except Exception as exc:  # too old, deleted, ...
            log.info("auto_send_confirm_edit_failed", error=type(exc).__name__)
    await bot.send_message(user.telegram_control_chat_id, text)


def create_router() -> Router:
    router = Router(name="auto_send")
    router.callback_query.middleware(OwnershipMiddleware())
    router.callback_query.register(on_autoconfirm, F.data.regexp(r"^autoconfirm:"))
    return router
