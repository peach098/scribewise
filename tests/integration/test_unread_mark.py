"""specs/009 end to end: chats are marked unread after the agent reads (US1) or sends (US2),
never for what the user has already seen and never twice for the same message (US3).

Intake -> scheduler -> reply gate -> composer -> card -> review callbacks, with an
``UnreadMarker`` wired in, against the fake LLM, the fake gateway and an offline Bot API
session. Synthetic data only.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ChatKind,
    Contact,
    ContactKind,
    ConversationTurn,
    Draft,
    DraftState,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway, MarkReadCall
from scribe.telegram.gateway import (
    GatewayBusy,
    OutgoingEvent,
    ReadEvent,
    UnreadMarkClearedEvent,
)
from tests.conftest import (
    FrozenClock,
    LogCapture,
    MakeChat,
    MakeContact,
    MakeUser,
    next_telegram_id,
)
from tests.integration.bot_support import callback_update
from tests.integration.us1_support import (
    grounded_reply,
    next_message_id,
    reply_needed_output,
    select_all_history,
)
from tests.integration.us2_support import US2Harness

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
    return US2Harness(
        session_factory, settings, llm_router, fake_llm, gateway, clock, with_unread_marker=True
    )


async def _setup(
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact, **user_values: object
) -> tuple[User, WorkChat, Contact]:
    values: dict[str, object] = {"delay_min_s": 0, "delay_max_s": 0, **user_values}
    user = await make_user(state=UserState.active, **values)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    return user, chat, contact


async def _floor(harness: US2Harness, chat: WorkChat) -> int | None:
    async with harness.session_factory() as session:
        return await session.scalar(
            select(WorkChat.unread_mark_floor).where(WorkChat.id == chat.id)
        )


def _marks(gateway: FakeMessagingGateway, chat: WorkChat) -> int:
    return sum(1 for _, chat_id in gateway.mark_unread_calls if chat_id == chat.telegram_chat_id)


# --- US1: chats the agent read stay visible -------------------------------------------------------


async def test_read_marks_the_chat_unread_once_after_the_read(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    incoming = await harness.incoming(user, chat, contact, "Can you check the report?")
    await harness.tick()

    assert gateway.call_order == [
        ("mark_read", chat.telegram_chat_id),
        ("mark_unread", chat.telegram_chat_id),
    ]
    assert await _floor(harness, chat) == incoming.message_id
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending

    await harness.tick()  # nothing new: nothing more
    assert _marks(gateway, chat) == 1


async def test_closing_message_the_gate_skips_is_still_marked(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    harness.set_reply_needed(reply_needed_output(False, "thanks"))
    await harness.incoming(user, chat, contact, "thanks!")
    await harness.tick()
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.skipped
    assert await harness.drafts(user) == []
    assert _marks(gateway, chat) == 1


async def test_two_turns_of_one_group_in_one_tick(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=0, delay_max_s=0)
    bob = await make_contact(user, display_name="Synthetic Bob")
    carol = await make_contact(user, display_name="Synthetic Carol")
    group = await make_chat(user, title="Synthetic team", kind=ChatKind.group)
    # specs/006: a group opens a turn only when the user is tagged or replied to.
    await harness.incoming(user, group, bob, "Bob: status?", mentions_me=True)
    carol_msg = await harness.incoming(user, group, carol, "Carol: budget?", mentions_me=True)
    assert len(await harness.turns(user)) == 2
    await harness.tick()

    read_ids = {c.up_to_message_id for c in gateway.mark_read_calls}
    assert await _floor(harness, group) == carol_msg.message_id
    assert 1 <= _marks(gateway, group) <= len(read_ids)


async def test_no_mark_without_a_read(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    # Media only: handled manually, never read.
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "", has_media=True)
    await harness.tick()
    assert (await harness.turns(user))[0].state == TurnState.handle_manually

    # A paused user and a user whose access is suspended: messages stay unread.
    paused, paused_chat, paused_contact = await _setup(make_user, make_chat, make_contact)
    suspended, suspended_chat, suspended_contact = await _setup(make_user, make_chat, make_contact)
    async with session_factory() as session:
        await users_repo.update_user(session, paused.id, state=UserState.paused)
        await users_repo.update_user(session, suspended.id, access_suspended_at=clock())
        await session.commit()
    await harness.incoming(paused, paused_chat, paused_contact, "Question?")
    await harness.incoming(suspended, suspended_chat, suspended_contact, "Question?")
    await harness.at(clock() + timedelta(minutes=5))

    assert gateway.mark_read_calls == []
    assert gateway.mark_unread_calls == []


async def test_failed_read_marks_nothing_and_the_retry_marks_once(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    incoming = await harness.incoming(user, chat, contact, "Question?")
    gateway.fail_mark_read_with = GatewayBusy(120)
    await harness.tick()
    assert gateway.mark_unread_calls == []
    assert await _floor(harness, chat) is None
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.waiting

    gateway.fail_mark_read_with = None
    await harness.at(clock() + timedelta(seconds=61))
    assert _marks(gateway, chat) == 1
    assert await _floor(harness, chat) == incoming.message_id


async def test_failed_mark_never_affects_drafting(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    gateway.fail_mark_unread_with = GatewayBusy(30)
    await harness.incoming(user, chat, contact, "Question?")
    await harness.tick()

    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.pending_review
    (draft,) = await harness.drafts(user)
    assert draft.control_message_id is not None  # the card was posted
    assert harness.notifications == []
    assert len(harness.messages()) == 1  # only the card, no notice about the mark
    [failed] = log_capture.events("unread_mark_failed")
    assert failed["cause"] == "read" and failed["error"] == "GatewayBusy"


async def test_requeued_turn_is_not_marked_again(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    harness.set_reply_needed(reply_needed_output(False, "thanks"))  # stops after the read
    await harness.incoming(user, chat, contact, "thanks")
    await harness.tick()
    assert _marks(gateway, chat) == 1

    (turn,) = await harness.turns(user)
    async with session_factory() as session:  # a crash right after the read
        await session.execute(
            update(ConversationTurn)
            .where(ConversationTurn.id == turn.id)
            .values(state=TurnState.reading, reply_decision=None)
        )
        await session.commit()
    assert await harness.scheduler.requeue_interrupted_turns() == 1
    await harness.tick()
    assert len(gateway.mark_read_calls) == 2  # read again
    assert _marks(gateway, chat) == 1  # but not marked again


async def test_processor_without_a_marker_behaves_as_before(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    plain = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await plain.incoming(user, chat, contact, "Question?")
    await plain.tick()
    (draft,) = await plain.drafts(user)
    await plain.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    assert len(gateway.mark_read_calls) == 1 and len(gateway.send_text_calls) == 1
    assert gateway.mark_unread_calls == []
    assert await _floor(plain, chat) is None


async def test_regenerate_of_an_outdated_draft_marks_after_its_read(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "When is the report due?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    follow_up = await harness.incoming(user, chat, contact, "Also, is it in English?")
    (draft,) = await harness.drafts(user)
    assert draft.outdated
    gateway.reset_calls()

    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{draft.id}"))
    assert gateway.call_order == [
        ("mark_read", chat.telegram_chat_id),
        ("mark_unread", chat.telegram_chat_id),
    ]
    assert await _floor(harness, chat) == follow_up.message_id

    # The new version regenerated again with nothing new to read: no second mark.
    _, new = await harness.drafts(user)
    async with session_factory() as session:
        await session.execute(update(Draft).where(Draft.id == new.id).values(outdated=True))
        await session.commit()
    gateway.reset_calls()
    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{new.id}"))
    assert [c[0] for c in gateway.call_order] == ["mark_read"]


# --- US2: chats the agent answered stay visible ---------------------------------------------------


async def test_approved_send_marks_the_chat_after_sending(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Question?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    gateway.reset_calls()

    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    (sent,) = gateway.send_text_calls
    assert gateway.call_order == [
        ("send_text", chat.telegram_chat_id),
        ("mark_unread", chat.telegram_chat_id),
    ]
    assert await _floor(harness, chat) == sent.message_id
    assert [e["cause"] for e in log_capture.events("unread_mark_set")] == ["read", "send"]

    # The echo of the agent's own send is not a manual reply and marks nothing more.
    await harness.gateway.emit_outgoing(
        OutgoingEvent(
            user_id=user.id,
            chat_id=chat.telegram_chat_id,
            message_id=sent.message_id,
            text=REPLY,
            sent_at=harness.clock(),
        )
    )
    assert _marks(gateway, chat) == 1
    assert log_capture.events("unread_mark_seen") == []


async def test_edit_and_send_marks_the_chat(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    from tests.integration.bot_support import text_update

    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Question?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    gateway.reset_calls()
    control = user.telegram_control_chat_id
    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    await harness.feed(text_update(control, "My own synthetic wording"))
    (sent,) = gateway.send_text_calls
    assert sent.text == "My own synthetic wording"
    assert gateway.call_order[-1] == ("mark_unread", chat.telegram_chat_id)
    assert await _floor(harness, chat) == sent.message_id


async def test_failed_send_marks_nothing(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Question?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    floor = await _floor(harness, chat)
    gateway.reset_calls()
    gateway.fail_send_with = GatewayBusy(600)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    assert gateway.mark_unread_calls == []
    assert await _floor(harness, chat) == floor


async def test_failed_mark_after_send_never_affects_the_send(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Question?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    harness.api.clear()
    gateway.fail_mark_unread_with = GatewayBusy(30)

    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.sent
    assert harness.messages() == []  # no extra control-chat message
    assert any("Sent" in (e.text or "") for e in harness.edits())
    [failed] = log_capture.events("unread_mark_failed")
    assert failed["cause"] == "send" and failed["error"] == "GatewayBusy"


async def test_mark_after_send_runs_outside_the_draft_row_lock(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
) -> None:
    session_factory = committed_session_factory
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    gateway = FakeMessagingGateway()
    harness = US2Harness(
        session_factory, settings, llm_router, fake_llm, gateway, clock, with_unread_marker=True
    )
    async with session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings
        )
        user.state = UserState.active
        peer = next_telegram_id()
        contact = await chats_repo.upsert_contact(
            session, user.id, telegram_peer_id=peer, kind=ContactKind.person, display_name="Anna"
        )
        chat = await chats_repo.upsert_work_chat(
            session, user.id, telegram_chat_id=peer, title="Anna", kind=ChatKind.private,
            enabled=True,
        )  # fmt: skip
        await session.commit()
    await harness.incoming(user, chat, contact, "Synthetic question?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    lock_free: list[bool] = []

    async def hook(user_id: uuid.UUID, chat_id: int) -> None:
        if not gateway.send_text_calls:
            return  # the mark after the read
        async with session_factory() as other:
            try:
                await other.execute(
                    text("SELECT id FROM drafts WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": draft.id},
                )
                lock_free.append(True)
            except DBAPIError:
                lock_free.append(False)
            await other.rollback()

    gateway.on_mark_unread = hook
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    assert lock_free == [True]
    assert len(gateway.mark_unread_calls) == 2


# --- US3: the mark goes away when the user looks, and stays away ----------------------------------


async def test_read_elsewhere_before_the_delay_means_no_mark(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=300, delay_max_s=300
    )
    incoming = await harness.incoming(user, chat, contact, "Question?")
    await harness.read_elsewhere(user, chat, incoming.message_id)
    await harness.tick()  # due now (existing behaviour), read by the agent
    assert [c.up_to_message_id for c in gateway.mark_read_calls] == [incoming.message_id]
    assert gateway.mark_unread_calls == []
    assert "seen" in [e["reason"] for e in log_capture.events("unread_mark_skipped")]
    [seen] = log_capture.events("unread_mark_seen")
    assert seen["source"] == "read_elsewhere"


async def test_cleared_mark_is_not_set_again_until_something_new(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    harness.set_reply_needed(reply_needed_output(False, "thanks"))
    first = await harness.incoming(user, chat, contact, "thanks")
    await harness.tick()
    assert _marks(gateway, chat) == 1
    gateway.reset_calls()

    await gateway.emit_unread_mark_cleared(
        UnreadMarkClearedEvent(user_id=user.id, chat_id=chat.telegram_chat_id)
    )
    assert await _floor(harness, chat) == first.message_id
    assert gateway.mark_read_calls == [] and gateway.mark_unread_calls == []  # FR-008

    # A re-read of the same turn marks nothing.
    (turn,) = await harness.turns(user)
    async with session_factory() as session:
        await session.execute(
            update(ConversationTurn)
            .where(ConversationTurn.id == turn.id)
            .values(state=TurnState.reading, reply_decision=None)
        )
        await session.commit()
    await harness.scheduler.requeue_interrupted_turns()
    await harness.tick()
    assert gateway.mark_unread_calls == []

    # The next new message read by the agent: exactly one new mark.
    await harness.incoming(user, chat, contact, "thanks again")
    await harness.tick()
    assert _marks(gateway, chat) == 1


async def test_manual_reply_counts_as_seen(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=300, delay_max_s=300
    )
    await harness.incoming(user, chat, contact, "Question?")
    reply_id = await harness.manual_reply(user, chat)
    assert await _floor(harness, chat) == reply_id
    [seen] = log_capture.events("unread_mark_seen")
    assert seen["source"] == "manual_reply" and seen["up_to"] == reply_id
    assert gateway.mark_unread_calls == []


async def test_echo_of_the_agents_own_read_does_not_cancel_the_mark(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)

    async def echo(call: MarkReadCall) -> None:
        await gateway.emit_read_elsewhere(
            ReadEvent(
                user_id=call.user_id, chat_id=call.chat_id, max_message_id=call.up_to_message_id
            )
        )

    gateway.on_mark_read = echo
    incoming = await harness.incoming(user, chat, contact, "Question?")
    await harness.tick()
    assert _marks(gateway, chat) == 1
    assert await _floor(harness, chat) == incoming.message_id


async def test_setting_off_still_records_seen_and_on_never_backfills(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact, mark_unread=False)
    harness.set_reply_needed(reply_needed_output(False, "thanks"))
    incoming = await harness.incoming(user, chat, contact, "thanks")
    await harness.read_elsewhere(user, chat, incoming.message_id)
    assert await _floor(harness, chat) == incoming.message_id
    await harness.tick()
    assert gateway.mark_unread_calls == []

    async with session_factory() as session:
        await users_repo.update_user(session, user.id, mark_unread=True)
        await session.commit()
    (turn,) = await harness.turns(user)
    async with session_factory() as session:  # the agent reads the same message again
        await session.execute(
            update(ConversationTurn)
            .where(ConversationTurn.id == turn.id)
            .values(state=TurnState.reading, reply_decision=None)
        )
        await session.commit()
    await harness.scheduler.requeue_interrupted_turns()
    await harness.tick()
    assert gateway.mark_unread_calls == []


async def test_seen_signals_for_other_chats_change_nothing(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.active)
    disabled = await make_chat(user, enabled=False)
    for chat_id in (disabled.telegram_chat_id, next_telegram_id()):
        await gateway.emit_read_elsewhere(
            ReadEvent(user_id=user.id, chat_id=chat_id, max_message_id=50)
        )
        await gateway.emit_unread_mark_cleared(
            UnreadMarkClearedEvent(user_id=user.id, chat_id=chat_id)
        )
        await gateway.emit_outgoing(
            OutgoingEvent(
                user_id=user.id,
                chat_id=chat_id,
                message_id=next_message_id(),
                text="synthetic",
                sent_at=harness.clock(),
            )
        )
    async with session_factory() as session:
        floors = (
            (
                await session.execute(
                    select(WorkChat.unread_mark_floor).where(WorkChat.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
    assert floors == [None]
    assert gateway.mark_unread_calls == []


# --- FR-012: logs carry ids and reasons only ------------------------------------------------------

UNREAD_EVENTS = ("unread_mark_set", "unread_mark_skipped", "unread_mark_failed", "unread_mark_seen")
ALLOWED_FIELDS = {
    "event",
    "level",
    "logger",
    "timestamp",
    "user_id",
    "work_chat_id",
    "cause",
    "reason",
    "source",
    "up_to",
    "error",
}


async def test_unread_mark_logs_carry_no_content(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    secret = "SYNTHETIC-SECRET-CONTENT"
    user = await make_user(state=UserState.active, delay_min_s=0, delay_max_s=0)
    contact = await make_contact(user, display_name=f"{secret} name")
    chat = await make_chat(user, title=f"{secret} title", telegram_chat_id=contact.telegram_peer_id)
    incoming = await harness.incoming(user, chat, contact, f"{secret} question?")
    await harness.tick()  # set (read)
    (draft,) = await harness.drafts(user)
    gateway.fail_mark_unread_with = GatewayBusy(30)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    gateway.fail_mark_unread_with = None
    await harness.read_elsewhere(user, chat, incoming.message_id)  # seen
    await harness.manual_reply(user, chat, f"{secret} manual")  # seen

    events = [e for name in UNREAD_EVENTS for e in log_capture.events(name)]
    assert {e["event"] for e in events} == set(UNREAD_EVENTS) - {"unread_mark_skipped"}
    for event in events:
        assert set(event) <= ALLOWED_FIELDS, event
        assert secret not in json.dumps(event)
