"""TaskSource contract for Trello (T050, contracts/internal-interfaces.md "TaskSource").

Trello is served by respx: no request ever leaves the process. Every value is synthetic.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scribe.context.tasks.base import Container, Task, TaskSourceIdentity, TaskSourceUnavailable
from scribe.context.tasks.trello import TRELLO_API, TrelloTaskSource
from scribe.db.models import TaskProvider, TaskSourceConnection

KEY = "synthetic-key-0123456789abcdef"
TOKEN = "synthetic-token-fedcba9876543210"
CREDENTIALS = {"key": KEY, "token": TOKEN}
ROOT = Path(__file__).resolve().parents[2]


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _conn(board_ids: list[str], user_id: uuid.UUID | None = None) -> TaskSourceConnection:
    return TaskSourceConnection(
        user_id=user_id or uuid.uuid4(),
        provider=TaskProvider.trello,
        credentials=dict(CREDENTIALS),
        config_json={"board_ids": board_ids},
    )


def _mock_board(router: respx.MockRouter, board_id: str = "b1") -> dict[str, respx.Route]:
    cards = [
        {
            "id": "c1",
            "name": "Q3 report",
            "due": "2026-10-16T15:00:00.000Z",
            "idList": "l2",
            "idMembers": ["m2", "m3"],
            "url": "https://trello.com/c/synthetic1",
        },
        {
            "id": "c2",
            "name": "Synthetic backlog item",
            "due": None,
            "idList": "l1",
            "idMembers": [],
            "url": "https://trello.com/c/synthetic2",
        },
        {  # due date marked complete: finished, not an open task
            "id": "c3",
            "name": "Synthetic finished item",
            "due": "2026-10-12T15:00:00.000Z",
            "dueComplete": True,
            "idList": "l2",
            "idMembers": [],
            "url": "https://trello.com/c/synthetic3",
        },
    ]
    lists = [{"id": "l1", "name": "To do"}, {"id": "l2", "name": "In progress"}]
    members = [{"id": "m2", "fullName": "Anna Testova"}, {"id": "m3", "fullName": "Boris Sample"}]
    return {
        "cards": router.get(f"{TRELLO_API}/1/boards/{board_id}/cards").respond(200, json=cards),
        "lists": router.get(f"{TRELLO_API}/1/boards/{board_id}/lists").respond(200, json=lists),
        "members": router.get(f"{TRELLO_API}/1/boards/{board_id}/members").respond(
            200, json=members
        ),
    }


def _assert_authenticated_gets(router: respx.MockRouter) -> None:
    assert router.calls, "no request was made"
    for call in router.calls:
        assert call.request.method == "GET"  # read-only (FR-021)
        params = call.request.url.params
        assert params["key"] == KEY and params["token"] == TOKEN


async def test_validate_returns_identity() -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{TRELLO_API}/1/members/me").respond(
            200, json={"id": "m1", "username": "synthetic_user", "fullName": "Synthetic User"}
        )
        identity = await source.validate(CREDENTIALS)
        _assert_authenticated_gets(router)
    assert identity == TaskSourceIdentity(
        id="m1", username="synthetic_user", full_name="Synthetic User"
    )


async def test_list_containers_returns_open_boards() -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{TRELLO_API}/1/members/me/boards").respond(
            200,
            json=[
                {"id": "b1", "name": "Synthetic board", "closed": False},
                {"id": "b2", "name": "Archived board", "closed": True},
            ],
        )
        boards = await source.list_containers(CREDENTIALS)
        _assert_authenticated_gets(router)
    assert boards == [Container(id="b1", name="Synthetic board")]
    assert route.calls.last.request.url.params["filter"] == "open"


async def test_list_open_tasks_maps_status_due_and_assignees() -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=True) as router:
        routes = _mock_board(router)
        tasks = await source.list_open_tasks(_conn(["b1"]))
        _assert_authenticated_gets(router)
    params = routes["cards"].calls.last.request.url.params
    assert params["filter"] == "open"
    assert set(params["fields"].split(",")) == {
        "name",
        "desc",
        "due",
        "dueComplete",
        "idList",
        "idMembers",
        "url",
    }
    assert tasks == [
        Task(
            id="c1",
            title="Q3 report",
            status="In progress",  # status = list name
            due_at=datetime(2026, 10, 16, 15, 0, tzinfo=UTC),  # aware, never a UTC date
            assignees=["Anna Testova", "Boris Sample"],  # member full names
            url="https://trello.com/c/synthetic1",
            source="trello",
        ),
        Task(
            id="c2",
            title="Synthetic backlog item",
            status="To do",
            due_at=None,
            assignees=[],
            url="https://trello.com/c/synthetic2",
            source="trello",
        ),
    ]


async def test_list_open_tasks_reads_every_selected_board_and_no_board_means_no_call() -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=True) as router:
        _mock_board(router, "b1")
        _mock_board(router, "b2")
        tasks = await source.list_open_tasks(_conn(["b1", "b2"]))
    assert len(tasks) == 4  # 2 open cards per board (the finished one is skipped)
    with respx.mock(assert_all_called=False) as router:
        assert await source.list_open_tasks(_conn([])) == []
        assert not router.calls


async def test_open_tasks_are_cached_for_60_seconds_per_user() -> None:
    clock = FakeMonotonic()
    source = TrelloTaskSource(monotonic=clock)
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    with respx.mock(assert_all_called=True) as router:
        routes = _mock_board(router)
        first = await source.list_open_tasks(_conn(["b1"], user_a))
        clock.now += 59
        assert await source.list_open_tasks(_conn(["b1"], user_a)) == first
        assert routes["cards"].call_count == 1  # served from the cache
        await source.list_open_tasks(_conn(["b1"], user_b))  # another user: own cache entry
        assert routes["cards"].call_count == 2
        clock.now += 2  # 61 s after the first fetch
        await source.list_open_tasks(_conn(["b1"], user_a))
        assert routes["cards"].call_count == 3
        # A different board selection is never answered from the old entry.
        _mock_board(router, "b9")
        await source.list_open_tasks(_conn(["b9"], user_a))
    assert source.cached_tasks(user_a) is not None
    source.invalidate(user_a)
    assert source.cached_tasks(user_a) is None


@pytest.mark.parametrize(
    ("due", "timezone", "local"),
    [
        ("2026-10-16T22:30:00.000Z", "Europe/Moscow", date(2026, 10, 17)),  # Sat 01:30 Moscow
        ("2026-10-17T01:00:00.000Z", "America/New_York", date(2026, 10, 16)),  # Fri 21:00 EDT
        ("2026-12-19T01:00:00.000Z", "America/New_York", date(2026, 12, 18)),  # Fri 20:00 EST
    ],
)
async def test_due_date_is_the_users_local_date(due: str, timezone: str, local: date) -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{TRELLO_API}/1/boards/b1/cards").respond(
            200,
            json=[{"id": "c1", "name": "x", "due": due, "idList": "l1", "idMembers": []}],
        )
        router.get(f"{TRELLO_API}/1/boards/b1/lists").respond(200, json=[])
        router.get(f"{TRELLO_API}/1/boards/b1/members").respond(200, json=[])
        (task,) = await source.list_open_tasks(_conn(["b1"]))
    assert task.due_on(timezone) == local
    assert task.due_at is not None and task.due_at.tzinfo is not None


async def test_one_unreadable_board_is_skipped_and_reported() -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=True) as router:
        _mock_board(router, "b1")
        router.get(f"{TRELLO_API}/1/boards/gone/cards").respond(404, text="not found")
        router.get(f"{TRELLO_API}/1/boards/gone/lists").respond(404, text="not found")
        router.get(f"{TRELLO_API}/1/boards/gone/members").respond(404, text="not found")
        tasks = await source.list_open_tasks(_conn(["b1", "gone"]))
    assert [t.id for t in tasks] == ["c1", "c2"]
    assert getattr(tasks, "skipped_containers", None) == 1


async def test_every_board_failing_is_unavailable() -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=False) as router:
        for board in ("b1", "b2"):
            for part in ("cards", "lists", "members"):
                router.get(f"{TRELLO_API}/1/boards/{board}/{part}").respond(401, text="no")
        with pytest.raises(TaskSourceUnavailable, match="HTTP 401"):
            await source.list_open_tasks(_conn(["b1", "b2"]))


def test_done_lists_count_as_finished() -> None:
    def task(status: str) -> Task:
        return Task(
            id="c", title="t", status=status, due_at=None, assignees=[], url="", source="trello"
        )

    for status in ("Done", "Готово", "Closed", "Сделано ✅", "Completed this week"):
        assert task(status).done, status
    for status in ("To do", "In progress", "Doing", "Review", ""):
        assert not task(status).done, status


def _error_responses() -> list[Any]:
    return [
        httpx.Response(401, text="invalid token"),
        httpx.Response(429, text="rate limited"),
        httpx.Response(500, text="server error"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"unexpected": "shape"}),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
    ]


def _respond(route: respx.Route, response: Any) -> None:
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)


@pytest.mark.parametrize("response", _error_responses())
async def test_every_http_error_is_task_source_unavailable(response: Any) -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{TRELLO_API}/1/boards/b1/lists").respond(200, json=[])
        router.get(f"{TRELLO_API}/1/boards/b1/members").respond(200, json=[])
        for path in ("/1/boards/b1/cards", "/1/members/me", "/1/members/me/boards"):
            _respond(router.get(f"{TRELLO_API}{path}"), response)
        calls = [
            source.list_open_tasks(_conn(["b1"])),
            source.validate(CREDENTIALS),
            source.list_containers(CREDENTIALS),
        ]
        for call in calls:
            with pytest.raises(TaskSourceUnavailable) as caught:
                await call
            # Credentials never leak into the error (it is logged and shown).
            assert KEY not in str(caught.value) and TOKEN not in str(caught.value)


async def test_missing_credentials_are_unavailable_without_a_request() -> None:
    source = TrelloTaskSource()
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(TaskSourceUnavailable):
            await source.validate({"key": KEY})
        assert not router.calls


def test_trello_source_is_read_only() -> None:
    """FR-021: the Trello module issues GET requests only."""
    code = (ROOT / "src/scribe/context/tasks/trello.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(post|put|patch|delete)\(", code)
    assert not re.search(r"""["'](POST|PUT|PATCH|DELETE)["']""", code)
