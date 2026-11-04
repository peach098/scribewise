"""LLMRouter: route resolution, prompt loading, filter sanitising, batching, logging (T014)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scribe.llm.adapters.anthropic import AnthropicAdapter
from scribe.llm.adapters.fake import FakeAdapter, FakeResponseMissing, fingerprint_request
from scribe.llm.adapters.openai_compat import OpenAICompatAdapter
from scribe.llm.provider import (
    Capabilities,
    CommitmentExtraction,
    CommitmentItem,
    CommitmentRef,
    CommitmentRequest,
    ContactInfo,
    DraftOutput,
    DraftRequest,
    FilterCandidates,
    FilterContextOutput,
    FilterContextRequest,
    HistoryItem,
    MessageItem,
    ProfileContext,
    ProviderAdapter,
    ProviderOutputInvalid,
    ProviderUnavailable,
    ReplyNeededOutput,
    ReplyNeededRequest,
    StyleJudgeOutput,
    StyleJudgeRequest,
    StyleProfileOutput,
    StyleProfileRequest,
    StyleRules,
    StyleSample,
    TaskItem,
    Usage,
)
from scribe.llm.registry import (
    FILTER_MAX_EARLIER,
    KNOWN_PROVIDERS,
    LLMRouter,
    PromptError,
    PromptStore,
    build_adapters,
    effort_sent,
    parse_prompt,
    sanitize_filter_output,
)
from scribe.llm.routes import RouteTable, RouteTableError, load_route_table
from tests.conftest import LogCapture

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
SECRET = "SYNTHETIC-CONFIDENTIAL-CONTENT"


def _draft_output(text: str = "Synthetic reply") -> DraftOutput:
    return DraftOutput.model_validate(
        {
            "text": text,
            "language": "en",
            "needs_info": False,
            "placeholders": [],
            "sources": [{"kind": "message", "id": "msg:1", "used_for": "request"}],
            "flags": {
                "creates_commitment": {"value": False, "what": None, "due": None},
                "sensitive": {"value": False, "categories": []},
            },
            "conflicts": [],
            "tone_shift_applied": "none",
            "context_warnings": [],
        }
    )


def _draft_request(incoming: str = SECRET) -> DraftRequest:
    return DraftRequest(
        incoming_messages=[MessageItem(id="msg:1", sender="Colleague", text=incoming)],
        contact=ContactInfo(display_name="Colleague", kind="person"),
        profile=ProfileContext(rules_json={"formality": 2}, examples=["Synthetic example"]),
    )


def _rules() -> StyleRules:
    return StyleRules(
        languages=["en"],
        formality=2,
        typical_length_words=12,
        greetings=[],
        sign_offs=[],
        emoji_usage="rare",
        punctuation_habits=[],
        typical_phrases=[],
        never_does=[],
    )


def _profile_request(n: int = 1) -> StyleProfileRequest:
    return StyleProfileRequest(
        samples=[StyleSample(text=f"synthetic sample {n}", sent_at=T0)], scope="global"
    )


def test_adapters_satisfy_protocol(fake_llm: FakeAdapter) -> None:
    adapters: list[ProviderAdapter] = [fake_llm]
    assert adapters[0].name == "fake"


async def test_draft_reply_resolves_tier_and_sends_prompt_as_system(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("compose.draft", _draft_output())
    quality = await llm_router.draft_reply(_draft_request(), user_tier="quality")
    economy = await llm_router.draft_reply(_draft_request(), user_tier="economy")

    assert (quality.model, economy.model) == ("fake-large", "fake-small")
    assert quality.output.text == "Synthetic reply"
    assert quality.prompt_version == "1" and quality.provider == "fake"
    assert quality.cost_usd > 0
    assert quality.usage_record()["model"] == "fake-large"

    first = fake_llm.calls_for("compose.draft")[0]
    assert first.system == "Draft a reply in the user's style."
    assert SECRET not in first.system
    assert first.cache_prefix is True and first.effort == "medium"
    assert first.user_blocks[0].name == "profile" and first.user_blocks[0].stable
    assert any(SECRET in block.content for block in first.user_blocks)


async def test_responses_keyed_by_request_fingerprint(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    special = _draft_request("special synthetic question")
    fake_llm.set_response("compose.draft", _draft_output("generic"))
    fake_llm.set_response("compose.draft", _draft_output("specific"), request=special)
    assert (await llm_router.draft_reply(special, user_tier="quality")).output.text == "specific"
    other = await llm_router.draft_reply(_draft_request(), user_tier="quality")
    assert other.output.text == "generic"
    assert fake_llm.calls[0].fingerprint == fingerprint_request(special)


async def test_unregistered_fake_call_fails_loudly(llm_router: LLMRouter) -> None:
    with pytest.raises(FakeResponseMissing):
        await llm_router.style_judge(
            StyleJudgeRequest(draft_text="x", profile_summary="y", reference_examples=[])
        )


async def test_filter_context_keeps_only_offered_ids_and_caps_history(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    history = [
        HistoryItem(id=f"msg:{i}", is_user=False, sender="C", text="synthetic", sent_at=T0)
        for i in range(20)
    ]
    request = FilterContextRequest(
        incoming_messages=[MessageItem(id="msg:in", sender="C", text="synthetic")],
        candidates=FilterCandidates(
            history=history,
            tasks=[TaskItem(id="task:1", title="Synthetic task", status="Doing")],
            commitments=[CommitmentRef(id="commit:1", what="synthetic")],
        ),
    )
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(
            history_ids=["msg:invented", "msg:3", "msg:3"] + [f"msg:{i}" for i in range(20)],
            task_ids=["task:1", "task:404"],
            commitment_ids=["commit:999"],
        ),
    )
    result = await llm_router.filter_context(request, user_tier="economy")
    assert result.model == "fake-small"
    assert result.output.history_ids[0] == "msg:3"
    assert "msg:invented" not in result.output.history_ids
    assert len(result.output.history_ids) == 15
    assert len(set(result.output.history_ids)) == 15
    assert result.output.task_ids == ["task:1"]
    assert result.output.commitment_ids == []
    # Issue #69: the filter's system prompt is the same for every user, so it is cached.
    (call,) = fake_llm.calls_for("retrieve.filter_context")
    assert call.cache_prefix is True


async def test_build_style_profile_sync_and_batch(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    output = StyleProfileOutput(
        rules_json=_rules(), summary_text="Short.", chosen_example_indices=[0]
    )
    fake_llm.set_response("profile.build", output)

    sync = await llm_router.build_style_profile(_profile_request())
    batched = await llm_router.build_style_profile(_profile_request(), batch=True)
    assert sync.batch is False and batched.batch is True
    assert batched.cost_usd == pytest.approx(sync.cost_usd / 2, abs=1e-6)
    assert [c.batch for c in fake_llm.calls_for("profile.build")] == [False, True]

    results = await llm_router.build_style_profiles([_profile_request(1), _profile_request(2)])
    assert all(not isinstance(r, Exception) and r.batch for r in results)


async def test_build_style_profiles_without_batch_capability_runs_sync(
    prompts_dir: Path,
) -> None:
    from tests.conftest import fake_price_table, fake_route_table

    no_batch = FakeAdapter(
        capabilities=Capabilities(
            structured_outputs=True, prompt_caching=False, batch=False, reasoning_effort=False
        )
    )
    no_batch.set_response(
        "profile.build",
        StyleProfileOutput(rules_json=_rules(), summary_text="s", chosen_example_indices=[]),
    )
    router = LLMRouter(
        routes=fake_route_table(),
        prices=fake_price_table(),
        adapters={"fake": no_batch},
        prompts=PromptStore(prompts_dir),
    )
    results = await router.build_style_profiles([_profile_request(1), _profile_request(2)])
    assert len(results) == 2
    assert [c.batch for c in no_batch.calls] == [False, False]
    assert all(c.effort is None and c.cache_prefix is False for c in no_batch.calls)


async def test_extract_commitments_returns_items(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    item = CommitmentItem(
        what="send the deck", to_whom="Colleague", due=None, confidence=0.9, evidence="I'll send"
    )
    fake_llm.set_response("extract.commitments", CommitmentExtraction(items=[item]))
    result = await llm_router.extract_commitments(
        CommitmentRequest(outgoing_text="I'll send", contact_display_name="Colleague", sent_at=T0)
    )
    assert result.output == [item]
    assert result.route == "extract.commitments"


async def test_classify_reply_needed_routed(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    output = ReplyNeededOutput(
        reply_needed=False,
        category="thanks",
        confidence=0.93,
        resolves_pending=False,
        decided_by="default",
    )
    fake_llm.set_response("classify.reply_needed", output)
    request = ReplyNeededRequest(
        incoming_messages=[MessageItem(id="msg:1", sender="Colleague", text="thanks!")],
        contact=ContactInfo(display_name="Colleague", kind="person"),
    )
    result = await llm_router.classify_reply_needed(request)
    assert result.output == output
    assert (result.route, result.model, result.prompt_version) == (
        "classify.reply_needed",
        "fake-small",
        "1",
    )
    (call,) = fake_llm.calls_for("classify.reply_needed")
    assert call.effort == "low" and call.cache_prefix is False
    assert [b.name for b in call.user_blocks] == [
        "user_rules",  # specs/013: the user's own rules ({} when none)
        "incoming_messages",
        "history",
        "contact",
        "pending_draft",
    ]
    assert all(b.kind == "data" for b in call.user_blocks)

    fake_llm.set_response("classify.reply_needed", ProviderUnavailable("synthetic outage"))
    with pytest.raises(ProviderUnavailable):
        await llm_router.classify_reply_needed(request)


async def test_style_judge(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    fake_llm.set_response("eval.style_judge", StyleJudgeOutput(score=0.85, reasons=["ok"]))
    result = await llm_router.style_judge(
        StyleJudgeRequest(draft_text="x", profile_summary="y", reference_examples=["z"])
    )
    assert result.output.score == 0.85


async def test_logs_metadata_never_content(
    llm_router: LLMRouter, fake_llm: FakeAdapter, log_capture: LogCapture
) -> None:
    fake_llm.set_response("compose.draft", _draft_output(f"reply mentioning {SECRET}"))
    await llm_router.draft_reply(_draft_request(), user_tier="quality")
    fake_llm.set_response("compose.draft", ProviderUnavailable("synthetic outage"))
    with pytest.raises(ProviderUnavailable):
        await llm_router.draft_reply(_draft_request(), user_tier="quality")

    ok, failed = log_capture.events("llm_call")
    for field in ("route", "provider", "model", "prompt_version", "cost_usd", "latency_ms"):
        assert field in ok
    assert ok["route"] == "compose.draft" and ok["decision"] == "ok"
    assert failed["decision"] == "ProviderUnavailable" and failed["level"] == "warning"
    assert SECRET not in log_capture.raw


def test_prompt_parsing(tmp_path: Path) -> None:
    prompt = parse_prompt("draft", "---\nversion: 3\n---\nSystem text.\n")
    assert (prompt.version, prompt.text) == ("3", "System text.")
    for raw in ("No front matter", "---\nauthor: x\n---\nbody", "---\nversion: 1\n---\n  \n"):
        with pytest.raises(PromptError):
            parse_prompt("draft", raw)
    with pytest.raises(PromptError):
        PromptStore(tmp_path).get("missing")


_CACHE_PARAMS: dict[str, Any] = {
    "model": "claude-opus-5-5",
    "system": "Synthetic system prompt.",
    "user_blocks": [],
    "schema": StyleJudgeOutput,
    "effort": None,
    "max_tokens": 100,
    "cache_prefix": True,
}


def test_build_adapters_from_shipped_config() -> None:
    config = Path(__file__).resolve().parents[2] / "config" / "llm_routes.yml"
    table = load_route_table(config, environ={})
    adapters = build_adapters(table, {"ANTHROPIC_API_KEY": "test-key-not-real"})
    assert set(adapters) == {"anthropic"}  # openai_compat is configured but unused
    assert isinstance(adapters["anthropic"], AnthropicAdapter)
    with pytest.raises(RouteTableError, match="ANTHROPIC_API_KEY"):
        build_adapters(table, {})

    # Prompt-cache TTL: 1 hour by default, 5 minutes on the operator's request.
    default = adapters["anthropic"]
    assert isinstance(default, AnthropicAdapter)
    params = default.build_params(**_CACHE_PARAMS)
    assert params["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    short = load_route_table(config, environ={"SCRIBE_LLM__PROVIDERS__ANTHROPIC__CACHE_TTL": "5m"})
    five = build_adapters(short, {"ANTHROPIC_API_KEY": "test-key-not-real"})["anthropic"]
    assert isinstance(five, AnthropicAdapter)
    params = five.build_params(**_CACHE_PARAMS)
    assert params["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    table = load_route_table(
        config,
        environ={
            "SCRIBE_LLM__EVAL_STYLE_JUDGE__PROVIDER": "openai_compat",
            "SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__NATIVE_JSON_SCHEMA": "false",
        },
    )
    adapters = build_adapters(
        table, {"ANTHROPIC_API_KEY": "test-key", "OPENAI_API_KEY": "test-key"}
    )
    compat = adapters["openai_compat"]
    assert isinstance(compat, OpenAICompatAdapter)
    assert compat.capabilities.structured_outputs is False
    assert compat.capabilities.reasoning_effort is False  # effort is not sent unless enabled

    table = load_route_table(
        config,
        environ={
            "SCRIBE_LLM__EVAL_STYLE_JUDGE__PROVIDER": "openai_compat",
            "SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__REASONING_EFFORT": "true",
        },
    )
    compat = build_adapters(table, {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"})[
        "openai_compat"
    ]
    assert compat.capabilities.reasoning_effort is True

    table = load_route_table(
        config,
        environ={
            "SCRIBE_LLM__EVAL_STYLE_JUDGE__PROVIDER": "openai_compat",
            "SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__MAX_TOKENS_PARAM": "max_tokens",
        },
    )
    compat = build_adapters(table, {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"})[
        "openai_compat"
    ]
    assert isinstance(compat, OpenAICompatAdapter)
    assert compat._max_tokens_param == "max_tokens"
    assert {"anthropic", "openai_compat", "fake"} == KNOWN_PROVIDERS


def test_build_adapters_refuses_effort_none_where_thinking_cannot_be_turned_off() -> None:
    """Issue #68: ``effort: none`` needs an openai_compat provider with disable_thinking;
    Anthropic has no such effort, and a server without the flag is not sent a guess."""
    config = Path(__file__).resolve().parents[2] / "config" / "llm_routes.yml"
    keys = {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"}
    on_anthropic = load_route_table(
        config, environ={"SCRIBE_LLM__CLASSIFY_REPLY_NEEDED__EFFORT": "none"}
    )
    with pytest.raises(RouteTableError, match=r"classify\.reply_needed: effort 'none'"):
        build_adapters(on_anthropic, keys)

    compat = {
        "SCRIBE_LLM__COMPOSE_DRAFT__ECONOMY__PROVIDER": "openai_compat",
        "SCRIBE_LLM__COMPOSE_DRAFT__ECONOMY__EFFORT": "none",
    }
    with pytest.raises(RouteTableError, match=r"compose\.draft\[economy\]"):
        build_adapters(load_route_table(config, environ=compat), keys)

    enabled = {**compat, "SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__DISABLE_THINKING": "true"}
    adapters = build_adapters(load_route_table(config, environ=enabled), keys)
    assert adapters["openai_compat"].capabilities.disable_thinking is True
    assert adapters["anthropic"].capabilities.disable_thinking is False


@pytest.mark.parametrize(
    ("reasoning_effort", "disable_thinking", "effort", "sent"),
    [
        (True, True, "none", "none"),
        (True, False, "none", None),
        (False, True, "none", "none"),
        (False, True, "low", None),
        (True, True, "low", "low"),
        (True, True, None, None),
    ],
)
def test_effort_sent(
    reasoning_effort: bool, disable_thinking: bool, effort: Any, sent: Any
) -> None:
    capabilities = Capabilities(
        structured_outputs=True,
        prompt_caching=False,
        batch=False,
        reasoning_effort=reasoning_effort,
        disable_thinking=disable_thinking,
    )
    assert effort_sent(capabilities, effort) == sent


def test_fake_table_is_a_route_table() -> None:
    from tests.conftest import fake_route_table

    assert isinstance(fake_route_table(), RouteTable)


# --- search over earlier conversations (specs/004 T008) ------------------------------------------


def test_filter_output_without_search_fields_is_valid() -> None:
    output = FilterContextOutput.model_validate(
        {"history_ids": [], "task_ids": [], "commitment_ids": []}
    )
    assert output.needs_earlier_context is False
    assert output.search_queries == []


def _filter_request() -> FilterContextRequest:
    return FilterContextRequest(
        incoming_messages=[MessageItem(id="msg:in", sender="C", text="synthetic")],
        candidates=FilterCandidates(),
    )


def test_sanitizer_cleans_search_queries() -> None:
    raw = FilterContextOutput(
        history_ids=[],
        task_ids=[],
        commitment_ids=[],
        needs_earlier_context=True,
        search_queries=["  счёт ", "", "x", "Счёт", "invoice", "a" * 80, "q4", "q5", "q6"],
    )
    clean = sanitize_filter_output(raw, _filter_request(), max_history=15)
    assert clean.needs_earlier_context is True
    assert clean.search_queries == ["счёт", "invoice", "a" * 60, "q4", "q5"]


def test_sanitizer_clears_queries_when_not_needed() -> None:
    raw = FilterContextOutput(
        history_ids=[], task_ids=[], commitment_ids=[], search_queries=["invoice"]
    )
    clean = sanitize_filter_output(raw, _filter_request(), max_history=15)
    assert clean.needs_earlier_context is False
    assert clean.search_queries == []


def test_sanitizer_with_no_queries_left_means_no_search() -> None:
    raw = FilterContextOutput(
        history_ids=[],
        task_ids=[],
        commitment_ids=[],
        needs_earlier_context=True,
        search_queries=["", " "],
    )
    clean = sanitize_filter_output(raw, _filter_request(), max_history=15)
    assert clean.needs_earlier_context is False


async def test_rank_earlier_sends_only_matches_and_caps_selection(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    matches = [
        HistoryItem(id=f"earlier:{i}", is_user=False, sender="C", text="synthetic", sent_at=T0)
        for i in range(1, 15)
    ]
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(
            history_ids=["msg:1", "earlier:3"] + [f"earlier:{i}" for i in range(1, 15)],
            task_ids=["task:1"],
            commitment_ids=["commit:1"],
            needs_earlier_context=True,
            search_queries=["again"],
        ),
    )
    incoming = [MessageItem(id="msg:in", sender="C", text="synthetic")]
    result = await llm_router.rank_earlier(incoming, matches, user_tier="economy")
    assert result.output.history_ids[0] == "earlier:3"
    assert "msg:1" not in result.output.history_ids
    assert len(result.output.history_ids) == FILTER_MAX_EARLIER == 10
    assert result.output.task_ids == [] and result.output.commitment_ids == []
    assert result.output.needs_earlier_context is False and result.output.search_queries == []
    [call] = fake_llm.calls_for("retrieve.filter_context")
    assert call.cache_prefix is True
    payload = "".join(b.content for b in call.user_blocks)
    compact = payload.replace(" ", "")
    assert '"tasks":[]' in compact and '"id":"e14"' in compact  # short ids (#71)
    assert "earlier:" not in payload and "msg:in" not in payload


async def test_failed_call_logs_usage_and_finish_reason_never_content(
    llm_router: LLMRouter, fake_llm: FakeAdapter, log_capture: LogCapture
) -> None:
    """Issue #30: a truncated answer's line has the reported usage and finish reason."""
    truncated = ProviderOutputInvalid(
        "StyleProfileOutput output truncated at max tokens",
        truncated=True,
        usage=Usage(input_tokens=9000, output_tokens=2000, reasoning_tokens=1800),
        finish_reason="length",
    )
    fake_llm.set_response("profile.build", truncated)
    request = StyleProfileRequest(samples=[StyleSample(text=SECRET, sent_at=T0)], scope="global")
    with pytest.raises(ProviderOutputInvalid):
        await llm_router.build_style_profile(request)

    (line,) = log_capture.events("llm_call")
    assert line["decision"] == "ProviderOutputInvalid" and line["level"] == "warning"
    assert line["truncated"] is True and line["finish_reason"] == "length"
    tokens = (line["input_tokens"], line["output_tokens"], line["reasoning_tokens"])
    assert tokens == (9000, 2000, 1800)
    assert line["effort"] == "medium" and line["max_tokens"] == 2000
    assert isinstance(line["latency_ms"], int)
    assert SECRET not in log_capture.raw


