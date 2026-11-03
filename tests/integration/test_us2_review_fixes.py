"""US2 races and edge cases from the Phase 4 review: messages that arrive while a draft is being
composed, manual replies racing incoming messages, stranded ``reading`` turns, re-planning after
"read now", restart requeue inside working hours, "Send anyway", group read marks, lock order,
/pause from out of office and reject races. Synthetic data only."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Contact,
    ContactKind,
    ConversationTurn,
    Draft,
    DraftState,
    DroppedReason,
    Message,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.drafting import composer as composer_module
from scribe.intake.waiting import replan_waiting_turns
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.scheduling.scheduler import DueTurnsProcessor
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage, OutgoingEvent
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser, next_telegram_id
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, next_message_id, select_all_history
from tests.integration.us2_support import US2Harness, button_texts, callback_datas

REPLY = "Synthetic reply about the report"
BANNER = "⚠️ New messages arrived"


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
    user_values.setdefault("delay_min_s", 0)
    user_values.setdefault("delay_max_s", 0)
    user = await make_user(state=UserState.active, **user_values)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    return user, chat, contact


def _during_llm_call(
    monkeypatch: pytest.MonkeyPatch, action: Callable[[], Awaitable[Any]]
) -> list[bool]:
    """Run ``action`` once while the next compose call is "in flight" (after the turn's messages
    were loaded, before the draft is stored)."""
    original = composer_module.compose_output
    fired: list[bool] = []

    async def compose_output(router: Any, inp: Any, **kwargs: Any) -> Any:
        result = await original(router, inp, **kwargs)
        if not fired:
            fired.append(True)
            await action()
        return result

    monkeypatch.setattr(composer_module, "compose_output", compose_output)
    return fired


async def _message_row(
    session_factory: async_sessionmaker[AsyncSession], chat: WorkChat, telegram_id: int
) -> Message:
    async with session_factory() as session:
        return (
            await session.execute(
                select(Message).where(
                    Message.work_chat_id == chat.id, Message.telegram_message_id == telegram_id
                )
            )
        ).scalar_one()


def _answers(harness: US2Harness) -> list[str]:
    return [a.text or "" for a in harness.api.of_type(AnswerCallbackQuery)]


# --- finding 1: messages arriving while a draft is being composed -------------------------------


async def test_regenerate_keeps_message_that_arrives_during_llm_call(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    first = await harness.incoming(user, chat, contact, "When is the report due?")
    await harness.tick()
    (v1,) = await harness.drafts(user)
    harness.api.clear()

    arrived: list[IncomingMessage] = []

    async def contact_writes() -> None:
        arrived.append(await harness.incoming(user, chat, contact, "Also, in English please"))

    fired = _during_llm_call(monkeypatch, contact_writes)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{v1.id}"))
    assert fired

    v1, v2 = await harness.drafts(user)
    assert v1.state == DraftState.superseded
    assert v2.state == DraftState.pending and v2.version == 2
    assert v2.outdated  # v2 does not cover the new message: banner, no one-tap send
    (turn,) = await harness.turns(user)
    late = await _message_row(session_factory, chat, arrived[0].message_id)
    assert turn.last_message_id == late.id
    covered = await _message_row(session_factory, chat, first.message_id)
    assert v2.output_json["reply_to_message_id"] == str(covered.id)
    card = harness.messages()[-1]
    assert card.text.startswith(BANNER)
    assert f"approve:{v2.id}:anyway" in callback_datas(card.reply_markup)
    assert "✅ Send" not in button_texts(card.reply_markup)


async def test_message_while_turn_is_reading_joins_it(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    fake_llm: FakeAdapter,
    monkeypatch: pytest.MonkeyPatch,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "When is the report due?")
    arrived: list[IncomingMessage] = []

    async def contact_writes() -> None:
        arrived.append(await harness.incoming(user, chat, contact, "Also, in English please"))

    _during_llm_call(monkeypatch, contact_writes)
    await harness.tick()

    (turn,) = await harness.turns(user)  # joined the turn being drafted, no parallel turn
    assert turn.state == TurnState.pending_review
    (draft,) = await harness.drafts(user)
    assert draft.outdated
    (card,) = harness.messages()
    assert card.text.startswith(BANNER)

    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{draft.id}"))
    _, v2 = await harness.drafts(user)
    assert v2.state == DraftState.pending and not v2.outdated
    block = next(
        b for b in fake_llm.calls_for("compose.draft")[-1].user_blocks
        if b.name == "incoming_messages"
    )  # fmt: skip
    assert "Also, in English please" in block.content
    assert gateway.mark_read_calls[-1].up_to_message_id == arrived[0].message_id
    assert len(await harness.turns(user)) == 1


# --- finding 2: manual reply vs incoming message ordering ---------------------------------------


async def test_manual_reply_handled_before_incoming_drops_its_turn(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=300, delay_max_s=300
    )
    # Like Telethon: the incoming handler is still resolving the sender (before intake) when the
    # user's reply from their phone is handled.
    resolving = asyncio.Event()
    gateway._new_message_handlers = []

    async def slow_incoming(message: IncomingMessage) -> None:
        await resolving.wait()
        await harness.intake.handle_new_message(message)

    gateway.on_new_message(slow_incoming)
    incoming = asyncio.create_task(harness.incoming(user, chat, contact, "Can you check it?"))
    await asyncio.sleep(0)
    clock.advance(seconds=1)
    await harness.manual_reply(user, chat, "Checked, all fine")
    resolving.set()
    await incoming

    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.dropped
    assert turn.dropped_reason == DroppedReason.manual_reply
    await harness.at(clock() + timedelta(hours=1))
    assert gateway.mark_read_calls == []
    assert await harness.drafts(user) == []


async def test_late_outgoing_update_does_not_drop_newer_turn(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    earlier_reply_id = next_message_id()  # sent before the contact's new message
    sent_at = clock() - timedelta(minutes=1)
    await harness.incoming(user, chat, contact, "New question")

    await gateway.emit_outgoing(  # delivered late (or redelivered)
        OutgoingEvent(
            user_id=user.id,
            chat_id=chat.telegram_chat_id,
            message_id=earlier_reply_id,
            text="Earlier answer",
            sent_at=sent_at,
        )
    )

    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.waiting
    assert harness.notifications == []
    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending


# --- finding 3: a failed statement must not strand a batch in "reading" -------------------------


def _processor(harness: US2Harness) -> DueTurnsProcessor:
    handler = harness.scheduler._due_turns_handler
    assert isinstance(handler, DueTurnsProcessor)
    return handler


async def test_failed_batch_read_at_update_falls_back_per_turn(
    harness: US2Harness,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    other = await make_contact(user, display_name="Synthetic Boris")
    other_chat = await make_chat(user, title="Boris", telegram_chat_id=other.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Question one")
    await harness.incoming(user, other_chat, other, "Question two")
    processor = _processor(harness)
    original = processor._set_turns
    failed: list[int] = []

    async def flaky(turn_ids: list[uuid.UUID], **values: Any) -> None:
        if "read_at" in values and len(turn_ids) > 1:
            failed.append(len(turn_ids))
            raise ConnectionError("synthetic database failure")
        await original(turn_ids, **values)

    monkeypatch.setattr(processor, "_set_turns", flaky)
    await harness.tick()

    assert failed == [2]
    turns = await harness.turns(user)
    assert [t.state for t in turns] == [TurnState.pending_review] * 2
    assert all(t.read_at is not None for t in turns)
    assert len(await harness.drafts(user)) == 2


async def test_turn_stranded_in_reading_is_requeued_by_the_sweep(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Question")
    processor = _processor(harness)
    original = processor._set_turns

    async def broken(turn_ids: list[uuid.UUID], **values: Any) -> None:
        raise ConnectionError("synthetic database failure")

    monkeypatch.setattr(processor, "_set_turns", broken)
    await harness.tick()
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.reading  # even the per-turn fallback failed
    monkeypatch.setattr(processor, "_set_turns", original)

    await harness.at(clock() + timedelta(minutes=5))
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.reading  # not stuck long enough yet

    await harness.at(clock() + timedelta(minutes=6))  # 11 min in "reading": requeued, drafted
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.pending_review
    assert len(await harness.drafts(user)) == 1
    assert gateway.send_text_calls == []


# --- finding 4: re-planning keeps "read now" / read elsewhere -----------------------------------

BERLIN = {"timezone": "Europe/Berlin", "delay_min_s": 300, "delay_max_s": 300}


async def test_read_now_while_paused_survives_resume(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact, **BERLIN)
    control = user.telegram_control_chat_id
    await harness.feed(text_update(control, "/pause"))
    clock.set(datetime(2026, 10, 14, 20, 0, tzinfo=UTC))  # 22:00 Berlin
    await harness.incoming(user, chat, contact, "Late question")
    (turn,) = await harness.turns(user)
    assert turn.due_at == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)

    clock.set(datetime(2026, 10, 14, 21, 0, tzinfo=UTC))  # 23:00: "Read now"
    await harness.feed(callback_update(control, f"readnow:{turn.id}"))
    assert any("after /resume" in text for text in _answers(harness))
    clock.set(datetime(2026, 10, 14, 21, 30, tzinfo=UTC))  # 23:30: /resume
    await harness.feed(text_update(control, "/resume"))

    (turn,) = await harness.turns(user)
    assert turn.due_at == datetime(2026, 10, 14, 21, 0, tzinfo=UTC)  # not 09:00 tomorrow
    await harness.tick()
    assert len(gateway.mark_read_calls) == 1
    assert len(await harness.drafts(user)) == 1


async def test_read_elsewhere_while_paused_survives_resume(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact, **BERLIN)
    control = user.telegram_control_chat_id
    await harness.feed(text_update(control, "/pause"))
    clock.set(datetime(2026, 10, 14, 20, 0, tzinfo=UTC))
    late = await harness.incoming(user, chat, contact, "Late question")
    clock.set(datetime(2026, 10, 14, 21, 0, tzinfo=UTC))
    await harness.read_elsewhere(user, chat, max_message_id=late.message_id)
    clock.set(datetime(2026, 10, 14, 21, 30, tzinfo=UTC))
    await harness.feed(text_update(control, "/resume"))

    (turn,) = await harness.turns(user)
    assert turn.due_at == datetime(2026, 10, 14, 21, 0, tzinfo=UTC)
    async with session_factory() as session:
        (record,) = (
            await session.execute(
                select(ActivityRecord).where(
                    ActivityRecord.user_id == user.id,
                    ActivityRecord.kind == ActivityKind.read_now,
                )
            )
        ).scalars()
    assert record.conversation_turn_id == turn.id
    assert record.meta_json == {"source": "read_elsewhere"}


# --- finding 5: restart requeue respects working hours ------------------------------------------


async def test_restart_requeue_respects_working_hours(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact, **BERLIN)
    clock.set(datetime(2026, 10, 14, 10, 0, tzinfo=UTC))
    chats = [chat]
    for name in ("Boris", "Clara"):
        person = await make_contact(user, display_name=f"Synthetic {name}")
        chats.append(await make_chat(user, title=name, telegram_chat_id=person.telegram_peer_id))
        await harness.incoming(user, chats[-1], person, "Question")
    await harness.incoming(user, chat, contact, "Question")
    unread, read, read_now = sorted(await harness.turns(user), key=lambda t: t.work_chat_id.int)
    async with session_factory() as session:
        await session.execute(
            update(ConversationTurn)
            .where(ConversationTurn.user_id == user.id)
            .values(state=TurnState.reading)
        )
        await session.execute(
            update(ConversationTurn).where(ConversationTurn.id == read.id).values(read_at=clock())
        )
        await activity_repo.record(
            session, user.id, ActivityKind.read_now, at=clock(), conversation_turn_id=read_now.id
        )
        await session.commit()

    clock.set(datetime(2026, 10, 15, 1, 0, tzinfo=UTC))  # restart at 03:00 Berlin
    assert await harness.scheduler.requeue_interrupted_turns() == 3

    due = {t.id: t.due_at for t in await harness.turns(user)}
    assert due[unread.id] == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)  # 09:00 Berlin
    assert due[read.id] == clock()  # already marked read: drafting may continue now
    assert due[read_now.id] == clock()  # the user asked for it
    assert all(t.state == TurnState.waiting for t in await harness.turns(user))


# --- findings 6 and 7: "Send anyway" ------------------------------------------------------------


async def test_plain_approve_of_outdated_draft_is_refused(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    from scribe.sending.send import DraftOutdated, send_approved_draft

    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id
    await harness.incoming(user, chat, contact, "When is the report due?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    await harness.incoming(user, chat, contact, "Also, in English please")
    harness.api.clear()

    # The old card's ✅ Send (e.g. its re-render failed): nothing is sent, the user is asked.
    for data in (f"approve:{draft.id}", f"approve:{draft.id}:confirm"):
        await harness.feed(callback_update(control, data))
    assert gateway.send_text_calls == []
    prompts = [m for m in harness.messages() if "Regenerate" in m.text]
    assert len(prompts) == 2
    assert callback_datas(prompts[0].reply_markup) == [
        f"regen:{draft.id}",
        f"approve:{draft.id}:anyway",
    ]
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending and draft.approved_by_user_at is None

    # send.py enforces it too, under the row lock.
    async with session_factory() as session:
        await session.execute(
            update(Draft).where(Draft.id == draft.id).values(approved_by_user_at=clock())
        )
        await session.commit()
    with pytest.raises(DraftOutdated):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
        )
    assert gateway.send_text_calls == []

    await harness.feed(callback_update(control, f"approve:{draft.id}:anyway"))
    (sent,) = gateway.send_text_calls
    assert sent.text == REPLY
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.sent


async def test_send_anyway_quotes_the_drafted_message(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    quoted = next_message_id()
    first = await harness.incoming(user, chat, contact, "About that, when?", reply_to_id=quoted)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    await harness.incoming(user, chat, contact, "And who?", reply_to_id=quoted)

    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}:anyway"))
    (sent,) = gateway.send_text_calls
    assert sent.reply_to == first.message_id  # the message the draft answers, not "And who?"


# --- finding 8: never read past another contact's waiting message in a group --------------------


async def _group(
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, WorkChat, Contact, Contact]:
    user = await make_user(state=UserState.active, delay_min_s=600, delay_max_s=600)
    bob = await make_contact(user, display_name="Synthetic Bob")
    carol = await make_contact(user, display_name="Synthetic Carol")
    group = await make_chat(user, title="Synthetic team", kind=ChatKind.group)
    return user, group, bob, carol


async def test_regenerate_in_group_does_not_read_other_contacts_waiting_message(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, group, bob, carol = await _group(make_user, make_chat, make_contact)
    bob_first = await harness.incoming(user, group, bob, "Bob: status?", mentions_me=True)
    await harness.at(clock() + timedelta(minutes=10))
    (draft,) = await harness.drafts(user)
    carol_msg = await harness.incoming(
        user, group, carol, "Carol: separate question", mentions_me=True
    )
    bob_more = await harness.incoming(user, group, bob, "Bob: and the budget?", mentions_me=True)
    (draft,) = await harness.drafts(user)
    assert draft.outdated

    gateway.reset_calls()
    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{draft.id}"))
    (call,) = gateway.mark_read_calls
    assert call.up_to_message_id == carol_msg.message_id - 1  # not up to Bob's newer message
    assert bob_first.message_id <= call.up_to_message_id < bob_more.message_id


async def test_unaddressed_group_message_does_not_outdate_the_pending_draft(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """006 edge case: only an addressed message marks a pending group draft outdated."""
    user, group, bob, _ = await _group(make_user, make_chat, make_contact)
    await harness.incoming(user, group, bob, "Bob: status?", mentions_me=True)
    await harness.at(clock() + timedelta(minutes=10))
    (draft,) = await harness.drafts(user)
    await harness.incoming(user, group, bob, "Bob: chatting with the team")
    (draft,) = await harness.drafts(user)
    assert not draft.outdated
    assert len(await harness.turns(user)) == 1


async def test_scheduler_in_group_does_not_read_other_contacts_waiting_message(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, group, bob, carol = await _group(make_user, make_chat, make_contact)
    await harness.incoming(user, group, bob, "Bob: status?", mentions_me=True)
    clock.advance(seconds=1)
    carol_msg = await harness.incoming(
        user, group, carol, "Carol: separate question", mentions_me=True
    )
    clock.advance(seconds=1)
    await harness.incoming(user, group, bob, "Bob: and the budget?", mentions_me=True)
    turns = await harness.turns(user)
    assert len(turns) == 3
    carol_turn = turns[1]
    async with session_factory() as session:  # Carol's random delay is longer
        await session.execute(
            update(ConversationTurn)
            .where(ConversationTurn.id == carol_turn.id)
            .values(due_at=clock() + timedelta(hours=1))
        )
        await session.commit()

    await harness.at(clock() + timedelta(minutes=15))
    assert gateway.mark_read_calls
    assert max(c.up_to_message_id for c in gateway.mark_read_calls) < carol_msg.message_id
    assert len(await harness.drafts(user)) == 2
    (carol_turn,) = [t for t in await harness.turns(user) if t.id == carol_turn.id]
    assert carol_turn.state == TurnState.waiting


# --- finding 10b: /pause from out of office -----------------------------------------------------


async def test_pause_from_out_of_office_resumes_out_of_office(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.out_of_office)
    control = user.telegram_control_chat_id
    await harness.feed(text_update(control, "/pause"))
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None and stored.state == UserState.paused
    await harness.feed(text_update(control, "/resume"))
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None and stored.state == UserState.out_of_office

    active = await make_user(state=UserState.active)
    await harness.feed(text_update(active.telegram_control_chat_id, "/pause"))
    await harness.feed(text_update(active.telegram_control_chat_id, "/resume"))
    async with session_factory() as session:
        stored = await users_repo.get_user(session, active.id)
        assert stored is not None and stored.state == UserState.active


# --- real commits on separate connections (lock order and races) --------------------------------


async def _committed_setup(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, **user_values: Any
) -> tuple[User, WorkChat, Contact]:
    async with session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings
        )
        user.state = UserState.active
        user.delay_min_s = user.delay_max_s = 0
        for key, value in user_values.items():
            setattr(user, key, value)
        peer = next_telegram_id()
        contact = await chats_repo.upsert_contact(
            session, user.id, telegram_peer_id=peer, kind=ContactKind.person, display_name="Anna"
        )
        chat = await chats_repo.upsert_work_chat(
            session, user.id, telegram_chat_id=peer, title="Anna", kind=ChatKind.private,
            enabled=True,
        )  # fmt: skip
        await session.commit()
    return user, chat, contact


class BlockingSendGateway(FakeMessagingGateway):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_text(
        self, user_id: uuid.UUID, chat_id: int, text: str, reply_to: int | None, *, random_id: int
    ) -> int:
        self.entered.set()
        await self.release.wait()
        return await super().send_text(user_id, chat_id, text, reply_to, random_id=random_id)


async def test_manual_reply_racing_an_approved_send_waits_for_it(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 9: the manual-reply handler locked the chat's drafts (none yet), a draft is
    composed and approved, ``send.py`` is inside Telegram; the handler then locks the turn. With
    the draft -> turn order on both sides the handler waits for the send instead of deadlocking
    with it."""
    from scribe.intake import events as events_module

    session_factory = committed_session_factory
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    gateway = BlockingSendGateway()
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    user, chat, contact = await _committed_setup(session_factory, settings)
    await harness.incoming(user, chat, contact, "Synthetic question?")

    drafts_locked = asyncio.Event()
    proceed = asyncio.Event()
    original = events_module._is_agent_send

    async def paused_is_agent_send(*args: Any) -> bool:
        drafts_locked.set()
        await proceed.wait()
        return await original(*args)

    monkeypatch.setattr(events_module, "_is_agent_send", paused_is_agent_send)
    reply = asyncio.create_task(harness.manual_reply(user, chat, "Answered myself"))
    await asyncio.wait_for(drafts_locked.wait(), 5)

    await harness.tick()  # composes the draft (the handler holds no draft lock for it)
    (draft,) = await harness.drafts(user)
    approve = asyncio.create_task(
        harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    )
    await asyncio.wait_for(gateway.entered.wait(), 5)
    proceed.set()
    await asyncio.sleep(0.3)  # the handler now waits for the turn row held by send.py
    gateway.release.set()
    await asyncio.wait_for(asyncio.gather(approve, reply), 15)

    (sent,) = gateway.send_text_calls
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.sent and draft.sent_telegram_message_id == sent.message_id
    async with session_factory() as session:
        turn = await session.get(ConversationTurn, draft.conversation_turn_id)
        assert turn is not None and turn.state == TurnState.answered
        stored = await session.scalar(
            select(Message.id).where(
                Message.work_chat_id == chat.id, Message.telegram_message_id == reply.result()
            )
        )
        assert stored is not None  # the manual reply was handled, not lost to a deadlock


