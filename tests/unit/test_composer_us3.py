"""Composer US3 rules without a database (T058): namespaced task / commitment ids, the filter may
only select offered ones, the deterministic conflict backstop, and task candidate order."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from scribe.context.tasks.base import Task
from scribe.drafting.composer import ComposeInput, compose_output, task_items
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import (
    CommitmentRef,
    ContactInfo,
    DraftOutput,
    FilterContextOutput,
    MessageItem,
    ProfileContext,
    ProviderUnavailable,
    TaskItem,
)
from scribe.llm.registry import LLMRouter

WEDNESDAY = date(2026, 10, 14)
COMMITMENT = CommitmentRef(id="commit:c1", what="send the deck", due=date(2026, 10, 15))
TASK = TaskItem(id="task:t1", title="Q3 report", status="Doing", due=date(2026, 10, 16))


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


def _input(text: str = "can you send the deck Wednesday?", **values: Any) -> ComposeInput:
    return ComposeInput(
        incoming=[MessageItem(id="msg:in1", sender="Synthetic Colleague", text=text)],
        history=[],
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
        profile=ProfileContext(),
        tasks=[TASK],
        commitments=[COMMITMENT],
        reference_date=WEDNESDAY,
        **values,
    )


async def test_backstop_adds_conflicts_for_selected_items(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(
            history_ids=[], task_ids=["task:t1", "task:invented"], commitment_ids=["commit:c1"]
        ),
    )
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(llm_router, _input())
    assert [(c.with_kind, c.with_id) for c in result.output.conflicts] == [
        ("commitment", "commit:c1"),
        ("task", "task:t1"),
    ]
    request = result.request
    assert [t.id for t in request.tasks] == ["task:t1"]  # the invented id was never offered


async def test_backstop_keeps_model_conflicts_and_skips_unselected_items(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=[], task_ids=[], commitment_ids=["commit:c1"]),
    )
    model_conflict = {"with_kind": "commitment", "with_id": "commit:c1", "description": "model"}
    fake_llm.set_response("compose.draft", _draft(conflicts=[model_conflict]))
    result = await compose_output(llm_router, _input())
    assert [c.description for c in result.output.conflicts] == ["model"]  # not duplicated


async def test_no_backstop_on_filter_fallback_or_without_requested_date(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("retrieve.filter_context", ProviderUnavailable("synthetic outage"))
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(llm_router, _input())
    assert result.output.conflicts == [] and "filter_unavailable" in result.system_warnings

    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=[], task_ids=["task:t1"], commitment_ids=["commit:c1"]),
    )
    result = await compose_output(llm_router, _input("how is the deck going?"))
    assert result.output.conflicts == []


async def test_unknown_commitment_source_is_rejected(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=[], task_ids=[], commitment_ids=["commit:c1"]),
    )
    fake_llm.set_response(
        "compose.draft",
        _draft(sources=[{"kind": "commitment", "id": "commit:other", "used_for": "invented"}]),
    )
    result = await compose_output(llm_router, _input("ok?"))
    assert len(fake_llm.calls_for("compose.draft")) == 2
    assert result.output.sources == [] and result.output.needs_info
    assert "unsupported_sources" in result.system_warnings


def test_task_items_namespace_ids_and_put_the_contacts_tasks_first() -> None:
    def task(task_id: str, due: date | None, assignees: list[str]) -> Task:
        return Task(
            id=task_id,
            title=f"Task {task_id}",
            status="Doing",
            due_at=datetime(due.year, due.month, due.day, 12, tzinfo=UTC) if due else None,
            assignees=assignees,
            url="",
            source="trello",
        )

    tasks = [
        task("a", None, []),
        task("b", date(2026, 10, 20), []),
        task("c", date(2026, 10, 30), ["Anna Testova"]),
        task("d", date(2026, 10, 16), []),
    ]
    items = task_items(tasks, "anna testova")
    assert [i.id for i in items] == ["task:c", "task:d", "task:b", "task:a"]
    assert items[0].assignees == ["Anna Testova"]
    many = [task(str(n), None, []) for n in range(150)]
    assert len(task_items(many, "")) == 100
