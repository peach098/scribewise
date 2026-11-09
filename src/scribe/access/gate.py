"""Control-bot access gate middleware (specs/005-beta-subscription-access/research.md Q2).

An outer middleware on ``message``, ``callback_query`` and ``inline_query``, registered right
after ``UserContextMiddleware`` (which drops non-private chats and sets ``user`` and ``lang``).
With ``Settings.access_gate`` on, each update gets one of (contracts/control-chat.md):

- **allow**: the sender is an operator or has access (beta and/or an active subscription). A
  username beta entry is linked to the sender here (FR-009). The handler runs as usual.
- **refuse**: no access and no ``users`` row. The handler never runs (no user row, no FSM state,
  no LLM call); ``refused_contacts`` is bumped and ``access.refused`` is sent at most once per
  cooldown.
- **restricted**: no access but a ``users`` row exists. Only :data:`RESTRICTED_COMMANDS`, any
  message in the ``/delete_my_data`` confirmation step and :data:`RESTRICTED_CALLBACK_PREFIXES`
  pass; everything else gets ``access.ended_reply`` under the same cooldown.

Both replies carry the **Join the beta** / **Subscription** buttons, or say instead that an
application is pending or was rejected recently (:func:`scribe.access.apply.no_access_reply`).
For anyone without access, ``apply:`` buttons and the messages of the application flow (text,
media and ``/cancel`` while in ``ApplyStates``; other commands are not) pass to
``scribe.access.apply`` without counting against the cooldown (US5).

Payments (specs/015, research P7): for anyone without access, the service messages
``successful_payment`` and ``refunded_payment``, ``pay:`` buttons, ``/paysupport``, ``/terms``
and any message in ``PaySupportStates`` (``/cancel`` included) also pass, without counting
against the cooldown (:func:`payment_allows`): a person with no account must be credited for
what they paid. ``pre_checkout_query`` and ``subscription`` updates never reach this gate.

Callback queries are always answered (silently) so the client's spinner stops; inline queries
get an empty result. Logs hold only ``answered`` and ``has_account``, never an id or a username.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import policy, repo
from scribe.access.apply import APPLY_PREFIX, ApplyStates, no_access_reply
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import User
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, language_for_code, normalize_lang
from scribe.logging import get_logger
from scribe.payments.states import PaySupportStates
from scribe.telegram.control_bot.account_command import DeleteStates

log = get_logger("scribe.access")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

# Restricted mode (no access, account exists): what still reaches its handler (FR-011).
RESTRICTED_COMMANDS = frozenset({"my_data", "delete_my_data", "cancel", "account"})
PAY_PREFIX = "pay:"  # specs/015: the payment buttons; the gate lets them through
PAY_COMMANDS = frozenset({"paysupport", "terms"})
RESTRICTED_CALLBACK_PREFIXES = ("mydata:",)
RESTRICTED_STATES = frozenset({DeleteStates.confirm.state})  # any message in these FSM states


def command_name(text: str | None) -> str | None:
    """``/my_data@scribe_bot args`` -> ``my_data``; ``None`` for a non-command."""
    if not text or not text.startswith("/"):
        return None
    parts = text[1:].split(maxsplit=1)
    return parts[0].split("@", 1)[0].lower() if parts else None


async def apply_allows(event: TelegramObject, state: FSMContext | None) -> bool:
    """Whether an update belongs to the beta application flow (US5): an ``apply:`` button, or
    a message other than a command (``/cancel`` excepted) while in ``ApplyStates``."""
    if isinstance(event, CallbackQuery):
        return (event.data or "").startswith(APPLY_PREFIX)
    if not isinstance(event, Message) or state is None:
        return False
    if command_name(event.text) not in (None, "cancel"):
        return False
    return await state.get_state() in ApplyStates.__all_states_names__


async def payment_allows(event: TelegramObject, state: FSMContext | None) -> bool:
    """Whether an update belongs to the payments flows (specs/015 research P7): a payment
    service message, a ``pay:`` button, ``/paysupport`` or ``/terms``, or any message (a
    ``/cancel`` included) while in ``PaySupportStates``."""
    if isinstance(event, CallbackQuery):
        return (event.data or "").startswith(PAY_PREFIX)
    if not isinstance(event, Message):
        return False
    if event.successful_payment is not None or event.refunded_payment is not None:
        return True
    if command_name(event.text) in PAY_COMMANDS:
        return True
    return state is not None and await state.get_state() in PaySupportStates.__all_states_names__


async def restricted_allows(event: TelegramObject, state: FSMContext | None) -> bool:
    """Whether an update of a user without access still reaches its handler."""
    if isinstance(event, CallbackQuery):
        return (event.data or "").startswith(RESTRICTED_CALLBACK_PREFIXES)
    if not isinstance(event, Message):
        return False
    if command_name(event.text) in RESTRICTED_COMMANDS:
        return True
    return state is not None and await state.get_state() in RESTRICTED_STATES


class AccessGateMiddleware(BaseMiddleware):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Clock = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._clock = clock

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not self._settings.access_gate:
            return await handler(event, data)
        sender = getattr(event, "from_user", None)
        if sender is None:
            return await handler(event, data)
        if isinstance(event, InlineQuery):
            # No UserContextMiddleware on inline queries: look the account up here.
            async with self._session_factory() as session:
                user: User | None = await users_repo.get_by_control_chat(session, sender.id)
        elif "user" not in data:
            return await handler(event, data)  # not a private-chat update the bot serves
        else:
            user = data["user"]
        if policy.is_operator(sender.id, self._settings):
            return await handler(event, data)

        now = self._clock()
        async with self._session_factory() as session:
            status = await repo.access_status(
                session, self._settings, sender.id, sender.username, now=now
            )
            await session.commit()  # a username beta entry may have been linked
        if status.has_access:
            return await handler(event, data)
        raw_state = data.get("state")
        state = raw_state if isinstance(raw_state, FSMContext) else None
        if await apply_allows(event, state):
            if (
                user is not None
                and isinstance(event, CallbackQuery)
                and event.data == "apply:start"
            ):
                await _drop_unfinished_flow(event, state, data)  # the application replaces it
            return await handler(event, data)
        if await payment_allows(event, state):
            return await handler(event, data)
        if user is not None and await restricted_allows(event, state):
            await _drop_unfinished_flow(event, state, data)
            return await handler(event, data)

        if isinstance(event, InlineQuery):
            await event.answer([], cache_time=0)
            return None
        lang = data.get("lang") or _language(user, sender.language_code)
        text, markup = "", None
        async with self._session_factory() as session:
            answered = await repo.touch_refused(
                session, sender.id, now=now, cooldown_s=self._settings.access_refusal_cooldown_s
            )
            if answered:
                text, markup = await no_access_reply(
                    session,
                    sender.id,
                    has_account=user is not None,
                    lang=lang,
                    now=now,
                    settings=self._settings,
                )
            await session.commit()
        log.info("access_refused", answered=answered, has_account=user is not None)
        if isinstance(event, Message):
            if answered:
                await event.answer(text, reply_markup=markup)
        elif isinstance(event, CallbackQuery):
            await event.answer()
            if answered:
                bot: Bot | None = data.get("bot") or event.bot
                chat_id = event.message.chat.id if event.message is not None else sender.id
                if bot is not None:
                    await bot.send_message(chat_id, text, reply_markup=markup)
        return None


async def _drop_unfinished_flow(
    event: TelegramObject, state: FSMContext | None, data: dict[str, Any]
) -> None:
    """A user without access cannot finish setup, the Telegram login or any other flow, and the
    data commands only answer outside those flows. So before ``/my_data`` or ``/delete_my_data``
    (or a ``mydata:`` or ``apply:start`` button) end whatever flow is open, including a running
    login attempt. The delete confirmation step, the beta application (which the data commands
    answer in) and ``/cancel`` are left to their own handlers."""
    if state is None or (isinstance(event, Message) and command_name(event.text) == "cancel"):
        return
    current = await state.get_state()
    if (
        current is None
        or current in RESTRICTED_STATES
        or current in ApplyStates.__all_states_names__
    ):
        return
    login = data.get("telegram_login")
    end = getattr(login, "end", None)
    chat_id: int | None = None
    if isinstance(event, Message):
        chat_id = event.chat.id
    elif isinstance(event, CallbackQuery) and event.message is not None:
        chat_id = event.message.chat.id
    if end is not None and chat_id is not None:
        await end(chat_id, "cancelled", drain=True)
    await state.clear()
    data["raw_state"] = None  # aiogram's StateFilter reads this, not the storage
    log.info("access_flow_dropped", flow=current.split(":", 1)[0])


def _language(user: User | None, language_code: str | None) -> Lang:
    if user is not None:
        return normalize_lang(user.language)
    return language_for_code(language_code)


__all__ = [
    "PAY_PREFIX",
    "RESTRICTED_CALLBACK_PREFIXES",
    "RESTRICTED_COMMANDS",
    "RESTRICTED_STATES",
    "AccessGateMiddleware",
    "apply_allows",
    "command_name",
    "payment_allows",
    "restricted_allows",
]
