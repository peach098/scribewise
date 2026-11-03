"""Composer validation without a database (T027): sources must reference request ids; the context
filter may only select offered ids and falls back deterministically when it fails."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from scribe.drafting.composer import ComposeInput, compose_output, invalid_source_ids
from scribe.drafting.stored_output import load_stored_output
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import (
    CommitmentRef,
    ContactInfo,
    DraftOutput,
    DraftRequest,
    FilterContextOutput,
    HistoryItem,
    MessageItem,
    ProfileContext,
    ProviderUnavailable,
    TaskItem,
)
from scribe.llm.registry import LLMRouter

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def _draft(**overrides: Any) -> DraftOutput:
    payload: dict[str, Any] = {
        "text": "Synthetic reply.",
        "language": "en",
        "needs_info": False,
        "placeholders": [],
        "sources": [{"kind": "message", "id": "msg:in1", "used_for": "the question"}],
        "flags": {
            "creates_commitment": {"value": False, "what": None, "due": None},
            "sensitive": {"value": False, "categories": []},
        },
        "conflicts": [],
        "tone_shift_applied": "none",
        "context_warnings": [],
    }
    payload.update(overrides)
    return DraftOutput.model_validate(payload)


def _history(n: int) -> list[HistoryItem]:
    return [
        HistoryItem(
            id=f"msg:h{i}",
            is_user=i % 2 == 0,
            sender="me" if i % 2 == 0 else "Synthetic Colleague",
            text=f"Synthetic history line {i}",
            sent_at=T0 - timedelta(minutes=n - i),
        )
        for i in range(n)
    ]


def _input(history: list[HistoryItem] | None = None, **values: Any) -> ComposeInput:
    return ComposeInput(
        incoming=[MessageItem(id="msg:in1", sender="Synthetic Colleague", text="Status?")],
        history=history if history is not None else _history(3),
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
        profile=ProfileContext(rules_json={"formality": 2}, examples=["Synthetic example"]),
        **values,
    )


def test_invalid_source_ids_helper() -> None:
    request = DraftRequest(
        incoming_messages=_input().incoming,
        history=_history(2),
        contact=_input().contact,
        tasks=[TaskItem(id="task:t1", title="Synthetic task", status="Doing")],
    )
    ok = _draft(
        sources=[
            {"kind": "message", "id": "msg:in1", "used_for": "q"},
            {"kind": "message", "id": "msg:h1", "used_for": "h"},
            {"kind": "task", "id": "task:t1", "used_for": "t"},
        ]
    )
    assert invalid_source_ids(ok, request) == []
    bad = _draft(
        sources=[
            {"kind": "task", "id": "task:unknown", "used_for": "invented"},
            {"kind": "message", "id": "task:t1", "used_for": "wrong kind"},
        ],
        conflicts=[{"with_kind": "commitment", "with_id": "commit:nope", "description": "x"}],
    )
    assert invalid_source_ids(bad, request) == ["task:unknown", "task:t1", "commit:nope"]


async def test_sources_must_reference_request_ids(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=["msg:h0"], task_ids=[], commitment_ids=[]),
    )
    invented = _draft(
        sources=[
            {"kind": "message", "id": "msg:in1", "used_for": "the question"},
            {"kind": "task", "id": "task:unknown", "used_for": "an invented deadline"},
        ]
    )
    fake_llm.set_response("compose.draft", invented)

    result = await compose_output(llm_router, _input())

    assert len(fake_llm.calls_for("compose.draft")) == 2  # rejected, regenerated once
    assert result.output.needs_info is True
    assert [s.id for s in result.output.sources] == ["msg:in1"]  # the invented id is dropped
    assert "unsupported_sources" in result.system_warnings
    # The model-facing contract is untouched: system warnings live outside DraftOutput.
    assert "unsupported_sources" not in result.output.context_warnings
    assert result.output_json()["system_warnings"] == result.system_warnings
    # Stored: metadata in the clear, content encrypted; it reads back as the same DraftOutput.
    stored = load_stored_output(result.output_json(), result.output.text)
    assert stored.output == DraftOutput.model_validate(result.output.model_dump())


async def test_regenerated_output_used_when_valid(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=[], task_ids=[], commitment_ids=[]),
    )
    outputs = iter(
        [
            _draft(sources=[{"kind": "commitment", "id": "commit:x", "used_for": "invented"}]),
            _draft(text="Grounded reply."),
        ]
    )

    def respond(call: FakeCall) -> DraftOutput:
        return next(outputs)

    fake_llm.set_response("compose.draft", respond)
    result = await compose_output(llm_router, _input())
    assert result.output.text == "Grounded reply."
    assert result.output.needs_info is False
    assert result.system_warnings == []
    assert len(result.compose_results) == 2
    assert result.filter_result is not None
    compose_cost = sum(r.cost_usd for r in result.compose_results)
    assert result.cost_usd == pytest.approx(compose_cost + result.filter_result.cost_usd)
    assert result.usage_json()["compose"]["calls"] == 2


async def test_filter_context_selects_only_offered_ids(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(
            history_ids=["msg:h2", "msg:not-offered", "msg:h0", "msg:h2"],
            task_ids=["task:invented"],
            commitment_ids=["commit:invented"],
        ),
    )
    fake_llm.set_response("compose.draft", _draft())

    result = await compose_output(llm_router, _input(history=_history(4)))

    request = result.request  # the real ids (the prompt carries short aliases, issue #71)
    assert [h.id for h in request.history] == ["msg:h0", "msg:h2"]  # chronological, offered only
    assert request.tasks == [] and request.commitments == []
    assert result.system_warnings == []
    assert result.filter_result is not None


async def test_same_day_items_reach_the_draft_without_the_filter(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    """Issue #75: the filter has no date anchor, so an item due on the requested day (a
    double-booking) is kept even when the filter drops it; other items still follow it."""
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=[], task_ids=[], commitment_ids=[]),
    )
    fake_llm.set_response("compose.draft", _draft())
    thursday = date(2026, 10, 15)
    inp = _input(
        history=[],
        commitments=[
            CommitmentRef(id="commit:demo", what="client demo", due=thursday),
            CommitmentRef(id="commit:later", what="report", due=date(2026, 10, 20)),
        ],
        tasks=[
            TaskItem(id="task:open", title="Release", status="Doing", due=thursday),
            TaskItem(id="task:done", title="Old", status="Done", due=thursday),
        ],
        reference_date=date(2026, 10, 14),
    )
    inp.incoming = [
        MessageItem(id="msg:in1", sender="Synthetic Colleague", text="Can you review on Thursday?")
    ]

    request = (await compose_output(llm_router, inp)).request
    assert [c.id for c in request.commitments] == ["commit:demo"]
    assert [t.id for t in request.tasks] == ["task:open"]


async def test_filter_failure_falls_back_to_recent_history(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("retrieve.filter_context", ProviderUnavailable("synthetic outage"))
    fake_llm.set_response("compose.draft", _draft())

    result = await compose_output(llm_router, _input(history=_history(20)))

    request = result.request
    assert [h.id for h in request.history] == [f"msg:h{i}" for i in range(5, 20)]  # last 15
    assert result.system_warnings == ["filter_unavailable"]
    assert result.filter_result is None
    assert "filter_unavailable" not in result.output.context_warnings


async def test_filter_skipped_without_candidates(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(llm_router, _input(history=[]))
    assert fake_llm.calls_for("retrieve.filter_context") == []
    assert result.filter_result is None and result.system_warnings == []


async def test_sensitive_keywords_merge_into_flags(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("compose.draft", _draft())
    inp = _input(history=[])
    inp.incoming = [MessageItem(id="msg:in1", sender="X", text="Send me the server password")]
    result = await compose_output(llm_router, inp)
    assert result.output.flags.sensitive.value is True
    assert result.output.flags.sensitive.categories == ["credentials"]


async def test_preset_system_warnings_are_kept(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(
        llm_router, _input(history=[], system_warnings=["weak_style_profile"])
    )
    assert result.system_warnings == ["weak_style_profile"]