async def test_failed_batch_item_logs_usage(
    llm_router: LLMRouter, fake_llm: FakeAdapter, log_capture: LogCapture
) -> None:
    fake_llm.set_response(
        "profile.build",
        ProviderOutputInvalid(
            "truncated", truncated=True, usage=Usage(output_tokens=2000), finish_reason="length"
        ),
    )
    (outcome,) = await llm_router.build_style_profiles([_profile_request()])
    assert isinstance(outcome, ProviderOutputInvalid)
    (line,) = log_capture.events("llm_call")
    assert line["batch"] is True and line["output_tokens"] == 2000
    assert line["finish_reason"] == "length"


async def test_lighter_profile_build_lowers_effort_and_runs_sync(
    llm_router: LLMRouter, fake_llm: FakeAdapter, log_capture: LogCapture
) -> None:
    fake_llm.set_response(
        "profile.build",
        StyleProfileOutput(rules_json=_rules(), summary_text="s", chosen_example_indices=[]),
    )
    await llm_router.build_style_profile(_profile_request())
    await llm_router.build_style_profile(_profile_request(), batch=True, lighter=True)
    assert [(c.effort, c.batch) for c in fake_llm.calls_for("profile.build")] == [
        ("medium", False),
        ("low", False),
    ]
    assert [line["effort"] for line in log_capture.events("llm_call")] == ["medium", "low"]


