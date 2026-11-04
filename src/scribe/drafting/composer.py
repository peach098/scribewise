"""Draft composition pipeline (contracts/internal-interfaces.md "Draft composition pipeline").

    turn -> candidates (history.load_candidates) + open tasks (TaskSource, tolerant)
            + open commitments to the contact (and, in a group, to the person who wrote)
         -> LLMRouter.filter_context (ids only; deterministic fallback on failure)
         -> deadlines of the selected tasks from their comments / description
            (LLMRouter.extract_deadlines on the cheap route, only for texts the parser left)
         -> select profile + chat overrides + the user's grammatical gender
            (``users.grammatical_gender`` as ``DraftRequest.user``, outside ``profile``, so the
            global and every contact profile get it; issue #46)
         -> LLMRouter.draft_reply (the user's tier; economy at the daily soft cap, and no draft
            at all at the hard cap: ``DraftingPaused``, issue #74, ``drafting/budget.py``)
         -> sources must reference request ids (regenerate once, then strip + needs_info)
         -> conflicts = model ∪ deterministic backstop (exactly one requested date, not a bare
            "today" and not in the past, earlier than a selected commitment's / unfinished
            task's due date)
         -> sensitive flags = keywords ∪ model
         -> injection checks (``drafting/injection.py``): instruction-like incoming text and
            action claims in the draft add the hard-stop system warnings
            ``suspected_injection`` / ``action_claim`` (the model cannot raise or clear them)
         -> persist Draft (pending, remind/expiry, provider/model/usage) + activity record

:func:`compose_output` is the database-free core (unit tests, eval runner); :class:`Composer`
loads the turn, runs the core outside any transaction, then persists.

US3: tasks enter the request as ``task:<provider id>`` and commitments as ``commit:<uuid>``; the
source check below covers both, and the filter route may only select offered ids. Tasks are
fetched after the database session is closed, through the process-wide
:func:`~scribe.context.tasks.service.default_task_sources` (60 s per-user cache, short
timeouts); any failure drafts with ``tasks=[]`` and the system warning ``tasks_unavailable``.
A task's ``due`` is its effective deadline (``context/tasks/deadlines.py``): the due field, or a
newer deadline from a comment, or one from the description, with ``deadline_source`` and a
``deadline_note`` quoting it. After the filter route, the selected tasks' remaining comments are
offered to ``extract.deadlines`` (``ComposeInput.task_details``; never with the deterministic
fallback, which passes every task).
The first composer also installs a :class:`~scribe.context.commitments.CommitmentExtractor` as
the process default that ``send.py`` and ``intake/events.py`` use for outgoing messages; later
composers with the same session factory reuse it (``ensure_default_extractor``).

Warnings the *system* detects (``filter_unavailable``, ``unsupported_sources``,
``weak_style_profile`` when the selected profile is weak, ``tasks_unavailable`` when the task
source fails, ``tasks_partial`` when some boards could not be read, ``out_of_office`` when the
user is out of office and the draft is an out-of-office reply, the hard stops
``suspected_injection`` / ``action_claim`` from ``drafting/injection.py``, and the reply gate's
``drafted_by_habit`` / ``drafted_on_request`` that the scheduler passes in as
``extra_system_warnings``, specs/004-skip-closing-messages) are kept in
``system_warnings`` next to the model output in ``drafts.output_json``; ``DraftOutput`` itself
stays exactly the model-facing contract. ``drafts.output_json`` keeps only metadata in the clear
and every content-bearing field encrypted (``drafting/stored_output.py``); the draft text lives
in ``drafts.text`` only. The card renders the union of
``output.context_warnings`` and ``system_warnings``. ``reply_to_message_id`` (also system-side)
is the ``messages.id`` of the newest incoming message the draft was composed for: ``send.py``
quotes that message, not one that joined the turn later.

The turn's newest message is snapshotted when the messages are loaded. If it changed by the
time the draft is stored (a message joined while the LLM was working), or the superseded
draft had been flagged outdated meanwhile, the new draft is stored ``outdated`` so the card
shows the "New messages arrived" banner instead of offering a one-tap send (FR-015).

specs/004 (research S1, S2, S8, S10): when the draft may search earlier conversations
(``ComposeInput.search_scope`` and an :class:`~scribe.context.earlier.EarlierSearch`), the
filter call also decides whether older context is needed and suggests search queries; it then
runs even when there are no candidates. A needed search runs over the sender's enabled work
chats, a second filter call ranks the linked matches (≤ 10, within ``10 s − search time``,
at least 2 s), and the ranked ones join ``DraftRequest.history`` as ``earlier:<n>`` after the
recent history, oldest first. Failures never block the draft: ``earlier_search_unavailable``,
``earlier_search_no_match``, ``earlier_search_off`` (and ``filter_unavailable`` when a filter
call fails) explain what happened on the card. Cited matches are kept in the draft
(``stored_output.earlier_messages``); one ``earlier_search`` log event per draft has counts and
timings only.

Issue #73: 🔁 Regenerate on unchanged inputs. The filter's selection (and the ranked earlier
matches, by Telegram ids) is kept in ``drafts.llm_usage_json`` (``drafting/selection.py``). A
regenerate of a draft that is not outdated and whose stored selection covers the turn's current
``last_message_id`` applies it to the fresh candidates and fetches the earlier matches again
by id: no filter, search or ranking call, only the drafting call (which writes a new text).
Anything that no longer fits (a selected item gone, search switched on or off, the fetch
failing) selects afresh. ``extract.deadlines`` answers are memoised for the day
(``DeadlineMemo``, process-wide). ``llm_usage_json.regenerations`` counts regenerates on the
same messages for the cap in ``review.py``.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.context import comment_commitments, task_dates
from scribe.context import commitments as commitments_ctx
from scribe.context.earlier import (
    EarlierMatch,
    EarlierRef,
    EarlierSearch,
    EarlierSearchResult,
    SearchChat,
    SearchScope,
    excerpt,
)
from scribe.context.history import Candidates, load_candidates
from scribe.context.tasks import deadlines as deadlines_ctx
from scribe.context.tasks.base import Task, is_done_status
from scribe.context.tasks.service import TaskSources, default_task_sources, task_ref
from scribe.db.models import (
    ActivityKind,
    Contact,
    ContactKind,
    ConversationTurn,
    Draft,
    DraftState,
    DroppedReason,
    TaskProvider,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import BudgetCaps, DraftingPaused, budget_level, drafting_tier
from scribe.drafting.flags import detect_sensitive, merge_sensitive
from scribe.drafting.injection import ACTION_CLAIM, SUSPECTED_INJECTION, injection_warnings
from scribe.drafting.promises import detect_promise, merge_commitment
from scribe.drafting.selection import (
    REGENERATIONS_KEY,
    SELECTION_KEY,
    StoredSelection,
    regenerations,
    stored_selection,
    task_fingerprint,
)
from scribe.drafting.stored_output import EXCERPT_CHARS, REPLY_TO_KEY, StoredEarlier, pack_output
from scribe.llm.provider import (
    ChatOverrides,
    CommitmentRef,
    ContactInfo,
    DraftOutput,
    DraftRequest,
    FilterCandidates,
    FilterContextOutput,
    FilterContextRequest,
    HistoryItem,
    MessageItem,
    ProfileContext,
    ProviderError,
    TaskItem,
    Tier,
    UserContext,
    UserStateContext,
)
from scribe.llm.registry import FILTER_MAX_EARLIER, FILTER_MAX_HISTORY, LLMRouter, RoutedResult
from scribe.logging import get_logger
from scribe.style.profiles import (
    effective_user_rules,
    is_weak,
    profile_context,
    select_profile,
)

log = get_logger("scribe.drafting")

SystemWarning = Literal[
    "filter_unavailable",
    "unsupported_sources",
    "tasks_unavailable",
    "tasks_partial",
    "weak_style_profile",
    "out_of_office",
    "drafted_by_habit",  # 004 FR-005: the user usually replies to closings in this chat
    "drafted_on_request",  # 004 FR-013: "Draft anyway" on a skipped message
    "earlier_search_unavailable",
    "earlier_search_no_match",
    "earlier_search_off",
    "suspected_injection",
    "action_claim",
]
SYSTEM_WARNINGS: tuple[str, ...] = (
    "filter_unavailable",
    "unsupported_sources",
    "tasks_unavailable",
    "tasks_partial",
    "weak_style_profile",
    "out_of_office",
    "drafted_by_habit",
    "drafted_on_request",
    "earlier_search_unavailable",
    "earlier_search_no_match",
    "earlier_search_off",
    SUSPECTED_INJECTION,
    ACTION_CLAIM,
)
# specs/004 FR-S08: search plus ranking stay within 10 s; ranking gets at least 2 s.
EARLIER_BUDGET_S = 10.0
MIN_RANK_S = 2.0
_SEARCH_FAILED = frozenset({"failed", "rate_limited", "timeout", "not_started"})
# Set by the reply gate, not by composition: a regenerated draft keeps them (004).
GATE_SYSTEM_WARNINGS = frozenset({"drafted_by_habit", "drafted_on_request"})
OOO_DEFAULT_TEXT = "I'm out of office."
_PLACEHOLDER = re.compile(r"\[\[[^\[\]]+\]\]")


class ComposeError(Exception):
    """The turn cannot be drafted (gone, wrong state, no messages left)."""


class DraftNoLongerPending(ComposeError):
    """Regenerate raced with approve/reject/expiry: the old draft is no longer pending."""


SYSTEM_OUTPUT_KEYS = ("system_warnings", REPLY_TO_KEY)


@dataclass(frozen=True)
class _Snapshot:
    """What a draft covers, taken when the turn's messages are loaded."""

    last_message_id: uuid.UUID | None
    old_outdated: bool  # the draft being regenerated was already outdated
    # The message the reply answers (sent as a reply to it where appropriate): the contact's
    # last own message, not a forward that joined after it (#47).
    reply_to_message_id: uuid.UUID | None = None
    # Issue #73: regenerates of the draft being replaced on the same messages (None: no draft
    # is being replaced, or it was outdated: the new draft covers new messages).
    old_regenerations: int | None = None


