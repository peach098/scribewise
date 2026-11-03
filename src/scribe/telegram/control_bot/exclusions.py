"""🚫 Exclude chats… in ``/chats`` while work account mode is on (specs/008,
contracts/control-chat.md "Exclusion picker").

- ``wxopen:0`` (shown under /chats only while the mode is on): opens the shared chat picker
  (``chat_picker``, purpose ``exclude``, ``exclusions_only``) with the user's private dialogs
  (``list_dialogs`` already leaves out bots, Saved Messages, service and deleted accounts),
  each marked ✅ included or 🚫 excluded from its ``work_chats`` row. A chat that never wrote has
  no row and shows ✅. Telegram unavailable → "couldn't reach Telegram … /chats", nothing
  changes. Taps toggle 🚫 / ✅ in the FSM snapshot only; only ids of that snapshot are accepted.
- Done (:func:`finish_exclusions`): compares each dialog with its current row and calls
  ``work_account.exclude_chat`` (creating an ``auto`` excluded row for a chat that never wrote)
  or ``work_account.include_chat``; dropped draft cards get "🚫 Chat excluded — dropped". One
  summary answer, then the /chats list.
- ``/cancel`` closes the picker without changes; another command closes it first
  (``chat_picker.AbandonedPickerMiddleware``, registered in ``bot.py``); free text → a hint.

``wxopen`` names no stored entity; it and the picker callbacks act only on the caller's rows and
FSM data. Logs carry counts only, never titles.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.clock import Clock, utcnow
from scribe.db.models import User, UserState, WorkChatOrigin
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, Msg, t
from scribe.logging import get_logger
from scribe.telegram.control_bot import chat_picker
from scribe.telegram.control_bot.review import DraftPresenter
from scribe.telegram.gateway import Dialog, MessagingGateway

log = get_logger("scribe.control_bot")

PURPOSE = "exclude"
CHAT_EXCLUDED_STATUS = Msg("status.chat_excluded")
CHAT_DISABLED_STATUS = Msg("status.chat_disabled")


class ChatsStates(StatesGroup):
    exclude = State()


async def open_exclusions(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    lang: Lang,
) -> None:
    if user is None or user.state == UserState.setting_up:
        await callback.answer(t("common.not_set_up", lang))
        return
    async with session_factory() as session:
        current = await users_repo.get_user(session, user.id) or user
        rows = await chats_repo.list_work_chats(session, user.id)
    if not current.work_account:
        await callback.answer(t("chats.exclude_mode_off", lang))
        return
    await callback.answer()
    if await state.get_state() == ChatsStates.exclude.state:
        await state.clear()  # a second tap closes the previous picker
    excluded = {chat.telegram_chat_id for chat in rows if chats_repo.is_excluded(chat)}
    outcome = await chat_picker.show_chat_picker(
        bot,
        current.telegram_control_chat_id,
        current,
        state,
        gateway,
        purpose=PURPOSE,
        picking_state=ChatsStates.exclude,
        kinds={"private"},
        excluded=excluded,
    )
    log.info("exclusions_opened", user_id=str(user.id), outcome=outcome.value)


async def finish_exclusions(done: chat_picker.PickResult) -> None:
    """The ``exclude`` finisher (Done in the exclusion picker)."""
    callback, bot, user, lang = done.callback, done.bot, done.user, done.lang
    session_factory: async_sessionmaker[AsyncSession] = done.services["session_factory"]
    clock: Clock = done.services.get("clock", utcnow)
    await done.state.clear()
    async with session_factory() as session:
        current = await users_repo.get_user(session, user.id)
        rows = {
            chat.telegram_chat_id: chat
            for chat in await chats_repo.list_work_chats(session, user.id)
        }
    if current is None or not current.work_account:
        await callback.answer(t("chats.exclude_mode_off", lang))
        return
    wanted = {row[0] for row in done.excluded}
    now = clock()
    presenter = DraftPresenter(bot, session_factory, clock=clock)
    excluded_count = included_count = 0
    for chat_id, title, kind in done.dialogs:
        row = rows.get(chat_id)
        currently = row is not None and chats_repo.is_excluded(row)
        if chat_id in wanted and not currently:
            dropped = await work_account.exclude_chat(
                session_factory,
                user.id,
                work_chat_id=row.id if row is not None else None,
                dialog=Dialog(chat_id=chat_id, title=title, kind=kind) if row is None else None,
                now=now,
            )
            if dropped is None:
                continue
            excluded_count += 1
            picked = row is not None and row.origin == WorkChatOrigin.picked
            status = CHAT_DISABLED_STATUS if picked else CHAT_EXCLUDED_STATUS
            for draft_id in dropped:
                await presenter.show_status(user.id, draft_id, status)
        elif chat_id not in wanted and currently and row is not None:
            if await work_account.include_chat(session_factory, user.id, row.id, now=now):
                included_count += 1
    await callback.answer()
    log.info(
        "exclusions_saved", user_id=str(user.id), excluded=excluded_count, included=included_count
    )
    text = t("chats.exclude_done", lang, excluded=excluded_count, included=included_count)
    if callback.message is not None:
        await bot.edit_message_text(
            text=text, chat_id=callback.message.chat.id, message_id=callback.message.message_id
        )
    else:
        await bot.send_message(user.telegram_control_chat_id, text)
    # settings imports this module for its router; imported here to avoid the cycle.
    from scribe.telegram.control_bot.settings import send_chats_list

    await send_chats_list(bot, user.telegram_control_chat_id, current, session_factory, lang)


async def cancel_exclusions(message: Message, bot: Bot, state: FSMContext, lang: Lang) -> None:
    data = await state.get_data()
    await state.clear()
    message_id = data.get("message_id")
    if message_id is not None:
        try:  # best effort: the picker may be too old to edit
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=int(message_id), reply_markup=None
            )
        except Exception as exc:
            log.warning("control_message_edit_failed", error=type(exc).__name__)
    await message.answer(t("chats.exclude_cancelled", lang))


async def exclusions_hint(message: Message, lang: Lang) -> None:
    await message.answer(t("chats.exclude_hint", lang))


chat_picker.register_purpose(
    chat_picker.PickerPurpose(
        name=PURPOSE,
        prompt_key="chats.exclude_prompt",
        finish=finish_exclusions,
        empty_key="chats.exclude_none",
        retry_command="/chats",
        exclusions_only=True,
    )
)


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


def create_router(idle: StateFilter) -> Router:
    """``idle``: the states in which control-chat commands answer (see ``settings.py``)."""
    router = Router(name="exclusions")
    router.callback_query.register(open_exclusions, _action("wxopen"), idle)
    picking = StateFilter(ChatsStates.exclude)
    chat_picker.register_picker_handlers(router, picking)
    router.message.register(cancel_exclusions, Command("cancel"), picking)
    router.message.register(exclusions_hint, picking, F.text & ~F.text.startswith("/"))
    return router


__all__ = ["ChatsStates", "create_router", "finish_exclusions"]
