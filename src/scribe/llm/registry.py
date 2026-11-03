"""LLM router: resolves a logical route to adapter + model + effort, loads the versioned
prompt, calls the adapter and logs one structured line per call (never content).

The ``llm_call`` line has the effort sent, token usage, latency and cost; a failed call logs the
error class and, when the provider reported them for the failed answer (truncated or invalid
output), the token usage (reasoning tokens included) and the finish reason (issue #30).
Every call also carries the ``user_id`` it is made for (``None`` for eval runs) and the cache
hit/miss token counts; with a ``spend_sink`` (the service plugs in the daily ``llm_spend``
aggregate, issue #67) each billed call, failed ones with reported usage included, is recorded
for that user. Costs are priced at the call's start time (DeepSeek off-peak, ``pricing.py``).
``build_style_profile(..., lighter=True)`` is the lighter retry of ``profile.build``: the
route's effort one step lower (``routes.lighter_effort``), always a synchronous call.

Provider adapters are registered by name in ``ADAPTER_FACTORIES``; adding a provider is a new
adapter module plus one entry there.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from scribe.clock import Clock, utcnow
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.aliases import (
    alias_draft_request,
    alias_filter_request,
    unalias_draft_output,
    unalias_filter_output,
)
from scribe.llm.pricing import PriceTable
from scribe.llm.provider import (
    Capabilities,
    CommitmentExtraction,
    CommitmentItem,
    CommitmentRequest,
    DeadlineExtraction,
    DeadlineItem,
    DeadlineRequest,
    DraftOutput,
    DraftRequest,
    Effort,
    FilterCandidates,
    FilterContextOutput,
    FilterContextRequest,
    HistoryItem,
    LLMRequest,
    MessageItem,
    ProviderAdapter,
    ProviderError,
    ProviderOutputInvalid,
    ReplyNeededOutput,
    ReplyNeededRequest,
    RouteName,
    StructuredCall,
    StructuredResult,
    StyleJudgeOutput,
    StyleJudgeRequest,
    StyleProfileOutput,
    StyleProfileRequest,
    Tier,
    Usage,
)
from scribe.llm.routes import (
    ProviderConfig,
    RouteConfig,
    RouteTable,
    RouteTableError,
    lighter_effort,
)
from scribe.llm.spend import SpendEntry, SpendSink
from scribe.logging import get_logger

log = get_logger("scribe.llm")

FILTER_MAX_HISTORY = 15
FILTER_MAX_EARLIER = 10  # specs/004 FR-S06: earlier messages that join a draft
MAX_SEARCH_QUERIES = 5  # FR-S04
SEARCH_QUERY_CHARS = 60

PROMPT_FOR_ROUTE: dict[RouteName, str] = {
    "retrieve.filter_context": "filter_context",
    "classify.reply_needed": "reply_needed",
    "extract.commitments": "commitments",
    "extract.deadlines": "deadlines",
    "compose.draft": "draft",
    "profile.build": "style_profile",
    "eval.style_judge": "style_judge",
}


# --- prompts ------------------------------------------------------------------------------------


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


_FRONT_MATTER = re.compile(r"\A---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)\Z", re.DOTALL)


def parse_prompt(name: str, raw: str) -> Prompt:
    """Prompt file = YAML front matter with ``version:`` then the system prompt text."""
    match = _FRONT_MATTER.match(raw)
    if match is None:
        raise PromptError(f"prompt {name!r} has no front matter with a version line")
    meta = yaml.safe_load(match.group("meta")) or {}
    if not isinstance(meta, dict) or meta.get("version") in (None, ""):
        raise PromptError(f"prompt {name!r} front matter needs 'version:'")
    body = match.group("body").strip()
    if not body:
        raise PromptError(f"prompt {name!r} is empty")
    return Prompt(name=name, version=str(meta["version"]), text=body)


class PromptStore:
    """Loads ``<directory>/<name>.md`` once and caches it."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._cache: dict[str, Prompt] = {}

    def get(self, name: str) -> Prompt:
        prompt = self._cache.get(name)
        if prompt is None:
            path = self.directory / f"{name}.md"
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PromptError(f"cannot read prompt {path}: {exc}") from exc
            prompt = self._cache[name] = parse_prompt(name, raw)
        return prompt


# --- router -------------------------------------------------------------------------------------


def effort_sent(capabilities: Capabilities, effort: Effort | None) -> Effort | None:
    """The effort the adapter gets: ``none`` (thinking off) when it can turn thinking off,
    any other effort when it takes ``reasoning_effort``, else None (the provider default)."""
    if effort == "none":
        return effort if capabilities.disable_thinking else None
    return effort if capabilities.reasoning_effort else None


