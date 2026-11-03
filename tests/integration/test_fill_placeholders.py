"""Issue #2: filling a needs-info draft's placeholders by replying with the missing values."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    Contact,
    Draft,
    DraftState,
    User,
    UserState,
    WorkChat,
)
from scribe.drafting.stored_output import load_stored_output
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import US1Harness, grounded_reply, select_all_history

ONE = "Отчёт будет готов [[день]]."
TWO = "Отчёт будет [[день]] к [[время]], [[день]] точно."
PLAIN = "Привет! Отчёт будет к пятнице, пришлю."


def _needs_info(text: str, *questions: tuple[str, str], **overrides: Any) -> Any:
    placeholders = [{"marker": m, "question": q} for m, q in questions]
    return grounded_reply(text, needs_info=True, placeholders=placeholders, **overrides)


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
    fake_llm.set_response("compose.draft", _needs_info(ONE, ("[[день]]", "Which day?")))
    return US1Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _drafted(
    harness: US1Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, WorkChat, Contact, Draft]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Когда будет отчёт?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    return user, chat, contact, draft


async def _kinds(session_factory: async_sessionmaker[AsyncSession], user: User) -> list[Any]:
    async with session_factory() as session:
        result = await session.execute(
            select(ActivityRecord.kind).where(ActivityRecord.user_id == user.id)
        )
        return list(result.scalars().all())


async def test_reply_with_one_value_posts_a_new_version_and_never_sends(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id
    (card,) = harness.api.of_type(SendMessage)
    assert "❓ Needs info:\n1. Which day?\n↩️ Reply with the missing value" in card.text
    harness.api.clear()

    await harness.feed(text_update(control, "  в пятницу  ", reply_to=draft.control_message_id))

    assert gateway.send_text_calls == []
    old, new = await harness.drafts(user)
    assert old.id == draft.id and old.state == DraftState.superseded
    assert new.version == 2 and new.state == DraftState.pending and not new.outdated
    assert new.text == new.edited_text == "Отчёт будет готов в пятницу."
    output = load_stored_output(new.output_json, new.text).output
    assert output is not None and output.needs_info is False and output.placeholders == []
    assert new.output_json["needs_info"] is False and "placeholders" not in new.output_json
    assert "в пятницу" not in str(new.output_json)  # values only in the encrypted columns
    edits = harness.api.of_type(EditMessageText)
    assert edits and "✍️ Superseded" in (edits[-1].text or "") and edits[-1].reply_markup is None
    (new_card,) = harness.api.of_type(SendMessage)
    assert "Отчёт будет готов в пятницу." in new_card.text and "❓" not in new_card.text
    assert new.control_message_id is not None and new.control_message_id != old.control_message_id
    assert "в пятницу" not in log_capture.raw
    assert log_capture.events("placeholders_filled")

    # All placeholders filled: Send asks nothing and sends the values as an edited reply.
    harness.api.clear()
    await harness.feed(callback_update(control, f"approve:{new.id}"))
    assert not any("Send anyway?" in t for t in harness.api.sent_texts)
    (sent,) = gateway.send_text_calls
    assert (sent.chat_id, sent.text) == (chat.telegram_chat_id, "Отчёт будет готов в пятницу.")
    async with session_factory() as session:
        stored = await session.get(Draft, new.id)
        assert stored is not None and stored.state == DraftState.sent
        assert stored.edited_text == "Отчёт будет готов в пятницу."
    assert ActivityKind.edited_and_approved in await _kinds(session_factory, user)
    assert any("✅ Sent (edited)" in (e.text or "") for e in harness.api.of_type(EditMessageText))
    assert "в пятницу" not in log_capture.raw


async def test_several_values_one_per_line_and_count_mismatch(
    harness: US1Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    fake_llm.set_response(
        "compose.draft",
        _needs_info(TWO, ("[[время]]", "What time?"), ("[[день]]", "Which day?")),
    )
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id
    (card,) = harness.api.of_type(SendMessage)
    assert "1. What time?\n2. Which day?\n↩️ Reply with the missing values (one per line)" in (
        card.text
    )
    harness.api.clear()

    # A plain message (not a reply) while this is the newest pending card; one value short.
    await harness.feed(text_update(control, "15:00"))
    (error,) = harness.api.sent_texts
    assert "needs 2 values, one per line" in error and "1. What time?\n2. Which day?" in error
    assert "You sent 1. Nothing was changed" in error
    (unchanged,) = await harness.drafts(user)
    assert unchanged.state == DraftState.pending and unchanged.text == TWO

    harness.api.clear()
    await harness.feed(text_update(control, "15:00\n\nв пятницу\n"))
    old, new = await harness.drafts(user)
    assert old.state == DraftState.superseded
    assert new.text == "Отчёт будет в пятницу к 15:00, в пятницу точно."
    assert gateway.send_text_calls == []

    # The filled card is not a needs-info card: further chatting fills nothing.
    harness.api.clear()
    await harness.feed(text_update(control, "Just chatting with the bot"))
    assert len(await harness.drafts(user)) == 2 and harness.api.requests == []


async def test_plain_text_without_a_needs_info_card_is_ignored(
    harness: US1Harness,
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    fake_llm.set_response("compose.draft", grounded_reply(PLAIN))
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id
    harness.api.clear()

    await harness.feed(text_update(control, "пятница"))
    assert harness.api.requests == []

    # A reply to that card explains instead of guessing.
    await harness.feed(text_update(control, "пятница", reply_to=draft.control_message_id))
    assert harness.api.sent_texts == [
        "This draft has no missing values to fill in. Tap ✏️ Edit to rewrite it."
    ]
    assert len(await harness.drafts(user)) == 1


async def test_edit_still_works_and_commands_are_never_values(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id

    await harness.feed(text_update(control, "/nonsense", reply_to=draft.control_message_id))
    assert len(await harness.drafts(user)) == 1

    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    await harness.feed(text_update(control, "Отчёт будет в понедельник."))
    (sent,) = gateway.send_text_calls
    assert sent.text == "Отчёт будет в понедельник."
    (stored,) = await harness.drafts(user)
    assert stored.state == DraftState.sent


async def test_sensitive_confirmation_stays_after_filling(
    harness: US1Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    fake_llm.set_response(
        "compose.draft",
        _needs_info(
            ONE,
            ("[[день]]", "Which day?"),
            flags={
                "creates_commitment": {"value": False, "what": None, "due": None},
                "sensitive": {"value": True, "categories": ["legal"]},
            },
        ),
    )
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id

    # Before filling, Send asks about both.
    await harness.feed(callback_update(control, f"approve:{draft.id}"))
    assert "unresolved placeholders and sensitive content" in harness.api.sent_texts[-1]

    await harness.feed(text_update(control, "в пятницу", reply_to=draft.control_message_id))
    _, new = await harness.drafts(user)
    harness.api.clear()
    await harness.feed(callback_update(control, f"approve:{new.id}"))
    assert gateway.send_text_calls == []
    assert harness.api.sent_texts == ["⚠️ This draft has sensitive content. Send anyway?"]

    # The old card's buttons are dead.
    await harness.feed(text_update(control, "в субботу", reply_to=draft.control_message_id))
    assert harness.api.sent_texts[-1] == "This draft has expired"
