"""Control bot: aiogram ``Dispatcher`` wiring (research R2).

- ``UserContextMiddleware`` resolves the caller's ``users`` row from the control chat id and
  passes ``user`` (or ``None``), ``user_id`` and ``lang`` to every handler; updates from
  non-private chats are ignored. ``lang`` is the stored ``users.language``, or for someone
  without an account the default from their Telegram ``language_code`` (``scribe.i18n``).
  ``scribe.access.gate.AccessGateMiddleware`` then applies the beta/subscription access gate
  (specs/005; off unless ``SCRIBE_ACCESS_GATE``), and
  ``connect_flow.ControlChatOwnerMiddleware`` refuses Telegram login, reconnect and
  disconnect buttons pressed by anyone but the chat's owner. After the gate,
  ``chat_picker.AbandonedPickerMiddleware`` closes an open ➕ Add work chats picker when another
  command is sent (specs/007).
- FSM state lives in Postgres (``PostgresFSMStorage``) so it survives restarts.
- Shared services (``session_factory``, ``settings``, and whatever ``__main__`` adds:
  ``llm_router``, ``gateway``, ``client_pool``, ``clock``, ``telegram_login``) are dispatcher
  workflow data, injected into handlers by name. Code running outside a handler (the QR wait
  loop) reaches a chat's FSM context with ``dispatcher.fsm.get_context(bot, chat_id, chat_id)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import apply
from scribe.access.gate import AccessGateMiddleware
from scribe.clock import utcnow
from scribe.config import Settings
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, language_for_code, normalize_lang
from scribe.telegram.control_bot import (
    add_chats,
    auto_send_flow,
    chat_picker,
    connect_flow,
    edit_prefill,
    exclusions,
    review,
    setup_flow,
    tasks_command,
    telegram_command,
)
from scribe.telegram.control_bot.render import help_text
from scribe.telegram.control_bot.settings import create_router as create_settings_router

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _private_chat_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message):
        return event.chat.id if event.chat.type == "private" else None
    if isinstance(event, CallbackQuery):
        message = event.message
        if message is not None:
            return message.chat.id if message.chat.type == "private" else None
        return event.from_user.id
    return None


class UserContextMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        chat_id = _private_chat_id(event)
        if chat_id is None:
            return None  # the control bot only works in private chats
        async with self._session_factory() as session:
            user = await users_repo.get_by_control_chat(session, chat_id)
        data["user"] = user
        data["user_id"] = user.id if user is not None else None
        data["lang"] = event_language(event, user.language if user is not None else None)
        return await handler(event, data)


def event_language(event: TelegramObject, stored: str | None) -> Lang:
    """The stored language, else the default for the sender's Telegram ``language_code``."""
    if stored is not None:
        return normalize_lang(stored)
    sender = getattr(event, "from_user", None)
    return language_for_code(sender.language_code if sender is not None else None)


async def help_command(message: Message, lang: Lang) -> None:
    await message.answer(help_text(lang))


def create_base_router() -> Router:
    router = Router(name="base")
    router.message.register(help_command, Command("help"))
    # US2: /settings, /chats, /waiting, /pause, /resume answer in any state, before the
    # state-bound text handlers of setup and review.
    router.include_router(create_settings_router())
    return router


def create_dispatcher(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: BaseStorage,
    routers: list[Router] | None = None,  # fresh instances: a router attaches only once
    **workflow_data: Any,
) -> Dispatcher:
    dispatcher = Dispatcher(
        storage=storage, session_factory=session_factory, settings=settings, **workflow_data
    )
    middleware = UserContextMiddleware(session_factory)
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
    # specs/005: right after the user lookup, so it sees ``user`` and ``lang``; a no-op while
    # ``settings.access_gate`` is off. Inline queries have no user lookup: the gate does its own.
    gate = AccessGateMiddleware(session_factory, settings, clock=workflow_data.get("clock", utcnow))
    dispatcher.message.outer_middleware(gate)
    dispatcher.callback_query.outer_middleware(gate)
    dispatcher.inline_query.outer_middleware(gate)
    # specs/007: a command other than /cancel closes an open ➕ Add work chats picker first;
    # dispatcher-level, so /help and /start (answered before the settings router) close it too.
    picker = chat_picker.AbandonedPickerMiddleware(
        [add_chats.AddChatsStates.picking, exclusions.ChatsStates.exclude]  # 007, 008
    )
    dispatcher.message.outer_middleware(picker)
    # After the user lookup: login/reconnect/disconnect buttons only for the chat's owner.
    dispatcher.callback_query.outer_middleware(connect_flow.ControlChatOwnerMiddleware())
    # Issue #56: a button outside an open Trello/Jira connect flow drops its pending credentials
    # first (an older 📦 Export must not see them); dispatcher-level, so whichever router
    # answers the button. Commands: the settings router's message middleware.
    dispatcher.callback_query.outer_middleware(tasks_command.AbandonedConnectMiddleware())
    # The beta application flow (specs/005 US5) first, so the text typed in ``ApplyStates`` is
    # never taken by another flow's handlers. /telegram before connect_flow so it answers in any
    # state; then connect_flow, whose state-bound handlers take login input before the setup
    # router.
    base = [
        apply.create_router(),
        create_base_router(),
        telegram_command.create_router(),
        connect_flow.create_router(),
        setup_flow.create_router(),
        review.create_router(),
        auto_send_flow.create_router(),  # specs/014: autoconfirm:<chat>:<manual|offer|no>
        edit_prefill.create_router(),  # inline queries (✏️ Edit in input field)
    ]
    for router in [*base, *(routers or [])]:
        dispatcher.include_router(router)
    return dispatcher


def create_bot(settings: Settings) -> Bot:
    """Fails fast (``TokenValidationError``) on a malformed token."""
    return Bot(token=settings.bot_token.get_secret_value())