@dataclass(frozen=True)
class RoutedResult[T]:
    """Route output plus what callers persist for observability (no content)."""

    output: T
    route: RouteName
    provider: str
    model: str
    prompt_version: str
    usage: Usage
    cost_usd: float
    batch: bool = False

    def usage_record(self) -> dict[str, object]:
        """Metadata for ``drafts.llm_usage_json``."""
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            **self.usage.model_dump(),
            "cost_usd": self.cost_usd,
            "batch": self.batch,
        }


def sanitize_filter_output(
    output: FilterContextOutput, request: FilterContextRequest, *, max_history: int
) -> FilterContextOutput:
    """The filter may only *select*: drop ids that were not offered, dedupe, cap history."""

    def keep(ids: Sequence[str], offered: set[str]) -> list[str]:
        return list(dict.fromkeys(i for i in ids if i in offered))

    candidates = request.candidates
    queries = search_queries(output.search_queries) if output.needs_earlier_context else []
    return FilterContextOutput(
        history_ids=keep(output.history_ids, {h.id for h in candidates.history})[:max_history],
        task_ids=keep(output.task_ids, {t.id for t in candidates.tasks}),
        commitment_ids=keep(output.commitment_ids, {c.id for c in candidates.commitments}),
        needs_earlier_context=bool(queries),
        search_queries=queries,
    )


def search_queries(raw: Sequence[str]) -> list[str]:
    """Trimmed, ≥ 2 chars, ≤ 60 chars, case-insensitively unique, at most 5 (specs/004 S1)."""
    seen: set[str] = set()
    queries: list[str] = []
    for item in raw:
        query = " ".join(item.split())[:SEARCH_QUERY_CHARS].strip()
        if len(query) < 2 or query.casefold() in seen:
            continue
        seen.add(query.casefold())
        queries.append(query)
    return queries[:MAX_SEARCH_QUERIES]


