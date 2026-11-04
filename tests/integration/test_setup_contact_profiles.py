"""Issue #4: setup builds the same profiles as 🔄 Rebuild (spec 001 FR-034, FR-035): the global
profile plus per-contact profiles of the top contacts, with ``own_message_count_6m`` set from the
import. A failed contact build never loses the global profile. Fake LLM and gateway, offline Bot
API; synthetic data only."""

from __future__ import annotations

from datetime import timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import ProfileScope, ProfileState, User, UserState
from scribe.db.repos import chats as chats_repo
from scribe.i18n import t
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import ProviderOutputInvalid, StyleProfileOutput
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot.setup_flow import _import_and_finish
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeUser, next_telegram_id
from tests.integration.bot_support import offline_bot
from tests.integration.us1_support import next_message_id
from tests.integration.us4_support import profiles_of, scope_of, style_output


async def _run_setup(
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> str:
    bot, api = offline_bot()
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot.id, user.telegram_control_chat_id, 0)
    )
    await _import_and_finish(
        bot,
        user.telegram_control_chat_id,
        state,
        user,
        session_factory,
        settings,
        llm_router,
        gateway,
        clock,
    )
    edits = [r for r in api.requests if isinstance(r, EditMessageText) and r.text]
    return str(edits[0].text)  # the "learned from N messages" note


async def _chats_with_history(
    user: User,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    counts: list[int],
) -> list[int]:
    """One enabled private chat per entry of ``counts`` with that many own messages."""
    chat_ids: list[int] = []
    for n, count in enumerate(counts):
        chat = await make_chat(user, telegram_chat_id=next_telegram_id(), title=f"Contact {n:02d}")
        chat_ids.append(chat.telegram_chat_id)
        gateway.own_history[(user.id, chat.telegram_chat_id)] = [
            IncomingMessage(
                user_id=user.id,
                chat_id=chat.telegram_chat_id,
                message_id=next_message_id(),
                sender_peer_id=0,
                sender_display_name="me",
                text=f"Synthetic own text {i}",
                has_media=False,
                sent_at=clock() - timedelta(days=1 + i % 60),
                is_outgoing=True,
            )  # fmt: skip
            for i in range(count)
        ]
    return chat_ids


async def test_setup_builds_contact_profiles_and_counts(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.setting_up)
    # 12 and 30 own messages get a profile; 4 is too few (global profile applies).
    chat_ids = await _chats_with_history(user, make_chat, gateway, clock, [12, 30, 4])
    fake_llm.set_response(
        "profile.build", lambda c: style_output("Global." if scope_of(c) == "global" else "Own.")
    )

    note = await _run_setup(user, session_factory, settings, llm_router, gateway, clock)

    assert note.startswith(t("setup.learned", n=46))
    assert t("setup.contacts", n=2) in note
    async with session_factory() as session:
        contacts = {c.telegram_peer_id: c for c in await chats_repo.list_contacts(session, user.id)}
    assert [contacts[c].own_message_count_6m for c in chat_ids] == [12, 30, 4]
    current = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    assert {p.contact_id for p in current} == {contacts[chat_ids[0]].id, contacts[chat_ids[1]].id}
    (global_profile,) = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.global_
    )
    assert global_profile.sample_size > 0
    # The user is waiting: every build is a synchronous call, never the batch API.
    assert all(not c.batch for c in fake_llm.calls_for("profile.build"))


async def test_setup_caps_contact_profiles_at_the_top_20(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.setting_up)
    counts = [30 + i for i in range(25)]  # 25 eligible chats; the 5 smallest stay on global
    chat_ids = await _chats_with_history(user, make_chat, gateway, clock, counts)
    fake_llm.set_response("profile.build", style_output())

    note = await _run_setup(user, session_factory, settings, llm_router, gateway, clock)

    assert t("setup.contacts", n=settings.max_contact_profiles) in note
    async with session_factory() as session:
        contacts = {c.telegram_peer_id: c for c in await chats_repo.list_contacts(session, user.id)}
    current = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    assert {p.contact_id for p in current} == {contacts[c].id for c in chat_ids[5:]}
    # Every contact's count is stored, so weekly refreshes rank them correctly.
    assert [contacts[c].own_message_count_6m for c in chat_ids] == counts


async def test_setup_keeps_global_profile_when_contact_builds_fail(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.setting_up)
    await _chats_with_history(user, make_chat, gateway, clock, [15, 20])

    def respond(call: FakeCall) -> StyleProfileOutput:
        if scope_of(call) == "global":
            return style_output("Global.")
        raise ProviderOutputInvalid("broken", truncated=False)

    fake_llm.set_response("profile.build", respond)

    note = await _run_setup(user, session_factory, settings, llm_router, gateway, clock)

    assert t("setup.learned", n=35) in note
    assert t("setup.contacts_failed", n=2) in note
    (global_profile,) = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.global_
    )
    assert global_profile.summary_text == "Global."
    assert not await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    async with session_factory() as session:
        assert await chats_repo.list_contacts(session, user.id)  # counts were still stored


async def test_setup_reports_contact_profiles_when_global_build_fails(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.setting_up)
    await _chats_with_history(user, make_chat, gateway, clock, [15, 20])

    def respond(call: FakeCall) -> StyleProfileOutput:
        if scope_of(call) == "global":
            raise ProviderOutputInvalid("broken", truncated=False)
        return style_output("Own.")

    fake_llm.set_response("profile.build", respond)

    note = await _run_setup(user, session_factory, settings, llm_router, gateway, clock)

    assert note.startswith(t("setup.learn_failed"))
    assert t("setup.contacts", n=2) in note
    # The neutral default stands in for the failed global build; the contacts keep theirs.
    assert await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.global_
    )
    assert (
        len(
            await profiles_of(
                session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
            )
        )
        == 2
    )
