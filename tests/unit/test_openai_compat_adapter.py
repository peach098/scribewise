"""OpenAI-compatible adapter with a fake client (T016). No network."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from scribe.llm.adapters.openai_compat import OpenAICompatAdapter
from scribe.llm.provider import (
    ProviderOutputInvalid,
    ProviderRefused,
    ProviderUnavailable,
    StyleJudgeOutput,
    data_block,
)

GOOD = json.dumps({"score": 0.7, "reasons": ["synthetic"]})


def _completion(
    content: str | None = GOOD, *, finish_reason: str = "stop", refusal: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        model="served-model",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, refusal=refusal),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
        ),
    )


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _adapter(responses: list[Any], **kwargs: Any) -> tuple[OpenAICompatAdapter, FakeCompletions]:
    completions = FakeCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAICompatAdapter(client, **kwargs), completions  # type: ignore[arg-type]


async def _complete(adapter: OpenAICompatAdapter) -> Any:
    return await adapter.complete_structured(
        model="local-model",
        system="Judge style.",
        user_blocks=[data_block("draft_text", "synthetic")],
        schema=StyleJudgeOutput,
        effort="low",
        max_tokens=500,
        cache_prefix=True,
    )


async def test_native_json_schema_path() -> None:
    adapter, completions = _adapter([_completion()])
    result = await _complete(adapter)
    assert result.output.score == 0.7
    assert result.model == "served-model"
    assert (result.usage.input_tokens, result.usage.cached_input_tokens) == (60, 40)
    (params,) = completions.calls
    assert params["response_format"]["type"] == "json_schema"
    assert params["response_format"]["json_schema"]["strict"] is True
    assert params["messages"][0] == {"role": "system", "content": "Judge style."}
    assert "synthetic" in params["messages"][1]["content"]
    assert "reasoning_effort" not in params  # capability off by default
    assert adapter.capabilities.batch is False and adapter.capabilities.prompt_caching is False


async def test_reasoning_effort_sent_when_supported() -> None:
    adapter, completions = _adapter([_completion()], reasoning_effort=True)
    await _complete(adapter)
    assert completions.calls[0]["reasoning_effort"] == "low"


async def _complete_with(adapter: OpenAICompatAdapter, effort: Any) -> Any:
    return await adapter.complete_structured(
        model="deepseek-flash",
        system="Judge style.",
        user_blocks=[data_block("draft_text", "synthetic")],
        schema=StyleJudgeOutput,
        effort=effort,
        max_tokens=500,
        cache_prefix=False,
    )


async def test_effort_none_turns_deepseek_thinking_off() -> None:
    """Issue #68: ``thinking: {type: disabled}`` in the body (DeepSeek's switch, which "takes
    precedence over reasoning_effort"), sent through the SDK's ``extra_body``; no
    ``reasoning_effort`` with it."""
    adapter, completions = _adapter([_completion()], reasoning_effort=True, disable_thinking=True)
    await _complete_with(adapter, "none")
    (params,) = completions.calls
    assert params["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in params


async def test_other_efforts_leave_thinking_on() -> None:
    adapter, completions = _adapter([_completion()], reasoning_effort=True, disable_thinking=True)
    await _complete_with(adapter, "low")
    (params,) = completions.calls
    assert params["reasoning_effort"] == "low" and "extra_body" not in params


async def test_effort_none_sends_nothing_without_the_capability() -> None:
    """The router never passes ``none`` here (``build_adapters`` refuses the table); if it did,
    nothing is sent rather than ``reasoning_effort: none`` to a server that may reject it."""
    adapter, completions = _adapter([_completion()], reasoning_effort=True)
    await _complete_with(adapter, "none")
    (params,) = completions.calls
    assert "extra_body" not in params and "reasoning_effort" not in params


async def test_max_tokens_goes_out_as_max_completion_tokens_by_default() -> None:
    adapter, completions = _adapter([_completion()])
    await _complete(adapter)
    (params,) = completions.calls
    assert params["max_completion_tokens"] == 500
    assert "max_tokens" not in params


async def test_max_tokens_param_switches_the_request_field() -> None:
    # DeepSeek documents only max_tokens and ignores max_completion_tokens.
    adapter, completions = _adapter(
        [_completion("not json"), _completion(GOOD)],
        native_json_schema=False,
        max_tokens_param="max_tokens",
    )
    await _complete(adapter)
    assert len(completions.calls) == 2  # the schema-fallback retry carries the limit too
    for params in completions.calls:
        assert params["max_tokens"] == 500
        assert "max_completion_tokens" not in params


async def test_schema_fallback_path_retries_once() -> None:
    adapter, completions = _adapter(
        [_completion("not json"), _completion(GOOD)], native_json_schema=False
    )
    result = await _complete(adapter)
    assert result.output.reasons == ["synthetic"]
    assert len(completions.calls) == 2
    assert all("response_format" not in call for call in completions.calls)
    assert "JSON schema" in completions.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("completion", "error"),
    [
        (_completion(None, refusal="I can't help with that."), ProviderRefused),
        (_completion(finish_reason="length"), ProviderOutputInvalid),
        (_completion(finish_reason="content_filter"), ProviderRefused),
        (_completion('{"score": 3}'), ProviderOutputInvalid),
    ],
)
async def test_bad_completions(completion: Any, error: type[Exception]) -> None:
    adapter, _ = _adapter([completion])
    with pytest.raises(error):
        await _complete(adapter)


async def test_api_errors_map_to_provider_unavailable() -> None:
    request = httpx2.Request("POST", "http://localhost/v1/chat/completions")
    rate = openai.RateLimitError("rate", response=httpx2.Response(429, request=request), body=None)
    adapter, _ = _adapter([rate, openai.APIConnectionError(request=request)])
    with pytest.raises(ProviderUnavailable) as info:
        await _complete(adapter)
    assert info.value.status_code == 429
    with pytest.raises(ProviderUnavailable):
        await _complete(adapter)


async def test_batch_falls_back_to_sequential_calls() -> None:
    from scribe.llm.provider import StructuredCall

    adapter, completions = _adapter([_completion(), _completion(finish_reason="length")])
    call = StructuredCall(
        model="m",
        system="s",
        user_blocks=[data_block("x", 1)],
        schema=StyleJudgeOutput,
        effort=None,
        max_tokens=10,
    )
    first, second = await adapter.complete_structured_batch([call, call])
    assert not isinstance(first, Exception)
    assert isinstance(second, ProviderOutputInvalid)


async def test_truncation_carries_usage_with_reasoning_tokens() -> None:
    """Issue #30: finish_reason "length" still reports usage, reasoning tokens included."""
    completion = _completion('{"score": 0.', finish_reason="length")
    completion.usage.completion_tokens = 32000
    completion.usage.completion_tokens_details = SimpleNamespace(reasoning_tokens=30500)
    adapter, _ = _adapter([completion])
    with pytest.raises(ProviderOutputInvalid) as info:
        await _complete(adapter)
    exc = info.value
    assert exc.truncated is True and exc.finish_reason == "length"
    assert exc.usage is not None
    assert (exc.usage.input_tokens, exc.usage.output_tokens) == (60, 32000)
    assert exc.usage.reasoning_tokens == 30500


