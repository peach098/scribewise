"""Connecting a Telegram account (002 contracts/control-chat.md "Setup FSM states").

Shared by setup (``purpose="setup"``, then the chat picker) and reconnect
(``purpose="reconnect"``): ``connect_choose`` -> (``connect_qr`` | ``connect_phone`` ->
``connect_code``) -> ``connect_password?`` -> :func:`finish_login`.

The QR wait runs in a background task per attempt (``attempt.task``, research Q3): it shows the
code as a photo, swaps it on expiry up to ``tg_qr_max_codes`` times and hands over to the
password step or :func:`finish_login`. Whenever the loop ends (also when the attempt is cancelled)
the photo is deleted, so no scannable code stays in the chat. In ``connect_password`` the same
task slot holds the password timer (``tg_login_password_timeout_s``, both methods).

The in-memory login attempt lives in :class:`LoginManager`, which records every attempt exactly
once (in ``end``). FSM data holds only ``purpose``: the phone number, login code, password and QR
token are never logged and never stored; the messages that carry them are deleted from the chat
(best effort).

The ``tglogin``, ``tgreconnect`` and ``tgdisconnect`` buttons only work for the owner of the
control chat (:class:`ControlChatOwnerMiddleware`). After a restart, chats left in a connect
state are sent back to the method choice (:func:`recover_interrupted_logins`, research Q10).

Every text is in the user's language (``users.language``, ``scribe.i18n``); the background QR
loop and the password timer carry it in :class:`_LoginContext`.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    TelegramObject,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import ActivityKind, BotFsmState, LoginMethod, User, UserState
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, language_for_code, normalize_lang, t
from scribe.logging import get_logger
from scribe.telegram.control_bot import setup_flow
from scribe.telegram.control_bot.render import (
    account_label,
    format_callback,
    method_choice_keyboard,
    not_my_account_keyboard,
    qr_caption,
    qr_expired_keyboard,
    qr_keyboard,
    use_phone_keyboard,
    use_qr_keyboard,
)
from scribe.telegram.gateway import MessagingGateway
from scribe.telegram.login import (
    ActivityRecorder,
    CodeExpired,
    InvalidCode,
    InvalidPassword,
    InvalidPhone,
    LoginAttempt,
    LoginError,
    LoginFlood,
    LoginManager,
    LoginOutcome,
    LoginPurpose,
    LoginResult,
    QrCode,
    QrOutcome,
)
from scribe.telegram.qr_image import render_qr_png

log = get_logger("scribe.control_bot")

QR_CALLBACK = format_callback("tglogin", 0, "qr")
PHONE_CALLBACK = format_callback("tglogin", 0, "phone")
CANCEL_CALLBACK = format_callback("tglogin", 0, "cancel")
NOT_ME_CALLBACK = format_callback("tglogin", 0, "notme")
QR_FILENAME = "telegram-login-qr.png"

# The password timer's sleep; tests replace it to fire the timeout without waiting.
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class ConnectStates(StatesGroup):
    connect_choose = State()
    connect_qr = State()
    connect_phone = State()
    connect_code = State()
    connect_password = State()


# Restart cleanup: the method an interrupted attempt used, as far as the state tells.
# ``connect_password`` follows both methods, so it records ``method=None`` (unknown) rather than
# a guess that would skew per-method rates.
_STATE_METHOD: dict[str | None, str] = {
    ConnectStates.connect_qr.state: "qr",
    ConnectStates.connect_phone.state: "phone_code",
    ConnectStates.connect_code.state: "phone_code",
}
_INTERRUPTIBLE_STATES = tuple(
    name
    for name in ConnectStates.__all_states_names__
    if name != ConnectStates.connect_choose.state
)
_OWNED_CALLBACKS = ("tglogin:", "tgreconnect:", "tgdisconnect:")


# --- helpers ------------------------------------------------------------------------------------


async def _delete_quietly(message: Message, bot: Bot) -> None:
    """Remove a message carrying login secrets from the control chat (best effort)."""
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception as exc:
        log.info("login_message_delete_failed", error=type(exc).__name__)


def normalize_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    return "+" + digits if 7 <= len(digits) <= 15 else None


def _purpose_of(data: dict[str, Any]) -> LoginPurpose:
    return "reconnect" if data.get("purpose") == "reconnect" else "setup"


async def get_purpose(state: FSMContext) -> LoginPurpose:
    return _purpose_of(await state.get_data())


async def in_connect_state(state: FSMContext) -> bool:
    return await state.get_state() in ConnectStates.__all_states_names__


def _log_step(
    user_id: uuid.UUID, method: str | None, step: str, *, code_no: int | None = None
) -> None:
    """``login_step``: metadata only, never the URL, token, phone or password (FR-008)."""
    log.info("login_step", user_id=str(user_id), method=method, step=step, code_no=code_no)


def login_activity_recorder(session_factory: async_sessionmaker[AsyncSession]) -> ActivityRecorder:
    """``LoginManager`` ``record`` callback: one ``telegram_login`` activity row per attempt,
    plus the ``login_ended`` log line."""

    async def record(user_id: uuid.UUID, meta: dict[str, Any]) -> None:
        log.info(
            "login_ended",
            user_id=str(user_id),
            method=meta.get("method"),
            purpose=meta.get("purpose"),
            outcome=meta.get("outcome"),
            duration_s=meta.get("duration_s"),
        )
        async with session_factory() as session:
            await activity_repo.record(session, user_id, ActivityKind.telegram_login, meta=meta)
            await session.commit()

    return record


async def chat_language(session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> Lang:
    """The stored language of the control chat's user (English when there is none)."""
    async with session_factory() as session:
        user = await users_repo.get_by_control_chat(session, chat_id)
    return normalize_lang(user.language if user is not None else None)


