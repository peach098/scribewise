"""Specs/013 end to end: a turn that only informs is skipped as ``informational`` (US2) and the
user's rules decide whether a turn gets a draft, in either direction (US3), through the same
skip flow as 004. A sent "I'll look" draft is a commitment without a date (US1, FR-006).

Intake -> scheduler -> reply gate -> (skip | composer -> card), against the fake LLM, the fake
gateway and an offline Bot API session. Synthetic data only."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    Contact,
    ConversationTurn,
    DecisionSource,
    SkipCategory,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import turns as turns_repo
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import CommitmentExtraction, CommitmentItem, RouteName
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import OutgoingEvent
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import (
    draft_output,
    grounded_reply,
    reply_needed_output,
    select_all_history,
)
from tests.integration.us2_support import US2Harness
from tests.integration.us3_support import block, commitments_of, inline_extractor
from tests.integration.us4_support import add_profile

REPLY = "Synthetic reply."
ROUTE: RouteName = "classify.reply_needed"
FYI = (
    "Synthetic release note: the build moved to Thursday, the test environment is down on "
    "Tuesday from 10 to 12, and the changelog is on the wiki page. Nothing is needed from you."
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
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    fake_llm.set_response("extract.commitments", CommitmentExtraction(items=[]))
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    harness.composer.commitments = inline_extractor(session_factory, llm_router, clock)
    return harness


async def _setup(
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact, name: str = "Anna Testova"
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name=name)
    chat = await make_chat(user, title=name, telegram_chat_id=contact.telegram_peer_id)
    return user, chat, contact


async def _turns(harness: US2Harness, user: User) -> list[ConversationTurn]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.user_id == user.id)
            .order_by(ConversationTurn.arrived_at)
        )
        return list(result.scalars().all())


async def _one_turn(
    harness: US2Harness, user: User, chat: WorkChat, contact: Contact, text: str, **values: Any
) -> ConversationTurn:
    await harness.incoming(user, chat, contact, text, **values)
    await harness.tick()
    return [t for t in await _turns(harness, user) if t.work_chat_id == chat.id][-1]


def _classifier_blocks(fake_llm: FakeAdapter) -> dict[str, Any]:
    call = fake_llm.calls_for(ROUTE)[-1]
    return {b.name: json.loads(b.content) for b in call.user_blocks}


def _shown(harness: US2Harness) -> str:
    for request in reversed(harness.api.requests):
        if isinstance(request, SendMessage | EditMessageText):
            return request.text or ""
    raise AssertionError("nothing shown")


async def _skipped_activity(harness: US2Harness, user: User) -> list[ActivityRecord]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user.id, ActivityRecord.kind == ActivityKind.skipped
            )
        )
        return list(result.scalars().all())


# --- US2: informational turns --------------------------------------------------------------------


async def test_a_long_fyi_with_media_is_skipped_as_informational(
    harness: US2Harness,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    harness.set_reply_needed(reply_needed_output(False, "informational", 0.92))
    turn = await _one_turn(harness, user, chat, contact, FYI, has_media=True)

    assert len(FYI) > 160
    assert turn.state == TurnState.skipped
    assert (turn.reply_decision, turn.skip_category, turn.decision_source) == (
        "skip",
        "informational",
        "model",
    )
    assert await harness.drafts(user) == [] and harness.api.requests == []
    (record,) = await _skipped_activity(harness, user)
    assert record.meta_json == {
        "category": "informational",
        "source": "model",
        "confidence": 0.92,
        "decided_by": "default",
        "has_rules": False,
    }
    blocks = _classifier_blocks(harness.fake_llm)
    assert blocks["incoming_messages"][0]["has_media"] is True
    assert blocks["user_rules"] == {}


async def test_draft_anyway_on_an_informational_skip_produces_a_draft(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    harness.set_reply_needed(reply_needed_output(False, "informational", 0.92))
    skipped = await _one_turn(harness, user, chat, contact, FYI)
    assert skipped.state == TurnState.skipped

    await harness.feed(callback_update(user.telegram_control_chat_id, f"draftany:{skipped.id}"))
    await harness.tick()
    turn = (await _turns(harness, user))[0]
    assert turn.state == TurnState.pending_review
    assert (turn.reply_decision, turn.decision_source, turn.skip_category) == (
        "draft",
        "user",
        "informational",
    )
    assert len(await harness.drafts(user)) == 1
    assert len(harness.fake_llm.calls_for(ROUTE)) == 1  # not asked again


async def test_a_habit_of_answering_informational_turns_drafts_the_next_one(
    harness: US2Harness,
    db_session: AsyncSession,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Model-decided ``informational`` turns count for the habit rule (data-model.md)."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    for days in range(1, 4):
        past = await turns_repo.create_turn(
            db_session,
            user.id,
            work_chat_id=chat.id,
            contact_id=contact.id,
            first_message_id=None,
            arrived_at=clock() - timedelta(days=days),
            delay_s=0,
            due_at=clock() - timedelta(days=days),
            state=TurnState.answered,
        )
        past.skip_category = SkipCategory.informational
        past.decision_source = DecisionSource.model
    await db_session.flush()
    harness.set_reply_needed(reply_needed_output(False, "informational", 0.92))
    turn = await _one_turn(harness, user, chat, contact, FYI)
    assert (turn.state, turn.decision_source) == (TurnState.pending_review, "habit")


# --- US1 (FR-006): "I'll look" is a commitment without a date -----------------------------------


async def test_a_sent_ill_look_draft_is_a_commitment_without_a_due_date(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)

    def respond(call: FakeCall) -> Any:
        return draft_output(
            "Посмотрю",
            sources=[
                {
                    "kind": "message",
                    "id": block(call, "incoming_messages")[0]["id"],
                    "used_for": "the request",
                }
            ],
            flags={
                "creates_commitment": {"value": True, "what": "look at the report", "due": None},
                "sensitive": {"value": False, "categories": []},
            },
        )

    fake_llm.set_response("compose.draft", respond)
    fake_llm.set_response(
        "extract.commitments",
        CommitmentExtraction(
            items=[
                CommitmentItem(
                    what="look at the report",
                    to_whom="Anna Testova",
                    due=None,
                    confidence=0.9,
                    evidence="Посмотрю",
                )
            ]
        ),
    )
    await _one_turn(harness, user, chat, contact, "Пришлёшь отчёт сегодня?")
    (draft,) = await harness.drafts(user)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    (sent,) = gateway.send_text_calls
    assert sent.text == "Посмотрю"
    await gateway.emit_outgoing(
        OutgoingEvent(
            user_id=user.id,
            chat_id=chat.telegram_chat_id,
            message_id=sent.message_id,
            text=sent.text,
            sent_at=clock(),
        )
    )
    await harness.composer.commitments.drain()

    (stored,) = await commitments_of(session_factory, user)
    assert (stored.what, stored.due, stored.contact_id) == (
        "look at the report",
        None,
        contact.id,
    )


# --- US3: user rules in the gate -----------------------------------------------------------------


async def test_a_global_rule_reaches_the_gate_and_a_question_becomes_a_candidate(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    # Without rules a question is never asked about (the pre-gate drafts it).
    harness.set_reply_needed(reply_needed_output(True))
    plain = await _one_turn(harness, user, chat, contact, "когда созвон?")
    assert plain.decision_source == "pregate" and harness.fake_llm.calls_for(ROUTE) == []

    await add_profile(session_factory, user, user_rules=["Не отвечай на объявления"])
    other = await make_contact(user, display_name="Boris Sample")
    other_chat = await make_chat(user, title="Boris", telegram_chat_id=other.telegram_peer_id)
    ruled = await _one_turn(harness, user, other_chat, other, "когда созвон? жду ответа")
    assert ruled.decision_source == "model"
    rules = _classifier_blocks(harness.fake_llm)["user_rules"]["rules"]
    assert [(r["text"], r.get("scope")) for r in rules] == [("Не отвечай на объявления", None)]
    call = harness.fake_llm.calls_for(ROUTE)[-1]
    assert [b.name for b in call.user_blocks if b.trust == "user"] == ["user_rules"]


async def test_a_contact_rule_reaches_only_that_contacts_requests(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, anna = await _setup(make_user, make_chat, make_contact)
    boris = await make_contact(user, display_name="Boris Sample")
    boris_chat = await make_chat(user, title="Boris", telegram_chat_id=boris.telegram_peer_id)
    await add_profile(session_factory, user, user_rules=["Не отвечай на объявления"])
    await add_profile(session_factory, user, contact=anna, user_rules=["Всегда готовь черновик"])
    harness.set_reply_needed(reply_needed_output(True))

    await _one_turn(harness, user, chat, anna, "Релиз перенесли на четверг.")
    anna_rules = _classifier_blocks(harness.fake_llm)["user_rules"]["rules"]
    assert [(r["text"], r["scope"]) for r in anna_rules] == [
        ("Не отвечай на объявления", "global"),
        ("Всегда готовь черновик", "contact"),
    ]
    await _one_turn(harness, user, boris_chat, boris, "Релиз перенесли на четверг.")
    boris_rules = _classifier_blocks(harness.fake_llm)["user_rules"]["rules"]
    assert [r["text"] for r in boris_rules] == ["Не отвечай на объявления"]  # the global one only
    assert all("scope" not in r for r in boris_rules)


async def test_a_rule_skip_is_stored_listed_and_bypasses_the_habit_rule(
    harness: US2Harness,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await add_profile(session_factory, user, user_rules=["Не отвечай на объявления"])
    # The user answered every earlier closing here: the habit rule alone would draft.
    for days in range(1, 4):
        past = await turns_repo.create_turn(
            db_session,
            user.id,
            work_chat_id=chat.id,
            contact_id=contact.id,
            first_message_id=None,
            arrived_at=clock() - timedelta(days=days),
            delay_s=0,
            due_at=clock() - timedelta(days=days),
            state=TurnState.answered,
        )
        past.skip_category = SkipCategory.thanks
        past.decision_source = DecisionSource.model
    await db_session.flush()
    harness.set_reply_needed(reply_needed_output(False, "none", 0.9, decided_by="user_rule"))

    turn = await _one_turn(harness, user, chat, contact, "Объявление: в пятницу общий созвон?")
    assert turn.state == TurnState.skipped
    assert (turn.reply_decision, turn.skip_category, turn.decision_source) == (
        "skip",
        "user_rule",
        "rule",
    )
    assert await harness.drafts(user) == [] and harness.api.requests == []
    (record,) = await _skipped_activity(harness, user)
    assert record.meta_json["decided_by"] == "user_rule" and record.meta_json["has_rules"] is True

    await harness.feed(text_update(user.telegram_control_chat_id, "/skipped"))
    assert "Reason: by your rule" in _shown(harness)


async def test_a_rule_draft_is_stored_with_the_rule_source(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await add_profile(session_factory, user, contact=contact, user_rules=["Всегда готовь черновик"])
    harness.set_reply_needed(
        reply_needed_output(True, "informational", 0.9, decided_by="user_rule")
    )
    turn = await _one_turn(harness, user, chat, contact, "Релиз перенесли на четверг.")
    assert turn.state == TurnState.pending_review
    assert (turn.reply_decision, turn.decision_source) == ("draft", "rule")
    assert len(await harness.drafts(user)) == 1


async def test_skipping_off_for_the_chat_drafts_every_turn_without_the_classifier(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await add_profile(session_factory, user, user_rules=["Не отвечай на объявления"])
    async with session_factory() as session:
        await session.execute(
            update(WorkChat).where(WorkChat.id == chat.id).values(skip_closing=False)
        )
        await session.commit()
    harness.set_reply_needed(reply_needed_output(False, "none", 0.95, decided_by="user_rule"))
    turn = await _one_turn(harness, user, chat, contact, "Объявление: в пятницу созвон.")
    assert turn.state == TurnState.pending_review
    assert (turn.reply_decision, turn.decision_source) == ("draft", "disabled")
    assert harness.fake_llm.calls_for(ROUTE) == []


async def test_a_forwarded_only_turn_is_skipped_whatever_the_rules_say(
    harness: US2Harness,
    clock: FrozenClock,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """FR-008a exception: the forwards-only rule is decided before any user rule is read."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await add_profile(session_factory, user, user_rules=["Всегда готовь черновик"])
    harness.set_reply_needed(reply_needed_output(True, "none", 0.9, decided_by="user_rule"))
    await harness.incoming(
        user, chat, contact, "Synthetic forwarded text", is_forwarded=True, forwarded_from="Origin"
    )
    clock.advance(hours=2)  # a forward waits for the contact's own words first
    await harness.tick()
    (turn,) = await _turns(harness, user)
    assert turn.state == TurnState.skipped and turn.skip_category == "forwarded"
    assert harness.fake_llm.calls_for(ROUTE) == []