async def test_invalid_output_carries_usage() -> None:
    adapter, _ = _adapter([_completion('{"score": 3}')])
    with pytest.raises(ProviderOutputInvalid) as info:
        await _complete(adapter)
    assert info.value.truncated is False and info.value.finish_reason == "stop"
    assert info.value.usage is not None and info.value.usage.output_tokens == 10


async def test_schema_fallback_failure_carries_total_usage() -> None:
    adapter, _ = _adapter(
        [_completion("not json"), _completion("still not")], native_json_schema=False
    )
    with pytest.raises(ProviderOutputInvalid) as info:
        await _complete(adapter)
    assert info.value.truncated is False
    assert info.value.usage is not None and info.value.usage.output_tokens == 20


def _deepseek_usage(*, with_details: bool) -> Any:
    """A usage object as DeepSeek returns it, parsed by the OpenAI SDK (extra fields kept)."""
    from openai.types import CompletionUsage

    raw: dict[str, Any] = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "total_tokens": 1050,
        "prompt_cache_hit_tokens": 768,
        "prompt_cache_miss_tokens": 232,
        "completion_tokens_details": {"reasoning_tokens": 30},
    }
    if with_details:
        raw["prompt_tokens_details"] = {"cached_tokens": 768}
    return CompletionUsage.model_validate(raw)


@pytest.mark.parametrize("with_details", [True, False])
async def test_deepseek_cache_hits_are_cached_input(with_details: bool) -> None:
    """DeepSeek reports cache hits as prompt_cache_hit_tokens; they must not count as input."""
    completion = _completion()
    completion.usage = _deepseek_usage(with_details=with_details)
    adapter, _ = _adapter([completion])
    result = await _complete(adapter)
    usage = result.usage
    assert (usage.input_tokens, usage.cached_input_tokens) == (232, 768)
    assert (usage.output_tokens, usage.reasoning_tokens) == (50, 30)
    # Issue #67: the llm_call line's hit/miss counts are DeepSeek's own two fields.
    assert (usage.cache_hit_tokens, usage.cache_miss_tokens) == (768, 232)


async def test_usage_without_cache_fields() -> None:
    completion = _completion()
    completion.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10)
    adapter, _ = _adapter([completion])
    result = await _complete(adapter)
    assert (result.usage.input_tokens, result.usage.cached_input_tokens) == (100, 0)
