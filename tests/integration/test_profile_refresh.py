"""The automatic profile refresh: the interval, the threshold of new own messages, the /settings
switch, building from the history read from Telegram (as 🔄 Rebuild does) and skipping users
whose client is not running. Fake LLM, fake gateway, offline Bot API; synthetic data only."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    Contact,
    ProfileScope,
    ProfileState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import users as users_repo
from scribe.jobs import refresh_all_users
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.style.profiles import refresh_profiles
from scribe.telegram.control_bot.render import format_callback
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import next_message_id
from tests.integration.us2_support import US2Harness, button_texts
from tests.integration.us4_support import (
    add_own_messages,
    add_profile,
    make_harness,
    profiles_of,
    style_output,
)

TOGGLE = format_callback("settings", "profile_refresh")


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("profile.build", style_output())
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


class RunningGateway(FakeMessagingGateway):
    """The fake gateway plus the client pool's ``is_running``."""

    def __init__(self, running: set[uuid.UUID]) -> None:
        super().__init__()
        self.running = running

    def is_running(self, user_id: uuid.UUID) -> bool:
        return user_id in self.running


async def _user_with_chat(
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    *,
    built_days_ago: int = 40,
) -> tuple[User, Contact, WorkChat]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic contact")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id, title="Synthetic")
    await add_profile(session_factory, user, at=clock() - timedelta(days=built_days_ago))
    return user, contact, chat


def _history(user: User, chat: WorkChat, count: int, clock: FrozenClock) -> list[IncomingMessage]:
    """``count`` own messages spread over the last 5 months, as Telegram returns them."""
    return [
        IncomingMessage(
            user_id=user.id,
            chat_id=chat.telegram_chat_id,
            message_id=next_message_id(),
            sender_peer_id=0,
            sender_display_name="",
            text=f"Synthetic older message {n}",
            has_media=False,
            sent_at=clock() - timedelta(days=150 - n),
            is_outgoing=True,
        )
        for n in range(count)
    ]


async def _refresh_records(session_factory: async_sessionmaker[AsyncSession], user: User) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count()).where(
                    ActivityRecord.user_id == user.id,
                    ActivityRecord.kind == ActivityKind.profile_refreshed,
                )
            )
            or 0
        )


async def _set_choice(
    session_factory: async_sessionmaker[AsyncSession], user: User, value: bool | None
) -> None:
    async with session_factory() as session:
        await users_repo.update_user(session, user.id, profile_auto_refresh=value)
        await session.commit()


async def test_threshold_counts_messages_since_the_profile_was_built(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
) -> None:
    tuned = settings.model_copy(update={"profile_refresh_min_new_messages": 45})
    user, contact, chat = await _user_with_chat(
        make_user, make_contact, make_chat, session_factory, clock, built_days_ago=20
    )
    # The contact's profile was built with the global one (else all 74 count for a first one).
    await add_profile(session_factory, user, contact=contact, at=clock() - timedelta(days=20))
    # Older than the profiles: not new.
    await add_own_messages(session_factory, user, chat, 30, at=clock() - timedelta(days=25))
    await add_own_messages(session_factory, user, chat, 44, at=clock() - timedelta(days=2))
    result = await refresh_profiles(
        session_factory, llm_router, user.id, settings=tuned, clock=clock
    )
    assert not result.changed
    assert fake_llm.calls_for("profile.build") == []

    await add_own_messages(session_factory, user, chat, 1, at=clock() - timedelta(days=1))
    result = await refresh_profiles(
        session_factory, llm_router, user.id, settings=tuned, clock=clock
    )
    assert result.changed and result.global_refreshed


