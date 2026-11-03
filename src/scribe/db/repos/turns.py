"""``conversation_turns`` rows, always scoped by ``user_id``."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import CursorResult, exists, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import (
    ConversationTurn,
    DecisionSource,
    DroppedReason,
    Message,
    ReplyDecision,
    SkipCategory,
    TurnState,
)


async def create_turn(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    work_chat_id: uuid.UUID,
    contact_id: uuid.UUID,
    first_message_id: uuid.UUID | None,
    arrived_at: datetime,
    delay_s: int,
    due_at: datetime,
    state: TurnState = TurnState.waiting,
) -> ConversationTurn:
    turn = ConversationTurn(
        user_id=user_id,
        work_chat_id=work_chat_id,
        contact_id=contact_id,
        first_message_id=first_message_id,
        last_message_id=first_message_id,
        arrived_at=arrived_at,
        delay_s=delay_s,
        due_at=due_at,
        state=state,
    )
    session.add(turn)
    await session.flush()
    return turn


async def attach_message(
    session: AsyncSession,
    user_id: uuid.UUID,
    turn_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    expected_state: TurnState,
) -> bool:
    """Make ``message_id`` the turn's last message, only while the turn is in
    ``expected_state`` (one guarded UPDATE: a concurrent claim, skip or drop either wins or
    waits on the row). Returns whether it was attached. ORM copies of the turn already in the
    session are not refreshed; read it again with ``populate_existing`` when needed."""
    attached = await session.execute(
        update(ConversationTurn)
        .where(
            ConversationTurn.user_id == user_id,
            ConversationTurn.id == turn_id,
            ConversationTurn.state == expected_state,
        )
        .values(last_message_id=message_id)
        .returning(ConversationTurn.id)
        .execution_options(synchronize_session=False)
    )
    return attached.scalar_one_or_none() is not None


async def get_turn(
    session: AsyncSession, user_id: uuid.UUID, turn_id: uuid.UUID
) -> ConversationTurn | None:
    result = await session.execute(
        select(ConversationTurn).where(
            ConversationTurn.user_id == user_id, ConversationTurn.id == turn_id
        )
    )
    return result.scalar_one_or_none()


async def find_turn_in_state(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    work_chat_id: uuid.UUID,
    state: TurnState,
    contact_id: uuid.UUID | None = None,
    arrived_after: datetime | None = None,
) -> ConversationTurn | None:
    """Latest turn in ``state`` for the chat (optionally the contact / arrival window)."""
    query = select(ConversationTurn).where(
        ConversationTurn.user_id == user_id,
        ConversationTurn.work_chat_id == work_chat_id,
        ConversationTurn.state == state,
    )
    if contact_id is not None:
        query = query.where(ConversationTurn.contact_id == contact_id)
    if arrived_after is not None:
        query = query.where(ConversationTurn.arrived_at >= arrived_after)
    result = await session.execute(query.order_by(ConversationTurn.arrived_at.desc()).limit(1))
    return result.scalar_one_or_none()


async def list_turns(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    state: TurnState,
    work_chat_id: uuid.UUID | None = None,
) -> list[ConversationTurn]:
    query = select(ConversationTurn).where(
        ConversationTurn.user_id == user_id, ConversationTurn.state == state
    )
    if work_chat_id is not None:
        query = query.where(ConversationTurn.work_chat_id == work_chat_id)
    result = await session.execute(query.order_by(ConversationTurn.due_at))
    return list(result.scalars().all())


async def count_due_waiting(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> int:
    """``waiting`` turns whose ``due_at`` has passed (the backlog notice after a reconnect)."""
    result = await session.execute(
        select(func.count())
        .select_from(ConversationTurn)
        .where(
            ConversationTurn.user_id == user_id,
            ConversationTurn.state == TurnState.waiting,
            ConversationTurn.due_at <= now,
        )
    )
    return int(result.scalar_one())


async def update_turn(
    session: AsyncSession, user_id: uuid.UUID, turn_id: uuid.UUID, **values: Any
) -> ConversationTurn | None:
    turn = await get_turn(session, user_id, turn_id)
    if turn is None:
        return None
    for key, value in values.items():
        setattr(turn, key, value)
    await session.flush()
    return turn


# --- reply gate (specs/004-skip-closing-messages) -------------------------------------------------


def _rowcount(result: object) -> int:
    return int(result.rowcount) if isinstance(result, CursorResult) else 0


async def set_decision(
    session: AsyncSession,
    user_id: uuid.UUID,
    turn_id: uuid.UUID,
    *,
    decision: ReplyDecision,
    source: DecisionSource,
    category: SkipCategory | None,
    confidence: float | None,
    decided_at: datetime,
    state: TurnState | None = None,
    last_message_id: uuid.UUID | None = None,
) -> bool:
    """Store the gate decision while the turn is still ``reading`` (guarded, like
    ``DueTurnsProcessor._set_turn``) and, when ``last_message_id`` is given, still ends at that
    message (no message joined it after the decision, issue #47). Returns whether a row
    changed.

    ``skip_category = COALESCE(:category, skip_category)``: a later decision without a category
    ("draft anyway") keeps the closing category the habit rule counts (research R3).
    """
    category_column = ConversationTurn.__table__.c.skip_category
    values: dict[str, Any] = {
        "reply_decision": decision,
        "decision_source": source,
        "skip_category": func.coalesce(
            literal(category, type_=category_column.type), ConversationTurn.skip_category
        ),
        "decision_confidence": None if confidence is None else Decimal(f"{confidence:.2f}"),
        "decided_at": decided_at,
    }
    if state is not None:
        values["state"] = state
    result = await session.execute(
        update(ConversationTurn)
        .where(
            ConversationTurn.user_id == user_id,
            ConversationTurn.id == turn_id,
            ConversationTurn.state == TurnState.reading,
            *(
                [ConversationTurn.last_message_id == last_message_id]
                if last_message_id is not None
                else []
            ),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return _rowcount(result) == 1


async def set_dropped_category(
    session: AsyncSession, user_id: uuid.UUID, turn_id: uuid.UUID, category: SkipCategory
) -> bool:
    """Habit rule input: the closing category of a turn a manual reply dropped before the gate
    decided it (guarded: still ``dropped`` / ``manual_reply``, no category yet)."""
    result = await session.execute(
        update(ConversationTurn)
        .where(
            ConversationTurn.user_id == user_id,
            ConversationTurn.id == turn_id,
            ConversationTurn.state == TurnState.dropped,
            ConversationTurn.dropped_reason == DroppedReason.manual_reply,
            ConversationTurn.skip_category.is_(None),
        )
        .values(skip_category=category)
        .execution_options(synchronize_session=False)
    )
    return _rowcount(result) == 1


async def habit_replied_count(
    session: AsyncSession,
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    *,
    before: datetime,
    window: int,
    reply_window: timedelta,
    retention_days: int,
) -> int:
    """Research R3: of the last ``window`` closing turns in the chat (``skip_category`` set and
    not ``forwarded``, inside the retention window, arrived before ``before``), how many the
    user answered: by a sent draft, by a manual reply that dropped the turn, or by an outgoing
    message within ``reply_window`` after the turn's last message and before the next incoming
    turn."""
    last_message = select(Message.sent_at).where(Message.id == ConversationTurn.last_message_id)
    result = await session.execute(
        select(
            ConversationTurn.state,
            ConversationTurn.dropped_reason,
            ConversationTurn.arrived_at,
            func.coalesce(last_message.scalar_subquery(), ConversationTurn.arrived_at),
        )
        .where(
            ConversationTurn.user_id == user_id,
            ConversationTurn.work_chat_id == work_chat_id,
            ConversationTurn.skip_category.is_not(None),
            # Not closings: forwards only (#47), the daily LLM hard cap (#74).
            ConversationTurn.skip_category.not_in([SkipCategory.forwarded, SkipCategory.budget]),
            ConversationTurn.arrived_at >= before - timedelta(days=retention_days),
            ConversationTurn.arrived_at < before,
        )
        .order_by(ConversationTurn.arrived_at.desc())
        .limit(window)
    )
    replied = 0
    for state, dropped_reason, arrived_at, last_at in result.all():
        if state == TurnState.answered or dropped_reason == DroppedReason.manual_reply:
            replied += 1
            continue
        next_turn_at = await session.scalar(
            select(func.min(ConversationTurn.arrived_at)).where(
                ConversationTurn.user_id == user_id,
                ConversationTurn.work_chat_id == work_chat_id,
                ConversationTurn.arrived_at > arrived_at,
            )
        )
        conditions = [
            Message.user_id == user_id,
            Message.work_chat_id == work_chat_id,
            Message.is_outgoing.is_(True),
            Message.sent_at > last_at,
            Message.sent_at <= last_at + reply_window,
        ]
        if next_turn_at is not None:
            conditions.append(Message.sent_at < next_turn_at)
        if await session.scalar(select(exists().where(*conditions))):
            replied += 1
    return replied


def _skipped_since(user_id: uuid.UUID, since: datetime) -> list[Any]:
    # Turns whose messages were purged have nothing to show or draft from (data-model.md).
    return [
        ConversationTurn.user_id == user_id,
        ConversationTurn.state == TurnState.skipped,
        ConversationTurn.decided_at >= since,
        ConversationTurn.last_message_id.is_not(None),
    ]


async def list_skipped(
    session: AsyncSession, user_id: uuid.UUID, *, since: datetime, limit: int, offset: int
) -> list[ConversationTurn]:
    """Skipped turns decided at or after ``since``, newest decision first (``/skipped``)."""
    result = await session.execute(
        select(ConversationTurn)
        .where(*_skipped_since(user_id, since))
        .order_by(ConversationTurn.decided_at.desc(), ConversationTurn.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def count_skipped(session: AsyncSession, user_id: uuid.UUID, *, since: datetime) -> int:
    result = await session.execute(
        select(func.count()).select_from(ConversationTurn).where(*_skipped_since(user_id, since))
    )
    return int(result.scalar_one())


async def draft_anyway_available(
    session: AsyncSession, user_id: uuid.UUID, turn: ConversationTurn
) -> bool:
    """FR-013: the turn's messages still exist, the user has not written in the chat since its
    last message, and no later turn in the chat was answered."""
    if turn.user_id != user_id or turn.last_message_id is None:
        return False
    last_at = await session.scalar(
        select(Message.sent_at).where(
            Message.user_id == user_id, Message.id == turn.last_message_id
        )
    )
    if last_at is None:
        return False
    replied = await session.scalar(
        select(
            exists().where(
                Message.user_id == user_id,
                Message.work_chat_id == turn.work_chat_id,
                Message.is_outgoing.is_(True),
                Message.sent_at > last_at,
            )
        )
    )
    answered_later = await session.scalar(
        select(
            exists().where(
                ConversationTurn.user_id == user_id,
                ConversationTurn.work_chat_id == turn.work_chat_id,
                ConversationTurn.id != turn.id,
                ConversationTurn.arrived_at > turn.arrived_at,
                ConversationTurn.state == TurnState.answered,
            )
        )
    )
    return not (replied or answered_later)


async def mark_draft_anyway(
    session: AsyncSession, user_id: uuid.UUID, turn_id: uuid.UUID, *, now: datetime
) -> bool:
    """``skipped -> waiting`` due now, for the scheduler to draft it (guarded: once)."""
    result = await session.execute(
        update(ConversationTurn)
        .where(
            ConversationTurn.user_id == user_id,
            ConversationTurn.id == turn_id,
            ConversationTurn.state == TurnState.skipped,
        )
        .values(
            state=TurnState.waiting,
            due_at=now,
            draft_anyway_at=now,
            decision_source=DecisionSource.user,
        )
        .execution_options(synchronize_session=False)
    )
    return _rowcount(result) == 1
