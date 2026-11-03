"""Scheduler against Postgres (T021): claiming, SKIP LOCKED, draft steps, jobs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ChatKind,
    ContactKind,
    ConversationTurn,
    Draft,
    ScheduledJob,
    TurnState,
    UserState,
)
from scribe.db.repos import chats, drafts, turns, users
from scribe.scheduling.scheduler import Daily, Scheduler
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser, next_telegram_id


async def _turn(
    db: AsyncSession, user_id: uuid.UUID, chat_id: uuid.UUID, contact_id: uuid.UUID, due: object
) -> ConversationTurn:
    return await turns.create_turn(
        db,
        user_id,
        work_chat_id=chat_id,
        contact_id=contact_id,
        first_message_id=None,
        arrived_at=due,  # type: ignore[arg-type]
        delay_s=0,
        due_at=due,  # type: ignore[arg-type]
    )


async def test_due_turns_are_claimed_and_handed_over(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    chat, contact = await make_chat(user), await make_contact(user)
    due = await _turn(db_session, user.id, chat.id, contact.id, clock() - timedelta(seconds=1))
    now = await _turn(db_session, user.id, chat.id, contact.id, clock())
    later = await _turn(db_session, user.id, chat.id, contact.id, clock() + timedelta(minutes=5))
    answered = await _turn(db_session, user.id, chat.id, contact.id, clock())
    answered.state = TurnState.answered
    await db_session.commit()

    scheduler = Scheduler(session_factory, tick_s=5, clock=clock)
    await scheduler.tick()  # no handler registered: nothing is claimed
    await db_session.refresh(due)
    assert due.state == TurnState.waiting

    handed: list[list[uuid.UUID]] = []

    async def handler(turn_ids: list[uuid.UUID]) -> None:
        handed.append(turn_ids)

    scheduler.on_due_turns(handler)
    await scheduler.tick()
    await scheduler.wait_idle()
    await scheduler.tick()  # already claimed turns are not handed over twice
    await scheduler.wait_idle()

    assert handed == [[due.id, now.id]]
    for turn, state in [
        (due, TurnState.reading),
        (now, TurnState.reading),
        (later, TurnState.waiting),
        (answered, TurnState.answered),
    ]:
        await db_session.refresh(turn)
        assert turn.state == state


async def test_claims_are_committed_and_skip_locked_across_instances(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    async with committed_session_factory() as db:
        user = await users.create_user(db, control_chat_id=next_telegram_id(), settings=settings)
        user.state = UserState.active  # only active / out-of-office users' turns are claimed
        chat = await chats.upsert_work_chat(
            db, user.id, telegram_chat_id=next_telegram_id(), title="t", kind=ChatKind.private
        )
        contact = await chats.upsert_contact(
            db,
            user.id,
            telegram_peer_id=next_telegram_id(),
            kind=ContactKind.person,
            display_name="c",
        )
        ids = {
            (await _turn(db, user.id, chat.id, contact.id, clock() - timedelta(seconds=i))).id
            for i in range(30)
        }
        await db.commit()

    claimed: list[uuid.UUID] = []
    states_seen_by_handler: set[TurnState] = set()

    async def handler(turn_ids: list[uuid.UUID]) -> None:
        # Runs outside the claiming transaction: a fresh session already sees "reading".
        async with committed_session_factory() as session:
            rows = await session.execute(
                select(ConversationTurn.state).where(ConversationTurn.id.in_(turn_ids))
            )
            states_seen_by_handler.update(rows.scalars().all())
        claimed.extend(turn_ids)

    instances = [
        Scheduler(committed_session_factory, tick_s=5, clock=clock, batch_size=7) for _ in range(3)
    ]
    for instance in instances:
        instance.on_due_turns(handler)
    for _ in range(3):
        await asyncio.gather(*(instance.tick() for instance in instances))
    await asyncio.gather(*(instance.wait_idle() for instance in instances))

    assert sorted(claimed) == sorted(ids)  # every turn exactly once
    assert states_seen_by_handler == {TurnState.reading}


async def test_reminder_and_expiry_steps_receive_locked_pending_drafts(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user()
    chat, contact = await make_chat(user), await make_contact(user)

    async def draft(prepared_ago: timedelta) -> Draft:
        turn = await _turn(db_session, user.id, chat.id, contact.id, clock())
        return await drafts.create_draft(
            db_session,
            user.id,
            conversation_turn_id=turn.id,
            text="synthetic",
            output_json={},
            prepared_at=clock() - prepared_ago,
        )

    fresh = await draft(timedelta(minutes=10))
    remind = await draft(timedelta(hours=3))
    expired = await draft(timedelta(hours=25))
    await db_session.commit()

    reminded: list[uuid.UUID] = []
    expiring: list[uuid.UUID] = []

    async def on_reminders(session: AsyncSession, rows: Sequence[Draft]) -> None:
        for row in rows:
            reminded.append(row.id)
            row.reminded_at = clock()

    async def on_expiries(session: AsyncSession, rows: Sequence[Draft]) -> None:
        expiring.extend(row.id for row in rows)

    scheduler = Scheduler(session_factory, tick_s=5, clock=clock)
    scheduler.on_reminders(on_reminders)
    scheduler.on_expiries(on_expiries)
    await scheduler.tick()
    await scheduler.tick()  # reminded_at now set: no second reminder

    assert sorted(reminded) == sorted([remind.id, expired.id])
    assert expiring == [expired.id, expired.id]  # handler did not change state: still due
    await db_session.refresh(fresh)
    assert fresh.reminded_at is None


async def test_jobs_run_once_per_schedule_and_failures_retry(
    committed_session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    """Jobs run as concurrent background tasks, so this test uses real connections."""
    runs: list[str] = []

    async def good() -> None:
        runs.append("good")

    async def bad() -> None:
        runs.append("bad")
        raise RuntimeError("synthetic failure")

    scheduler = Scheduler(committed_session_factory, tick_s=5, clock=clock, job_retry_s=600)
    scheduler.register_job("good_job", Daily(at=time(3, 0)), good)
    scheduler.register_job("bad_job", Daily(at=time(3, 0)), bad)
    await scheduler.ensure_jobs()
    await scheduler.ensure_jobs()  # idempotent

    async def job(name: str) -> ScheduledJob:
        async with committed_session_factory() as session:
            query = select(ScheduledJob).where(ScheduledJob.name == name)
            return (await session.execute(query)).scalar_one()

    first_run = (await job("good_job")).next_run_at
    assert first_run > clock()
    await scheduler.tick()
    await scheduler.wait_idle()
    assert runs == []  # not due yet

    clock.set(first_run + timedelta(seconds=1))
    await scheduler.tick()
    await scheduler.wait_idle()
    await scheduler.tick()
    await scheduler.wait_idle()
    assert sorted(runs) == ["bad", "good"]  # once each despite two ticks

    good_row, bad_row = await job("good_job"), await job("bad_job")
    assert good_row.last_status == "ok" and good_row.next_run_at == first_run + timedelta(days=1)
    assert bad_row.last_status == "error: RuntimeError"
    assert bad_row.next_run_at == clock() + timedelta(seconds=600)


async def test_turns_of_suspended_users_are_not_claimed(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """specs/005 T021: a user whose access ended is not read or drafted (research Q6)."""
    serving = await make_user(state=UserState.active)
    suspended = await make_user(state=UserState.active, access_suspended_at=clock())
    due = clock() - timedelta(seconds=1)
    turn_ids = {}
    for user in (serving, suspended):
        chat, contact = await make_chat(user), await make_contact(user)
        turn_ids[user.id] = (await _turn(db_session, user.id, chat.id, contact.id, due)).id
    await db_session.commit()

    handed: list[list[uuid.UUID]] = []

    async def handler(ids: list[uuid.UUID]) -> None:
        handed.append(ids)

    scheduler = Scheduler(session_factory, tick_s=5, clock=clock)
    scheduler.on_due_turns(handler)
    await scheduler.tick()
    await scheduler.wait_idle()

    assert handed == [[turn_ids[serving.id]]]
    left = await db_session.get(ConversationTurn, turn_ids[suspended.id])
    assert left is not None
    await db_session.refresh(left)
    assert left.state == TurnState.waiting
