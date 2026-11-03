"""Deterministic in-process adapter for tests.

Canned outputs are registered per route (each route has its own output schema) and optionally
per request fingerprint; every call is recorded for assertions. Unregistered calls fail loudly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from scribe.llm.aliases import prompt_request
from scribe.llm.provider import (
    ROUTE_OUTPUT_SCHEMAS,
    BatchOutcome,
    Block,
    Capabilities,
    Effort,
    LLMRequest,
    ProviderError,
    RouteName,
    StructuredCall,
    StructuredResult,
    Usage,
)

FakeResponse = BaseModel | Exception | Callable[["FakeCall"], BaseModel]


class FakeResponseMissing(LookupError):
    """No canned response registered for this call."""


def fingerprint_blocks(blocks: Sequence[Block]) -> str:
    payload = json.dumps([[b.kind, b.name, b.content] for b in blocks], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fingerprint_request(request: LLMRequest) -> str:
    """The fingerprint the adapter will see for ``request`` (the router sends its blocks, with
    short ids for the filter and compose routes, issue #71)."""
    return fingerprint_blocks(prompt_request(request).to_blocks())


@dataclass(frozen=True)
class FakeCall:
    model: str
    system: str
    user_blocks: tuple[Block, ...]
    schema: type[BaseModel]
    effort: Effort | None
    max_tokens: int
    cache_prefix: bool
    batch: bool
    fingerprint: str


class FakeAdapter:
    name = "fake"

    def __init__(
        self,
        *,
        capabilities: Capabilities | None = None,
        latency_s: float = 0.0,
    ) -> None:
        self.capabilities = capabilities or Capabilities(
            structured_outputs=True,
            prompt_caching=True,
            batch=True,
            reasoning_effort=True,
            disable_thinking=True,
        )
        self.latency_s = latency_s
        self.calls: list[FakeCall] = []
        self._responses: dict[tuple[type[BaseModel], str | None], FakeResponse] = {}

    def set_response(
        self, route: RouteName, response: FakeResponse, *, request: LLMRequest | None = None
    ) -> None:
        """Register the output for ``route`` (for one request only when ``request`` is given)."""
        key = (
            ROUTE_OUTPUT_SCHEMAS[route],
            None if request is None else fingerprint_request(request),
        )
        self._responses[key] = response

    def calls_for(self, route: RouteName) -> list[FakeCall]:
        schema = ROUTE_OUTPUT_SCHEMAS[route]
        return [call for call in self.calls if call.schema is schema]

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
        return await self._complete(
            StructuredCall(
                model=model,
                system=system,
                user_blocks=user_blocks,
                schema=schema,
                effort=effort,
                max_tokens=max_tokens,
                cache_prefix=cache_prefix,
            ),
            schema,
            batch=False,
        )

    async def complete_structured_batch(
        self, calls: Sequence[StructuredCall]
    ) -> list[BatchOutcome]:
        outcomes: list[BatchOutcome] = []
        for call in calls:
            try:
                outcomes.append(await self._complete(call, call.schema, batch=True))
            except ProviderError as exc:
                outcomes.append(exc)
        return outcomes

    async def _complete[T: BaseModel](
        self, request: StructuredCall, schema: type[T], *, batch: bool
    ) -> StructuredResult[T]:
        fingerprint = fingerprint_blocks(request.user_blocks)
        call = FakeCall(
            model=request.model,
            system=request.system,
            user_blocks=tuple(request.user_blocks),
            schema=schema,
            effort=request.effort,
            max_tokens=request.max_tokens,
            cache_prefix=request.cache_prefix,
            batch=batch,
            fingerprint=fingerprint,
        )
        self.calls.append(call)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)

        response = self._responses.get((schema, fingerprint), self._responses.get((schema, None)))
        if response is None:
            raise FakeResponseMissing(f"no fake response for {schema.__name__} ({fingerprint})")
        if isinstance(response, Exception):
            raise response
        output = response(call) if callable(response) else response
        validated = schema.model_validate(output.model_dump())
        chars = len(request.system) + sum(len(b.content) for b in request.user_blocks)
        usage = Usage(
            input_tokens=chars // 4,
            output_tokens=len(validated.model_dump_json()) // 4,
            latency_ms=int(self.latency_s * 1000),
        )
        return StructuredResult(output=validated, usage=usage, model=request.model, batch=batch)
