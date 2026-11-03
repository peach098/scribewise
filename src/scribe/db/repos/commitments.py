"""``commitments`` rows, always scoped by ``user_id``.

Jira-sourced rows (specs/003-jira-commitments J10, J11) may have no contact (``to_whom`` names
the counterparty) and carry the issue / comment they were found in. ``close`` records who closed
the row: ``/commitments`` closes as the user, the Jira sync as the system (with an
``auto_note``); only a row closed by the system can be reopened from the list.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import (
    Commitment,
    CommitmentAutoNote,
    CommitmentClosedBy,
    CommitmentSourceKind,
    CommitmentState,
)


async def add_commitment(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None,
    what: str,
    due: date | None,
    source_sent_at: datetime,
    confidence: Decimal | float,
    source_message_text: str | None = None,
    to_whom: str | None = None,
    source_kind: CommitmentSourceKind = CommitmentSourceKind.telegram_message,
    jira_issue_key: str | None = None,
    jira_comment_id: str | None = None,
    jira_comment_url: str | None = None,
    restricted: bool = False,
) -> Commitment:
    commitment = Commitment(
        user_id=user_id,
        contact_id=contact_id,
        what=what,
        due=due,
        source_sent_at=source_sent_at,
        confidence=Decimal(str(confidence)),
        source_message_text=source_message_text,
        to_whom=to_whom,
        source_kind=source_kind,
        jira_issue_key=jira_issue_key,
        jira_comment_id=jira_comment_id,
        jira_comment_url=jira_comment_url,
        restricted=restricted,
    )
    session.add(commitment)
    await session.flush()
    return commitment


async def get_commitment(
    session: AsyncSession, user_id: uuid.UUID, commitment_id: uuid.UUID
) -> Commitment | None:
    result = await session.execute(
        select(Commitment).where(Commitment.user_id == user_id, Commitment.id == commitment_id)
    )
    return result.scalar_one_or_none()


async def list_open(
    session: AsyncSession, user_id: uuid.UUID, *, contact_id: uuid.UUID | None = None
) -> list[Commitment]:
    query = select(Commitment).where(
        Commitment.user_id == user_id, Commitment.state == CommitmentState.open
    )
    if contact_id is not None:
        query = query.where(Commitment.contact_id == contact_id)
    result = await session.execute(query.order_by(Commitment.due.nulls_last()))
    return list(result.scalars().all())


async def close(
    session: AsyncSession,
    user_id: uuid.UUID,
    commitment_id: uuid.UUID,
    state: CommitmentState,
    *,
    at: datetime,
    closed_by: CommitmentClosedBy = CommitmentClosedBy.user,
    auto_note: CommitmentAutoNote | None = None,
) -> None:
    """Resolve or delete: sets ``closed_at`` (the purge key) and who closed it."""
    if state == CommitmentState.open:
        raise ValueError("use a closing state")
    commitment = await get_commitment(session, user_id, commitment_id)
    if commitment is not None:
        commitment.state = state
        commitment.closed_at = at
        commitment.closed_by = closed_by
        if auto_note is not None:
            commitment.auto_note = auto_note
        await session.flush()


async def reopen(
    session: AsyncSession, user_id: uuid.UUID, commitment_id: uuid.UUID
) -> Commitment | None:
    """Reopen a commitment Scribe closed by itself (Reopen / Restore, J11). The row then counts
    as changed by the user, so the same automatic change never closes it again."""
    commitment = await get_commitment(session, user_id, commitment_id)
    if (
        commitment is None
        or commitment.state == CommitmentState.open
        or commitment.closed_by != CommitmentClosedBy.system
    ):
        return None
    commitment.state = CommitmentState.open
    commitment.closed_at = None
    commitment.closed_by = None
    commitment.auto_note = None
    commitment.user_modified = True
    await session.flush()
    return commitment


async def get_by_comment(
    session: AsyncSession, user_id: uuid.UUID, comment_id: str, *, open_only: bool = True
) -> list[Commitment]:
    """Commitments linked to a Jira comment (newest first); with ``open_only=False`` also the
    closed ones (a comment of a row the user deleted is never stored again)."""
    query = select(Commitment).where(
        Commitment.user_id == user_id, Commitment.jira_comment_id == comment_id
    )
    if open_only:
        query = query.where(Commitment.state == CommitmentState.open)
    result = await session.execute(query.order_by(Commitment.created_at.desc()))
    return list(result.scalars().all())


async def list_open_for_issues(
    session: AsyncSession, user_id: uuid.UUID, issue_keys: Sequence[str]
) -> list[Commitment]:
    """Open commitments about any of ``issue_keys`` (earliest due first, undated last)."""
    keys = list(dict.fromkeys(issue_keys))
    if not keys:
        return []
    result = await session.execute(
        select(Commitment)
        .where(
            Commitment.user_id == user_id,
            Commitment.state == CommitmentState.open,
            Commitment.jira_issue_key.in_(keys),
        )
        .order_by(Commitment.due.nulls_last(), Commitment.created_at)
    )
    return list(result.scalars().all())


async def list_open_jira(session: AsyncSession, user_id: uuid.UUID) -> list[Commitment]:
    """Open commitments linked to a Jira comment (the daily verification, J8 / J11)."""
    result = await session.execute(
        select(Commitment).where(
            Commitment.user_id == user_id,
            Commitment.state == CommitmentState.open,
            Commitment.jira_comment_id.is_not(None),
        )
    )
    return list(result.scalars().all())


async def list_open_without_contact(session: AsyncSession, user_id: uuid.UUID) -> list[Commitment]:
    """Open commitments whose counterparty is no Telegram contact (merge candidates by name)."""
    result = await session.execute(
        select(Commitment).where(
            Commitment.user_id == user_id,
            Commitment.state == CommitmentState.open,
            Commitment.contact_id.is_(None),
        )
    )
    return list(result.scalars().all())


async def list_recently_closed_by_system(
    session: AsyncSession, user_id: uuid.UUID, *, since: datetime
) -> list[Commitment]:
    """Commitments Scribe closed by itself since ``since`` (newest first), for Reopen/Restore."""
    result = await session.execute(
        select(Commitment)
        .where(
            Commitment.user_id == user_id,
            Commitment.state != CommitmentState.open,
            Commitment.closed_by == CommitmentClosedBy.system,
            Commitment.closed_at >= since,
        )
        .order_by(Commitment.closed_at.desc())
    )
    return list(result.scalars().all())


async def list_open_for_contacts(
    session: AsyncSession, user_id: uuid.UUID, contact_ids: list[uuid.UUID]
) -> list[Commitment]:
    """Open commitments made to any of ``contact_ids`` (earliest due first, undated last)."""
    if not contact_ids:
        return []
    result = await session.execute(
        select(Commitment)
        .where(
            Commitment.user_id == user_id,
            Commitment.state == CommitmentState.open,
            Commitment.contact_id.in_(contact_ids),
        )
        .order_by(Commitment.due.nulls_last(), Commitment.created_at)
    )
    return list(result.scalars().all())


async def update_open(
    session: AsyncSession,
    user_id: uuid.UUID,
    commitment_id: uuid.UUID,
    *,
    what: str | None = None,
    due: date | None = None,
    clear_due: bool = False,
) -> Commitment | None:
    """Edit an open commitment (FR-031); returns None when it is not the caller's or not open."""
    commitment = await get_commitment(session, user_id, commitment_id)
    if commitment is None or commitment.state != CommitmentState.open:
        return None
    commitment.user_modified = True
    if what is not None:
        commitment.what = what
    if clear_due:
        commitment.due = None
    elif due is not None:
        commitment.due = due
    await session.flush()
    return commitment
