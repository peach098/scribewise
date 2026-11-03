"""Announcement and delivery queries (specs/011-release-notes data-model.md).

Every function takes the caller's ``AsyncSession`` and never commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import (
    RELEASE_OPEN_STATES,
    RELEASE_RECEIVED_STATES,
    ReleaseAnnouncement,
    ReleaseDelivery,
    ReleaseDeliveryState,
    ReleaseTrigger,
)
from scribe.release_notes import version as versions
from scribe.release_notes.version import Version


def _parsed(raw: Iterable[str]) -> list[Version]:
    result = []
    for value in raw:
        try:
            result.append(versions.parse(value))
        except ValueError:  # never written by this code; ignored rather than fatal
            continue
    return result


async def last_handled_version(session: AsyncSession) -> Version | None:
    """The newest ``release_version`` over all announcements (research R3), or ``None``."""
    rows = await session.scalars(select(ReleaseAnnouncement.release_version).distinct())
    return max(_parsed(rows.all()), default=None)


async def create_announcement(
    session: AsyncSession,
    *,
    release_version: Version,
    covered: Sequence[Version],
    notes_json: dict[str, Any],
    trigger: ReleaseTrigger,
    now: datetime,
) -> ReleaseAnnouncement | None:
    """Insert one announcement. For ``auto`` a second row for the same version is never
    written (the partial unique index): ``None`` means another start announced it first."""
    statement = insert(ReleaseAnnouncement).values(
        id=uuid.uuid4(),
        release_version=versions.format(release_version),
        versions=[versions.format(v) for v in sorted(covered)],
        notes_json=notes_json,
        trigger=trigger,
        created_at=now,
        recipients=0,
    )
    if trigger is ReleaseTrigger.auto:
        statement = statement.on_conflict_do_nothing(
            index_elements=[ReleaseAnnouncement.release_version],
            index_where=text("trigger = 'auto'"),
        )
    new_id = await session.scalar(statement.returning(ReleaseAnnouncement.id))
    if new_id is None:
        return None
    return await session.get(ReleaseAnnouncement, new_id)


async def add_deliveries(
    session: AsyncSession,
    announcement: ReleaseAnnouncement,
    user_ids: Sequence[uuid.UUID],
    now: datetime,
) -> int:
    """One ``pending`` delivery per user, due now; returns how many were created."""
    if not user_ids:
        return 0
    rows = [
        {
            "id": uuid.uuid4(),
            "announcement_id": announcement.id,
            "user_id": user_id,
            "state": ReleaseDeliveryState.pending,
            "next_attempt_at": now,
        }
        for user_id in user_ids
    ]
    await session.execute(insert(ReleaseDelivery).values(rows).on_conflict_do_nothing())
    return len(rows)


async def received_versions(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    exclude_announcement: uuid.UUID | None = None,
) -> set[Version]:
    """Versions the user already got: deliveries ``sent`` or ``unknown`` (research R5)."""
    query = (
        select(ReleaseAnnouncement.versions)
        .join(ReleaseDelivery, ReleaseDelivery.announcement_id == ReleaseAnnouncement.id)
        .where(
            ReleaseDelivery.user_id == user_id,
            ReleaseDelivery.state.in_(RELEASE_RECEIVED_STATES),
        )
    )
    if exclude_announcement is not None:
        query = query.where(ReleaseAnnouncement.id != exclude_announcement)
    received: set[Version] = set()
    for covered in (await session.scalars(query)).all():
        received.update(_parsed(covered))
    return received


async def claim_next_due(session: AsyncSession, now: datetime) -> ReleaseDelivery | None:
    """The oldest due ``pending`` delivery, locked (``FOR UPDATE SKIP LOCKED``)."""
    result = await session.scalars(
        select(ReleaseDelivery)
        .where(
            ReleaseDelivery.state == ReleaseDeliveryState.pending,
            ReleaseDelivery.next_attempt_at <= now,
        )
        .order_by(ReleaseDelivery.next_attempt_at, ReleaseDelivery.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return result.first()


async def finish_if_done(session: AsyncSession, announcement_id: uuid.UUID, now: datetime) -> bool:
    """Set ``finished_at`` when no delivery is ``pending`` or ``sending``; True if it was set
    now."""
    open_rows = await session.scalar(
        select(func.count())
        .select_from(ReleaseDelivery)
        .where(
            ReleaseDelivery.announcement_id == announcement_id,
            ReleaseDelivery.state.in_(RELEASE_OPEN_STATES),
        )
    )
    if open_rows:
        return False
    finished = await session.scalar(
        update(ReleaseAnnouncement)
        .where(
            ReleaseAnnouncement.id == announcement_id,
            ReleaseAnnouncement.finished_at.is_(None),
        )
        .values(finished_at=now)
        .returning(ReleaseAnnouncement.id)
        .execution_options(synchronize_session=False)
    )
    return finished is not None


async def counts_by_state(
    session: AsyncSession, announcement_id: uuid.UUID
) -> dict[ReleaseDeliveryState, int]:
    """Deliveries of one announcement per state (every state present, zero when none)."""
    rows = await session.execute(
        select(ReleaseDelivery.state, func.count())
        .where(ReleaseDelivery.announcement_id == announcement_id)
        .group_by(ReleaseDelivery.state)
    )
    counts = dict.fromkeys(ReleaseDeliveryState, 0)
    for state, count in rows.all():
        counts[ReleaseDeliveryState(state)] = int(count)
    return counts


async def announcements(session: AsyncSession, *, limit: int | None) -> list[ReleaseAnnouncement]:
    """Newest first."""
    query = select(ReleaseAnnouncement).order_by(
        ReleaseAnnouncement.created_at.desc(), ReleaseAnnouncement.id
    )
    if limit is not None:
        query = query.limit(limit)
    return list((await session.scalars(query)).all())


async def deliveries_of_user(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[ReleaseDelivery, list[str]]]:
    """The user's deliveries with the versions of their announcement (data export)."""
    rows = await session.execute(
        select(ReleaseDelivery, ReleaseAnnouncement.versions)
        .join(ReleaseAnnouncement, ReleaseAnnouncement.id == ReleaseDelivery.announcement_id)
        .where(ReleaseDelivery.user_id == user_id)
        .order_by(ReleaseDelivery.created_at, ReleaseAnnouncement.created_at)
    )
    return [(delivery, list(covered)) for delivery, covered in rows.all()]
