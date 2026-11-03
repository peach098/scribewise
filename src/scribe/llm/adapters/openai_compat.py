"""OpenAI and OpenAI-compatible servers (vLLM, Ollama, ...) via ``openai.AsyncOpenAI``.

Uses ``response_format`` JSON schema when the server supports it (``native_json_schema``),
otherwise ``schema_fallback``. No batch API and no explicit cache markers are used; prompt
caching that the server applies on its own (OpenAI, DeepSeek) is read from the usage and billed
at the cached rate. ``reasoning_effort`` is sent only when the provider config enables it
(``providers.openai_compat.reasoning_effort``). A route with ``effort: none`` turns thinking
off with DeepSeek's ``thinking: {type: disabled}`` body field (issue #68), only when the
provider config enables it (``providers.openai_compat.disable_thinking``); no
``reasoning_effort`` is sent with it. A route's ``max_tokens`` goes out as
``max_completion_tokens`` unless ``providers.openai_compat.max_tokens_param`` names
``max_tokens`` (DeepSeek, servers that predate the OpenAI rename).

A truncated (``finish_reason == "length"``) or invalid answer raises ``ProviderOutputInvalid``
carrying the reported token usage (reasoning tokens included) and the finish reason, so the
``llm_call`` failure line can show where the budget went; never the content.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import openai
from pydantic import BaseModel, ValidationError

from scribe.llm.provider import (
    BatchOutcome,
    Block,
    Capabilities,
    Effort,
    MaxTokensParam,
    ProviderError,
    ProviderOutputInvalid,
    ProviderRefused,
    ProviderUnavailable,
    StructuredCall,
    StructuredResult,
    Usage,
)
from scribe.llm.schema_fallback import ChatMessage, complete_with_schema_fallback

RETRYABLE_STATUS = frozenset({408, 409, 429})


def _map_api_error(exc: openai.APIError) -> ProviderUnavailable:
    if isinstance(exc, openai.RateLimitError):
        return ProviderUnavailable("openai_compat rate limited", status_code=429, retryable=True)
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        return ProviderUnavailable(
            f"openai_compat API error {status}",
            status_code=status,
            retryable=status >= 500 or status in RETRYABLE_STATUS,
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderUnavailable("openai_compat connection error", retryable=True)
    return ProviderUnavailable(f"openai_compat error ({type(exc).__name__})", retryable=False)


def _cached_prompt_tokens(raw: Any) -> int:
    """Prompt tokens served from the server's cache.

    OpenAI reports them as ``prompt_tokens_details.cached_tokens``; DeepSeek reports
    ``prompt_cache_hit_tokens`` (and usually the same number in ``cached_tokens``). Either way
    they are part of ``prompt_tokens``.
    """
    details = getattr(raw, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if not cached:
        cached = getattr(raw, "prompt_cache_hit_tokens", None)
    return cached if isinstance(cached, int) and cached > 0 else 0


def _usage(raw: Any, latency_ms: int) -> Usage:
    if raw is None:
        return Usage(latency_ms=latency_ms)
    cached = _cached_prompt_tokens(raw)
    prompt = raw.prompt_tokens or 0
    completion = getattr(raw, "completion_tokens_details", None)
    reasoning = (getattr(completion, "reasoning_tokens", None) or 0) if completion else 0
    return Usage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=raw.completion_tokens or 0,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
        latency_ms=latency_ms,
    )


class OpenAICompatAdapter:
    name = "openai_compat"

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        *,
        native_json_schema: bool = True,
        reasoning_effort: bool = False,
        disable_thinking: bool = False,
        max_tokens_param: MaxTokensParam = "max_completion_tokens",
    ) -> None:
        self._client = client
        self._max_tokens_param = max_tokens_param
        self.capabilities = Capabilities(
            structured_outputs=native_json_schema,
            prompt_caching=False,
            batch=False,
            reasoning_effort=reasoning_effort,
            disable_thinking=disable_thinking,
        )

    async def _chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int,
        effort: Effort | None,
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str, Usage, str, str | None]:
        """(content, usage, served model, finish reason)."""
        params: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            self._max_tokens_param: max_tokens,
        }
        if response_format is not None:
            params["response_format"] = response_format
        if effort == "none":
            if self.capabilities.disable_thinking:
                # DeepSeek: "takes precedence over reasoning_effort"; not an OpenAI SDK argument.
                params["extra_body"] = {"thinking": {"type": "disabled"}}
        elif effort is not None and self.capabilities.reasoning_effort:
            params["reasoning_effort"] = effort
        started = time.monotonic()
        try:
            completion = await self._client.chat.completions.create(**params)
        except openai.APIError as exc:
            raise _map_api_error(exc) from None
        latency_ms = int((time.monotonic() - started) * 1000)
        if not completion.choices:
            raise ProviderOutputInvalid("openai_compat returned no choices")
        choice = completion.choices[0]
        usage = _usage(completion.usage, latency_ms)
        if getattr(choice.message, "refusal", None):
            raise ProviderRefused("openai_compat refused the request")
        if choice.finish_reason == "length":
            raise ProviderOutputInvalid(
                "openai_compat output truncated at max tokens",
                truncated=True,
                usage=usage,
                finish_reason=choice.finish_reason,
            )
        if choice.finish_reason == "content_filter":
            raise ProviderRefused("openai_compat content filter", category="content_filter")
        return choice.message.content or "", usage, completion.model, choice.finish_reason

    async def complete_structured[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user_blocks: Sequence[Block],
        schema: type[T],
        effort: Effort | None,
        max_tokens: int,
        cache_prefix: bool,
    ) -> StructuredResult[T]:
        user_text = "\n\n".join(block.render() for block in user_blocks)
        if self.capabilities.structured_outputs:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            }
            text, usage, served, finish_reason = await self._chat(
                model=model,
                system=system,
                messages=[{"role": "user", "content": user_text}],
                max_tokens=max_tokens,
                effort=effort,
                response_format=response_format,
            )
            try:
                output = schema.model_validate_json(text)
            except ValidationError:
                raise ProviderOutputInvalid(
                    f"{schema.__name__} failed validation",
                    usage=usage,
                    finish_reason=finish_reason,
                ) from None
            return StructuredResult(output=output, usage=usage, model=served)

        served_models: list[str] = []

        async def complete(system_text: str, messages: list[ChatMessage]) -> tuple[str, Usage]:
            text, usage, served, _ = await self._chat(
                model=model,
                system=system_text,
                messages=messages,
                max_tokens=max_tokens,
                effort=effort,
            )
            served_models.append(served)
            return text, usage

        output, usage = await complete_with_schema_fallback(
            complete, system=system, user_text=user_text, schema=schema
        )
        return StructuredResult(output=output, usage=usage, model=served_models[-1])

    async def complete_structured_batch(
        self, calls: Sequence[StructuredCall]
    ) -> list[BatchOutcome]:
        """No batch API: sequential calls (the router only batches when capabilities allow)."""
        outcomes: list[BatchOutcome] = []
        for call in calls:
            try:
                outcomes.append(
                    await self.complete_structured(
                        model=call.model,
                        system=call.system,
                        user_blocks=call.user_blocks,
                        schema=call.schema,
                        effort=call.effort,
                        max_tokens=call.max_tokens,
                        cache_prefix=call.cache_prefix,
                    )
                )
            except ProviderError as exc:
                outcomes.append(exc)
        return outcomes
