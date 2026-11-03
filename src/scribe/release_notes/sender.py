"""Delivering release notes: the ``release_notes`` scheduler job (specs/011-release-notes
research R6-R9, plan.md "Sender loop").

Every 15 s (and once at start) the job runs :meth:`ReleaseNotesSender.run_once`: on its first
run in the process the startup check (:func:`.announce.ensure_auto_announcement`), then
:meth:`ReleaseNotesSender.drain`. The drain is one sequential sender per process, guarded by a
lock (a run that finds it taken returns at once):

1. A short transaction claims the oldest due ``pending`` delivery (``FOR UPDATE SKIP LOCKED``),
   checks the account and its access again (FR-010), computes the user's personal versions (the
   announcement's versions minus those already received) and the digest parts, and marks the
   row ``sending`` or ``skipped``.
2. Each remaining part is sent silently, as plain text (``disable_notification=True``, no parse
   mode), paced by :class:`Pacer`; a second short transaction records each accepted part, and
   ``sent`` after the last one.

``TelegramRetryAfter`` pauses the sender and retries the same part without counting an attempt.
Other errors end as ``blocked``, ``failed`` or a retry later with backoff (:func:`classify`).
A crash between Telegram accepting a part and the commit leaves the row in ``sending``;
:func:`recover_interrupted`, called before the scheduler starts, counts that part as sent, so
nobody gets a part twice (FR-016).

Logs carry delivery and user ids, versions, results, counts and error class names, never the
text, chat ids or usernames (FR-023).
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import repo as access_repo
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import (
    ReleaseAnnouncement,
    ReleaseDelivery,
    ReleaseDeliveryState,
    User,
)
from scribe.logging import get_logger
from scribe.release_notes import repo
from scribe.release_notes import version as versions
from scribe.release_notes.announce import ensure_auto_announcement
from scribe.release_notes.notes import build_parts, from_snapshot
from scribe.release_notes.version import Version, running_version
from scribe.scheduling.scheduler import Every, Scheduler

log = get_logger("scribe.release_notes")

JOB_NAME = "release_notes"
JOB_SCHEDULE = Every(timedelta(seconds=15))
PER_CHAT_GAP_S = 1.1  # Telegram allows about one message per second per chat
RETRY_AFTER_MARGIN_S = 1
BACKOFF = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
)

Sleep = Callable[[float], Awaitable[None]]


# --- pacing and error handling (research R7, R8) -----------------------------------------------


class Pacer:
    """At most ``rate_per_s`` sends a second overall, and at least :data:`PER_CHAT_GAP_S`
    between two sends to the same chat. Uses the injected clock and sleep. One instance may be
    shared by several senders (issue #46: the grammatical gender question uses the release
    notes pacer, so both together stay within one budget). A chat's own gap is waited out
    without the lock, so it does not hold up sends to other chats; the overall interval is
    waited under the lock. Only when another sender used the same chat meanwhile is the rest of
    that chat's gap also waited under the lock (rare; it keeps the wait a single pass)."""

    def __init__(self, rate_per_s: float, *, clock: Clock, sleep: Sleep) -> None:
        self.min_interval = timedelta(seconds=1 / rate_per_s)
        self._clock = clock
        self._sleep = sleep
        self._last: datetime | None = None
        self._last_by_chat: dict[int, datetime] = {}
        self._lock = asyncio.Lock()

    def _chat_wait(self, chat_id: int) -> float:
        """Seconds until ``chat_id``'s gap is over (0 when it is)."""
        last = self._last_by_chat.get(chat_id)
        if last is None:
            return 0.0
        return max(0.0, (last + timedelta(seconds=PER_CHAT_GAP_S) - self._clock()).total_seconds())

    async def wait(self, chat_id: int) -> None:
        # The chat's own gap first, without the lock: other chats go on meanwhile. Computed
        # once (no loop), so an injected clock that does not advance cannot make it spin.
        gap = self._chat_wait(chat_id)
        if gap > 0:
            await self._sleep(gap)
        async with self._lock:
            now = self._clock()
            earliest = now if self._last is None else max(now, self._last + self.min_interval)
            chat_last = self._last_by_chat.get(chat_id)
            if chat_last is not None:  # another sender used this chat meanwhile
                earliest = max(earliest, chat_last + timedelta(seconds=PER_CHAT_GAP_S))
            if earliest > now:
                await self._sleep((earliest - now).total_seconds())
            sent_at = max(self._clock(), earliest)
            self._last = sent_at
            self._last_by_chat[chat_id] = sent_at
            horizon = sent_at - timedelta(seconds=PER_CHAT_GAP_S)
            for old in [c for c, at in self._last_by_chat.items() if at <= horizon]:
                del self._last_by_chat[old]


def backoff(attempts: int) -> timedelta:
    """Delay after the ``attempts``-th failed attempt: 30 s, 2 min, 10 min, then 30 min."""
    return BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]


class Outcome(enum.StrEnum):
    retry_after = "retry_after"
    blocked = "blocked"
    failed = "failed"
    retryable = "retryable"