# --- database-free core -------------------------------------------------------------------------


@dataclass
class ComposeInput:
    incoming: list[MessageItem]
    history: list[HistoryItem]
    contact: ContactInfo
    profile: ProfileContext
    # Specs/013: the user's own rules (``AppliedRules.user_rules_json()``), sent as their own
    # block, not inside the profile.
    user_rules: dict[str, Any] = field(default_factory=dict)
    tier: Tier = "quality"
    chat_overrides: ChatOverrides = field(default_factory=ChatOverrides)
    tasks: list[TaskItem] = field(default_factory=list)
    commitments: list[CommitmentRef] = field(default_factory=list)
    user_state: UserStateContext = field(default_factory=UserStateContext)
    # Issue #46: ``users.grammatical_gender`` (None: not set), sent as ``DraftRequest.user``.
    user: UserContext = field(default_factory=UserContext)
    system_warnings: list[str] = field(default_factory=list)
    # The local date of the newest incoming message: the drafting model's ``today`` and what
    # resolves "Wednesday" for the conflict backstop. None: no backstop, no anchor.
    reference_date: date | None = None
    timezone: str | None = None  # the user's IANA time zone
    # The loaded tasks behind ``tasks`` (by task ref), for the deadline refinement of the ones
    # the filter selects. Empty (eval threads, tests): no refinement.
    task_details: dict[str, Task] = field(default_factory=dict)
    # specs/004: None disables the search over earlier conversations for this draft.
    search_scope: SearchScope | None = None
    search_chats: list[SearchChat] = field(default_factory=list)
    search_enabled: bool = True  # users.search_earlier_messages
    # Issue #73: the selection of the draft being regenerated, when the turn's newest message
    # is unchanged; used instead of filter, earlier search and ranking when it still applies.
    reuse: StoredSelection | None = None
    # Issue #67: whose LLM spend these calls are (None: eval threads, tests).
    user_id: uuid.UUID | None = None


