"""Work account mode at intake (specs/008 T015, T016, T026): picking up private chats on their
first message (either direction), never bots, Saved Messages, service accounts or unpicked
groups, and exclusions. Fake LLM, fake gateway; synthetic data only."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Contact,
    ConversationTurn,
    Message,
    TurnState,
    User,
    UserState,
    WorkChat,
    WorkChatOrigin,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.intake.router import Action, IntakeRouter
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage, OutgoingEvent
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeUser, next_telegram_id
from tests.integration.us1_support import grounded_reply, next_message_id
from tests.integration.us2_support import US2Harness
from tests.integration.us4_support import make_harness

CONTACT_A = 900000101
CONTACT_B = 900000102


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


def incoming(
    user: User, chat_id: int, *, peer_kind: str | None = "user", **values: Any
) -> IncomingMessage:
    return IncomingMessage(
        user_id=user.id,
        chat_id=chat_id,
        message_id=values.pop("message_id", None) or next_message_id(),
        sender_peer_id=values.pop("sender_peer_id", chat_id),
        sender_display_name=values.pop("sender_display_name", "Test Contact A"),
        text=values.pop("text", "Synthetic question?"),
        has_media=False,
        sent_at=values.pop("sent_at"),
        chat_kind=values.pop("chat_kind", "private"),
        chat_title=values.pop("chat_title", ""),
        peer_kind=peer_kind,  # type: ignore[arg-type]
        **values,
    )


async def _user(make_user: MakeUser, session_factory: async_sessionmaker[AsyncSession], *,
                on: bool = True) -> User:  # fmt: skip
    user = await make_user(state=UserState.active)
    if on:
        result = await work_account.switch(session_factory, user.id, on=True, now=user.created_at)
        assert result.changed
    return user


async def _chat(
    session_factory: async_sessionmaker[AsyncSession], user: User, telegram_chat_id: int
) -> WorkChat | None:
    async with session_factory() as session:
        return await chats_repo.get_work_chat_by_telegram_id(session, user.id, telegram_chat_id)


async def _count(session_factory: async_sessionmaker[AsyncSession], model: Any, user: User) -> int:
    async with session_factory() as session:
        found = await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user.id)
        )
        return int(found or 0)


async def _activity(
    session_factory: async_sessionmaker[AsyncSession], user: User, kind: ActivityKind
) -> list[ActivityRecord]:
    async with session_factory() as session:
        result = await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user.id, ActivityRecord.kind == kind
            )
        )
        return list(result.scalars().all())


# --- US1: pick-up on an incoming message ---------------------------------------------------------


async def test_unknown_private_contact_is_picked_up_and_drafted(
    harness: US2Harness,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    user = await _user(make_user, session_factory)
    decision = await harness.intake.route(
        incoming(user, CONTACT_A, sent_at=clock(), sender_display_name="Test Contact A")
    )
    assert decision.action == Action.new_turn
    chat = await _chat(session_factory, user, CONTACT_A)
    assert chat is not None
    assert (chat.origin, chat.enabled, chat.excluded, chat.kind) == (
        WorkChatOrigin.auto,
        True,
        False,
        ChatKind.private,
    )
    assert chat.title == "Test Contact A"
    (added,) = await _activity(session_factory, user, ActivityKind.chats_added)
    assert added.meta_json["origin"] == "auto"
    assert added.meta_json["work_chat_ids"] == [str(chat.id)]
    assert await _count(session_factory, Message, user) == 1

    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert draft.text == "Synthetic reply"


async def test_first_time_contact_and_redelivery(
    harness: US2Harness,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    user = await _user(make_user, session_factory)
    message = incoming(user, CONTACT_B, sent_at=clock(), sender_display_name="Test Contact B")
    assert (await harness.intake.route(message)).action == Action.new_turn
    assert (await harness.intake.route(message)).action == Action.duplicate
    assert len(await _activity(session_factory, user, ActivityKind.chats_added)) == 1


@pytest.mark.parametrize("peer_kind", ["bot", "self", "service", "deleted", None])
async def test_ineligible_private_chats_store_nothing(
    harness: US2Harness,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    peer_kind: str | None,
) -> None:
    user = await _user(make_user, session_factory)
    decision = await harness.intake.route(incoming(user, CONTACT_A, sent_at=clock(),
                                                   peer_kind=peer_kind))  # fmt: skip
    assert decision.action == Action.ignore
    assert await _chat(session_factory, user, CONTACT_A) is None
    for model in (Message, Contact, WorkChat):
        assert await _count(session_factory, model, user) == 0


async def test_unpicked_group_and_mode_off_are_ignored(
    harness: US2Harness,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    on = await _user(make_user, session_factory)
    group = incoming(
        on, -100900000201, sent_at=clock(), chat_kind="group", peer_kind=None, mentions_me=True
    )
    assert (await harness.intake.route(group)).action == Action.ignore
    off = await _user(make_user, session_factory, on=False)
    assert (await harness.intake.route(incoming(off, CONTACT_A, sent_at=clock()))).action == (
        Action.ignore
    )
    for user in (on, off):
        for model in (Message, Contact, WorkChat):
            assert await _count(session_factory, model, user) == 0


async def test_two_concurrent_first_messages_make_one_chat_and_one_turn(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    session_factory = committed_session_factory
    async with session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings
        )
        user.state = UserState.active
        await session.commit()
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    router = IntakeRouter(session_factory, settings, clock=clock)
    first = incoming(user, CONTACT_A, sent_at=clock(), text="Synthetic one")
    second = incoming(user, CONTACT_A, sent_at=clock(), text="Synthetic two")
    decisions = await asyncio.gather(router.route(first), router.route(second))
    assert sorted(d.action.value for d in decisions) == ["attach", "new_turn"]
    async with session_factory() as session:
        chats = (
            await session.execute(select(WorkChat).where(WorkChat.user_id == user.id))
        ).scalars()
        assert len(list(chats)) == 1
        turns = await session.scalar(
            select(func.count())
            .select_from(ConversationTurn)
            .where(ConversationTurn.user_id == user.id)
        )
        assert turns == 1


# --- US1: pick-up on an outgoing message (the user writes first) ---------------------------------


async def test_outgoing_first_then_answer_has_the_opening_message(
    harness: US2Harness,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    clock: FrozenClock,
) -> None:
    user = await _user(make_user, session_factory)
    opening = "Synthetic opening question about the invoice"
    await harness.gateway.emit_outgoing(
        OutgoingEvent(
            user_id=user.id,
            chat_id=CONTACT_A,
            message_id=next_message_id(),
            text=opening,
            sent_at=clock(),
            chat_kind="private",
            peer_kind="user",
            chat_title="Test Contact A",
        )
    )
    chat = await _chat(session_factory, user, CONTACT_A)
    assert chat is not None and chat.origin == WorkChatOrigin.auto and chat.enabled
    assert chat.title == "Test Contact A"
    assert await _count(session_factory, Message, user) == 1

    clock.advance(minutes=1)
    await harness.intake.route(incoming(user, CONTACT_A, sent_at=clock()))
    await harness.tick()
    (draft,) = await harness.drafts(user)
    (call,) = fake_llm.calls_for("compose.draft")
    assert opening in " ".join(block.content for block in call.user_blocks)


@pytest.mark.parametrize("peer_kind", ["self", "bot"])
async def test_outgoing_to_saved_messages_or_bot_stores_nothing(
    harness: US2Harness,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    peer_kind: str,
) -> None:
    user = await _user(make_user, session_factory)
    await harness.gateway.emit_outgoing(
        OutgoingEvent(
            user_id=user.id,
            chat_id=CONTACT_A,
            message_id=next_message_id(),
            text="Synthetic note",
            sent_at=clock(),
            peer_kind=peer_kind,  # type: ignore[arg-type]
        )
    )
    assert await _count(session_factory, WorkChat, user) == 0
    assert await _count(session_factory, Message, user) == 0


# --- US2: exclusions at intake --------------------------------------------------------------------


async def test_excluded_chats_store_nothing_until_included(
    harness: US2Harness,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    from scribe.telegram.gateway import Dialog

    user = await _user(make_user, session_factory)
    # A: wrote before, then excluded. B: excluded before it ever wrote.
    await harness.intake.route(incoming(user, CONTACT_A, sent_at=clock()))
    chat_a = await _chat(session_factory, user, CONTACT_A)
    assert chat_a is not None
    await work_account.exclude_chat(session_factory, user.id, work_chat_id=chat_a.id, now=clock())
    await work_account.exclude_chat(
        session_factory,
        user.id,
        dialog=Dialog(chat_id=CONTACT_B, title="Test Contact B", kind="private"),
        now=clock(),
    )
    chat_b = await _chat(session_factory, user, CONTACT_B)
    assert chat_b is not None and chat_b.excluded and not chat_b.enabled
    stored = await _count(session_factory, Message, user)
    contacts = await _count(session_factory, Contact, user)

    clock.advance(minutes=1)
    for chat_id in (CONTACT_A, CONTACT_B):
        decision = await harness.intake.route(incoming(user, chat_id, sent_at=clock()))
        assert decision.action == Action.ignore
    assert await _count(session_factory, Message, user) == stored
    assert await _count(session_factory, Contact, user) == contacts  # B has no contact row

    assert await work_account.include_chat(session_factory, user.id, chat_b.id, now=clock())
    clock.advance(minutes=1)
    decision = await harness.intake.route(incoming(user, CONTACT_B, sent_at=clock()))
    assert decision.action == Action.new_turn
    async with session_factory() as session:
        turns = (
            await session.execute(
                select(ConversationTurn).where(ConversationTurn.work_chat_id == chat_b.id)
            )
        ).scalars()
        (turn,) = list(turns)
    assert turn.state == TurnState.waiting  # only the message after the include


async def test_disabled_picked_private_chat_stays_ignored_and_counts_as_excluded(
    harness: US2Harness,
    make_user: MakeUser,
    make_chat: MakeChat,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    await make_chat(user, telegram_chat_id=CONTACT_A, title="Test Contact A", enabled=False)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    decision = await harness.intake.route(incoming(user, CONTACT_A, sent_at=clock()))
    assert decision.action == Action.ignore
    async with session_factory() as session:
        assert await chats_repo.count_excluded(session, user.id) == 1
    assert await _count(session_factory, Message, user) == 0


async def test_pick_up_respects_a_mode_turned_off(
    session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser, clock: FrozenClock
) -> None:
    user = await _user(make_user, session_factory)
    await work_account.switch(session_factory, user.id, on=False, now=clock() + timedelta(1))
    async with session_factory() as session:
        chat = await work_account.pick_up(
            session,
            user.id,
            telegram_chat_id=CONTACT_A,
            title="Test Contact A",
            chat_kind="private",
            peer_kind="user",
            direction="in",
            now=clock(),
        )
    assert chat is None


async def test_work_account_logs_carry_ids_and_counts_only(
    harness: US2Harness,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    log_capture: LogCapture,
) -> None:
    """T051 (Principle II): no chat title or message text in the new log events."""
    user = await _user(make_user, session_factory)
    await harness.intake.route(
        incoming(user, CONTACT_A, sent_at=clock(), sender_display_name="Secret Title Synthetic",
                 text="Secret body synthetic")
    )  # fmt: skip
    await harness.intake.route(incoming(user, CONTACT_B, sent_at=clock(), peer_kind="bot"))
    chat = await _chat(session_factory, user, CONTACT_A)
    assert chat is not None
    await work_account.exclude_chat(session_factory, user.id, work_chat_id=chat.id, now=clock())
    await work_account.include_chat(session_factory, user.id, chat.id, now=clock())
    await work_account.switch(session_factory, user.id, on=False, now=clock())
    assert "Secret" not in log_capture.raw
    allowed = {"event", "level", "timestamp", "logger", "user_id", "work_chat_id", "direction",
               "origin", "on", "paused_chats", "dropped_drafts", "dropped_waiting",
               "enabled_chats", "peer_kind", "fixed"}  # fmt: skip
    for name in ("work_chat_picked_up", "work_chat_excluded", "work_chat_included",
                 "work_account_switched", "work_account_ineligible"):  # fmt: skip
        events = log_capture.events(name)
        assert events, name
        for event in events:
            assert set(event) <= allowed, (name, set(event) - allowed)