def classify(exc: BaseException) -> Outcome:
    """What a send error means for the delivery (research R8)."""
    if isinstance(exc, TelegramRetryAfter):
        return Outcome.retry_after
    if isinstance(exc, TelegramForbiddenError | TelegramNotFound):
        return Outcome.blocked
    if isinstance(exc, TelegramBadRequest):
        if "chat not found" in exc.message.lower():
            return Outcome.blocked
        return Outcome.failed
    return Outcome.retryable  # network, server and any other API error


# --- the sender ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Claimed:
    delivery_id: uuid.UUID
    announcement_id: uuid.UUID
    user_id: uuid.UUID
    chat_id: int
    parts: list[str]
    parts_sent: int


class ReleaseNotesSender:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        settings: Settings,
        *,
        clock: Clock = utcnow,
        sleep: Sleep = asyncio.sleep,
        running: Callable[[], Version | None] = running_version,
    ) -> None:
        self._session_factory = session_factory
        self._bot = bot
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._running = running
        self.pacer = Pacer(settings.release_notes_rate_per_s, clock=clock, sleep=sleep)
        self._lock = asyncio.Lock()
        self._checked = False

    async def run_once(self) -> None:
        """One run of the job: the startup check on the first run only, then the drain."""
        if not self._checked:
            await ensure_auto_announcement(
                self._session_factory, self._settings, now=self._clock(), running=self._running()
            )
            self._checked = True  # a failure above is retried by the scheduler
        await self.drain()

    async def drain(self) -> int:
        """Send every due delivery, one at a time; returns how many deliveries were handled.
        Returns 0 at once when another drain of this process is running."""
        if self._lock.locked():
            return 0
        handled = 0
        async with self._lock:
            while True:
                claimed = await self._claim()
                if claimed is None:
                    break
                handled += 1
                if isinstance(claimed, _Claimed):
                    await self._deliver(claimed)
        return handled

    async def _claim(self) -> _Claimed | bool | None:
        """tx1: ``None`` when nothing is due, ``True`` when the row was skipped."""
        settings = self._settings
        async with self._session_factory() as session:
            now = self._clock()
            delivery = await repo.claim_next_due(session, now)
            if delivery is None:
                return None
            user = await session.get(User, delivery.user_id)
            announcement = await session.get(ReleaseAnnouncement, delivery.announcement_id)
            assert announcement is not None  # FK
            reason: str | None = None
            parts: list[str] = []
            if user is None:
                reason = "no_account"
            elif settings.access_gate and not await _has_access(session, settings, user, now):
                reason = "no_access"
            else:
                received = await repo.received_versions(
                    session, user.id, exclude_announcement=announcement.id
                )
                personal = [
                    v for v in (versions.parse(raw) for raw in announcement.versions)
                    if v not in received
                ]  # fmt: skip
                if not personal:
                    reason = "already_received"
                else:
                    parts = build_parts(
                        from_snapshot(announcement.notes_json), personal, user.language
                    )
                    if not parts:
                        reason = "no_text"
            if reason is not None or user is None:
                delivery.state = ReleaseDeliveryState.skipped
                delivery.skip_reason = reason
                counts = await self._finish(session, announcement.id, now)
                await session.commit()
                log.info(
                    "release_notes_delivery",
                    delivery_id=str(delivery.id),
                    user_id=str(delivery.user_id),
                    result="skipped",
                    skip_reason=reason,
                )
                _log_finished(announcement.id, counts)
                return True
            delivery.state = ReleaseDeliveryState.sending
            delivery.parts_total = len(parts)
            claimed = _Claimed(
                delivery.id,
                announcement.id,
                user.id,
                user.telegram_control_chat_id,
                parts,
                min(delivery.parts_sent, len(parts)),
            )
            await session.commit()
        return claimed

    async def _deliver(self, claimed: _Claimed) -> None:
        index = claimed.parts_sent
        while index < len(claimed.parts):
            try:
                await self.pacer.wait(claimed.chat_id)
            except asyncio.CancelledError:  # shutdown before this part went out
                await asyncio.shield(self._release(claimed, index))
                raise
            try:
                await self._bot.send_message(
                    claimed.chat_id, claimed.parts[index], disable_notification=True
                )
            except Exception as exc:
                outcome = classify(exc)
                if outcome is Outcome.retry_after:
                    assert isinstance(exc, TelegramRetryAfter)
                    log.warning(
                        "release_notes_retry_after",
                        delivery_id=str(claimed.delivery_id),
                        retry_after=exc.retry_after,
                    )
                    await self._sleep(exc.retry_after + RETRY_AFTER_MARGIN_S)
                    continue
                await self._record_error(claimed, outcome, exc)
                return
            index += 1
            if not await self._record_part(claimed, index):
                return

    async def _release(self, claimed: _Claimed, parts_sent: int) -> None:
        """Back to ``pending`` when the sender stops while waiting, before a part was sent, so
        the restart sends it rather than counting it as ``unknown``."""
        async with self._session_factory() as session:
            delivery = await session.get(ReleaseDelivery, claimed.delivery_id)
            if delivery is not None and delivery.state is ReleaseDeliveryState.sending:
                delivery.state = ReleaseDeliveryState.pending
                delivery.parts_sent = parts_sent
                delivery.next_attempt_at = self._clock()
                await session.commit()

    async def _record_part(self, claimed: _Claimed, parts_sent: int) -> bool:
        """tx2: one more part accepted; ``sent`` after the last one. False when the row is
        gone (the user deleted their data meanwhile)."""
        async with self._session_factory() as session:
            now = self._clock()
            delivery = await session.get(ReleaseDelivery, claimed.delivery_id)
            if delivery is None:
                return False
            delivery.parts_sent = parts_sent
            done = parts_sent >= len(claimed.parts)
            counts = None
            if done:
                delivery.state = ReleaseDeliveryState.sent
                delivery.delivered_at = now
                counts = await self._finish(session, claimed.announcement_id, now)
            await session.commit()
        if done:
            log.info(
                "release_notes_delivery",
                delivery_id=str(claimed.delivery_id),
                user_id=str(claimed.user_id),
                result="sent",
                parts=parts_sent,
            )
            _log_finished(claimed.announcement_id, counts)
        return True

    async def _record_error(self, claimed: _Claimed, outcome: Outcome, exc: Exception) -> None:
        error = type(exc).__name__
        async with self._session_factory() as session:
            now = self._clock()
            delivery = await session.get(ReleaseDelivery, claimed.delivery_id)
            if delivery is None:
                return
            delivery.error = error
            if outcome is Outcome.blocked:
                delivery.state = ReleaseDeliveryState.blocked
            elif outcome is Outcome.failed:
                delivery.state = ReleaseDeliveryState.failed
            else:
                delivery.attempts += 1
                if delivery.attempts >= self._settings.release_notes_max_attempts:
                    delivery.state = ReleaseDeliveryState.failed
                else:
                    delivery.state = ReleaseDeliveryState.pending
                    delivery.next_attempt_at = now + backoff(delivery.attempts)
            counts = None
            if delivery.state is not ReleaseDeliveryState.pending:
                counts = await self._finish(session, claimed.announcement_id, now)
            result = delivery.state.value
            attempts = delivery.attempts
            await session.commit()
        log.warning(
            "release_notes_delivery",
            delivery_id=str(claimed.delivery_id),
            user_id=str(claimed.user_id),
            result=result,
            error=error,
            attempts=attempts,
        )
        _log_finished(claimed.announcement_id, counts)

    @staticmethod
    async def _finish(
        session: AsyncSession, announcement_id: uuid.UUID, now: datetime
    ) -> dict[str, int] | None:
        """Finish the announcement when nothing is left; its counts by state, for the log."""
        await session.flush()
        if not await repo.finish_if_done(session, announcement_id, now):
            return None
        counts = await repo.counts_by_state(session, announcement_id)
        return {state.value: count for state, count in counts.items()}


