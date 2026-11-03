"""A suspended user's drafts are never sent or shown (specs/005 T020, research Q6, Q7).

Synthetic data only. The gate is off here: these are the agent-side choke points that hold even
if the bot middleware were bypassed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    Contact,
    ConversationTurn,
    Draft,
    DraftState,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.sending.send import AccessSuspended, SendRefused, send_approved_draft
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update
from tests.integration.us1_support import US1Harness, grounded_reply, select_all_history

REPLY = "Synthetic reply: the report is due on Friday."


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


async def _drafted(
    harness: US1Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, WorkChat, Contact, Draft]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending
    return user, chat, contact, draft


async def _suspend(
    session_factory: async_sessionmaker[AsyncSession], user: User, clock: FrozenClock
) -> None:
    async with session_factory() as session:
        await users_repo.update_user(session, user.id, access_suspended_at=clock())
        await session.commit()


async def _stored(
    session_factory: async_sessionmaker[AsyncSession], user: User, draft: Draft
) -> tuple[Draft, ConversationTurn]:
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
        assert stored is not None
        turn = await session.get(ConversationTurn, stored.conversation_turn_id)
        assert turn is not None
        return stored, turn


def test_access_suspended_is_a_send_refusal() -> None:
    assert issubclass(AccessSuspended, SendRefused)


async def test_send_refused_for_suspended_user(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    async with session_factory() as session:
        await drafts_repo.update_draft(session, user.id, draft.id, approved_by_user_at=clock())
        await session.commit()
    await _suspend(session_factory, user, clock)

    with pytest.raises(AccessSuspended):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
        )

    assert gateway.send_text_calls == []
    stored, turn = await _stored(session_factory, user, draft)
    assert stored.state == DraftState.pending and stored.sent_telegram_message_id is None
    assert turn.state == TurnState.pending_review


async def test_approve_of_suspended_user_replies_access_ended(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """The race between a tap and the reconcile job: the send path itself refuses."""
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _suspend(session_factory, user, clock)
    harness.api.clear()

    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))

    assert gateway.send_text_calls == []
    expected = t("access.ended_reply", "en")
    assert expected in harness.api.sent_texts
    stored, _ = await _stored(session_factory, user, draft)
    assert stored.state != DraftState.sent


async def test_card_of_suspended_user_is_expired_not_shown(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Spec edge case "Access ends mid-draft": a compose that finishes after the suspension."""
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _suspend(session_factory, user, clock)
    harness.api.clear()

    await harness.presenter.post_card(user.id, draft.id)

    assert harness.api.of_type(SendMessage) == []
    stored, turn = await _stored(session_factory, user, draft)
    assert stored.state == DraftState.expired and stored.finalized_at == clock()
    assert turn.state == TurnState.handle_manually


async def test_weekly_refresh_skips_suspended_users(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    clock: FrozenClock,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scribe import jobs
    from scribe.style.profiles import RefreshResult

    serving = await make_user(state=UserState.active)
    await make_user(state=UserState.active, access_suspended_at=clock())
    refreshed: list[uuid.UUID] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> RefreshResult:
        refreshed.append(args[3])
        raise RuntimeError("synthetic: stop after recording the user")

    monkeypatch.setattr(jobs, "refresh_user", fake_refresh)
    await jobs.refresh_all_users(session_factory, llm_router, None, settings=settings, clock=clock)

    assert refreshed == [serving.id]
