"""Issue #47 end to end: a forwarded message is context, never the contact's own words.

A forward alone creates no draft (its turn is skipped as ``forwarded`` and listed in
``/skipped``); with the contact's own message, in either order within the grouping window, it is
one turn whose draft answers that message with the forward as context.

Intake -> scheduler -> reply gate -> composer, against the fake LLM and the fake gateway.
Synthetic data only.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ChatKind,
    Contact,
    ConversationTurn,
    Message,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import messages as messages_repo
from scribe.intake.router import OpenTurn
from scribe.intake.waiting import replan_waiting_turns
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.scheduling.scheduler import DueTurnsProcessor, Scheduler
from scribe.style.importer import import_own_messages
from scribe.style.profiles import refresh_profiles
from scribe.telegram.control_bot.review import source_labels
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage, OutgoingEvent, ReadEvent
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import (
    US1Harness,
    grounded_reply,
    next_message_id,
    select_all_history,
)
from tests.integration.us4_support import add_own_messages, add_profile, by_scope, style_output

REPLY = "Synthetic reply."
FORWARD = "Synthetic origin: the release ships on Friday, I promise."
ORIGIN = "Synthetic Origin"
COMMENT = "Can you check whether this affects our API?"


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
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    return US1Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _setup(
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact, **user_values: Any
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active, delay_min_s=120, delay_max_s=120, **user_values)
    contact = await make_contact(user, display_name="Synthetic Colleague")
    chat = await make_chat(
        user, title="Synthetic Colleague", telegram_chat_id=contact.telegram_peer_id
    )
    return user, chat, contact


async def _turns(harness: US1Harness, user: User) -> list[ConversationTurn]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ConversationTurn).where(ConversationTurn.user_id == user.id)
        )
        return list(result.scalars().all())


async def _messages(harness: US1Harness, user: User) -> list[Message]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(Message).where(Message.user_id == user.id).order_by(Message.sent_at)
        )
        return list(result.scalars().all())


def _shown(harness: US1Harness) -> str:
    """The text of the last message sent or edited in the control chat."""
    for request in reversed(harness.api.requests):
        if isinstance(request, SendMessage | EditMessageText):
            return request.text or ""
    raise AssertionError("nothing shown")


def _incoming_block(harness: US1Harness) -> list[dict[str, Any]]:
    (call,) = harness.fake_llm.calls_for("compose.draft")
    blocks = [json.loads(b.content) for b in call.user_blocks if b.name == "incoming_messages"]
    return list(blocks[0])


async def test_a_forward_alone_is_skipped_as_forwarded_without_a_model_call(
    harness: US1Harness,
    clock: FrozenClock,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """A forward alone creates no draft: its turn is skipped as ``forwarded`` when due, with
    no classifier or drafting call, and is listed in ``/skipped`` with that reason."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    # A long question: the closing pre-gate alone would draft it.
    await harness.incoming(
        user, chat, contact, FORWARD + " What do you think?", is_forwarded=True,
        forwarded_from=ORIGIN,
    )  # fmt: skip
    clock.advance(hours=2)
    await harness.tick()

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.skipped
    assert (turn.reply_decision, turn.decision_source, turn.skip_category) == (
        "skip",
        "pregate",
        "forwarded",
    )
    assert await harness.drafts(user) == []
    assert harness.fake_llm.calls_for("classify.reply_needed") == []
    assert harness.fake_llm.calls_for("compose.draft") == []
    (stored,) = await _messages(harness, user)
    assert stored.is_forwarded and stored.forwarded_from == ORIGIN
    async with harness.session_factory() as session:
        raw = (
            await session.execute(
                text("SELECT forwarded_from FROM messages WHERE id = :id"), {"id": stored.id}
            )
        ).scalar_one()
    assert raw is not None and ORIGIN not in raw  # ciphertext at rest

    await harness.feed(text_update(user.telegram_control_chat_id, "/skipped"))
    shown = _shown(harness)
    assert "Reason: forwarded message" in shown and "Synthetic Colleague" in shown


async def test_forwarded_reason_is_localized_in_skipped(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact, language="ru")
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    clock.advance(hours=2)
    await harness.tick()

    await harness.feed(text_update(user.telegram_control_chat_id, "/skipped"))
    assert "Причина: пересланное сообщение" in _shown(harness)


