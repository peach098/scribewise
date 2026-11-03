"""``/skipped``: messages the reply gate skipped, "Draft anyway" and the skip switches
(specs/004-skip-closing-messages contracts/control-chat.md; FR-012 to FR-014).

- ``/skipped`` and ``skippage:<page>:<n>``: skipped turns of the last ``skipped_list_days``,
  newest first, with a preview decrypted only for this render in the owner's control chat.
- ``draftany:<turn_id>``: availability is checked again, then one guarded update moves the turn
  ``skipped -> waiting`` (due now) and records ``draft_anyway``. The scheduler drafts it on the
  next tick through the normal compose -> card -> approve path: nothing here can send
  (Principle I). At the user's daily LLM hard cap (issue #74) it is refused with an alert
  saying when drafting resumes.
- ``skipcfg:global:on|off``, ``skipchat:<work_chat_id>:off|on|inherit`` and
  ``skipchats:<page>`` (the per-chat view) change ``users.skip_closing`` /
  ``work_chats.skip_closing``; the gate reads them fresh for the next message. The same per-chat
  value is also on the ``/chats`` chat screen (``chatskip:`` in ``settings.py``).

The router is included in the settings router (``settings.py``), so the commands answer only
when no other flow is active and ``ownership.OwnershipMiddleware`` refuses a ``draftany`` /
``skipchat`` naming another user's turn or chat; the handlers load every row scoped to the
calling user as well. Texts are in the user's language (``scribe.i18n``). Message text is never
logged.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.context.history import turn_messages
from scribe.db.models import ActivityKind, ConversationTurn, TurnState, User
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import paused_alert, paused_now
from scribe.i18n import Lang, t
from scribe.logging import get_logger
from scribe.telegram.control_bot.render import (
    SKIPPED_PAGE_SIZE,
    CallbackData,
    ChatSkipView,
    InvalidCallbackData,
    SkippedItemView,
    chat_skip_settings_keyboard,
    is_chat_skip_settings,
    on_off,
    parse_callback,
    render_chat_skip_settings,
    render_skipped_list,
    skipped_keyboard,
    skipped_time,
)

log = get_logger("scribe.control_bot")

CHAT_VALUES: dict[str, bool | None] = {"on": True, "off": False, "inherit": None}
GLOBAL_VALUES = {"on": True, "off": False}

View = tuple[str, InlineKeyboardMarkup]


# --- views --------------------------------------------------------------------------------------


def _pages(total: int) -> int:
    return max(1, math.ceil(total / SKIPPED_PAGE_SIZE))


async def _item_view(
    session: AsyncSession, user: User, turn: ConversationTurn, *, drafting: bool
) -> SkippedItemView:
    chat = await chats_repo.get_work_chat(session, user.id, turn.work_chat_id)
    messages = await turn_messages(session, turn)
    # In a group the turn's contact is the group; the sender is whoever wrote the messages.
    sender_id = messages[-1].sender_contact_id if messages else None
    contact = await chats_repo.get_contact(session, user.id, sender_id or turn.contact_id)
    moment = messages[-1].sent_at if messages else turn.arrived_at
    return SkippedItemView(
        turn_id=turn.id,
        work_chat_id=turn.work_chat_id,
        sender=contact.display_name if contact is not None else "",
        chat_title=chat.title if chat is not None else "",
        local_time=skipped_time(moment, user.timezone, user.language),
        preview=" ".join(m.text for m in messages),
        reason=turn.skip_category.value if turn.skip_category is not None else "",
        available=await turns_repo.draft_anyway_available(session, user.id, turn),
        chat_override=chat.skip_closing if chat is not None else None,
        drafting=drafting,
    )


async def skipped_view(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    *,
    page: int,
    now: datetime,
    drafting: uuid.UUID | None = None,
    keep: ConversationTurn | None = None,
) -> View | None:
    """The ``/skipped`` page (``None`` if the user is gone). ``keep`` is a turn that just left
    ``skipped`` ("Draft anyway") and is still shown on its page, as drafting."""
    since = now - timedelta(days=settings.skipped_list_days)
    async with session_factory() as session:
        user = await users_repo.get_user(session, user_id)
        if user is None:
            return None
        total = await turns_repo.count_skipped(session, user.id, since=since)
        pages = _pages(total)
        page = min(max(page, 0), pages - 1)
        turns = await turns_repo.list_skipped(
            session, user.id, since=since, limit=SKIPPED_PAGE_SIZE, offset=page * SKIPPED_PAGE_SIZE
        )
        if keep is not None and all(turn.id != keep.id for turn in turns):
            turns = sorted([*turns, keep], key=lambda turn: turn.decided_at or now, reverse=True)
            total += 1
        items = [
            await _item_view(session, user, turn, drafting=turn.id == drafting) for turn in turns
        ]
        global_on, lang = user.skip_closing, user.language
    text = render_skipped_list(
        items,
        total=total,
        page=page,
        global_on=global_on,
        days=settings.skipped_list_days,
        lang=lang,
    )
    return text, skipped_keyboard(items, page=page, pages=pages, global_on=global_on, lang=lang)


async def chat_settings_view(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, *, page: int
) -> View | None:
    async with session_factory() as session:
        user = await users_repo.get_user(session, user_id)
        if user is None:
            return None
        chats = await chats_repo.list_work_chats(session, user.id, enabled_only=True)
        global_on, lang = user.skip_closing, user.language
    pages = _pages(len(chats))
    page = min(max(page, 0), pages - 1)
    shown = [
        ChatSkipView(work_chat_id=c.id, title=c.title, value=c.skip_closing)
        for c in chats[page * SKIPPED_PAGE_SIZE : (page + 1) * SKIPPED_PAGE_SIZE]
    ]
    return (
        render_chat_skip_settings(shown, page=page, pages=pages, global_on=global_on, lang=lang),
        chat_skip_settings_keyboard(shown, page=page, pages=pages, global_on=global_on, lang=lang),
    )


async def _edit(bot: Bot, callback: CallbackQuery, view: View | None) -> None:
    if view is None or callback.message is None:
        return
    text, markup = view
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=markup,
        )
    except Exception as exc:  # unchanged text, message too old to edit, ...
        log.warning("skipped_view_edit_failed", error=type(exc).__name__)


def _from_chat_settings(callback: CallbackQuery) -> bool:
    message = callback.message
    text = getattr(message, "text", None) if message is not None else None
    return isinstance(text, str) and is_chat_skip_settings(text)


async def _callback(callback: CallbackQuery, user: User | None, lang: str) -> CallbackData | None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return None
    try:
        return parse_callback(callback.data)
    except InvalidCallbackData:
        await callback.answer(t("alert.not_found", lang))
        return None


def _page_number(raw: str) -> int:
    try:
        return max(int(raw), 0)
    except ValueError:
        return 0


# --- handlers -----------------------------------------------------------------------------------


async def skipped_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    view = await skipped_view(session_factory, settings, user.id, page=0, now=clock())
    if view is not None:
        text, markup = view
        await message.answer(text, reply_markup=markup)


async def on_skip_page(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``skippage:<page>:<n>``: the list, edited in place."""
    data = await _callback(callback, user, lang)
    if data is None or user is None:
        return
    await callback.answer()
    page = _page_number(data.entity_id)
    view = await skipped_view(session_factory, settings, user.id, page=page, now=clock())
    await _edit(bot, callback, view)


