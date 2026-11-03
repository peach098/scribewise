"""Reply gate core (004 T012, contracts/reply-gate.md): the pre-gate and the order of
``decide()``. Database-free; the classifier is the fake adapter. Synthetic texts only."""

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


@pytest.mark.parametrize(
    ("texts", "has_media", "expected"),
    [
        (["thanks"], False, True),
        (["👍"], False, True),
        (["спасибо", "до завтра"], False, True),
        ([" ".join(["word"] * 15)], False, True),
        ([" ".join(["word"] * 16)], False, False),
        (["ok?"], False, False),
        (["ок？"], False, False),  # full-width question mark
        (["a" * 161], False, False),
        (["a" * 160], False, True),
        (["thanks"], True, False),
        ([], False, False),
        (["   "], False, False),
    ],
)
def test_is_skip_candidate(texts: list[str], has_media: bool, expected: bool) -> None:
    assert is_skip_candidate(texts, has_media=has_media, max_words=15) is expected


# --- decide() -----------------------------------------------------------------------------------


def _req(*texts: str) -> ReplyNeededRequest:
    return ReplyNeededRequest(
        incoming_messages=[
            MessageItem(id=f"msg:{i}", sender="Synthetic Colleague", text=t)
            for i, t in enumerate(texts)
        ],
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
    )


def _output(reply_needed: bool, category: str = "none", confidence: float = 0.9) -> Any:
    return ReplyNeededOutput.model_validate(
        {
            "reply_needed": reply_needed,
            "category": category,
            "confidence": confidence,
            "resolves_pending": False,
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
        "max_words": 15,
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
    media = await _decide(llm_router, _req("thanks"), has_media=True)
    assert _pair(media) == ("draft", "pregate")
    assert fake_llm.calls_for(ROUTE) == []


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


async def test_forwarded_media_is_not_the_contacts_media(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(ROUTE, _output(True))
    decision = await _decide(llm_router, _mixed(("", True), ("ok", False)), has_media=False)
    assert _pair(decision) == ("draft", "model")  # asked: the photo was forwarded
