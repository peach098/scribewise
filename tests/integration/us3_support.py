"""US3 wiring for integration tests: the US2 harness plus a Trello board served by respx, task
source connections and commitment helpers. Synthetic data only."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.context.commitments import CommitmentExtractor, install_default_extractor
from scribe.context.tasks.trello import TRELLO_API
from scribe.db.models import Commitment, TaskProvider, User
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.llm.adapters.fake import FakeCall
from scribe.llm.registry import LLMRouter

TRELLO_CREDENTIALS = {"key": "synthetic-trello-key", "token": "synthetic-trello-token"}
BOARD_ID = "b0a4d0000000000000000001"


def block(call: FakeCall, name: str) -> Any:
    for item in call.user_blocks:
        if item.name == name:
            return json.loads(item.content)
    raise KeyError(name)


async def connect_trello(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    board_ids: list[str] | None = None,
) -> None:
    async with session_factory() as session:
        await tasks_conn_repo.upsert_connection(
            session,
            user.id,
            provider=TaskProvider.trello,
            credentials=dict(TRELLO_CREDENTIALS),
            config_json={"board_ids": board_ids if board_ids is not None else [BOARD_ID]},
        )
        await session.commit()


def mock_trello_board(
    router: respx.MockRouter,
    cards: list[dict[str, Any]],
    *,
    board_id: str = BOARD_ID,
    lists: list[dict[str, str]] | None = None,
    members: list[dict[str, str]] | None = None,
) -> respx.Route:
    lists = lists if lists is not None else [{"id": "l1", "name": "In progress"}]
    members = members if members is not None else [{"id": "m1", "fullName": "Anna Testova"}]
    route = router.get(f"{TRELLO_API}/1/boards/{board_id}/cards").respond(200, json=cards)
    router.get(f"{TRELLO_API}/1/boards/{board_id}/lists").respond(200, json=lists)
    router.get(f"{TRELLO_API}/1/boards/{board_id}/members").respond(200, json=members)
    return route


def trello_card(card_id: str, name: str, due: date | None, **values: Any) -> dict[str, Any]:
    return {
        "id": card_id,
        "name": name,
        "due": f"{due.isoformat()}T15:00:00.000Z" if due is not None else None,
        "idList": values.pop("idList", "l1"),
        "idMembers": values.pop("idMembers", ["m1"]),
        "url": values.pop("url", f"https://trello.com/c/{card_id}"),
    }


async def commitments_of(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> list[Commitment]:
    async with session_factory() as session:
        result = await session.execute(
            select(Commitment).where(Commitment.user_id == user.id).order_by(Commitment.created_at)
        )
        return list(result.scalars().all())


def inline_extractor(
    session_factory: async_sessionmaker[AsyncSession], llm_router: LLMRouter, clock: Any
) -> CommitmentExtractor:
    """The test sessions share one connection: run extraction inside the caller (savepoints of
    concurrent sessions on one connection would undo each other). The background behaviour is
    covered with real commits in ``test_extraction_runs_in_the_background``."""
    extractor = CommitmentExtractor(session_factory, llm_router, clock=clock, run_inline=True)
    install_default_extractor(extractor)
    return extractor
