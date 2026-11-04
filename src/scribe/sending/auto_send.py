"""Opt-in auto-send (specs/014-auto-send, Constitution Principle I).

Scribe never sends a draft on its own except in a chat the user turned auto-send on for, for a
draft that is *eligible*, after a cancel window the user was shown on the draft's card. This
module holds everything that decides or performs that; no other module can approve a send
without a tap.

Guarantees (each one is tested, SC-001 / SC-002):

- **Opt-in, scoped, reversible, off by default**: ``work_chats.auto_send`` is false for every
  chat until the user confirms it for that one chat (:func:`set_chat_auto_send`); turning it off,
  or disabling / excluding the chat, stops it at once and stops running countdowns.
- **Eligibility is fail closed** (:func:`eligibility`, research A4): a pure function that lists
  every reason a draft must wait for the user, run when the card is posted and again when the
  window ends. ``injection.hard_stops`` is the seam ``drafting/injection.py`` names; any warning
  not in :data:`AUTO_SEND_ALLOWED_WARNINGS` (empty in this release) blocks.
- **No countdown without a card** (research A4a, FR-006): the countdown is armed only by
  :func:`commit_arm`, in the transaction that stores ``drafts.control_message_id`` after the card
  was posted; the scheduler step selects only drafts with a card and the database refuses
  ``auto_send_due_at`` without one (``ck_drafts_auto_send_needs_card``).
- **The user's approval is never faked** (research A1): the auto path sets
  ``drafts.auto_approved_at``, never ``approved_by_user_at``; the database refuses a ``sent``
  draft with neither.
- **Exactly one of the user and the countdown wins** (research A3): every card action and the
  step lock the draft row; :func:`stop_countdown` and :func:`is_firing` decide who got there first.

Logs carry ids and codes only, never text (Principle II).
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.context.commitments import CommitmentExtractor
from scribe.context.task_dates import MISMATCH_PREFIX
from scribe.db.models import (
    ActivityKind,
    AutoSendOfferState,
    AutoSendSource,
    AutoSendStop,
    ConversationTurn,
    Draft,
    DraftDecisionKind,
    DraftState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import decisions as decisions_repo
from scribe.drafting.injection import hard_stops
from scribe.drafting.placeholders import PLACEHOLDER
from scribe.drafting.stored_output import load_stored_output
from scribe.i18n import LocalizedError, Msg, t
from scribe.llm.provider import DraftOutput
from scribe.logging import get_logger
from scribe.scheduling.working_hours import next_allowed
from scribe.sending.send import (
    AccessSuspended,
    DraftOutdated,
    SendRefused,
    send_approved_draft,
)
from scribe.telegram.control_bot.render import button, format_callback, keyboard
from scribe.telegram.gateway import AlreadyDelivered, MessagingGateway
from scribe.telegram.unread_mark import UnreadMarker

log = get_logger("scribe.sending")

# Thresholds of the offer and the suggestion to turn auto-send off (data-model.md).
OFFER_MIN_DECISIONS = 20
OFFER_RATE = 0.60  # strictly greater
OFFER_WINDOW = timedelta(days=30)
OFFER_SNOOZE = timedelta(days=30)
OFFER_GAP = timedelta(hours=24)
AUTO_SEND_WINDOWS = (60, 120, 300, 600, 900)  # FR-007: the cancel window, 1 to 15 minutes
WARN_SAMPLE = 10
WARN_MAX_INTERVENTIONS = 4  # strictly more
WARN_GAP = timedelta(days=7)

# Warning codes that do not stop an automatic send. Empty on purpose: a warning added later must
# not silently become auto-sendable (research A4).
AUTO_SEND_ALLOWED_WARNINGS: frozenset[str] = frozenset()

# A countdown that ended longer ago than this (the service was down) is not sent: the user's tap
# on "Don't send automatically" may still be waiting in Telegram's queue (research A3).
FIRE_GRACE = timedelta(minutes=10)

# A claim (``auto_approved_at`` set, no stop) older than this whose send never finished (a restart
# or a shutdown between the step's commit and the send) is stopped as ``send_failed`` and its card
# becomes an ordinary one; never retried (research A3). A send in flight holds the draft row, so
# the recovery skips it.
FIRING_STALE_AFTER = timedelta(minutes=2)

_WEAK_STYLE = "weak_style_profile"
_OUT_OF_OFFICE = "out_of_office"


class BlockReason(enum.StrEnum):
    """Why a draft must wait for the user's own approval (``autosend.reason.<code>``)."""

    hard_stop = "hard_stop"  # suspected prompt injection or a claimed action
    commitment = "commitment"  # the draft makes a promise
    sensitive = "sensitive"
    placeholders = "placeholders"  # needs details from the user
    dates_differ = "dates_differ"
    conflict = "conflict"
    weak_style = "weak_style"
    out_of_office = "out_of_office"
    warning = "warning"  # any other warning on the draft
    no_output = "no_output"  # the stored model output is gone or unreadable
    outdated = "outdated"  # new messages arrived after the draft
    chat_off = "chat_off"  # auto-send is off for the chat, or the chat is disabled / excluded
    outside_hours = "outside_hours"  # the window ended outside the user's working hours
    suspended = "suspended"  # access ended (specs/005)
    paused = "paused"  # /pause (or not set up)
    not_first_version = "not_first_version"  # regenerated or filled: the user is working on it
    possibly_unnecessary = "possibly_unnecessary"  # a closing message says no reply is needed


