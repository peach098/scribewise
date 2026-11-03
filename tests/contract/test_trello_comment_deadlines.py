"""Trello deadlines from comments and descriptions (issue #6), against a recorded-shape board
response (``fixtures/trello/board_cards_with_comments.json``: the ``/1/boards/{id}/cards``
payload with nested ``actions``). respx serves Trello; every value is synthetic."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import respx

from scribe.context.tasks.base import Task
from scribe.context.tasks.deadlines import pending_texts, resolve_all
from scribe.context.tasks.trello import (
    ACTIONS_LIMIT,
    COMMENTS_PER_CARD,
    TRELLO_API,
    TrelloTaskSource,
)
from scribe.db.models import TaskProvider, TaskSourceConnection

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "trello"
TZ = "Europe/Moscow"
MEMBERS = [
    {"id": "66f2a1000000000000000m01", "fullName": "Anna Testova"},
    {"id": "66f2a1000000000000000m02", "fullName": "Ivan Petrov"},
]
LISTS = [
    {"id": "66f2a1000000000000000101", "name": "To do"},
    {"id": "66f2a1000000000000000102", "name": "In progress"},
]


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _conn(board_ids: list[str]) -> TaskSourceConnection:
    return TaskSourceConnection(
        user_id=uuid.uuid4(),
        provider=TaskProvider.trello,
        credentials={"key": "synthetic-key", "token": "synthetic-token"},
        config_json={"board_ids": board_ids},
    )


def _mock(router: respx.MockRouter, board: str, cards: list[dict[str, Any]]) -> respx.Route:
    route = router.get(f"{TRELLO_API}/1/boards/{board}/cards").respond(200, json=cards)
    router.get(f"{TRELLO_API}/1/boards/{board}/lists").respond(200, json=LISTS)
    router.get(f"{TRELLO_API}/1/boards/{board}/members").respond(200, json=MEMBERS)
    return route


async def _load(cards: list[dict[str, Any]] | None = None) -> list[Task]:
    with respx.mock(assert_all_called=True) as router:
        _mock(
            router, "b1", cards if cards is not None else _fixture("board_cards_with_comments.json")
        )
        return list(await TrelloTaskSource().list_open_tasks(_conn(["b1"])))


def _by_title(tasks: list[Task]) -> dict[str, Task]:
    return {task.title: task for task in tasks}


async def test_comments_come_nested_in_the_cards_call_so_requests_stay_bounded() -> None:
    cards = _fixture("board_cards_with_comments.json")
    with respx.mock(assert_all_called=True) as router:
        route = _mock(router, "b1", cards)
        tasks = await TrelloTaskSource().list_open_tasks(_conn(["b1"]))
        # Three requests per board whatever the number of cards: no request per card.
        assert len(router.calls) == 3
    params = route.calls.last.request.url.params
    assert params["actions"] == "commentCard,updateCard:due"
    assert params["actions_limit"] == str(ACTIONS_LIMIT)
    assert "desc" in params["fields"].split(",")
    assert params["action_memberCreator_fields"] == "fullName"
    assert len(tasks) == len(cards)


async def test_comments_description_and_due_change_are_mapped() -> None:
    tasks = _by_title(await _load())
    landing = tasks["Landing page"]
    assert [c.text for c in landing.comments] == [  # oldest first
        "Draft ready. Final deadline is 10.11",
        "Client asked for more time, moving to Nov 5",
    ]
    assert [c.author for c in landing.comments] == ["Anna Testova", "Ivan Petrov"]
    assert landing.comments[-1].at == datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    assert landing.due_set_at == datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
    assert landing.description == "Marketing landing for the autumn campaign."
    # The description's anchor is the card's creation time (encoded in the Trello id).
    assert landing.description_at == datetime.fromtimestamp(0x66F2A100, UTC)
    # A malformed action (no date) is skipped, the card and the board are kept.
    api_docs = tasks["API docs"]
    assert [c.text for c in api_docs.comments] == ["deadline Nov 1, as agreed"]
    assert tasks["Q4 budget"].due_set_at is None


async def test_only_the_last_comments_are_kept_and_long_ones_are_cut() -> None:
    actions = [
        {
            "id": f"a{i}",
            "type": "commentCard",
            "date": f"2026-10-{10 + i:02d}T09:00:00.000Z",
            "data": {"text": f"comment {i} " + "x" * 2000},
            "memberCreator": {"fullName": "Ivan Petrov"},
        }
        for i in range(8, 0, -1)  # newest first, as Trello sends them
    ]
    card: dict[str, Any] = {
        "id": "c1",
        "name": "Busy card",
        "due": None,
        "idList": "l1",
        "idMembers": [],
    }
    (task,) = await _load([{**card, "actions": actions}])
    assert len(task.comments) == COMMENTS_PER_CARD
    assert [c.text.split()[1] for c in task.comments] == ["4", "5", "6", "7", "8"]
    assert all(len(c.text) <= 1000 for c in task.comments)


async def test_effective_deadlines_from_the_recorded_board() -> None:
    tasks = _by_title(resolve_all(await _load(), TZ))

    # No due field, a comment "deadline: 5 Nov": due 5 Nov, from Ivan's comment.
    budget = tasks["Q4 budget"]
    assert budget.due_at is None
    assert budget.due_on(TZ) == date(2026, 11, 5)
    assert budget.deadline_source == "comment"
    assert budget.deadline is not None and budget.deadline.author == "Ivan Petrov"
    assert "deadline: 5 Nov" in budget.deadline_quote

    # A newer comment moving the deadline overrides the older comment and the due field.
    landing = tasks["Landing page"]
    assert landing.due_on(TZ) == date(2026, 11, 5)
    assert landing.deadline_source == "comment"
    assert landing.deadline is not None
    assert landing.deadline.noted_at == datetime(2026, 10, 19, 8, 0, tzinfo=UTC)

    # "до пятницы" written on Thu 15 Oct is Fri 16 Oct (not the Friday after today).
    contract = tasks["Contract review"]
    assert contract.due_on(TZ) == date(2026, 10, 16)

    # The due field changed after the comment: the due field wins.
    api_docs = tasks["API docs"]
    assert api_docs.due_on(TZ) == date(2026, 11, 20)
    assert api_docs.deadline_source == "due"

    # The description is the fallback when neither the due field nor a comment has a date.
    onboarding = tasks["Onboarding guide"]
    assert onboarding.due_on(TZ) == date(2026, 12, 1)
    assert onboarding.deadline_source == "description"

    # A meeting is not a deadline; a fuzzy ETA is left for the LLM step.
    assert tasks["Design sync"].due_on(TZ) is None
    assert pending_texts(tasks["Design sync"], TZ) == []
    assert tasks["Mobile release"].due_on(TZ) is None
    (pending,) = pending_texts(tasks["Mobile release"], TZ)
    assert pending.kind == "comment" and pending.author == "Ivan Petrov"
