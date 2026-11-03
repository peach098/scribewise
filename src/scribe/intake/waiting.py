"""Waiting turns: the random reading delay and the planned reading time (FR-016, FR-017).

A wait that was cut short on purpose ("👀 Read now", or the user read the chat on another
device) is recorded explicitly as an ``activity_records`` row of kind ``read_now`` for the turn
(``meta_json.source = "read_elsewhere"`` for the latter). Re-planning (/resume, new working
hours, days or time zone) and putting a turn back to ``waiting`` never move such a turn into
the future again.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ConversationTurn,
    Message,
    TurnState,
    User,
    WorkChat,
)
from scribe.scheduling.working_hours import next_allowed

_SYSTEM_RANDOM = random.SystemRandom()


def pick_delay(min_s: int, max_s: int, rng: random.Random | None = None) -> int:
    """Uniform whole seconds in ``[min_s, max_s]`` (``min == max`` gives a fixed delay)."""
    if min_s < 0 or min_s > max_s:
        raise ValueError(f"invalid delay range {min_s}..{max_s}")
    return (rng or _SYSTEM_RANDOM).randint(min_s, max_s)


def delay_range(user: User, chat: WorkChat) -> tuple[int, int]:
    """Per-chat override when both bounds are set, else the user's global range."""
    if chat.override_delay_min_s is not None and chat.override_delay_max_s is not None:
        return chat.override_delay_min_s, chat.override_delay_max_s
    return user.delay_min_s, user.delay_max_s


def planned_reading_time(arrived_at: datetime, delay_s: int, user: User) -> datetime:
    """``next_allowed(arrived_at + delay)`` in the user's time zone and working hours."""
    return next_allowed(
        arrived_at + timedelta(seconds=delay_s),
        user.timezone,
        user.working_days,
        user.work_start,
        user.work_end,
    )


def replanned_due_at(
    arrived_at: datetime,
    delay_s: int,
    due_at: datetime,
    user: User,
    now: datetime,
    *,
    read_now: bool = False,
    hold_until: datetime | None = None,
) -> datetime:
    """The reading time of a waiting turn under the user's *current* working time.

    ``next_allowed(max(arrived_at + delay, hold_until, now))``: never before the delay has
    passed, never before a forwards-only turn's hold (issue #47), never outside working hours,
    never in the past. A turn whose wait was cut short on purpose keeps its ``due_at``:
    ``read_now`` (an explicit "read now" / read-elsewhere record, whenever it happened), or
    ``due_at`` before ``arrived_at + delay`` (rows from before the record).
    """
    planned = arrived_at + timedelta(seconds=delay_s)
    if read_now or due_at < planned:
        return due_at
    earliest = max(planned, now) if hold_until is None else max(planned, hold_until, now)
    return next_allowed(earliest, user.timezone, user.working_days, user.work_start, user.work_end)


def requeued_due_at(
    read_at: datetime | None, user: User, now: datetime, *, read_now: bool
) -> datetime:
    """``due_at`` of a turn put back from ``reading`` to ``waiting`` (restart, stuck sweep).

    Already marked read (``read_at`` set) or explicitly asked for ("read now", read
    elsewhere): due now. Otherwise it was never read, so the working hours still apply
    (SC-004): the next allowed moment from now.
    """
    if read_at is not None or read_now:
        return now
    return next_allowed(now, user.timezone, user.working_days, user.work_start, user.work_end)


async def read_now_turn_ids(
    session: AsyncSession, user_id: uuid.UUID | None, turn_ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    """Turns among ``turn_ids`` whose wait was cut short on purpose (``read_now`` records)."""
    if not turn_ids:
        return set()
    query = select(ActivityRecord.conversation_turn_id).where(
        ActivityRecord.kind == ActivityKind.read_now,
        ActivityRecord.conversation_turn_id.in_(list(turn_ids)),
    )
    if user_id is not None:
        query = query.where(ActivityRecord.user_id == user_id)
    return {turn_id for turn_id in (await session.execute(query)).scalars() if turn_id}


async def replan_waiting_turns(session: AsyncSession, user: User, now: datetime) -> int:
    """Recompute ``due_at`` of the user's waiting turns after their working hours, days or time
    zone changed, or after /resume (FR-017). Returns how many turns moved.

    A blocking ``FOR UPDATE`` (in id order): a turn locked by someone else is waited for, not
    skipped. Waiting turns have no drafts, so this takes no draft lock (lock order holds).
    """
    result = await session.execute(
        select(ConversationTurn)
        .where(ConversationTurn.user_id == user.id, ConversationTurn.state == TurnState.waiting)
        .order_by(ConversationTurn.id)
        .with_for_update()
    )
    turns = list(result.scalars().all())
    shortened = await read_now_turn_ids(session, user.id, [turn.id for turn in turns])
    moved = 0
    for turn in turns:
        due_at = replanned_due_at(
            turn.arrived_at,
            turn.delay_s,
            turn.due_at,
            user,
            now,
            read_now=turn.id in shortened,
            hold_until=turn.forwards_hold_until,
        )
        if due_at != turn.due_at:
            turn.due_at = due_at
            moved += 1
    await session.flush()
    return moved


async def first_waiting_message_ids(
    session: AsyncSession, user_id: uuid.UUID, work_chat_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Per chat, the Telegram id of the earliest first message of a still-``waiting`` turn.

    Marking a chat read is a high-water mark, so reading past this id would read a message
    that is still meant to stay unread (another contact's turn in a group).
    """
    if not work_chat_ids:
        return {}
    rows = await session.execute(
        select(ConversationTurn.work_chat_id, func.min(Message.telegram_message_id))
        .join(Message, Message.id == ConversationTurn.first_message_id)
        .where(
            ConversationTurn.user_id == user_id,
            ConversationTurn.work_chat_id.in_(list(work_chat_ids)),
            ConversationTurn.state == TurnState.waiting,
        )
        .group_by(ConversationTurn.work_chat_id)
    )
    return {chat_id: int(first) for chat_id, first in rows.all() if first is not None}


def readable_up_to(target: int, first_waiting: int | None) -> int:
    """Read up to ``target`` but never up to a still-waiting message: ``min(target, first - 1)``."""
    return target if first_waiting is None else min(target, first_waiting - 1)
