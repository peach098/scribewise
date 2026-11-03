"""US2 (T042): messages stay unread until their random delay has passed inside working hours;
manual reply, read elsewhere, read now, pause, the outdated banner and turn extension."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    Contact,
    Draft,
    DraftState,
    DroppedReason,
    Message,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import users as users_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import US2Harness, button_texts, callback_datas

REPLY = "Synthetic reply about the report"


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    return US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _setup(
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    **user_values: object,
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active, **user_values)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    return user, chat, contact


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


async def test_message_stays_unread_until_due(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=180, delay_max_s=300
    )
    arrived = clock()
    incoming = await harness.incoming(user, chat, contact, "When is the report due?")
    (turn,) = await harness.turns(user)
    assert arrived + timedelta(seconds=180) <= turn.due_at <= arrived + timedelta(seconds=300)

    for moment in (arrived, arrived + timedelta(seconds=179), turn.due_at - timedelta(seconds=1)):
        await harness.at(moment)
        assert gateway.mark_read_calls == []  # FR-016: never read before the delay
        assert await harness.drafts(user) == []
        assert harness.messages() == []

    await harness.at(turn.due_at)
    assert [(c.chat_id, c.up_to_message_id) for c in gateway.mark_read_calls] == [
        (chat.telegram_chat_id, incoming.message_id)
    ]
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending
    (card,) = harness.messages()
    assert REPLY in card.text
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.pending_review and turn.read_at == turn.due_at


async def test_outside_working_hours_waits_for_next_window(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    # 22:00 on Wednesday in Berlin (UTC+2 in October): hours are 09:00-19:00 local.
    user, chat, contact = await _setup(make_user, make_chat, make_contact, timezone="Europe/Berlin")
    clock.set(datetime(2026, 10, 14, 20, 0, tzinfo=UTC))
    await harness.incoming(user, chat, contact, "Are you around?")
    (turn,) = await harness.turns(user)
    next_morning = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)  # Thursday 09:00 Berlin
    assert turn.due_at == next_morning

    for moment in (clock() + timedelta(hours=1), next_morning - timedelta(minutes=1)):
        await harness.at(moment)
        assert gateway.mark_read_calls == []
        assert await harness.drafts(user) == []

    await harness.at(next_morning + timedelta(seconds=5))
    assert len(gateway.mark_read_calls) == 1
    assert len(await harness.drafts(user)) == 1


async def test_manual_reply_drops_turn(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=300, delay_max_s=300
    )
    # 1. A waiting message: the user answers from their phone before the delay is over.
    await harness.incoming(user, chat, contact, "Can you check the invoice?")
    clock.advance(seconds=60)
    outgoing_id = await harness.manual_reply(user, chat, "Checked, all fine")

    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.dropped
    assert turn.dropped_reason == DroppedReason.manual_reply
    assert harness.notifications == [
        (user.id, "✅ You replied to Synthetic Anna yourself — dropped the waiting message/draft.")
    ]
    async with session_factory() as session:
        stored = (
            await session.execute(
                select(Message).where(
                    Message.work_chat_id == chat.id, Message.telegram_message_id == outgoing_id
                )
            )
        ).scalar_one()
        assert stored.is_outgoing and stored.text == "Checked, all fine"

    await harness.at(clock() + timedelta(minutes=10))
    assert gateway.mark_read_calls == []  # never read, never drafted
    assert await harness.drafts(user) == []

    # 2. A pending draft: a manual reply drops the draft and closes its card.
    await harness.incoming(user, chat, contact, "One more question")
    await harness.at(clock() + timedelta(minutes=10))
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending
    harness.notifications.clear()

    await harness.manual_reply(user, chat)

    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.dropped and draft.finalized_at is not None
    second = (await harness.turns(user))[-1]
    assert second.state == TurnState.dropped
    assert second.dropped_reason == DroppedReason.manual_reply
    assert len(harness.notifications) == 1
    (edit,) = harness.edits()
    assert edit.message_id == draft.control_message_id
    assert edit.text is not None and "You replied yourself — dropped" in edit.text
    assert edit.reply_markup is None
    assert len(await _activity(session_factory, user, ActivityKind.dropped)) == 2

    # The approve button of the dropped card does nothing.
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    assert gateway.send_text_calls == []


async def test_read_elsewhere_shortens_wait(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=600, delay_max_s=600
    )
    incoming = await harness.incoming(user, chat, contact, "Ping")

    await harness.read_elsewhere(user, chat, max_message_id=incoming.message_id - 1)  # older only
    (turn,) = await harness.turns(user)
    assert turn.due_at == incoming.sent_at + timedelta(seconds=600)

    clock.advance(seconds=30)
    await harness.read_elsewhere(user, chat, max_message_id=incoming.message_id)
    (turn,) = await harness.turns(user)
    assert turn.due_at == clock()
    await harness.tick()
    assert len(gateway.mark_read_calls) == 1
    (first,) = await harness.drafts(user)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"reject:{first.id}"))

    # Outside working hours too: once the user has read it, holding it serves no purpose.
    clock.set(datetime(2026, 10, 14, 22, 0, tzinfo=UTC))
    late = await harness.incoming(user, chat, contact, "Late question")
    (_, turn) = await harness.turns(user)
    assert turn.due_at.date() == datetime(2026, 10, 15, tzinfo=UTC).date()
    await harness.read_elsewhere(user, chat, max_message_id=late.message_id)
    await harness.tick()
    assert gateway.mark_read_calls[-1].up_to_message_id == late.message_id
    assert len(await harness.drafts(user)) == 2


async def test_outdated_on_new_message(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "When is the report due?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    harness.api.clear()

    follow_up = await harness.incoming(user, chat, contact, "Also, is it in English?")

    (turn,) = await harness.turns(user)  # attached to the pending turn, no new turn
    assert turn.state == TurnState.pending_review
    (draft,) = await harness.drafts(user)
    assert draft.outdated and draft.state == DraftState.pending
    (edit,) = harness.edits()
    assert edit.message_id == draft.control_message_id
    assert edit.text is not None and edit.text.startswith("⚠️ New messages arrived")
    assert "Also, is it in English?" in edit.text
    datas = callback_datas(edit.reply_markup)
    assert f"regen:{draft.id}" in datas and f"approve:{draft.id}:anyway" in datas
    assert f"approve:{draft.id}" not in datas
    assert "Send anyway" in button_texts(edit.reply_markup)
    assert "✅ Send" not in button_texts(edit.reply_markup)

    # Regenerate covers both messages and reads the chat up to the new one.
    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{draft.id}"))
    old, new = await harness.drafts(user)
    assert old.state == DraftState.superseded
    assert new.state == DraftState.pending and not new.outdated and new.version == 2
    last_compose = fake_llm.calls_for("compose.draft")[-1]
    incoming_block = next(b for b in last_compose.user_blocks if b.name == "incoming_messages")
    assert "When is the report due?" in incoming_block.content
    assert "Also, is it in English?" in incoming_block.content
    assert gateway.mark_read_calls[-1].up_to_message_id == follow_up.message_id

    # Another message makes the new version outdated; "Send anyway" sends it via approval.
    await harness.incoming(user, chat, contact, "Thanks in advance")
    (_, new) = await harness.drafts(user)
    assert new.outdated
    assert gateway.send_text_calls == []
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{new.id}:anyway"))
    (sent,) = gateway.send_text_calls
    assert sent.text == REPLY
    (_, new) = await harness.drafts(user)
    assert new.state == DraftState.sent and new.approved_by_user_at is not None


async def test_read_now_skips_delay(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=3600, delay_max_s=3600
    )
    clock.set(datetime(2026, 10, 14, 10, 0, tzinfo=UTC))
    await harness.incoming(user, chat, contact, "Need the numbers for the Q3 summary")
    (turn,) = await harness.turns(user)

    await harness.feed(text_update(user.telegram_control_chat_id, "/waiting"))
    (item,) = [m for m in harness.messages() if "will be read at" in m.text]
    assert "Synthetic Anna · arrived 10:00 · will be read at 11:00" in item.text
    assert "› Need the numbers for the Q3 summary" in item.text
    assert callback_datas(item.reply_markup) == [f"readnow:{turn.id}"]

    # Another user cannot read this user's turn.
    other = await make_user(state=UserState.active)
    await harness.feed(callback_update(other.telegram_control_chat_id, f"readnow:{turn.id}"))
    (turn,) = await harness.turns(user)
    assert turn.due_at == clock() + timedelta(hours=1)

    clock.advance(seconds=20)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"readnow:{turn.id}"))
    (turn,) = await harness.turns(user)
    assert turn.due_at == clock()
    assert len(await _activity(session_factory, user, ActivityKind.read_now)) == 1

    await harness.tick()
    assert len(gateway.mark_read_calls) == 1
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending

    harness.api.clear()
    await harness.feed(text_update(user.telegram_control_chat_id, "/waiting"))
    assert any("Nothing is waiting" in text for text in harness.bot_texts())


async def test_paused_user_leaves_messages_unread(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.feed(text_update(user.telegram_control_chat_id, "/pause"))
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None and stored.state == UserState.paused
    assert any("Paused" in text for text in harness.bot_texts())

    await harness.incoming(user, chat, contact, "Are you there?")
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.waiting  # still received and stored
    for _ in range(3):
        await harness.at(clock() + timedelta(minutes=30))
    assert gateway.mark_read_calls == []  # never read while paused
    assert await harness.drafts(user) == []

    await harness.feed(text_update(user.telegram_control_chat_id, "/resume"))
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None and stored.state == UserState.active
    await harness.tick()
    assert len(gateway.mark_read_calls) == 1
    assert len(await harness.drafts(user)) == 1


async def test_new_messages_while_waiting_extend_turn_without_moving_due_at(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=300, delay_max_s=300
    )
    await harness.incoming(user, chat, contact, "First part")
    (turn,) = await harness.turns(user)
    due_at = turn.due_at
    clock.advance(seconds=30)
    second = await harness.incoming(user, chat, contact, "Second part")

    (turn,) = await harness.turns(user)
    assert turn.due_at == due_at and turn.state == TurnState.waiting
    await harness.at(due_at)
    assert [c.up_to_message_id for c in gateway.mark_read_calls] == [second.message_id]
    (draft,) = await harness.drafts(user)
    assert isinstance(draft, Draft)
    block = next(
        b for b in fake_llm.calls_for("compose.draft")[-1].user_blocks
        if b.name == "incoming_messages"
    )  # fmt: skip
    assert "First part" in block.content and "Second part" in block.content
    assert len([m for m in harness.messages() if isinstance(m, SendMessage)]) == 1
