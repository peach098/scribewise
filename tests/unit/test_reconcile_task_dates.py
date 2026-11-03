"""Which date a task's draft uses and the mismatch warning (specs/003-jira-commitments T040)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from scribe.context.task_dates import MismatchPart, mismatch_parts, reconcile_task_dates
from scribe.context.tasks.base import Task, TaskComment
from scribe.context.tasks.deadlines import resolve_deadline
from scribe.drafting.composer import task_item
from scribe.llm.provider import CommitmentRef
from scribe.telegram.control_bot.render import _warning

TZ = "Europe/Moscow"


def _at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


def _task(
    *,
    source: str = "jira",
    due: date | None = date(2026, 10, 25),
    due_set_at: datetime | None = None,
    comments: tuple[TaskComment, ...] = (),
) -> Task:
    task = Task(
        id="PAY-142" if source == "jira" else "c1",
        title="Fix refund rounding",
        status="In Progress",
        due_at=datetime(due.year, due.month, due.day, 12, tzinfo=UTC) if due else None,
        due_is_date=source == "jira",
        assignees=[],
        url="",
        source=source,
        comments=comments,
        due_set_at=due_set_at,
    )
    return resolve_deadline(task, TZ)


def _promise(due: date, at: datetime, key: str = "PAY-142") -> CommitmentRef:
    return CommitmentRef(
        id="commit:1",
        what="fix on staging",
        due=due,
        issue_key=key,
        due_at=at,
        source_note="your Jira comment on PAY-142",
    )


def _run(task: Task, commitments: list[CommitmentRef]) -> tuple[date | None, list[str], str | None]:
    item = task_item(task, TZ)
    (out,), warnings = reconcile_task_dates([item], {item.id: task}, commitments, TZ)
    return out.due, warnings, out.deadline_note


def test_newer_promise_wins_and_warns() -> None:
    due, warnings, note = _run(_task(due_set_at=_at(2)), [_promise(date(2026, 10, 23), _at(21))])
    assert due == date(2026, 10, 23)
    assert note == "from your own promise (your Jira comment on PAY-142): “fix on staging”"
    (warning,) = warnings
    assert mismatch_parts(warning) == (
        "PAY-142",
        [
            MismatchPart(on=date(2026, 10, 23), kind="promise_jira", at=date(2026, 10, 21)),
            MismatchPart(on=date(2026, 10, 25), kind="due"),
        ],
    )
    assert _warning(warning, "en") == (
        "📅 Dates differ: PAY-142: Fri 23 Oct (your Jira comment, 21 Oct) · Sun 25 Oct (due date)"
    )
    assert _warning(warning, "ru").startswith("📅 Даты расходятся: PAY-142: ")


def test_due_changed_after_the_promise_wins() -> None:
    due, warnings, _ = _run(_task(due_set_at=_at(22)), [_promise(date(2026, 10, 23), _at(21))])
    assert due == date(2026, 10, 25)
    assert len(warnings) == 1  # still shown: the user promised another date


def test_equal_dates_or_a_single_date_do_not_warn() -> None:
    assert _run(_task(), [_promise(date(2026, 10, 25), _at(21))])[1] == []
    assert _run(_task(), [])[1] == []
    assert _run(_task(due=None), [_promise(date(2026, 10, 23), _at(21))])[1] == []


def test_promises_about_other_issues_are_ignored() -> None:
    due, warnings, _ = _run(_task(), [_promise(date(2026, 10, 23), _at(21), key="CORE-1")])
    assert due == date(2026, 10, 25) and warnings == []


def test_newest_of_several_promises_wins() -> None:
    promises = [_promise(date(2026, 10, 23), _at(18)), _promise(date(2026, 10, 27), _at(20))]
    due, warnings, _ = _run(_task(), promises)
    assert due == date(2026, 10, 27) and len(warnings) == 1


def test_trello_comment_deadline_vs_due_field() -> None:
    comment = TaskComment(id="a1", text="deadline moved to Nov 5", author="Ivan", at=_at(13))
    due, warnings, _ = _run(_task(source="trello", comments=(comment,)), [])
    assert due == date(2026, 11, 5)
    (warning,) = warnings
    text = _warning(warning, "en")
    assert "Thu 5 Nov (a comment, 13 Oct)" in text and "(due date)" in text
    assert "Ivan" not in warning  # no names in the stored warning


def test_other_or_broken_warnings_are_not_mismatches() -> None:
    assert mismatch_parts("tasks_unavailable") is None
    assert mismatch_parts("date_mismatch:{not json") is None
