"""PostgreSQL-polling scheduler (research R4, contracts/internal-interfaces.md).

Every ``tick_s`` seconds a tick runs five steps in order, each in its own transaction with
``SELECT ... FOR UPDATE SKIP LOCKED``. The row locks keep the steps consistent with the rest of
this process, but they do not make a second instance safe: per-chat ordering uses process-local
locks (``intake/locks.py``) and a second process would start Telegram clients from the same
session strings. Only one instance may run per database; ``python -m scribe`` enforces it with
an advisory lock (``scribe.instance_lock``, #60).

0. stuck turns: turns left in ``reading`` for more than ``stuck_reading_s`` (default 10 min)
   that no handler in this process is working on (a failed statement stranded them) go back
   to ``waiting`` by the same rule as the startup requeue (:func:`requeue_reading_turns`).
1. due turns: ``waiting AND due_at <= now`` of users who are ``active`` or ``out_of_office``
   (a paused user's messages stay unread, US2) and whose access is not suspended (specs/005)
   -> ``reading``; each claim transaction
   **commits and releases the locks**, then the registered handler receives the claimed turn
   ids *outside* any transaction, as a background task (ticks keep running). Claims repeat in
   batches of ``batch_size`` within one tick until nothing is due or ``max_in_flight_turns``
   claimed turns are being handled, so a burst of due turns is read within one tick (SC-009).
2. reminders: pending drafts with ``remind_at <= now AND reminded_at IS NULL``.
3. expiries: pending drafts with ``expires_at <= now``.
   Steps 2 and 3 call their handler with the locked rows inside the step's transaction; the
   handler updates the rows, the step commits. A handler may return an awaitable to run after
   the commit (control-chat messages, so a rolled-back step never notifies).
4. jobs: ``scheduled_jobs`` rows with ``next_run_at <= now`` for registered jobs. The row is
   claimed by advancing ``next_run_at`` (commit), the job runs in the background, then
   ``last_run_at``/``last_status`` are recorded (a failed job is retried after
   ``job_retry_s``). At startup :meth:`Scheduler.ensure_jobs` creates missing rows (due at once
   for a job registered with ``run_at_start``) and requeues a job whose last scheduled run was
   claimed but never finished (the process stopped meanwhile), so it runs again on the next tick
   instead of a period later.

A step with no registered handler does nothing (it does not claim rows). User stories plug in
their handlers; nothing in the scheduler can send a reply (Principle I).

:class:`DueTurnsProcessor` is the step-1 handler (US1, US2): it marks the claimed turns' chats
read up to each turn's last message (``read_concurrency`` at a time) and returns, so reading
never waits for the LLM; the reply gate (specs/004-skip-closing-messages) and composition
(turn -> ``pending_review``) and the review card follow in the background,
``compose_concurrency`` at a time. A turn the gate skips (it only thanks, says goodbye or
acknowledges, or it holds forwarded messages only, issue #47) ends ``skipped``: no draft, no
card, no notification; a failure of the gate itself drafts, except a forwards-only turn, which
waits and is decided again. A chat is never read past the first message
of a turn that is still ``waiting`` there (another contact's message in a group). A failure
reverts the turn to ``waiting`` with ``due_at = now + 60 s``; if even that fails, the turn is
left in ``reading`` for step 0. Turns left in ``reading`` by a crash or restart are put back to
``waiting`` at startup by :meth:`Scheduler.requeue_interrupted_turns`: due now when already
read (or "read now"), otherwise at the next allowed moment of the user's working hours.

Issue #74 (``drafting/budget.py``): with a ``BudgetGuard``, a turn of a user whose daily LLM
hard cap is reached is skipped as ``budget`` before the reply gate (no model call; listed in
``/skipped``) and the user gets one notice per UTC day; the composer refusing for the same
reason (``DraftingPaused``) ends the same way.

specs/009 (mark chats unread): with an ``UnreadMarker``, the chat is marked unread right after
the read. The marker decides before ``mark_read`` (``prepare``) and commits after it succeeds
(``commit``), so an echo of the agent's own read cannot cancel the mark and a re-read of an
already-marked message marks nothing; a failed read marks nothing. The marker never raises and
is bounded (5 s), so reading and drafting never depend on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.context.history import turn_messages
from scribe.db.models import (
    ConversationTurn,
    Draft,
    DraftState,
    Message,
    ScheduledJob,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import jobs as jobs_repo
from scribe.drafting.budget import BudgetGuard, DraftingPaused
from scribe.drafting.composer import ComposeError
from scribe.drafting.reply_gate import BUDGET_SKIP, GateDecision, forwards_only_skip, record_skip
from scribe.intake.waiting import (
    first_waiting_message_ids,
    read_now_turn_ids,
    readable_up_to,
    requeued_due_at,
)
from scribe.logging import get_logger
from scribe.telegram.gateway import MessagingGateway
from scribe.telegram.unread_mark import UnreadMarker

log = get_logger("scribe.scheduler")

DueTurnsHandler = Callable[[list[uuid.UUID]], Awaitable[None]]
AfterCommit = Callable[[], Awaitable[None]]
DraftStepHandler = Callable[[AsyncSession, Sequence[Draft]], Awaitable[AfterCommit | None]]
JobFunc = Callable[[], Awaitable[None]]

# Users whose due turns are read and drafted; a paused (or not yet set up) user's stay unread.
DRAFTING_USER_STATES = (UserState.active, UserState.out_of_office)


@runtime_checkable
class BackgroundWork(Protocol):
    """A step handler that keeps work running after it returns (``wait_idle`` drains it)."""

    async def wait_idle(self) -> None: ...


@runtime_checkable
class TracksTurns(Protocol):
    """A step-1 handler that knows which claimed turns it is still working on."""

    def owned_turn_ids(self) -> set[uuid.UUID]: ...


# --- job schedules ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Daily:
    at: time  # UTC

    def next_after(self, moment: datetime) -> datetime:
        moment = moment.astimezone(UTC)
        candidate = datetime.combine(moment.date(), self.at, tzinfo=UTC)
        return candidate if candidate > moment else candidate + timedelta(days=1)


@dataclass(frozen=True)
class Weekly:
    weekday: int  # 0 = Monday
    at: time  # UTC

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be 0..6")

    def next_after(self, moment: datetime) -> datetime:
        moment = moment.astimezone(UTC)
        days_ahead = (self.weekday - moment.weekday()) % 7
        candidate = datetime.combine(moment.date() + timedelta(days=days_ahead), self.at, UTC)
        return candidate if candidate > moment else candidate + timedelta(days=7)


@dataclass(frozen=True)
class Every:
    """A fixed interval from the previous run (the Jira comment sync, specs/003 J8)."""

    interval: timedelta

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError("interval must be positive")

    def next_after(self, moment: datetime) -> datetime:
        return moment + self.interval


Schedule = Daily | Weekly | Every


def next_run(schedule: Schedule, after: datetime) -> datetime:
    """First scheduled time strictly after ``after``."""
    return schedule.next_after(after)


def previous_run(schedule: Schedule, before: datetime) -> datetime:
    """Latest scheduled time strictly before ``before``."""
    if isinstance(schedule, Every):
        return before - schedule.interval
    period = timedelta(days=1) if isinstance(schedule, Daily) else timedelta(days=7)
    candidate = schedule.next_after(before - period - timedelta(microseconds=1))
    return candidate if candidate < before else candidate - period


@dataclass(frozen=True)
class Job:
    name: str
    schedule: Schedule
    func: JobFunc
    run_at_start: bool = False  # a new row is due at once (e.g. a one-off data migration)


# --- scheduler ----------------------------------------------------------------------------------


class Scheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tick_s: float,
        clock: Clock = utcnow,
        batch_size: int = 500,
        max_in_flight_turns: int = 2000,
        job_retry_s: float = 900,
        stuck_reading_s: float = 600,
    ) -> None:
        self._session_factory = session_factory
        self.tick_s = tick_s
        self._clock = clock
        self._batch_size = batch_size
        self._max_in_flight_turns = max_in_flight_turns
        self._job_retry = timedelta(seconds=job_retry_s)
        self._stuck_reading = timedelta(seconds=stuck_reading_s)
        self._claimed: set[uuid.UUID] = set()  # handed to the step-1 handler, not yet returned
        self._due_turns_handler: DueTurnsHandler | None = None
        self._reminders_handler: DraftStepHandler | None = None
        self._expiries_handler: DraftStepHandler | None = None
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._turns_in_flight = 0
        self.last_tick_at: datetime | None = None

    # --- registration (stories plug in here) ---------------------------------------------------

    def on_due_turns(self, handler: DueTurnsHandler) -> None:
        self._due_turns_handler = handler

    def on_reminders(self, handler: DraftStepHandler) -> None:
        self._reminders_handler = handler

    def on_expiries(self, handler: DraftStepHandler) -> None:
        self._expiries_handler = handler

    def register_job(
        self, name: str, schedule: Schedule, func: JobFunc, *, run_at_start: bool = False
    ) -> None:
        self._jobs[name] = Job(name, schedule, func, run_at_start)

    async def ensure_jobs(self) -> None:
        """Create a ``scheduled_jobs`` row for every registered job that has none yet (due now
        for ``run_at_start`` jobs, else at the next scheduled time), and requeue jobs whose
        latest scheduled run was claimed but never recorded as finished (a restart interrupted
        it): they are due now."""
        now = self._clock()
        async with self._session_factory() as session:
            for job in self._jobs.values():
                first = now if job.run_at_start else next_run(job.schedule, now)
                await jobs_repo.ensure_job(session, job.name, first)
            for row in await jobs_repo.list_jobs(session):
                registered = self._jobs.get(row.name)
                if registered is None or row.next_run_at <= now:
                    continue
                due = previous_run(registered.schedule, row.next_run_at)
                claimed = row.created_at is not None and row.created_at <= due <= now
                finished = row.last_run_at is not None and row.last_run_at >= due
                if claimed and not finished:
                    row.next_run_at = now
                    log.info("scheduled_job_requeued", job=row.name, missed_run=due.isoformat())
            await session.commit()

    # --- loop -----------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        log.info("scheduler_started", tick_s=self.tick_s, jobs=sorted(self._jobs))
        while not stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.tick_s)
        log.info("scheduler_stopped")

    async def tick(self, *, drain: bool = False) -> None:
        """Run the four steps once. ``drain`` waits for each step's background work before the
        next step starts (tests on a single shared connection); production ticks never wait."""
        for name, step in (
            ("stuck_turns", self._step_stuck_turns),
            ("due_turns", self._step_due_turns),
            ("reminders", self._step_reminders),
            ("expiries", self._step_expiries),
            ("jobs", self._step_jobs),
        ):
            try:
                await step()
            except Exception:
                log.exception("scheduler_step_failed", step=name)
            if drain:
                await self.wait_idle()
        self.last_tick_at = self._clock()

    async def wait_idle(self) -> None:
        """Wait for background handler/job tasks (tests, graceful shutdown)."""
        while True:
            while self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
            handler = self._due_turns_handler
            if isinstance(handler, BackgroundWork):
                await handler.wait_idle()
            if not self._tasks:
                return

    async def requeue_interrupted_turns(self) -> int:
        """At startup: turns left in ``reading`` by a crash or restart go back to ``waiting``
        (:func:`requeue_reading_turns`), so they are read (again, harmlessly) and drafted.
        Single instance only."""
        async with self._session_factory() as session, session.begin():
            count = await requeue_reading_turns(session, self._clock())
        if count:
            log.warning("scheduler_requeued_interrupted_turns", count=count)
        return count

    async def shutdown(self, timeout_s: float = 30) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(list(self._tasks), timeout=timeout_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # --- steps ----------------------------------------------------------------------------------

    async def _step_stuck_turns(self) -> None:
        """Step 0: requeue turns stranded in ``reading`` (not owned by a handler here)."""
        handler = self._due_turns_handler
        if handler is None:
            return
        now = self._clock()
        owned = set(self._claimed)
        if isinstance(handler, TracksTurns):
            owned |= handler.owned_turn_ids()
        async with self._session_factory() as session, session.begin():
            count = await requeue_reading_turns(
                session,
                now,
                changed_before=now - self._stuck_reading,
                exclude=owned,
                limit=self._batch_size,
            )
        if count:
            log.warning("scheduler_requeued_stuck_turns", count=count)

    async def _step_due_turns(self) -> None:
        handler = self._due_turns_handler
        if handler is None:
            return
        while True:
            capacity = min(self._batch_size, self._max_in_flight_turns - self._turns_in_flight)
            if capacity <= 0:
                return
            turn_ids = await self._claim_due_turns(capacity)
            if not turn_ids:
                return
            # Committed: locks released before any Telegram or LLM call.
            log.info("scheduler_due_turns_claimed", count=len(turn_ids))
            self._turns_in_flight += len(turn_ids)
            self._claimed.update(turn_ids)
            self._spawn(self._run_due_turns(handler, turn_ids), "due_turns")
            if len(turn_ids) < capacity:
                return

    async def _claim_due_turns(self, limit: int) -> list[uuid.UUID]:
        now = self._clock()
        drafting_users = select(User.id).where(
            User.state.in_(DRAFTING_USER_STATES),
            User.access_suspended_at.is_(None),  # access ended (specs/005 research Q6)
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(ConversationTurn.id)
                .where(
                    ConversationTurn.state == TurnState.waiting,
                    ConversationTurn.due_at <= now,
                    ConversationTurn.user_id.in_(drafting_users),
                )
                .order_by(ConversationTurn.due_at)
                .limit(limit)
                .with_for_update(of=ConversationTurn, skip_locked=True)
            )
            turn_ids = list(result.scalars().all())
            if turn_ids:
                await session.execute(
                    update(ConversationTurn)
                    .where(ConversationTurn.id.in_(turn_ids))
                    # updated_at on the service clock: step 0 measures "stuck" from here.
                    .values(state=TurnState.reading, updated_at=now)
                )
        return turn_ids

    async def _run_due_turns(self, handler: DueTurnsHandler, turn_ids: list[uuid.UUID]) -> None:
        try:
            await handler(turn_ids)
        finally:
            self._turns_in_flight -= len(turn_ids)
            self._claimed.difference_update(turn_ids)

    async def _step_reminders(self) -> None:
        if self._reminders_handler is None:
            return
        now = self._clock()
        await self._draft_step(
            self._reminders_handler,
            Draft.remind_at <= now,
            Draft.reminded_at.is_(None),
        )

    async def _step_expiries(self) -> None:
        if self._expiries_handler is None:
            return
        await self._draft_step(self._expiries_handler, Draft.expires_at <= self._clock())

    async def _draft_step(self, handler: DraftStepHandler, *conditions: Any) -> None:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(Draft)
                .where(Draft.state == DraftState.pending, *conditions)
                .order_by(Draft.prepared_at)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            drafts = list(result.scalars().all())
            after_commit = await handler(session, drafts) if drafts else None
        if after_commit is not None:
            self._spawn(_await(after_commit), "after_draft_step")

    async def _step_jobs(self) -> None:
        if not self._jobs:
            return
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(ScheduledJob)
                .where(ScheduledJob.next_run_at <= now, ScheduledJob.name.in_(self._jobs))
                .with_for_update(skip_locked=True)
            )
            due = list(result.scalars().all())
            for row in due:  # claim: move next_run_at forward before running
                row.next_run_at = next_run(self._jobs[row.name].schedule, now)
        for row in due:
            self._spawn(self._run_job(self._jobs[row.name], now), f"job:{row.name}")

    async def _run_job(self, job: Job, started_at: datetime) -> None:
        status = "ok"
        try:
            await job.func()
        except Exception as exc:
            status = f"error: {type(exc).__name__}"
            log.exception("scheduled_job_failed", job=job.name)
        async with self._session_factory() as session:
            row = await jobs_repo.get_job(session, job.name)
            if row is not None:
                row.last_run_at = started_at
                row.last_status = status
                if status != "ok":
                    row.next_run_at = min(row.next_run_at, self._clock() + self._job_retry)
                await session.commit()
        log.info("scheduled_job_finished", job=job.name, status=status)

    def _spawn(self, coroutine: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(self._guard(coroutine, name), name=f"scheduler:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guard(coroutine: Coroutine[Any, Any, None], name: str) -> None:
        try:
            await coroutine
        except Exception:
            log.exception("scheduler_task_failed", task=name)


async def _await(func: AfterCommit) -> None:
    await func()


async def requeue_reading_turns(
    session: AsyncSession,
    now: datetime,
    *,
    changed_before: datetime | None = None,
    exclude: set[uuid.UUID] | None = None,
    limit: int | None = None,
) -> int:
    """Put turns left in ``reading`` back to ``waiting`` (startup, stuck-turn sweep).

    ``due_at`` follows :func:`~scribe.intake.waiting.requeued_due_at`: now when the turn was
    already marked read or explicitly asked for ("read now", read elsewhere), otherwise the
    next allowed moment of the user's working hours, so a requeue never reads at 03:00
    (SC-004). Never sends anything. Returns how many turns were requeued.
    """
    query = (
        select(ConversationTurn, User)
        .join(User, User.id == ConversationTurn.user_id)
        .where(ConversationTurn.state == TurnState.reading)
        .order_by(ConversationTurn.id)
        .with_for_update(of=ConversationTurn, skip_locked=True)
    )
    if changed_before is not None:
        query = query.where(ConversationTurn.updated_at <= changed_before)
    if exclude:
        query = query.where(ConversationTurn.id.not_in(list(exclude)))
    if limit is not None:
        query = query.limit(limit)
    rows = (await session.execute(query)).all()
    if not rows:
        return 0
    shortened = await read_now_turn_ids(session, None, [turn.id for turn, _ in rows])
    for turn, user in rows:
        turn.due_at = requeued_due_at(turn.read_at, user, now, read_now=turn.id in shortened)
        turn.state = TurnState.waiting
    await session.flush()
    return len(rows)


# --- step 1 handler: read and draft (US1) -------------------------------------------------------


class TurnComposer(Protocol):
    async def compose(
        self,
        user_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        supersede_draft_id: uuid.UUID | None = None,
        extra_system_warnings: Sequence[str] = (),
    ) -> Draft: ...


class CardPoster(Protocol):
    async def post_card(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> None: ...


class ReplyGateProtocol(Protocol):
    """``scribe.drafting.reply_gate.ReplyGate`` (004 contracts/reply-gate.md)."""

    async def decide_for_turn(self, user_id: uuid.UUID, turn_id: uuid.UUID) -> GateDecision: ...

    async def record_skip(
        self, user_id: uuid.UUID, turn_id: uuid.UUID, decision: GateDecision
    ) -> bool: ...

    async def record_draft_decision(
        self, user_id: uuid.UUID, turn_id: uuid.UUID, decision: GateDecision
    ) -> None: ...


# A skip that lost to a message joining the turn is decided again at most this many times.
GATE_ATTEMPTS = 3

# Why a drafted turn has a draft at all, shown on its card (004 FR-005, FR-013).
GATE_WARNINGS: dict[str, tuple[str, ...]] = {
    "habit": ("drafted_by_habit",),
    "user": ("drafted_on_request",),
}


@dataclass(frozen=True)
class ReadTarget:
    turn_id: uuid.UUID
    user_id: uuid.UUID
    telegram_chat_id: int | None  # None: the turn's chat or last message is gone
    telegram_message_id: int | None  # read up to here (capped below still-waiting messages)


class DueTurnsProcessor:
    """Read claimed turns, then draft them in the background.

    ``__call__`` returns once every claimed turn has been marked read (``read_concurrency`` at
    a time), so a slow LLM never delays reading (SC-009); composition runs afterwards,
    ``compose_concurrency`` at a time, and :meth:`wait_idle` drains it. Runs outside any
    transaction (the scheduler committed the claim). Never sends anything: the only way to send
    is an approval callback (Principle I).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: MessagingGateway,
        composer: TurnComposer,
        presenter: CardPoster | None,
        *,
        gate: ReplyGateProtocol | None = None,
        marker: UnreadMarker | None = None,
        budget: BudgetGuard | None = None,
        clock: Clock = utcnow,
        read_concurrency: int = 50,
        compose_concurrency: int = 10,
        retry_s: float = 60,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._composer = composer
        self._presenter = presenter
        self._gate = gate
        self._marker = marker
        self._budget = budget
        self._clock = clock
        self._read_semaphore = asyncio.Semaphore(read_concurrency)
        self._compose_semaphore = asyncio.Semaphore(compose_concurrency)
        self._retry = timedelta(seconds=retry_s)
        self._tasks: set[asyncio.Task[None]] = set()
        self._owned: dict[uuid.UUID, int] = {}  # claimed turns still being read or drafted

    async def __call__(self, turn_ids: list[uuid.UUID]) -> None:
        self._own(turn_ids)
        composing: list[uuid.UUID] = []
        try:
            composing = await self._read_phase(turn_ids)
        finally:
            self._release([t for t in turn_ids if t not in set(composing)])

    async def _read_phase(self, turn_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Read the claimed turns; returns the ids handed over to composition. Every failure
        falls back per turn to :meth:`_retry_later`; whatever still fails stays ``reading``
        for the scheduler's stuck-turn sweep."""
        try:
            targets = await self._load_targets(turn_ids)
        except Exception as exc:
            log.error("due_turns_load_failed", count=len(turn_ids), error=type(exc).__name__)
            for turn_id in turn_ids:
                await self._retry_later(ReadTarget(turn_id, uuid.UUID(int=0), None, None), exc)
            return []
        read = await asyncio.gather(*(self._read_bounded(target) for target in targets))
        done = [target for target, ok in zip(targets, read, strict=True) if ok]
        if not done:
            return []
        read_at = self._clock()
        try:
            await self._set_turns([t.turn_id for t in done], read_at=read_at)
        except Exception as exc:
            log.error("turns_read_at_failed", count=len(done), error=type(exc).__name__)
            done = [t for t in done if await self._mark_read_one(t, read_at)]
        for target in done:
            task = asyncio.create_task(self._compose_owned(target), name="compose-turn")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return [t.turn_id for t in done]

    async def _mark_read_one(self, target: ReadTarget, read_at: datetime) -> bool:
        try:
            await self._set_turns([target.turn_id], read_at=read_at)
        except Exception as exc:
            await self._retry_later(target, exc)
            return False
        return True

    def owned_turn_ids(self) -> set[uuid.UUID]:
        """Claimed turns this processor is still reading or drafting (stuck-turn sweep)."""
        return set(self._owned)

    def _own(self, turn_ids: list[uuid.UUID]) -> None:
        for turn_id in turn_ids:
            self._owned[turn_id] = self._owned.get(turn_id, 0) + 1

    def _release(self, turn_ids: list[uuid.UUID]) -> None:
        for turn_id in turn_ids:
            left = self._owned.get(turn_id, 0) - 1
            if left > 0:
                self._owned[turn_id] = left
            else:
                self._owned.pop(turn_id, None)

    async def _compose_owned(self, target: ReadTarget) -> None:
        try:
            await self._compose_bounded(target)
        finally:
            self._release([target.turn_id])

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def process(self, turn_id: uuid.UUID) -> None:
        """Read and draft one claimed turn, start to finish."""
        await self([turn_id])
        await self.wait_idle()

    # --- read phase -----------------------------------------------------------------------------

    async def _load_targets(self, turn_ids: list[uuid.UUID]) -> list[ReadTarget]:
        """One query for the whole claim: chat and last-message Telegram ids per turn, then
        one per user for the chats' still-waiting turns (never read past their first message,
        e.g. another contact's message in a group)."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ConversationTurn.id,
                        ConversationTurn.user_id,
                        ConversationTurn.work_chat_id,
                        WorkChat.telegram_chat_id,
                        Message.telegram_message_id,
                    )
                    .select_from(ConversationTurn)
                    .outerjoin(WorkChat, WorkChat.id == ConversationTurn.work_chat_id)
                    .outerjoin(Message, Message.id == ConversationTurn.last_message_id)
                    .where(
                        ConversationTurn.id.in_(turn_ids),
                        ConversationTurn.state == TurnState.reading,
                    )
                )
            ).all()
            chats_by_user: dict[uuid.UUID, set[uuid.UUID]] = {}
            for _, user_id, work_chat_id, _, _ in rows:
                chats_by_user.setdefault(user_id, set()).add(work_chat_id)
            floors: dict[uuid.UUID, int] = {}
            for user_id, chat_ids in chats_by_user.items():
                floors.update(await first_waiting_message_ids(session, user_id, chat_ids))
        targets = []
        for turn_id, user_id, work_chat_id, telegram_chat_id, telegram_message_id in rows:
            if telegram_message_id is not None:
                telegram_message_id = readable_up_to(telegram_message_id, floors.get(work_chat_id))
            targets.append(ReadTarget(turn_id, user_id, telegram_chat_id, telegram_message_id))
        return targets

    async def _read_bounded(self, target: ReadTarget) -> bool:
        async with self._read_semaphore:
            try:
                return await self._read(target)
            except Exception:
                log.exception("due_turn_failed", turn_id=str(target.turn_id))
                return False

    async def _read(self, target: ReadTarget) -> bool:
        if target.telegram_chat_id is None or target.telegram_message_id is None:
            log.warning("turn_not_draftable", turn_id=str(target.turn_id), error="no_messages")
            await self._set_turns([target.turn_id], state=TurnState.handle_manually)
            return False
        if target.telegram_message_id < 1:  # everything up to here is another turn's, unread
            return True
        ticket = None
        if self._marker is not None:  # decide before our own read (specs/009 research U4)
            ticket = await self._marker.prepare(
                target.user_id, target.telegram_chat_id, target.telegram_message_id
            )
        try:
            await self._gateway.mark_read(
                target.user_id, target.telegram_chat_id, target.telegram_message_id
            )
        except Exception as exc:
            await self._retry_later(target, exc)
            return False
        if self._marker is not None:
            await self._marker.commit(ticket)
        return True

    # --- compose phase --------------------------------------------------------------------------

    async def _compose_bounded(self, target: ReadTarget) -> None:
        async with self._compose_semaphore:
            try:
                await self._compose(target)
            except Exception:
                log.exception("due_turn_failed", turn_id=str(target.turn_id))

    async def _compose(self, target: ReadTarget) -> None:
        if await self._budget_paused(target):
            await self._pause(target)
            return  # issue #74: no model call at the daily hard cap
        warnings = await self._reply_gate(target)
        if warnings is None:
            return  # skipped: no draft, no card, no notification (004 FR-008)
        try:
            draft = await self._composer.compose(
                target.user_id, target.turn_id, extra_system_warnings=warnings
            )
        except DraftingPaused:  # the reply gate's own call reached the hard cap
            await self._pause(target)
            return
        except ComposeError as exc:
            log.warning("turn_not_draftable", turn_id=str(target.turn_id), error=type(exc).__name__)
            await self._set_turns([target.turn_id], state=TurnState.handle_manually)
            return
        except Exception as exc:
            await self._retry_later(target, exc)
            return
        if self._presenter is not None:
            try:
                await self._presenter.post_card(target.user_id, draft.id)
            except Exception as exc:
                log.error("draft_card_failed", draft_id=str(draft.id), error=type(exc).__name__)

    async def _budget_paused(self, target: ReadTarget) -> bool:
        """The user's daily hard cap is reached (issue #74). A failed check drafts."""
        if self._budget is None:
            return False
        try:
            return await self._budget.paused(target.user_id)
        except Exception as exc:
            log.warning(
                "budget_check_failed", turn_id=str(target.turn_id), error=type(exc).__name__
            )
            return False

    async def _pause(self, target: ReadTarget) -> None:
        """Skip the turn as ``budget`` (listed in /skipped, "Draft anyway" works again the next
        UTC day) and send the day's one notice. A failed store retries the turn later."""
        try:
            stored = await record_skip(
                self._session_factory,
                target.user_id,
                target.turn_id,
                BUDGET_SKIP,
                now=self._clock(),
            )
        except Exception as exc:
            await self._retry_later(target, exc)
            return
        log.info("turn_skipped_budget", turn_id=str(target.turn_id), stored=stored)
        if self._budget is not None:
            await self._budget.notify_paused(target.user_id)

    async def _reply_gate(self, target: ReadTarget) -> tuple[str, ...] | None:
        """``None`` when the turn was skipped (or put back to wait), else the gate's warnings
        for the draft card.

        Runs after the turn was marked read (FR-007). A failure in the gate itself never
        reverts the turn: it is drafted (004 FR-004), except a turn of forwarded messages only,
        which is never drafted by itself (issue #47): it goes back to ``waiting`` and is decided
        again later (also when it cannot even be checked). Without a gate such a turn is skipped
        directly. A skip whose turn gained a message after the decision is not stored; the turn
        is decided again (``GATE_ATTEMPTS`` times at most, then it waits)."""
        user_id, turn_id = target.user_id, target.turn_id
        for _ in range(GATE_ATTEMPTS):
            if self._gate is None:
                checked, skip = await self._forwards_only_skip(target)
                if not checked:
                    await self._retry_later(target, LookupError("forwards check failed"))
                    return None
                if skip is None:
                    return ()
                if await self._store_forwards_only(target, skip) is not None:
                    return None
                continue  # a message joined the turn after the check: check again
            try:
                decision = await self._gate.decide_for_turn(user_id, turn_id)
                if decision.failed:
                    checked, skip = await self._forwards_only_skip(target)
                    if not checked:
                        await self._retry_later(target, LookupError("reply gate failed"))
                        return None
                    if skip is not None:
                        decision = skip  # the gate's own rule, checked here: store it below
                if decision.decision == "skip":
                    if await self._gate.record_skip(user_id, turn_id, decision):
                        return None
                    if not await self._still_reading(target):
                        return None  # the turn left ``reading``: not ours any more
                    continue  # a message joined the turn after the decision: decide again
                await self._gate.record_draft_decision(user_id, turn_id, decision)
            except Exception as exc:
                log.warning(
                    "reply_gate_step_failed", turn_id=str(turn_id), error=type(exc).__name__
                )
                checked, skip = await self._forwards_only_skip(target)
                if not checked:
                    await self._retry_later(target, exc)
                    return None
                if skip is None:
                    return ()
                # The gate's own rule, checked here: store the skip (guarded) directly.
                stored = await self._store_forwards_only(target, skip)
                if stored is not None:
                    return None
                continue
            return GATE_WARNINGS.get(decision.source, ())
        await self._retry_later(target, LookupError("turn kept changing"))
        return None

    async def _store_forwards_only(self, target: ReadTarget, skip: GateDecision) -> bool | None:
        """Store a forwards-only skip the scheduler checked itself (guarded by the turn's last
        message). True: stored; False: the turn left ``reading`` or the store failed (it is
        retried later); None: a message joined meanwhile, decide again. Never raises."""
        try:
            if await record_skip(
                self._session_factory, target.user_id, target.turn_id, skip, now=self._clock()
            ):
                return True
            if await self._still_reading(target):
                return None
            return False
        except Exception as exc:
            await self._retry_later(target, exc)
            return False

    async def _forwards_only_skip(self, target: ReadTarget) -> tuple[bool, GateDecision | None]:
        """(checked, the forwards-only skip decision or None) by the gate's own rule
        (``forwards_only_skip``, issue #47); ``checked`` is False when the turn could not be
        loaded. Never raises."""
        try:
            async with self._session_factory() as session:
                turn = await session.get(ConversationTurn, target.turn_id)
                if turn is None or turn.user_id != target.user_id:
                    return True, None
                return True, forwards_only_skip(turn, await turn_messages(session, turn))
        except Exception as exc:
            log.warning(
                "forwards_check_failed", turn_id=str(target.turn_id), error=type(exc).__name__
            )
            return False, None

    async def _still_reading(self, target: ReadTarget) -> bool:
        async with self._session_factory() as session:
            state = await session.scalar(
                select(ConversationTurn.state).where(
                    ConversationTurn.id == target.turn_id,
                    ConversationTurn.user_id == target.user_id,
                )
            )
        return state == TurnState.reading

    async def _retry_later(self, target: ReadTarget, exc: Exception) -> None:
        """Back to ``waiting`` in ``retry_s``. Never raises: if even this fails the turn stays
        ``reading`` and the scheduler's stuck-turn sweep requeues it."""
        retry_at = self._clock() + self._retry
        log.warning(
            "turn_draft_failed_retrying",
            turn_id=str(target.turn_id),
            error=type(exc).__name__,
            retry_at=retry_at.isoformat(),
        )
        try:
            await self._set_turns([target.turn_id], state=TurnState.waiting, due_at=retry_at)
        except Exception as again:
            log.error("turn_retry_failed", turn_id=str(target.turn_id), error=type(again).__name__)

    async def _set_turns(self, turn_ids: list[uuid.UUID], **values: Any) -> None:
        """Update turns only while this processor still owns them (state ``reading``): a manual
        reply may have dropped one meanwhile."""
        async with self._session_factory() as session:
            await session.execute(
                update(ConversationTurn)
                .where(
                    ConversationTurn.id.in_(turn_ids), ConversationTurn.state == TurnState.reading
                )
                .values(updated_at=self._clock(), **values)
            )
            await session.commit()
