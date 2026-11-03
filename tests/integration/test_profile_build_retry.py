"""Issue #30: a truncated ``profile.build`` answer is retried once with a lighter request; the
global and the contact profiles fail independently; the import marks group-chat samples; and
``sample_size`` is what was sent while ``weak`` follows the history. Fake LLM, fake gateway,
offline Bot API; synthetic data only."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import ChatKind, ProfileScope, ProfileState, User, UserState
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import ProviderOutputInvalid, StyleProfileOutput, Usage
from scribe.llm.registry import LLMRouter
from scribe.style.importer import import_own_messages
from scribe.style.profiles import (
    build_global_profile,
    build_profiles_from_import,
    refresh_profiles,
)
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeUser, next_telegram_id
from tests.integration.bot_support import callback_update
from tests.integration.us1_support import next_message_id
from tests.integration.us2_support import US2Harness
from tests.integration.us4_support import (
    add_own_messages,
    add_profile,
    make_harness,
    profiles_of,
    scope_of,
    style_output,
)


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


def _truncated() -> ProviderOutputInvalid:
    return ProviderOutputInvalid(
        "StyleProfileOutput output truncated at max tokens",
        truncated=True,
        usage=Usage(output_tokens=2000),
        finish_reason="length",
    )


def _sample_count(call: FakeCall) -> int:
    (block,) = [b for b in call.user_blocks if b.name == "samples"]
    return len(json.loads(block.content))


def _own_history(
    gateway: FakeMessagingGateway, user: User, chat_id: int, n: int, clock: FrozenClock
) -> None:
    gateway.own_history[(user.id, chat_id)] = [
        IncomingMessage(
            user_id=user.id, chat_id=chat_id, message_id=next_message_id(), sender_peer_id=0,
            sender_display_name="me", text=f"Synthetic own text number {i}", has_media=False,
            sent_at=clock() - timedelta(days=10, minutes=i), is_outgoing=True,
        )
        for i in range(n)
    ]  # fmt: skip


async def _two_chats(
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> tuple[User, int, int]:
    """A group with 15 own messages and a private chat with 12."""
    user = await make_user(state=UserState.active)
    group = await make_chat(user, kind=ChatKind.group, title="Synthetic Team")
    person_id = next_telegram_id()
    await make_chat(user, telegram_chat_id=person_id, title="Synthetic Ivan")
    _own_history(gateway, user, group.telegram_chat_id, 15, clock)
    _own_history(gateway, user, person_id, 12, clock)
    return user, group.telegram_chat_id, person_id


async def test_import_marks_group_chat_samples(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user, group_id, person_id = await _two_chats(make_user, make_chat, gateway, clock)
    imported = await import_own_messages(
        gateway, session_factory, user.id, settings=settings, clock=clock
    )
    assert all(s.group and s.chat_id == group_id for s in imported.by_chat[group_id])
    assert all(not s.group and s.chat_id == person_id for s in imported.by_chat[person_id])


async def test_rebuild_builds_contact_profiles_when_the_global_build_fails(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user, _, _ = await _two_chats(make_user, make_chat, gateway, clock)
    old_global = await add_profile(session_factory, user, summary="Old global.")

    def respond(call: FakeCall) -> StyleProfileOutput:
        if scope_of(call) == "global":
            raise _truncated()  # also the lighter retry
        return style_output("Contact.")

    fake_llm.set_response("profile.build", respond)
    imported = await import_own_messages(
        gateway, session_factory, user.id, settings=settings, clock=clock
    )
    summary = await build_profiles_from_import(
        session_factory, llm_router, user.id, imported, settings=settings, clock=clock
    )

    assert summary.global_profile is None and summary.contact_profiles == 2
    global_calls = [c for c in fake_llm.calls_for("profile.build") if scope_of(c) == "global"]
    assert [(c.effort, _sample_count(c)) for c in global_calls] == [("medium", 27), ("low", 13)]
    (current_global,) = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.global_
    )
    assert current_global.id == old_global.id  # nothing changed for the global profile
    contacts = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    assert len(contacts) == 2


async def test_rebuild_note_when_only_the_global_profile_failed(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user, _, _ = await _two_chats(make_user, make_chat, gateway, clock)

    def respond(call: FakeCall) -> StyleProfileOutput:
        if scope_of(call) == "global":
            raise _truncated()
        return style_output("Contact.")

    fake_llm.set_response("profile.build", respond)
    await harness.feed(callback_update(user.telegram_control_chat_id, "profrebuild:1"))
    done = harness.messages()[-1].text
    assert done.startswith("⚠️ I couldn't rebuild your global profile right now")
    assert "Rebuilt: 2 contact profiles." in done


async def test_global_retry_success_stores_what_was_sent(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    from scribe.llm.provider import StyleSample

    user = await make_user(state=UserState.active)
    samples = [
        StyleSample(text=f"Synthetic line number {i}", sent_at=clock() - timedelta(minutes=i))
        for i in range(80)
    ]

    def respond(call: FakeCall) -> StyleProfileOutput:
        if call.effort == "medium":
            raise _truncated()
        return style_output("Lighter.")

    fake_llm.set_response("profile.build", respond)
    profile = await build_global_profile(
        session_factory, llm_router, user.id, samples, settings=settings, clock=clock
    )
    assert profile.summary_text == "Lighter."
    assert profile.sample_size == 40  # the retry's half of the 80
    assert profile.rules_json["weak"] is False  # 80 own messages: weak follows the history


async def test_global_build_failing_twice_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    from scribe.llm.provider import StyleSample

    user = await make_user(state=UserState.active)
    old = await add_profile(session_factory, user, summary="Old global.")
    fake_llm.set_response("profile.build", _truncated())
    samples = [StyleSample(text=f"Synthetic line number {i}", sent_at=clock()) for i in range(60)]
    with pytest.raises(ProviderOutputInvalid):
        await build_global_profile(
            session_factory, llm_router, user.id, samples, settings=settings, clock=clock
        )
    assert len(fake_llm.calls_for("profile.build")) == 2
    (current,) = await profiles_of(session_factory, user, state=ProfileState.current)
    assert current.id == old.id


async def test_refresh_retries_truncated_batch_items_synchronously(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.active)
    await add_profile(session_factory, user, summary="Old global.")
    chat = await make_chat(user, title="Synthetic Olga")
    clock.advance(days=1)
    await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(hours=1))

    def respond(call: FakeCall) -> StyleProfileOutput:
        if call.batch:
            raise _truncated()
        return style_output("Refreshed.")

    fake_llm.set_response("profile.build", respond)
    result = await refresh_profiles(
        session_factory, llm_router, user.id, settings=settings, clock=clock
    )

    assert result.global_refreshed and result.contacts_refreshed == 1
    calls = fake_llm.calls_for("profile.build")
    assert [c.batch for c in calls] == [True, True, False, False]
    assert all(c.effort == "low" for c in calls if not c.batch)