async def test_draft_anyway_on_a_forwards_only_turn(
    harness: US1Harness,
    clock: FrozenClock,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True, forwarded_from=ORIGIN)
    clock.advance(hours=2)
    await harness.tick()
    (skipped,) = await _turns(harness, user)
    assert skipped.state == TurnState.skipped

    control = user.telegram_control_chat_id
    await harness.feed(callback_update(control, f"draftany:{skipped.id}"))
    await harness.tick()

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review
    assert (turn.decision_source, turn.skip_category) == ("user", "forwarded")
    (draft,) = await harness.drafts(user)
    assert "drafted_on_request" in draft.output_json["system_warnings"]
    assert harness.fake_llm.calls_for("classify.reply_needed") == []
    # The draft may address the forwarded content; it is still marked as forwarded.
    assert [(m["text"], m.get("forwarded"), m.get("forwarded_from")) for m in _incoming_block(
        harness
    )] == [(FORWARD, True, ORIGIN)]  # fmt: skip
    # The card names the forward's origin rather than crediting the forwarder (review finding).
    card = harness.api.of_type(SendMessage)[-1].text or ""
    assert "Synthetic Colleague, forwarded from Synthetic Origin" in card
    async with harness.session_factory() as session:
        user_row = await session.get(User, user.id)
        assert user_row is not None
        user_row.language = "ru"
        labels = await source_labels(session, user_row, draft)
    assert labels and labels[0].startswith("Synthetic Colleague, переслано от Synthetic Origin")
    await harness.feed(callback_update(control, f"approve:{draft.id}"))
    (sent,) = gateway.send_text_calls
    assert (sent.chat_id, sent.text) == (chat.telegram_chat_id, REPLY)


async def test_forward_then_comment_is_one_turn_answering_the_comment(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True, forwarded_from=ORIGIN)
    clock.advance(seconds=3)
    await harness.incoming(user, chat, contact, COMMENT)
    clock.advance(seconds=120)
    await harness.tick()

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review
    assert turn.skip_category is None
    assert len(await harness.drafts(user)) == 1
    incoming = _incoming_block(harness)
    assert [(m["text"], m.get("forwarded"), m.get("forwarded_from")) for m in incoming] == [
        (FORWARD, True, ORIGIN),
        (COMMENT, None, None),
    ]