def _log_finished(announcement_id: uuid.UUID, counts: dict[str, int] | None) -> None:
    if counts is not None:
        log.info("release_notes_finished", announcement_id=str(announcement_id), **counts)


async def _has_access(session: AsyncSession, settings: Settings, user: User, now: datetime) -> bool:
    if user.access_suspended_at is not None:
        return False
    statuses = await access_repo.bulk_status(session, settings, [user], now=now)
    return statuses[user.id].has_access


async def recover_interrupted(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Rows left in ``sending`` by a crash (data-model.md state machine): the part in doubt
    counts as sent. It was the last one: ``unknown`` (never resent). Otherwise back to
    ``pending`` for the remaining parts. Returns how many rows were recovered."""
    unknown = resumed = 0
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(ReleaseDelivery)
                .where(ReleaseDelivery.state == ReleaseDeliveryState.sending)
                .with_for_update()
            )
        ).all()
        finished: set[uuid.UUID] = set()
        for delivery in rows:
            in_doubt = delivery.parts_sent + 1
            if delivery.parts_total is None or in_doubt >= delivery.parts_total:
                delivery.state = ReleaseDeliveryState.unknown
                delivery.parts_sent = delivery.parts_total or in_doubt
                finished.add(delivery.announcement_id)
                unknown += 1
            else:
                delivery.state = ReleaseDeliveryState.pending
                delivery.parts_sent = in_doubt
                delivery.next_attempt_at = now
                resumed += 1
        await session.flush()
        for announcement_id in finished:
            await repo.finish_if_done(session, announcement_id, now)
        await session.commit()
    if rows:
        log.warning("release_notes_recovered", unknown=unknown, resumed=resumed)
    return len(rows)


def register_release_notes_job(
    scheduler: Scheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    clock: Clock = utcnow,
) -> ReleaseNotesSender:
    """Register ``release_notes`` (every 15 s, due at once at start)."""
    sender = ReleaseNotesSender(session_factory, bot, settings, clock=clock)
    scheduler.register_job(JOB_NAME, JOB_SCHEDULE, sender.run_once, run_at_start=True)
    return sender
