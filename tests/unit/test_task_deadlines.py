"""Effective task deadlines (issue #6) without a database: the deterministic parser, the choice
between the due field, comments and the description, the LLM step on the fake adapter, and the
composer wiring (only tasks the filter selected; the draft context shows the source)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from scribe.context import commitments as commitments_ctx
from scribe.context.tasks.base import Task, TaskComment
from scribe.context.tasks.deadlines import (
    MAX_LLM_TASKS,
    deadline_note,
    find_deadline,
    pending_texts,
    refine_deadlines,
    resolve_deadline,
)
from scribe.drafting.composer import ComposeInput, compose_output, task_item, task_items
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import (
    ContactInfo,
    DeadlineExtraction,
    DeadlineItem,
    DraftOutput,
    FilterContextOutput,
    MessageItem,
    ProfileContext,
    ProviderUnavailable,
)
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot.review import _task_label

THU = date(2026, 10, 15)  # Thursday
TZ = "Europe/Moscow"


def _at(day: int, hour: int = 9, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=UTC)


def _task(
    *comments: tuple[str, datetime] | tuple[str, datetime, str],
    due: datetime | None = None,
    due_set_at: datetime | None = None,
    description: str = "",
    task_id: str = "t1",
) -> Task:
    return Task(
        id=task_id,
        title="Synthetic task",
        status="In progress",
        due_at=due,
        assignees=[],
        url="",
        source="trello",
        description=description,
        description_at=_at(1) if description else None,
        comments=tuple(
            TaskComment(
                id=f"a{i}", text=c[0], at=c[1], author=c[2] if len(c) > 2 else "Ivan Petrov"
            )
            for i, c in enumerate(comments)
        ),
        due_set_at=due_set_at,
    )


# --- the deterministic parser ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("deadline: 5 Nov", date(2026, 11, 5)),
        ("Deadline 05.11", date(2026, 11, 5)),
        ("сдать до 5 ноября", date(2026, 11, 5)),
        ("срок — 5-е ноября 2026", date(2026, 11, 5)),
        ("due 2026-11-05", date(2026, 11, 5)),
        ("moving to Nov 5th", date(2026, 11, 5)),
        ("перенесли на 12.11", date(2026, 11, 12)),
        ("сдать до пятницы", date(2026, 10, 16)),  # Thursday's comment: the next day
        ("by Friday please", date(2026, 10, 16)),
        ("by end of next week", date(2026, 10, 23)),
        ("сдаём до конца следующей недели", date(2026, 10, 23)),
        ("до конца недели", date(2026, 10, 16)),
        ("deadline: tomorrow", date(2026, 10, 16)),
        ("ETA end of month", date(2026, 10, 31)),
        ("deadline was Nov 1, moved to Nov 5", date(2026, 11, 5)),
        ("deadline 3 Jan", date(2027, 1, 3)),  # a past day-month is next year's
        ("deadline Nov 5, call on Friday to sync", date(2026, 11, 5)),
        ("Old plan: by 1 Nov.\nNew: deadline moved to 9 Nov", date(2026, 11, 9)),
    ],
)
def test_explicit_and_relative_deadlines(text: str, expected: date) -> None:
    found = find_deadline(text, THU)
    assert found is not None, text
    assert found[0] == expected


@pytest.mark.parametrize(
    "text",
    [
        "Call on Friday to go through the mockups",
        "созвон в среду",
        "Due to illness the demo is on Friday",
        "Shipped version 1.10 today",
        "today we agreed on the scope",
        "Looks good, thanks!",
        "",
    ],
)
def test_dates_without_a_deadline_cue_are_not_deadlines(text: str) -> None:
    assert find_deadline(text, THU) is None


# --- the choice ---------------------------------------------------------------------------------


def test_no_due_and_a_comment_deadline_is_due_that_day() -> None:
    task = resolve_deadline(_task(("deadline: 5 Nov", _at(18))), TZ)
    assert task.due_on(TZ) == date(2026, 11, 5)
    assert task.due_on("America/Los_Angeles") == date(2026, 11, 5)  # a date, not a moment
    assert task.deadline_source == "comment"
    assert task.deadline_quote == "deadline: 5 Nov"


def test_relative_phrases_resolve_against_the_comments_local_date() -> None:
    # 22:30 UTC on Sat 31 Oct is Sun 1 Nov 01:30 in Moscow: "tomorrow" is Mon 2 Nov there.
    comment = ("deadline: tomorrow", datetime(2026, 10, 31, 22, 30, tzinfo=UTC))
    assert resolve_deadline(_task(comment), TZ).due_on(TZ) == date(2026, 11, 2)
    assert resolve_deadline(_task(comment), "UTC").due_on("UTC") == date(2026, 11, 1)


def test_newer_comment_overrides_the_older_comment_and_the_due_field() -> None:
    task = resolve_deadline(
        _task(
            ("deadline is 10.11", _at(15), "Anna Testova"),
            ("moving to Nov 5", _at(18)),
            due=datetime(2026, 10, 30, 15, tzinfo=UTC),
            due_set_at=_at(12),
        ),
        TZ,
    )
    assert task.due_on(TZ) == date(2026, 11, 5)
    assert task.deadline is not None and task.deadline.author == "Ivan Petrov"
    assert deadline_note(task.deadline, TZ) == (
        "from a comment by Ivan Petrov, Sun 18 Oct: “moving to Nov 5”"
    )


def test_due_changed_after_the_comment_wins() -> None:
    due = datetime(2026, 11, 20, 15, tzinfo=UTC)
    task = resolve_deadline(_task(("deadline Nov 1", _at(17)), due=due, due_set_at=_at(20)), TZ)
    assert task.due_on(TZ) == date(2026, 11, 20)
    assert task.deadline_source == "due"
    assert deadline_note(task.deadline, TZ) is None


def test_unknown_due_change_is_older_than_the_fetched_comments() -> None:
    due = datetime(2026, 11, 20, 15, tzinfo=UTC)
    task = resolve_deadline(_task(("deadline Nov 1", _at(17)), due=due), TZ)
    assert task.due_on(TZ) == date(2026, 11, 1)


def test_comment_confirming_the_due_date_keeps_the_due_field_as_source() -> None:
    due = datetime(2026, 11, 5, 15, tzinfo=UTC)
    task = resolve_deadline(_task(("deadline: 5 Nov", _at(18)), due=due), TZ)
    assert task.deadline_source == "due"


def test_description_is_the_fallback() -> None:
    with_desc = _task(description="Scope: one page.\nDeadline: 2026-12-01")
    assert resolve_deadline(with_desc, TZ).deadline_source == "description"
    due = datetime(2026, 11, 20, 15, tzinfo=UTC)
    with_due = _task(description="Deadline: 2026-12-01", due=due)
    assert resolve_deadline(with_due, TZ).due_on(TZ) == date(2026, 11, 20)


def test_unresolved_task_falls_back_to_the_due_field() -> None:
    due = datetime(2026, 10, 16, 22, 30, tzinfo=UTC)
    task = _task(due=due)
    assert task.deadline is None and task.deadline_source == "due"
    assert task.due_on(TZ) == date(2026, 10, 17)


def test_conflict_backstop_uses_the_effective_deadline() -> None:
    task = resolve_deadline(_task(("deadline: 5 Nov", _at(18))), TZ)
    (item,) = task_items([task], "Synthetic Colleague", TZ)
    assert item.due == date(2026, 11, 5) and item.deadline_source == "comment"
    (conflict,) = commitments_ctx.find_conflicts([date(2026, 10, 30)], [], [item])
    assert "due Thu 5 Nov" in conflict.description


def test_card_label_names_the_comment_source() -> None:
    task = resolve_deadline(_task(("moving to Nov 5", _at(18))), TZ)
    assert _task_label(task, TZ, detailed=False) == (
        "Trello “Synthetic task” (due Thu 5 Nov, from a comment by Ivan Petrov)"
    )
    detailed = _task_label(task, TZ, detailed=True)
    assert "from a comment by Ivan Petrov, Sun 18 Oct: “moving to Nov 5”" in detailed
    assert _task_label(task, TZ, "ru", detailed=False) == (
        "Trello «Synthetic task» (до Чт, 5 ноя, из комментария — Ivan Petrov)"
    )
    assert "из комментария — Ivan Petrov, Вс, 18 окт: «moving to Nov 5»" in _task_label(
        task, TZ, "ru", detailed=True
    )
    described = resolve_deadline(_task(description="Deadline: 5 Nov"), TZ)
    assert described.deadline is not None and described.deadline.source == "description"
    assert "(due Thu 5 Nov, from the description)" in _task_label(described, TZ, detailed=False)
    assert "(до Чт, 5 ноя, из описания)" in _task_label(described, TZ, "ru", detailed=False)
    assert "из описания карточки: «Deadline: 5 Nov»" in _task_label(
        described, TZ, "ru", detailed=True
    )


def test_jira_card_label_names_the_issue_key() -> None:
    task = _task(("moving to Nov 5", _at(18)), task_id="PAY-142").model_copy(
        update={"source": "jira", "title": "Fix refund rounding"}
    )
    label = _task_label(resolve_deadline(task, TZ), TZ, detailed=False)
    assert label == (
        "Jira PAY-142 “Fix refund rounding” (due Thu 5 Nov, from a comment by Ivan Petrov)"
    )


# --- the LLM step -------------------------------------------------------------------------------

FUZZY = "ETA: two weeks after the demo, the demo is on the 26th"


def _item(**values: Any) -> DeadlineItem:
    payload: dict[str, Any] = {
        "task_id": "t1",
        "text_id": "c0",
        "source": "comment",
        "deadline_at": date(2026, 11, 9),
        "quote": "two weeks after the demo",
        "confidence": 0.9,
    }
    payload.update(values)
    return DeadlineItem.model_validate(payload)


def test_pending_texts_skip_parsed_older_and_hintless_comments() -> None:
    task = _task(
        (FUZZY, _at(10)),  # older than the parsed deadline below: cannot change the outcome
        ("deadline: 5 Nov", _at(15)),
        ("thanks!", _at(16)),  # no hint of a date
        ("ETA after the demo", _at(17)),
    )
    assert [t.id for t in pending_texts(task, TZ)] == ["c3"]
    due = datetime(2026, 11, 20, 15, tzinfo=UTC)
    assert pending_texts(_task((FUZZY, _at(10)), due=due, due_set_at=_at(11)), TZ) == []


async def test_llm_deadline_applies_with_source_and_quote(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[_item()]))
    task = resolve_deadline(_task((FUZZY, _at(20))), TZ)
    assert task.due_on(TZ) is None
    (refined,) = await refine_deadlines(llm_router, [task], TZ)
    assert refined.due_on(TZ) == date(2026, 11, 9)
    assert refined.deadline_source == "comment"
    assert refined.deadline_quote == "two weeks after the demo"
    assert refined.deadline is not None and refined.deadline.noted_at == _at(20)
    (call,) = fake_llm.calls_for("extract.deadlines")
    assert call.model == "fake-small"  # the cheap route


@pytest.mark.parametrize(
    "item",
    [
        _item(quote="three weeks after the launch"),  # not a quote of the comment
        _item(confidence=0.5),
        _item(text_id="c9"),  # not an offered text
        _item(task_id="t9"),
        _item(source="description"),  # wrong kind
        _item(deadline_at=date(2031, 1, 1)),  # implausible
    ],
)
async def test_ungrounded_llm_items_are_dropped(
    llm_router: LLMRouter, fake_llm: FakeAdapter, item: DeadlineItem
) -> None:
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[item]))
    (refined,) = await refine_deadlines(llm_router, [_task((FUZZY, _at(20)))], TZ)
    assert refined.due_on(TZ) is None


async def test_llm_failure_keeps_the_parsed_deadlines(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("extract.deadlines", ProviderUnavailable("synthetic outage"))
    task = resolve_deadline(_task(("deadline: 5 Nov", _at(15)), (FUZZY, _at(20))), TZ)
    (refined,) = await refine_deadlines(llm_router, [task], TZ)
    assert refined.due_on(TZ) == date(2026, 11, 5)


async def test_no_llm_call_without_pending_texts_and_at_most_one_call_bounded(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[]))
    await refine_deadlines(llm_router, [_task(("deadline: 5 Nov", _at(15)))], TZ)
    assert fake_llm.calls_for("extract.deadlines") == []
    many = [_task((FUZZY, _at(20)), task_id=f"t{i}") for i in range(MAX_LLM_TASKS + 5)]
    await refine_deadlines(llm_router, many, TZ)
    (call,) = fake_llm.calls_for("extract.deadlines")
    assert len(_block(call, "tasks")) == MAX_LLM_TASKS


def _block(call: FakeCall, name: str) -> Any:
    return next(json.loads(b.content) for b in call.user_blocks if b.name == name)


# --- composer wiring ----------------------------------------------------------------------------


def _draft() -> DraftOutput:
    return DraftOutput.model_validate(
        {
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
    )


def _compose_input() -> ComposeInput:
    tasks = [
        resolve_deadline(_task((FUZZY, _at(20)), task_id="t1"), TZ),
        resolve_deadline(_task((FUZZY, _at(20)), task_id="t2"), TZ),
    ]
    return ComposeInput(
        incoming=[MessageItem(id="msg:in1", sender="Synthetic Colleague", text="When is it?")],
        history=[],
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
        profile=ProfileContext(),
        tasks=[task_item(t, TZ) for t in tasks],
        reference_date=date(2026, 10, 21),
        timezone=TZ,
        task_details={f"task:{t.id}": t for t in tasks},
    )


async def test_composer_refines_only_the_selected_tasks_and_shows_the_source(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=[], task_ids=["task:t1"], commitment_ids=[]),
    )
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[_item()]))
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(llm_router, _compose_input())

    (call,) = fake_llm.calls_for("extract.deadlines")
    assert [t["id"] for t in _block(call, "tasks")] == ["t1"]  # the filter's choice only
    (filter_call,) = fake_llm.calls_for("retrieve.filter_context")
    assert FUZZY not in "".join(b.content for b in filter_call.user_blocks)  # comments stay out
    (task,) = result.request.tasks
    assert task.due == date(2026, 11, 9)
    assert task.deadline_source == "comment"
    assert task.deadline_note == (
        "from a comment by Ivan Petrov, Tue 20 Oct: “two weeks after the demo”"
    )
    (compose,) = fake_llm.calls_for("compose.draft")
    assert _block(compose, "tasks")[0]["deadline_note"] == task.deadline_note


async def test_composer_fallback_without_the_filter_makes_no_deadline_call(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("retrieve.filter_context", ProviderUnavailable("synthetic outage"))
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[_item()]))
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(llm_router, _compose_input())
    assert fake_llm.calls_for("extract.deadlines") == []
    assert [t.due for t in result.request.tasks] == [None, None]


# --- date-only due fields (Jira ``duedate``, specs/003-jira-commitments J7) -----------------------


@pytest.mark.parametrize("timezone", ["Pacific/Kiritimati", "Pacific/Pago_Pago", "UTC"])
def test_date_only_due_is_the_same_day_in_every_time_zone(timezone: str) -> None:
    task = _task(due=datetime(2026, 10, 25, 12, 0, tzinfo=UTC)).model_copy(
        update={"due_is_date": True}
    )
    assert task.due_on(timezone) == date(2026, 10, 25)
    resolved = resolve_deadline(task, timezone)
    assert resolved.deadline is not None
    assert resolved.deadline.on == date(2026, 10, 25)
    assert resolved.due_on(timezone) == date(2026, 10, 25)


def test_moment_due_still_converts_to_the_local_date() -> None:
    task = _task(due=datetime(2026, 10, 25, 12, 0, tzinfo=UTC))
    assert task.due_on("Pacific/Kiritimati") == date(2026, 10, 26)
