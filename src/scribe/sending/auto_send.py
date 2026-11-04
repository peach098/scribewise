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

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from scribe.context.task_dates import MISMATCH_PREFIX
from scribe.db.models import (
    Draft,
    User,
    UserState,
    WorkChat,
)
from scribe.drafting.injection import hard_stops
from scribe.drafting.placeholders import PLACEHOLDER
from scribe.llm.provider import DraftOutput
from scribe.logging import get_logger
from scribe.scheduling.working_hours import next_allowed

log = get_logger("scribe.sending")

# Thresholds of the offer and the suggestion to turn auto-send off (data-model.md).
OFFER_MIN_DECISIONS = 20
OFFER_RATE = 0.60  # strictly greater
OFFER_WINDOW = timedelta(days=30)
OFFER_SNOOZE = timedelta(days=30)
OFFER_GAP = timedelta(hours=24)
WARN_SAMPLE = 10
WARN_MAX_INTERVENTIONS = 4  # strictly more
WARN_GAP = timedelta(days=7)

# Warning codes that do not stop an automatic send. Empty on purpose: a warning added later must
# not silently become auto-sendable (research A4).
AUTO_SEND_ALLOWED_WARNINGS: frozenset[str] = frozenset()

# A countdown that ended longer ago than this (the service was down) is not sent: the user's tap
# on "Don't send automatically" may still be waiting in Telegram's queue (research A3).
FIRE_GRACE = timedelta(minutes=10)

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
