"""➕ Add work chats after setup (specs/007-add-work-chats, contracts/control-chat.md).

- ``chatadd:0`` (the ➕ row under ``/chats``, set-up users only): checks the Telegram
  connection, then opens the shared chat picker (``chat_picker``, purpose ``add``) with every
  dialog that is not a work chat yet, enabled or disabled. Disconnected → the reconnect text;
  ``list_dialogs`` failing → "couldn't reach Telegram … /chats"; nothing left → a one-line
  answer. None of these sets a state or writes anything.
- Done (:func:`finish_add`): inserts the selected chats as enabled work chats with the default
  settings (``chats_repo.add_work_chats``: insert-only, so a double Done or a stale snapshot
  never duplicates a chat or re-enables a disabled one), writes one ``chats_added`` activity
  record (work chat ids only) in the same transaction, edits the picker to "Added N" (or
  "Nothing was added"), sends the ``/chats`` list, then updates the style profiles like 🔄
  Rebuild (``profile_command.rebuild_profiles``): it waits for a rebuild already running and
  ends with exactly one note (result, nothing new to learn, or failed).
- Leaving: ``/cancel`` → "Nothing was added"; another command closes the picker first
  (``chat_picker.AbandonedPickerMiddleware``, registered in ``bot.py``) and is answered as
  usual; free text → a one-line hint; buttons of a closed picker → "This list is closed".

Work account mode (specs/008 FR-011): while it is on, every private chat already is a work chat,
so the picker (purpose ``add_groups``) lists only groups.

``chatadd`` names no stored entity, so ``OwnershipMiddleware`` lets it through: it and the
picker callbacks act only on the caller's own rows and FSM data. Group chats get no group-mode
question (spec 006). Logs carry counts only, never titles.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.db.models import ActivityKind, SessionStatus, User, UserState
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.db.repos.chats import NewWorkChat
from scribe.i18n import Lang, t
from scribe.logging import get_logger
from scribe.telegram.control_bot import chat_picker, profile_command
from scribe.telegram.control_bot.render import reconnect_keyboard
from scribe.telegram.gateway import MessagingGateway

log = get_logger("scribe.control_bot")

PURPOSE = "add"
GROUPS_PURPOSE = "add_groups"  # specs/008: work account mode on, groups only
GROUP_KINDS = frozenset({"group", "channel_comments"})


class AddChatsStates(StatesGroup):
    picking = State()


async def add_chats_callback(
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
    await callback.answer()
    if await state.get_state() == AddChatsStates.picking.state:
        await state.clear()  # a second ➕ closes the previous picker
    chat_id = user.telegram_control_chat_id
    async with session_factory() as session:
        connection = await users_repo.get_telegram_session(session, user.id)
        existing = {c.telegram_chat_id for c in await chats_repo.list_work_chats(session, user.id)}
        current = await users_repo.get_user(session, user.id) or user
    groups_only = current.work_account
    if connection is None or connection.status != SessionStatus.active:
        await bot.send_message(
            chat_id, t("telegram.disconnected_start", lang), reply_markup=reconnect_keyboard(lang)
        )
        return
    outcome = await chat_picker.show_chat_picker(
        bot,
        chat_id,
        user,
        state,
        gateway,
        purpose=GROUPS_PURPOSE if groups_only else PURPOSE,
        picking_state=AddChatsStates.picking,
        exclude=existing,
        kinds=GROUP_KINDS if groups_only else None,
    )
    log.info("add_chats_opened", user_id=str(user.id), outcome=outcome.value)


async def finish_add(done: chat_picker.PickResult) -> None:
    """The ``add`` finisher (Done in the /chats picker)."""
    callback, bot, user, lang = done.callback, done.bot, done.user, done.lang
    services = done.services
    session_factory: async_sessionmaker[AsyncSession] = services["session_factory"]
    clock: Clock = services.get("clock", utcnow)
    # First: from now on the picker is closed, and other commands are not held up by it.
    await done.state.clear()
    new = [
        NewWorkChat(chat_id, title, chat_picker.chat_kind(kind))
        for chat_id, title, kind in done.chosen
    ]
    async with session_factory() as session:
        created = await chats_repo.add_work_chats(session, user.id, new)
        if created:
            await activity_repo.record(
                session,
                user.id,
                ActivityKind.chats_added,
                at=clock(),
                meta={"work_chat_ids": [str(chat.id) for chat in created]},
            )
        await session.commit()
    await callback.answer()
    log.info("chats_added", user_id=str(user.id), selected=len(new), added=len(created))
    text = t("chats.added", lang, n=len(created)) if created else t("chats.add_nothing", lang)
    if callback.message is not None:
        await bot.edit_message_text(
            text=text, chat_id=callback.message.chat.id, message_id=callback.message.message_id
        )
    else:
        await bot.send_message(user.telegram_control_chat_id, text)
    # settings imports this module for its router; imported here to avoid the cycle.
    from scribe.telegram.control_bot.settings import send_chats_list

    await send_chats_list(bot, user.telegram_control_chat_id, user, session_factory, lang)
    if created:
        await _learn_new_chats(done, {chat.telegram_chat_id for chat in created})


async def _learn_new_chats(done: chat_picker.PickResult, new_ids: set[int]) -> None:
    """FR-013: the 🔄 Rebuild work, waiting for a running one, skipped without own messages."""
    services = done.services
    needed = ("settings", "llm_router", "gateway")
    if any(services.get(name) is None for name in needed):
        log.warning("add_chats_learn_skipped", user_id=str(done.user.id), reason="no_services")
        return
    await profile_command.rebuild_profiles(
        done.bot,
        done.user,
        services["session_factory"],
        services["settings"],
        services["llm_router"],
        services["gateway"],
        done.lang,
        services.get("clock", utcnow),
        wait=True,
        only_if_new_in=new_ids,
        announce=False,
        failure_key="chats.add_learn_failed",
    )


async def cancel_add(message: Message, bot: Bot, state: FSMContext, lang: Lang) -> None:
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
    await message.answer(t("chats.add_cancelled", lang))


async def add_hint(message: Message, lang: Lang) -> None:
    await message.answer(t("chats.add_hint", lang))


chat_picker.register_purpose(
    chat_picker.PickerPurpose(
        name=PURPOSE,
        prompt_key="chats.add_prompt",
        finish=finish_add,
        empty_key="chats.add_none_left",
        retry_command="/chats",
    )
)
chat_picker.register_purpose(
    chat_picker.PickerPurpose(
        name=GROUPS_PURPOSE,
        prompt_key="chats.add_prompt_groups",
        finish=finish_add,
        empty_key="chats.add_none_left_groups",
        retry_command="/chats",
    )
)


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


def create_router(idle: StateFilter) -> Router:
    """``idle``: the states in which control-chat commands answer (see ``settings.py``)."""
    router = Router(name="add_chats")
    router.callback_query.register(add_chats_callback, _action("chatadd"), idle)
    picking = StateFilter(AddChatsStates.picking)
    chat_picker.register_picker_handlers(router, picking)
    router.message.register(cancel_add, Command("cancel"), picking)
    router.message.register(add_hint, picking, F.text & ~F.text.startswith("/"))
    return router


__all__ = ["AddChatsStates", "create_router", "finish_add"]
