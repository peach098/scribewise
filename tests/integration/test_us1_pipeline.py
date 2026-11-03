"""US1 building blocks against Postgres: intake storage and grouping, context candidates,
composer persistence, scheduler step 1 failure handling, sending details, global profile."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context.history import load_candidates
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Contact,
    ConversationTurn,
    Draft,
    DraftState,
    DroppedReason,
    Message,
    ProfileScope,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import turns as turns_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import ProviderUnavailable, StyleProfileOutput, StyleRules, StyleSample
from scribe.llm.registry import LLMRouter
from scribe.sending.send import SendRefused, send_approved_draft
from scribe.style.importer import import_own_messages
from scribe.style.profiles import build_global_profile, select_profile
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import GatewayBusy, IncomingMessage
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.us1_support import US1Harness, grounded_reply, select_all_history


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US1Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    return US1Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _turns(session_factory: async_sessionmaker[AsyncSession]) -> list[ConversationTurn]:
    async with session_factory() as session:
        result = await session.execute(
            select(ConversationTurn).order_by(ConversationTurn.arrived_at)
        )
        return list(result.scalars().all())


async def _messages(session_factory: async_sessionmaker[AsyncSession]) -> list[Message]:
    async with session_factory() as session:
        result = await session.execute(select(Message).order_by(Message.sent_at))
        return list(result.scalars().all())


# --- intake -------------------------------------------------------------------------------------


async def test_turn_waits_for_random_delay_inside_working_hours(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=120, delay_max_s=600)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Synthetic question")

    (turn,) = await _turns(session_factory)
    assert turn.state == TurnState.waiting
    assert 120 <= turn.delay_s <= 600
    assert turn.due_at == turn.arrived_at + timedelta(seconds=turn.delay_s)
    (message,) = await _messages(session_factory)
    assert message.text == "Synthetic question" and message.sender_contact_id == contact.id
    assert turn.first_message_id == turn.last_message_id == message.id

    # Per-chat override wins over the global range.
    override = await make_chat(user, override_delay_min_s=5, override_delay_max_s=5)
    other = await make_contact(user)
    await harness.incoming(user, override, other, "Another synthetic question")
    turns = await _turns(session_factory)
    assert sorted(t.delay_s for t in turns)[0] == 5


async def test_outside_working_hours_due_at_next_window(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, timezone="Europe/Moscow")
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    clock.set(clock().replace(hour=19, minute=30))  # 22:30 in Moscow, Wednesday
    await harness.incoming(user, chat, contact, "Late synthetic question")
    (turn,) = await _turns(session_factory)
    assert turn.due_at == clock().replace(day=15, hour=6, minute=0)  # Thu 09:00 MSK


async def test_grouping_window_attaches_to_waiting_turn(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=300, delay_max_s=300)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    first = await harness.incoming(user, chat, contact, "Part one")
    clock.advance(seconds=30)
    second = await harness.incoming(user, chat, contact, "Part two")
    clock.advance(seconds=90)
    await harness.incoming(user, chat, contact, "Much later")

    turns = await _turns(session_factory)
    assert len(turns) == 2
    async with session_factory() as session:
        assert turns[0].last_message_id is not None
        last = await messages_repo.get_message(session, user.id, turns[0].last_message_id)
    assert last is not None and last.telegram_message_id == second.message_id
    assert turns[0].due_at == first.sent_at + timedelta(seconds=300)  # unchanged by grouping
    assert len(await _messages(session_factory)) == 3


async def test_duplicate_delivery_is_idempotent(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    message = await harness.incoming(user, chat, contact, "Synthetic")
    await gateway.emit_new_message(message)
    assert len(await _messages(session_factory)) == 1
    assert len(await _turns(session_factory)) == 1


async def test_group_rules_and_group_contact(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    group = await make_chat(user, kind=ChatKind.group, title="Synthetic Ops")
    member = await make_contact(user, display_name="Synthetic Member")
    await harness.incoming(user, group, member, "General chatter", chat_kind="group")
    assert await _turns(session_factory) == []  # stored for context only
    assert len(await _messages(session_factory)) == 1

    clock.advance(minutes=5)
    await harness.incoming(user, group, member, "@me question", chat_kind="group", mentions_me=True)
    (turn,) = await _turns(session_factory)
    async with session_factory() as session:
        group_contact = await chats_repo.get_contact(session, user.id, turn.contact_id)
    assert group_contact is not None and group_contact.kind.value == "group"
    assert group_contact.telegram_peer_id == group.telegram_chat_id


async def test_group_turns_only_when_addressed(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """006 FR-001, SC-004: an unaddressed message opens no turn (chats formerly set to
    ``always`` included: the setting is gone, see test_migration_0009_drops_group_mode); a reply
    without a tag does; unaddressed messages still reach the draft as history."""
    user = await make_user(state=UserState.active, delay_min_s=1, delay_max_s=1)
    group = await make_chat(user, kind=ChatKind.group)
    member = await make_contact(user, display_name="Synthetic Member")
    await harness.incoming(user, group, member, "Synthetic deploy is at noon", chat_kind="group")
    assert await _turns(session_factory) == []
    assert len(await _messages(session_factory)) == 1

    clock.advance(minutes=5)
    await harness.incoming(
        user, group, member, "So, can you confirm?", chat_kind="group", is_reply_to_me=True
    )
    (turn,) = await _turns(session_factory)
    assert turn.state == TurnState.waiting

    clock.advance(minutes=5)
    await harness.tick()
    (compose,) = fake_llm.calls_for("compose.draft")
    sent = "\n".join(block.content for block in compose.user_blocks)
    assert "Synthetic deploy is at noon" in sent  # stored context is in the draft request


async def test_intake_log_carries_the_reason_and_no_text(
    harness: US1Harness,
    log_capture: LogCapture,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """006 FR-011: every intake decision logs why; message text never reaches the log."""
    user = await make_user(state=UserState.active)
    group = await make_chat(user, kind=ChatKind.group, title="Synthetic Ops")
    member = await make_contact(user, display_name="Synthetic Member")
    other = await make_contact(user, display_name="Synthetic Other")
    texts = ["Synthetic plain chatter", "Synthetic tag for you", "Synthetic reply to you"]
    await harness.incoming(user, group, member, texts[0], chat_kind="group")
    clock.advance(minutes=5)
    await harness.incoming(user, group, member, texts[1], chat_kind="group", mentions_me=True)
    clock.advance(minutes=5)
    await harness.incoming(user, group, other, texts[2], chat_kind="group", is_reply_to_me=True)
    intake = [(e["action"], e["reason"]) for e in log_capture.events("intake")]
    assert intake == [
        ("store_only", "not_addressed"),
        ("new_turn", "tagged"),
        ("new_turn", "reply"),
    ]
    assert not any(text in log_capture.raw for text in texts)


async def test_media_only_notifies_and_is_handled_manually(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Boris")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "", has_media=True)
    await harness.tick()

    (turn,) = await _turns(session_factory)
    assert turn.state == TurnState.handle_manually
    assert turn.dropped_reason == DroppedReason.media_only
    ((notified_user, notice),) = harness.notifications
    assert notified_user == user.id and "Synthetic Boris" in notice and "manually" in notice
    assert gateway.mark_read_calls == []  # nothing read, nothing drafted
    (message,) = await _messages(session_factory)
    assert message.has_media and message.text == ""


# --- context ------------------------------------------------------------------------------------


async def test_load_candidates_window_and_limits(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=600, delay_max_s=600)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    group = await make_chat(user, kind=ChatKind.group)
    async with session_factory() as session:
        base = clock() - timedelta(days=40)
        for i in range(60):  # 10 of them older than the 30-day window
            await messages_repo.add_message(
                session,
                user.id,
                work_chat_id=chat.id,
                telegram_message_id=1000 + i,
                text=f"old line {i}",
                sent_at=base + timedelta(days=i * 0.6),
                is_outgoing=i % 3 == 0,
                sender_contact_id=None if i % 3 == 0 else contact.id,
            )
        for i in range(25):  # the contact also wrote in a group chat
            await messages_repo.add_message(
                session,
                user.id,
                work_chat_id=group.id,
                telegram_message_id=5000 + i,
                text=f"group line {i}",
                sent_at=clock() - timedelta(days=2, minutes=i),
                sender_contact_id=contact.id,
            )
        await session.commit()
    await harness.incoming(user, chat, contact, "First part")
    clock.advance(seconds=10)
    await harness.incoming(user, chat, contact, "Second part")
    (turn,) = await _turns(session_factory)

    async with session_factory() as session:
        candidates = await load_candidates(session, turn, now=clock(), retention_days=30)

    assert [m.text for m in candidates.incoming] == ["First part", "Second part"]
    assert all(m.id.startswith("msg:") for m in candidates.incoming)
    ids = [h.id for h in candidates.history]
    assert len(ids) == len(set(ids))
    assert not {m.id for m in candidates.incoming} & set(ids)
    in_chat = [h for h in candidates.history if not h.text.startswith("group")]
    from_group = [h for h in candidates.history if h.text.startswith("group")]
    # Issue #70: anchored windows. The chat holds 62 messages: the window starts at position 20
    # (42 messages, 40 without the turn); the contact wrote 25 elsewhere (10..29: all of them).
    assert len(in_chat) == 40 and len(from_group) == 25
    assert in_chat[0].text == "old line 20"
    assert all(h.sent_at >= clock() - timedelta(days=30) for h in candidates.history)
    assert [h.sent_at for h in candidates.history] == sorted(h.sent_at for h in candidates.history)
    assert {h.sender for h in candidates.history if h.is_user} == {"me"}
    assert {h.sender for h in candidates.history if not h.is_user} == {"Synthetic Anna"}
    # specs/004 S6: Telegram ids of everything offered, for de-duplicating search matches.
    group_ids = {
        (gid, mid) for gid, mid in candidates.telegram_ids if gid == group.telegram_chat_id
    }
    assert len(group_ids) == 25
    assert len(candidates.telegram_ids) == 40 + 25 + 2
    assert all(
        gid in {chat.telegram_chat_id, group.telegram_chat_id} for gid, _ in candidates.telegram_ids
    )

    # Issue #70: a newer message only appends at the tail; the head stays byte-identical.
    async with session_factory() as session:
        await messages_repo.add_message(
            session,
            user.id,
            work_chat_id=chat.id,
            telegram_message_id=2000,
            text="my reply",
            sent_at=clock() + timedelta(seconds=5),
            is_outgoing=True,
        )
        await session.commit()
        later = await load_candidates(session, turn, now=clock(), retention_days=30)
    assert later.history[: len(candidates.history)] == candidates.history
    assert [h.text for h in later.history[len(candidates.history) :]] == ["my reply"]


# --- composer + scheduler step 1 ---------------------------------------------------------------


async def test_draft_row_records_llm_metadata(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    async with session_factory() as session:
        await messages_repo.add_message(
            session,
            user.id,
            work_chat_id=chat.id,
            telegram_message_id=1,
            text="earlier",
            sent_at=clock() - timedelta(hours=1),
            sender_contact_id=contact.id,
        )
        await session.commit()
    await harness.incoming(user, chat, contact, "Synthetic question")
    await harness.tick()

    (draft,) = await harness.drafts(user)
    (turn,) = await _turns(session_factory)
    assert turn.state == TurnState.pending_review and turn.read_at == clock()
    assert draft.llm_provider == "fake" and draft.llm_model == "fake-large"
    assert draft.prompt_version == "1"
    # Issue #73: plus the selection a 🔁 Regenerate reuses (ids only, no content).
    assert set(draft.llm_usage_json) == {"filter", "compose", "cost_usd", "selection"}
    assert draft.llm_usage_json["compose"]["model"] == "fake-large"
    assert draft.llm_usage_json["filter"]["model"] == "fake-small"
    assert draft.llm_usage_json["cost_usd"] > 0
    assert draft.output_json["system_warnings"] == ["weak_style_profile"]  # no profile built
    assert "text" not in draft.output_json and draft.text == "Synthetic reply"  # encrypted only
    assert draft.remind_at == draft.prepared_at + timedelta(hours=2)
    assert draft.expires_at == draft.prepared_at + timedelta(hours=24)
    async with session_factory() as session:
        record = (
            await session.execute(
                select(ActivityRecord).where(ActivityRecord.kind == ActivityKind.draft_prepared)
            )
        ).scalar_one()
    assert record.draft_id == draft.id and "text" not in record.meta_json


@pytest.mark.parametrize("failure", ["compose", "mark_read"])
async def test_failure_reverts_turn_to_waiting(
    failure: str,
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    if failure == "compose":
        fake_llm.set_response("compose.draft", ProviderUnavailable("synthetic outage"))
    else:
        gateway.fail_mark_read_with = GatewayBusy(600)
    await harness.incoming(user, chat, contact, "Synthetic question")
    await harness.tick()

    (turn,) = await _turns(session_factory)
    assert turn.state == TurnState.waiting
    assert turn.due_at == clock() + timedelta(seconds=60)
    assert await harness.drafts(user) == []
    assert harness.api.requests == []

    # Recovers on a later tick.
    gateway.fail_mark_read_with = None
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    clock.advance(seconds=61)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending


# --- sending ------------------------------------------------------------------------------------


async def _approved_draft(
    harness: US1Harness, user: User, chat: WorkChat, contact: Contact, **incoming_values: Any
) -> tuple[IncomingMessage, Draft]:
    incoming = await harness.incoming(user, chat, contact, "Synthetic question", **incoming_values)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    async with harness.session_factory() as session:
        await drafts_repo.update_draft(
            session, user.id, draft.id, approved_by_user_at=harness.clock()
        )
        await session.commit()
    return incoming, draft


async def test_reply_to_only_in_groups_or_replies(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    private = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    incoming, draft = await _approved_draft(harness, user, private, contact, reply_to_id=42)
    await send_approved_draft(
        harness.session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=harness.clock
    )
    assert gateway.send_text_calls[-1].reply_to == incoming.message_id

    user2 = await make_user(state=UserState.active)
    member = await make_contact(user2)
    group = await make_chat(user2, kind=ChatKind.group)
    incoming, draft = await _approved_draft(
        harness, user2, group, member, chat_kind="group", mentions_me=True
    )
    result = await send_approved_draft(
        harness.session_factory,
        gateway,
        user_id=user2.id,
        draft_id=draft.id,
        edited_text="Edited synthetic",
        clock=harness.clock,
    )
    last = gateway.send_text_calls[-1]
    assert (last.reply_to, last.text) == (incoming.message_id, "Edited synthetic")
    assert result.edited and result.telegram_message_id == last.message_id


async def test_outgoing_storage_is_idempotent_and_send_happens_once(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    _, draft = await _approved_draft(harness, user, chat, contact)
    result = await send_approved_draft(
        session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=harness.clock
    )

    # Telethon may echo the agent's own send as an outgoing event: storing it again is a no-op.
    async with session_factory() as session:
        again = await messages_repo.add_message_if_absent(
            session,
            user.id,
            work_chat_id=chat.id,
            telegram_message_id=result.telegram_message_id,
            text="echo",
            sent_at=harness.clock(),
            is_outgoing=True,
        )
        await session.commit()
        count = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.telegram_message_id == result.telegram_message_id)
        )
    assert count == 1 and again.text == "Synthetic reply"

    with pytest.raises(SendRefused):  # a second approval cannot send twice
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=harness.clock
        )
    assert len(gateway.send_text_calls) == 1


async def test_failed_send_keeps_draft_pending(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    _, draft = await _approved_draft(harness, user, chat, contact)
    gateway.fail_send_with = GatewayBusy(900)
    with pytest.raises(GatewayBusy):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=harness.clock
        )
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
        turn = await turns_repo.get_turn(session, user.id, draft.conversation_turn_id)
    assert stored is not None and stored.state == DraftState.pending
    assert turn is not None and turn.state == TurnState.pending_review
    assert len(await _messages(session_factory)) == 1  # only the incoming one


# --- style profile ------------------------------------------------------------------------------


def _rules() -> StyleRules:
    return StyleRules(
        languages=["ru"],
        formality=2,
        typical_length_words=14,
        greetings=["Привет"],
        sign_offs=[],
        emoji_usage="rare",
        punctuation_habits=["no trailing period"],
        typical_phrases=["пришлю"],
        never_does=["uses formal address"],
    )


async def test_import_and_build_global_profile(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user()
    enabled = await make_chat(user, title="Synthetic work")
    disabled = await make_chat(user, title="Synthetic personal", enabled=False)
    for chat, count in ((enabled, 70), (disabled, 30)):
        gateway.own_history[(user.id, chat.telegram_chat_id)] = [
            IncomingMessage(
                user_id=user.id,
                chat_id=chat.telegram_chat_id,
                message_id=i,
                sender_peer_id=1,
                sender_display_name="",
                text=f"own synthetic line {i}",
                has_media=False,
                sent_at=clock() - timedelta(days=i * 3),  # last ones are older than 6 months
                is_outgoing=True,
            )
            for i in range(count)
        ]
    result = await import_own_messages(
        gateway, session_factory, user.id, settings=settings, clock=clock
    )
    assert all("personal" not in (s.contact_display_name or "") for s in result.samples)
    assert len(result.samples) == result.per_chat_counts[enabled.telegram_chat_id]
    assert 55 <= len(result.samples) <= 62  # ~183 days / 3

    fake_llm.set_response(
        "profile.build",
        StyleProfileOutput(
            rules_json=_rules(), summary_text="Short, casual.", chosen_example_indices=[0, 1, 999]
        ),
    )
    profile = await build_global_profile(
        session_factory, llm_router, user.id, result.samples, settings=settings
    )
    assert profile.scope == ProfileScope.global_ and profile.is_current and profile.version == 1
    assert profile.examples == [result.samples[0].text, result.samples[1].text]
    assert profile.rules_json["weak"] is False and profile.rules_json["formality"] == 2
    assert profile.sample_size == len(result.samples)
    async with session_factory() as session:
        selected = await select_profile(session, user.id, contact_id=None)
    assert selected is not None and selected.id == profile.id


async def test_weak_and_empty_profiles(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    empty = await build_global_profile(session_factory, llm_router, user.id, [], settings=settings)
    assert empty.rules_json["weak"] is True and empty.sample_size == 0
    assert fake_llm.calls_for("profile.build") == []  # nothing to learn from: no LLM call

    fake_llm.set_response(
        "profile.build",
        StyleProfileOutput(
            rules_json=_rules(), summary_text="s", chosen_example_indices=list(range(80))
        ),
    )
    samples = [StyleSample(text=f"line {i}", sent_at=clock()) for i in range(80)]
    rebuilt = await build_global_profile(
        session_factory, llm_router, user.id, samples, settings=settings
    )
    assert rebuilt.version == 2 and rebuilt.rules_json["weak"] is False
    assert len(rebuilt.examples) == settings.profile_examples_global
    few = await build_global_profile(
        session_factory, llm_router, user.id, samples[:10], settings=settings
    )
    assert few.rules_json["weak"] is True
    async with session_factory() as session:
        profiles = await profiles_repo.list_profiles(session, user.id)
    assert [(p.version, p.is_current) for p in profiles] == [(1, False), (2, False), (3, True)]
    assert [p.state.value for p in profiles] == ["retired", "previous", "current"]