async def test_replan_waits_for_a_locked_turn(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> None:
    """Finding 10a: a turn locked by someone else is waited for, not skipped."""
    session_factory = committed_session_factory
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    user, chat, contact = await _committed_setup(session_factory, settings, **BERLIN)
    clock.set(datetime(2026, 10, 14, 20, 0, tzinfo=UTC))  # 22:00 Berlin, after hours
    await harness.incoming(user, chat, contact, "Late question")
    (turn,) = await harness.turns(user)
    assert turn.due_at == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)

    async with session_factory() as holder:
        await holder.execute(
            select(ConversationTurn).where(ConversationTurn.id == turn.id).with_for_update()
        )

        async def replan() -> int:
            async with session_factory() as session:
                stored = await users_repo.get_user(session, user.id)
                assert stored is not None
                stored.work_end = time(23, 0)
                moved = await replan_waiting_turns(session, stored, clock())
                await session.commit()
                return moved

        task = asyncio.create_task(replan())
        await asyncio.sleep(0.3)
        assert not task.done()  # blocked on the row, not skipping it
        await holder.commit()
        moved = await asyncio.wait_for(task, 5)

    assert moved == 1
    (turn,) = await harness.turns(user)
    assert turn.due_at == datetime(2026, 10, 14, 20, 5, tzinfo=UTC)  # 22:05 Berlin


