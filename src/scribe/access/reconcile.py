"""Access reconciliation job (specs/005-beta-subscription-access/research.md Q4, Q6, Q7).

``access_reconcile`` runs every minute (and once at start). While ``SCRIBE_ACCESS_GATE`` is on it
compares every user's computed access (:func:`scribe.access.policy.status`, keyed by
``users.telegram_control_chat_id``) with ``users.access_suspended_at``:

- **lost** (no access, column null): set the column, expire the user's ``pending`` drafts (turns
  ``handle_manually``, as the deadline expiry does), write an ``access_lost`` event. After the
  commit: stop the user's Telegram client and send ``access.ended_notice`` once.
- **regained** (access, column set): clear the column, write ``access_restored``. After the
  commit: start the client (a no-op without an active session) and send
  ``access.restored_notice``, plus ``access.reconnect_hint`` when the session is not active.
- **ending** (an active subscription, not yet reminded, ending within 3 days, and nothing else -
  beta, operator, a chained subscription - extends access past its end): set ``reminded_at``,
  write ``subscription_reminded``. After the commit: send ``access.sub_reminder`` to people that
  have an account (a plugin's notice decorator may change it and add buttons, docs/plugins.md).

With the gate **off** nobody is gated: every suspended user is regained (event meta
``{"reason": "gate_off"}``), so turning the gate off undoes its suspensions. Nothing else happens.

``users.state`` is never touched, so a user who paused themselves stays paused. A second run with
nothing changed does nothing (idempotent). Logs carry ids and counts only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import policy
from scribe.access import repo as access_repo
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    Draft,
    DraftState,
    SessionStatus,
    Subscription,
    TelegramSession,
    User,
)
from scribe.drafting.budget import NoticeDecorator
from scribe.i18n import Msg, format_day_month_year
from scribe.logging import get_logger
from scribe.scheduling.deadlines import expire_draft
from scribe.scheduling.scheduler import Every, Scheduler

log = get_logger("scribe.access")

RECONCILE_JOB = "access_reconcile"
RECONCILE_SCHEDULE = Every(timedelta(seconds=60))
REASON_BETA_REMOVED = "beta_removed"
REASON_SUBSCRIPTION_ENDED = "subscription_ended"
REASON_GATE_OFF = "gate_off"
REMIND_BEFORE = timedelta(days=3)  # FR-016


class ClientLifecycle(Protocol):
    """The part of the client pool (``MessagingGateway``) this job uses."""

    async def start(self, user_id: uuid.UUID) -> None: ...
    async def stop(self, user_id: uuid.UUID) -> None: ...


@dataclass(frozen=True)
class ReconcileResult:
    checked: int = 0
    lost: int = 0
    regained: int = 0
    reminded: int = 0


@dataclass(frozen=True)
class _Change:
    user_id: uuid.UUID
    chat_id: int
    lang: str
    reason: str | None = None  # lost only
    session_active: bool = True  # regained only


@dataclass(frozen=True)
class _Reminder:
    chat_id: int
    lang: str
    timezone: str
    ends_at: datetime
    user_id: uuid.UUID | None = None


async def reconcile_access(
    session_factory: async_sessionmaker[AsyncSession],
    pool: ClientLifecycle,
    bot: Bot,
    settings: Settings,
    *,
    clock: Clock = utcnow,
    decorate: NoticeDecorator | None = None,
) -> ReconcileResult:
    """One run of the job; see the module docstring. ``decorate``: a plugin's change to the
    reminder."""
    if not settings.access_gate:
        return await _restore_all(session_factory, pool, bot, now=clock())
    now = clock()
    lost: list[_Change] = []
    regained: list[_Change] = []
    async with session_factory() as session:
        users = list((await session.scalars(select(User).order_by(User.created_at))).all())
        statuses = await access_repo.bulk_status(session, settings, users, now=now)
        for user in users:
            telegram_id = user.telegram_control_chat_id
            status = statuses[user.id]
            if not status.has_access and user.access_suspended_at is None:
                subs = await access_repo.subscriptions_for(session, telegram_id)
                reason = await _loss_reason(session, telegram_id, subs, now)
                await _suspend(session, user, reason, now)
                lost.append(_Change(user.id, telegram_id, user.language, reason=reason))
            elif status.has_access and user.access_suspended_at is not None:
                regained.append(await _restore(session, user, now))
        reminded, reminders = await _remind(session, settings, users, now)
        await session.commit()

    for change in lost:
        await _after_lost(pool, bot, settings, change)
    for change in regained:
        await _after_regained(pool, bot, change)
    for reminder in reminders:
        await _send_reminder(bot, settings, reminder, decorate)
    result = ReconcileResult(
        checked=len(users), lost=len(lost), regained=len(regained), reminded=reminded
    )
    log.info(
        "access_reconcile",
        checked=result.checked,
        lost=result.lost,
        regained=result.regained,
        reminded=result.reminded,
    )
    return result


async def _restore_all(
    session_factory: async_sessionmaker[AsyncSession],
    pool: ClientLifecycle,
    bot: Bot,
    *,
    now: datetime,
) -> ReconcileResult:
    """Gate off: regain every suspended user (the gate's suspensions no longer apply)."""
    regained: list[_Change] = []
    async with session_factory() as session:
        users = (
            await session.scalars(
                select(User)
                .where(User.access_suspended_at.is_not(None))
                .order_by(User.created_at)
                .with_for_update()
            )
        ).all()
        for user in users:
            regained.append(await _restore(session, user, now, meta={"reason": REASON_GATE_OFF}))
        await session.commit()
    for change in regained:
        await _after_regained(pool, bot, change)
    if not regained:
        return ReconcileResult()
    result = ReconcileResult(checked=len(regained), regained=len(regained))
    log.info("access_reconcile_gate_off", regained=result.regained)
    return result


def register_access_reconcile(
    scheduler: Scheduler,
    session_factory: async_sessionmaker[AsyncSession],
    pool: ClientLifecycle,
    bot: Bot,
    settings: Settings,
    clock: Clock = utcnow,
    decorate: NoticeDecorator | None = None,
) -> None:
    """Register ``access_reconcile`` (every 60 s, due at once at start)."""

    async def job() -> None:
        await reconcile_access(session_factory, pool, bot, settings, clock=clock, decorate=decorate)

    scheduler.register_job(RECONCILE_JOB, RECONCILE_SCHEDULE, job, run_at_start=True)


# --- inside the transaction ---------------------------------------------------------------------


async def _loss_reason(
    session: AsyncSession, telegram_id: int, subs: list[Subscription], now: datetime
) -> str:
    """Whatever ended most recently: the beta entry's removal or a subscription's end."""
    beta_removed_at = await session.scalar(
        select(func.max(AccessEvent.at)).where(
            AccessEvent.telegram_id == telegram_id,
            AccessEvent.kind == AccessEventKind.beta_removed,
        )
    )
    ends = [
        sub.cancelled_at if sub.cancelled_at is not None else sub.ends_at
        for sub in subs
        if sub.cancelled_at is not None or sub.ends_at <= now
    ]
    subscription_ended_at = max(ends) if ends else None
    if subscription_ended_at is not None and (
        beta_removed_at is None or subscription_ended_at > beta_removed_at
    ):
        return REASON_SUBSCRIPTION_ENDED
    return REASON_BETA_REMOVED  # also a pre-gate user who was never listed


async def _suspend(session: AsyncSession, user: User, reason: str, now: datetime) -> None:
    user.access_suspended_at = now
    pending = await session.scalars(
        select(Draft)
        .where(Draft.user_id == user.id, Draft.state == DraftState.pending)
        .with_for_update()
    )
    for draft in pending.all():
        await expire_draft(session, draft, now=now)
    await access_repo.record_event(
        session,
        user.telegram_control_chat_id,
        AccessEventKind.access_lost,
        actor="system",
        meta={"reason": reason},
        now=now,
    )


async def _restore(
    session: AsyncSession, user: User, now: datetime, *, meta: dict[str, str] | None = None
) -> _Change:
    user.access_suspended_at = None
    await access_repo.record_event(
        session,
        user.telegram_control_chat_id,
        AccessEventKind.access_restored,
        actor="system",
        meta=meta,
        now=now,
    )
    active = await _session_active(session, user.id)
    return _Change(user.id, user.telegram_control_chat_id, user.language, session_active=active)


async def _remind(
    session: AsyncSession, settings: Settings, users: list[User], now: datetime
) -> tuple[int, list[_Reminder]]:
    """Mark the subscriptions due for the 3-day reminder: how many, and the messages to send
    (one a person).

    Due: active, not reminded, ``ends_at - now <= 3 days``, and access does not go on past
    ``ends_at`` - no beta entry, not an operator, no other open subscription starting by then and
    ending later (a chained one). People without an account are marked but not messaged.
    """
    due = list(
        (
            await session.scalars(
                select(Subscription)
                .where(
                    Subscription.cancelled_at.is_(None),
                    Subscription.reminded_at.is_(None),
                    Subscription.starts_at <= now,
                    Subscription.ends_at > now,
                    Subscription.ends_at <= now + REMIND_BEFORE,
                )
                .order_by(Subscription.ends_at)
                .with_for_update()
            )
        ).all()
    )
    if not due:
        return 0, []
    ids = sorted({sub.telegram_id for sub in due})
    betas = await access_repo.betas_by_id(session, ids)
    subs = await access_repo.subscriptions_by_id(session, ids)
    accounts = {user.telegram_control_chat_id: user for user in users}
    reminders: dict[int, _Reminder] = {}
    marked = 0
    for sub in due:
        telegram_id = sub.telegram_id
        if telegram_id in betas or policy.is_operator(telegram_id, settings):
            continue
        if any(_extends(other, sub) for other in subs.get(telegram_id, [])):
            continue
        sub.reminded_at = now
        marked += 1
        await access_repo.record_event(
            session,
            telegram_id,
            AccessEventKind.subscription_reminded,
            actor="system",
            meta={"subscription_id": sub.id, "ends_at": sub.ends_at},
            now=now,
        )
        user = accounts.get(telegram_id)
        if user is not None and telegram_id not in reminders:
            reminders[telegram_id] = _Reminder(
                telegram_id, user.language, user.timezone, sub.ends_at, user.id
            )
    return marked, list(reminders.values())


def _extends(other: Subscription, sub: Subscription) -> bool:
    """``other`` keeps access going past ``sub``'s end (overlapping or chained, not cancelled)."""
    return (
        other.id != sub.id
        and other.cancelled_at is None
        and other.starts_at <= sub.ends_at
        and other.ends_at > sub.ends_at
    )


async def _session_active(session: AsyncSession, user_id: uuid.UUID) -> bool:
    status = await session.scalar(
        select(TelegramSession.status).where(TelegramSession.user_id == user_id)
    )
    return status is None or status == SessionStatus.active  # no session: nothing to reconnect


# --- after the commit ---------------------------------------------------------------------------


async def _after_lost(pool: ClientLifecycle, bot: Bot, settings: Settings, change: _Change) -> None:
    try:
        await pool.stop(change.user_id)
    except Exception as exc:
        log.warning(
            "access_client_stop_failed", user_id=str(change.user_id), error=type(exc).__name__
        )
    text = Msg("access.ended_notice", reason_text=Msg(f"access.reason_{change.reason}")).render(
        change.lang
    )
    if settings.subscription_contact:
        line = Msg("access.subscription_line", subscription_contact=settings.subscription_contact)
        text = f"{text}\n{line.render(change.lang)}"
    await _notify(bot, change, text, "access_lost")


async def _after_regained(pool: ClientLifecycle, bot: Bot, change: _Change) -> None:
    try:
        await pool.start(change.user_id)
    except Exception as exc:
        log.warning(
            "access_client_start_failed", user_id=str(change.user_id), error=type(exc).__name__
        )
    text = Msg("access.restored_notice").render(change.lang)
    if not change.session_active:
        text = f"{text}\n{Msg('access.reconnect_hint').render(change.lang)}"
    await _notify(bot, change, text, "access_restored")


async def _send_reminder(
    bot: Bot, settings: Settings, reminder: _Reminder, decorate: NoticeDecorator | None = None
) -> None:
    """Send ``access.sub_reminder`` (through ``decorate`` when set); a Telegram error or a
    failing decorator is logged and skipped."""
    try:
        zone = ZoneInfo(reminder.timezone)
    except (ValueError, KeyError):  # a bad stored zone never blocks the reminder
        zone = ZoneInfo("UTC")
    day = format_day_month_year(reminder.ends_at.astimezone(zone).date(), reminder.lang)
    renew = settings.subscription_contact or Msg("access.renew_send_start")
    message = Msg("access.sub_reminder", date=day, renew=renew)
    try:
        markup = None
        if decorate is not None and reminder.user_id is not None:
            message, factory = await decorate(reminder.user_id, message)
            markup = factory(reminder.lang) if factory is not None else None
        await bot.send_message(reminder.chat_id, message.render(reminder.lang), reply_markup=markup)
    except Exception as exc:  # blocked bot, network: one user never stops the others
        log.warning("access_reminder_failed", error=type(exc).__name__)


async def _notify(bot: Bot, change: _Change, text: str, transition: str) -> None:
    try:
        await bot.send_message(change.chat_id, text)
    except Exception as exc:  # blocked bot, network: one user never stops the others
        log.warning(
            "access_notice_failed",
            user_id=str(change.user_id),
            transition=transition,
            error=type(exc).__name__,
        )
        return
    log.info("access_notice_sent", user_id=str(change.user_id), transition=transition)
