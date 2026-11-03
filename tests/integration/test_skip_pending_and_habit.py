"""004 on top of 001 US2–US4: a closing message while a draft is pending (FR-011, T031–T033),
the habit rule fed by real manual replies (research R3), the "Skip thanks/bye" row in /chats
(T034), skipped / "Draft anyway" in /history (T035) and the Russian /skipped.

US2 harness: intake (with the reply gate), manual-reply handling, the outdated card. Synthetic
data only.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    Contact,
    ConversationTurn,
    DroppedReason,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import ProviderUnavailable, ReplyNeededOutput, RouteName
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, reply_needed_output, select_all_history
from tests.integration.us2_support import US2Harness, button_texts, callback_datas

REPLY = "Synthetic reply."
ROUTE: RouteName = "classify.reply_needed"
QUESTION = "When is the synthetic report due?"


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
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact, **user_values: Any
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active, **user_values)
    contact = await make_contact(user, display_name="Synthetic Colleague")
    chat = await make_chat(
        user, title="Synthetic Colleague", telegram_chat_id=contact.telegram_peer_id
    )
    return user, chat, contact


def _closing(resolves: bool = False, category: str = "thanks") -> ReplyNeededOutput:
    return reply_needed_output(False, category, 0.95, resolves_pending=resolves)


def _pending_only(output: ReplyNeededOutput) -> Any:
    """The classifier answer for the pending-draft check; turns still need a reply."""

    def respond(call: FakeCall) -> ReplyNeededOutput:
        pending = next(b for b in call.user_blocks if b.name == "pending_draft")
        return output if pending.content.strip() == "true" else reply_needed_output(True)

    return respond


async def _pending_draft(harness: US2Harness, user: User, chat: WorkChat, contact: Contact) -> None:
    await harness.incoming(user, chat, contact, QUESTION)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert not draft.outdated
    harness.api.clear()


# --- T031: closing message while a draft is pending (FR-011) ------------------------------------


async def test_closing_while_pending_does_not_mark_outdated(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await _pending_draft(harness, user, chat, contact)
    harness.set_reply_needed(_pending_only(_closing()))

    await harness.incoming(user, chat, contact, "thanks!")

    (draft,) = await harness.drafts(user)
    assert not draft.outdated and not draft.possibly_unnecessary
    (call,) = harness.fake_llm.calls_for(ROUTE)[-1:]
    assert any(b.name == "pending_draft" and b.content.strip() == "true" for b in call.user_blocks)
    (edit,) = harness.edits()  # one re-render, after the gate: no banner, one-tap Send kept
    assert edit.text is not None and not edit.text.startswith("⚠️ New messages arrived")
    assert "💤" not in edit.text
    assert f"approve:{draft.id}" in callback_datas(edit.reply_markup)
    (turn,) = await harness.turns(user)  # the message joined the pending turn
    assert turn.state == TurnState.pending_review


async def test_resolving_message_flags_possibly_unnecessary(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await _pending_draft(harness, user, chat, contact)
    harness.set_reply_needed(_pending_only(_closing(resolves=True)))

    await harness.incoming(user, chat, contact, "never mind, solved it, thanks")

    (draft,) = await harness.drafts(user)
    assert draft.possibly_unnecessary and not draft.outdated
    (edit,) = harness.edits()
    assert edit.text is not None
    assert edit.text.split("\n")[0] == "💤 Synthetic Colleague: this may no longer be needed"
    assert button_texts(edit.reply_markup)[:2] == ["✅ Send", "✏️ Edit"]  # all buttons kept
    datas = callback_datas(edit.reply_markup)
    for action in ("approve", "edit", "regen", "reject"):
        assert f"{action}:{draft.id}" in datas

    # The user may still send it: the normal approval path.
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    (sent,) = gateway.send_text_calls
    assert sent.text == REPLY


async def test_question_while_pending_marks_outdated(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await _pending_draft(harness, user, chat, contact)
    harness.set_reply_needed(_closing())  # would skip, but a question is never asked about
    calls = len(harness.fake_llm.calls_for(ROUTE))

    await harness.incoming(user, chat, contact, "thanks! and the deadline?")

    (draft,) = await harness.drafts(user)
    assert draft.outdated and not draft.possibly_unnecessary
    assert len(harness.fake_llm.calls_for(ROUTE)) == calls  # pre-gate: no classifier call
    (edit,) = harness.edits()
    assert edit.text is not None and edit.text.startswith("⚠️ New messages arrived")


async def test_pending_gate_failure_marks_outdated(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await _pending_draft(harness, user, chat, contact)
    harness.set_reply_needed(ProviderUnavailable("synthetic outage"))

    await harness.incoming(user, chat, contact, "ok")

    (draft,) = await harness.drafts(user)
    assert draft.outdated and not draft.possibly_unnecessary
    (edit,) = harness.edits()
    assert edit.text is not None and edit.text.startswith("⚠️ New messages arrived")


async def test_closing_after_question_keeps_outdated(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    """A draft already outdated by an earlier message stays outdated; a resolving closing
    still adds the 💤 banner on top."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await _pending_draft(harness, user, chat, contact)
    await harness.incoming(user, chat, contact, "Also, is it in English?")
    harness.set_reply_needed(_pending_only(_closing(resolves=True)))

    await harness.incoming(user, chat, contact, "never mind, thanks")

    (draft,) = await harness.drafts(user)
    assert draft.outdated and draft.possibly_unnecessary
    edit = harness.edits()[-1]
    assert edit.text is not None and edit.text.startswith("⚠️ New messages arrived")
    assert "💤 Synthetic Colleague: this may no longer be needed" in edit.text