async def on_draft_anyway(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``draftany:<turn_id>``: back to ``waiting`` (due now) for the normal draft flow."""
    data = await _callback(callback, user, lang)
    if data is None or user is None:
        return
    try:
        turn_id = uuid.UUID(data.entity_id)
    except ValueError:
        await callback.answer(t("alert.not_found", lang))
        return
    now = clock()
    if await paused_now(session_factory, settings, user.id, now):  # issue #74
        await callback.answer(paused_alert(user.timezone, now).render(lang), show_alert=True)
        return
    async with session_factory() as session:
        turn = await turns_repo.get_turn(session, user.id, turn_id)  # ownership check
        if turn is None:
            log.warning("skipped_callback_foreign_or_unknown_turn", user_id=str(user.id))
            await callback.answer(t("alert.not_found", lang))
            return
        available = turn.state == TurnState.skipped and await turns_repo.draft_anyway_available(
            session, user.id, turn
        )
        marked = available and await turns_repo.mark_draft_anyway(
            session, user.id, turn.id, now=now
        )
        if marked:
            await activity_repo.record(
                session,
                user.id,
                ActivityKind.draft_anyway,
                at=now,
                conversation_turn_id=turn.id,
                meta={
                    "skipped_at": turn.decided_at.isoformat() if turn.decided_at else None,
                    "category": turn.skip_category.value if turn.skip_category else None,
                },
            )
        await session.commit()
    if not marked:
        await callback.answer(t("alert.already_handled", lang))
        return
    log.info("draft_anyway_requested", user_id=str(user.id), turn_id=str(turn.id))
    await callback.answer(t("alert.drafting", lang))
    view = await skipped_view(
        session_factory, settings, user.id, page=0, now=now, drafting=turn.id, keep=turn
    )
    await _edit(bot, callback, view)


async def on_skip_global(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``skipcfg:global:on|off``: ``users.skip_closing``; the originating view re-rendered."""
    data = await _callback(callback, user, lang)
    if data is None or user is None:
        return
    value = GLOBAL_VALUES.get(data.extra or "")
    if data.entity_id != "global" or value is None:
        await callback.answer(t("alert.not_found", lang))
        return
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        if stored is not None:
            stored.skip_closing = value
        await session.commit()
    log.info("skip_setting_changed", user_id=str(user.id), scope="global", value=data.extra)
    await callback.answer(t("alert.skipping", lang, state=on_off(value, lang)))
    await _rerender(bot, callback, session_factory, settings, user.id, clock())


async def on_skip_chat(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``skipchat:<work_chat_id>:off|on|inherit``: ``work_chats.skip_closing``."""
    data = await _callback(callback, user, lang)
    if data is None or user is None:
        return
    choice = data.extra or ""
    try:
        work_chat_id = uuid.UUID(data.entity_id)
    except ValueError:
        work_chat_id = None
    if work_chat_id is None or choice not in CHAT_VALUES:
        await callback.answer(t("alert.not_found", lang))
        return
    async with session_factory() as session:
        chat = await chats_repo.get_work_chat(session, user.id, work_chat_id)  # ownership check
        if chat is None:
            log.warning("skipped_callback_foreign_or_unknown_chat", user_id=str(user.id))
            await callback.answer(t("alert.not_found", lang))
            return
        chat.skip_closing = CHAT_VALUES[choice]
        await session.commit()
    log.info(
        "skip_setting_changed",
        user_id=str(user.id),
        scope="chat",
        work_chat_id=str(work_chat_id),
        value=choice,
    )
    await callback.answer(t("alert.saved", lang))
    await _rerender(bot, callback, session_factory, settings, user.id, clock())


async def on_chat_settings(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    """``skipchats:<page>``: the per-chat view, edited in place."""
    data = await _callback(callback, user, lang)
    if data is None or user is None:
        return
    await callback.answer()
    page = _page_number(data.entity_id)
    await _edit(bot, callback, await chat_settings_view(session_factory, user.id, page=page))


async def _rerender(
    bot: Bot,
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    now: datetime,
) -> None:
    if _from_chat_settings(callback):
        view = await chat_settings_view(session_factory, user_id, page=0)
    else:
        view = await skipped_view(session_factory, settings, user_id, page=0, now=now)
    await _edit(bot, callback, view)


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


def create_router(idle: StateFilter) -> Router:
    """``idle``: the states in which control-chat commands answer (see ``settings.py``)."""
    router = Router(name="skipped")
    router.message.register(skipped_command, Command("skipped"), idle)
    router.callback_query.register(on_skip_page, _action("skippage"))
    router.callback_query.register(on_draft_anyway, _action("draftany"))
    router.callback_query.register(on_skip_global, _action("skipcfg"))
    router.callback_query.register(on_skip_chat, _action("skipchat"))
    router.callback_query.register(on_chat_settings, _action("skipchats"))
    return router


__all__ = ["chat_settings_view", "create_router", "skipped_command", "skipped_view"]
