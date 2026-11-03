"""Route table loading, tiers, env overrides and fail-fast validation (T013)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scribe.llm.pricing import ModelPrice, PriceTable, load_price_table
from scribe.llm.provider import ROUTE_NAMES
from scribe.llm.routes import (
    RouteTableError,
    TieredRoute,
    apply_env_overrides,
    lighter_effort,
    load_route_table,
    parse_route_table,
    validate_route_table,
)

CONFIG = Path(__file__).resolve().parents[2] / "config"
KNOWN = {"anthropic", "openai_compat", "fake"}


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((CONFIG / "llm_routes.yml").read_text())
    return raw


def _prices() -> PriceTable:
    return load_price_table(CONFIG / "llm_prices.yml")


def test_shipped_config_is_valid() -> None:
    table = load_route_table(CONFIG / "llm_routes.yml", environ={})
    validate_route_table(table, _prices(), KNOWN)
    assert set(table.routes) == set(ROUTE_NAMES)
    assert isinstance(table.routes["compose.draft"], TieredRoute)
    assert table.resolve("profile.build").prefer_batch is True


def test_deepseek_table_needs_the_operator_attestation() -> None:
    path = CONFIG / "llm_routes.deepseek.yml"
    with pytest.raises(RouteTableError, match="data_use_confirmed"):
        validate_route_table(load_route_table(path, environ={}), _prices(), KNOWN)
    attested = {"SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__DATA_USE_CONFIRMED": "true"}
    table = load_route_table(path, environ=attested)
    validate_route_table(table, _prices(), KNOWN)
    assert set(table.routes) == set(ROUTE_NAMES)
    provider = table.providers["openai_compat"]
    assert provider.native_json_schema is False and provider.reasoning_effort is True
    assert provider.disable_thinking is True
    assert provider.max_tokens_param == "max_tokens"  # DeepSeek ignores max_completion_tokens


def test_route_table_overrides_and_tiers() -> None:
    table = parse_route_table(_raw(), environ={})
    # Tier resolution for compose.draft; other routes ignore the tier.
    assert table.resolve("compose.draft", "quality").model == "claude-opus-5-5"
    assert table.resolve("compose.draft", "economy").model == "claude-sonnet-5-5"
    assert table.resolve("compose.draft").model == "claude-opus-5-5"  # default tier
    assert table.resolve("retrieve.filter_context", "economy").model == "claude-haiku-5-5"

    environ = {
        "SCRIBE_LLM__COMPOSE_DRAFT__ECONOMY__MODEL": "claude-haiku-5-5",
        "SCRIBE_LLM__COMPOSE_DRAFT__QUALITY__MAX_TOKENS": "6000",
        "SCRIBE_LLM__EXTRACT_COMMITMENTS__PROVIDER": "openai_compat",
        "SCRIBE_LLM__EXTRACT_COMMITMENTS__MODEL": "openai-compat-small",
        "SCRIBE_LLM__RETRIEVE_FILTER_CONTEXT__EFFORT": "medium",
        "SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__DATA_USE_CONFIRMED": "true",
        "UNRELATED_VARIABLE": "ignored",
    }
    table = parse_route_table(_raw(), environ=environ)
    assert table.resolve("compose.draft", "economy").model == "claude-haiku-5-5"
    assert table.resolve("compose.draft", "quality").max_tokens == 6000
    assert table.resolve("compose.draft", "quality").model == "claude-opus-5-5"
    commitments = table.resolve("extract.commitments")
    assert (commitments.provider, commitments.model) == ("openai_compat", "openai-compat-small")
    assert table.resolve("retrieve.filter_context").effort == "medium"
    assert table.providers["openai_compat"].data_use_confirmed is True
    validate_route_table(table, _prices(), KNOWN)

    # Explicit "tiers" segment works too; the source mapping is not mutated.
    raw = _raw()
    data = apply_env_overrides(raw, {"SCRIBE_LLM__COMPOSE_DRAFT__TIERS__ECONOMY__EFFORT": "low"})
    assert data["routes"]["compose.draft"]["tiers"]["economy"]["effort"] == "low"
    assert raw["routes"]["compose.draft"]["tiers"]["economy"]["effort"] == "medium"


@pytest.mark.parametrize(
    "key",
    ["SCRIBE_LLM__NO_SUCH_ROUTE__MODEL", "SCRIBE_LLM__COMPOSE_DRAFT", "SCRIBE_LLM__PROVIDERS__X"],
)
def test_malformed_override_fails(key: str) -> None:
    with pytest.raises(RouteTableError):
        parse_route_table(_raw(), environ={key: "x"})


def test_invalid_values_fail() -> None:
    with pytest.raises(RouteTableError):
        parse_route_table(_raw(), environ={"SCRIBE_LLM__PROFILE_BUILD__EFFORT": "extreme"})
    with pytest.raises(RouteTableError):
        parse_route_table(_raw(), environ={"SCRIBE_LLM__PROFILE_BUILD__MAX_TOKENS": "0"})
    raw = _raw()
    del raw["routes"]["eval.style_judge"]
    with pytest.raises(RouteTableError, match="missing routes"):
        parse_route_table(raw, environ={})
    raw = _raw()
    del raw["routes"]["compose.draft"]["tiers"]["quality"]
    with pytest.raises(RouteTableError):
        parse_route_table(raw, environ={})


def test_unknown_provider_fails_startup() -> None:
    table = parse_route_table(_raw(), environ={"SCRIBE_LLM__EVAL_STYLE_JUDGE__PROVIDER": "nope"})
    with pytest.raises(RouteTableError, match="unknown provider 'nope'"):
        validate_route_table(table, _prices(), KNOWN)


def test_unpriced_model_fails_startup() -> None:
    table = parse_route_table(
        _raw(), environ={"SCRIBE_LLM__COMPOSE_DRAFT__ECONOMY__MODEL": "claude-unpriced"}
    )
    with pytest.raises(RouteTableError, match=r"compose\.draft\[economy\].*claude-unpriced"):
        validate_route_table(table, _prices(), KNOWN)


def test_provider_without_data_use_confirmation_fails_startup() -> None:
    table = parse_route_table(
        _raw(),
        environ={
            "SCRIBE_LLM__EXTRACT_COMMITMENTS__PROVIDER": "openai_compat",
            "SCRIBE_LLM__EXTRACT_COMMITMENTS__MODEL": "openai-compat-small",
        },
    )
    with pytest.raises(RouteTableError, match="data_use_confirmed"):
        validate_route_table(table, _prices(), KNOWN)
    # An unused unconfirmed provider is fine (openai_compat ships unconfirmed).
    validate_route_table(parse_route_table(_raw(), environ={}), _prices(), KNOWN)


def test_provider_known_to_table_but_not_registered_fails() -> None:
    table = parse_route_table(_raw(), environ={})
    with pytest.raises(RouteTableError, match="unknown provider 'anthropic'"):
        validate_route_table(table, _prices(), {"fake"})


def test_missing_file_is_route_table_error(tmp_path: Path) -> None:
    with pytest.raises(RouteTableError):
        load_route_table(tmp_path / "missing.yml", environ={})


def test_prices_cover_shipped_claude_models() -> None:
    prices = _prices()
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        assert isinstance(prices.models[model], ModelPrice)


@pytest.mark.parametrize(
    ("effort", "lighter"),
    [("max", "xhigh"), ("xhigh", "high"), ("high", "medium"), ("medium", "low"), ("low", "low")],
)
def test_lighter_effort_steps_down_once(effort: Any, lighter: Any) -> None:
    assert lighter_effort(effort) == lighter


def test_lighter_effort_keeps_provider_default() -> None:
    assert lighter_effort(None) is None


def test_lighter_effort_keeps_thinking_off() -> None:
    assert lighter_effort("none") == "none"


def test_effort_none_parses_and_disable_thinking_is_off_by_default() -> None:
    table = parse_route_table(_raw(), environ={"SCRIBE_LLM__CLASSIFY_REPLY_NEEDED__EFFORT": "none"})
    assert table.resolve("classify.reply_needed").effort == "none"
    assert table.providers["openai_compat"].disable_thinking is False
    with pytest.raises(RouteTableError, match="effort"):
        parse_route_table(_raw(), environ={"SCRIBE_LLM__CLASSIFY_REPLY_NEEDED__EFFORT": "off"})


def test_openai_compat_reasoning_effort_is_off_by_default() -> None:
    table = parse_route_table(_raw(), environ={})
    assert table.providers["openai_compat"].reasoning_effort is False
    table = parse_route_table(
        _raw(), environ={"SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__REASONING_EFFORT": "true"}
    )
    assert table.providers["openai_compat"].reasoning_effort is True


def test_anthropic_cache_ttl_defaults_to_one_hour_and_accepts_only_known_values() -> None:
    table = parse_route_table(_raw(), environ={})
    assert table.providers["anthropic"].cache_ttl == "1h"
    key = "SCRIBE_LLM__PROVIDERS__ANTHROPIC__CACHE_TTL"
    assert parse_route_table(_raw(), environ={key: "5m"}).providers["anthropic"].cache_ttl == "5m"
    with pytest.raises(RouteTableError):
        parse_route_table(_raw(), environ={key: "24h"})


def test_openai_compat_max_tokens_param_defaults_and_validates() -> None:
    table = parse_route_table(_raw(), environ={})
    assert table.providers["openai_compat"].max_tokens_param == "max_completion_tokens"
    table = parse_route_table(
        _raw(), environ={"SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__MAX_TOKENS_PARAM": "max_tokens"}
    )
    assert table.providers["openai_compat"].max_tokens_param == "max_tokens"
    bad = {"SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__MAX_TOKENS_PARAM": "num_predict"}
    with pytest.raises(RouteTableError):
        parse_route_table(_raw(), environ=bad)