async def test_closing_while_pending_with_skipping_off_marks_outdated(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact, skip_closing=False)
    await _pending_draft(harness, user, chat, contact)
    harness.set_reply_needed(_pending_only(_closing()))
    calls = len(harness.fake_llm.calls_for(ROUTE))

    await harness.incoming(user, chat, contact, "thanks!")

    (draft,) = await harness.drafts(user)
    assert draft.outdated
    assert len(harness.fake_llm.calls_for(ROUTE)) == calls  # disabled: no classifier call


# --- habit rule with real manual replies (research R3, main's outgoing messages) ---------------


async def test_habit_counts_manual_replies_after_a_skip(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """The user answers three skipped "thanks" by hand within minutes (stored outgoing
    messages): the fourth is drafted because of the habit."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    harness.set_reply_needed(_closing())
    for _ in range(3):
        await harness.incoming(user, chat, contact, "thanks!")
        await harness.tick()
        assert (await harness.turns(user))[-1].state == TurnState.skipped
        clock.advance(minutes=5)
        await harness.manual_reply(user, chat, "you're welcome")
        clock.advance(hours=2)

    await harness.incoming(user, chat, contact, "thanks!")
    await harness.tick()
    turn = (await harness.turns(user))[-1]
    assert (turn.state, turn.decision_source) == (TurnState.pending_review, "habit")
    (draft,) = await harness.drafts(user)
    assert "drafted_by_habit" in draft.output_json["system_warnings"]


async def test_habit_counts_closings_answered_before_reading(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """A manual reply that drops a closing turn before the gate saw it: the dropped turn is
    classified afterwards and its category counts for the habit rule."""
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=600, delay_max_s=600
    )
    harness.set_reply_needed(_closing())
    for _ in range(3):
        await harness.incoming(user, chat, contact, "thanks!")
        clock.advance(minutes=2)
        await harness.manual_reply(user, chat, "anytime")
        turn = (await harness.turns(user))[-1]
        assert (turn.state, turn.dropped_reason) == (TurnState.dropped, DroppedReason.manual_reply)
        assert turn.skip_category == "thanks" and turn.reply_decision is None
        clock.advance(hours=2)

    await harness.incoming(user, chat, contact, "thanks!")
    clock.advance(minutes=10)
    await harness.tick()
    turn = (await harness.turns(user))[-1]
    assert (turn.state, turn.decision_source) == (TurnState.pending_review, "habit")


async def test_dropped_question_is_not_classified(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(
        make_user, make_chat, make_contact, delay_min_s=600, delay_max_s=600
    )
    harness.set_reply_needed(_closing())
    await harness.incoming(user, chat, contact, QUESTION)
    clock.advance(minutes=2)
    await harness.manual_reply(user, chat, "Friday")
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.dropped and turn.skip_category is None
    assert harness.fake_llm.calls_for(ROUTE) == []  # pre-gate: not a candidate


# --- T034: "Skip thanks/bye" in /chats ----------------------------------------------------------


async def test_chats_screen_sets_chat_skip_value(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.feed(callback_update(user.telegram_control_chat_id, f"chatcfg:{chat.id}"))
    (screen,) = harness.messages()
    assert "💤 Skip thanks/bye: Global (ON)" in screen.text.split("\n")
    datas = callback_datas(screen.reply_markup)
    assert [d for d in datas if d.startswith("chatskip:")] == [
        f"chatskip:{chat.id}:inherit",
        f"chatskip:{chat.id}:off",
        f"chatskip:{chat.id}:on",
    ]
    assert "✓ Global (ON)" in button_texts(screen.reply_markup)
    harness.api.clear()

    await harness.feed(callback_update(user.telegram_control_chat_id, f"chatskip:{chat.id}:off"))
    async with harness.session_factory() as session:
        stored = await session.get(WorkChat, chat.id)
        assert stored is not None and stored.skip_closing is False
    (screen,) = harness.messages()
    assert "💤 Skip thanks/bye: Always draft" in screen.text.split("\n")
    assert "✓ Always draft" in button_texts(screen.reply_markup)

    # The gate reads it for the next message: drafted, classifier not asked.
    harness.set_reply_needed(_closing())
    await harness.incoming(user, chat, contact, "thanks!")
    await harness.tick()
    turn = (await harness.turns(user))[-1]
    assert (turn.state, turn.decision_source) == (TurnState.pending_review, "disabled")


async def test_chatskip_foreign_chat_refused(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, _, _ = await _setup(make_user, make_chat, make_contact)
    _, other_chat, _ = await _setup(make_user, make_chat, make_contact)
    await harness.feed(
        callback_update(user.telegram_control_chat_id, f"chatskip:{other_chat.id}:on")
    )
    answers = [r.text for r in harness.api.of_type(AnswerCallbackQuery)]
    assert answers == ["Chat not found"]
    async with harness.session_factory() as session:
        stored = await session.get(WorkChat, other_chat.id)
        assert stored is not None and stored.skip_closing is None


# --- T035: /history -----------------------------------------------------------------------------


async def test_history_shows_skipped_and_draft_anyway_without_content(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    harness.set_reply_needed(_closing(category="farewell"))
    await harness.incoming(user, chat, contact, "bye, have a synthetic weekend")
    await harness.tick()
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.skipped
    await harness.feed(callback_update(user.telegram_control_chat_id, f"draftany:{turn.id}"))
    harness.api.clear()

    await harness.feed(text_update(user.telegram_control_chat_id, "/history"))
    (history,) = harness.messages()
    lines = history.text.split("\n")
    assert any(line.endswith("· 💤 Skipped — goodbye · Synthetic Colleague") for line in lines)
    assert any(line.endswith("· ✍️ Drafted on request · Synthetic Colleague") for line in lines)
    assert "weekend" not in history.text  # never message content


# --- Russian UI ---------------------------------------------------------------------------------


async def test_skipped_list_in_russian(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact, language="ru")
    harness.set_reply_needed(_closing())
    await harness.incoming(user, chat, contact, "спасибо!")
    await harness.tick()
    harness.api.clear()

    await harness.feed(text_update(user.telegram_control_chat_id, "/skipped"))
    (listing,) = harness.messages()
    lines = listing.text.split("\n")
    assert lines[0] == "💤 Пропущено за последние 7 дней — 1   ·   Пропуск «спасибо/ок/пока»: ВКЛ"
    assert "   “спасибо!”" in lines and "   Причина: благодарность" in lines
    labels = button_texts(listing.reply_markup)
    assert "Выключить пропуск" in labels and "1 · ✍️ Всё равно черновик" in labels

    await harness.feed(
        callback_update(
            user.telegram_control_chat_id,
            "skipcfg:global:off",
            message_text=listing.text,
        )
    )
    answers = [r.text for r in harness.api.of_type(AnswerCallbackQuery)]
    assert answers[-1] == "Пропуск «спасибо/ок/пока»: ВЫКЛ"
    edit = harness.api.of_type(EditMessageText)[-1]
    assert edit.text is not None and edit.text.split("\n")[0].endswith(
        "Пропуск «спасибо/ок/пока»: ВЫКЛ"
    )

    await harness.feed(text_update(user.telegram_control_chat_id, "/help"))
    help_text = harness.api.of_type(SendMessage)[-1].text
    assert "/skipped — Сообщения без черновика (спасибо, пока…)" in help_text.split("\n")


async def test_skipped_command_in_help_and_menu(harness: US2Harness, make_user: MakeUser) -> None:
    user = await make_user(state=UserState.active)
    await harness.feed(text_update(user.telegram_control_chat_id, "/help"))
    help_text = harness.api.of_type(SendMessage)[-1].text
    lines = help_text.split("\n")
    assert "/skipped — Messages I didn't draft for (thanks, bye…)" in lines
    assert lines.index("/skipped — Messages I didn't draft for (thanks, bye…)") == (
        next(i for i, line in enumerate(lines) if line.startswith("/waiting")) + 1
    )


async def test_skipped_turns_are_listed_newest_first(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    harness.set_reply_needed(_closing())
    for text in ("thanks!", "ok, noted"):
        await harness.incoming(user, chat, contact, text)
        await harness.tick()
        clock.advance(hours=1)
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ConversationTurn.state).where(ConversationTurn.user_id == user.id)
        )
        assert set(result.scalars().all()) == {TurnState.skipped}
    harness.api.clear()
    await harness.feed(text_update(user.telegram_control_chat_id, "/skipped"))
    (listing,) = harness.messages()
    lines = listing.text.split("\n")
    first = next(i for i, line in enumerate(lines) if line.startswith("1. "))
    assert lines[first + 1] == "   “ok, noted”"
