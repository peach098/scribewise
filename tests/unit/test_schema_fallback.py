"""Schema-in-prompt fallback: parse -> validate -> one retry -> ProviderOutputInvalid (T016)."""

from __future__ import annotations

import json

import pytest

from scribe.llm.provider import ProviderOutputInvalid, StyleJudgeOutput, Usage
from scribe.llm.schema_fallback import (
    ChatMessage,
    complete_with_schema_fallback,
    error_summary,
    extract_json,
)

GOOD = json.dumps({"score": 0.8, "reasons": ["synthetic"]})


class ScriptedModel:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def __call__(self, system: str, messages: list[ChatMessage]) -> tuple[str, Usage]:
        self.calls.append((system, list(messages)))
        return self.replies.pop(0), Usage(input_tokens=10, output_tokens=5)


async def _run(model: ScriptedModel) -> tuple[StyleJudgeOutput, Usage]:
    return await complete_with_schema_fallback(
        model, system="Judge style.", user_text="<data>draft</data>", schema=StyleJudgeOutput
    )


async def test_valid_first_reply() -> None:
    model = ScriptedModel([GOOD])
    output, usage = await _run(model)
    assert output.score == 0.8
    assert usage == Usage(input_tokens=10, output_tokens=5)
    system, messages = model.calls[0]
    assert system.startswith("Judge style.")
    assert '"reasons"' in system  # schema is in the system prompt
    assert messages == [{"role": "user", "content": "<data>draft</data>"}]


async def test_invalid_then_valid_retries_once_with_error_summary() -> None:
    bad = json.dumps({"score": 1.7, "reasons": ["secret synthetic value"]})
    model = ScriptedModel([bad, f"Sure! ```json\n{GOOD}\n```"])
    output, usage = await _run(model)
    assert output.reasons == ["synthetic"]
    assert usage.input_tokens == 20 and usage.output_tokens == 10
    _, retry_messages = model.calls[1]
    assert [m["role"] for m in retry_messages] == ["user", "assistant", "user"]
    correction = retry_messages[-1]["content"]
    assert "score" in correction and "less_than_equal" in correction
    assert "secret synthetic value" not in correction  # values are never echoed


async def test_two_invalid_replies_raise() -> None:
    model = ScriptedModel(["no json here", '{"score": "high"}'])
    with pytest.raises(ProviderOutputInvalid, match="StyleJudgeOutput invalid after 2 attempts"):
        await _run(model)
    assert len(model.calls) == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('Here you go: {"a": {"b": 2}} thanks', '{"a": {"b": 2}}'),
    ],
)
def test_extract_json(text: str, expected: str) -> None:
    assert extract_json(text) == expected


def test_extract_json_without_object_raises() -> None:
    with pytest.raises(ValueError):
        extract_json("no object")


def test_error_summary_for_non_validation_error() -> None:
    assert error_summary(ValueError("x")) == "reply was not a JSON object"