@dataclass(frozen=True)
class Eligibility:
    reasons: tuple[BlockReason, ...]

    @property
    def eligible(self) -> bool:
        return not self.reasons


def eligibility(
    *,
    draft: Draft,
    output: DraftOutput | None,
    system_warnings: Sequence[str],
    context_warnings: Sequence[str],
    chat: WorkChat,
    user: User,
    fire_at: datetime | None = None,
) -> Eligibility:
    """Every reason ``draft`` may not be sent without the user (empty: eligible).

    Pure and deterministic. ``fire_at`` is set when the window ends (not when the card is
    posted) and adds the working-hours check. Fail closed: it never raises; an unexpected error
    blocks the draft (``warning``)."""
    try:
        reasons = _reasons(draft, output, system_warnings, context_warnings, chat, user, fire_at)
    except Exception as exc:  # a bug here must stop a send, never allow one
        log.error("auto_send_eligibility_failed", error=type(exc).__name__)
        reasons = {BlockReason.warning}
    return Eligibility(tuple(reason for reason in BlockReason if reason in reasons))


def _reasons(
    draft: Draft,
    output: DraftOutput | None,
    system_warnings: Sequence[str],
    context_warnings: Sequence[str],
    chat: WorkChat,
    user: User,
    fire_at: datetime | None,
) -> set[BlockReason]:
    reasons: set[BlockReason] = set()
    if hard_stops(system_warnings):
        reasons.add(BlockReason.hard_stop)
    if output is None:
        reasons.add(BlockReason.no_output)
    else:
        if output.flags.creates_commitment.value:
            reasons.add(BlockReason.commitment)
        if output.flags.sensitive.value:
            reasons.add(BlockReason.sensitive)
        if output.needs_info:
            reasons.add(BlockReason.placeholders)
        if output.conflicts:
            reasons.add(BlockReason.conflict)
    if PLACEHOLDER.search(draft.text or ""):
        reasons.add(BlockReason.placeholders)

    warnings = [*system_warnings, *context_warnings]
    if any(w.startswith(MISMATCH_PREFIX) for w in system_warnings):
        reasons.add(BlockReason.dates_differ)
    if _WEAK_STYLE in warnings:
        reasons.add(BlockReason.weak_style)
    if _OUT_OF_OFFICE in warnings:
        reasons.add(BlockReason.out_of_office)
    handled = {_WEAK_STYLE, _OUT_OF_OFFICE, *hard_stops(system_warnings)}
    for code in warnings:
        if code in handled or code.startswith(MISMATCH_PREFIX):
            continue
        if code not in AUTO_SEND_ALLOWED_WARNINGS:
            reasons.add(BlockReason.warning)

    if draft.outdated:
        reasons.add(BlockReason.outdated)
    if draft.possibly_unnecessary:
        reasons.add(BlockReason.possibly_unnecessary)
    if draft.version != 1 or draft.edited_text is not None:
        reasons.add(BlockReason.not_first_version)

    if not chat.auto_send or not chat.enabled or chat.excluded:
        reasons.add(BlockReason.chat_off)
    if user.access_suspended_at is not None:
        reasons.add(BlockReason.suspended)
    if user.state == UserState.out_of_office:
        reasons.add(BlockReason.out_of_office)
    elif user.state != UserState.active:
        reasons.add(BlockReason.paused)
    if fire_at is not None and _outside_hours(fire_at, user):
        reasons.add(BlockReason.outside_hours)
    return reasons


def _outside_hours(moment: datetime, user: User) -> bool:
    try:
        allowed = next_allowed(
            moment, user.timezone, user.working_days, user.work_start, user.work_end
        )
    except Exception:  # an unreadable schedule or time zone: do not send
        return True
    return allowed != moment


# --- the countdown ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmPlan:
    """What the card is posted with (research A4a): ``planned_due_at`` is the time printed on it,
    ``None`` when the draft is not eligible (``reasons`` says why)."""

    planned_due_at: datetime | None
    reasons: tuple[BlockReason, ...]


