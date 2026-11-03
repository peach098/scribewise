"""Intake routing decisions (T025): enabled-chat filter, group rule, grouping window, media-only.

The DB-backed part of ``IntakeRouter`` is covered in tests/integration; here the pure decision
function is exercised without Postgres.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scribe.db.models import ChatKind
from scribe.intake.router import (
    Action,
    ChatRule,
    IntakeDecision,
    IntakeReason,
    OpenTurn,
    address_reason,
    decide,
    is_media_only,
)
from scribe.telegram.gateway import IncomingMessage

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
WINDOW = 60
PRIVATE = ChatRule(enabled=True, kind=ChatKind.private)
GROUP = ChatRule(enabled=True, kind=ChatKind.group)


def _msg(**values: object) -> IncomingMessage:
    base = IncomingMessage(
        user_id=uuid.UUID(int=1),
        chat_id=555,
        message_id=10,
        sender_peer_id=777,
        sender_display_name="Synthetic Colleague",
        text="Synthetic question?",
        has_media=False,
        sent_at=T0,
    )
    return replace(base, **values)  # type: ignore[arg-type]


def _open(seconds_ago: float) -> OpenTurn:
    return OpenTurn(turn_id=uuid.UUID(int=99), last_message_at=T0 - timedelta(seconds=seconds_ago))


def test_unknown_or_disabled_chat_is_ignored() -> None:
    assert decide(_msg(), None, None, grouping_window_s=WINDOW).action == Action.ignore
    disabled = replace(PRIVATE, enabled=False)
    assert decide(_msg(), disabled, None, grouping_window_s=WINDOW).action == Action.ignore


def test_outgoing_messages_are_not_intake() -> None:
    assert decide(_msg(is_outgoing=True), PRIVATE, None, grouping_window_s=WINDOW).action == (
        Action.ignore
    )


def test_private_chat_message_creates_turn() -> None:
    decision = decide(_msg(), PRIVATE, None, grouping_window_s=WINDOW)
    assert decision.action == Action.new_turn
    assert decision.turn_id is None


@pytest.mark.parametrize(
    ("chat", "values", "expected"),
    [
        (PRIVATE, {}, IntakeReason.private),
        (GROUP, {"is_reply_to_me": True}, IntakeReason.reply),
        (GROUP, {"mentions_me": True}, IntakeReason.tagged),
        (GROUP, {"mentions_me": True, "is_reply_to_me": True}, IntakeReason.reply),
        (GROUP, {}, None),
    ],
)
def test_address_reason(
    chat: ChatRule, values: dict[str, object], expected: IntakeReason | None
) -> None:
    kind = "private" if chat.kind == ChatKind.private else "group"
    assert address_reason(_msg(chat_kind=kind, **values), chat) == expected


@pytest.mark.parametrize(
    ("chat", "values", "open_turn", "action", "reason"),
    [
        (GROUP, {}, None, Action.store_only, IntakeReason.not_addressed),
        (GROUP, {"mentions_me": True}, None, Action.new_turn, IntakeReason.tagged),
        (GROUP, {"is_reply_to_me": True}, None, Action.new_turn, IntakeReason.reply),
        (GROUP, {}, 10, Action.attach, IntakeReason.follow_up),
        (GROUP, {"mentions_me": True}, 10, Action.attach, IntakeReason.tagged),
        (
            GROUP,
            {"text": "", "has_media": True},
            None,
            Action.store_only,
            IntakeReason.not_addressed,
        ),
        (
            GROUP,
            {"text": "", "has_media": True, "mentions_me": True},
            None,
            Action.handle_manually,
            IntakeReason.tagged,
        ),
        (PRIVATE, {}, None, Action.new_turn, IntakeReason.private),
    ],
)
def test_decide_carries_the_reason(
    chat: ChatRule,
    values: dict[str, object],
    open_turn: float | None,
    action: Action,
    reason: IntakeReason,
) -> None:
    kind = "private" if chat.kind == ChatKind.private else "group"
    turn = _open(seconds_ago=open_turn) if open_turn is not None else None
    decision = decide(_msg(chat_kind=kind, **values), chat, turn, grouping_window_s=WINDOW)
    assert (decision.action, decision.reason) == (action, reason)


def test_no_chat_rule_field_can_make_an_unaddressed_group_message_a_turn() -> None:
    assert set(ChatRule.__dataclass_fields__) == {"enabled", "kind"}
    decision = decide(_msg(chat_kind="group"), GROUP, None, grouping_window_s=WINDOW)
    assert decision.action == Action.store_only


def test_grouping_window_merges_consecutive_messages() -> None:
    inside = decide(_msg(), PRIVATE, _open(seconds_ago=59), grouping_window_s=WINDOW)
    assert inside.action == Action.attach
    assert inside.turn_id == uuid.UUID(int=99)

    edge = decide(_msg(), PRIVATE, _open(seconds_ago=60), grouping_window_s=WINDOW)
    assert edge.action == Action.attach

    outside = decide(_msg(), PRIVATE, _open(seconds_ago=61), grouping_window_s=WINDOW)
    assert outside.action == Action.new_turn


def test_group_follow_up_within_window_joins_the_addressed_turn() -> None:
    # The first message mentioned the user; the sender's follow-up 10 s later does not.
    follow_up = _msg(chat_kind="group")
    decision = decide(follow_up, GROUP, _open(seconds_ago=10), grouping_window_s=WINDOW)
    assert decision.action == Action.attach


def test_media_only_goes_to_handle_manually() -> None:
    voice = _msg(text="", has_media=True)
    assert is_media_only(voice)
    assert decide(voice, PRIVATE, None, grouping_window_s=WINDOW).action == Action.handle_manually
    # Even right after a text message from the same sender: media is never guessed at.
    assert (
        decide(voice, PRIVATE, _open(seconds_ago=5), grouping_window_s=WINDOW).action
        == Action.handle_manually
    )
    # Not addressed in a group: stored for context only, no notification.
    group_voice = _msg(text="", has_media=True, chat_kind="group")
    assert decide(group_voice, GROUP, None, grouping_window_s=WINDOW).action == Action.store_only


def test_media_with_caption_is_text() -> None:
    photo = _msg(text="Synthetic caption with a question?", has_media=True)
    assert not is_media_only(photo)
    assert decide(photo, PRIVATE, None, grouping_window_s=WINDOW).action == Action.new_turn
    whitespace = _msg(text="   ", has_media=True)
    assert is_media_only(whitespace)


# --- forwarded messages (#47) ---------------------------------------------------------------------


def test_a_forward_is_never_addressed() -> None:
    forward = _msg(is_forwarded=True, forwarded_from="Synthetic Origin")
    assert address_reason(forward, PRIVATE) is None
    tagged_reply = replace(forward, chat_kind="group", mentions_me=True, is_reply_to_me=True)
    assert address_reason(tagged_reply, GROUP) is None


def test_a_forward_alone_in_a_private_chat_opens_a_forwards_only_turn() -> None:
    # The turn is never drafted by itself: the reply gate skips it as ``forwarded`` when due,
    # unless the contact's own message joins it first.
    for values in ({}, {"text": "", "has_media": True}, {"mentions_me": True}):
        forward = _msg(is_forwarded=True, **values)
        decision = decide(forward, PRIVATE, None, grouping_window_s=WINDOW)
        assert (decision.action, decision.reason) == (Action.new_turn, IntakeReason.forwarded)
        late = decide(forward, PRIVATE, _open(seconds_ago=61), grouping_window_s=WINDOW)
        assert (late.action, late.reason) == (Action.new_turn, IntakeReason.forwarded)


def test_a_forward_alone_in_a_group_is_stored_as_context_only() -> None:
    for values in ({}, {"text": "", "has_media": True}, {"mentions_me": True}):
        forward = _msg(is_forwarded=True, chat_kind="group", is_reply_to_me=True, **values)
        decision = decide(forward, GROUP, None, grouping_window_s=WINDOW)
        assert (decision.action, decision.reason) == (Action.store_only, IntakeReason.forwarded)
        late = decide(forward, GROUP, _open(seconds_ago=61), grouping_window_s=WINDOW)
        assert late.action == Action.store_only


def test_the_contacts_own_message_joins_a_turn_a_forward_opened() -> None:
    comment = _msg()
    decision = decide(comment, PRIVATE, _open(seconds_ago=5), grouping_window_s=WINDOW)
    assert decision == IntakeDecision(Action.attach, uuid.UUID(int=99), IntakeReason.private)


@pytest.mark.parametrize("chat", [PRIVATE, GROUP])
def test_a_forward_within_the_window_joins_the_open_turn_as_context(chat: ChatRule) -> None:
    kind = "private" if chat.kind == ChatKind.private else "group"
    for values in ({}, {"text": "", "has_media": True}):  # a forwarded photo is context too
        forward = _msg(is_forwarded=True, chat_kind=kind, **values)
        decision = decide(forward, chat, _open(seconds_ago=5), grouping_window_s=WINDOW)
        assert decision == IntakeDecision(Action.attach, uuid.UUID(int=99), IntakeReason.follow_up)


def test_forwarded_outgoing_messages_are_still_not_intake() -> None:
    forward = _msg(is_forwarded=True, is_outgoing=True)
    assert decide(forward, PRIVATE, None, grouping_window_s=WINDOW).action == Action.ignore