class LLMRouter:
    def __init__(
        self,
        *,
        routes: RouteTable,
        prices: PriceTable,
        adapters: Mapping[str, ProviderAdapter],
        prompts: PromptStore,
        clock: Clock = utcnow,
        spend_sink: SpendSink | None = None,
    ) -> None:
        self.routes = routes
        self.prices = prices
        self.adapters = dict(adapters)
        self.prompts = prompts
        self.clock = clock
        self.spend_sink = spend_sink

    async def filter_context(
        self, req: FilterContextRequest, *, user_tier: Tier, user_id: uuid.UUID | None = None
    ) -> RoutedResult[FilterContextOutput]:
        result = await self._filter(req, user_tier, user_id)
        clean = sanitize_filter_output(result.output, req, max_history=FILTER_MAX_HISTORY)
        return replace(result, output=clean)

    async def classify_reply_needed(
        self, req: ReplyNeededRequest, *, user_id: uuid.UUID | None = None
    ) -> RoutedResult[ReplyNeededOutput]:
        """Reply gate classifier (004): untiered; raises ``ProviderError`` on failure."""
        return await self._call(
            "classify.reply_needed", req, ReplyNeededOutput, None, user_id=user_id
        )

    async def rank_earlier(
        self,
        incoming: Sequence[MessageItem],
        matches: Sequence[HistoryItem],
        *,
        user_tier: Tier,
        user_id: uuid.UUID | None = None,
    ) -> RoutedResult[FilterContextOutput]:
        """specs/004 S2: the filter route picks the found earlier messages that help answer the
        incoming ones, most relevant first, at most ``FILTER_MAX_EARLIER``. Only the incoming
        messages and the matches are sent (FR-S04a)."""
        req = FilterContextRequest(
            incoming_messages=list(incoming), candidates=FilterCandidates(history=list(matches))
        )
        result = await self._filter(req, user_tier, user_id)
        clean = sanitize_filter_output(result.output, req, max_history=FILTER_MAX_EARLIER)
        ranked = clean.model_copy(
            update={
                "task_ids": [],
                "commitment_ids": [],
                "needs_earlier_context": False,
                "search_queries": [],
            }
        )
        return replace(result, output=ranked)

    async def draft_reply(
        self, req: DraftRequest, *, user_tier: Tier, user_id: uuid.UUID | None = None
    ) -> RoutedResult[DraftOutput]:
        """Sent with short ids (issue #71); the answer comes back with the real ids, so
        ``invalid_source_ids`` checks it against ``req`` itself."""
        sent, aliases = alias_draft_request(req)
        result = await self._call(
            "compose.draft", sent, DraftOutput, user_tier, cache_prefix=True, user_id=user_id
        )
        return replace(result, output=unalias_draft_output(result.output, aliases))

    async def _filter(
        self, req: FilterContextRequest, user_tier: Tier, user_id: uuid.UUID | None
    ) -> RoutedResult[FilterContextOutput]:
        """The filter route with short ids (issue #71): the answer comes back with the real
        ids, unknown aliases left as they are, before ``sanitize_filter_output`` sees it."""
        sent, aliases = alias_filter_request(req)
        result = await self._call(
            "retrieve.filter_context",
            sent,
            FilterContextOutput,
            user_tier,
            cache_prefix=True,
            user_id=user_id,
        )
        return replace(result, output=unalias_filter_output(result.output, aliases))

    async def build_style_profile(
        self,
        req: StyleProfileRequest,
        *,
        batch: bool = False,
        lighter: bool = False,
        user_id: uuid.UUID | None = None,
    ) -> RoutedResult[StyleProfileOutput]:
        """One profile; ``lighter``: the route's effort one step lower (the retry after a
        truncated answer), always a synchronous call."""
        if batch and not lighter:
            (outcome,) = await self.build_style_profiles([req], user_id=user_id)
            if isinstance(outcome, ProviderError):
                raise outcome
            return outcome
        return await self._call(
            "profile.build", req, StyleProfileOutput, None, lighter=lighter, user_id=user_id
        )

    async def build_style_profiles(
        self,
        reqs: Sequence[StyleProfileRequest],
        *,
        interactive: bool = False,
        user_id: uuid.UUID | None = None,
    ) -> list[RoutedResult[StyleProfileOutput] | ProviderError]:
        """Several profiles of one user at once: one batch when the route prefers it and the
        adapter can batch (50% cost, up to 24 h), else sequential synchronous calls.
        ``interactive`` (the user is waiting, e.g. 🔄 Rebuild) always uses synchronous calls."""
        route: RouteName = "profile.build"
        config = self.routes.resolve(route)
        adapter = self.adapters[config.provider]
        if interactive or not (config.prefer_batch and adapter.capabilities.batch):
            results: list[RoutedResult[StyleProfileOutput] | ProviderError] = []
            for req in reqs:
                try:
                    results.append(
                        await self._call(route, req, StyleProfileOutput, None, user_id=user_id)
                    )
                except ProviderError as exc:
                    results.append(exc)
            return results

        prompt = self.prompts.get(PROMPT_FOR_ROUTE[route])
        effort = effort_sent(adapter.capabilities, config.effort)
        calls = [
            StructuredCall(
                model=config.model,
                system=prompt.text,
                user_blocks=req.to_blocks(),
                schema=StyleProfileOutput,
                effort=effort,
                max_tokens=config.max_tokens,
            )
            for req in reqs
        ]
        at = self.clock()
        started = time.monotonic()
        outcomes = await adapter.complete_structured_batch(calls)
        latency_ms = int((time.monotonic() - started) * 1000)
        routed: list[RoutedResult[StyleProfileOutput] | ProviderError] = []
        for outcome in outcomes:
            if isinstance(outcome, ProviderError):
                await self._failed(
                    route,
                    config,
                    effort,
                    prompt.version,
                    outcome,
                    at=at,
                    user_id=user_id,
                    batch=True,
                )
                routed.append(outcome)
                continue
            assert isinstance(outcome.output, StyleProfileOutput)
            typed = StructuredResult(
                output=outcome.output,
                usage=outcome.usage,
                model=outcome.model,
                batch=outcome.batch,
                fallback_used=outcome.fallback_used,
            )
            routed.append(
                await self._finish(
                    route, config, prompt.version, typed, latency_ms, at=at, user_id=user_id
                )
            )
        return routed

    async def extract_commitments(
        self, req: CommitmentRequest, *, user_id: uuid.UUID | None = None
    ) -> RoutedResult[list[CommitmentItem]]:
        result = await self._call(
            "extract.commitments", req, CommitmentExtraction, None, user_id=user_id
        )
        return RoutedResult(
            output=list(result.output.items),
            route=result.route,
            provider=result.provider,
            model=result.model,
            prompt_version=result.prompt_version,
            usage=result.usage,
            cost_usd=result.cost_usd,
            batch=result.batch,
        )

    async def extract_deadlines(
        self, req: DeadlineRequest, *, user_id: uuid.UUID | None = None
    ) -> RoutedResult[list[DeadlineItem]]:
        result = await self._call(
            "extract.deadlines", req, DeadlineExtraction, None, user_id=user_id
        )
        return RoutedResult(
            output=list(result.output.items),
            route=result.route,
            provider=result.provider,
            model=result.model,
            prompt_version=result.prompt_version,
            usage=result.usage,
            cost_usd=result.cost_usd,
            batch=result.batch,
        )

    async def style_judge(self, req: StyleJudgeRequest) -> RoutedResult[StyleJudgeOutput]:
        return await self._call("eval.style_judge", req, StyleJudgeOutput, None)

    # --- internals ------------------------------------------------------------------------------

    async def _call[T: BaseModel](
        self,
        route: RouteName,
        request: LLMRequest,
        schema: type[T],
        tier: Tier | None,
        *,
        cache_prefix: bool = False,
        lighter: bool = False,
        user_id: uuid.UUID | None = None,
    ) -> RoutedResult[T]:
        config = self.routes.resolve(route, tier)
        if lighter:
            config = config.model_copy(update={"effort": lighter_effort(config.effort)})
        adapter = self.adapters[config.provider]
        prompt = self.prompts.get(PROMPT_FOR_ROUTE[route])
        effort = effort_sent(adapter.capabilities, config.effort)
        at = self.clock()
        started = time.monotonic()
        try:
            result = await adapter.complete_structured(
                model=config.model,
                system=prompt.text,
                user_blocks=request.to_blocks(),
                schema=schema,
                effort=effort,
                max_tokens=config.max_tokens,
                cache_prefix=cache_prefix and adapter.capabilities.prompt_caching,
            )
        except ProviderError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            await self._failed(
                route,
                config,
                effort,
                prompt.version,
                exc,
                tier,
                at=at,
                user_id=user_id,
                latency_ms=latency_ms,
            )
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        return await self._finish(
            route, config, prompt.version, result, latency_ms, tier, at=at, user_id=user_id
        )

    async def _finish[T: BaseModel](
        self,
        route: RouteName,
        config: RouteConfig,
        prompt_version: str,
        result: StructuredResult[T],
        latency_ms: int,
        tier: Tier | None = None,
        *,
        at: datetime,
        user_id: uuid.UUID | None = None,
    ) -> RoutedResult[T]:
        provider, requested_model = config.provider, config.model
        priced_model = result.model if self.prices.has(result.model) else requested_model
        cost = self.prices.cost_usd(priced_model, result.usage, batch=result.batch, at=at)
        adapter = self.adapters.get(provider)
        sent = effort_sent(adapter.capabilities, config.effort) if adapter is not None else None
        log.info(
            "llm_call",
            route=route,
            user_id=str(user_id) if user_id is not None else None,
            tier=tier,
            provider=provider,
            model=result.model,
            requested_model=requested_model,
            prompt_version=prompt_version,
            effort=sent,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            reasoning_tokens=result.usage.reasoning_tokens,
            cached_input_tokens=result.usage.cached_input_tokens,
            cache_write_input_tokens=result.usage.cache_write_input_tokens,
            cache_write_1h_input_tokens=result.usage.cache_write_1h_input_tokens,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            cache_miss_tokens=result.usage.cache_miss_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            batch=result.batch,
            fallback_used=result.fallback_used,
            decision="ok",
        )
        await self._record_spend(route, user_id, at, result.usage, cost)
        return RoutedResult(
            output=result.output,
            route=route,
            provider=provider,
            model=result.model,
            prompt_version=prompt_version,
            usage=result.usage,
            cost_usd=cost,
            batch=result.batch,
        )

    async def _failed(
        self,
        route: RouteName,
        config: RouteConfig,
        effort: Effort | None,
        prompt_version: str,
        exc: ProviderError,
        tier: Tier | None = None,
        *,
        at: datetime,
        user_id: uuid.UUID | None = None,
        latency_ms: int | None = None,
        batch: bool = False,
    ) -> None:
        """The failed call's line: error class and message (never content), plus the token
        usage, its cost and the finish reason the provider reported for a truncated or invalid
        answer. A failure with reported usage was billed, so it is recorded as spend."""
        fields: dict[str, object] = {}
        billed: tuple[Usage, float] | None = None
        if isinstance(exc, ProviderOutputInvalid):
            fields["truncated"] = exc.truncated
            if exc.finish_reason is not None:
                fields["finish_reason"] = exc.finish_reason
            if exc.usage is not None:
                usage = exc.usage
                cost = (
                    self.prices.cost_usd(config.model, usage, batch=batch, at=at)
                    if self.prices.has(config.model)
                    else None
                )
                fields.update(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    cache_write_input_tokens=usage.cache_write_input_tokens,
                    cache_write_1h_input_tokens=usage.cache_write_1h_input_tokens,
                    cache_hit_tokens=usage.cache_hit_tokens,
                    cache_miss_tokens=usage.cache_miss_tokens,
                    cost_usd=cost,
                )
                if cost is not None:
                    billed = (usage, cost)
                if latency_ms is None and usage.latency_ms:
                    latency_ms = usage.latency_ms
        log.warning(
            "llm_call",
            route=route,
            user_id=str(user_id) if user_id is not None else None,
            tier=tier,
            provider=config.provider,
            model=config.model,
            prompt_version=prompt_version,
            effort=effort,
            max_tokens=config.max_tokens,
            latency_ms=latency_ms,
            batch=batch,
            decision=type(exc).__name__,
            error=str(exc),
            **fields,
        )
        if billed is not None:
            await self._record_spend(route, user_id, at, *billed, failed=True)

    async def _record_spend(
        self,
        route: RouteName,
        user_id: uuid.UUID | None,
        at: datetime,
        usage: Usage,
        cost: float,
        *,
        failed: bool = False,
    ) -> None:
        """Hand the call to the spend sink; a storage problem never fails the LLM call."""
        if self.spend_sink is None or user_id is None:
            return
        entry = SpendEntry(
            user_id=user_id, route=route, at=at, usage=usage, cost_usd=cost, failed=failed
        )
        try:
            await self.spend_sink(entry)
        except Exception as exc:
            log.warning(
                "llm_spend_not_recorded",
                route=route,
                user_id=str(user_id),
                error=type(exc).__name__,
            )


