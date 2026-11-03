"""``activity_records`` rows (FR-027). ``meta_json`` holds ids/counts only, never content."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import ActivityKind, ActivityRecord


async def record(
    session: AsyncSession,
    user_id: uuid.UUID,
    kind: ActivityKind,
    *,
    at: datetime | None = None,
    draft_id: uuid.UUID | None = None,
    conversation_turn_id: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> ActivityRecord:
    row = ActivityRecord(
        user_id=user_id,
        kind=kind,
        draft_id=draft_id,
        conversation_turn_id=conversation_turn_id,
        meta_json=meta or {},
    )
    if at is not None:
        row.at = at
    session.add(row)
    await session.flush()
    return row


async def list_recent(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 20
) -> list[ActivityRecord]:
    result = await session.execute(
        select(ActivityRecord)
        .where(ActivityRecord.user_id == user_id)
        .order_by(ActivityRecord.at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def has_login_outcome_since(
    session: AsyncSession, user_id: uuid.UUID, outcome: str, since: datetime
) -> bool:
    """Whether a ``telegram_login`` row with this outcome was recorded at or after ``since``."""
    result = await session.execute(
        select(ActivityRecord.id)
        .where(
            ActivityRecord.user_id == user_id,
            ActivityRecord.kind == ActivityKind.telegram_login,
            ActivityRecord.at >= since,
            ActivityRecord.meta_json["outcome"].astext == outcome,
        )
        .limit(1)
    )
    return result.first() is not None
