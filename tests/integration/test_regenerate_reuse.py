"""Issue #73: 🔁 Regenerate reuses the stored context selection while the turn's newest message is
unchanged (only the drafting call runs again), selects afresh after new messages, and is capped
per draft with a hint. Synthetic data only."""

from __future__ import annotations

import itertools
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context.earlier import EarlierSearch
from scribe.db.models import Contact, Draft, DraftState, User, UserState, WorkChat
from scribe.db.repos import messages as messages_repo
from scribe.drafting.selection import regenerations, stored_selection
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import DraftOutput
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import US2Harness


def _make_harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    **overrides: Any,
) -> US2Harness:
    numbers = itertools.count(1)

    def reply(call: FakeCall) -> DraftOutput:
        return grounded_reply(f"Synthetic reply number {next(numbers)}")(call)

    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", reply)
    effective = settings.model_copy(update=overrides) if overrides else settings
    harness = US2Harness(session_factory, effective, llm_router, fake_llm, gateway, clock)
    # As in the service: the scheduler's composer and 🔁 Regenerate both may search.
    harness.composer.earlier_search = EarlierSearch(gateway, effective, clock=clock)
    return harness


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    return _make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _setup(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active, delay_min_s=0, delay_max_s=0)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    async with session_factory() as session:  # recent history: the filter has candidates
        await messages_repo.add_message(
            session,
            user.id,
            work_chat_id=chat.id,
            telegram_message_id=1,
            text="Synthetic earlier line",
            sent_at=clock() - timedelta(hours=1),
            sender_contact_id=contact.id,
        )
        await session.commit()
    return user, chat, contact


async def _first_draft(harness: US2Harness, user: User, chat: WorkChat, contact: Contact) -> Draft:
    await harness.incoming(user, chat, contact, "Synthetic question about the report")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    return draft


async def _regen(harness: US2Harness, user: User, draft: Draft) -> Draft:
    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{draft.id}"))
    return (await harness.drafts(user))[-1]


def _answers(harness: US2Harness) -> list[AnswerCallbackQuery]:
    return harness.api.of_type(AnswerCallbackQuery)


async def test_regenerate_reuses_selection_and_reruns_compose(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(session_factory, clock, make_user, make_chat, make_contact)
    v1 = await _first_draft(harness, user, chat, contact)
    (turn,) = await harness.turns(user)
    selection = stored_selection(v1.llm_usage_json)
    assert selection is not None and selection.last_message_id == str(turn.last_message_id)
    assert len(selection.history_ids) == 1
    assert "regenerations" not in v1.llm_usage_json

    v2 = await _regen(harness, user, v1)

    assert v2.version == 2 and v2.state == DraftState.pending
    assert len(fake_llm.calls_for("retrieve.filter_context")) == 1  # the selection was reused
    assert len(fake_llm.calls_for("compose.draft")) == 2  # the draft was written again
    assert v2.text != v1.text
    blocks = fake_llm.calls_for("compose.draft")[-1].user_blocks
    history = next(b for b in blocks if b.name == "history")
    assert "Synthetic earlier line" in history.content  # same context as the first draft
    assert v2.llm_usage_json["selection_reused"] is True
    assert v2.llm_usage_json["filter"] is None
    assert regenerations(v2.llm_usage_json) == 1
    assert stored_selection(v2.llm_usage_json) == selection


async def test_new_message_selects_afresh(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(session_factory, clock, make_user, make_chat, make_contact)
    v1 = await _first_draft(harness, user, chat, contact)
    await harness.incoming(user, chat, contact, "Synthetic follow-up")  # joins the turn
    (v1,) = await harness.drafts(user)
    assert v1.outdated

    v2 = await _regen(harness, user, v1)

    assert len(fake_llm.calls_for("retrieve.filter_context")) == 2  # not the stale selection
    assert "selection_reused" not in v2.llm_usage_json
    assert regenerations(v2.llm_usage_json) == 0  # new messages start the count again
    (turn,) = await harness.turns(user)
    selection = stored_selection(v2.llm_usage_json)
    assert selection is not None and selection.last_message_id == str(turn.last_message_id)
    incoming = next(
        b for b in fake_llm.calls_for("compose.draft")[-1].user_blocks
        if b.name == "incoming_messages"
    )  # fmt: skip
    assert "Synthetic follow-up" in incoming.content


@pytest.mark.parametrize(("lang", "hint"), [("en", "regenerated 2 times"), ("ru", "2 раза")])
async def test_regenerate_cap_shows_a_hint(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    lang: str,
    hint: str,
) -> None:
    harness = _make_harness(
        session_factory, settings, llm_router, fake_llm, gateway, clock, regenerate_max_per_draft=2
    )
    user, chat, contact = await _setup(session_factory, clock, make_user, make_chat, make_contact)
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == user.id).values(language=lang))
        await session.commit()
    v1 = await _first_draft(harness, user, chat, contact)
    v2 = await _regen(harness, user, v1)
    v3 = await _regen(harness, user, v2)
    assert (v3.version, regenerations(v3.llm_usage_json)) == (3, 2)
    composed = len(fake_llm.calls_for("compose.draft"))

    refused = await _regen(harness, user, v3)

    assert refused.id == v3.id and refused.state == DraftState.pending  # nothing changed
    assert len(fake_llm.calls_for("compose.draft")) == composed
    answer = _answers(harness)[-1]
    assert answer.show_alert and hint in (answer.text or "")

    # New messages: the draft is outdated, and only a regenerate covers them.
    await harness.incoming(user, chat, contact, "Synthetic follow-up")
    v4 = await _regen(harness, user, v3)
    assert v4.version == 4 and regenerations(v4.llm_usage_json) == 0
