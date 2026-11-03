"""Issue #67: the router attributes each billed call to a user (``llm_call`` line and spend sink),
logs cache hit/miss tokens and prices calls at their start time. Synthetic content only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.pricing import ModelPrice, OffPeak, PriceTable
from scribe.llm.provider import (
    ContactInfo,
    DraftOutput,
    DraftRequest,
    MessageItem,
    ProfileContext,
    ProviderOutputInvalid,
    ProviderUnavailable,
    ReplyNeededOutput,
    ReplyNeededRequest,
    StyleProfileOutput,
    StyleProfileRequest,
    StyleRules,
    StyleSample,
    Usage,
)
from scribe.llm.registry import LLMRouter, PromptStore
from scribe.llm.spend import SpendEntry
from tests.conftest import LogCapture, fake_route_table

USER = uuid.UUID("00000000-0000-4000-8000-000000000067")
SATURDAY_NOON = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
MONDAY_PEAK = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)
SECRET = "SYNTHETIC-CONFIDENTIAL-CONTENT"


class Sink:
    def __init__(self, error: Exception | None = None) -> None:
        self.entries: list[SpendEntry] = []
        self.error = error

    async def __call__(self, entry: SpendEntry) -> None:
        if self.error is not None:
            raise self.error
        self.entries.append(entry)


def _router(
    fake: FakeAdapter, prompts_dir: Path, sink: Sink | None, at: datetime = SATURDAY_NOON
) -> LLMRouter:
    off_peak = OffPeak(multiplier=0.5, peak_weekdays=[0, 1, 2, 3, 4], peak_hours_utc=[(6, 10)])
    price = ModelPrice(input_per_m=1.0, output_per_m=2.0, cached_input_per_m=0.1, off_peak=off_peak)
    return LLMRouter(
        routes=fake_route_table(),
        prices=PriceTable(models={"fake-small": price, "fake-large": price}),
        adapters={"fake": fake},
        prompts=PromptStore(prompts_dir),
        clock=lambda: at,
        spend_sink=sink,
    )


def _draft_output() -> DraftOutput:
    return DraftOutput.model_validate(
        {
            "text": "Synthetic reply",
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


def _draft_request() -> DraftRequest:
    return DraftRequest(
        incoming_messages=[MessageItem(id="msg:1", sender="Colleague", text=SECRET)],
        contact=ContactInfo(display_name="Colleague", kind="person"),
        profile=ProfileContext(rules_json={"formality": 2}, examples=["Synthetic example"]),
    )


def _profile_request() -> StyleProfileRequest:
    return StyleProfileRequest(
        samples=[StyleSample(text=SECRET, sent_at=SATURDAY_NOON)], scope="global"
    )


def _profile_output() -> StyleProfileOutput:
    rules = StyleRules(
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
    return StyleProfileOutput(rules_json=rules, summary_text="s", chosen_example_indices=[])


async def test_call_is_logged_and_recorded_for_its_user(
    prompts_dir: Path, log_capture: LogCapture
) -> None:
    fake, sink = FakeAdapter(), Sink()
    fake.set_response("compose.draft", _draft_output())
    router = _router(fake, prompts_dir, sink)
    result = await router.draft_reply(_draft_request(), user_tier="quality", user_id=USER)

    (line,) = log_capture.events("llm_call")
    assert line["user_id"] == str(USER)
    assert line["cache_hit_tokens"] == 0
    assert line["cache_miss_tokens"] == result.usage.input_tokens
    assert line["cache_write_1h_input_tokens"] == 0
    (entry,) = sink.entries
    assert (entry.user_id, entry.route, entry.at) == (USER, "compose.draft", SATURDAY_NOON)
    assert entry.usage == result.usage and entry.cost_usd == result.cost_usd
    assert entry.failed is False
    assert SECRET not in log_capture.raw


async def test_cost_uses_the_call_start_time(prompts_dir: Path) -> None:
    fake = FakeAdapter()
    fake.set_response("compose.draft", _draft_output())
    off_peak = await _router(fake, prompts_dir, None, SATURDAY_NOON).draft_reply(
        _draft_request(), user_tier="quality"
    )
    peak = await _router(fake, prompts_dir, None, MONDAY_PEAK).draft_reply(
        _draft_request(), user_tier="quality"
    )
    assert off_peak.usage == peak.usage
    assert off_peak.cost_usd == pytest.approx(peak.cost_usd / 2, abs=1e-6)


async def test_without_user_nothing_is_recorded(prompts_dir: Path, log_capture: LogCapture) -> None:
    fake, sink = FakeAdapter(), Sink()
    fake.set_response("compose.draft", _draft_output())
    await _router(fake, prompts_dir, sink).draft_reply(_draft_request(), user_tier="quality")
    assert sink.entries == []
    (line,) = log_capture.events("llm_call")
    assert line["user_id"] is None


async def test_every_route_passes_the_user(prompts_dir: Path) -> None:
    fake, sink = FakeAdapter(), Sink()
    fake.set_response(
        "classify.reply_needed",
        ReplyNeededOutput(
            reply_needed=False, category="thanks", confidence=0.9, resolves_pending=False
        ),
    )
    fake.set_response("profile.build", _profile_output())
    router = _router(fake, prompts_dir, sink)
    request = ReplyNeededRequest(
        incoming_messages=[MessageItem(id="msg:1", sender="Colleague", text="thanks")],
        history=[],
        contact=ContactInfo(display_name="Colleague", kind="person"),
        pending_draft=False,
    )
    await router.classify_reply_needed(request, user_id=USER)
    await router.build_style_profile(_profile_request(), user_id=USER)
    await router.build_style_profile(_profile_request(), batch=True, user_id=USER)
    await router.build_style_profiles([_profile_request()], interactive=True, user_id=USER)
    assert [(e.user_id, e.route) for e in sink.entries] == [
        (USER, "classify.reply_needed"),
        (USER, "profile.build"),
        (USER, "profile.build"),
        (USER, "profile.build"),
    ]


async def test_billed_failure_is_recorded_as_failed(
    prompts_dir: Path, log_capture: LogCapture
) -> None:
    fake, sink = FakeAdapter(), Sink()
    usage = Usage(input_tokens=1_000_000, output_tokens=500_000)
    fake.set_response(
        "profile.build",
        ProviderOutputInvalid("truncated", truncated=True, usage=usage, finish_reason="length"),
    )
    with pytest.raises(ProviderOutputInvalid):
        await _router(fake, prompts_dir, sink).build_style_profile(_profile_request(), user_id=USER)
    (entry,) = sink.entries
    assert entry.failed is True and entry.usage == usage
    assert entry.cost_usd == pytest.approx((1.0 + 1.0) * 0.5)  # off-peak Saturday
    (line,) = log_capture.events("llm_call")
    assert line["user_id"] == str(USER) and line["cost_usd"] == entry.cost_usd
    assert line["cache_miss_tokens"] == 1_000_000


async def test_failure_without_usage_is_not_recorded(prompts_dir: Path) -> None:
    fake, sink = FakeAdapter(), Sink()
    fake.set_response("compose.draft", ProviderUnavailable("synthetic outage"))
    with pytest.raises(ProviderUnavailable):
        await _router(fake, prompts_dir, sink).draft_reply(
            _draft_request(), user_tier="quality", user_id=USER
        )
    assert sink.entries == []


async def test_spend_storage_failure_never_fails_the_call(
    prompts_dir: Path, log_capture: LogCapture
) -> None:
    fake = FakeAdapter()
    fake.set_response("compose.draft", _draft_output())
    router = _router(fake, prompts_dir, Sink(error=RuntimeError("db down")))
    result = await router.draft_reply(_draft_request(), user_tier="quality", user_id=USER)
    assert result.output.text == "Synthetic reply"
    (warning,) = log_capture.events("llm_spend_not_recorded")
    assert warning["error"] == "RuntimeError" and warning["route"] == "compose.draft"
