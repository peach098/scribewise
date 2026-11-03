"""``/telegram``: connection status card, Disconnect and Reconnect (002 contracts/control-chat.md
"`/telegram` status card"; takes over 001 T071).

- **Disconnect** (``tgdisconnect:0`` -> confirm ``tgdisconnect:1``) logs the session out and
  marks it ``revoked``; ``users.state`` and waiting turns are left as they are (research Q9), so
  a disconnect is handled exactly like a session ended from Telegram.
- **Reconnect** (``tgreconnect:0``, also on the revoked / error notices and on ``/start``) runs
  the connect sub-flow with ``purpose="reconnect"`` (``connect_flow.begin_connect``).

Texts are in the user's language (``scribe.i18n``).
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import SessionStatus, User, UserState
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, t
from scribe.logging import get_logger
from scribe.telegram.control_bot import connect_flow
from scribe.telegram.control_bot.render import (
    disconnect_confirm_keyboard,
    disconnect_keyboard,
    format_callback,
    reconnect_keyboard,
    telegram_status_card,
)
from scribe.telegram.gateway import MessagingGateway
from scribe.telegram.login import LoginPurpose

log = get_logger("scribe.control_bot")

DISCONNECT_CALLBACK = format_callback("tgdisconnect", 0)
DISCONNECT_CONFIRM_CALLBACK = format_callback("tgdisconnect", 1)
RECONNECT_CALLBACK = format_callback("tgreconnect", 0)


async def telegram_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
) -> None:
    if user is None:
        await message.answer(t("telegram.not_connected", lang))
        return
    async with session_factory() as session:
        row = await users_repo.get_telegram_session(session, user.id)
    if row is None:
        await message.answer(t("telegram.not_connected", lang))
        return
    # Sessions stored before the account label existed fall back to the control-chat name.
    card = telegram_status_card(
        account=row.account_label or user.display_name,
        status=row.status.value,
        logged_out_elsewhere=row.last_error is not None,
        login_method=row.login_method.value,
        connected_at=row.connected_at,
        tz=user.timezone,
        device_model=settings.tg_device_model,
        lang=lang,
    )
    active = row.status == SessionStatus.active
    markup = disconnect_keyboard(lang) if active else reconnect_keyboard(lang)
    await message.answer(card, reply_markup=markup)


async def _session_status(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> SessionStatus | None:
    async with session_factory() as session:
        row = await users_repo.get_telegram_session(session, user.id)
    return row.status if row is not None else None


async def disconnect_ask(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    """``tgdisconnect:0``: first step, ask to confirm."""
    await callback.answer()
    if user is None or callback.message is None:
        return
    chat_id = callback.message.chat.id
    if await _session_status(session_factory, user) != SessionStatus.active:
        await _send_disconnected(bot, chat_id, lang)
        return
    await bot.send_message(
        chat_id,
        t("telegram.disconnect_confirm", lang),
        reply_markup=disconnect_confirm_keyboard(lang),
    )


async def _send_disconnected(bot: Bot, chat_id: int, lang: str) -> None:
    text = t("telegram.disconnected", lang)
    await bot.send_message(chat_id, text, reply_markup=reconnect_keyboard(lang))


async def disconnect_confirm(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    lang: Lang,
) -> None:
    """``tgdisconnect:1``: log out, session ``revoked``; ``users.state`` is not changed."""
    await callback.answer()
    if user is None or callback.message is None:
        return
    chat_id = callback.message.chat.id
    if await _session_status(session_factory, user) != SessionStatus.active:
        await _send_disconnected(bot, chat_id, lang)
        return
    try:
        await gateway.log_out(user.id)
    except Exception as exc:
        log.error("telegram_disconnect_failed", user_id=str(user.id), error=type(exc).__name__)
        await bot.send_message(chat_id, t("connect.telegram_unavailable", lang))
        return
    async with session_factory() as session:
        await users_repo.set_telegram_session_status(session, user.id, SessionStatus.revoked)
        await session.commit()
    log.info("telegram_disconnected", user_id=str(user.id))
    await _send_disconnected(bot, chat_id, lang)


async def reconnect_button(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    """``tgreconnect:0``: the method choice with ``purpose="reconnect"`` (session not active)."""
    if user is None or callback.message is None:
        await callback.answer()
        return
    if await _session_status(session_factory, user) == SessionStatus.active:
        await callback.answer(t("telegram.already_connected", lang))
        return
    await callback.answer()
    # A user still in setup whose session ended goes back through setup (then the chat picker).
    purpose: LoginPurpose = "setup" if user.state == UserState.setting_up else "reconnect"
    await connect_flow.begin_connect(bot, callback.message.chat.id, user, state, purpose)


def create_router() -> Router:
    router = Router(name="telegram")
    router.message.register(telegram_command, Command("telegram"))
    router.callback_query.register(disconnect_ask, F.data == DISCONNECT_CALLBACK)
    router.callback_query.register(disconnect_confirm, F.data == DISCONNECT_CONFIRM_CALLBACK)
    router.callback_query.register(reconnect_button, F.data == RECONNECT_CALLBACK)
    return router


__all__ = ["create_router", "telegram_command"]
