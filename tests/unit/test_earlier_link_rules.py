"""Link rules for group matches (specs/004 research S5, FR-S05b) and the stored excerpt (T014)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from scribe.context.earlier import excerpt, link_reason
from scribe.db.models import ChatKind
from scribe.telegram.gateway import FoundMessage
from tests.integration.earlier_support import (
    SENDER_PEER_ID,
    THIRD_PEER_ID,
    USER_PEER_ID,
    group_found,
    make_scope,
    private_found,
)

SCOPE = replace(make_scope(), sender_username="anna_test", user_username="Test_User")


def third(text: str = "synthetic budget note", **values: Any) -> FoundMessage:
    return group_found(text, sender_peer_id=THIRD_PEER_ID, sender_display_name="Ivan", **values)


def test_private_chat_match_is_always_linked() -> None:
    message = private_found("budget agreed", sender_peer_id=THIRD_PEER_ID)
    assert link_reason(message, SCOPE, ChatKind.private, {}, {}) == "private"


def test_group_match_by_the_user_is_own() -> None:
    assert link_reason(group_found("budget", is_outgoing=True), SCOPE, ChatKind.group, {}, {}) == (
        "own"
    )
    # The user's own message seen from another device (not flagged outgoing) is also "own".
    by_peer = group_found("budget", sender_peer_id=USER_PEER_ID)
    assert link_reason(by_peer, SCOPE, ChatKind.group, {}, {}) == "own"


def test_group_match_by_the_sender() -> None:
    message = group_found("budget", sender_peer_id=SENDER_PEER_ID)
    assert link_reason(message, SCOPE, ChatKind.group, {}, {}) == "sender"


@pytest.mark.parametrize(
    "target_values",
    [{"sender_peer_id": SENDER_PEER_ID}, {"is_outgoing": True}],
    ids=["to-sender", "to-user"],
)
def test_reply_to_a_sender_or_user_message(target_values: dict[str, Any]) -> None:
    target = group_found("when is the budget due?", **target_values)
    message = third(reply_to_id=target.message_id)
    targets = {target.message_id: target}
    assert link_reason(message, SCOPE, ChatKind.group, targets, {}) == "reply"


def test_reply_to_a_third_party_message_is_not_linked() -> None:
    target = third("unrelated question")
    message = third(reply_to_id=target.message_id)
    assert link_reason(message, SCOPE, ChatKind.group, {target.message_id: target}, {}) is None


def test_reply_target_missing_is_not_linked() -> None:
    message = third(reply_to_id=12345)
    assert link_reason(message, SCOPE, ChatKind.group, {}, {}) is None


@pytest.mark.parametrize(
    "replier_values",
    [{"sender_peer_id": SENDER_PEER_ID}, {"is_outgoing": True}],
    ids=["by-sender", "by-user"],
)
def test_replied_by_the_sender_or_the_user(replier_values: dict[str, Any]) -> None:
    message = third("the budget is approved")
    replier = group_found("thanks!", reply_to_id=message.message_id, **replier_values)
    replied_by = {message.message_id: replier}
    assert link_reason(message, SCOPE, ChatKind.group, {}, replied_by) == "replied_by"


def test_replied_by_a_third_party_is_not_linked() -> None:
    message = third("the budget is approved")
    replier = third("ok", reply_to_id=message.message_id)
    assert link_reason(message, SCOPE, ChatKind.group, {}, {message.message_id: replier}) is None


@pytest.mark.parametrize("peer", [SENDER_PEER_ID, USER_PEER_ID])
def test_mention_by_peer_id(peer: int) -> None:
    message = third("please check the budget", mentioned_peer_ids={peer})
    assert link_reason(message, SCOPE, ChatKind.group, {}, {}) == "mention"


@pytest.mark.parametrize("username", ["anna_test", "ANNA_TEST", "test_user", "@Test_User"])
def test_mention_by_username_is_case_insensitive(username: str) -> None:
    message = third("please check the budget", mentioned_usernames={username})
    assert link_reason(message, SCOPE, ChatKind.group, {}, {}) == "mention"


def test_mention_of_unknown_username_is_not_linked() -> None:
    scope = make_scope()  # usernames unknown
    message = third("please check the budget", mentioned_usernames={"anna_test"})
    assert link_reason(message, scope, ChatKind.group, {}, {}) is None
    other = third("please check the budget", mentioned_peer_ids={THIRD_PEER_ID})
    assert link_reason(other, SCOPE, ChatKind.group, {}, {}) is None


def test_same_thread_as_the_incoming_message() -> None:
    scope = replace(SCOPE, incoming_thread_top_id=777)
    in_thread = third(thread_top_id=777, reply_to_id=778)
    direct_reply_to_top = third(reply_to_id=777)
    top = third(message_id=777)
    for message in (in_thread, direct_reply_to_top, top):
        assert link_reason(message, scope, ChatKind.group, {}, {}) == "thread"
    assert link_reason(third(thread_top_id=900), scope, ChatKind.group, {}, {}) is None


def test_unrelated_third_party_group_message_is_dropped() -> None:
    assert link_reason(third(), SCOPE, ChatKind.group, {}, {}) is None
    assert link_reason(third(), SCOPE, ChatKind.channel_comments, {}, {}) is None


def test_no_peers_known_links_only_outgoing() -> None:
    scope = make_scope(sender_peer_id=None, user_peer_id=None)
    assert link_reason(third(), scope, ChatKind.group, {}, {}) is None
    assert link_reason(group_found("x", is_outgoing=True), scope, ChatKind.group, {}, {}) == "own"


# --- excerpt --------------------------------------------------------------------------------------


def test_short_text_unchanged() -> None:
    assert excerpt("budget is approved", ["budget"]) == "budget is approved"
    text = "x" * 200
    assert excerpt(text, ["nothing"]) == text


def test_long_text_cut_around_the_first_hit() -> None:
    text = "a" * 300 + " budget approved " + "b" * 300
    cut = excerpt(text, ["missing", "Budget"])
    assert len(cut) == 200
    assert "budget approved" in cut
    assert cut in text


def test_earliest_hit_of_several_queries_wins() -> None:
    text = "c" * 250 + " deadline " + "d" * 250 + " invoice " + "e" * 250
    cut = excerpt(text, ["invoice", "deadline"])
    assert "deadline" in cut and "invoice" not in cut


def test_no_hit_takes_the_start() -> None:
    text = "start " + "z" * 400
    assert excerpt(text, ["absent"]) == text[:200]


def test_hit_near_the_end_keeps_full_length() -> None:
    text = "y" * 400 + " budget"
    cut = excerpt(text, ["budget"])
    assert len(cut) == 200 and cut.endswith("budget")


def test_custom_limit() -> None:
    text = "q" * 100 + "budget" + "q" * 100
    cut = excerpt(text, ["budget"], limit=50)
    assert len(cut) == 50 and "budget" in cut
