"""Which date a task's draft uses, and when to warn that dates differ (specs/003 J12, FR-J16).

A task can carry several dates: its due field (changed at ``due_set_at``), a deadline agreed in a
comment or the description (issue #6, ``Task.deadline``), and the user's own promises about it
(open commitments whose ``issue_key`` is the task's Jira key, from a Jira comment or a Telegram
message). :func:`reconcile_task_dates` makes the **newest** of them the task's ``due`` for the
draft (a promise reads "from your own promise …", so the draft follows the user's latest word
without any prompt change) and, when two or more different dates exist, returns a
``date_mismatch:<json>`` system warning per task: the card renders it in the user's language as
``📅 Dates differ: …`` whatever the model writes (SC-004).

The warning holds the issue key, ISO dates and source kinds only (no names, titles or comment
text), because system warnings are stored in the clear (``drafting/stored_output.py``); the
card turns it into text (:func:`mismatch_parts`). Pure and O(tasks × commitments).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from scribe.context.tasks.base import Task
from scribe.llm.provider import CommitmentRef, TaskItem

MISMATCH_PREFIX = "date_mismatch:"
SOURCE_KINDS = frozenset({"due", "comment", "description", "promise_jira", "promise_telegram"})
_OLDEST = datetime.min.replace(tzinfo=UTC)


# Source kinds in a mismatch warning (the card has one text per kind).
SourceKind = Literal["due", "comment", "description", "promise_jira", "promise_telegram"]


@dataclass(frozen=True)
class DatedSource:
    on: date
    at: datetime  # when this date was set / written / promised (``_OLDEST`` when unknown)
    kind: SourceKind = "due"
    note: str | None = None  # for the draft context when this date wins (English, model-facing)

    @property
    def known_at(self) -> bool:
        return self.at is not _OLDEST


def _local(moment: datetime, timezone: str) -> date:
    return moment.astimezone(ZoneInfo(timezone)).date()


def task_key(item: TaskItem, task: Task | None) -> str | None:
    """The Jira issue key of a task (``None`` for Trello cards)."""
    if task is not None and task.source == "jira":
        return task.id
    return None


def dated_sources(
    item: TaskItem,
    task: Task | None,
    commitments: Sequence[CommitmentRef],
    timezone: str,
) -> list[DatedSource]:
    found: list[DatedSource] = []
    if task is not None and task.due_at is not None:
        on = task.due_local_date(task.due_at, timezone)
        found.append(DatedSource(on=on, at=task.due_set_at or _OLDEST))
    if item.due is not None and item.deadline_source in ("comment", "description"):
        noted = task.deadline.noted_at if task is not None and task.deadline else None
        if noted is None and task is not None and task.comments:
            noted = task.comments[-1].at  # refined by the LLM: the newest fetched comment
        at = noted or (task.description_at if task is not None else None) or _OLDEST
        kind: SourceKind = "comment" if item.deadline_source == "comment" else "description"
        found.append(DatedSource(on=item.due, at=at, kind=kind, note=item.deadline_note))
    key = task_key(item, task)
    if key is not None:
        for commitment in commitments:
            if commitment.issue_key != key or commitment.due is None:
                continue
            at = commitment.due_at or _OLDEST
            where = commitment.source_note or "your promise"
            jira = commitment.source_note is not None and "Jira" in commitment.source_note
            found.append(
                DatedSource(
                    on=commitment.due,
                    at=at,
                    kind="promise_jira" if jira else "promise_telegram",
                    note=f"from your own promise ({where}): “{commitment.what}”",
                )
            )
    return found


def mismatch_warning(key: str | None, sources: Sequence[DatedSource], timezone: str) -> str | None:
    if len({s.on for s in sources}) < 2:
        return None
    ordered = sorted(sources, key=lambda s: s.at, reverse=True)
    seen: set[tuple[date, str]] = set()
    dates: list[dict[str, str]] = []
    for source in ordered:
        if (source.on, source.kind) in seen:
            continue
        seen.add((source.on, source.kind))
        entry = {"on": source.on.isoformat(), "kind": source.kind}
        if source.known_at and source.kind != "due":
            entry["at"] = _local(source.at, timezone).isoformat()
        dates.append(entry)
    payload = {"key": key, "dates": dates}
    return MISMATCH_PREFIX + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def reconcile_task_dates(
    items: Sequence[TaskItem],
    tasks: Mapping[str, Task],
    commitments: Sequence[CommitmentRef],
    timezone: str,
) -> tuple[list[TaskItem], list[str]]:
    """``items`` with each ``due`` set to the newest dated source, plus the mismatch warnings.
    ``tasks`` maps a task ref (``task:<id>``) to the loaded task."""
    result: list[TaskItem] = []
    warnings: list[str] = []
    for item in items:
        task = tasks.get(item.id)
        sources = dated_sources(item, task, commitments, timezone)
        if not sources:
            result.append(item)
            continue
        newest = max(sources, key=lambda s: s.at)
        if newest.kind.startswith("promise") and newest.on != item.due:
            item = item.model_copy(
                update={
                    "due": newest.on,
                    "deadline_source": "comment",
                    "deadline_note": newest.note,
                }
            )
        result.append(item)
        warning = mismatch_warning(task_key(item, task), sources, timezone)
        if warning is not None and warning not in warnings:
            warnings.append(warning)
    return result, warnings


@dataclass(frozen=True)
class MismatchPart:
    on: date
    kind: SourceKind
    at: date | None = None


def mismatch_parts(warning: str) -> tuple[str | None, list[MismatchPart]] | None:
    """(issue key, dated sources) of a ``date_mismatch:`` warning; ``None`` for other codes
    or an unreadable payload (the card then shows nothing for it)."""
    if not warning.startswith(MISMATCH_PREFIX):
        return None
    try:
        payload = json.loads(warning[len(MISMATCH_PREFIX) :])
        parts = [
            MismatchPart(
                on=date.fromisoformat(entry["on"]),
                kind=entry["kind"],
                at=date.fromisoformat(entry["at"]) if entry.get("at") else None,
            )
            for entry in payload["dates"]
            if entry.get("kind") in SOURCE_KINDS
        ]
    except (ValueError, KeyError, TypeError):
        return None
    key = payload.get("key")
    return (key if isinstance(key, str) else None), parts


__all__ = [
    "MISMATCH_PREFIX",
    "DatedSource",
    "dated_sources",
    "MismatchPart",
    "mismatch_parts",
    "mismatch_warning",
    "reconcile_task_dates",
]