@pytest.mark.parametrize(("disable_thinking", "sent"), [(True, "none"), (False, None)])
async def test_effort_none_reaches_the_adapter_and_the_log(
    prompts_dir: Path, log_capture: LogCapture, disable_thinking: bool, sent: str | None
) -> None:
    """Issue #68: a route with ``effort: none`` hands ``none`` to an adapter that can turn
    thinking off (and logs it as the effort sent); any other adapter gets no effort."""
    from tests.conftest import fake_price_table, fake_route_table

    table = fake_route_table()
    off = table.resolve("classify.reply_needed").model_copy(update={"effort": "none"})
    table.routes["classify.reply_needed"] = off
    fake = FakeAdapter(
        capabilities=Capabilities(
            structured_outputs=True,
            prompt_caching=False,
            batch=False,
            reasoning_effort=True,
            disable_thinking=disable_thinking,
        )
    )
    router = LLMRouter(
        routes=table,
        prices=fake_price_table(),
        adapters={"fake": fake},
        prompts=PromptStore(prompts_dir),
    )
    fake.set_response(
        "classify.reply_needed",
        ReplyNeededOutput(
            reply_needed=True,
            category="none",
            confidence=0.9,
            resolves_pending=False,
            decided_by="default",
        ),
    )
    await router.classify_reply_needed(
        ReplyNeededRequest(
            incoming_messages=[MessageItem(id="msg:1", sender="C", text="synthetic?")],
            contact=ContactInfo(display_name="C", kind="person"),
        )
    )
    (call,) = fake.calls_for("classify.reply_needed")
    assert call.effort == sent
    (line,) = log_capture.events("llm_call")
    assert line["effort"] == sent