async def test_the_habit_count_ignores_rule_decisions(
    db_session: AsyncSession,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Data model: turns decided by a rule (skip or draft) never shape the learned habit; a
    model-decided ``informational`` turn does."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    cases = [
        (SkipCategory.informational, DecisionSource.model, True),
        (SkipCategory.thanks, DecisionSource.rule, False),  # a rule draft with a category
        (SkipCategory.user_rule, DecisionSource.rule, False),  # a rule skip
        (SkipCategory.user_rule, DecisionSource.user, False),  # a rule skip drafted on request
        (SkipCategory.thanks, DecisionSource.model, True),
    ]
    for days, (category, source, _) in enumerate(cases, start=1):
        turn = await turns_repo.create_turn(
            db_session,
            user.id,
            work_chat_id=chat.id,
            contact_id=contact.id,
            first_message_id=None,
            arrived_at=clock() - timedelta(days=days),
            delay_s=0,
            due_at=clock() - timedelta(days=days),
            state=TurnState.answered,
        )
        turn.skip_category = category
        turn.decision_source = source
    await db_session.flush()
    count = await turns_repo.habit_replied_count(
        db_session,
        user.id,
        chat.id,
        before=clock(),
        window=10,
        reply_window=timedelta(minutes=30),
        retention_days=30,
    )
    assert count == sum(counted for _, _, counted in cases)
