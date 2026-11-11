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
finishes, so a day can end slightly above the hard cap (by one turn's cost). The routes in
``SCRIBE_LLM_BUDGET_EXCLUDED_ROUTES`` are not counted (:attr:`BudgetCaps.excluded_routes`).

Usage limits per user (docs/plugins.md): the caps of a user and the other limits come from a
:class:`LimitsResolver`. The default, :class:`SettingsLimitsResolver`, gives every user the
deployment caps and no other limit (no query, the behaviour described above). A plugin may
install its own; then :attr:`UsageLimits.max_drafts_per_day` pauses drafting for the rest of the
UTC day once that many drafts were created (reason ``drafts``, notice ``budget.drafts_notice``),
:attr:`UsageLimits.max_work_chats` limits the enabled work chats and
:attr:`UsageLimits.profile_refresh_allowed` / :attr:`UsageLimits.profile_refresh_default` steer
the automatic profile refresh and 🔄 Rebuild. A notice decorator (:data:`NoticeDecorator`) may
change the "drafting paused" notice and add buttons to it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import ActivityKind, ActivityRecord, Draft, User
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.i18n import Msg
from scribe.llm.provider import Tier
from scribe.logging import get_logger

log = get_logger("scribe.budget")

BudgetLevel = Literal["ok", "soft", "hard"]
PauseReason = Literal["cap", "drafts"]
MarkupFactory = Callable[[str], InlineKeyboardMarkup]  # the user's language -> buttons
# A plugin's decoration of a background notice: (user id, message) -> (message, buttons).
NoticeDecorator = Callable[[uuid.UUID, Msg], Awaitable[tuple[Msg, MarkupFactory | None]]]


class Notifier(Protocol):
    def __call__(
        self, user_id: uuid.UUID, message: Msg, /, reply_markup: MarkupFactory | None = None
    ) -> Awaitable[None]: ...


class DraftingPaused(Exception):
    """The user's daily hard cap is reached: no draft until the next UTC day."""


@dataclass(frozen=True)
class BudgetCaps:
    soft_usd: float
    hard_usd: float
    # Routes whose spend the cap check does not count (SCRIBE_LLM_BUDGET_EXCLUDED_ROUTES).
    excluded_routes: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_settings(cls, settings: Settings) -> BudgetCaps:
        return cls(
            settings.llm_daily_soft_cap_usd,
            settings.llm_daily_hard_cap_usd,
            frozenset(settings.llm_budget_excluded_routes),
        )

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


@dataclass(frozen=True)
class UsageLimits:
    """What one user may use now. ``None`` limits are unlimited."""

    caps: BudgetCaps
    max_drafts_per_day: int | None = None  # drafts created per UTC day
    max_work_chats: int | None = None  # enabled work chats; work account mode needs None
    profile_refresh_allowed: bool = True  # automatic refresh and 🔄 Rebuild possible at all
    profile_refresh_default: bool = True  # automatic refresh when the user has not chosen


class LimitsResolver(Protocol):
    async def limits_for(self, session: AsyncSession, user: User, now: datetime) -> UsageLimits:
        """The user's limits at ``now``; may read the database in ``session``."""
        ...


class SettingsLimitsResolver:
    """The default: the deployment caps for everyone and no other limit (no query)."""

    def __init__(self, caps: BudgetCaps, *, profile_refresh_default: bool = True) -> None:
        self.limits = UsageLimits(caps=caps, profile_refresh_default=profile_refresh_default)

    @classmethod
    def from_settings(cls, settings: Settings) -> SettingsLimitsResolver:
        return cls(
            BudgetCaps.from_settings(settings),
            profile_refresh_default=settings.profile_auto_refresh_default,
        )

    async def limits_for(self, session: AsyncSession, user: User, now: datetime) -> UsageLimits:
        return self.limits


class ExcludingRoutes:
    """A plugin's resolver with the deployment's excluded routes always applied to its caps."""

    def __init__(self, inner: LimitsResolver, excluded_routes: Collection[str]) -> None:
        self.inner = inner
        self.excluded_routes = frozenset(excluded_routes)

    async def limits_for(self, session: AsyncSession, user: User, now: datetime) -> UsageLimits:
        limits = await self.inner.limits_for(session, user, now)
        if self.excluded_routes <= limits.caps.excluded_routes:
            return limits
        caps = replace(
            limits.caps, excluded_routes=limits.caps.excluded_routes | self.excluded_routes
        )
        return replace(limits, caps=caps)


def default_resolver(limits: LimitsResolver | None, settings: Settings) -> LimitsResolver:
    """``limits``, or the deployment's :class:`SettingsLimitsResolver` when none is given."""
    return limits if limits is not None else SettingsLimitsResolver.from_settings(settings)


async def user_limits(
    session: AsyncSession, user: uuid.UUID | User, limits: LimitsResolver, now: datetime
) -> UsageLimits:
    """The limits of ``user`` (an id or the row). The default resolver needs no query; for
    another one a missing user has no caps and no limits."""
    if isinstance(limits, SettingsLimitsResolver):
        return limits.limits
    row = user if isinstance(user, User) else await session.get(User, user)
    if row is None:
        return UsageLimits(caps=BudgetCaps(0, 0))
    return await limits.limits_for(session, row, now)


async def drafts_today(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> int:
    """Drafts created for the user since the start of the UTC day of ``now``."""
    start = datetime.combine(utc_day(now), time(0, 0), tzinfo=UTC)
    count = await session.scalar(
        select(func.count())
        .select_from(Draft)
        .where(Draft.user_id == user_id, Draft.created_at >= start)
    )
    return int(count or 0)


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
    spent = await llm_spend_repo.spent_on(
        session, user_id, utc_day(now), exclude_routes=caps.excluded_routes
    )
    return caps.level(spent)


async def user_budget_level(
    session: AsyncSession, user_id: uuid.UUID, limits: LimitsResolver, now: datetime
) -> BudgetLevel:
    """:func:`budget_level` with the user's caps from ``limits``."""
    usage = await user_limits(session, user_id, limits, now)
    return await budget_level(session, user_id, usage.caps, now)


async def level_for(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    limits: LimitsResolver,
    now: datetime,
) -> BudgetLevel:
    if isinstance(limits, SettingsLimitsResolver) and not limits.limits.caps.enabled:
        return "ok"
    async with session_factory() as session:
        return await user_budget_level(session, user_id, limits, now)


async def paused_now(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    now: datetime,
    limits: LimitsResolver | None = None,
) -> bool:
    """The user's daily hard cap is reached (the control bot's check before a model call);
    ``limits`` defaults to the deployment caps."""
    resolver = default_resolver(limits, settings)
    return await level_for(session_factory, user_id, resolver, now) == "hard"


def paused_alert(timezone: str, now: datetime) -> Msg:
    """The short text for a refused 🔁 Regenerate, ✍️ Draft anyway or 🔄 Rebuild."""
    return Msg("budget.paused_alert", time=resume_time(now, timezone))


NOTICE_KEYS: dict[PauseReason, str] = {
    "cap": "budget.paused_notice",
    "drafts": "budget.drafts_notice",
}


class BudgetGuard:
    """The budget as the scheduler and the reply gate see it: the level of a user now, the
    draft-count limit, and the once-a-day notice when drafting pauses (``decorate``: a plugin's
    change to that notice)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        limits: LimitsResolver,
        *,
        notifier: Notifier | None = None,
        clock: Clock = utcnow,
        decorate: NoticeDecorator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.limits = limits
        self._notifier = notifier
        self._clock = clock
        self._decorate = decorate

    async def level(self, user_id: uuid.UUID) -> BudgetLevel:
        return await level_for(self._session_factory, user_id, self.limits, self._clock())

    async def paused(self, user_id: uuid.UUID) -> bool:
        return await self.pause_reason(user_id) is not None

    async def pause_reason(self, user_id: uuid.UUID) -> PauseReason | None:
        """Why drafting is paused for the user now: ``cap`` (the daily hard cap), ``drafts``
        (the day's draft limit), or None. The default resolver needs at most one query."""
        if isinstance(self.limits, SettingsLimitsResolver):
            return "cap" if await self.level(user_id) == "hard" else None
        now = self._clock()
        async with self._session_factory() as session:
            usage = await user_limits(session, user_id, self.limits, now)
            if await budget_level(session, user_id, usage.caps, now) == "hard":
                return "cap"
            limit = usage.max_drafts_per_day
            if limit is not None and await drafts_today(session, user_id, now) >= limit:
                return "drafts"
        return None

    async def notify_paused(self, user_id: uuid.UUID, reason: PauseReason = "cap") -> bool:
        """Send the "drafting paused until tomorrow" notice (``reason``: its text) unless one
        was sent this UTC day; whether it was sent now. The user row is locked while checking,
        so two turns paused at the same moment send one notice. Never raises."""
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
                    # The cap's records stay as before; another reason is named.
                    meta={
                        "day": day.isoformat(),
                        **({} if reason == "cap" else {"reason": reason}),
                    },
                )
                timezone = user.timezone
                await session.commit()
        except Exception as exc:
            log.error("budget_notice_failed", user_id=str(user_id), error=type(exc).__name__)
            return False
        log.info("budget_drafting_paused", user_id=str(user_id), day=day.isoformat(), reason=reason)
        if self._notifier is not None:
            try:
                message = Msg(NOTICE_KEYS[reason], time=resume_time(now, timezone))
                if self._decorate is None:
                    await self._notifier(user_id, message)
                else:
                    message, markup = await self._decorate(user_id, message)
                    await self._notifier(user_id, message, reply_markup=markup)
            except Exception as exc:
                log.error("budget_notice_failed", user_id=str(user_id), error=type(exc).__name__)
        return True


__all__ = [
    "NOTICE_KEYS",
    "BudgetCaps",
    "BudgetGuard",
    "BudgetLevel",
    "DraftingPaused",
    "ExcludingRoutes",
    "LimitsResolver",
    "MarkupFactory",
    "NoticeDecorator",
    "Notifier",
    "PauseReason",
    "SettingsLimitsResolver",
    "UsageLimits",
    "budget_level",
    "default_resolver",
    "drafting_tier",
    "drafts_today",
    "level_for",
    "next_reset",
    "paused_alert",
    "paused_now",
    "resume_time",
    "user_budget_level",
    "user_limits",
    "utc_day",
]
