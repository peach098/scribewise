"""``jira_comment_cursor`` rows: the user's Jira comments already checked for promises
(specs/003-jira-commitments J9). No content, only ids, times and a body fingerprint."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import JiraCommentCursor, JiraCursorOutcome


async def get_many(
    session: AsyncSession, user_id: uuid.UUID, comment_ids: Sequence[str]
) -> dict[str, JiraCommentCursor]:
    ids = list(dict.fromkeys(comment_ids))
    if not ids:
        return {}
    result = await session.execute(
        select(JiraCommentCursor).where(
            JiraCommentCursor.user_id == user_id, JiraCommentCursor.comment_id.in_(ids)
        )
    )
    return {row.comment_id: row for row in result.scalars().all()}


async def upsert(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    issue_key: str,
    comment_id: str,
    comment_updated_at: datetime,
    body_hmac: bytes,
    outcome: JiraCursorOutcome,
) -> None:
    values = {
        "issue_key": issue_key,
        "comment_updated_at": comment_updated_at,
        "body_hmac": body_hmac,
        "outcome": outcome,
    }
    statement = insert(JiraCommentCursor).values(
        id=uuid.uuid4(), user_id=user_id, comment_id=comment_id, **values
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[JiraCommentCursor.user_id, JiraCommentCursor.comment_id],
            set_={**values, "updated_at": datetime.now(comment_updated_at.tzinfo)},
        )
    )


async def delete_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(delete(JiraCommentCursor).where(JiraCommentCursor.user_id == user_id))


async def delete_older_than(session: AsyncSession, cutoff: datetime) -> int:
    """Retention: rows whose comment was last changed before ``cutoff`` (all users)."""
    result = await session.execute(
        delete(JiraCommentCursor).where(JiraCommentCursor.comment_updated_at < cutoff)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def count_for_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(JiraCommentCursor.id).where(JiraCommentCursor.user_id == user_id)
    )
    return len(result.all())
