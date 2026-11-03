"""Route table: which provider, model, effort and token limit serve each logical LLM route.

Loaded from ``config/llm_routes.yml``; any leaf can be overridden with an environment variable
``SCRIBE_LLM__<ROUTE>__<FIELD>`` where ``<ROUTE>`` is the route name upper-cased with ``.``
replaced by ``_`` (``COMPOSE_DRAFT__ECONOMY__MODEL``, ``EXTRACT_COMMITMENTS__PROVIDER``), or
``SCRIBE_LLM__PROVIDERS__<NAME>__<FIELD>`` for provider settings. Values are parsed as YAML
scalars (``true``, ``4000``). :func:`validate_route_table` is the fail-fast startup check.
:func:`lighter_effort` is the one-step-lower effort of a lighter retry (``profile.build``).
"""

from __future__ import annotations

import os
from collections.abc import Collection, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scribe.llm.pricing import PriceTable
from scribe.llm.provider import ROUTE_NAMES, Effort, MaxTokensParam, RouteName, Tier

ENV_PREFIX = "SCRIBE_LLM__"
DEFAULT_TIER: Tier = "quality"
EFFORT_LADDER: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")


def lighter_effort(effort: Effort | None) -> Effort | None:
    """One step down the ladder (max → xhigh → high → medium → low); ``low``, ``none``
    (thinking off) and None (the provider default) stay as they are."""
    if effort is None or effort == "none":
        return effort
    return EFFORT_LADDER[max(EFFORT_LADDER.index(effort) - 1, 0)]


class RouteTableError(ValueError):
    pass


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    effort: Effort | None = None
    max_tokens: int = Field(gt=0)
    prefer_batch: bool = False


class TieredRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: dict[Tier, RouteConfig]

    @model_validator(mode="after")
    def _has_default_tier(self) -> Self:
        if DEFAULT_TIER not in self.tiers:
            raise ValueError(f"tiered route needs a {DEFAULT_TIER!r} tier")
        return self


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_env: str | None = None
    base_url_env: str | None = None
    data_use_confirmed: bool = False
    # openai_compat only: server supports response_format json_schema (else schema-in-prompt).
    native_json_schema: bool = True
    # openai_compat only: send the route's effort as ``reasoning_effort``. Off: it is never
    # sent, so a route's ``effort`` has no effect on that server.
    reasoning_effort: bool = False
    # anthropic only: TTL of the prompt-cache breakpoints. 1h writes cost 2x input and outlive
    # the gaps between one user's drafts; 5m writes cost 1.25x (fallback for steady traffic).
    cache_ttl: Literal["5m", "1h"] = "1h"
    # openai_compat only: the server takes DeepSeek's ``thinking: {type: disabled}`` body field,
    # sent for a route with ``effort: none`` (thinking off). Off: a route with ``effort: none``
    # on this provider is refused at startup (``registry.build_adapters``).
    disable_thinking: bool = False
    # openai_compat only: the request field that carries a route's ``max_tokens``. OpenAI wants
    # ``max_completion_tokens`` (``max_tokens`` is deprecated there); DeepSeek documents only
    # ``max_tokens`` and would otherwise apply its own default limit.
    max_tokens_param: MaxTokensParam = "max_completion_tokens"


class RouteTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routes: dict[RouteName, RouteConfig | TieredRoute]
    providers: dict[str, ProviderConfig]

    @model_validator(mode="after")
    def _all_routes_present(self) -> Self:
        missing = [name for name in ROUTE_NAMES if name not in self.routes]
        if missing:
            raise ValueError(f"route table is missing routes: {', '.join(missing)}")
        return self

    def resolve(self, route: RouteName, tier: Tier | None = None) -> RouteConfig:
        """Config for ``route``; ``tier`` selects within a tiered route (default ``quality``)."""
        entry = self.routes[route]
        if isinstance(entry, TieredRoute):
            return entry.tiers.get(tier or DEFAULT_TIER, entry.tiers[DEFAULT_TIER])
        return entry

    def resolved(self) -> Iterator[tuple[RouteName, Tier | None, RouteConfig]]:
        for route, entry in self.routes.items():
            if isinstance(entry, TieredRoute):
                for tier, config in entry.tiers.items():
                    yield route, tier, config
            else:
                yield route, None, entry


def _env_route_key(route: str) -> str:
    return route.replace(".", "_").upper()


def apply_env_overrides(raw: dict[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    """Return a copy of the raw YAML mapping with every ``SCRIBE_LLM__*`` override applied."""
    data: dict[str, Any] = yaml.safe_load(yaml.safe_dump(raw)) or {}
    routes_by_env = {_env_route_key(name): name for name in ROUTE_NAMES}
    for key in sorted(environ):
        if not key.upper().startswith(ENV_PREFIX):
            continue
        parts = [p for p in key[len(ENV_PREFIX) :].split("__")]
        if len(parts) < 2 or not all(parts):
            raise RouteTableError(f"{key}: expected {ENV_PREFIX}<ROUTE>__<FIELD>")
        head = parts[0].upper()
        if head == "PROVIDERS":
            if len(parts) < 3:
                raise RouteTableError(f"{key}: expected {ENV_PREFIX}PROVIDERS__<NAME>__<FIELD>")
            path = ["providers", *[p.lower() for p in parts[1:]]]
        elif head in routes_by_env:
            route = routes_by_env[head]
            rest = [p.lower() for p in parts[1:]]
            node = data.get("routes", {}).get(route)
            if isinstance(node, dict) and "tiers" in node and rest[0] != "tiers":
                rest = ["tiers", *rest]
            path = ["routes", route, *rest]
        else:
            raise RouteTableError(f"{key}: unknown route {parts[0]!r}")
        _set_path(data, path, yaml.safe_load(environ[key]))
    return data


def _set_path(data: dict[str, Any], path: list[str], value: Any) -> None:
    node = data
    for part in path[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise RouteTableError(f"cannot override below non-mapping {'.'.join(path)}")
        node = child
    node[path[-1]] = value


def parse_route_table(raw: dict[str, Any], environ: Mapping[str, str] | None = None) -> RouteTable:
    data = apply_env_overrides(raw, os.environ if environ is None else environ)
    try:
        return RouteTable.model_validate(data)
    except ValidationError as exc:
        raise RouteTableError(f"invalid route table: {exc}") from exc


def load_route_table(path: Path, environ: Mapping[str, str] | None = None) -> RouteTable:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RouteTableError(f"cannot read route table {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RouteTableError(f"route table {path} must be a mapping")
    return parse_route_table(raw, environ)


def validate_route_table(
    table: RouteTable, prices: PriceTable, known_providers: Collection[str]
) -> None:
    """Fail fast: unknown provider, unpriced model, or provider without data-use attestation."""
    errors: list[str] = []
    for route, tier, config in table.resolved():
        where = route if tier is None else f"{route}[{tier}]"
        provider = table.providers.get(config.provider)
        if config.provider not in known_providers or provider is None:
            errors.append(f"{where}: unknown provider {config.provider!r}")
            continue
        if not prices.has(config.model):
            errors.append(f"{where}: model {config.model!r} missing from the price table")
        if not provider.data_use_confirmed:
            errors.append(
                f"{where}: provider {config.provider!r} lacks data_use_confirmed: true"
                " (operator must confirm it does not train on submitted data)"
            )
    if errors:
        raise RouteTableError("; ".join(errors))
