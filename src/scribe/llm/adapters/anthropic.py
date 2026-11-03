"""Anthropic (Claude) adapter (research R5).

- Structured outputs via ``output_config.format`` (JSON schema derived from the pydantic model
  with ``anthropic.transform_schema``); no tools, no assistant prefill.
- Thinking is left at the model default (adaptive; it cannot be disabled on Claude Opus 5.5);
  depth is controlled per route with ``output_config.effort``.
- Prompt caching (``cache_prefix``): the system prompt, then the stable data blocks (e.g. the
  style profile) first. One breakpoint ends the system prompt, which is identical for every user
  of a route, and one ends the stable blocks (per user); at most 2 of the 4 allowed. Both use
  the same TTL, 1 hour by default (``providers.anthropic.cache_ttl``), so the rule that longer
  TTLs come before shorter ones always holds. 1-hour writes cost 2x input, 5-minute ones 1.25x.
- Server-side refusal fallbacks (``fallbacks="default"`` + beta header) on every model that
  supports them; Claude Haiku 5.5 has none, so the parameter is not sent for it, and the
  Message Batches API rejects it, so batches never carry it.
- ``stop_reason`` is checked before content is read: ``refusal`` -> ``ProviderRefused``;
  ``max_tokens`` -> ``ProviderOutputInvalid(truncated=True)``. A truncated or invalid answer
  carries the reported usage and stop reason for the ``llm_call`` log line (thinking tokens are
  part of ``output_tokens``; the API does not report them separately).
- Errors: RateLimitError -> APIStatusError -> APIConnectionError, all ``ProviderUnavailable``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, cast

import anthropic
from pydantic import BaseModel, ValidationError

from scribe.llm.provider import (
    BatchOutcome,
    Block,
    Capabilities,
    Effort,
    ProviderError,
    ProviderOutputInvalid,
    ProviderRefused,
    ProviderUnavailable,
    StructuredCall,
    StructuredResult,
    Usage,
)

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models without server-side refusal fallback: sending `fallbacks` is pointless (or a 400).
MODELS_WITHOUT_SERVER_FALLBACK = frozenset({"claude-haiku-5-5"})
CacheTTL = Literal["5m", "1h"]
DEFAULT_CACHE_TTL: CacheTTL = "1h"
MAX_CACHE_BREAKPOINTS = 4  # API limit per request
RETRYABLE_STATUS = frozenset({408, 409, 429})

Sleep = Callable[[float], Awaitable[None]]


def _map_api_error(exc: Exception) -> ProviderUnavailable:
    """Typed SDK errors -> ProviderUnavailable (message carries no request content)."""
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderUnavailable("anthropic rate limited", status_code=429, retryable=True)
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        return ProviderUnavailable(
            f"anthropic API error {status}",
            status_code=status,
            retryable=status >= 500 or status in RETRYABLE_STATUS,
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailable("anthropic connection error", retryable=True)
    return ProviderUnavailable(f"anthropic error ({type(exc).__name__})", retryable=False)


def _usage(raw: Any, latency_ms: int) -> Usage:
    """``cache_creation_input_tokens`` is every cache write; ``cache_creation`` splits it by TTL.
    1-hour writes (2x input) are kept apart from 5-minute ones (1.25x) so each is priced right."""
    written = getattr(raw, "cache_creation_input_tokens", None) or 0
    breakdown = getattr(raw, "cache_creation", None)
    written_1h = getattr(breakdown, "ephemeral_1h_input_tokens", None) or 0
    written_5m = getattr(breakdown, "ephemeral_5m_input_tokens", None) or 0
    written = max(written, written_1h + written_5m)
    return Usage(
        input_tokens=raw.input_tokens or 0,
        output_tokens=raw.output_tokens or 0,
        cached_input_tokens=getattr(raw, "cache_read_input_tokens", None) or 0,
        cache_write_input_tokens=written - written_1h,
        cache_write_1h_input_tokens=written_1h,
        latency_ms=latency_ms,
    )


def parse_message[T: BaseModel](
    message: Any, schema: type[T], *, latency_ms: int, batch: bool = False
) -> StructuredResult[T]:
    """Validate a Messages API response; ``stop_reason`` is checked before any content."""
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        raise ProviderRefused(
            "anthropic refused the request", category=getattr(details, "category", None)
        )
    usage = message.usage
    if stop_reason == "max_tokens":
        raise ProviderOutputInvalid(
            f"{schema.__name__} output truncated at max_tokens",
            truncated=True,
            usage=_usage(usage, latency_ms) if usage is not None else None,
            finish_reason=stop_reason,
        )
    text = "".join(block.text for block in message.content if block.type == "text")
    try:
        output = schema.model_validate_json(text)
    except ValidationError:
        # `from None`: the validation error would echo model output (content) into tracebacks.
        raise ProviderOutputInvalid(
            f"{schema.__name__} failed validation",
            usage=_usage(usage, latency_ms) if usage is not None else None,
            finish_reason=stop_reason,
        ) from None
    iterations = getattr(usage, "iterations", None) or []
    fallback_used = any(getattr(it, "type", None) == "fallback_message" for it in iterations)
    return StructuredResult(
        output=output,
        usage=_usage(usage, latency_ms),
        model=str(message.model),
        batch=batch,
        fallback_used=fallback_used,
    )


class AnthropicAdapter:
    name = "anthropic"
    capabilities = Capabilities(
        structured_outputs=True, prompt_caching=True, batch=True, reasoning_effort=True
    )

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        models_without_server_fallback: frozenset[str] = MODELS_WITHOUT_SERVER_FALLBACK,
        batch_poll_interval_s: float = 30.0,
        batch_timeout_s: float = 24 * 3600,
        sleep: Sleep = asyncio.sleep,
        cache_ttl: CacheTTL = DEFAULT_CACHE_TTL,
    ) -> None:
        self._client = client
        self._cache_ttl = cache_ttl
        self._no_fallback = models_without_server_fallback
        self._poll_interval_s = batch_poll_interval_s
        self._batch_timeout_s = batch_timeout_s
        self._sleep = sleep

    def build_params(
        self,
        *,
        model: str,
        system: str,
        user_blocks: Sequence[Block],
        schema: type[BaseModel],
        effort: Effort | None,
        max_tokens: int,
        cache_prefix: bool,
        for_batch: bool = False,
    ) -> dict[str, Any]:
        stable = [b for b in user_blocks if b.stable]
        volatile = [b for b in user_blocks if not b.stable]
        system_block: dict[str, Any] = {"type": "text", "text": system}
        content: list[dict[str, Any]] = [
            {"type": "text", "text": block.render()} for block in [*stable, *volatile]
        ]
        if cache_prefix:
            # Shared by every user of the route: cached on its own, so a user's first draft
            # reads it even when their profile prefix has expired.
            system_block["cache_control"] = self._cache_control()
            if stable:
                content[len(stable) - 1]["cache_control"] = self._cache_control()

        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
        }
        if effort == "none":
            # Thinking off is an openai_compat (DeepSeek) option; build_adapters refuses a
            # route table that gives it to Anthropic, so this is a programming error.
            raise ValueError("anthropic has no effort 'none'")
        if effort is not None:
            output_config["effort"] = effort

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [system_block],
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
        }
        if not for_batch and model not in self._no_fallback:
            params["betas"] = [SERVER_FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    def _cache_control(self) -> dict[str, Any]:
        return {"type": "ephemeral", "ttl": self._cache_ttl}

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
        params = self.build_params(
            model=model,
            system=system,
            user_blocks=user_blocks,
            schema=schema,
            effort=effort,
            max_tokens=max_tokens,
            cache_prefix=cache_prefix,
        )
        started = time.monotonic()
        try:
            message = await self._client.beta.messages.create(**params)
        except anthropic.APIError as exc:
            raise _map_api_error(exc) from None
        latency_ms = int((time.monotonic() - started) * 1000)
        return parse_message(message, schema, latency_ms=latency_ms)

    async def complete_structured_batch(
        self, calls: Sequence[StructuredCall]
    ) -> list[BatchOutcome]:
        """Message Batches API (50% price; usually < 1 h, at most 24 h)."""
        requests = [
            {
                "custom_id": f"req-{index}",
                "params": self.build_params(
                    model=call.model,
                    system=call.system,
                    user_blocks=call.user_blocks,
                    schema=call.schema,
                    effort=call.effort,
                    max_tokens=call.max_tokens,
                    cache_prefix=call.cache_prefix,
                    for_batch=True,
                ),
            }
            for index, call in enumerate(calls)
        ]
        started = time.monotonic()
        batches = self._client.messages.batches
        try:
            batch = await batches.create(requests=cast(Any, requests))
            while batch.processing_status != "ended":
                if time.monotonic() - started > self._batch_timeout_s:
                    raise ProviderUnavailable("anthropic batch timed out", retryable=True)
                await self._sleep(self._poll_interval_s)
                batch = await batches.retrieve(batch.id)
            latency_ms = int((time.monotonic() - started) * 1000)
            outcomes: dict[str, BatchOutcome] = {}
            async for item in await batches.results(batch.id):
                index = int(item.custom_id.removeprefix("req-"))
                outcomes[item.custom_id] = self._batch_outcome(
                    item.result, calls[index].schema, latency_ms
                )
        except anthropic.APIError as exc:
            raise _map_api_error(exc) from None
        return [
            outcomes.get(f"req-{i}", ProviderUnavailable("missing batch result"))
            for i in range(len(calls))
        ]

    @staticmethod
    def _batch_outcome(result: Any, schema: type[BaseModel], latency_ms: int) -> BatchOutcome:
        if result.type == "succeeded":
            try:
                return parse_message(result.message, schema, latency_ms=latency_ms, batch=True)
            except ProviderError as exc:
                return exc
        if result.type == "errored":
            error = getattr(getattr(result, "error", None), "error", None)
            error_type = getattr(error, "type", "unknown")
            return ProviderUnavailable(
                f"anthropic batch item errored ({error_type})",
                retryable=error_type != "invalid_request_error",
            )
        return ProviderUnavailable(f"anthropic batch item {result.type}", retryable=True)
