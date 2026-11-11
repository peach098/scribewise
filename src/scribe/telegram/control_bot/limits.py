"""Usage limits in the control bot (docs/plugins.md): the user's :class:`UsageLimits` from the
resolver in the dispatcher's workflow data (``limits``; default: the deployment caps and no
other limit) and the replies when a limit refuses something, with the buttons of the offer
provider (``offer_provider``) when a plugin set one.

With a plugin's resolver, :class:`UsageLimitsMiddleware` resolves the limits of the update's
user once per update and keeps them for the code that renders without a session
(:func:`current_usage`, e.g. the /settings text). With the default resolver no limit applies,
the middleware is not installed and none of these replies is ever sent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardMarkup, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import OfferProvider, offer_rows
from scribe.clock import Clock, utcnow
from scribe.config import Settings, get_settings
from scribe.db.models import User
from scribe.db.repos import chats as chats_repo
from scribe.drafting.budget import (
    BudgetCaps,
    LimitsResolver,
    SettingsLimitsResolver,
    UsageLimits,
    default_resolver,
    user_limits,
)
from scribe.i18n import t
from scribe.logging import get_logger

log = get_logger("scribe.control_bot")

_CURRENT: ContextVar[UsageLimits | None] = ContextVar("scribe_usage_limits", default=None)


def current_usage() -> UsageLimits | None:
    """The limits of the user of the update being handled (None: default resolver, or no
    account)."""
    return _CURRENT.get()


class UsageLimitsMiddleware(BaseMiddleware):
    """After the user lookup: the user's :class:`UsageLimits` for the rest of the update."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        limits: LimitsResolver,
        clock: Clock = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits
        self._clock = clock

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        if not isinstance(user, User):
            return await handler(event, data)
        async with self._session_factory() as session:
            usage = await user_limits(session, user, self._limits, self._clock())
        token = _CURRENT.set(usage)
        try:
            return await handler(event, data)
        finally:
            _CURRENT.reset(token)


def resolver_of(limits: LimitsResolver | None, settings: Settings | None) -> LimitsResolver:
    """``limits``, else the deployment's default resolver."""
    if limits is not None:
        return limits
    try:
        return default_resolver(None, settings or get_settings())
    except Exception:  # no complete configuration (e.g. a test): no limits at all
        return SettingsLimitsResolver(BudgetCaps(0, 0))


async def usage_of(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    limits: LimitsResolver | None,
    settings: Settings | None = None,
    clock: Clock = utcnow,
) -> UsageLimits:
    """The user's limits: from ``limits`` when given, else those of the update being handled,
    else the deployment's defaults (no query)."""
    if limits is None and (known := current_usage()) is not None:
        return known
    resolver = resolver_of(limits, settings)
    if isinstance(resolver, SettingsLimitsResolver):
        return resolver.limits
    async with session_factory() as session:
        return await user_limits(session, user, resolver, clock())


async def chat_slots(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    limits: LimitsResolver | None,
    settings: Settings | None = None,
    clock: Clock = utcnow,
) -> tuple[int | None, int]:
    """(the user's ``max_work_chats`` or None, how many more chats may be enabled now)."""
    usage = await usage_of(session_factory, user, limits, settings, clock)
    if usage.max_work_chats is None:
        return None, 0
    async with session_factory() as session:
        enabled = await chats_repo.count_enabled(session, user.id)
    return usage.max_work_chats, max(usage.max_work_chats - enabled, 0)


async def offer_markup(
    offer: OfferProvider | None,
    session_factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    lang: str,
) -> InlineKeyboardMarkup | None:
    if offer is None:
        return None
    async with session_factory() as session:
        return await offer_rows(offer, session, telegram_id, lang)


async def send_refusal(
    bot: Bot,
    user: User,
    text: str,
    offer: OfferProvider | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A limit's refusal in the control chat, with the offer provider's buttons."""
    chat_id = user.telegram_control_chat_id
    markup = await offer_markup(offer, session_factory, chat_id, user.language)
    await bot.send_message(chat_id, text, reply_markup=markup)


async def refuse_chat_limit(
    bot: Bot,
    user: User,
    limit: int,
    offer: OfferProvider | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log.info("work_chat_limit_reached", user_id=str(user.id), limit=limit)
    text = t("chats.limit_reached", user.language, n=limit)
    await send_refusal(bot, user, text, offer, session_factory)


__all__ = [
    "UsageLimitsMiddleware",
    "chat_slots",
    "current_usage",
    "offer_markup",
    "refuse_chat_limit",
    "resolver_of",
    "send_refusal",
    "usage_of",
]