async def test_a_forward_does_not_close_the_pending_draft(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """A forward is never judged as a closing message: it joins the pending turn and leaves
    the draft outdated (FR-015), like any new message from the contact."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, COMMENT)
    clock.advance(hours=2)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert not draft.outdated
    calls = len(harness.fake_llm.calls_for("classify.reply_needed"))

    await harness.incoming(user, chat, contact, "thanks", is_forwarded=True)

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review
    (draft,) = await harness.drafts(user)
    assert draft.outdated
    assert len(harness.fake_llm.calls_for("classify.reply_needed")) == calls


async def test_comment_then_forward_is_one_turn_answering_the_comment(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, COMMENT)
    clock.advance(seconds=3)
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True, forwarded_from=ORIGIN)
    clock.advance(seconds=120)
    await harness.tick()

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review
    assert len(await harness.drafts(user)) == 1
    incoming = _incoming_block(harness)
    assert [(m["text"], m.get("forwarded"), m.get("forwarded_from")) for m in incoming] == [
        (COMMENT, None, None),
        (FORWARD, True, ORIGIN),
    ]


async def test_a_forward_in_a_group_is_never_addressed(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=120, delay_max_s=120)
    contact = await make_contact(user, display_name="Synthetic Member")
    group = await make_chat(user, title="Synthetic Group", kind=ChatKind.group)
    await harness.incoming(
        user, group, contact, FORWARD, is_forwarded=True, mentions_me=True, is_reply_to_me=True
    )
    clock.advance(hours=2)
    await harness.tick()

    assert await _turns(harness, user) == []
    assert harness.fake_llm.calls_for("classify.reply_needed") == []
    (stored,) = await _messages(harness, user)
    assert stored.is_forwarded and stored.forwarded_from is None


async def test_a_forward_in_a_group_joins_the_senders_open_turn_as_context(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=120, delay_max_s=120)
    contact = await make_contact(user, display_name="Synthetic Member")
    group = await make_chat(user, title="Synthetic Group", kind=ChatKind.group)
    await harness.incoming(user, group, contact, COMMENT, mentions_me=True)
    clock.advance(seconds=3)
    await harness.incoming(user, group, contact, FORWARD, is_forwarded=True, forwarded_from=ORIGIN)
    clock.advance(seconds=120)
    await harness.tick()

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review
    incoming = _incoming_block(harness)
    assert [(m["text"], m.get("forwarded")) for m in incoming] == [
        (COMMENT, None),
        (FORWARD, True),
    ]


# --- style learning: the user's own forwards are not their writing --------------------------------


def _sample_texts(fake_llm: FakeAdapter) -> list[str]:
    texts: list[str] = []
    for call in fake_llm.calls_for("profile.build"):
        for block in call.user_blocks:
            if block.name == "samples":
                texts += [s["text"] for s in json.loads(block.content)]
    return texts


async def test_style_import_skips_outgoing_forwards(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, _ = await _setup(make_user, make_chat, make_contact)

    def own(n: int, **values: Any) -> IncomingMessage:
        return IncomingMessage(
            user_id=user.id, chat_id=chat.telegram_chat_id, message_id=next_message_id(),
            sender_peer_id=0, sender_display_name="", text=f"Synthetic own text {n}",
            has_media=False, sent_at=clock() - timedelta(days=3), is_outgoing=True, **values,
        )  # fmt: skip

    gateway.own_history[(user.id, chat.telegram_chat_id)] = [
        own(1),
        replace(own(2, is_forwarded=True, forwarded_from=ORIGIN), text=FORWARD),
        own(3),
    ]
    imported = await import_own_messages(
        gateway, session_factory, user.id, settings=settings, clock=clock
    )
    assert [s.text for s in imported.samples] == ["Synthetic own text 1", "Synthetic own text 3"]
    assert imported.per_chat_counts == {chat.telegram_chat_id: 2}


async def test_style_refresh_skips_outgoing_forwards(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await add_profile(session_factory, user, summary="Old global summary.")
    await add_profile(session_factory, user, contact=contact, summary="Old contact summary.")
    clock.advance(days=2)
    await add_own_messages(session_factory, user, chat, 14, at=clock() - timedelta(days=1))
    async with session_factory() as session:
        for _ in range(3):
            await messages_repo.add_message(
                session, user.id, work_chat_id=chat.id, telegram_message_id=next_message_id(),
                text=FORWARD, sent_at=clock() - timedelta(days=1), is_outgoing=True,
                is_forwarded=True, forwarded_from=ORIGIN,
            )  # fmt: skip
        await session.commit()
    fake_llm.set_response(
        "profile.build", by_scope(style_output("New global."), style_output("New contact."))
    )

    await refresh_profiles(session_factory, llm_router, user.id, settings=settings, clock=clock)

    samples = _sample_texts(fake_llm)
    assert samples and all(t.startswith("Synthetic own message") for t in samples)
    assert FORWARD not in samples


# --- review round: timing, races and failures -----------------------------------------------------


async def test_a_forwards_only_turn_waits_the_grouping_window_for_the_comment(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """The reading delay (5 s here) is shorter than the grouping window (60 s): the turn is
    held until the window after its last forward has passed."""
    user = await make_user(state=UserState.active, delay_min_s=5, delay_max_s=5)
    contact = await make_contact(user, display_name="Synthetic Colleague")
    chat = await make_chat(
        user, title="Synthetic Colleague", telegram_chat_id=contact.telegram_peer_id
    )
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    clock.advance(seconds=30)
    await harness.incoming(user, chat, contact, FORWARD + " (2)", is_forwarded=True)
    clock.advance(seconds=50)  # 80 s after the first forward, 50 s after the second
    await harness.tick()
    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.waiting

    await harness.incoming(user, chat, contact, COMMENT)
    clock.advance(seconds=10)
    await harness.tick()
    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review
    assert [m.get("forwarded") for m in _incoming_block(harness)] == [True, True, None]


async def test_a_comment_joining_after_the_decision_is_not_skipped(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate decides "forwards only", then the contact's comment joins the reading turn
    before the skip is stored: the skip is refused and the turn is decided again (drafted)."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    decide = harness.gate.decide_for_turn
    joined: list[bool] = []

    async def decide_then_comment(user_id: Any, turn_id: Any) -> Any:
        decision = await decide(user_id, turn_id)
        if not joined:
            joined.append(True)
            await harness.incoming(user, chat, contact, COMMENT)
        return decision

    monkeypatch.setattr(harness.gate, "decide_for_turn", decide_then_comment)
    clock.advance(hours=2)
    await harness.tick()

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review and turn.skip_category is None
    assert [m["text"] for m in _incoming_block(harness)] == [FORWARD, COMMENT]


async def test_a_gate_failure_never_drafts_a_forwards_only_turn(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)

    async def broken(*_: Any) -> Any:
        raise RuntimeError("synthetic gate failure")

    monkeypatch.setattr(harness.gate, "_decide", broken)
    clock.advance(hours=2)
    await harness.tick()
    # The scheduler applies the gate's forwards-only rule itself and stores the skip.
    (turn,) = await _turns(harness, user)
    assert (turn.state, turn.skip_category) == (TurnState.skipped, "forwarded")
    assert await harness.drafts(user) == []

    # A gate failure on a turn with the contact's own words still drafts (004 FR-004).
    await harness.incoming(user, chat, contact, COMMENT)
    clock.advance(hours=2)
    await harness.tick()
    assert len(await harness.drafts(user)) == 1


async def test_without_a_gate_a_forwards_only_turn_is_skipped(
    harness: US1Harness,
    clock: FrozenClock,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    scheduler = Scheduler(harness.session_factory, tick_s=5, clock=clock)
    scheduler.on_due_turns(
        DueTurnsProcessor(
            harness.session_factory,
            gateway,
            harness.composer,
            harness.presenter,
            gate=None,
            clock=clock,
            read_concurrency=1,
            compose_concurrency=1,
        )
    )
    clock.advance(hours=2)
    await scheduler.tick(drain=True)

    (turn,) = await _turns(harness, user)
    assert (turn.state, turn.skip_category) == (TurnState.skipped, "forwarded")
    assert harness.fake_llm.calls_for("compose.draft") == []


async def test_forward_then_mention_in_a_group_is_one_turn(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=120, delay_max_s=120)
    contact = await make_contact(user, display_name="Synthetic Member")
    other = await make_contact(user, display_name="Synthetic Other")
    group = await make_chat(user, title="Synthetic Group", kind=ChatKind.group)
    old = await harness.incoming(user, group, contact, "Old synthetic forward", is_forwarded=True)
    clock.advance(minutes=10)  # outside the window: stays context only
    await harness.incoming(user, group, contact, FORWARD, is_forwarded=True, forwarded_from=ORIGIN)
    clock.advance(seconds=2)
    await harness.incoming(user, group, other, "Synthetic chatter")
    clock.advance(seconds=3)
    await harness.incoming(user, group, contact, COMMENT, mentions_me=True)
    clock.advance(seconds=120)
    await harness.tick()

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review
    incoming = _incoming_block(harness)
    assert [(m["text"], m.get("forwarded")) for m in incoming] == [
        (FORWARD, True),
        (COMMENT, None),
    ]
    assert old.text not in [m["text"] for m in incoming]


# --- third review round ---------------------------------------------------------------------------


async def _group_setup(
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active, delay_min_s=120, delay_max_s=120)
    contact = await make_contact(user, display_name="Synthetic Member")
    group = await make_chat(user, title="Synthetic Group", kind=ChatKind.group)
    return user, group, contact


async def test_a_pulled_in_forward_does_not_pull_in_messages_to_others(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, group, contact = await _group_setup(make_user, make_chat, make_contact)
    await harness.incoming(user, group, contact, FORWARD, is_forwarded=True)
    clock.advance(seconds=2)
    await harness.incoming(user, group, contact, "@bob can you check this?")
    clock.advance(seconds=2)
    await harness.incoming(user, group, contact, "@user fyi", mentions_me=True)
    clock.advance(seconds=120)
    await harness.tick()

    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.pending_review
    assert [(m["text"], m.get("forwarded")) for m in _incoming_block(harness)] == [
        (FORWARD, True),
        ("@user fyi", None),
    ]


async def test_a_later_forward_never_precedes_the_mention_in_the_same_second(
    harness: US1Harness,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Out of order, same second: a forward with a higher Telegram id is not pulled in front
    of the mention; one with a lower id is."""
    user, group, contact = await _group_setup(make_user, make_chat, make_contact)
    later = next_message_id() + 10
    await harness.incoming(user, group, contact, FORWARD, is_forwarded=True, message_id=later)
    mention = await harness.incoming(
        user, group, contact, COMMENT, mentions_me=True, message_id=later - 1
    )
    (turn,) = await _turns(harness, user)
    first = await _message_by_telegram_id(harness, user, mention.message_id)
    assert turn.first_message_id == first.id and turn.opening_message_id is None

    user2, group2, contact2 = await _group_setup(make_user, make_chat, make_contact)
    await harness.incoming(user2, group2, contact2, FORWARD, is_forwarded=True)
    await harness.incoming(user2, group2, contact2, COMMENT, mentions_me=True)
    (turn2,) = await _turns(harness, user2)
    forward = await _message_by_telegram_id(harness, user2, None, text=FORWARD)
    assert turn2.first_message_id == forward.id and turn2.opening_message_id is not None


async def _message_by_telegram_id(
    harness: US1Harness, user: User, telegram_id: int | None, *, text: str | None = None
) -> Message:
    for message in await _messages(harness, user):
        if telegram_id is not None and message.telegram_message_id == telegram_id:
            return message
        if text is not None and message.text == text:
            return message
    raise AssertionError("message not found")


async def test_the_reply_quotes_the_contacts_own_message_not_a_later_forward(
    harness: US1Harness,
    clock: FrozenClock,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Private chat: the comment is itself a reply (so the answer quotes it); a forward joins
    after it. Group: the mention is quoted, not the forward after it."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    comment = await harness.incoming(user, chat, contact, COMMENT, reply_to_id=1)
    clock.advance(seconds=3)
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    clock.advance(seconds=120)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    own = await _message_by_telegram_id(harness, user, comment.message_id)
    assert draft.output_json["reply_to_message_id"] == str(own.id)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    (sent,) = gateway.send_text_calls
    assert sent.reply_to == comment.message_id

    user2, group, member = await _group_setup(make_user, make_chat, make_contact)
    mention = await harness.incoming(user2, group, member, COMMENT, mentions_me=True)
    clock.advance(seconds=3)
    await harness.incoming(user2, group, member, FORWARD, is_forwarded=True)
    clock.advance(seconds=200)
    await harness.tick()
    (draft2,) = await harness.drafts(user2)
    await harness.feed(callback_update(user2.telegram_control_chat_id, f"approve:{draft2.id}"))
    assert gateway.send_text_calls[-1].reply_to == mention.message_id


async def test_draft_anyway_on_forwards_only_replies_to_the_last_forward(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    clock.advance(seconds=3)
    last = await harness.incoming(user, chat, contact, FORWARD + " (2)", is_forwarded=True)
    clock.advance(hours=2)
    await harness.tick()
    (skipped,) = await _turns(harness, user)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"draftany:{skipped.id}"))
    await harness.tick()
    (draft,) = await harness.drafts(user)
    stored = await _message_by_telegram_id(harness, user, last.message_id)
    assert draft.output_json["reply_to_message_id"] == str(stored.id)


async def test_a_replan_keeps_the_forwards_only_hold(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=5, delay_max_s=5)
    contact = await make_contact(user, display_name="Synthetic Colleague")
    chat = await make_chat(
        user, title="Synthetic Colleague", telegram_chat_id=contact.telegram_peer_id
    )
    first = clock()
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    clock.advance(seconds=30)
    await harness.incoming(user, chat, contact, FORWARD + " (2)", is_forwarded=True)
    clock.advance(seconds=10)
    async with harness.session_factory() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        await replan_waiting_turns(session, stored, clock())
        await session.commit()
    (turn,) = await _turns(harness, user)
    assert turn.due_at >= first + timedelta(seconds=30 + 60)


async def test_the_hold_never_delays_a_turn_the_user_asked_for(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """A forward joining a "Draft anyway" turn or a "read now" turn leaves it due now."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    forward = await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    clock.advance(hours=2)
    await harness.tick()
    (skipped,) = await _turns(harness, user)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"draftany:{skipped.id}"))
    (asked,) = await _turns(harness, user)
    due = asked.due_at
    await harness.incoming(
        user, chat, contact, "x", is_forwarded=True, sent_at=forward.sent_at + timedelta(seconds=5)
    )
    (turn,) = await _turns(harness, user)
    assert turn.last_message_id != asked.last_message_id and turn.due_at == due

    user2, chat2, contact2 = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user2, chat2, contact2, FORWARD, is_forwarded=True)
    (waiting,) = await _turns(harness, user2)
    async with harness.session_factory() as session:
        row = await session.get(ConversationTurn, waiting.id)
        assert row is not None
        read_now_at = row.due_at = clock()
        await activity_repo.record(
            session, user2.id, ActivityKind.read_now, at=clock(), conversation_turn_id=row.id
        )
        await session.commit()
    clock.advance(seconds=5)
    await harness.incoming(user2, chat2, contact2, "y", is_forwarded=True)
    (turn2,) = await _turns(harness, user2)
    assert turn2.due_at == read_now_at


async def test_a_message_never_attaches_to_a_turn_that_left_waiting(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interleaving: intake looked the open turn up while it was waiting, then the scheduler
    skipped it before the attach. The guarded attach fails and the comment opens a new turn."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    forward = await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    clock.advance(hours=2)
    await harness.tick()
    (skipped,) = await _turns(harness, user)
    assert skipped.state == TurnState.skipped

    async def stale_open_turn(*_: Any) -> OpenTurn:
        return OpenTurn(turn_id=skipped.id, last_message_at=forward.sent_at)

    monkeypatch.setattr(harness.intake, "_open_turn", stale_open_turn)
    await harness.incoming(
        user, chat, contact, COMMENT, sent_at=forward.sent_at + timedelta(seconds=10)
    )

    turns = await _turns(harness, user)
    assert len(turns) == 2
    old = next(t for t in turns if t.id == skipped.id)
    assert (old.state, old.last_message_id) == (TurnState.skipped, skipped.last_message_id)
    new = next(t for t in turns if t.id != skipped.id)
    assert new.state == TurnState.waiting


# --- fourth review round --------------------------------------------------------------------------


async def test_the_opening_message_is_the_cut_off_for_read_and_manual_reply(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Group turn pulled back to forward F, opened by mention O: a read or a manual reply
    between F and O has not seen O, so the turn stays waiting (not due now, not dropped)."""
    user, group, contact = await _group_setup(make_user, make_chat, make_contact)
    forward = await harness.incoming(user, group, contact, FORWARD, is_forwarded=True)
    between = next_message_id()
    await harness.incoming(user, group, contact, COMMENT, mentions_me=True)
    (turn,) = await _turns(harness, user)
    assert turn.opening_message_id is not None
    due = turn.due_at

    await gateway.emit_read_elsewhere(
        ReadEvent(user_id=user.id, chat_id=group.telegram_chat_id, max_message_id=between)
    )
    await gateway.emit_outgoing(
        OutgoingEvent(
            user_id=user.id,
            chat_id=group.telegram_chat_id,
            message_id=between,
            text="Synthetic reply to someone else",
            sent_at=clock(),
            chat_kind="group",
        )
    )
    (turn,) = await _turns(harness, user)
    assert (turn.state, turn.due_at) == (TurnState.waiting, due)
    assert forward.message_id < between


async def test_the_contacts_own_message_releases_the_hold(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=5, delay_max_s=5)
    contact = await make_contact(user, display_name="Synthetic Colleague")
    chat = await make_chat(
        user, title="Synthetic Colleague", telegram_chat_id=contact.telegram_peer_id
    )
    first = clock()
    await harness.incoming(user, chat, contact, FORWARD, is_forwarded=True)
    (held,) = await _turns(harness, user)
    assert held.forwards_hold_until == first + timedelta(seconds=60)
    assert held.due_at == first + timedelta(seconds=60)
    clock.advance(seconds=10)
    await harness.incoming(user, chat, contact, COMMENT)
    (turn,) = await _turns(harness, user)
    assert turn.forwards_hold_until is None and turn.due_at == first + timedelta(seconds=5)

    clock.advance(seconds=2)
    async with harness.session_factory() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        await replan_waiting_turns(session, stored, clock())
        await session.commit()
    (turn,) = await _turns(harness, user)
    assert turn.due_at < first + timedelta(seconds=60)  # the normal delay again


async def test_the_gate_sees_the_senders_messages_between_forward_and_mention(
    harness: US1Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, group, contact = await _group_setup(make_user, make_chat, make_contact)
    await harness.incoming(user, group, contact, FORWARD, is_forwarded=True)
    clock.advance(seconds=2)
    await harness.incoming(user, group, contact, "@bob can you check this?")
    clock.advance(seconds=2)
    await harness.incoming(user, group, contact, "@user thanks", mentions_me=True)
    clock.advance(seconds=200)
    await harness.tick()

    (call,) = harness.fake_llm.calls_for("classify.reply_needed")
    blocks = {b.name: json.loads(b.content) for b in call.user_blocks}
    assert [m["text"] for m in blocks["incoming_messages"]] == [FORWARD, "@user thanks"]
    assert "@bob can you check this?" in [m["text"] for m in blocks["history"]]
    assert FORWARD not in [m["text"] for m in blocks["history"]]