def on_late_scan(
    bot: Bot, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[[int], Awaitable[None]]:
    """``LoginManager`` ``on_late_scan`` callback (research Q4): the manager has already logged
    the drained client out; this only tells the user."""

    async def notify(chat_id: int) -> None:
        lang = await chat_language(session_factory, chat_id)
        await bot.send_message(chat_id, t("connect.late_scan", lang))

    return notify


async def _back_to_choice(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    purpose: LoginPurpose,
    lang: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """End of an attempt that did not connect: back to ``connect_choose`` with the method
    choice (or another keyboard offering the next step)."""
    await state.set_state(ConnectStates.connect_choose)
    await state.set_data({"purpose": purpose})
    markup = reply_markup or method_choice_keyboard(lang)
    await bot.send_message(chat_id, text, reply_markup=markup)


@dataclass(frozen=True)
class _LoginContext:
    """What a login step needs outside a handler (the QR loop and the password timer).

    Captured from the handler: the ``FSMContext`` stays valid after the handler returns.
    """

    bot: Bot
    chat_id: int
    state: FSMContext
    telegram_login: LoginManager
    session_factory: async_sessionmaker[AsyncSession]
    gateway: MessagingGateway
    client_pool: MessagingGateway
    settings: Settings
    clock: Clock
    lang: str  # the user's UI language

    def is_current(self, attempt: LoginAttempt) -> bool:
        return self.telegram_login.get(self.chat_id) is attempt

    async def finish(self, attempt: LoginAttempt) -> LoginOutcome:
        return await finish_login(
            self.bot,
            self.chat_id,
            attempt,
            self.state,
            telegram_login=self.telegram_login,
            session_factory=self.session_factory,
            gateway=self.gateway,
            client_pool=self.client_pool,
            clock=self.clock,
            lang=self.lang,
        )


# --- entry point --------------------------------------------------------------------------------


async def begin_connect(
    bot: Bot, chat_id: int, user: User, state: FSMContext, purpose: LoginPurpose
) -> None:
    """Show the method choice; FSM data is reset to ``{"purpose": purpose}``."""
    await state.set_state(ConnectStates.connect_choose)
    await state.set_data({"purpose": purpose})
    log.info("login_step", user_id=str(user.id), method=None, step="choose", purpose=purpose)
    lang = user.language
    await bot.send_message(
        chat_id, t("connect.method_choice", lang), reply_markup=method_choice_keyboard(lang)
    )


async def choose_phone(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    telegram_login: LoginManager,
    lang: Lang,
) -> None:
    """``tglogin:0:phone``: end any attempt in progress (a QR one is drained), ask for the phone."""
    await callback.answer()
    if user is None or callback.message is None:
        return
    chat_id = callback.message.chat.id
    previous = telegram_login.get(chat_id)
    await telegram_login.end(chat_id, "cancelled", drain=True)
    await state.set_state(ConnectStates.connect_phone)
    await state.set_data({"purpose": await get_purpose(state)})
    if previous is not None and previous.method == "qr":
        await bot.send_message(chat_id, t("connect.switched_to_phone", lang))
    await bot.send_message(
        chat_id, t("connect.phone_prompt", lang), reply_markup=use_qr_keyboard(lang)
    )


async def cancel_button(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    telegram_login: LoginManager,
    lang: Lang,
) -> None:
    """``tglogin:0:cancel``: same as ``/cancel``."""
    await callback.answer()
    if user is None or callback.message is None:
        return
    await setup_flow.cancel_setup(bot, callback.message.chat.id, state, telegram_login, lang)


# --- QR login (research Q1-Q5) ------------------------------------------------------------------


async def choose_qr(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    telegram_login: LoginManager,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    client_pool: MessagingGateway,
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``tglogin:0:qr`` (method choice, "Use a QR code instead", "Show a new code"): start a new
    QR round. ``LoginManager.begin`` ends and drains any previous attempt first (FR-009)."""
    await callback.answer()
    if user is None or callback.message is None:
        return
    ctx = _LoginContext(
        bot=bot,
        chat_id=callback.message.chat.id,
        state=state,
        telegram_login=telegram_login,
        session_factory=session_factory,
        gateway=gateway,
        client_pool=client_pool,
        settings=settings,
        clock=clock,
        lang=lang,
    )
    await start_qr_round(ctx, user, await get_purpose(state))


async def start_qr_round(ctx: _LoginContext, user: User, purpose: LoginPurpose) -> None:
    attempt = await ctx.telegram_login.begin(
        ctx.chat_id, user_id=user.id, purpose=purpose, method="qr"
    )
    await ctx.state.set_state(ConnectStates.connect_qr)
    await ctx.state.set_data({"purpose": purpose})
    try:
        await _show_qr(ctx, attempt, await attempt.client.start_qr(), user.timezone)
    except Exception as exc:
        await _qr_failed(ctx, attempt, exc)
        return
    if ctx.is_current(attempt):  # a concurrent press may have replaced it meanwhile
        attempt.task = asyncio.create_task(_qr_loop(ctx, attempt, user.timezone))
    else:
        await _remove_qr_photo(ctx, attempt)


async def _show_qr(ctx: _LoginContext, attempt: LoginAttempt, qr: QrCode, tz: str) -> None:
    """Send the code as a photo, or swap the photo in place on refresh (research Q2)."""
    attempt.qr_codes_shown += 1
    attempt.qr_expires_at = qr.expires_at
    total = ctx.settings.tg_qr_max_codes
    caption = qr_caption(attempt.qr_codes_shown, total, qr.expires_at, tz, ctx.lang)
    markup = qr_keyboard(ctx.lang, qr.url if ctx.settings.tg_qr_same_device_link else None)
    photo = BufferedInputFile(render_qr_png(qr.url), filename=QR_FILENAME)
    if attempt.qr_message_id is None:
        sent = await ctx.bot.send_photo(ctx.chat_id, photo, caption=caption, reply_markup=markup)
        attempt.qr_message_id = sent.message_id
        step = "qr_shown"
    else:
        await ctx.bot.edit_message_media(
            InputMediaPhoto(media=photo, caption=caption),
            chat_id=ctx.chat_id,
            message_id=attempt.qr_message_id,
            reply_markup=markup,
        )
        step = "qr_refreshed"
    _log_step(attempt.user_id, attempt.method, step, code_no=attempt.qr_codes_shown)


async def _remove_qr_photo(ctx: _LoginContext, attempt: LoginAttempt) -> None:
    """Delete the QR photo so no scannable code stays in the chat (best effort)."""
    message_id, attempt.qr_message_id = attempt.qr_message_id, None
    if message_id is None:
        return
    try:
        await ctx.bot.delete_message(ctx.chat_id, message_id)
    except Exception as exc:
        log.info("login_message_delete_failed", error=type(exc).__name__)


async def _qr_failed(ctx: _LoginContext, attempt: LoginAttempt, exc: Exception) -> None:
    await _remove_qr_photo(ctx, attempt)
    if not ctx.is_current(attempt):  # replaced or cancelled meanwhile: nothing to report
        return
    lang = ctx.lang
    if isinstance(exc, LoginFlood):
        text = t("connect.rate_limited", lang, seconds=exc.seconds)
        markup = use_phone_keyboard(lang)
    else:
        log.error("login_qr_failed", user_id=str(attempt.user_id), error=type(exc).__name__)
        text, markup = t("connect.telegram_unavailable", lang), method_choice_keyboard(lang)
    await ctx.telegram_login.end(ctx.chat_id, "failed", error_kind=type(exc).__name__)
    await _back_to_choice(ctx.bot, ctx.chat_id, ctx.state, attempt.purpose, lang, text, markup)


async def _qr_loop(ctx: _LoginContext, attempt: LoginAttempt, tz: str) -> None:
    """``attempt.task`` while in ``connect_qr``: wait for a scan, refresh expired codes."""
    try:
        while True:
            expires_at = attempt.qr_expires_at or ctx.clock()
            timeout_s = max((expires_at - ctx.clock()).total_seconds(), 1.0)
            try:
                outcome = await attempt.client.wait_qr(timeout_s)
                if (
                    outcome is QrOutcome.expired
                    and attempt.qr_codes_shown < ctx.settings.tg_qr_max_codes
                ):
                    await _show_qr(ctx, attempt, await attempt.client.refresh_qr(), tz)
                    continue
            except Exception as exc:
                await _qr_failed(ctx, attempt, exc)
                return
            await _remove_qr_photo(ctx, attempt)
            if outcome is QrOutcome.expired:
                # Stop generating codes for someone who walked away (FR-005).
                await ctx.telegram_login.end(ctx.chat_id, "expired")
                await _back_to_choice(
                    ctx.bot,
                    ctx.chat_id,
                    ctx.state,
                    attempt.purpose,
                    ctx.lang,
                    t("connect.qr_expired", ctx.lang),
                    qr_expired_keyboard(ctx.lang),
                )
                return
            _log_step(attempt.user_id, attempt.method, "scanned", code_no=attempt.qr_codes_shown)
            if outcome is QrOutcome.password_needed:
                await _ask_password(ctx, attempt)
                return
            await ctx.finish(attempt)
            return
    finally:  # also when the attempt is cancelled (switch method, /cancel, new attempt)
        await _remove_qr_photo(ctx, attempt)


# --- password step (both methods) ---------------------------------------------------------------


async def _ask_password(ctx: _LoginContext, attempt: LoginAttempt) -> None:
    attempt.password_needed = True
    await ctx.state.set_state(ConnectStates.connect_password)
    _log_step(attempt.user_id, attempt.method, "password_asked")
    _start_password_timer(ctx, attempt)
    # Both methods (contracts/control-chat.md "Password step").
    await ctx.bot.send_message(ctx.chat_id, t("connect.password_prompt", ctx.lang))


def _start_password_timer(ctx: _LoginContext, attempt: LoginAttempt) -> None:
    """(Re)start the ``tg_login_password_timeout_s`` timer in the attempt's task slot."""
    previous = attempt.task
    if previous is not None and previous is not asyncio.current_task() and not previous.done():
        previous.cancel()
    attempt.task = asyncio.create_task(_password_timeout(ctx, attempt))


async def _password_timeout(ctx: _LoginContext, attempt: LoginAttempt) -> None:
    await _sleep(ctx.settings.tg_login_password_timeout_s)
    if not ctx.is_current(attempt):
        return
    await ctx.telegram_login.end(ctx.chat_id, "expired")
    text = t("connect.login_timeout", ctx.lang)
    await _back_to_choice(ctx.bot, ctx.chat_id, ctx.state, attempt.purpose, ctx.lang, text)


# --- "This is not my account" (FR-012) ----------------------------------------------------------


async def not_my_account(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    lang: Lang,
) -> None:
    """``tglogin:0:notme``, valid until setup completes: log the just-connected account out,
    forget it and go back to the method choice."""
    if user is None or callback.message is None:
        await callback.answer()
        return
    if user.state != UserState.setting_up:
        await callback.answer(t("connect.not_my_account_unavailable", lang))
        return
    await callback.answer()
    chat_id = callback.message.chat.id
    async with session_factory() as session:
        tg_session = await users_repo.get_telegram_session(session, user.id)
    if tg_session is None:  # already undone (double tap)
        return
    try:
        await gateway.log_out(user.id)  # stops the client and ends the Telegram session
    except Exception as exc:
        log.error("login_not_my_account_failed", user_id=str(user.id), error=type(exc).__name__)
        await bot.send_message(chat_id, t("connect.telegram_unavailable", lang))
        return
    async with session_factory() as session:
        await users_repo.delete_telegram_session(session, user.id)
        await users_repo.update_user(session, user.id, telegram_user_id=None)
        await session.commit()
    # The login attempt itself was already recorded as "connected"; this is a separate row.
    await login_activity_recorder(session_factory)(
        user.id,
        {
            "method": tg_session.login_method.value,
            "purpose": "setup",
            "outcome": "not_my_account",
            "qr_codes_shown": 0,
            "password_needed": False,
            "duration_s": 0,
            "error_kind": None,
        },
    )
    try:  # the button has done its job
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=callback.message.message_id, reply_markup=None
        )
    except Exception as exc:
        log.info("login_message_edit_failed", error=type(exc).__name__)
    await state.clear()
    text = t("connect.not_my_account_done", lang)
    await _back_to_choice(bot, chat_id, state, "setup", lang, text)


# --- phone login steps --------------------------------------------------------------------------


async def command_during_login(message: Message, bot: Bot, state: FSMContext, lang: Lang) -> None:
    """A command other than /start, /cancel, /help or /telegram while the bot waits for a phone
    number, code or password: never login input. In ``connect_password`` it is still deleted
    first, as it may be a password that starts with "/" (FR-008)."""
    if await state.get_state() == ConnectStates.connect_password.state:
        await _delete_quietly(message, bot)
    await message.answer(t("connect.finish_connecting", lang))


async def phone_step(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    telegram_login: LoginManager,
    lang: Lang,
) -> None:
    await _delete_quietly(message, bot)
    phone = normalize_phone(message.text or "")
    if user is None:
        return
    if phone is None:
        await message.answer(t("connect.invalid_phone", lang), reply_markup=use_qr_keyboard(lang))
        return
    chat_id = message.chat.id
    attempt = await telegram_login.begin(
        chat_id, user_id=user.id, purpose=await get_purpose(state), method="phone_code"
    )
    try:
        await attempt.client.send_code(phone)
    except InvalidPhone:
        await telegram_login.end(chat_id, "failed", error_kind="InvalidPhone")
        await message.answer(t("connect.invalid_phone", lang), reply_markup=use_qr_keyboard(lang))
        return
    except LoginFlood as exc:
        await telegram_login.end(chat_id, "failed", error_kind="LoginFlood")
        text = t("connect.rate_limited", lang, seconds=exc.seconds)
        await message.answer(text, reply_markup=use_qr_keyboard(lang))
        return
    except Exception as exc:
        await telegram_login.end(chat_id, "failed", error_kind=type(exc).__name__)
        log.error("login_send_code_failed", user_id=str(user.id), error=type(exc).__name__)
        text = t("connect.telegram_unavailable", lang)
        await message.answer(text, reply_markup=use_qr_keyboard(lang))
        return
    _log_step(user.id, attempt.method, "code_sent")
    await state.set_state(ConnectStates.connect_code)
    await message.answer(t("connect.code_prompt", lang), reply_markup=use_qr_keyboard(lang))


async def _pending_login(
    message: Message, bot: Bot, state: FSMContext, telegram_login: LoginManager, lang: str
) -> LoginAttempt | None:
    attempt = telegram_login.get(message.chat.id)
    if attempt is None:  # e.g. after a restart, a timeout or a newer attempt
        purpose = await get_purpose(state)
        text = t("connect.login_lost", lang)
        await _back_to_choice(bot, message.chat.id, state, purpose, lang, text)
    return attempt


async def code_step(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    telegram_login: LoginManager,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    client_pool: MessagingGateway,
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    await _delete_quietly(message, bot)
    code = re.sub(r"\D", "", message.text or "")
    if user is None:
        return
    attempt = await _pending_login(message, bot, state, telegram_login, lang)
    if attempt is None:
        return
    chat_id = message.chat.id
    try:
        needs_password = await attempt.client.sign_in_code(code)
    except InvalidCode:
        await message.answer(t("connect.invalid_code", lang), reply_markup=use_qr_keyboard(lang))
        return
    except (CodeExpired, InvalidPhone, LoginFlood) as exc:
        await telegram_login.end(chat_id, "failed", error_kind=type(exc).__name__)
        text = (
            t("connect.rate_limited", lang, seconds=exc.seconds)
            if isinstance(exc, LoginFlood)
            else t("connect.code_expired", lang)
        )
        await _back_to_choice(bot, chat_id, state, attempt.purpose, lang, text)
        return
    ctx = _LoginContext(
        bot=bot,
        chat_id=chat_id,
        state=state,
        telegram_login=telegram_login,
        session_factory=session_factory,
        gateway=gateway,
        client_pool=client_pool,
        settings=settings,
        clock=clock,
        lang=lang,
    )
    if needs_password:
        await _ask_password(ctx, attempt)
        return
    await ctx.finish(attempt)


async def password_step(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    telegram_login: LoginManager,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    client_pool: MessagingGateway,
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """Both methods. Every message in ``connect_password`` is deleted first, whatever happens
    next (FR-008)."""
    await _delete_quietly(message, bot)
    if user is None:
        return
    attempt = await _pending_login(message, bot, state, telegram_login, lang)
    if attempt is None:
        return
    if not message.text:
        await message.answer(t("connect.password_as_text", lang))
        return
    chat_id = message.chat.id
    ctx = _LoginContext(
        bot=bot,
        chat_id=chat_id,
        state=state,
        telegram_login=telegram_login,
        session_factory=session_factory,
        gateway=gateway,
        client_pool=client_pool,
        settings=settings,
        clock=clock,
        lang=lang,
    )
    try:
        await attempt.client.sign_in_password(message.text)
    except InvalidPassword:
        _start_password_timer(ctx, attempt)  # the user is still there: a fresh 10 minutes
        await message.answer(t("connect.invalid_password", lang))
        return
    except LoginError as exc:
        # ``end`` also cancels the password timer.
        await telegram_login.end(chat_id, "failed", error_kind=type(exc).__name__)
        text = (
            t("connect.rate_limited", lang, seconds=exc.seconds)
            if isinstance(exc, LoginFlood)
            else t("connect.login_lost", lang)
        )
        await _back_to_choice(bot, chat_id, state, attempt.purpose, lang, text)
        return
    await ctx.finish(attempt)  # ``end`` inside cancels the password timer


# --- finish step (both methods, both purposes) --------------------------------------------------


async def _log_out_quietly(attempt: LoginAttempt) -> None:
    # The account was authorised but is not stored: end its Telegram session. A failure is
    # logged, never shown; the user can still end the session in Telegram's Active sessions.
    try:
        await attempt.client.log_out()
    except Exception as exc:
        log.error("login_log_out_failed", user_id=str(attempt.user_id), error=type(exc).__name__)


async def _reject(
    bot: Bot,
    chat_id: int,
    attempt: LoginAttempt,
    state: FSMContext,
    telegram_login: LoginManager,
    outcome: LoginOutcome,
    lang: str,
    text: str,
) -> LoginOutcome:
    await _log_out_quietly(attempt)
    await telegram_login.end(chat_id, outcome)
    log.info("login_rejected", user_id=str(attempt.user_id), outcome=outcome)
    await _back_to_choice(bot, chat_id, state, attempt.purpose, lang, text)
    return outcome


async def finish_login(
    bot: Bot,
    chat_id: int,
    attempt: LoginAttempt,
    state: FSMContext,
    *,
    telegram_login: LoginManager,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    client_pool: MessagingGateway,
    lang: str,
    clock: Clock = utcnow,
) -> LoginOutcome:
    """contracts/login-client.md "Finish step": account checks, store the session, start the
    user's client, then "Connected as" + **This is not my account** and the chat picker
    (setup) or "Reconnected as" (reconnect).

    The attempt is ended (and so recorded) here with the returned outcome; ``attempt.client``
    is already authorised.
    """
    user_id = attempt.user_id
    try:
        result: LoginResult = await attempt.client.complete()
    except Exception as exc:
        await telegram_login.end(chat_id, "failed", error_kind=type(exc).__name__)
        log.error("login_complete_failed", user_id=str(user_id), error=type(exc).__name__)
        text = t("connect.telegram_unavailable", lang)
        await _back_to_choice(bot, chat_id, state, attempt.purpose, lang, text)
        return "failed"

    label = account_label(result.display_name, result.username, result.masked_phone, lang)
    async with session_factory() as session:
        user = await users_repo.get_user(session, user_id)
        owner = await users_repo.find_user_by_telegram_user_id(session, result.telegram_user_id)
        stored = await users_repo.get_telegram_session(session, user_id)
    if user is None:  # the account was deleted meanwhile
        await _log_out_quietly(attempt)
        await telegram_login.end(chat_id, "failed", error_kind="UserGone")
        await state.clear()
        return "failed"
    if (
        attempt.purpose == "reconnect"
        and user.telegram_user_id is not None
        and user.telegram_user_id != result.telegram_user_id
    ):
        # Sessions stored before the label existed fall back to the control-chat name.
        expected = (stored.account_label if stored is not None else None) or user.display_name
        text = t(
            "connect.wrong_account",
            lang,
            other=label,
            expected=expected or t("connect.original_account", lang),
        )
        return await _reject(
            bot, chat_id, attempt, state, telegram_login, "wrong_account", lang, text
        )
    if owner is not None and owner.id != user_id:
        text = t("connect.account_taken", lang)
        return await _reject(
            bot, chat_id, attempt, state, telegram_login, "account_taken", lang, text
        )

    try:
        async with session_factory() as session:
            await users_repo.upsert_telegram_session(
                session,
                user_id,
                result.session_string,
                method=LoginMethod(attempt.method),
                connected_at=clock(),
                account_label=label,
            )
            await users_repo.update_user(session, user_id, telegram_user_id=result.telegram_user_id)
            await session.commit()
    except IntegrityError:  # a concurrent login took the account between the check and here
        text = t("connect.account_taken", lang)
        return await _reject(
            bot, chat_id, attempt, state, telegram_login, "account_taken", lang, text
        )
    # Closes the login client before the user's own client opens the same session.
    await telegram_login.end(chat_id, "connected")
    _log_step(user_id, attempt.method, "finished")
    try:
        await client_pool.start(user_id)
    except Exception as exc:
        log.error("login_client_start_failed", user_id=str(user_id), error=type(exc).__name__)
        await state.clear()
        command = "/telegram" if attempt.purpose == "reconnect" else "/start"
        problem = t("connect.telegram_unavailable", lang)
        await bot.send_message(
            chat_id, t("common.send_to_continue", lang, problem=problem, command=command)
        )
        return "connected"

    if attempt.purpose == "reconnect":
        await state.clear()
        await bot.send_message(chat_id, t("connect.reconnected", lang, account=label))
        # Backlog (research Q9): waiting turns that fell due while disconnected; the scheduler
        # handles them as usual. users.state is never changed by a reconnect.
        async with session_factory() as session:
            backlog = await turns_repo.count_due_waiting(session, user_id, clock())
        if backlog > 0:
            await bot.send_message(chat_id, t("connect.backlog", lang, n=backlog))
        return "connected"
    await bot.send_message(
        chat_id,
        t("connect.connected", lang, account=label),
        reply_markup=not_my_account_keyboard(lang),
    )
    await setup_flow.show_chat_picker(bot, chat_id, user, state, gateway)
    return "connected"


# --- callback ownership (T042) ------------------------------------------------------------------


def _owns_control_chat(callback: CallbackQuery, user: User | None) -> bool:
    """The press comes from the user's own control chat: a private chat whose id is
    ``users.telegram_control_chat_id``, pressed by the person that chat is with (in a private
    chat with the bot the chat id is that person's Telegram user id)."""
    message = callback.message
    return (
        user is not None
        and message is not None
        and message.chat.type == "private"
        and message.chat.id == user.telegram_control_chat_id
        and callback.from_user.id == message.chat.id
    )


class ControlChatOwnerMiddleware(BaseMiddleware):
    """Refuses login, reconnect and disconnect buttons pressed outside the owner's control chat.

    Registered after ``UserContextMiddleware``, which puts ``user`` into the handler data.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, CallbackQuery)
            and (event.data or "").startswith(_OWNED_CALLBACKS)
            and not _owns_control_chat(event, data.get("user"))
        ):
            user = data.get("user")
            log.warning(
                "login_callback_refused", user_id=str(user.id) if user is not None else None
            )
            # In the presser's language: they may not be the chat's owner.
            lang = language_for_code(event.from_user.language_code)
            await event.answer(t("connect.not_your_button", lang))
            return None
        return await handler(event, data)


# --- restart cleanup (T041, research Q10) -------------------------------------------------------


async def recover_interrupted_logins(
    bot: Bot, dispatcher: Dispatcher, session_factory: async_sessionmaker[AsyncSession]
) -> int:
    """At start-up, before polling: every chat left in a connect state with a login in progress
    gets its attempt recorded as ``interrupted`` and goes back to the method choice with the same
    purpose. ``connect_choose`` is left alone: no attempt runs there and its buttons still work
    after a restart, so repeated restarts do not repeat the notice or the record.

    The in-memory attempt died with the old process; no ``telegram_sessions`` row is written
    before the finish step, so nothing half-connected is left. A graceful shutdown has already
    recorded its pending attempts (``LoginManager.close_all``), so those are not recorded twice.
    Returns the number of chats cleaned up.
    """
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(BotFsmState).where(BotFsmState.state.in_(_INTERRUPTIBLE_STATES))
                )
            )
            .scalars()
            .all()
        )
    record = login_activity_recorder(session_factory)
    for row in rows:
        chat_id, step, purpose = row.chat_id, row.state, _purpose_of(row.data)
        state = dispatcher.fsm.get_context(bot, chat_id, chat_id)
        async with session_factory() as session:
            user = await users_repo.get_by_control_chat(session, chat_id)
            recorded = user is not None and await activity_repo.has_login_outcome_since(
                session, user.id, "interrupted", row.updated_at
            )
        if user is None:  # the account was deleted meanwhile
            await state.clear()
            continue
        if not recorded:
            await record(
                user.id,
                {
                    "method": _STATE_METHOD.get(step),
                    "purpose": purpose,
                    "outcome": "interrupted",
                    "qr_codes_shown": 0,
                    "password_needed": step == ConnectStates.connect_password.state,
                    "duration_s": None,  # the start time was in the lost attempt
                    "error_kind": None,
                },
            )
        try:
            lang = normalize_lang(user.language)
            text = t("connect.restarted", lang)
            await _back_to_choice(bot, chat_id, state, purpose, lang, text)
        except Exception as exc:  # e.g. the user blocked the bot; the state is reset anyway
            log.warning(
                "login_restart_notice_failed", user_id=str(user.id), error=type(exc).__name__
            )
    if rows:
        log.info("login_restart_cleanup", chats=len(rows))
    return len(rows)


# --- router -------------------------------------------------------------------------------------


def create_router() -> Router:
    router = Router(name="connect")
    # Commands reach the setup router (/start, /cancel) instead of being read as login input;
    # /help and /telegram are answered by routers included before this one (bot.py).
    not_command = ~Command("start", "cancel")
    router.message.register(
        command_during_login,
        StateFilter(
            ConnectStates.connect_phone, ConnectStates.connect_code, ConnectStates.connect_password
        ),
        F.text.startswith("/"),
        not_command,
    )
    for handler, step in (
        (phone_step, ConnectStates.connect_phone),
        (code_step, ConnectStates.connect_code),
    ):
        router.message.register(handler, StateFilter(step), F.text, not_command)
    # Any message in the password step is deleted first, text or not (FR-008).
    router.message.register(password_step, StateFilter(ConnectStates.connect_password), not_command)
    connecting = StateFilter(ConnectStates)
    router.callback_query.register(choose_qr, connecting, F.data == QR_CALLBACK)
    router.callback_query.register(choose_phone, connecting, F.data == PHONE_CALLBACK)
    router.callback_query.register(cancel_button, connecting, F.data == CANCEL_CALLBACK)
    # Valid until setup completes, whatever the setup step (checked in the handler).
    router.callback_query.register(not_my_account, F.data == NOT_ME_CALLBACK)
    return router


__all__ = [
    "ConnectStates",
    "ControlChatOwnerMiddleware",
    "begin_connect",
    "create_router",
    "finish_login",
    "get_purpose",
    "in_connect_state",
    "login_activity_recorder",
    "normalize_phone",
    "on_late_scan",
    "recover_interrupted_logins",
]
