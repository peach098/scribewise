"""Per-user daily LLM budget with graceful degradation (issue #74).

Each user has two caps on today's LLM spend (``llm_spend``, summed over routes, per UTC day):

- **soft** (``SCRIBE_LLM_DAILY_SOFT_CAP_USD``, default $1.00): at or above it, drafts use the
  ``economy`` tier of ``compose.draft`` (:func:`drafting_tier`). ``users.llm_tier`` is not
  changed: the next UTC day drafts with the stored tier again. The other routes have no tiers.
- **hard** (``SCRIBE_LLM_DAILY_HARD_CAP_USD``, default $3.00): at or above it, drafting pauses
  until the next UTC day. A due turn is skipped with the category ``budget`` before the reply
  gate (no model call; listed in ``/skipped``), the composer refuses (:class:`DraftingPaused`:
  🔁 Regenerate, ✍️ Draft anyway), the pending-draft and dropped-turn classifiers are not asked,
  and a manual 🔄 Rebuild of the style profiles is refused. The user gets one notice per UTC day
  in the control chat (:meth:`BudgetGuard.notify_paused`, remembered as an ``activity_records``
  row ``budget_paused``, so a restart never repeats it).

Not paused: commitment extraction from the user's own outgoing messages and Jira comments (cheap,
bounded by what the user writes, and a skipped extraction is never redone, so a promise would be
lost), the weekly profile refresh (it runs Sunday 02:00 UTC, early in a new budget day) and the
first profile build at setup.

A cap of 0 disables it. The check reads today's total with one indexed query
(:func:`scribe.db.repos.llm_spend.spent_on`) before each turn; a turn already being drafted
finishes, so a day can end slightly above the hard cap (by one turn's cost).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import ActivityKind, ActivityRecord, User
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.i18n import Msg
from scribe.llm.provider import Tier
from scribe.logging import get_logger

log = get_logger("scribe.budget")

BudgetLevel = Literal["ok", "soft", "hard"]
Notifier = Callable[[uuid.UUID, Msg], Awaitable[None]]


class DraftingPaused(Exception):
    """The user's daily hard cap is reached: no draft until the next UTC day."""


@dataclass(frozen=True)
class BudgetCaps:
    soft_usd: float
    hard_usd: float

    @classmethod
    def from_settings(cls, settings: Settings) -> BudgetCaps:
        return cls(settings.llm_daily_soft_cap_usd, settings.llm_daily_hard_cap_usd)

    @property
    def enabled(self) -> bool:
        return self.soft_usd > 0 or self.hard_usd > 0

    def level(self, spent: Decimal | float) -> BudgetLevel:
        """``hard`` at or above the hard cap, ``soft`` at or above the soft cap, else ``ok``;
        a cap of 0 never applies."""
        amount = Decimal(str(spent))
        if self.hard_usd > 0 and amount >= Decimal(str(self.hard_usd)):
            return "hard"
        if self.soft_usd > 0 and amount >= Decimal(str(self.soft_usd)):
            return "soft"
        return "ok"


def utc_day(at: datetime) -> date:
    """The budget day of ``at``: its UTC date (the ``llm_spend`` day)."""
    return (at.astimezone(UTC) if at.tzinfo is not None else at).date()


def next_reset(now: datetime) -> datetime:
    """When the budget day of ``now`` ends: the next midnight UTC."""
    return datetime.combine(utc_day(now) + timedelta(days=1), time(0, 0), tzinfo=UTC)


def resume_time(now: datetime, timezone: str) -> str:
    """``HH:MM`` of :func:`next_reset` in the user's time zone (for the notices)."""
    return f"{next_reset(now).astimezone(ZoneInfo(timezone)):%H:%M}"


def drafting_tier(stored: Tier, level: BudgetLevel) -> Tier:
    """The ``compose.draft`` tier for a draft: ``economy`` from the soft cap on, else the
    user's stored tier."""
    return "economy" if level != "ok" else stored


async def budget_level(
    session: AsyncSession, user_id: uuid.UUID, caps: BudgetCaps, now: datetime
) -> BudgetLevel:
    """The user's level today; no query when both caps are off."""
    if not caps.enabled:
        return "ok"
    return caps.level(await llm_spend_repo.spent_on(session, user_id, utc_day(now)))


async def level_for(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    caps: BudgetCaps,
    now: datetime,
) -> BudgetLevel:
    if not caps.enabled:
        return "ok"
    async with session_factory() as session:
        return await budget_level(session, user_id, caps, now)


async def paused_now(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    now: datetime,
) -> bool:
    """The user's daily hard cap is reached (the control bot's check before a model call)."""
    caps = BudgetCaps.from_settings(settings)
    return await level_for(session_factory, user_id, caps, now) == "hard"


def paused_alert(timezone: str, now: datetime) -> Msg:
    """The short text for a refused 🔁 Regenerate, ✍️ Draft anyway or 🔄 Rebuild."""
    return Msg("budget.paused_alert", time=resume_time(now, timezone))


class BudgetGuard:
    """The budget as the scheduler and the reply gate see it: the level of a user now, and the
    once-a-day notice when drafting pauses."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caps: BudgetCaps,
        *,
        notifier: Notifier | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self.caps = caps
        self._notifier = notifier
        self._clock = clock

    async def level(self, user_id: uuid.UUID) -> BudgetLevel:
        return await level_for(self._session_factory, user_id, self.caps, self._clock())

    async def paused(self, user_id: uuid.UUID) -> bool:
        return await self.level(user_id) == "hard"

    async def notify_paused(self, user_id: uuid.UUID) -> bool:
        """Send the "drafting paused until tomorrow" notice unless it was sent this UTC day;
        whether it was sent now. The user row is locked while checking, so two turns paused at
        the same moment send one notice. Never raises."""
        now = self._clock()
        day = utc_day(now)
        try:
            async with self._session_factory() as session:
                user = await session.scalar(
                    select(User).where(User.id == user_id).with_for_update()
                )
                if user is None:
                    return False
                start = datetime.combine(day, time(0, 0), tzinfo=UTC)
                sent = await session.scalar(
                    select(
                        exists().where(
                            ActivityRecord.user_id == user_id,
                            ActivityRecord.kind == ActivityKind.budget_paused,
                            ActivityRecord.at >= start,
                        )
                    )
                )
                if sent:
                    return False
                await activity_repo.record(
                    session,
                    user_id,
                    ActivityKind.budget_paused,
                    at=now,
                    meta={"day": day.isoformat()},
                )
                timezone = user.timezone
                await session.commit()
        except Exception as exc:
            log.error("budget_notice_failed", user_id=str(user_id), error=type(exc).__name__)
            return False
        log.info("budget_drafting_paused", user_id=str(user_id), day=day.isoformat())
        if self._notifier is not None:
            try:
                await self._notifier(
                    user_id, Msg("budget.paused_notice", time=resume_time(now, timezone))
                )
            except Exception as exc:
                log.error("budget_notice_failed", user_id=str(user_id), error=type(exc).__name__)
        return True


__all__ = [
    "BudgetCaps",
    "BudgetGuard",
    "BudgetLevel",
    "DraftingPaused",
    "budget_level",
    "drafting_tier",
    "level_for",
    "next_reset",
    "paused_alert",
    "paused_now",
    "resume_time",
    "utc_day",
]
