"""Scheduler steps 2 and 3 (US2, FR-032): draft reminders and expiry.

- Reminder: a ``pending`` draft whose ``remind_at`` has passed and that was never reminded gets
  ``reminded_at`` set and, after the step commits, one reply to its card in the control chat:
  "⏰ Still waiting for your decision on this draft." A draft that is already past its expiry
  (the service was down) is marked reminded silently; step 3 expires it in the same tick.
- Expiry: a ``pending`` draft whose ``expires_at`` has passed becomes ``expired`` (finalized),
  its turn ``handle_manually``, an ``expired`` activity record is written and, after commit,
  the card is edited to "⌛ Expired — handle manually" without buttons.

Both run inside the scheduler's ``FOR UPDATE SKIP LOCKED`` transaction; control-chat messages
are sent only after it commits. Nothing here can send to a contact (Principle I): an expired
draft can no longer be approved (review callbacks and ``send.py`` refuse it).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Protocol

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.clock import Clock, utcnow
from scribe.db.models import ActivityKind, ConversationTurn, Draft, DraftState, TurnState
from scribe.db.repos import activity as activity_repo
from scribe.i18n import Msg
from scribe.logging import get_logger
from scribe.scheduling.scheduler import AfterCommit, Scheduler

log = get_logger("scribe.scheduler")

# Rendered by the card presenter in the user's stored language (users.language).
REMINDER_TEXT = Msg("notice.reminder")
EXPIRED_STATUS = Msg("status.expired")


class ReviewCards(Protocol):
    async def remind(self, user_id: uuid.UUID, draft_id: uuid.UUID, text: Msg) -> None: ...
    async def show_status(self, user_id: uuid.UUID, draft_id: uuid.UUID, status: Msg) -> None: ...


async def expire_draft(session: AsyncSession, draft: Draft, *, now: datetime) -> None:
    """Mark a ``pending`` draft ``expired``, its turn ``handle_manually``, and record it.

    Shared by the deadline step, the access reconciliation (specs/005 research Q7) and the card
    presenter for a suspended user. No card is touched here; the caller does not flush.
    """
    draft.state = DraftState.expired
    draft.finalized_at = now
    await session.execute(
        update(ConversationTurn)
        .where(
            ConversationTurn.id == draft.conversation_turn_id,
            ConversationTurn.state == TurnState.pending_review,
        )
        .values(state=TurnState.handle_manually)
    )
    await activity_repo.record(
        session,
        draft.user_id,
        ActivityKind.expired,
        at=now,
        draft_id=draft.id,
        conversation_turn_id=draft.conversation_turn_id,
    )


class ReviewDeadlines:
    def __init__(self, cards: ReviewCards | None, *, clock: Clock = utcnow) -> None:
        self._cards = cards
        self._clock = clock

    async def remind(self, session: AsyncSession, drafts: Sequence[Draft]) -> AfterCommit:
        now = self._clock()
        due: list[tuple[uuid.UUID, uuid.UUID]] = []
        for draft in drafts:
            draft.reminded_at = now
            if draft.expires_at > now:
                due.append((draft.user_id, draft.id))
        await session.flush()
        log.info("draft_reminders", count=len(due), silent=len(drafts) - len(due))

        async def notify() -> None:
            for user_id, draft_id in due:
                await self._card_call(self._remind_one, user_id, draft_id)

        return notify

    async def expire(self, session: AsyncSession, drafts: Sequence[Draft]) -> AfterCommit:
        now = self._clock()
        expired: list[tuple[uuid.UUID, uuid.UUID]] = []
        for draft in drafts:
            await expire_draft(session, draft, now=now)
            expired.append((draft.user_id, draft.id))
        await session.flush()
        log.info("drafts_expired", count=len(expired))

        async def notify() -> None:
            for user_id, draft_id in expired:
                await self._card_call(self._expire_one, user_id, draft_id)

        return notify

    async def _remind_one(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> None:
        if self._cards is not None:
            await self._cards.remind(user_id, draft_id, REMINDER_TEXT)

    async def _expire_one(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> None:
        if self._cards is not None:
            await self._cards.show_status(user_id, draft_id, EXPIRED_STATUS)

    @staticmethod
    async def _card_call(
        func: Callable[[uuid.UUID, uuid.UUID], Awaitable[None]],
        user_id: uuid.UUID,
        draft_id: uuid.UUID,
    ) -> None:
        try:
            await func(user_id, draft_id)
        except Exception as exc:  # one failed card never blocks the others
            log.warning(
                "review_card_update_failed", draft_id=str(draft_id), error=type(exc).__name__
            )


def register_review_deadlines(
    scheduler: Scheduler,
    cards: ReviewCards | None,
    *,
    clock: Clock = utcnow,
) -> ReviewDeadlines:
    """Plug reminders (step 2) and expiry (step 3) into ``scheduler``."""
    deadlines = ReviewDeadlines(cards, clock=clock)
    scheduler.on_reminders(deadlines.remind)
    scheduler.on_expiries(deadlines.expire)
    return deadlines
