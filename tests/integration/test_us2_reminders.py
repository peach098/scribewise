"""US2 (T044, FR-032): one reminder 2 h after a draft is prepared, expiry after 24 h.
Neither ever sends anything to the contact (Principle I)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ConversationTurn,
    Draft,
    DraftState,
    TurnState,
    User,
    UserState,
)
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import US2Harness

REMINDER = "⏰ Still waiting for your decision on this draft."
EXPIRED = "⌛ Expired — handle manually"


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
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    return US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _pending_draft(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, Draft]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Status of the migration?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    harness.api.clear()
    return user, draft


async def test_reminder_after_2h(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, draft = await _pending_draft(harness, make_user, make_chat, make_contact)
    prepared = draft.prepared_at

    await harness.at(prepared + timedelta(hours=1, minutes=59))
    assert REMINDER not in harness.bot_texts()

    await harness.at(prepared + timedelta(hours=2))
    for minutes in (1, 30, 90):
        await harness.at(prepared + timedelta(hours=2, minutes=minutes))

    reminders = [m for m in harness.messages() if m.text == REMINDER]
    assert len(reminders) == 1  # exactly one
    (reminder,) = reminders
    assert reminder.chat_id == user.telegram_control_chat_id
    assert reminder.reply_parameters is not None or reminder.reply_to_message_id is not None
    (stored,) = await harness.drafts(user)
    assert stored.reminded_at == prepared + timedelta(hours=2)
    assert stored.state == DraftState.pending  # still approvable
    assert gateway.send_text_calls == [] and len(gateway.mark_read_calls) == 1


async def test_expiry_after_24h(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, draft = await _pending_draft(harness, make_user, make_chat, make_contact)
    prepared = draft.prepared_at

    await harness.at(prepared + timedelta(hours=1, minutes=59))
    assert harness.edits() == []
    # The service was down from then on: at 24 h the draft expires without a late reminder.
    await harness.at(prepared + timedelta(hours=24))

    (stored,) = await harness.drafts(user)
    assert stored.state == DraftState.expired
    assert stored.finalized_at == prepared + timedelta(hours=24)
    async with session_factory() as session:
        turn = await session.get(ConversationTurn, stored.conversation_turn_id)
        assert turn is not None and turn.state == TurnState.handle_manually
        expired_records = (
            (
                await session.execute(
                    select(ActivityRecord).where(
                        ActivityRecord.user_id == user.id,
                        ActivityRecord.kind == ActivityKind.expired,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(expired_records) == 1
    (edit,) = harness.edits()
    assert edit.message_id == draft.control_message_id
    assert edit.text is not None and EXPIRED in edit.text
    assert edit.reply_markup is None  # buttons removed
    assert REMINDER not in harness.bot_texts()

    harness.api.clear()
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    (answer,) = harness.api.of_type(AnswerCallbackQuery)
    assert answer.text == "This draft has expired"
    assert gateway.send_text_calls == []  # refused

    await harness.at(prepared + timedelta(hours=30))
    assert len(harness.edits()) == 0  # handled once