def is_armed(draft: Draft) -> bool:
    """A countdown is running: set when the card was posted, not stopped, not yet claimed."""
    return (
        draft.state == DraftState.pending
        and draft.auto_send_due_at is not None
        and draft.auto_send_stop is None
        and draft.auto_approved_at is None
    )


def is_firing(draft: Draft) -> bool:
    """The step has claimed the draft (``auto_approved_at`` set) and the send is under way or
    about to start: a tap on the card is too late. A failed send sets ``auto_send_stop`` and ends
    this state."""
    return (
        draft.state == DraftState.pending
        and draft.auto_approved_at is not None
        and draft.auto_send_stop is None
    )


def stop_countdown(draft: Draft, reason: AutoSendStop) -> bool:
    """Stop a running countdown on a row the caller holds ``FOR UPDATE``. True if it was running;
    False if there was none, it had stopped, or the step already claimed the draft."""
    if not is_armed(draft):
        return False
    draft.auto_send_stop = reason
    return True


async def note_stopped(
    session: AsyncSession, draft: Draft, reason: AutoSendStop, at: datetime
) -> None:
    """Record a stopped countdown (``auto_send_stopped``, attributable per Principle I)."""
    await activity_repo.record(
        session,
        draft.user_id,
        ActivityKind.auto_send_stopped,
        at=at,
        draft_id=draft.id,
        conversation_turn_id=draft.conversation_turn_id,
        meta={"reason": reason.value},
    )
    log.info(
        "auto_send_stopped",
        user_id=str(draft.user_id),
        draft_id=str(draft.id),
        reason=reason.value,
    )