@dataclass(frozen=True)
class _KeptSelection:
    """A fresh selection to store with the draft (task refs are fingerprinted only when stored:
    the database-free core runs without a keyring)."""

    history_ids: tuple[str, ...]
    task_refs: tuple[str, ...]
    commitment_ids: tuple[str, ...]
    earlier: tuple[EarlierRef, ...]
    warnings: tuple[str, ...]
    search: bool

    def stored(self) -> StoredSelection:
        return StoredSelection(
            history_ids=self.history_ids,
            task_fingerprints=tuple(task_fingerprint(ref) for ref in self.task_refs),
            commitment_ids=self.commitment_ids,
            earlier=self.earlier,
            warnings=self.warnings,
            search=self.search,
        )


# Selection-stage warnings after which a regenerate tries the filter / the search again.
_RETRY_WARNINGS = frozenset({"filter_unavailable", "earlier_search_unavailable"})


@dataclass
class Composition:
    output: DraftOutput
    system_warnings: list[str]
    request: DraftRequest
    filter_result: RoutedResult[FilterContextOutput] | None
    compose_results: list[RoutedResult[DraftOutput]]
    rank_result: RoutedResult[FilterContextOutput] | None = None
    earlier: EarlierSearchResult | None = None
    cited_earlier: list[EarlierMatch] = field(default_factory=list)
    offered_earlier: list[EarlierMatch] = field(default_factory=list)  # in the draft request
    reused: StoredSelection | None = None  # issue #73: the stored selection this draft reused
    kept: _KeptSelection | None = None  # issue #73: the fresh selection, to store

    def stored_selection(self) -> StoredSelection | None:
        """What ``llm_usage_json.selection`` keeps for the next regenerate (issue #73)."""
        if self.reused is not None:
            return self.reused
        return self.kept.stored() if self.kept is not None else None

    @property
    def final(self) -> RoutedResult[DraftOutput]:
        return self.compose_results[-1]

    @property
    def cost_usd(self) -> float:
        total = sum(r.cost_usd for r in self.compose_results)
        for extra in (self.filter_result, self.rank_result):
            total += extra.cost_usd if extra is not None else 0.0
        return total

    def stored_earlier(self) -> list[StoredEarlier]:
        """The cited earlier messages as kept in the draft (research S7)."""
        queries = self.earlier.queries if self.earlier is not None else ()
        return [
            StoredEarlier(
                ref=match.ref,
                chat_id=match.found.chat_id,
                message_id=match.found.message_id,
                sent_at=match.found.sent_at,
                chat_title=match.chat_title,
                sender=match.sender_name(),
                excerpt=excerpt(match.found.text, queries, EXCERPT_CHARS),
            )
            for match in self.cited_earlier
        ]

    def output_json(self, reply_to_message_id: str | None = None) -> dict[str, Any]:
        """``drafts.output_json``: plain metadata, content encrypted (``stored_output``)."""
        return pack_output(
            self.output,
            self.system_warnings,
            reply_to_message_id,
            earlier_messages=self.stored_earlier(),
        )

    def usage_json(self) -> dict[str, Any]:
        """``drafts.llm_usage_json``: per-route metadata and cost, never content."""
        usage = self.compose_results[0].usage
        for result in self.compose_results[1:]:
            usage = usage + result.usage
        compose = {
            **self.final.usage_record(),
            **usage.model_dump(),
            "cost_usd": sum(r.cost_usd for r in self.compose_results),
            "calls": len(self.compose_results),
        }
        usage_json: dict[str, Any] = {
            "filter": self.filter_result.usage_record() if self.filter_result else None,
            "compose": compose,
            "cost_usd": self.cost_usd,
        }
        if self.rank_result is not None:
            usage_json["rank"] = self.rank_result.usage_record()
        if self.reused is not None:
            usage_json["selection_reused"] = True
        return usage_json