async def test_reject_after_concurrent_drop_answers_already_handled(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> None:
    """Finding 10c: reject never overwrites a draft finished meanwhile."""
    session_factory = committed_session_factory
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    user, chat, contact = await _committed_setup(session_factory, settings)
    await harness.incoming(user, chat, contact, "Synthetic question?")
    await harness.tick()
    (draft,) = await harness.drafts(user)

    async with session_factory() as holder:  # e.g. the manual-reply drop, mid-transaction
        locked = (
            await holder.execute(select(Draft).where(Draft.id == draft.id).with_for_update())
        ).scalar_one()
        task = asyncio.create_task(
            harness.feed(callback_update(user.telegram_control_chat_id, f"reject:{draft.id}"))
        )
        await asyncio.sleep(0.3)
        locked.state = DraftState.dropped
        locked.finalized_at = clock()
        await holder.execute(
            update(ConversationTurn)
            .where(ConversationTurn.id == draft.conversation_turn_id)
            .values(state=TurnState.dropped, dropped_reason=DroppedReason.manual_reply)
        )
        await holder.commit()
        await asyncio.wait_for(task, 5)

    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.dropped
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.dropped
    assert "This draft was already handled" in _answers(harness)
    async with session_factory() as session:
        rejected = await session.scalar(
            select(ActivityRecord.id).where(
                ActivityRecord.user_id == user.id, ActivityRecord.kind == ActivityKind.rejected
            )
        )
    assert rejected is None