async def _lock_draft(
    session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID
) -> Draft | None:
    return (
        await session.execute(
            select(Draft)
            .where(Draft.user_id == user_id, Draft.id == draft_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def stop_for_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    now: datetime,
    cancelled: bool = False,
) -> bool | None:
    """The user acted on a card: stop its countdown under the draft lock (research A3). ``None``:
    too late, the step already claimed the draft; True: a running countdown was stopped; False:
    none was running. ``cancelled`` (the ⏹ button) also records an ``auto_cancelled`` decision."""
    async with session_factory() as session:
        draft = await _lock_draft(session, user_id, draft_id)
        if draft is None:
            return False
        if is_firing(draft):
            return None
        stopped = stop_countdown(draft, AutoSendStop.user_action)
        if stopped:
            await note_stopped(session, draft, AutoSendStop.user_action, now)
            if cancelled:
                await decisions_repo.record(
                    session,
                    user_id=user_id,
                    work_chat_id=await work_chat_id_of(session, draft),
                    draft_id=draft.id,
                    decision=DraftDecisionKind.auto_cancelled,
                    during_auto_send=True,
                    at=now,
                )
        await session.commit()
    return stopped


async def work_chat_id_of(session: AsyncSession, draft: Draft) -> uuid.UUID:
    return (
        await session.execute(
            select(ConversationTurn.work_chat_id).where(
                ConversationTurn.id == draft.conversation_turn_id
            )
        )
    ).scalar_one()


async def _context(
    session: AsyncSession, draft: Draft, *, share: bool
) -> tuple[WorkChat, User] | None:
    """The draft's work chat and user. ``share``: read them ``FOR SHARE SKIP LOCKED`` (the caller
    holds the draft row, and chat / user writers lock in the other order, so waiting could
    deadlock); None when a writer holds one of them or the chat is gone: the caller leaves the
    draft for the next tick or treats it as not armed."""
    chat_query = select(WorkChat).where(
        WorkChat.user_id == draft.user_id,
        WorkChat.id
        == select(ConversationTurn.work_chat_id)
        .where(ConversationTurn.id == draft.conversation_turn_id)
        .scalar_subquery(),
    )
    user_query = select(User).where(User.id == draft.user_id)
    if share:
        chat_query = chat_query.with_for_update(read=True, skip_locked=True)
        user_query = user_query.with_for_update(read=True, skip_locked=True)
    chat = (await session.execute(chat_query.execution_options(populate_existing=True))).scalar()
    user = (await session.execute(user_query.execution_options(populate_existing=True))).scalar()
    if chat is None or user is None:
        return None
    return chat, user


def _evaluate(
    draft: Draft, chat: WorkChat, user: User, *, fire_at: datetime | None = None
) -> Eligibility:
    stored = load_stored_output(draft.output_json, draft.text)
    output = stored.output
    return eligibility(
        draft=draft,
        output=output,
        system_warnings=stored.system_warnings,
        context_warnings=list(output.context_warnings) if output is not None else [],
        chat=chat,
        user=user,
        fire_at=fire_at,
    )


async def current_reasons(session: AsyncSession, draft_id: uuid.UUID) -> tuple[BlockReason, ...]:
    """Why the draft would not be sent on its own right now (the card line after a failed
    arming); empty if it would be, or if it cannot be evaluated."""
    draft = await session.get(Draft, draft_id)
    context = await _context(session, draft, share=False) if draft is not None else None
    if draft is None or context is None:
        return ()
    return _evaluate(draft, *context).reasons


async def plan_arm(session: AsyncSession, *, draft_id: uuid.UUID, now: datetime) -> ArmPlan | None:
    """Before the card is posted (read-only): the plan it is posted with. None when the chat has
    auto-send off, the draft is gone or finished, or it already has a card (a re-post never
    arms)."""
    draft = await session.get(Draft, draft_id)
    if draft is None or draft.state != DraftState.pending or draft.control_message_id is not None:
        return None
    context = await _context(session, draft, share=False)
    if context is None:
        return None
    chat, user = context
    if not chat.auto_send:
        return None
    result = _evaluate(draft, chat, user)
    planned = now + timedelta(seconds=user.auto_send_window_s) if result.eligible else None
    return ArmPlan(planned, result.reasons)


async def commit_arm(
    session: AsyncSession,
    *,
    draft_id: uuid.UUID,
    plan: ArmPlan,
    control_message_id: int,
    stored_at: datetime,
) -> bool:
    """In the transaction that stored ``control_message_id`` after the card was posted (research
    A4a): re-check eligibility under the locks and start the countdown. The window counts from
    ``stored_at``, never earlier than the time the card shows. False: not armed (the draft
    changed meanwhile); the caller re-renders the card without the countdown."""
    if plan.planned_due_at is None:
        return False
    draft = (
        await session.execute(
            select(Draft)
            .where(Draft.id == draft_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if draft is None or draft.state != DraftState.pending:
        return False
    if draft.control_message_id != control_message_id:  # the card this plan was made for
        return False
    context = await _context(session, draft, share=True)
    if context is None:
        return False
    chat, user = context
    result = _evaluate(draft, chat, user)
    if not result.eligible:
        log.info(
            "auto_send_blocked",
            user_id=str(draft.user_id),
            draft_id=str(draft.id),
            reasons=[r.value for r in result.reasons],
            phase="arm",
        )
        return False
    due_at = max(plan.planned_due_at, stored_at + timedelta(seconds=user.auto_send_window_s))
    draft.auto_send_due_at = due_at
    draft.auto_send_stop = None
    await session.flush()
    log.info(
        "auto_send_armed",
        user_id=str(draft.user_id),
        draft_id=str(draft.id),
        due_at=due_at.isoformat(),
    )
    return True


async def stop_chat_countdowns(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    work_chat_ids: Sequence[uuid.UUID] | None,
    reason: AutoSendStop,
    at: datetime | None = None,
) -> list[uuid.UUID]:
    """Stop every running countdown of the given chats (``None``: all the user's chats) with one
    conditional update; a draft the step has claimed is left alone. Returns the draft ids whose
    cards must be re-rendered."""
    turns = select(ConversationTurn.id).where(ConversationTurn.user_id == user_id)
    if work_chat_ids is not None:
        turns = turns.where(ConversationTurn.work_chat_id.in_(work_chat_ids))
    result = await session.execute(
        update(Draft)
        .where(
            Draft.user_id == user_id,
            Draft.state == DraftState.pending,
            Draft.auto_send_due_at.is_not(None),
            Draft.auto_send_stop.is_(None),
            Draft.auto_approved_at.is_(None),
            Draft.conversation_turn_id.in_(turns),
        )
        .values(auto_send_stop=reason)
        .returning(Draft.id, Draft.conversation_turn_id)
        .execution_options(synchronize_session=False)
    )
    stopped = list(result.all())
    moment = at or utcnow()
    for draft_id, turn_id in stopped:
        await activity_repo.record(
            session,
            user_id,
            ActivityKind.auto_send_stopped,
            at=moment,
            draft_id=draft_id,
            conversation_turn_id=turn_id,
            meta={"reason": reason.value},
        )
        log.info(
            "auto_send_stopped", user_id=str(user_id), draft_id=str(draft_id), reason=reason.value
        )
    return [draft_id for draft_id, _ in stopped]


async def set_chat_auto_send(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    on: bool,
    source: AutoSendSource | None,
    now: datetime,
    reason: str = "user",
) -> list[uuid.UUID]:
    """Turn auto-send on or off for one chat (FR-001, FR-009). On needs an enabled, not excluded
    chat and a ``source`` (``autosend.chat_not_active`` otherwise); off also stops the chat's
    running countdowns. Returns the draft ids whose cards must be re-rendered. Locks the chat
    row; the caller commits."""
    chat = (
        await session.execute(
            select(WorkChat)
            .where(WorkChat.user_id == user_id, WorkChat.id == work_chat_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if chat is None:
        raise LocalizedError("alert.chat_not_found")
    if on:
        if source is None:
            raise ValueError("turning auto-send on needs a source")
        if not chat.enabled or chat.excluded:
            raise LocalizedError("autosend.chat_not_active")
        if chat.auto_send:
            return []
        chat.auto_send = True
        chat.auto_send_source = source
        chat.auto_send_changed_at = now
        await activity_repo.record(
            session,
            user_id,
            ActivityKind.auto_send_on,
            at=now,
            meta={"work_chat_id": str(chat.id), "source": source.value},
        )
        log.info(
            "auto_send_changed",
            user_id=str(user_id),
            work_chat_id=str(chat.id),
            on=True,
            source=source.value,
        )
        return []
    if not chat.auto_send:
        return []
    chat.auto_send = False
    chat.auto_send_changed_at = now
    await activity_repo.record(
        session,
        user_id,
        ActivityKind.auto_send_off,
        at=now,
        meta={"work_chat_id": str(chat.id), "reason": reason},
    )
    log.info(
        "auto_send_changed",
        user_id=str(user_id),
        work_chat_id=str(chat.id),
        on=False,
        reason=reason,
    )
    return await stop_chat_countdowns(
        session, user_id=user_id, work_chat_ids=[chat.id], reason=AutoSendStop.disabled, at=now
    )


async def turn_off_everywhere(
    session: AsyncSession, *, user_id: uuid.UUID, now: datetime
) -> tuple[int, list[uuid.UUID]]:
    """Turn auto-send off in every chat of the user at once (FR-010, SC-005): one ``auto_send_off``
    activity record (``all: true``), every running countdown stopped. Returns how many chats were
    turned off and the draft ids whose cards must be re-rendered. Locks the chats; the caller
    commits."""
    chats = list(
        (
            await session.execute(
                select(WorkChat)
                .where(WorkChat.user_id == user_id, WorkChat.auto_send.is_(True))
                .order_by(WorkChat.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if not chats:
        return 0, []
    for chat in chats:
        chat.auto_send = False
        chat.auto_send_changed_at = now
    await activity_repo.record(
        session,
        user_id,
        ActivityKind.auto_send_off,
        at=now,
        meta={"all": True, "reason": "user", "chats": len(chats)},
    )
    log.info("auto_send_changed", user_id=str(user_id), all=True, on=False, chats=len(chats))
    stopped = await stop_chat_countdowns(
        session,
        user_id=user_id,
        work_chat_ids=[chat.id for chat in chats],
        reason=AutoSendStop.disabled,
        at=now,
    )
    return len(chats), stopped


async def count_auto_send_chats(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many of the user's chats have auto-send on (the /settings line)."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(WorkChat)
                .where(WorkChat.user_id == user_id, WorkChat.auto_send.is_(True))
            )
        ).scalar_one()
    )


def window_label(seconds: int, lang: str) -> str:
    """``2 min`` / ``90 s`` in the user's language."""
    if seconds % 60 == 0:
        return t("unit.min", lang, n=seconds // 60)
    return t("unit.s", lang, n=seconds)


# --- the scheduler step ---------------------------------------------------------------------------


class CardUpdates(Protocol):
    """The card presenter (``telegram/control_bot/review.py``)."""

    async def show_status(self, user_id: uuid.UUID, draft_id: uuid.UUID, status: Msg) -> None: ...

    async def show_manual(
        self,
        user_id: uuid.UUID,
        draft_id: uuid.UUID,
        reasons: Sequence[str] | None = None,
        *,
        stopped: bool = False,
    ) -> None: ...

    async def show_outdated(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> None: ...


class DecisionAdvisor(Protocol):
    """Evaluates the offer and the suggestion to turn auto-send off after a decision."""

    async def after_decision(self, user_id: uuid.UUID, work_chat_id: uuid.UUID) -> None: ...


@dataclass(frozen=True)
class _Claimed:
    user_id: uuid.UUID
    draft_id: uuid.UUID
    work_chat_id: uuid.UUID
    timezone: str


@dataclass(frozen=True)
class _Blocked:
    user_id: uuid.UUID
    draft_id: uuid.UUID
    reasons: tuple[str, ...]


def _stop_for(reasons: Sequence[BlockReason]) -> AutoSendStop:
    """Why a countdown that reached its end is stopped (the card says the reasons themselves)."""
    if BlockReason.paused in reasons:
        return AutoSendStop.paused
    if BlockReason.outside_hours in reasons:
        return AutoSendStop.outside_hours
    if BlockReason.chat_off in reasons:
        return AutoSendStop.disabled
    return AutoSendStop.ineligible


class AutoSender:
    """The scheduler's auto-send step handler (``Scheduler.on_auto_sends``) and the only code that
    approves a send without a tap. For each draft whose window has ended it checks eligibility
    again under the locks: eligible drafts get ``auto_approved_at`` (committed with the step) and
    are sent right after, blocked ones are stopped and their cards say why. A failed send is
    never retried here; the user's own Send reuses the same ``random_id`` (#54)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: MessagingGateway,
        presenter: CardUpdates,
        *,
        clock: Clock = utcnow,
        marker: UnreadMarker | None = None,
        commitments: CommitmentExtractor | None = None,
        advisor: DecisionAdvisor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._presenter = presenter
        self._clock = clock
        self._marker = marker
        self._commitments = commitments
        self._advisor = advisor

    async def __call__(
        self, session: AsyncSession, drafts: Sequence[Draft]
    ) -> Callable[[], Awaitable[None]] | None:
        now = self._clock()
        claimed: list[_Claimed] = []
        blocked: list[_Blocked] = []
        for draft in drafts:
            if draft.auto_approved_at is not None:  # claimed earlier: recover it if stale
                if draft.auto_send_stop is None and (
                    now - draft.auto_approved_at >= FIRING_STALE_AFTER
                ):
                    draft.auto_send_stop = AutoSendStop.send_failed
                    await note_stopped(session, draft, AutoSendStop.send_failed, now)
                    blocked.append(
                        _Blocked(draft.user_id, draft.id, (AutoSendStop.send_failed.value,))
                    )
                continue
            if draft.expires_at <= now or draft.auto_send_due_at is None:
                continue  # the expiry step ends it
            context = await _context(session, draft, share=True)
            if context is None:  # a writer holds the chat or user: decide on the next tick
                continue
            chat, user = context
            result = _evaluate(draft, chat, user, fire_at=now)
            reasons: tuple[str, ...] = tuple(r.value for r in result.reasons)
            stop: AutoSendStop | None = None
            if now - draft.auto_send_due_at > FIRE_GRACE:
                stop = AutoSendStop.ineligible
                reasons = reasons or (AutoSendStop.ineligible.value,)
            elif not result.eligible:
                stop = _stop_for(result.reasons)
            if stop is not None:
                draft.auto_send_stop = stop
                await note_stopped(session, draft, stop, now)
                log.info(
                    "auto_send_blocked",
                    user_id=str(draft.user_id),
                    draft_id=str(draft.id),
                    reasons=list(reasons),
                    phase="fire",
                )
                blocked.append(_Blocked(draft.user_id, draft.id, reasons))
                continue
            draft.auto_approved_at = now  # Principle I: never approved_by_user_at
            await activity_repo.record(
                session,
                draft.user_id,
                ActivityKind.auto_approved,
                at=now,
                draft_id=draft.id,
                conversation_turn_id=draft.conversation_turn_id,
                meta={"window_s": user.auto_send_window_s},
            )
            claimed.append(_Claimed(draft.user_id, draft.id, chat.id, user.timezone))
        await session.flush()
        if not claimed and not blocked:
            return None

        async def after_commit() -> None:
            for claim in claimed:  # sends first: card edits and flood waits never delay them
                await self._send(claim)
            for item in blocked:
                await self._card(
                    self._presenter.show_manual(item.user_id, item.draft_id, item.reasons)
                )

        return after_commit

    async def _send(self, claim: _Claimed) -> None:
        log.info("auto_send_fired", user_id=str(claim.user_id), draft_id=str(claim.draft_id))
        try:
            result = await send_approved_draft(
                self._session_factory,
                self._gateway,
                user_id=claim.user_id,
                draft_id=claim.draft_id,
                auto=True,
                clock=self._clock,
                commitments=self._commitments,
                marker=self._marker,
            )
        except asyncio.CancelledError:  # shutdown: the recovery stops the stale claim later
            log.warning(
                "auto_send_cancelled", user_id=str(claim.user_id), draft_id=str(claim.draft_id)
            )
            raise
        except AlreadyDelivered:  # Telegram: an earlier attempt was delivered (#54)
            log.info("send_already_delivered", draft_id=str(claim.draft_id))
            await self._stop_after_claim(claim, AutoSendStop.send_failed)
            await self._card(
                self._presenter.show_status(
                    claim.user_id, claim.draft_id, Msg("status.auto_already_delivered")
                )
            )
            return
        except DraftOutdated:  # a message arrived after the claim: the existing outdated card
            await self._stop_after_claim(claim, AutoSendStop.outdated)
            await self._card(self._presenter.show_outdated(claim.user_id, claim.draft_id))
            return
        except (AccessSuspended, SendRefused) as exc:
            log.warning("auto_send_refused", draft_id=str(claim.draft_id), reason=str(exc))
            if await self._stop_after_claim(claim, AutoSendStop.ineligible):
                await self._card(
                    self._presenter.show_manual(
                        claim.user_id, claim.draft_id, (AutoSendStop.ineligible.value,)
                    )
                )
            return
        except Exception as exc:  # no automatic retry: the user decides (and Send reuses the key)
            log.error(
                "auto_send_failed", user_id=str(claim.user_id), draft_id=str(claim.draft_id),
                error=type(exc).__name__,
            )  # fmt: skip
            await self._stop_after_claim(claim, AutoSendStop.send_failed)
            await self._card(
                self._presenter.show_manual(
                    claim.user_id, claim.draft_id, (AutoSendStop.send_failed.value,)
                )
            )
            return
        sent_at = result.sent_at.astimezone(ZoneInfo(claim.timezone))
        await self._card(
            self._presenter.show_status(
                claim.user_id, claim.draft_id, Msg("status.sent_auto", time=f"{sent_at:%H:%M}")
            )
        )
        if self._advisor is not None:
            await self._card(self._advisor.after_decision(claim.user_id, claim.work_chat_id))

    async def _stop_after_claim(self, claim: _Claimed, stop: AutoSendStop) -> bool:
        """The draft is still pending after a claimed send did not happen: end the countdown with
        ``stop`` so the card is an ordinary one again. False: it is no longer pending."""
        try:
            async with self._session_factory() as session:
                draft = await _lock_draft(session, claim.user_id, claim.draft_id)
                if draft is None or draft.state != DraftState.pending or draft.auto_send_stop:
                    return False
                draft.auto_send_stop = stop
                await note_stopped(session, draft, stop, self._clock())
                await session.commit()
        except Exception as exc:
            log.error(
                "auto_send_stop_failed", draft_id=str(claim.draft_id), error=type(exc).__name__
            )
            return False
        return True

    @staticmethod
    async def _card(update: Awaitable[None]) -> None:
        """A card update or advisor call after the commit: one failure never blocks the others."""
        try:
            await update
        except Exception as exc:
            log.warning("auto_send_card_update_failed", error=type(exc).__name__)


# --- the offer and the suggestion to turn auto-send off (research A7) ------------------------


def should_offer(rate: decisions_repo.ChatRate, chat: WorkChat, user: User, now: datetime) -> bool:
    """FR-015, FR-016, FR-018: at least 20 counted decisions in 30 days and a share sent unchanged
    strictly above 60 % (integer arithmetic, 12 of 20 is exactly 60 % and does not qualify),
    auto-send off, not opted out, not snoozed, and no offer in the last 24 hours."""
    if chat.auto_send or chat.auto_send_offer == AutoSendOfferState.opted_out:
        return False
    if rate.total < OFFER_MIN_DECISIONS or rate.unchanged * 100 <= rate.total * round(
        OFFER_RATE * 100
    ):
        return False
    if chat.auto_send_offer_not_before is not None and chat.auto_send_offer_not_before > now:
        return False
    last = user.auto_send_last_offer_at
    return last is None or now - last >= OFFER_GAP


def should_warn(outcomes: Sequence[bool], chat: WorkChat, now: datetime) -> bool:
    """FR-020: auto-send is on, the chat has at least ``WARN_SAMPLE`` drafts that had a countdown,
    more than ``WARN_MAX_INTERVENTIONS`` of the last ``WARN_SAMPLE`` needed the user, and the
    last suggestion is older than ``WARN_GAP``."""
    if not chat.auto_send or len(outcomes) < WARN_SAMPLE:
        return False
    if sum(1 for intervened in outcomes[:WARN_SAMPLE] if intervened) <= WARN_MAX_INTERVENTIONS:
        return False
    return chat.auto_send_warned_at is None or now - chat.auto_send_warned_at >= WARN_GAP


@dataclass(frozen=True)
class _Notice:
    """A message the advisor posts after its leases were committed."""

    chat_id: int
    text: str
    markup: InlineKeyboardMarkup


class AutoSendAdvisor:
    """Offers auto-send for a chat the user keeps sending unchanged, and suggests turning it off
    when the user keeps stepping in (FR-015..FR-020). :meth:`after_decision` runs after a
    decision's transaction committed (``review.py``, ``AutoSender``). It never raises: a failure
    is logged and the action that triggered it stays done. The leases (offer state, snooze,
    ``users.auto_send_last_offer_at``, ``auto_send_warned_at``) are committed *before* the message
    is posted, so a crash can never repeat a message."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        *,
        clock: Clock = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._bot = bot
        self._clock = clock

    async def after_decision(self, user_id: uuid.UUID, work_chat_id: uuid.UUID) -> None:
        try:
            notice = await self._lease(user_id, work_chat_id)
            if notice is not None:
                await self._bot.send_message(
                    notice.chat_id, notice.text, reply_markup=notice.markup
                )
        except Exception as exc:
            log.error(
                "autosend_advisor_failed",
                user_id=str(user_id),
                work_chat_id=str(work_chat_id),
                error=type(exc).__name__,
            )

    async def _lease(self, user_id: uuid.UUID, work_chat_id: uuid.UUID) -> _Notice | None:
        now = self._clock()
        async with self._session_factory() as session:
            chat = (
                await session.execute(
                    select(WorkChat)
                    .where(WorkChat.user_id == user_id, WorkChat.id == work_chat_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            user = await session.get(User, user_id)
            if chat is None or user is None:
                return None
            notice = (
                await self._warn(session, chat, user, now)
                if chat.auto_send
                else await self._offer(session, chat, user, now)
            )
            await session.commit()
        return notice

    async def _offer(
        self, session: AsyncSession, chat: WorkChat, user: User, now: datetime
    ) -> _Notice | None:
        if not chat.enabled or chat.excluded:
            return None
        rate = await decisions_repo.chat_rate(
            session, user_id=user.id, work_chat_id=chat.id, since=now - OFFER_WINDOW
        )
        if not should_offer(rate, chat, user, now):
            return None
        # One offer per 24 h across all chats (FR-018): the conditional update is the lease, and
        # a concurrent offer for another chat waits for it and then matches no row.
        leased = await session.execute(
            update(User)
            .where(
                User.id == user.id,
                (User.auto_send_last_offer_at.is_(None))
                | (User.auto_send_last_offer_at <= now - OFFER_GAP),
            )
            .values(auto_send_last_offer_at=now)
            .returning(User.id)
            .execution_options(synchronize_session=False)
        )
        if leased.first() is None:
            return None
        chat.auto_send_offer = AutoSendOfferState.offered
        chat.auto_send_offer_not_before = now + OFFER_SNOOZE
        await activity_repo.record(
            session,
            user.id,
            ActivityKind.auto_send_offered,
            at=now,
            meta={
                "work_chat_id": str(chat.id),
                "unchanged": rate.unchanged,
                "total": rate.total,
            },
        )
        log.info(
            "auto_send_offered",
            user_id=str(user.id),
            work_chat_id=str(chat.id),
            unchanged=rate.unchanged,
            total=rate.total,
        )
        lang = user.language
        text = t(
            "autosend.offer",
            lang,
            chat=chat.title or t("common.chat", lang),
            unchanged=rate.unchanged,
            total=rate.total,
            window=window_label(user.auto_send_window_s, lang),
        )
        markup = keyboard(
            [
                [
                    button(
                        t("btn.autosend_offer_on", lang),
                        format_callback("autooffer", chat.id, "on"),
                    ),
                    button(
                        t("btn.autosend_offer_later", lang),
                        format_callback("autooffer", chat.id, "later"),
                    ),
                ],
                [
                    button(
                        t("btn.autosend_offer_never", lang),
                        format_callback("autooffer", chat.id, "never"),
                    )
                ],
            ]
        )
        return _Notice(user.telegram_control_chat_id, text, markup)

    async def _warn(
        self, session: AsyncSession, chat: WorkChat, user: User, now: datetime
    ) -> _Notice | None:
        outcomes = await decisions_repo.recent_auto_outcomes(
            session, user_id=user.id, work_chat_id=chat.id, limit=WARN_SAMPLE
        )
        if not should_warn(outcomes, chat, now):
            return None
        chat.auto_send_warned_at = now
        interventions = sum(1 for intervened in outcomes if intervened)
        log.info(
            "auto_send_warned",
            user_id=str(user.id),
            work_chat_id=str(chat.id),
            interventions=interventions,
            sample=len(outcomes),
        )
        lang = user.language
        text = t(
            "autosend.warn",
            lang,
            chat=chat.title or t("common.chat", lang),
            interventions=interventions,
            sample=len(outcomes),
        )
        markup = keyboard(
            [
                [
                    button(
                        t("btn.autosend_warn_off", lang),
                        format_callback("chatauto", chat.id, "off"),
                    ),
                    button(
                        t("btn.autosend_warn_keep", lang),
                        format_callback("autowarn", chat.id, "keep"),
                    ),
                ]
            ]
        )
        return _Notice(user.telegram_control_chat_id, text, markup)