# --- adapter registry ---------------------------------------------------------------------------

AdapterFactory = Callable[[ProviderConfig, Mapping[str, str]], ProviderAdapter]


def _env_value(environ: Mapping[str, str], name: str | None, *, required: bool) -> str | None:
    if name is None:
        return None
    value = environ.get(name) or None
    if required and value is None:
        raise RouteTableError(f"environment variable {name} is not set")
    return value


def _anthropic_factory(config: ProviderConfig, environ: Mapping[str, str]) -> ProviderAdapter:
    import anthropic

    from scribe.llm.adapters.anthropic import AnthropicAdapter

    api_key = _env_value(environ, config.api_key_env, required=True)
    base_url = _env_value(environ, config.base_url_env, required=False)
    return AnthropicAdapter(
        anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url), cache_ttl=config.cache_ttl
    )


def _openai_compat_factory(config: ProviderConfig, environ: Mapping[str, str]) -> ProviderAdapter:
    import openai

    from scribe.llm.adapters.openai_compat import OpenAICompatAdapter

    api_key = _env_value(environ, config.api_key_env, required=True)
    base_url = _env_value(environ, config.base_url_env, required=False)
    return OpenAICompatAdapter(
        openai.AsyncOpenAI(api_key=api_key, base_url=base_url),
        native_json_schema=config.native_json_schema,
        reasoning_effort=config.reasoning_effort,
        disable_thinking=config.disable_thinking,
        max_tokens_param=config.max_tokens_param,
    )


