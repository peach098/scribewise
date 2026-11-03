"""Control bot skeleton (T020): /start creates the user, /help lists commands, middleware."""

from __future__ import annotations

from aiogram import Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import User, UserState
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from tests.conftest import MakeUser, next_telegram_id
from tests.integration.bot_support import offline_bot, text_update


def _dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    routers: list[Router] | None = None,
) -> Dispatcher:
    return create_dispatcher(
        session_factory=session_factory,
        settings=settings,
        storage=PostgresFSMStorage(session_factory),
        routers=routers,
    )


async def test_start_creates_user_in_setting_up_once(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    chat_id = next_telegram_id()
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, text_update(chat_id, "/start"))
    await dispatcher.feed_update(bot, text_update(chat_id, "/start"))

    user = await users_repo.get_by_control_chat(db_session, chat_id)
    assert user is not None
    assert user.state == UserState.setting_up
    assert user.display_name == "Synthetic User"
    assert (user.delay_min_s, user.delay_max_s) == (settings.delay_min_s, settings.delay_max_s)
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.telegram_control_chat_id == chat_id)
    )
    assert count == 1
    # The second /start ends the (empty) first attempt and shows the method choice again.
    assert api.sent_texts == [
        t("start.welcome"),
        t("connect.method_choice"),
        t("connect.method_choice"),
    ]


async def test_start_for_active_user(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, make_user: MakeUser
) -> None:
    user = await make_user(state=UserState.active)
    bot, api = offline_bot()
    await _dispatcher(session_factory, settings).feed_update(
        bot, text_update(user.telegram_control_chat_id, "/start")
    )
    assert api.sent_texts == [t("start.already_set_up")]


async def test_help_lists_commands(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    bot, api = offline_bot()
    await _dispatcher(session_factory, settings).feed_update(
        bot, text_update(next_telegram_id(), "/help")
    )
    (reply,) = api.sent_texts
    assert "/start" in reply and "/delete_my_data" in reply


async def test_middleware_resolves_user_and_ignores_groups(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, make_user: MakeUser
) -> None:
    user = await make_user()
    seen: list[object] = []
    router = Router()

    @router.message(Command("whoami"))
    async def whoami(message: Message, user_id: object) -> None:
        seen.append(user_id)

    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, settings, [router])
    await dispatcher.feed_update(bot, text_update(user.telegram_control_chat_id, "/whoami"))
    await dispatcher.feed_update(bot, text_update(next_telegram_id(), "/whoami"))
    await dispatcher.feed_update(bot, text_update(-100123, "/help", chat_type="group"))
    assert seen == [user.id, None]
    assert api.sent_texts == []  # nothing answered in the group
