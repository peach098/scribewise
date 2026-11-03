"""Pending Trello/Jira credentials in the FSM: purged when abandoned, never exported (issue #56).

Synthetic data only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.db.models import BotFsmState, ScheduledJob, UserState
from scribe.retention.pending_credentials import (
    JOB_NAME,
    purge_pending_credentials,
    register_pending_credentials_job,
)
from scribe.retention.user_data import export_user_data
from scribe.scheduling.scheduler import Scheduler
from scribe.telegram.control_bot.jira_connect import JiraStates
from scribe.telegram.control_bot.tasks_command import KEY_TTL, TasksStates
from tests.conftest import FrozenClock, MakeUser

PAT = "synthetic-personal-access-token-0123456789"
KEY = "0123456789abcdef0123456789abcdef"


class _RecordingBot:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True


async def _put(
    db_session: AsyncSession,
    chat_id: int,
    state: str | None,
    data: dict[str, Any],
    updated: datetime,
) -> None:
    db_session.add(BotFsmState(chat_id=chat_id, state=state, data=data))
    await db_session.flush()
    await db_session.execute(
        update(BotFsmState).where(BotFsmState.chat_id == chat_id).values(updated_at=updated)
    )


def test_prefixes_match_aiogram_state_names() -> None:
    assert JiraStates.cert.state == "JiraStates:cert"
    assert TasksStates.token.state == "TasksStates:token"


async def test_abandoned_connect_flows_lose_their_credentials(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    now = clock()
    stale = now - KEY_TTL - timedelta(seconds=1)
    fresh = now - KEY_TTL + timedelta(minutes=1)
    trello = {"key": KEY, "prompt_id": 77, "chat_id": 900000041}
    await _put(db_session, 900000041, TasksStates.token.state, trello, stale)  # purged
    pending = {"pending": {"pat": PAT}, "cert": {"sha256": "AA"}}
    await _put(db_session, 900000042, JiraStates.cert.state, pending, stale)  # purged
    await _put(db_session, 900000043, JiraStates.pat.state, {"site_url": "x"}, fresh)  # kept
    await _put(db_session, 900000044, "SettingsStates:delay", {}, now - timedelta(days=2))  # kept
    await _put(db_session, 900000045, "ApplyStates:use_case", {"use_case": "x"}, stale)  # kept
    await db_session.commit()
    bot = _RecordingBot()

    purged = await purge_pending_credentials(session_factory, now, cast(Bot, bot))

    db_session.expire_all()
    left = set((await db_session.execute(select(BotFsmState.chat_id))).scalars().all())
    assert {900000043, 900000044, 900000045} <= left
    assert not {900000041, 900000042} & left
    assert purged == 2
    assert bot.deleted == [(900000041, 77)]  # the token prompt echoed the key
    assert await purge_pending_credentials(session_factory, now) == 0


async def test_purge_runs_every_minute(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    scheduler = Scheduler(session_factory, tick_s=5, clock=clock)
    register_pending_credentials_job(scheduler, session_factory, None, clock)
    await scheduler.ensure_jobs()
    await scheduler.tick()
    await scheduler.wait_idle()
    row = (
        await db_session.execute(select(ScheduledJob).where(ScheduledJob.name == JOB_NAME))
    ).scalar_one()
    assert row.last_status == "ok"
    assert timedelta(0) < row.next_run_at - clock() <= timedelta(minutes=1)


async def test_export_never_holds_fsm_data(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await _put(db_session, chat_id, JiraStates.cert.state, {"pending": {"pat": PAT}}, clock())
    await db_session.commit()

    async with session_factory() as session:
        export = await export_user_data(session, user.id, now=clock())

    (row,) = export["bot_fsm_state"]
    assert row["state"] == JiraStates.cert.state
    assert "data" not in row
    assert PAT not in json.dumps(export)