async def test_built_from_the_telegram_history_like_a_rebuild(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
) -> None:
    user, contact, chat = await _user_with_chat(
        make_user, make_contact, make_chat, session_factory, clock
    )
    await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(days=3))
    gateway = RunningGateway({user.id})
    gateway.own_history[(user.id, chat.telegram_chat_id)] = _history(user, chat, 80, clock)

    result = await refresh_profiles(
        session_factory, llm_router, user.id, settings=settings, clock=clock, gateway=gateway
    )

    assert result.changed and result.global_refreshed
    current = await profiles_of(session_factory, user, state=ProfileState.current)
    by_scope = {(p.scope, p.contact_id): p for p in current}
    # 80 messages of the last months, not the 12 of the database window.
    assert by_scope[(ProfileScope.global_, None)].sample_size == 80
    assert by_scope[(ProfileScope.contact, contact.id)].sample_size == 80


async def test_without_a_gateway_the_database_window_is_used(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
) -> None:
    user, _, chat = await _user_with_chat(
        make_user, make_contact, make_chat, session_factory, clock
    )
    await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(days=3))

    result = await refresh_profiles(
        session_factory, llm_router, user.id, settings=settings, clock=clock
    )

    assert result.global_refreshed
    current = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.global_
    )
    assert current[0].sample_size == 12


async def test_job_respects_the_interval(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
) -> None:
    monthly = settings.model_copy(update={"profile_refresh_interval_days": 30})
    recent, _, recent_chat = await _user_with_chat(
        make_user, make_contact, make_chat, session_factory, clock, built_days_ago=20
    )
    due, _, due_chat = await _user_with_chat(
        make_user, make_contact, make_chat, session_factory, clock, built_days_ago=29
    )
    for user, chat in ((recent, recent_chat), (due, due_chat)):
        await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(days=1))

    await refresh_all_users(session_factory, llm_router, None, settings=monthly, clock=clock)

    assert await _refresh_records(session_factory, recent) == 0  # built 20 days ago
    assert await _refresh_records(session_factory, due) == 1  # 29 days: interval less a day


async def test_job_follows_the_switch_and_its_default(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
) -> None:
    off_by_default = settings.model_copy(update={"profile_auto_refresh_default": False})
    users = []
    for choice in (None, True, False):
        user, _, chat = await _user_with_chat(
            make_user, make_contact, make_chat, session_factory, clock
        )
        await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(days=1))
        await _set_choice(session_factory, user, choice)
        users.append(user)

    await refresh_all_users(session_factory, llm_router, None, settings=off_by_default, clock=clock)

    default_user, chose_on, chose_off = users
    assert await _refresh_records(session_factory, default_user) == 0
    assert await _refresh_records(session_factory, chose_on) == 1
    assert await _refresh_records(session_factory, chose_off) == 0


async def test_job_skips_users_whose_client_is_not_running(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
) -> None:
    online, _, online_chat = await _user_with_chat(
        make_user, make_contact, make_chat, session_factory, clock
    )
    offline, _, offline_chat = await _user_with_chat(
        make_user, make_contact, make_chat, session_factory, clock
    )
    gateway = RunningGateway({online.id})
    for user, chat in ((online, online_chat), (offline, offline_chat)):
        await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(days=1))
        gateway.own_history[(user.id, chat.telegram_chat_id)] = _history(user, chat, 20, clock)

    await refresh_all_users(
        session_factory, llm_router, None, settings=settings, clock=clock, gateway=gateway
    )

    assert await _refresh_records(session_factory, online) == 1
    assert await _refresh_records(session_factory, offline) == 0  # tried at the next run


async def test_settings_switch_stores_the_choice(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    assert user.profile_auto_refresh is None  # follows the default (on)

    await harness.feed(text_update(chat_id, "/settings"))
    (menu,) = harness.messages()
    assert "🔄 Automatic style profile refresh: on" in menu.text.split("\n")
    assert "🔄 Profile refresh: turn off" in button_texts(menu.reply_markup)

    await harness.feed(callback_update(chat_id, TOGGLE))
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
    assert stored is not None and stored.profile_auto_refresh is False
    assert "🔄 Automatic style profile refresh: off" in (harness.edits()[-1].text or "")

    await harness.feed(callback_update(chat_id, TOGGLE))
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
    assert stored is not None and stored.profile_auto_refresh is True
