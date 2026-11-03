"""``task_source_connections`` rows (one per user).

The Jira comment sync (specs/003-jira-commitments J8) claims due connections with
:func:`claim_due_jira`: the claim moves ``next_sync_at`` forward before the lock is released, so
two schedulers never sync the same user at once.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import (
    ConnectionStatus,
    JiraCommentCursor,
    TaskProvider,
    TaskSourceConnection,
)


async def get_connection(session: AsyncSession, user_id: uuid.UUID) -> TaskSourceConnection | None:
    result = await session.execute(
        select(TaskSourceConnection).where(TaskSourceConnection.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def upsert_connection(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    provider: TaskProvider,
    credentials: dict[str, Any],
    config_json: dict[str, Any] | None = None,
) -> TaskSourceConnection:
    conn = await get_connection(session, user_id)
    if conn is None:
        conn = TaskSourceConnection(
            user_id=user_id,
            provider=provider,
            credentials=credentials,
            config_json=config_json or {},
        )
        session.add(conn)
    else:
        conn.provider = provider
        conn.credentials = credentials
        conn.config_json = config_json or {}
        conn.status = ConnectionStatus.active
    await session.flush()
    return conn


async def set_status(session: AsyncSession, user_id: uuid.UUID, status: ConnectionStatus) -> None:
    conn = await get_connection(session, user_id)
    if conn is not None:
        conn.status = status
        await session.flush()


async def delete_connection(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Remove the connection and what the Jira comment sync remembered for it."""
    await session.execute(
        delete(TaskSourceConnection).where(TaskSourceConnection.user_id == user_id)
    )
    await session.execute(delete(JiraCommentCursor).where(JiraCommentCursor.user_id == user_id))


async def claim_due_jira(
    session: AsyncSession, now: datetime, *, interval: timedelta, limit: int = 100
) -> list[uuid.UUID]:
    """User ids whose Jira comment sync is due; each claimed row's ``next_sync_at`` becomes
    ``now + interval`` in this transaction (rows locked by another claim are skipped)."""
    result = await session.execute(
        select(TaskSourceConnection)
        .where(
            TaskSourceConnection.provider == TaskProvider.jira,
            TaskSourceConnection.status != ConnectionStatus.disconnected,
            or_(
                TaskSourceConnection.next_sync_at.is_(None),
                TaskSourceConnection.next_sync_at <= now,
            ),
        )
        .order_by(TaskSourceConnection.next_sync_at.nulls_first())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(result.scalars().all())
    for conn in claimed:
        conn.next_sync_at = now + interval
    await session.flush()
    return [conn.user_id for conn in claimed]


async def set_sync_state(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    watermark: datetime | None = None,
    next_sync_at: datetime | None = None,
) -> None:
    """Record sync progress (only the given fields change)."""
    conn = await get_connection(session, user_id)
    if conn is None:
        return
    if watermark is not None and (conn.sync_watermark is None or watermark > conn.sync_watermark):
        conn.sync_watermark = watermark
    if next_sync_at is not None:
        conn.next_sync_at = next_sync_at
    await session.flush()


async def list_jira_user_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Users with a Jira connection that is not disconnected (the daily verification)."""
    result = await session.execute(
        select(TaskSourceConnection.user_id).where(
            TaskSourceConnection.provider == TaskProvider.jira,
            TaskSourceConnection.status != ConnectionStatus.disconnected,
        )
    )
    return list(result.scalars().all())


async def get_connection_for_update(
    session: AsyncSession, user_id: uuid.UUID
) -> TaskSourceConnection | None:
    """The user's row, locked (``FOR UPDATE``) until the transaction ends and freshly loaded.

    Every read-modify-write of ``config_json`` (failure counter, alert flag, board selection,
    reconnect) goes through this, so concurrent writers wait instead of losing updates.
    """
    result = await session.execute(
        select(TaskSourceConnection)
        .where(TaskSourceConnection.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()
