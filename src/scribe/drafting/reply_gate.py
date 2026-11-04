"""Reply gate: does a due turn need a reply at all? (specs/004-skip-closing-messages,
specs/013-noncommittal-replies, contracts/reply-gate.md, research R1-R5)

    turn -> only forwarded messages? skip as ``forwarded`` (issue #47; no model call, any setting)
         -> pre-gate (is_skip_candidate: not empty, not very long, no '?' unless the user has
            rules; no I/O)
         -> LLMRouter.classify_reply_needed (candidates only; small model; sees the user's rules)
         -> a confident user-rule decision stands, in either direction (013), else
            skip only on a confident "no reply needed" with a category (a closing, or an
            informational message), unless the user usually answers such messages in this
            chat (habit rule, R3)

Every failure or doubt drafts: a missed reply is worse than an extra draft (FR-004).

:func:`decide` is the database-free core (unit tests, eval runner). :class:`ReplyGate` loads the
turn and its context, runs :func:`decide`, logs one ``reply_gate_decided`` line (never text) and
stores the decision. The gate never sends and never marks anything read: the scheduler marks
the turn read before asking the gate (FR-007), and a skip only changes the turn's state.

:meth:`ReplyGate.decide_for_pending` (FR-011) judges one new message that arrived while a draft
for the chat is pending review; intake (``intake/router.py``) uses it to keep a closing message
from marking that draft outdated, and to flag the draft ``possibly_unnecessary`` when the message
withdraws the request. It never stores anything itself; the habit rule does not apply there.

At the user's daily LLM hard cap (issue #74, ``drafting/budget.py``) the pending-draft check
drafts without asking the classifier (the draft becomes outdated) and a dropped turn is not
classified; due turns never reach the gate then (the scheduler skips them as ``budget``).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.context.history import USER_SENDER, forward_fields, message_ref, turn_messages
from scribe.db import models
from scribe.db.models import (
    ActivityKind,
    ChatKind,
    ReplyDecision,
    TurnState,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import BudgetCaps, budget_level
from scribe.llm.provider import (
    ContactInfo,
    HistoryItem,
    MessageItem,
    ProviderError,
    ReplyNeededOutput,
    ReplyNeededRequest,
)
from scribe.llm.registry import LLMRouter, RoutedResult
from scribe.logging import get_logger
from scribe.style.profiles import effective_user_rules, select_profile

log = get_logger("scribe.reply_gate")

SkipCategory = Literal[
    "thanks", "farewell", "acknowledgment", "informational", "user_rule", "forwarded", "budget"
]
DecisionSource = Literal["pregate", "model", "fallback", "habit", "disabled", "user", "rule"]

QUESTION_MARKS = ("?", "？")
DEFAULT_HABIT_MIN_REPLIES = 3


@dataclass(frozen=True)
class GateDecision:
    decision: Literal["draft", "skip"]
    source: DecisionSource
    category: SkipCategory | None  # set whenever the classifier recognised a closing
    confidence: float | None
    resolves_pending: bool = False
    routed: RoutedResult[ReplyNeededOutput] | None = None  # usage/cost for logging
    # The gate itself failed (not the classifier): the caller must not draft a turn that may
    # hold forwarded messages only (issue #47) without checking it.
    failed: bool = False
    # ``turns.last_message_id`` the decision saw: a skip is stored only while it is unchanged,
    # so a message that joined the turn meanwhile is never skipped with it.
    last_message_id: uuid.UUID | None = None
    # 013: whether the classifier settled it by a user rule or by the built-in rules, and
    # whether the user had rules at all when it was asked (logged and stored in the activity
    # record; never the rules' text).
    decided_by: Literal["default", "user_rule"] = "default"
    has_rules: bool = False


FALLBACK = GateDecision("draft", "fallback", None, None)
FAILED = GateDecision("draft", "fallback", None, None, failed=True)
FORWARDS_ONLY = GateDecision("skip", "pregate", "forwarded", None)
# Issue #74: the user's daily LLM hard cap is reached; stored by the scheduler before the gate
# runs (no model call), listed in /skipped like the other skips.
BUDGET_SKIP = GateDecision("skip", "pregate", "budget", None)


def is_forwards_only(messages: Sequence[models.Message | MessageItem]) -> bool:
    """The turn holds messages and every one of them is a forwarded copy (issue #47); stored
    messages or request items (``forwarded``)."""
    return bool(messages) and all(
        m.is_forwarded if isinstance(m, models.Message) else m.forwarded for m in messages
    )


def forwards_only_skip(
    turn: models.ConversationTurn, messages: Sequence[models.Message]
) -> GateDecision | None:
    """Issue #47, the one rule for a turn of forwarded messages only: skip it as ``forwarded``
    (no model call, whatever the skip setting) unless the user asked for a draft anyway. The
    decision carries the turn's ``last_message_id``, so the skip is not stored if a message
    joined the turn meanwhile. None: the rule does not apply."""
    if turn.draft_anyway_at is not None or not is_forwards_only(messages):
        return None
    return replace(FORWARDS_ONLY, last_message_id=turn.last_message_id)


def own_messages(messages: Sequence[models.Message]) -> list[models.Message]:
    """The messages the sender wrote themselves: forwarded copies are not their words (#47)."""
    return [m for m in messages if not m.is_forwarded]


# --- database-free core -------------------------------------------------------------------------


def is_skip_candidate(texts: Sequence[str], *, has_rules: bool, max_chars: int) -> bool:
    """Pure pre-gate: not empty, ≤ ``max_chars`` chars; a '?' / '？' disqualifies only when the
    user has no rules (``has_rules`` False): with rules, a question may be one a rule says to
    skip or to answer. Media does not disqualify (a captioned document is informational).

    It can only widen drafting: a turn that is not a candidate is drafted without asking the
    classifier. An empty turn is not a candidate either (the composer handles it)."""
    joined = " ".join(t.strip() for t in texts if t.strip())
    if not joined or len(joined) > max_chars:
        return False
    return has_rules or not any(mark in joined for mark in QUESTION_MARKS)


async def no_habit() -> int:
    """``habit_replied`` for callers where the habit rule does not apply (evals, pending check)."""
    return 0


async def decide(
    router: LLMRouter,
    req: ReplyNeededRequest,
    *,
    enabled: bool,
    draft_anyway: bool,
    habit_replied: Callable[[], Awaitable[int]],
    min_confidence: float,
    max_chars: int,
    has_media: bool = False,
    habit_min_replies: int = DEFAULT_HABIT_MIN_REPLIES,
    user_id: uuid.UUID | None = None,
) -> GateDecision:
    """Order: draft_anyway -> ("draft","user"); not enabled -> ("draft","disabled");
    not a candidate -> ("draft","pregate"); classifier error -> ("draft","fallback");
    ``decided_by`` user_rule (honoured only when ``req.user_rules`` holds rules) and
    reply_needed -> ("draft","rule"); user_rule, not reply_needed, confidence >= min ->
    ("skip","rule","user_rule"); user_rule with lower confidence -> ("draft","fallback");
    reply_needed -> ("draft","model"); no category or confidence < min -> ("draft","fallback");
    habit_replied() >= habit_min_replies -> ("draft","habit"); else ("skip","model") with the
    classifier's category (a closing or ``informational``).

    The pre-gate looks at the contact's own messages only: forwarded items (``forwarded:
    true``) are left out (issue #47). A turn of forwarded messages only never gets here:
    :func:`forwards_only_skip` decides it first. ``has_media`` is not used by the pre-gate
    any more (the classifier sees each item's own ``has_media``); it is kept for callers.

    ``habit_replied`` is awaited only when the classifier would skip by the built-in rules; a
    rule skip never consults it. Never touches the database or the gateway; raises only what is
    not a ``ProviderError`` (the caller treats that as ``fallback``).
    """
    if draft_anyway:
        return GateDecision("draft", "user", None, None)
    if not enabled:
        return GateDecision("draft", "disabled", None, None)
    # #47: forwarded text is not the contact's own words; it never makes a turn a closing.
    texts = [m.text for m in req.incoming_messages if not m.forwarded]
    has_rules = bool(req.user_rules.get("rules"))
    if not is_skip_candidate(texts, has_rules=has_rules, max_chars=max_chars):
        return GateDecision("draft", "pregate", None, None, has_rules=has_rules)
    try:
        routed = await router.classify_reply_needed(req, user_id=user_id)
    except ProviderError:
        return replace(FALLBACK, has_rules=has_rules)
    output = routed.output
    category: SkipCategory | None = None if output.category == "none" else output.category
    # A model that names a rule when there are none is not believed.
    decided_by = output.decided_by if has_rules else "default"

    def decision(
        kind: Literal["draft", "skip"],
        source: DecisionSource,
        skip_category: SkipCategory | None = category,
    ) -> GateDecision:
        return GateDecision(
            kind,
            source,
            skip_category,
            output.confidence,
            output.resolves_pending,
            routed,
            decided_by=decided_by,
            has_rules=has_rules,
        )

    if decided_by == "user_rule":
        if output.reply_needed:
            return decision("draft", "rule")
        if output.confidence < min_confidence:
            return decision("draft", "fallback")
        return decision("skip", "rule", "user_rule")  # never consults the habit rule
    if output.reply_needed:
        return decision("draft", "model")
    if category is None or output.confidence < min_confidence:
        return decision("draft", "fallback")  # a skip needs a confident category
    if await habit_replied() >= habit_min_replies:
        return decision("draft", "habit")
    return decision("skip", "model")


# --- service ------------------------------------------------------------------------------------


class ReplyGate:
    """Runs the gate for a claimed turn and stores the outcome (contracts/reply-gate.md)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm_router: LLMRouter,
        settings: Settings,
        *,
        clock: Clock = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._router = llm_router
        self._settings = settings
        self._clock = clock
        self._caps = BudgetCaps.from_settings(settings)

    async def decide_for_turn(self, user_id: uuid.UUID, turn_id: uuid.UUID) -> GateDecision:
        """Decide for a turn in ``reading``. Any failure that is not a ``ProviderError`` is
        logged and becomes ``fallback`` (draft) marked ``failed``: the scheduler drafts it only
        after checking it is not forwards-only (issue #47); this never raises.

        The effective skip setting is read fresh from the database on every call, so a change
        applies to the next message without a restart (FR-014)."""
        try:
            decision = await self._decide(user_id, turn_id)
        except Exception as exc:
            log.warning(
                "reply_gate_failed",
                user_id=str(user_id),
                turn_id=str(turn_id),
                error=type(exc).__name__,
            )
            decision = FAILED
        self._log(user_id, turn_id, decision)
        return decision

    async def decide_for_pending(self, user_id: uuid.UUID, message_id: uuid.UUID) -> GateDecision:
        """Decide for one incoming message that arrived while a draft is pending (FR-011).

        The request holds only this message (``pending_draft=True``) and the chat history before
        it; the habit rule does not apply. Any failure is logged and drafts; this never raises.
        """
        try:
            decision = await self._decide_pending(user_id, message_id)
        except Exception as exc:
            log.warning(
                "reply_gate_failed",
                user_id=str(user_id),
                message_id=str(message_id),
                pending=True,
                error=type(exc).__name__,
            )
            decision = FALLBACK
        self._log(user_id, None, decision, message_id=message_id)
        return decision

    async def classify_dropped(self, user_id: uuid.UUID, turn_id: uuid.UUID) -> None:
        """Habit rule input (research R3) for a turn a manual reply dropped before the gate saw
        it: when skipping is on for the chat and the turn passes the pre-gate, ask the
        classifier and store a recognised closing category on the turn (guarded: still
        ``dropped`` by ``manual_reply`` with no category). Changes nothing else; never raises."""
        try:
            decision = await self._decide_dropped(user_id, turn_id)
            if decision is None or decision.category is None or decision.source == "rule":
                return  # a rule's decision never feeds the habit rule (013)
            async with self._session_factory() as session:
                stored = await turns_repo.set_dropped_category(
                    session, user_id, turn_id, models.SkipCategory(decision.category)
                )
                await session.commit()
            log.info(
                "reply_gate_dropped_classified",
                user_id=str(user_id),
                turn_id=str(turn_id),
                category=decision.category,
                stored=stored,
            )
        except Exception as exc:
            log.warning(
                "reply_gate_failed",
                user_id=str(user_id),
                turn_id=str(turn_id),
                dropped=True,
                error=type(exc).__name__,
            )

    async def record_skip(
        self, user_id: uuid.UUID, turn_id: uuid.UUID, decision: GateDecision
    ) -> bool:
        """``reading -> skipped`` with the decision columns, plus the activity record
        ``skipped`` (no text), in one transaction. False if the turn left ``reading`` or a
        message joined it after the decision (``decision.last_message_id`` moved)."""
        return await record_skip(
            self._session_factory, user_id, turn_id, decision, now=self._clock()
        )

    async def record_draft_decision(
        self, user_id: uuid.UUID, turn_id: uuid.UUID, decision: GateDecision
    ) -> None:
        """Store a ``draft`` decision; the turn stays ``reading`` for the composer. A turn that
        was skipped and then drafted on request keeps its ``skip_category`` (COALESCE)."""
        async with self._session_factory() as session:
            await turns_repo.set_decision(
                session, user_id, turn_id, **_columns(decision), decided_at=self._clock()
            )
            await session.commit()

    # --- internals ------------------------------------------------------------------------------

    async def _decide(self, user_id: uuid.UUID, turn_id: uuid.UUID) -> GateDecision:
        settings = self._settings
        async with self._session_factory() as session:
            turn = await turns_repo.get_turn(session, user_id, turn_id)
            if turn is None:
                raise LookupError("turn not found")
            seen = turn.last_message_id
            in_turn = await turn_messages(session, turn)
            # #47: decided before any other lookup, so a later failure cannot draft it.
            skip = forwards_only_skip(turn, in_turn)
            if skip is not None:
                return skip
            user = await users_repo.get_user(session, user_id)
            if user is None:
                raise LookupError("user not found")
            chat = await chats_repo.get_work_chat(session, user_id, turn.work_chat_id)
            contact = await chats_repo.get_contact(session, user_id, turn.contact_id)
            if chat is None or contact is None:
                raise LookupError("chat or contact not found")
            enabled = chat.skip_closing if chat.skip_closing is not None else user.skip_closing
            # Rules are read only when the classifier can be asked: not after "draft anyway"
            # or with skipping off for the chat (those paths stay as they were, FR-008a).
            rules = (
                await _user_rules(session, user_id, contact.id, lang=user.language)
                if enabled and turn.draft_anyway_at is None
                else {}
            )
            names = _SenderNames(session, user_id)
            incoming = [
                MessageItem(
                    id=message_ref(m.id),
                    sender=await names.of(m.sender_contact_id),
                    text=m.text,
                    has_media=m.has_media,
                    **forward_fields(m),
                )
                for m in in_turn
            ]
            history = (
                await self._history(
                    session,
                    user_id,
                    turn.work_chat_id,
                    before=_history_before(turn, in_turn),
                    names=names,
                    exclude={m.id for m in in_turn},
                )
                if in_turn
                else []
            )
        request = ReplyNeededRequest(
            user_rules=rules,
            incoming_messages=incoming,
            history=history,
            contact=ContactInfo(display_name=contact.display_name, kind=contact.kind.value),
            pending_draft=False,
        )

        async def habit_replied() -> int:
            async with self._session_factory() as session:
                return await turns_repo.habit_replied_count(
                    session,
                    user_id,
                    turn.work_chat_id,
                    before=turn.arrived_at,
                    window=settings.skip_habit_window,
                    reply_window=timedelta(minutes=settings.skip_habit_reply_window_min),
                    retention_days=settings.message_retention_days,
                )

        decision = await decide(
            self._router,
            request,
            enabled=enabled,
            draft_anyway=turn.draft_anyway_at is not None,
            habit_replied=habit_replied,
            min_confidence=settings.skip_min_confidence,
            max_chars=settings.skip_candidate_max_chars,
            has_media=any(m.has_media for m in own_messages(in_turn)),
            habit_min_replies=settings.skip_habit_min_replies,
            user_id=user_id,
        )
        return replace(decision, last_message_id=seen)

    async def _decide_dropped(self, user_id: uuid.UUID, turn_id: uuid.UUID) -> GateDecision | None:
        """``None`` when there is nothing to classify (not a candidate, skipping off, gone)."""
        settings = self._settings
        async with self._session_factory() as session:
            turn = await turns_repo.get_turn(session, user_id, turn_id)
            user = await users_repo.get_user(session, user_id)
            if turn is None or user is None or turn.skip_category is not None:
                return None
            if await budget_level(session, user_id, self._caps, self._clock()) == "hard":
                return None  # issue #74: no model call at the daily hard cap
            chat = await chats_repo.get_work_chat(session, user_id, turn.work_chat_id)
            contact = await chats_repo.get_contact(session, user_id, turn.contact_id)
            if chat is None or contact is None:
                return None
            enabled = chat.skip_closing if chat.skip_closing is not None else user.skip_closing
            in_turn = await turn_messages(session, turn)
            has_media = any(m.has_media for m in own_messages(in_turn))
            texts = [m.text for m in own_messages(in_turn)]
            if is_forwards_only(in_turn):
                return None  # not a closing: the habit rule does not count it (#47)
            if not enabled:
                return None
            rules = await _user_rules(session, user_id, contact.id, lang=user.language)
            if not is_skip_candidate(
                texts, has_rules=bool(rules), max_chars=settings.skip_candidate_max_chars
            ):
                return None  # no classifier call: the habit rule only counts closings
            names = _SenderNames(session, user_id)
            incoming = [
                MessageItem(
                    id=message_ref(m.id),
                    sender=await names.of(m.sender_contact_id),
                    text=m.text,
                    has_media=m.has_media,
                    **forward_fields(m),
                )
                for m in in_turn
            ]
            history = await self._history(
                session, user_id, turn.work_chat_id, before=in_turn[0].sent_at, names=names
            )
        request = ReplyNeededRequest(
            user_rules=rules,
            incoming_messages=incoming,
            history=history,
            contact=ContactInfo(display_name=contact.display_name, kind=contact.kind.value),
            pending_draft=False,
        )
        return await decide(
            self._router,
            request,
            enabled=True,
            draft_anyway=False,
            habit_replied=no_habit,
            min_confidence=settings.skip_min_confidence,
            max_chars=settings.skip_candidate_max_chars,
            has_media=has_media,
            habit_min_replies=settings.skip_habit_min_replies,
            user_id=user_id,
        )

    async def _decide_pending(self, user_id: uuid.UUID, message_id: uuid.UUID) -> GateDecision:
        settings = self._settings
        async with self._session_factory() as session:
            message = await messages_repo.get_message(session, user_id, message_id)
            user = await users_repo.get_user(session, user_id)
            if message is None or user is None or message.is_outgoing:
                raise LookupError("message not found")
            if await budget_level(session, user_id, self._caps, self._clock()) == "hard":
                return FALLBACK  # issue #74: no model call at the daily hard cap (outdated)
            chat = await chats_repo.get_work_chat(session, user_id, message.work_chat_id)
            if chat is None:
                raise LookupError("chat not found")
            # The turn's contact: the person in a private chat, the group as a whole otherwise.
            contact = None
            if chat.kind != ChatKind.private:
                contact = await chats_repo.get_contact_by_peer(
                    session, user_id, chat.telegram_chat_id
                )
            if contact is None and message.sender_contact_id is not None:
                contact = await chats_repo.get_contact(session, user_id, message.sender_contact_id)
            if contact is None:
                raise LookupError("contact not found")
            enabled = chat.skip_closing if chat.skip_closing is not None else user.skip_closing
            rules = (
                await _user_rules(session, user_id, contact.id, lang=user.language)
                if enabled
                else {}
            )
            names = _SenderNames(session, user_id)
            incoming = [
                MessageItem(
                    id=message_ref(message.id),
                    sender=await names.of(message.sender_contact_id),
                    text=message.text,
                    has_media=message.has_media,
                    **forward_fields(message),
                )
            ]
            history = await self._history(
                session, user_id, message.work_chat_id, before=message.sent_at, names=names
            )
        request = ReplyNeededRequest(
            user_rules=rules,
            incoming_messages=incoming,
            history=history,
            contact=ContactInfo(display_name=contact.display_name, kind=contact.kind.value),
            pending_draft=True,
        )
        return await decide(
            self._router,
            request,
            enabled=enabled,
            draft_anyway=False,
            habit_replied=no_habit,
            min_confidence=settings.skip_min_confidence,
            max_chars=settings.skip_candidate_max_chars,
            has_media=message.has_media and not message.is_forwarded,
            habit_min_replies=settings.skip_habit_min_replies,
            user_id=user_id,
        )

    async def _history(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        work_chat_id: uuid.UUID,
        *,
        before: datetime,
        names: _SenderNames,
        exclude: frozenset[uuid.UUID] | set[uuid.UUID] = frozenset(),
    ) -> list[HistoryItem]:
        """The last ``skip_context_messages`` chat messages sent before ``before`` and not in
        ``exclude`` (the turn's own messages), oldest first."""
        limit = self._settings.skip_context_messages
        if limit == 0:
            return []
        since = self._clock() - timedelta(days=self._settings.message_retention_days)
        recent = await messages_repo.list_recent_in_chat(
            session, user_id, work_chat_id, since=since, limit=limit + len(exclude), before=before
        )
        recent = [m for m in recent if m.id not in exclude][:limit]
        return [
            HistoryItem(
                id=message_ref(m.id),
                is_user=m.is_outgoing,
                sender=await names.of(None if m.is_outgoing else m.sender_contact_id),
                text=m.text,
                sent_at=m.sent_at,
                **forward_fields(m),
            )
            for m in reversed(recent)
            if m.text.strip()
        ]

    def _log(
        self,
        user_id: uuid.UUID,
        turn_id: uuid.UUID | None,
        decision: GateDecision,
        *,
        message_id: uuid.UUID | None = None,
    ) -> None:
        routed = decision.routed
        log.info(
            "reply_gate_decided",
            user_id=str(user_id),
            turn_id=str(turn_id) if turn_id is not None else None,
            message_id=str(message_id) if message_id is not None else None,
            pending=message_id is not None,
            decision=decision.decision,
            category=decision.category,
            source=decision.source,
            confidence=decision.confidence,
            decided_by=decision.decided_by,
            has_rules=decision.has_rules,
            route=routed.route if routed is not None else None,
            provider=routed.provider if routed is not None else None,
            model=routed.model if routed is not None else None,
            prompt_version=routed.prompt_version if routed is not None else None,
            latency_ms=routed.usage.latency_ms if routed is not None else None,
            cost_usd=routed.cost_usd if routed is not None else None,
        )


def _history_before(turn: models.ConversationTurn, in_turn: list[models.Message]) -> datetime:
    """History ends where the turn starts; for a group turn pulled back to the sender's
    forward it ends at the message that opened the turn, so the sender's messages in between
    (to other people, not part of the turn) are still context for the gate (issue #47)."""
    if turn.opening_message_id is not None:
        for message in in_turn:
            if message.id == turn.opening_message_id:
                return message.sent_at
    return in_turn[0].sent_at


async def record_skip(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    turn_id: uuid.UUID,
    decision: GateDecision,
    *,
    now: datetime,
) -> bool:
    """``reading -> skipped`` with the decision columns, plus the activity record ``skipped``
    (no text), in one transaction. False if the turn left ``reading`` or, when the decision
    knows it, its ``last_message_id`` moved (a message joined after the decision)."""
    async with session_factory() as session:
        changed = await turns_repo.set_decision(
            session,
            user_id,
            turn_id,
            **_columns(decision),
            decided_at=now,
            state=TurnState.skipped,
            last_message_id=decision.last_message_id,
        )
        if changed:
            await activity_repo.record(
                session,
                user_id,
                ActivityKind.skipped,
                at=now,
                conversation_turn_id=turn_id,
                meta={
                    "category": decision.category,
                    "source": decision.source,
                    "confidence": decision.confidence,
                    "decided_by": decision.decided_by,
                    "has_rules": decision.has_rules,
                },
            )
        await session.commit()
    return changed


async def _user_rules(
    session: AsyncSession, user_id: uuid.UUID, contact_id: uuid.UUID, *, lang: str
) -> dict[str, Any]:
    """The user's effective rules for this contact, as the drafting call gets them (the same
    resolver as the composer), or ``{}`` when there are none."""
    profile = await select_profile(session, user_id, contact_id)
    applied = await effective_user_rules(session, user_id, profile, lang=lang)
    return applied.user_rules_json() if applied.own or applied.inherited else {}


class _SenderNames:
    """Contact display names, cached per decision (``me`` for the user's own messages)."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id
        self._names: dict[uuid.UUID, str] = {}

    async def of(self, contact_id: uuid.UUID | None) -> str:
        if contact_id is None:
            return USER_SENDER
        if contact_id not in self._names:
            contact = await chats_repo.get_contact(self._session, self._user_id, contact_id)
            self._names[contact_id] = contact.display_name if contact is not None else ""
        return self._names[contact_id]


class _DecisionColumns(TypedDict):
    decision: ReplyDecision
    source: models.DecisionSource
    category: models.SkipCategory | None
    confidence: float | None


def _columns(decision: GateDecision) -> _DecisionColumns:
    return {
        "decision": ReplyDecision(decision.decision),
        "source": models.DecisionSource(decision.source),
        "category": models.SkipCategory(decision.category) if decision.category else None,
        "confidence": decision.confidence,
    }


__all__ = [
    "BUDGET_SKIP",
    "FAILED",
    "FORWARDS_ONLY",
    "GateDecision",
    "forwards_only_skip",
    "ReplyGate",
    "decide",
    "is_forwards_only",
    "is_skip_candidate",
    "no_habit",
    "own_messages",
    "record_skip",
]
