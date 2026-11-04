"""``draft_decisions`` rows (specs/014 research A5): what the user did with each draft.

One row per decision, written in the transaction of the action itself (``sending/send.py`` for
a send, the review callbacks for reject / regenerate / stop). The rows hold ids, a code and a
flag, never content (Principle II); ``retention/purge.py`` deletes them after 31 days. They feed
two readers: the per-chat "sent unchanged" rate behind the auto-send offer (FR-014/FR-015) and
the intervention count behind the suggestion to turn it off (FR-020).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import Integer, case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import DraftDecision, DraftDecisionKind

# Not decisions for the rate: the countdown's own outcomes (the follow-up Send now / reject on a
# cancelled draft is the user's decision).
_NOT_COUNTED = (DraftDecisionKind.auto_sent, DraftDecisionKind.auto_cancelled)
# A draft the user had to act on although it was set to go out by itself. Send now without
# changes and the automatic send are not interventions.
_INTERVENTIONS = (
    DraftDecisionKind.auto_cancelled,
    DraftDecisionKind.sent_edited,
    DraftDecisionKind.rejected,
    DraftDecisionKind.regenerated,
)


class ChatRate(NamedTuple):
    unchanged: int
    total: int


async def record(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    draft_id: uuid.UUID | None,
    decision: DraftDecisionKind,
    during_auto_send: bool,
    at: datetime,
) -> None:
    session.add(
        DraftDecision(
            user_id=user_id,
            work_chat_id=work_chat_id,
            draft_id=draft_id,
            decision=decision,
            during_auto_send=during_auto_send,
            at=at,
        )
    )
    await session.flush()


async def chat_rate(
    session: AsyncSession, *, user_id: uuid.UUID, work_chat_id: uuid.UUID, since: datetime
) -> ChatRate:
    """(sent unchanged, all decisions) of the chat after ``since``, without the countdown's own
    rows. ``(0, 0)`` when there are none."""
    row = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (DraftDecision.decision == DraftDecisionKind.sent_unchanged, 1), else_=0
                        )
                    ),
                    0,
                ).cast(Integer),
                func.count(),
            ).where(
                DraftDecision.user_id == user_id,
                DraftDecision.work_chat_id == work_chat_id,
                DraftDecision.at > since,
                DraftDecision.decision.not_in(_NOT_COUNTED),
            )
        )
    ).one()
    return ChatRate(int(row[0]), int(row[1]))


async def recent_auto_outcomes(
    session: AsyncSession, *, user_id: uuid.UUID, work_chat_id: uuid.UUID, limit: int
) -> list[bool]:
    """One boolean per draft that had a countdown (``during_auto_send``), newest first: True when
    the user intervened (the draft has an ``auto_cancelled``, ``sent_edited``, ``rejected`` or
    ``regenerated`` row)."""
    intervened = func.bool_or(DraftDecision.decision.in_(_INTERVENTIONS))
    rows = await session.execute(
        select(intervened)
        .where(
            DraftDecision.user_id == user_id,
            DraftDecision.work_chat_id == work_chat_id,
            DraftDecision.during_auto_send.is_(True),
            DraftDecision.draft_id.is_not(None),
        )
        .group_by(DraftDecision.draft_id)
        .order_by(func.max(DraftDecision.at).desc(), DraftDecision.draft_id)
        .limit(limit)
    )
    return [bool(value) for value in rows.scalars().all()]


async def purge_older_than(session: AsyncSession, cutoff: datetime) -> int:
    """Delete every user's rows decided before ``cutoff`` (retention); returns how many."""
    result = await session.execute(
        delete(DraftDecision).where(DraftDecision.at < cutoff).returning(DraftDecision.id)
    )
    return len(result.scalars().all())
