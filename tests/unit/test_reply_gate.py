"""Reply gate core (004 T012, 013 T020/T029, contracts/reply-gate.md): the pre-gate and the order
of ``decide()``. Database-free; the classifier is the fake adapter. Synthetic texts only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from scribe.db.models import ConversationTurn, Message
from scribe.drafting.reply_gate import (
    GateDecision,
    decide,
    forwards_only_skip,
    is_forwards_only,
    is_skip_candidate,
    no_habit,
)
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import (
    ContactInfo,
    MessageItem,
    ProviderUnavailable,
    ReplyNeededOutput,
    ReplyNeededRequest,
    RouteName,
)
from scribe.llm.registry import LLMRouter

ROUTE: RouteName = "classify.reply_needed"


# --- pre-gate -----------------------------------------------------------------------------------


MAX_CHARS = 2000


@pytest.mark.parametrize(
    ("texts", "has_rules", "expected"),
    [
        (["thanks"], False, True),
        (["👍"], False, True),
        (["спасибо", "до завтра"], False, True),
        ([" ".join(["word"] * 16)], False, True),  # no word limit any more
        (["ok?"], False, False),
        (["ок？"], False, False),  # full-width question mark
        (["ok?"], True, True),  # a question is a candidate when the user has rules
        (["ок？"], True, True),
        (["a" * 1500], False, True),  # a long FYI
        (["a" * 2000], False, True),
        (["a" * 2001], False, False),
        (["a" * 2001], True, False),  # the length limit holds with rules too
        ([], False, False),
        (["   "], False, False),
        ([], True, False),
    ],
)
def test_is_skip_candidate(texts: list[str], has_rules: bool, expected: bool) -> None:
    assert is_skip_candidate(texts, has_rules=has_rules, max_chars=MAX_CHARS) is expected


# --- decide() -----------------------------------------------------------------------------------


RULES = {"rules": [{"text": "do not answer announcements", "source": "user"}]}


def _req(*texts: str, rules: dict[str, Any] | None = None, media: bool = False) -> Any:
    return ReplyNeededRequest(
        user_rules=rules or {},
        incoming_messages=[
            MessageItem(id=f"msg:{i}", sender="Synthetic Colleague", text=t, has_media=media)
            for i, t in enumerate(texts)
        ],
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
    )


def _output(
    reply_needed: bool,
    category: str = "none",
    confidence: float = 0.9,
    decided_by: str = "default",
) -> Any:
    return ReplyNeededOutput.model_validate(
        {
            "reply_needed": reply_needed,
            "category": category,
            "confidence": confidence,
            "resolves_pending": False,
            "decided_by": decided_by,
        }
    )


class HabitCounter:
    def __init__(self, value: int) -> None:
        self.value = value
        self.calls = 0

    async def __call__(self) -> int:
        self.calls += 1
        return self.value


async def _decide(router: LLMRouter, req: ReplyNeededRequest, **values: Any) -> GateDecision:
    options: dict[str, Any] = {
        "enabled": True,
        "draft_anyway": False,
        "habit_replied": no_habit,
        "min_confidence": 0.8,
        "max_chars": MAX_CHARS,
    }
    options.update(values)
    return await decide(router, req, **options)


def _pair(decision: GateDecision) -> tuple[str, str]:
    return decision.decision, decision.source


async def test_draft_anyway_skips_classifier(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    decision = await _decide(llm_router, _req("thanks"), draft_anyway=True)
    assert _pair(decision) == ("draft", "user")
    assert fake_llm.calls_for(ROUTE) == []


async def test_disabled_skips_classifier(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    decision = await _decide(llm_router, _req("thanks"), enabled=False)
    assert _pair(decision) == ("draft", "disabled")
    assert fake_llm.calls_for(ROUTE) == []


async def test_not_a_candidate_skips_classifier(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    decision = await _decide(llm_router, _req("thanks! when is the next call?"))
    assert _pair(decision) == ("draft", "pregate")
    assert (decision.category, decision.confidence, decision.routed) == (None, None, None)
    too_long = await _decide(llm_router, _req("a" * 2001))
    assert _pair(too_long) == ("draft", "pregate")
    assert fake_llm.calls_for(ROUTE) == []


async def test_long_fyi_and_media_reach_the_classifier(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    """013: a 1500-char FYI and a turn with media are candidates; the classifier is told
    about the media and the skip is the informational one."""
    fake_llm.set_response(ROUTE, _output(False, "informational", 0.9))
    decision = await _decide(llm_router, _req("n" * 1500, media=True))
    assert (decision.decision, decision.source, decision.category) == (
        "skip",
        "model",
        "informational",
    )
    (call,) = fake_llm.calls_for(ROUTE)
    (block,) = [b for b in call.user_blocks if b.name == "incoming_messages"]
    assert '"has_media":true' in block.content.replace(" ", "")


async def test_a_question_is_asked_only_with_rules(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(ROUTE, _output(True))
    without = await _decide(llm_router, _req("when is the call?"))
    assert _pair(without) == ("draft", "pregate")
    assert fake_llm.calls_for(ROUTE) == []
    with_rules = await _decide(llm_router, _req("when is the call?", rules=RULES))
    assert _pair(with_rules) == ("draft", "model")
    assert len(fake_llm.calls_for(ROUTE)) == 1


async def test_provider_error_drafts(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    fake_llm.set_response(ROUTE, ProviderUnavailable("synthetic outage"))
    decision = await _decide(llm_router, _req("thanks"))
    assert _pair(decision) == ("draft", "fallback")
    assert decision.category is None and decision.routed is None


async def test_reply_needed_drafts(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    fake_llm.set_response(ROUTE, _output(True))
    habit = HabitCounter(5)
    decision = await _decide(llm_router, _req("hi"), habit_replied=habit)
    assert _pair(decision) == ("draft", "model")
    assert decision.routed is not None and decision.routed.route == ROUTE
    assert habit.calls == 0  # only consulted when the classifier would skip


async def test_low_confidence_drafts_and_keeps_category(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(ROUTE, _output(False, "thanks", 0.79))
    habit = HabitCounter(0)
    decision = await _decide(llm_router, _req("thanks"), habit_replied=habit)
    assert _pair(decision) == ("draft", "fallback")
    assert (decision.category, decision.confidence) == ("thanks", 0.79)
    assert habit.calls == 0


async def test_habit_forces_draft(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    fake_llm.set_response(ROUTE, _output(False, "thanks", 0.9))
    habit = HabitCounter(3)
    decision = await _decide(llm_router, _req("thanks"), habit_replied=habit)
    assert _pair(decision) == ("draft", "habit")
    assert decision.category == "thanks" and habit.calls == 1


# --- informational skips and user rules (013) ---------------------------------------------------


async def test_confident_informational_is_skipped_unless_the_habit_says_otherwise(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(ROUTE, _output(False, "informational", 0.9))
    skipped = await _decide(llm_router, _req("The release moved to Thursday."))
    assert _pair(skipped) == ("skip", "model") and skipped.category == "informational"
    habit = await _decide(
        llm_router, _req("The release moved to Thursday."), habit_replied=HabitCounter(3)
    )
    assert _pair(habit) == ("draft", "habit")
    fake_llm.set_response(ROUTE, _output(False, "informational", 0.7))
    unsure = await _decide(llm_router, _req("The release moved to Thursday."))
    assert _pair(unsure) == ("draft", "fallback")


async def test_a_rule_skip_bypasses_the_habit_rule(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(ROUTE, _output(False, "none", 0.9, "user_rule"))
    habit = HabitCounter(5)
    decision = await _decide(
        llm_router, _req("New policy: all hands on Friday.", rules=RULES), habit_replied=habit
    )
    assert _pair(decision) == ("skip", "rule")
    assert (decision.category, decision.decided_by, decision.has_rules) == (
        "user_rule",
        "user_rule",
        True,
    )
    assert habit.calls == 0  # a rule never consults the habit rule


async def test_a_rule_draft_overrides_an_informational_category(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(ROUTE, _output(True, "informational", 0.9, "user_rule"))
    decision = await _decide(llm_router, _req("FYI: the doc is updated.", rules=RULES))
    assert _pair(decision) == ("draft", "rule")
    assert decision.decided_by == "user_rule"


async def test_a_rule_skip_needs_confidence(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    fake_llm.set_response(ROUTE, _output(False, "none", 0.79, "user_rule"))
    habit = HabitCounter(0)
    decision = await _decide(
        llm_router, _req("New policy: all hands on Friday.", rules=RULES), habit_replied=habit
    )
    assert _pair(decision) == ("draft", "fallback")
    assert habit.calls == 0


async def test_a_rule_the_user_does_not_have_is_not_believed(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    """The model names a rule although the request holds none: the default rules decide."""
    fake_llm.set_response(ROUTE, _output(False, "none", 0.95, "user_rule"))
    decision = await _decide(llm_router, _req("noted"))
    assert _pair(decision) == ("draft", "fallback")
    assert decision.decided_by == "default" and not decision.has_rules


async def test_the_rules_travel_in_a_trusted_block(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(ROUTE, _output(True))
    await _decide(llm_router, _req("hi", rules=RULES))
    (call,) = fake_llm.calls_for(ROUTE)
    trusted = [b.name for b in call.user_blocks if b.trust == "user"]
    assert trusted == ["user_rules"]
    assert all(
        b.render().startswith('<data name="user_rules" trust="user">')
        for b in call.user_blocks
        if b.name == "user_rules"
    )


async def test_rules_do_not_change_the_early_paths(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    asked = await _decide(llm_router, _req("thanks", rules=RULES), draft_anyway=True)
    assert _pair(asked) == ("draft", "user")
    off = await _decide(llm_router, _req("thanks", rules=RULES), enabled=False)
    assert _pair(off) == ("draft", "disabled")
    assert fake_llm.calls_for(ROUTE) == []


async def test_confident_closing_is_skipped(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    fake_llm.set_response(ROUTE, _output(False, "farewell", 0.9))
    habit = HabitCounter(2)
    decision = await _decide(llm_router, _req("bye, have a good weekend"), habit_replied=habit)
    assert _pair(decision) == ("skip", "model")
    assert (decision.category, decision.confidence) == ("farewell", 0.9)
    assert habit.calls == 1
    assert decision.routed is not None


async def test_no_habit_never_forces_a_draft(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    assert await no_habit() == 0
    fake_llm.set_response(ROUTE, _output(False, "acknowledgment", 0.95))
    decision = await _decide(llm_router, _req("👍"), habit_replied=no_habit)
    assert _pair(decision) == ("skip", "model")


async def test_skip_needs_a_closing_category(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    fake_llm.set_response(ROUTE, _output(False, "none", 0.95))
    decision = await _decide(llm_router, _req("noted"))
    assert decision.decision == "draft"
    assert decision.source == "fallback" and decision.category is None


# --- forwarded messages (#47) -------------------------------------------------------------------


def test_forwards_only_skip_is_the_one_rule() -> None:
    """#47: a turn of forwarded messages only is skipped as ``forwarded`` (no classifier),
    with the turn's last message id as the guard; "Draft anyway" or an own message: no."""
    last = uuid.uuid4()
    turn = ConversationTurn(last_message_id=last, draft_anyway_at=None)
    forward, own = Message(is_forwarded=True), Message(is_forwarded=False)
    skip = forwards_only_skip(turn, [forward, forward])
    assert skip is not None
    assert (skip.decision, skip.source, skip.category, skip.confidence) == (
        "skip",
        "pregate",
        "forwarded",
        None,
    )
    assert (skip.last_message_id, skip.routed, skip.failed) == (last, None, False)
    assert forwards_only_skip(turn, [forward, own]) is None
    assert forwards_only_skip(turn, []) is None
    asked = ConversationTurn(last_message_id=last, draft_anyway_at=datetime.now(UTC))
    assert forwards_only_skip(asked, [forward]) is None


