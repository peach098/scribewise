"""US1 end to end (T029): message -> draft card -> approve / edit / reject; Principle I."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Contact,
    ConversationTurn,
    Draft,
    DraftState,
    Message,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.sending.send import SendRefused, send_approved_draft
from scribe.style.profiles import refresh_profiles
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser, next_telegram_id
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import US1Harness, grounded_reply, select_all_history
from tests.integration.us4_support import add_own_messages, add_profile, style_output

REPLY = "Привет! Отчёт будет к пятнице, пришлю."


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
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    return user, chat, contact


async def _drafted(
    harness: US1Harness,
    user: User,
    chat: WorkChat,
    contact: Contact,
    text: str = "Когда будет отчёт?",
) -> tuple[IncomingMessage, Draft]:
    incoming = await harness.incoming(user, chat, contact, text)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    return incoming, draft


async def _count(session_factory: async_sessionmaker[AsyncSession], query: Select[Any]) -> int:
    async with session_factory() as session:
        return int((await session.execute(query)).scalar_one())


async def test_approve_sends_and_marks_sent(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    incoming, draft = await _drafted(harness, user, chat, contact)

    # The turn was read only now, and a card was posted to the control chat.
    assert [(c.chat_id, c.up_to_message_id) for c in gateway.mark_read_calls] == [
        (chat.telegram_chat_id, incoming.message_id)
    ]
    assert draft.state == DraftState.pending and draft.text == REPLY
    (card,) = harness.api.of_type(SendMessage)
    assert card.chat_id == user.telegram_control_chat_id
    assert REPLY in card.text and "Synthetic Anna" in card.text and "✍️ Draft" in card.text
    assert draft.control_message_id is not None
    assert gateway.send_text_calls == []

    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))

    (sent,) = gateway.send_text_calls
    assert (sent.chat_id, sent.text, sent.reply_to) == (chat.telegram_chat_id, REPLY, None)
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
        assert stored is not None
        assert stored.state == DraftState.sent
        assert stored.approved_by_user_at is not None and stored.finalized_at is not None
        assert stored.sent_telegram_message_id == sent.message_id
        turn = await session.get(ConversationTurn, stored.conversation_turn_id)
        assert turn is not None and turn.state == TurnState.answered
        outgoing = (
            await session.execute(
                select(Message).where(
                    Message.work_chat_id == chat.id,
                    Message.telegram_message_id == sent.message_id,
                )
            )
        ).scalar_one()
        assert outgoing.is_outgoing and outgoing.text == REPLY
        kinds = (
            (
                await session.execute(
                    select(ActivityRecord.kind).where(ActivityRecord.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
    assert {ActivityKind.draft_prepared, ActivityKind.approved, ActivityKind.sent} <= set(kinds)
    edits = harness.api.of_type(EditMessageText)
    assert edits and "✅ Sent" in (edits[-1].text or "") and edits[-1].reply_markup is None


async def test_edit_sends_edited_text(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    _, draft = await _drafted(harness, user, chat, contact)
    control = user.telegram_control_chat_id

    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    assert gateway.send_text_calls == []
    assert any("your version" in t for t in harness.api.sent_texts)

    await harness.feed(text_update(control, "Отчёт будет в пятницу утром."))

    (sent,) = gateway.send_text_calls
    assert sent.text == "Отчёт будет в пятницу утром."
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
        assert stored is not None and stored.state == DraftState.sent
        assert stored.edited_text == "Отчёт будет в пятницу утром." and stored.text == REPLY
        kinds = (
            (
                await session.execute(
                    select(ActivityRecord.kind).where(ActivityRecord.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
    assert ActivityKind.edited_and_approved in kinds
    assert any("✅ Sent (edited)" in (e.text or "") for e in harness.api.of_type(EditMessageText))

    # The edit state is over: a later text message sends nothing.
    await harness.feed(text_update(control, "Just chatting with the bot"))
    assert len(gateway.send_text_calls) == 1


async def test_reject_sends_nothing(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    _, draft = await _drafted(harness, user, chat, contact)

    await harness.feed(callback_update(user.telegram_control_chat_id, f"reject:{draft.id}"))

    assert gateway.send_text_calls == []
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
        assert stored is not None and stored.state == DraftState.rejected
        assert stored.finalized_at is not None and stored.approved_by_user_at is None
        turn = await session.get(ConversationTurn, stored.conversation_turn_id)
        assert turn is not None and turn.state == TurnState.rejected
    assert any("❌ Rejected" in (e.text or "") for e in harness.api.of_type(EditMessageText))


async def _drive_us4_controls(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    chat: WorkChat,
) -> None:
    control = user.telegram_control_chat_id
    harness.fake_llm.set_response("profile.build", style_output())
    global_profile = await add_profile(session_factory, user)
    async with session_factory() as session:
        accept, dismiss = [
            await profiles_repo.create_proposal(
                session,
                user.id,
                style_profile_id=global_profile.id,
                rule_json={"key": key, "text": rule},
                evidence_draft_ids=[],
            )  # fmt: skip
            for key, rule in (("emoji_removed", "no emojis"), ("shorter", "keep it short"))
        ]
        await session.commit()
    await harness.feed(callback_update(control, f"rule_accept:{accept.id}"))
    await harness.feed(callback_update(control, f"rule_dismiss:{dismiss.id}"))
    harness.clock.advance(hours=1)
    async with session_factory() as session:  # the user's own messages in another work chat
        other_chat = await chats_repo.upsert_work_chat(
            session, user.id, telegram_chat_id=next_telegram_id(), title="Synthetic Team",
            kind=ChatKind.private, enabled=True,
        )  # fmt: skip
        await session.commit()
    await add_own_messages(
        session_factory, user, other_chat, 12, at=harness.clock() - timedelta(minutes=30)
    )
    refreshed = await refresh_profiles(
        session_factory, harness.llm_router, user.id, settings=harness.settings,
        clock=harness.clock,
    )  # fmt: skip
    assert refreshed.batch_id is not None
    await harness.feed(callback_update(control, f"undo_refresh:{refreshed.batch_id}"))
    assert any(t.startswith("↩️ Restored") for t in harness.api.sent_texts)
    await harness.feed(callback_update(control, "profrebuild:0"))
    await harness.feed(callback_update(control, "profrebuild:1"))
    assert any(t.startswith("✅ Rebuilt") for t in harness.api.sent_texts)
    await harness.feed(callback_update(control, f"chatenable:{chat.id}:off"))
    await harness.feed(callback_update(control, f"chatenable:{chat.id}:on"))
    await harness.feed(text_update(control, "/ooo On vacation until Monday"))
    await harness.feed(text_update(control, "/ooo"))


async def test_no_send_without_approval(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    other = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    # Keyword-flagged as sensitive: Send needs an extra confirmation.
    _, v1 = await _drafted(harness, user, chat, contact, "Пришли, пожалуйста, пароль от стенда")

    for _ in range(3):  # scheduler ticks alone never send
        await harness.tick()

    await harness.feed(callback_update(control, f"sources:{v1.id}"))
    await harness.feed(callback_update(control, f"approve:{v1.id}"))  # asks to confirm only
    assert any("Send anyway?" in t for t in harness.api.sent_texts)
    await harness.feed(callback_update(control, f"edit:{v1.id}"))
    await harness.feed(text_update(control, "/cancel"))
    await harness.feed(callback_update(control, f"regen:{v1.id}"))

    v1_after, v2 = await harness.drafts(user)
    assert v1_after.state == DraftState.superseded
    assert v2.state == DraftState.pending and v2.version == 2

    # The superseded draft can no longer be approved, even with the confirmation step.
    await harness.feed(callback_update(control, f"approve:{v1.id}:confirm"))
    await harness.feed(callback_update(control, f"approve:{v1.id}:anyway"))
    await harness.feed(callback_update(control, f"approve:{v1.id}:anyway_confirm"))
    await harness.feed(callback_update(control, f"edit:{v1.id}"))
    assert any("This draft has expired" in t for t in _answers(harness))
    # Another user cannot act on this user's draft.
    await harness.feed(callback_update(other.telegram_control_chat_id, f"approve:{v2.id}:confirm"))
    await harness.feed(callback_update(other.telegram_control_chat_id, f"approve:{v2.id}:anyway"))
    await harness.feed(callback_update(other.telegram_control_chat_id, f"reject:{v2.id}"))
    await harness.feed(callback_update(control, f"reject:{v2.id}"))
    await harness.feed(callback_update(control, f"approve:{v2.id}:confirm"))
    await harness.feed(callback_update(control, f"approve:{v2.id}:anyway"))
    await harness.feed(callback_update(control, "approve:not-a-uuid"))
    await harness.feed(callback_update(control, "approve:not-a-uuid:anyway"))

    clock.advance(hours=25)
    for _ in range(3):
        await harness.tick()

    # US4 buttons and commands never send either (Principle I): rule suggestions, undo of a
    # refresh, rebuild, disabling / enabling the chat, out of office.
    await _drive_us4_controls(harness, session_factory, user, chat)
    for _ in range(3):
        await harness.tick()

    assert gateway.send_text_calls == []
    async with session_factory() as session:
        sent = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.is_outgoing.is_(True), Message.work_chat_id == chat.id)
        )
    assert sent == 0
    final = await harness.drafts(user)
    assert [d.state for d in final] == [DraftState.superseded, DraftState.rejected]
    assert all(d.approved_by_user_at is None for d in final)


def _answers(harness: US1Harness) -> list[str]:
    return [a.text or "" for a in harness.api.of_type(AnswerCallbackQuery)] + harness.api.sent_texts


async def test_non_work_chat_ignored_and_not_stored(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, _, contact = await _setup(make_user, make_chat, make_contact)
    disabled = await make_chat(user, title="Synthetic family chat", enabled=False)

    for chat_id in (next_telegram_id(), disabled.telegram_chat_id):
        await gateway.emit_new_message(
            IncomingMessage(
                user_id=user.id,
                chat_id=chat_id,
                message_id=1,
                sender_peer_id=contact.telegram_peer_id,
                sender_display_name="Synthetic Anna",
                text="Synthetic personal message",
                has_media=False,
                sent_at=harness.clock(),
            )
        )
    await harness.tick()

    assert await _count(session_factory, select(func.count()).select_from(Message)) == 0
    assert await _count(session_factory, select(func.count()).select_from(ConversationTurn)) == 0
    assert gateway.mark_read_calls == [] and gateway.send_text_calls == []
    assert harness.api.requests == [] and harness.notifications == []


async def test_sent_requires_approved_at_db_constraint(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    _, draft = await _drafted(harness, user, chat, contact)

    # send.py refuses a draft nobody approved...
    with pytest.raises(SendRefused):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
        )
    assert gateway.send_text_calls == []

    # ...and the database refuses "sent" without approved_by_user_at.
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
        assert stored is not None
        stored.state = DraftState.sent
        with pytest.raises(IntegrityError, match="sent_requires_approval"):
            await session.flush()
        await session.rollback()

    # Approved but expired: still refused.
    async with session_factory() as session:
        await drafts_repo.update_draft(
            session, user.id, draft.id, approved_by_user_at=clock(), expires_at=clock()
        )
        await session.commit()
    clock.advance(seconds=1)
    with pytest.raises(SendRefused):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
        )
    assert gateway.send_text_calls == []