def _add(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


def invalid_source_ids(output: DraftOutput, request: DraftRequest) -> list[str]:
    """Ids in ``sources``/``conflicts`` that the request did not offer (or of the wrong kind).
    Earlier messages found by search are offered as history (``earlier:<n>``), so they count
    as ``message`` sources exactly when the request carries them."""
    message_ids = {m.id for m in request.incoming_messages} | {h.id for h in request.history}
    by_kind = {
        "message": message_ids,
        "task": {t.id for t in request.tasks},
        "commitment": {c.id for c in request.commitments},
    }
    bad = [s.id for s in output.sources if s.id not in by_kind[s.kind]]
    bad += [c.with_id for c in output.conflicts if c.with_id not in by_kind[c.with_kind]]
    return bad


def _strip_invalid(output: DraftOutput, request: DraftRequest) -> DraftOutput:
    bad = set(invalid_source_ids(output, request))
    return output.model_copy(
        update={
            "sources": [s for s in output.sources if s.id not in bad],
            "conflicts": [c for c in output.conflicts if c.with_id not in bad],
            "needs_info": True,
        }
    )


SelectedContext = tuple[
    list[HistoryItem], list[TaskItem], list[CommitmentRef], RoutedResult[FilterContextOutput] | None
]
MAX_TASK_CANDIDATES = 100


async def _select_context(
    router: LLMRouter, inp: ComposeInput, warnings: list[str], *, always: bool = False
) -> SelectedContext:
    """``always``: call the filter even without candidates (it decides on the search)."""
    if not (always or inp.history or inp.tasks or inp.commitments):
        return [], [], [], None  # nothing to choose from: skip the call
    request = FilterContextRequest(
        incoming_messages=inp.incoming,
        candidates=FilterCandidates(
            history=inp.history, tasks=inp.tasks, commitments=inp.commitments
        ),
    )
    try:
        result = await router.filter_context(request, user_tier=inp.tier, user_id=inp.user_id)
    except ProviderError:
        # Deterministic fallback: last 15 history messages + all tasks and commitments.
        _add(warnings, "filter_unavailable")
        return inp.history[-FILTER_MAX_HISTORY:], inp.tasks, inp.commitments, None
    chosen = result.output  # already restricted to offered ids and capped by the router
    history_ids, task_ids = set(chosen.history_ids), set(chosen.task_ids)
    commitment_ids = set(chosen.commitment_ids)
    same_day_tasks, same_day_commitments = _same_day_items(inp)
    task_ids |= same_day_tasks
    commitment_ids |= same_day_commitments
    return (
        [h for h in inp.history if h.id in history_ids],  # keep chronological order
        [t for t in inp.tasks if t.id in task_ids],
        [c for c in inp.commitments if c.id in commitment_ids],
        result,
    )


def _same_day_items(inp: ComposeInput) -> tuple[set[str], set[str]]:
    """Ids of the open tasks and commitments due on a day the incoming messages ask for.

    The filter sees no date anchor, so it cannot tell that «в четверг» is the day of an
    unrelated commitment; such a same-day item is what a double-booking conflict is about
    (issue #75), so it always reaches the draft."""
    reference = inp.reference_date
    if reference is None:
        return set(), set()
    days = {
        day
        for message in inp.incoming
        for day in commitments_ctx.requested_dates(message.text, reference)
    }
    if not days:
        return set(), set()
    tasks = {t.id for t in inp.tasks if t.due in days and not is_done_status(t.status)}
    commitments = {c.id for c in inp.commitments if c.due in days}
    return tasks, commitments


@dataclass
class _EarlierStep:
    """What the search over earlier conversations added to one draft."""

    items: list[HistoryItem] = field(default_factory=list)  # oldest first
    offered: list[EarlierMatch] = field(default_factory=list)
    result: EarlierSearchResult | None = None
    rank_result: RoutedResult[FilterContextOutput] | None = None


async def _earlier_context(
    router: LLMRouter,
    inp: ComposeInput,
    earlier_search: EarlierSearch,
    scope: SearchScope,
    filter_result: RoutedResult[FilterContextOutput] | None,
    filter_failed: bool,
    warnings: list[str],
) -> _EarlierStep:
    """Research S8: decide, search, rank; never raises for search or ranking failures."""
    step = _EarlierStep()
    started = time.monotonic()  # the filter call has ended
    decision = "not_needed"
    rank_ms = 0
    if filter_failed:  # no decision and no queries: nothing is searched
        decision = "skipped"
        if inp.search_enabled:  # the card never hides a skipped search
            _add(warnings, "earlier_search_unavailable")
    elif filter_result is not None and filter_result.output.needs_earlier_context:
        if not inp.search_enabled:
            _add(warnings, "earlier_search_off")
            decision = "off"
        else:
            decision = "searched"
            step.result = await earlier_search.search(
                scope, inp.search_chats, filter_result.output.search_queries
            )
            matches = step.result.matches
            if step.result.outcome in _SEARCH_FAILED:
                _add(warnings, "earlier_search_unavailable")
            elif not matches:
                _add(warnings, "earlier_search_no_match")
            else:
                rank_started = time.monotonic()
                selected = await _rank(router, inp, matches, started, step, warnings)
                rank_ms = int((time.monotonic() - rank_started) * 1000)
                if not selected:
                    _add(warnings, "earlier_search_no_match")
                step.offered = sorted(selected, key=lambda m: (m.found.sent_at, m.found.message_id))
                timezone = inp.timezone or "UTC"
                step.items = [m.history_item(timezone) for m in step.offered]
    _log_earlier(scope, decision, step, rank_ms, started)
    return step


async def _rank(
    router: LLMRouter,
    inp: ComposeInput,
    matches: list[EarlierMatch],
    started: float,
    step: _EarlierStep,
    warnings: list[str],
) -> list[EarlierMatch]:
    """The ranked matches; the newest linked ones when ranking fails or runs out of time."""
    timezone = inp.timezone or "UTC"
    budget = max(MIN_RANK_S, EARLIER_BUDGET_S - (time.monotonic() - started))
    try:
        async with asyncio.timeout(budget):
            step.rank_result = await router.rank_earlier(
                inp.incoming,
                [m.history_item(timezone) for m in matches],
                user_tier=inp.tier,
                user_id=inp.user_id,
            )
    except ProviderError:
        _add(warnings, "filter_unavailable")
        return matches[:FILTER_MAX_EARLIER]  # newest first
    except TimeoutError:
        _add(warnings, "earlier_search_unavailable")
        return matches[:FILTER_MAX_EARLIER]
    by_ref = {m.ref: m for m in matches}
    return [by_ref[ref] for ref in step.rank_result.output.history_ids if ref in by_ref]


def _log_earlier(
    scope: SearchScope, decision: str, step: _EarlierStep, rank_ms: int, started: float
) -> None:
    """Research S10: counts and timings only, never queries or message text (FR-S17)."""
    result = step.result
    stats = result.stats if result is not None else None
    log.info(
        "earlier_search",
        user_id=str(scope.user_id),
        turn_id=str(scope.turn_id) if scope.turn_id is not None else None,
        decision=decision,
        outcome=result.outcome if result is not None else None,
        chats=stats.chats if stats is not None else 0,
        queries=stats.queries if stats is not None else 0,
        requests=stats.requests if stats is not None else 0,
        matches=stats.matches if stats is not None else 0,
        dropped_unlinked=stats.dropped_unlinked if stats is not None else 0,
        used=len(step.items),
        duration_ms=stats.duration_ms if stats is not None else 0,
        rank_ms=rank_ms,
        total_ms=int((time.monotonic() - started) * 1000),
    )


async def compose_output(
    router: LLMRouter,
    inp: ComposeInput,
    *,
    earlier_search: EarlierSearch | None = None,
    deadline_memo: deadlines_ctx.DeadlineMemo | None = None,
) -> Composition:
    """Filter context, search earlier conversations when needed, draft, validate sources and
    flags. Raises ``ProviderError`` when the drafting call itself fails (the caller retries the
    turn later). A stored selection (``inp.reuse``) that still applies replaces the filter, the
    search and the ranking (issue #73)."""
    warnings = list(inp.system_warnings)
    scope = inp.search_scope if earlier_search is not None else None
    search_on = scope is not None and inp.search_enabled
    before = len(warnings)
    filter_result: RoutedResult[FilterContextOutput] | None = None
    kept: _KeptSelection | None = None
    reused = await _reused_context(inp, earlier_search, scope, search_on, warnings)
    if reused is not None:
        history, tasks, commitments, step = reused
    else:
        history, tasks, commitments, filter_result = await _select_context(
            router, inp, warnings, always=search_on
        )
        filter_failed = "filter_unavailable" in warnings[before:]
        step = _EarlierStep()
        if scope is not None and earlier_search is not None:
            step = await _earlier_context(
                router, inp, earlier_search, scope, filter_result, filter_failed, warnings
            )
        if filter_result is not None and not _RETRY_WARNINGS & set(warnings[before:]):
            kept = _KeptSelection(
                history_ids=tuple(h.id for h in history),
                task_refs=tuple(t.id for t in tasks),
                commitment_ids=tuple(c.id for c in commitments),
                earlier=tuple(EarlierRef.of(m) for m in step.offered),
                warnings=tuple(warnings[before:]),
                search=search_on,
            )
    # Chosen by the filter, now or for the draft being regenerated: only related items.
    filtered = reused is not None or filter_result is not None
    if filtered:  # only the tasks relevant to this turn, never all of them
        tasks = await _with_refined_deadlines(router, inp, tasks, deadline_memo)
    # specs/003 J12: the newest of due date / comment deadline / own promise; warn on a mismatch.
    tasks, mismatches = task_dates.reconcile_task_dates(
        tasks, inp.task_details, inp.commitments, inp.timezone or "UTC"
    )
    for mismatch in mismatches:
        _add(warnings, mismatch)
    request = DraftRequest(
        incoming_messages=inp.incoming,
        history=[*history, *step.items],
        contact=inp.contact,
        profile=inp.profile,
        user_rules=inp.user_rules,
        user=inp.user,
        chat_overrides=inp.chat_overrides,
        tasks=tasks,
        commitments=commitments,
        user_state=inp.user_state,
        today=inp.reference_date,
        timezone=inp.timezone,
    )

    results = [await router.draft_reply(request, user_tier=inp.tier, user_id=inp.user_id)]
    output = results[0].output
    if invalid_source_ids(output, request):
        log.warning(
            "draft_invalid_sources", attempt=1, count=len(invalid_source_ids(output, request))
        )
        results.append(await router.draft_reply(request, user_tier=inp.tier, user_id=inp.user_id))
        output = results[-1].output
        if invalid_source_ids(output, request):
            log.warning("draft_invalid_sources", attempt=2)
            output = _strip_invalid(output, request)
            _add(warnings, "unsupported_sources")

    if filtered:  # only items the filter selected as related
        output = _with_backstop_conflicts(output, inp, tasks, commitments)
    if output.placeholders or _PLACEHOLDER.search(output.text):
        output = output.model_copy(update={"needs_info": True})
    detected = detect_sensitive(output.text, *(m.text for m in inp.incoming))
    sensitive = merge_sensitive(output.flags.sensitive, detected)
    if sensitive != output.flags.sensitive:
        flags = output.flags.model_copy(update={"sensitive": sensitive})
        output = output.model_copy(update={"flags": flags})
    output = _with_detected_promise(output)
    for code in injection_warnings(output.text, (m.text for m in inp.incoming)):
        log.info("draft_injection_check", warning=code)  # codes only, never the text
        _add(warnings, code)
    cited = {s.id for s in output.sources if s.kind == "message"}
    return Composition(
        output=DraftOutput.model_validate(output.model_dump()),
        system_warnings=warnings,
        request=request,
        filter_result=filter_result,
        compose_results=results,
        rank_result=step.rank_result,
        earlier=step.result,
        cited_earlier=[m for m in step.offered if m.ref in cited],
        offered_earlier=list(step.offered),
        reused=inp.reuse if reused is not None else None,
        kept=kept,
    )


ReusedContext = tuple[list[HistoryItem], list[TaskItem], list[CommitmentRef], _EarlierStep]


async def _reused_context(
    inp: ComposeInput,
    earlier_search: EarlierSearch | None,
    scope: SearchScope | None,
    search_on: bool,
    warnings: list[str],
) -> ReusedContext | None:
    """Issue #73: the stored selection applied to the fresh candidates, or None (select afresh)
    when there is none, the search availability changed, a selected item is gone, or the
    earlier matches cannot be fetched again."""
    stored = inp.reuse
    if stored is None or stored.search != search_on:
        return None
    history_ids, commitment_ids = set(stored.history_ids), set(stored.commitment_ids)
    fingerprints = set(stored.task_fingerprints)
    history = [h for h in inp.history if h.id in history_ids]
    tasks = [t for t in inp.tasks if task_fingerprint(t.id) in fingerprints]
    commitments = [c for c in inp.commitments if c.id in commitment_ids]
    if (
        {h.id for h in history} != history_ids
        or len({t.id for t in tasks}) != len(fingerprints)
        or {c.id for c in commitments} != commitment_ids
    ):
        log.info("draft_selection_stale")
        return None
    step = _EarlierStep()
    if stored.earlier:
        if earlier_search is None or scope is None:
            return None
        offered = await earlier_search.refetch(scope, inp.search_chats, stored.earlier)
        if offered is None:
            return None
        step.offered = offered
        step.items = [m.history_item(inp.timezone or "UTC") for m in offered]
    for warning in stored.warnings:
        _add(warnings, warning)
    log.info(
        "draft_selection_reused",
        history=len(history),
        tasks=len(tasks),
        commitments=len(commitments),
        earlier=len(step.items),
    )
    return history, tasks, commitments, step


def _with_detected_promise(output: DraftOutput) -> DraftOutput:
    """Issue #24: raise ``creates_commitment`` when the draft text promises a future action the
    model did not flag (keyword safety net; a model flag is never removed)."""
    commitment = merge_commitment(output.flags.creates_commitment, detect_promise(output.text))
    if commitment == output.flags.creates_commitment:
        return output
    if not output.flags.creates_commitment.value:
        log.info("draft_commitment_backstop")
    flags = output.flags.model_copy(update={"creates_commitment": commitment})
    return output.model_copy(update={"flags": flags})


def _with_backstop_conflicts(
    output: DraftOutput,
    inp: ComposeInput,
    tasks: list[TaskItem],
    commitments: list[CommitmentRef],
) -> DraftOutput:
    """FR-008 / SC-006: add the conflicts the deterministic rule finds and the model missed:
    exactly one requested date (not a bare "today", not in the past) earlier than the due date
    of a selected open commitment or unfinished task."""
    reference = inp.reference_date
    if reference is None:
        return output
    requested = [
        day
        for message in inp.incoming
        for day in commitments_ctx.requested_dates(message.text, reference)
    ]
    found = commitments_ctx.find_conflicts(requested, commitments, tasks)
    known = {(c.with_kind, c.with_id) for c in output.conflicts}
    extra = [c for c in found if (c.with_kind, c.with_id) not in known]
    if not extra:
        return output
    log.info("draft_conflict_backstop", added=len(extra))
    return output.model_copy(update={"conflicts": [*output.conflicts, *extra]})


async def _with_refined_deadlines(
    router: LLMRouter,
    inp: ComposeInput,
    tasks: list[TaskItem],
    memo: deadlines_ctx.DeadlineMemo | None = None,
) -> list[TaskItem]:
    """The selected tasks with deadlines the LLM finds in comments the parser left."""
    selected = [inp.task_details[t.id] for t in tasks if t.id in inp.task_details]
    if not selected:
        return tasks
    timezone = inp.timezone or "UTC"
    refined = await deadlines_ctx.refine_deadlines(
        router, selected, timezone, memo=memo, user_id=inp.user_id
    )
    by_ref = {task_ref(task.id): task for task in refined}
    return [task_item(by_ref[t.id], timezone) if t.id in by_ref else t for t in tasks]


def task_item(task: Task, timezone: str = "UTC") -> TaskItem:
    """One task for the LLM: ``due`` is the effective deadline in the user's time zone."""
    return TaskItem(
        id=task_ref(task.id),
        title=task.title,
        status=task.status,
        due=task.due_on(timezone),
        assignees=task.assignees,
        deadline_source=task.deadline_source,
        deadline_note=deadlines_ctx.deadline_note(task.deadline, timezone),
    )


def task_items(tasks: list[Task], contact_name: str, timezone: str = "UTC") -> list[TaskItem]:
    """Filter candidates: tasks assigned to the contact first, then by due date (capped). Due
    dates are the user's local dates (``timezone`` = ``users.timezone``)."""
    name = contact_name.casefold().strip()

    def order(task: Task) -> tuple[bool, bool, date]:
        assigned = bool(name) and any(a.casefold().strip() == name for a in task.assignees)
        due = task.due_on(timezone)
        return (not assigned, due is None, due or date.max)

    return [task_item(task, timezone) for task in sorted(tasks, key=order)[:MAX_TASK_CANDIDATES]]


# --- pipeline with persistence ------------------------------------------------------------------


class Composer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm_router: LLMRouter,
        settings: Settings,
        *,
        clock: Clock = utcnow,
        task_sources: TaskSources | None = None,
        commitments: commitments_ctx.CommitmentExtractor | None = None,
        earlier_search: EarlierSearch | None = None,
        deadline_memo: deadlines_ctx.DeadlineMemo | None = None,
    ) -> None:
        self._session_factory = session_factory
        # Issue #73: process-wide, so the Composer each 🔁 Regenerate builds shares it.
        self.deadline_memo = deadline_memo or deadlines_ctx.default_memo()
        # specs/004: None (tests, old callers) drafts without searching earlier conversations.
        self.earlier_search = earlier_search
        self._router = llm_router
        self._settings = settings
        self._clock = clock
        self._task_sources = task_sources
        # Outgoing messages (manual in intake/events.py, agent sends in send.py) are extracted
        # by the process-wide extractor: the composer is the one component that is built with
        # both the session factory and the LLM router. Installing is idempotent, so the
        # Composer each 🔁 Regenerate builds reuses the service's instance (and the shutdown
        # drain sees every extraction). The service passes its instance explicitly.
        self.commitments = commitments or commitments_ctx.ensure_default_extractor(
            session_factory, llm_router, clock=clock
        )
        if commitments_ctx.default_extractor() is None:
            commitments_ctx.install_default_extractor(self.commitments)

    @property
    def task_sources(self) -> TaskSources:
        return self._task_sources or default_task_sources()

    async def compose(
        self,
        user_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        supersede_draft_id: uuid.UUID | None = None,
        extra_system_warnings: Sequence[str] = (),
    ) -> Draft:
        """Draft ``turn_id`` and store it as the turn's pending draft (turn -> pending_review).

        With ``supersede_draft_id`` (regenerate) the old draft becomes ``superseded`` in the same
        transaction that stores the new version, and only if it is still pending.
        ``extra_system_warnings`` (from the reply gate) are added to the draft's
        ``system_warnings`` and shown on the card.
        """
        inp, profile_id, snapshot = await self._load(
            user_id, turn_id, supersede_draft_id, extra_system_warnings
        )
        composition = await compose_output(
            self._router,
            inp,
            earlier_search=self.earlier_search,
            deadline_memo=self.deadline_memo,
        )
        return await self._persist(
            user_id, turn_id, composition, profile_id, supersede_draft_id, snapshot
        )

    async def _load(
        self,
        user_id: uuid.UUID,
        turn_id: uuid.UUID,
        supersede_draft_id: uuid.UUID | None,
        extra_system_warnings: Sequence[str] = (),
    ) -> tuple[ComposeInput, uuid.UUID | None, _Snapshot]:
        """(input, style profile id, what the input covers)."""
        now = self._clock()
        async with self._session_factory() as session:
            user = await users_repo.get_user(session, user_id)
            turn = await turns_repo.get_turn(session, user_id, turn_id)
            if user is None or turn is None:
                raise ComposeError("turn not found")
            # Issue #74: no draft at the daily hard cap; the economy tier from the soft cap on.
            level = await budget_level(
                session, user_id, BudgetCaps.from_settings(self._settings), now
            )
            if level == "hard":
                raise DraftingPaused("daily LLM hard cap reached")
            tier = drafting_tier(user.llm_tier.value, level)
            if tier != user.llm_tier.value:
                log.info("budget_economy_tier", user_id=str(user_id), turn_id=str(turn_id))
            chat = await chats_repo.get_work_chat(session, user_id, turn.work_chat_id)
            contact = await chats_repo.get_contact(session, user_id, turn.contact_id)
            if chat is None or contact is None:
                raise ComposeError("chat or contact not found")
            candidates = await load_candidates(
                session,
                turn,
                now=now,
                retention_days=self._settings.message_retention_days,
                timezone=user.timezone,
            )
            if not candidates.incoming:
                raise ComposeError("turn has no text messages left")
            profile = await select_profile(session, user_id, contact.id)
            # Issue #42: global user rules apply to a contact profile's drafts too (read now,
            # so a changed global rule reaches the next draft without a rebuild).
            user_rules = await effective_user_rules(session, user_id, profile, lang=user.language)
            old = (
                await drafts_repo.get_draft(session, user_id, supersede_draft_id)
                if supersede_draft_id is not None
                else None
            )
            fresh_old = old is not None and not old.outdated
            snapshot = _Snapshot(
                reply_to_message_id=(
                    candidates.last_incoming.id if candidates.last_incoming is not None else None
                ),
                last_message_id=turn.last_message_id,
                old_outdated=old is not None and old.outdated,
                old_regenerations=(
                    regenerations(old.llm_usage_json) if old is not None and fresh_old else None
                ),
            )
            # Issue #73: the old draft's selection, only while it covers the turn's newest
            # message (no message joined since it was composed).
            reuse = stored_selection(old.llm_usage_json) if old is not None and fresh_old else None
            if reuse is not None and reuse.last_message_id != str(turn.last_message_id):
                reuse = None
            # Commitments to the turn's contact; in a group also to the person who wrote.
            contact_ids = [contact.id]
            last = candidates.last_incoming
            if contact.kind == ContactKind.group and last is not None and last.sender_contact_id:
                contact_ids.append(last.sender_contact_id)
            commitments = [
                commitments_ctx.to_ref(c)
                for c in await commitments_ctx.open_for(session, user_id, contact_ids)
            ]
            reference_date = (
                last.sent_at.astimezone(ZoneInfo(user.timezone)).date()
                if last is not None
                else now.astimezone(ZoneInfo(user.timezone)).date()
            )
            contact_name = contact.display_name
            timezone = user.timezone
            search_scope, search_chats = await self._search_scope(
                session, user, turn_id, chat, contact, candidates
            )
            search_enabled = user.search_earlier_messages
            user_context = UserContext.model_validate(
                {"grammatical_gender": user.grammatical_gender}
            )
            # FR-025: while out of office, drafts become out-of-office replies (users.ooo_message
            # is non-NULL exactly then; "" means no suggested text).
            ooo = (
                (user.ooo_message or OOO_DEFAULT_TEXT)
                if user.state == UserState.out_of_office and user.ooo_message is not None
                else None
            )
        # Outside the session: a slow task source never holds a database connection.
        loaded = await self.task_sources.load_open_tasks(self._session_factory, user_id)
        commitments += await self._issue_commitments(
            user_id, loaded.tasks, [m.text for m in candidates.incoming], commitments
        )
        warnings: list[str] = []
        for warning in extra_system_warnings:
            _add(warnings, warning)
        if is_weak(profile):
            warnings.append("weak_style_profile")
        if ooo is not None:
            warnings.append("out_of_office")
        if loaded.unavailable:
            warnings.append("tasks_unavailable")
        elif loaded.partial:
            warnings.append("tasks_partial")
        inp = ComposeInput(
            incoming=candidates.incoming,
            history=candidates.history,
            contact=ContactInfo(display_name=contact.display_name, kind=contact.kind.value),
            profile=profile_context(profile),
            user_rules=user_rules.user_rules_json(),
            tier=tier,
            chat_overrides=ChatOverrides(
                language=chat.override_language, formality=chat.override_formality
            ),
            tasks=task_items(loaded.tasks, contact_name, timezone),
            task_details={task_ref(task.id): task for task in loaded.tasks},
            commitments=commitments,
            user_state=UserStateContext(ooo=ooo),
            user=user_context,
            system_warnings=warnings,
            reference_date=reference_date,
            timezone=timezone,
            search_scope=search_scope,
            search_chats=search_chats,
            search_enabled=search_enabled,
            reuse=reuse,
            user_id=user_id,
        )
        return inp, profile.id if profile is not None else None, snapshot

    async def _search_scope(
        self,
        session: AsyncSession,
        user: User,
        turn_id: uuid.UUID,
        chat: WorkChat,
        contact: Contact,
        candidates: Candidates,
    ) -> tuple[SearchScope | None, list[SearchChat]]:
        """specs/004 S4: who and where to search for this draft; (None, []) when the operator
        switch is off or the composer has no search."""
        if self.earlier_search is None or not self._settings.earlier_search_enabled:
            return None, []
        last = candidates.last_incoming
        sender_contact_id = last.sender_contact_id if last is not None else None
        sender = contact if sender_contact_id in (None, contact.id) else None
        if sender_contact_id is not None and sender is None:
            sender = await chats_repo.get_contact(session, user.id, sender_contact_id)
        if sender is not None and sender.kind == ContactKind.group:
            sender = None  # a group as a whole is not a person to search for
        sender_peer_id = sender.telegram_peer_id if sender is not None else None
        scope = SearchScope(
            user_id=user.id,
            turn_id=turn_id,
            turn_chat_id=chat.telegram_chat_id,
            sender_peer_id=sender_peer_id,
            user_peer_id=user.telegram_user_id,
            incoming_thread_top_id=last.reply_to_telegram_message_id if last is not None else None,
            known=candidates.telegram_ids,
        )
        chats: list[SearchChat] = []
        if user.search_earlier_messages:
            chats = await self.earlier_search.chats_for(
                session,
                user.id,
                chat,
                sender.id if sender is not None else None,
                sender_peer_id,
            )
        return scope, chats

    async def _issue_commitments(
        self,
        user_id: uuid.UUID,
        tasks: list[Task],
        incoming: list[str],
        already: list[CommitmentRef],
    ) -> list[CommitmentRef]:
        """Open commitments about the loaded Jira issues or an issue the incoming messages name
        (selected projects only, specs/003 J12), not yet among ``already``."""
        keys = [task.id for task in tasks if task.source == "jira"]
        async with self._session_factory() as session:
            conn = await tasks_conn_repo.get_connection(session, user_id)
            if conn is not None and conn.provider == TaskProvider.jira:
                projects = [str(k) for k in (conn.config_json or {}).get("project_keys") or []]
                for text in incoming:
                    keys += comment_commitments.issue_keys_in(text, projects)
            if not keys:
                return []
            rows = await commitments_repo.list_open_for_issues(session, user_id, keys)
        known = {c.id for c in already}
        return [ref for ref in map(commitments_ctx.to_ref, rows) if ref.id not in known]

    async def _persist(
        self,
        user_id: uuid.UUID,
        turn_id: uuid.UUID,
        composition: Composition,
        profile_id: uuid.UUID | None,
        supersede_draft_id: uuid.UUID | None,
        snapshot: _Snapshot,
    ) -> Draft:
        now = self._clock()
        final = composition.final
        remind_after_s, expire_after_s = self._settings.review_deadlines_s
        async with self._session_factory() as session:
            # The chat first (shared; disabling a chat locks it for update before its drafts and
            # turns): a draft is never stored in a chat that is being or was disabled.
            enabled = await session.scalar(
                select(WorkChat.enabled)
                .where(
                    WorkChat.user_id == user_id,
                    WorkChat.id
                    == select(ConversationTurn.work_chat_id)
                    .where(ConversationTurn.user_id == user_id, ConversationTurn.id == turn_id)
                    .scalar_subquery(),
                )
                .with_for_update(read=True)
            )
            if not enabled:
                await self._end_turn_chat_disabled(session, user_id, turn_id)
                raise ComposeError("the chat was disabled")
            # Lock order everywhere: draft, then turn (send, manual-reply drop, intake's outdated
            # flag), so concurrent changes wait instead of deadlocking.
            old: Draft | None = None
            if supersede_draft_id is not None:
                old = (
                    await session.execute(
                        select(Draft)
                        .where(Draft.user_id == user_id, Draft.id == supersede_draft_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if old is None or old.state != DraftState.pending:
                    raise DraftNoLongerPending("draft is no longer pending")
            # Locked: a manual reply that dropped the turn meanwhile is seen, never overwritten.
            turn = (
                await session.execute(
                    select(ConversationTurn)
                    .where(ConversationTurn.user_id == user_id, ConversationTurn.id == turn_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if turn is None or turn.state not in (TurnState.reading, TurnState.pending_review):
                raise ComposeError("turn is no longer waiting for a draft")
            # A message joined the turn while the LLM was working, or flagged the old draft
            # outdated meanwhile: the new draft does not cover it, so it keeps the banner
            # (FR-015). An old draft already outdated when regenerate started is covered now.
            outdated = turn.last_message_id != snapshot.last_message_id or (
                old is not None and old.outdated and not snapshot.old_outdated
            )
            covered = snapshot.reply_to_message_id
            if supersede_draft_id is not None:
                await drafts_repo.finalize(
                    session, user_id, supersede_draft_id, DraftState.superseded, at=now
                )
            version = await drafts_repo.next_version(session, user_id, turn_id)
            draft = await drafts_repo.create_draft(
                session,
                user_id,
                conversation_turn_id=turn_id,
                text=composition.output.text,
                output_json=composition.output_json(str(covered) if covered is not None else None),
                prepared_at=now,
                version=version,
                style_profile_id=profile_id,
                prompt_version=final.prompt_version,
                llm_provider=final.provider,
                llm_model=final.model,
                llm_usage_json=self._usage_json(composition, supersede_draft_id, snapshot),
                remind_after=timedelta(seconds=remind_after_s),
                expire_after=timedelta(seconds=expire_after_s),
            )
            draft.outdated = outdated
            turn.state = TurnState.pending_review
            output = composition.output
            await activity_repo.record(
                session,
                user_id,
                ActivityKind.draft_prepared,
                at=now,
                draft_id=draft.id,
                conversation_turn_id=turn_id,
                meta={
                    "version": version,
                    "provider": final.provider,
                    "model": final.model,
                    "cost_usd": composition.cost_usd,
                    "needs_info": output.needs_info,
                    "sensitive": output.flags.sensitive.value,
                    "sources": len(output.sources),
                    "system_warnings": composition.system_warnings,
                    "outdated": outdated,
                },
            )
            await session.commit()
        log.info(
            "draft_prepared",
            user_id=str(user_id),
            draft_id=str(draft.id),
            turn_id=str(turn_id),
            version=version,
            provider=final.provider,
            model=final.model,
            prompt_version=final.prompt_version,
            cost_usd=composition.cost_usd,
            needs_info=output.needs_info,
            sensitive=output.flags.sensitive.value,
            system_warnings=composition.system_warnings,
        )
        return draft

    @staticmethod
    def _usage_json(
        composition: Composition, supersede_draft_id: uuid.UUID | None, snapshot: _Snapshot
    ) -> dict[str, Any]:
        """Usage plus, for the next 🔁 Regenerate (issue #73), the selection with the newest
        message it covers and the count of regenerates on the same messages."""
        usage = composition.usage_json()
        selection = composition.stored_selection()
        if selection is not None and snapshot.last_message_id is not None:
            covered = replace(selection, last_message_id=str(snapshot.last_message_id))
            usage[SELECTION_KEY] = covered.to_json()
        if supersede_draft_id is not None:
            previous = snapshot.old_regenerations
            usage[REGENERATIONS_KEY] = 0 if previous is None else previous + 1
        return usage

    @staticmethod
    async def _end_turn_chat_disabled(
        session: AsyncSession, user_id: uuid.UUID, turn_id: uuid.UUID
    ) -> None:
        """The chat was disabled while this turn was read and drafted: drop it, no draft."""
        turn = (
            await session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.user_id == user_id, ConversationTurn.id == turn_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if turn is not None and turn.state == TurnState.reading:
            turn.state = TurnState.dropped
            turn.dropped_reason = DroppedReason.chat_disabled
            await session.commit()


__all__ = [
    "GATE_SYSTEM_WARNINGS",
    "SYSTEM_WARNINGS",
    "ComposeError",
    "ComposeInput",
    "Composer",
    "Composition",
    "DraftNoLongerPending",
    "REPLY_TO_KEY",
    "SYSTEM_OUTPUT_KEYS",
    "compose_output",
    "invalid_source_ids",
    "task_item",
    "task_items",
]