def test_is_forwards_only() -> None:
    forward, own = Message(is_forwarded=True), Message(is_forwarded=False)
    assert is_forwards_only([forward])
    assert is_forwards_only([forward, forward])
    assert not is_forwards_only([forward, own])
    assert not is_forwards_only([own])
    assert not is_forwards_only([])


def _mixed(*items: tuple[str, bool]) -> ReplyNeededRequest:
    return ReplyNeededRequest(
        incoming_messages=[
            MessageItem(id=f"msg:{i}", sender="Synthetic Colleague", text=text, forwarded=fwd)
            for i, (text, fwd) in enumerate(items)
        ],
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
    )


async def test_forwarded_text_never_makes_a_turn_a_closing(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    """The pre-gate reads the contact's own words only (#47): a forwarded "Thanks!" next to a
    real request is not a closing, and a long forward does not hide an own "thanks"."""
    request = "can you please go through the attached figures and tell me what looks off?"
    decision = await _decide(llm_router, _mixed(("Thanks!", True), (request, False)))
    assert _pair(decision) == ("draft", "pregate")
    assert fake_llm.calls_for(ROUTE) == []

    fake_llm.set_response(ROUTE, _output(False, "thanks", 0.95))
    long_forward = "Can you look at this release plan and tell me what you think about it?"
    closing = await _decide(llm_router, _mixed((long_forward, True), ("thanks", False)))
    assert (closing.decision, closing.category) == ("skip", "thanks")
    (call,) = fake_llm.calls_for(ROUTE)
    (block,) = [b for b in call.user_blocks if b.name == "incoming_messages"]
    assert '"forwarded":true' in block.content.replace(" ", "")  # the classifier is told


async def test_a_forwarded_photo_does_not_stop_the_classifier(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(ROUTE, _output(True))
    decision = await _decide(llm_router, _mixed(("", True), ("ok", False)))
    assert _pair(decision) == ("draft", "model")  # asked: the contact's own "ok" is a candidate
    assert len(fake_llm.calls_for(ROUTE)) == 1
