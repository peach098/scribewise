"""Commitment rules without a database (T051, research R9): the 0.7 confidence floor, dedupe on
(contact, what, due), date extraction from a request and the conflict helper."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from scribe.context.commitments import (
    MIN_CONFIDENCE,
    dedupe_key,
    extract_dates,
    find_conflicts,
    select_storable,
)
from scribe.llm.provider import CommitmentItem, CommitmentRef, TaskItem

WEDNESDAY = date(2026, 10, 14)
CONTACT = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


def _item(what: str, due: date | None, confidence: float) -> CommitmentItem:
    return CommitmentItem(
        what=what, to_whom="Anna Testova", due=due, confidence=confidence, evidence="synthetic"
    )


def test_items_below_confidence_floor_are_not_stored() -> None:
    assert MIN_CONFIDENCE == 0.7
    items = [
        _item("send the deck", date(2026, 10, 15), 0.69),
        _item("send the report", date(2026, 10, 16), 0.7),
        _item("call back", None, 0.95),
    ]
    stored = select_storable(items, contact_id=CONTACT, existing=set())
    assert [i.what for i in stored] == ["send the report", "call back"]


def test_dedupe_on_contact_what_and_due() -> None:
    existing = {dedupe_key(CONTACT, "Send the deck", date(2026, 10, 15))}
    items = [
        _item("send  the deck ", date(2026, 10, 15), 0.9),  # same after normalising
        _item("send the deck", date(2026, 10, 16), 0.9),  # other due date: a new promise
        _item("send the deck", None, 0.9),  # no date: a new promise
        _item("Send the deck.", None, 0.8),  # duplicate inside the same extraction
    ]
    stored = select_storable(items, contact_id=CONTACT, existing=existing)
    assert [(i.what, i.due) for i in stored] == [
        ("send the deck", date(2026, 10, 16)),
        ("send the deck", None),
    ]
    other_contact = uuid.uuid4()
    assert dedupe_key(other_contact, "send the deck", date(2026, 10, 15)) not in existing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("can you send the presentation Wednesday?", [date(2026, 10, 14)]),
        ("can you send it on Thursday?", [date(2026, 10, 15)]),
        ("сможешь прислать в среду?", [date(2026, 10, 14)]),
        ("а к пятнице успеешь?", [date(2026, 10, 16)]),
        ("нужно до понедельника", [date(2026, 10, 19)]),
        ("tomorrow please", [date(2026, 10, 15)]),
        ("сделай сегодня", [date(2026, 10, 14)]),
        ("deadline 2026-10-20", [date(2026, 10, 20)]),
        ("до 21.10", [date(2026, 10, 21)]),
        ("by 22.10.2026", [date(2026, 10, 22)]),
        ("how are you?", []),
        ("version 2.5 is out", []),
    ],
)
def test_extract_dates_from_request(text: str, expected: list[date]) -> None:
    assert extract_dates(text, WEDNESDAY) == expected


def test_conflict_when_request_is_earlier_than_commitment_or_task_due() -> None:
    commitments = [
        CommitmentRef(id="commit:c1", what="send the presentation", due=date(2026, 10, 15)),
        CommitmentRef(id="commit:c2", what="no date promise", due=None),
    ]
    tasks = [
        TaskItem(id="task:t1", title="Q3 report", status="In progress", due=date(2026, 10, 16)),
        TaskItem(id="task:t2", title="Done soon", status="Doing", due=date(2026, 10, 13)),
    ]
    conflicts = find_conflicts([date(2026, 10, 14)], commitments, tasks)
    assert [(c.with_kind, c.with_id) for c in conflicts] == [
        ("commitment", "commit:c1"),
        ("task", "task:t1"),
    ]
    assert "Thu 15 Oct" in conflicts[0].description
    assert "Wed 14 Oct" in conflicts[0].description


def test_no_conflict_without_requested_date_or_when_request_is_not_earlier() -> None:
    commitments = [CommitmentRef(id="commit:c1", what="send the deck", due=date(2026, 10, 15))]
    tasks = [TaskItem(id="task:t1", title="Q3 report", status="Doing", due=date(2026, 10, 16))]
    assert find_conflicts([], commitments, tasks) == []
    assert find_conflicts([date(2026, 10, 16)], commitments, tasks) == []
    # Several requested dates are ambiguous: only the model may judge them (Phase 5 review F).
    assert find_conflicts([date(2026, 10, 20), date(2026, 10, 14)], commitments, []) == []
    # A finished task (list named like Done) is not an open deadline.
    done = [TaskItem(id="task:t9", title="Old", status="Done", due=date(2026, 10, 16))]
    assert find_conflicts([date(2026, 10, 14)], [], done) == []
