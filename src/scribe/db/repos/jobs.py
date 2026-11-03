"""``scheduled_jobs`` rows (global, not per user)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import ScheduledJob


async def ensure_job(session: AsyncSession, name: str, next_run_at: datetime) -> None:
    """Insert the job row if it does not exist; an existing schedule is left untouched."""
    await session.execute(
        insert(ScheduledJob)
        .values(name=name, next_run_at=next_run_at)
        .on_conflict_do_nothing(index_elements=[ScheduledJob.name])
    )


async def get_job(session: AsyncSession, name: str) -> ScheduledJob | None:
    result = await session.execute(select(ScheduledJob).where(ScheduledJob.name == name))
    return result.scalar_one_or_none()


async def list_jobs(session: AsyncSession) -> list[ScheduledJob]:
    result = await session.execute(select(ScheduledJob).order_by(ScheduledJob.name))
    return list(result.scalars().all())
