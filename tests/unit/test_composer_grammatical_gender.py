"""Issue #46: the user's grammatical gender travels in the drafting request as its own ``user``
block, outside ``profile`` (so the global and every contact profile get the same value)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from scribe.drafting.composer import ComposeInput, compose_output
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import (
    ContactInfo,
    DraftOutput,
    FilterContextOutput,
    MessageItem,
    ProfileContext,
    UserContext,
)
from scribe.llm.registry import LLMRouter


def _draft() -> DraftOutput:
    return DraftOutput.model_validate(
        {
            "text": "Сделала, проверь",
            "language": "ru",
            "needs_info": False,
            "placeholders": [],
            "sources": [{"kind": "message", "id": "msg:in1", "used_for": "the question"}],
            "flags": {
                "creates_commitment": {"value": False, "what": None, "due": None},
                "sensitive": {"value": False, "categories": []},
            },
            "conflicts": [],
            "tone_shift_applied": "none",
            "context_warnings": [],
        }
    )


def _input(**values: Any) -> ComposeInput:
    return ComposeInput(
        incoming=[MessageItem(id="msg:in1", sender="Synthetic Colleague", text="Готово?")],
        history=[],
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
        profile=ProfileContext(rules_json={"formality": 2}, examples=["Ок, гляну"]),
        **values,
    )


def _blocks(fake_llm: FakeAdapter) -> dict[str, Any]:
    (call,) = fake_llm.calls_for("compose.draft")
    return {b.name: json.loads(b.content) for b in call.user_blocks if b.kind == "data"}


@pytest.fixture(autouse=True)
def _responses(fake_llm: FakeAdapter) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=[], task_ids=[], commitment_ids=[]),
    )
    fake_llm.set_response("compose.draft", _draft())


@pytest.mark.parametrize("gender", ["masculine", "feminine"])
async def test_set_gender_is_its_own_block_outside_the_profile(
    llm_router: LLMRouter, fake_llm: FakeAdapter, gender: str
) -> None:
    result = await compose_output(
        llm_router, _input(user=UserContext.model_validate({"grammatical_gender": gender}))
    )
    blocks = _blocks(fake_llm)
    assert blocks["user"] == {"grammatical_gender": gender}
    assert "grammatical_gender" not in json.dumps(blocks["profile"])
    assert result.request.user.grammatical_gender == gender


async def test_unset_gender_is_sent_as_null(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    await compose_output(llm_router, _input())
    assert _blocks(fake_llm)["user"] == {"grammatical_gender": None}


async def test_the_user_block_is_not_part_of_the_cached_profile_prefix(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    await compose_output(
        llm_router, _input(user=UserContext.model_validate({"grammatical_gender": "feminine"}))
    )
    (call,) = fake_llm.calls_for("compose.draft")
    stable = [b.name for b in call.user_blocks if b.stable]
    assert stable == ["profile"]


def test_only_two_values() -> None:
    with pytest.raises(ValueError):
        UserContext.model_validate({"grammatical_gender": "neutral"})