def _fake_factory(config: ProviderConfig, environ: Mapping[str, str]) -> ProviderAdapter:
    return FakeAdapter()


ADAPTER_FACTORIES: dict[str, AdapterFactory] = {
    "anthropic": _anthropic_factory,
    "openai_compat": _openai_compat_factory,
    "fake": _fake_factory,
}
KNOWN_PROVIDERS = frozenset(ADAPTER_FACTORIES)


def build_adapters(table: RouteTable, environ: Mapping[str, str]) -> dict[str, ProviderAdapter]:
    """One adapter per provider that some route uses (API keys from ``environ``)."""
    used = sorted({config.provider for _, _, config in table.resolved()})
    adapters: dict[str, ProviderAdapter] = {}
    for name in used:
        factory = ADAPTER_FACTORIES.get(name)
        if factory is None:
            raise RouteTableError(f"unknown provider {name!r}")
        adapters[name] = factory(table.providers[name], environ)
    no_off = [
        route if tier is None else f"{route}[{tier}]"
        for route, tier, config in table.resolved()
        if config.effort == "none" and not adapters[config.provider].capabilities.disable_thinking
    ]
    if no_off:
        raise RouteTableError(
            f"{', '.join(no_off)}: effort 'none' (thinking off) needs an openai_compat provider"
            " with disable_thinking: true"
        )
    return adapters
