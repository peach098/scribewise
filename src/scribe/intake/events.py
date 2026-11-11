"""Gateway events besides new incoming messages (US2, FR-015, research R3).

``on_outgoing`` — a message sent from the user's account in an enabled work chat (with work
account mode on, specs/008, a first message to an eligible private chat without a row first
picks the chat up as an ``auto`` work chat, ``chats.work_account.pick_up``; the message is then
stored like any manual message, so the contact's answer is drafted with it as context):

- If it is the agent's own approved send echoed back by Telethon
  (``drafts.sent_telegram_message_id`` matches), nothing is dropped. ``send.py`` keeps the draft
  row locked while it calls Telegram, so the echo can arrive *before* that id is committed: the
  handler first takes ``FOR UPDATE`` on the chat's pending drafts, which waits for an in-flight
  send to commit, and only then compares.
- Otherwise the user replied manually: every ``waiting``/``reading``/``pending_review`` turn in
  the chat whose first message is *older* than the reply (lower Telegram message id; a turn
  whose first message was purged counts as older; for a group turn pulled back to a forward,
  the message that opened it counts, issue #47) is ``dropped`` (``manual_reply``), its
  pending draft ``dropped``, the user is told "✅ You replied to <Contact> yourself — dropped
  the waiting message/draft." and the card is closed. A late or redelivered outgoing update
  never drops a newer turn. The outgoing message is stored either way (idempotent insert), so
  later drafts see it, and intake drops a turn created later for a message older than it.
- A message the user forwarded into the chat (issue #47) counts as a manual reply too: it drops
  the open turns and pending drafts above. It is stored marked forwarded, so it is never a
  style sample and never goes to commitment extraction (it is not the user's own words).

004 (habit rule, research R3): a turn dropped by a manual reply before the reply gate saw it
(still ``waiting`` or being read) has no ``skip_category``, so the habit count could never see
that the user answers closing messages in this chat. After the transaction, each such turn
that passes the pure pre-gate is classified by ``ReplyGate.classify_dropped`` (skipping enabled
for the chat only); a recognised closing category is stored on the dropped turn, nothing else.

US3 (FR-031): a new manual message (not an agent send, not a redelivered update, not a
forward) goes to commitment extraction (``context/commitments.py``) after the transaction and
outside the chat lock; its LLM call runs in the background. Agent sends are extracted by
``send.py``, so the echo is skipped here and every outgoing message is extracted exactly once.

Incoming and outgoing messages of one chat are serialised on the shared per-chat lock
(``intake/locks.py``), so they are never handled interleaved.

``on_read_elsewhere`` — the user read the chat on another device: waiting turns whose first
message (the opening one for a group turn pulled back to a forward, issue #47) is at or below
the read mark become due now, regardless of working hours (the spec
edge case: once the user has read it, holding it serves no purpose). Each such turn gets a
``read_now`` activity record (``meta.source = "read_elsewhere"``) so re-planning keeps it due.

specs/009 (mark chats unread): with an ``UnreadMarker``, both events are also "seen" signals
from the user's own session. A read elsewhere in an enabled work chat raises the chat's
``unread_mark_floor`` to ``max_message_id`` after the commit; a manual outgoing message (not an
agent send) raises it to that message's id after the chat lock is released. The agent then
never marks the chat unread for messages the user has already seen.

Lock order is draft, then turn, as everywhere else (``send.py``, composer, intake): the chat's
pending drafts are locked first, then its turns; if a draft appeared in between (composed
meanwhile), the transaction is rolled back and retried rather than locking it after a turn.
Messages in chats that are not enabled work chats are never stored (FR-001). Message text is
never logged.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import NamedTuple, Protocol

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.clock import Clock, utcnow
from scribe.context.commitments import CommitmentExtractor, default_extractor
from scribe.db.models import (
    ActivityKind,
    ConversationTurn,
    Draft,
    DraftState,
    DroppedReason,
    Message,
    TurnState,
    WorkChat,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import messages as messages_repo
from scribe.drafting.budget import LimitsResolver
from scribe.i18n import Msg
from scribe.intake.locks import CHAT_LOCKS, ChatLocks
from scribe.logging import get_logger
from scribe.telegram.gateway import OutgoingEvent, ReadEvent
from scribe.telegram.unread_mark import UnreadMarker

log = get_logger("scribe.intake")

# Where a turn starts for "the user already saw / answered it" cut-offs: the message that
# opened it, which is not ``first_message_id`` when a group turn was pulled back to the
# sender's preceding forward (``opening_message_id``, issue #47).
_turn_start = func.coalesce(ConversationTurn.opening_message_id, ConversationTurn.first_message_id)

# (user_id, message): rendered by the notifier in the user's stored language.
Notifier = Callable[[uuid.UUID, Msg], Awaitable[None]]

MANUAL_REPLY_STATUS = Msg("status.manual_reply")
_THIS_CHAT = Msg("notice.this_chat")
OPEN_TURN_STATES = (TurnState.waiting, TurnState.reading, TurnState.pending_review)
_DROP_ATTEMPTS = 5


class _Outcome(NamedTuple):
    agent_send: bool
    dropped: list[tuple[str | Msg, uuid.UUID | None]]
    new_manual_chat_id: uuid.UUID | None  # set for a new manual message: extract commitments
    ungated: list[uuid.UUID]  # dropped turns the reply gate never decided (004 habit rule)


class DroppedTurnGate(Protocol):
    """``scribe.drafting.reply_gate.ReplyGate`` (004 habit rule)."""

    async def classify_dropped(self, user_id: uuid.UUID, turn_id: uuid.UUID) -> None: ...


class _DraftAppeared(Exception):
    """A pending draft was created between locking the drafts and locking the turns."""


class CardUpdater(Protocol):
    async def show_status(self, user_id: uuid.UUID, draft_id: uuid.UUID, status: Msg) -> None: ...


class ChatEventHandler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = utcnow,
        notifier: Notifier | None = None,
        cards: CardUpdater | None = None,
        locks: ChatLocks | None = None,
        commitments: CommitmentExtractor | None = None,
        gate: DroppedTurnGate | None = None,
        marker: UnreadMarker | None = None,
        limits: LimitsResolver | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits  # a plugin's work chat limit for pick-up (docs/plugins.md)
        self._gate = gate
        self._marker = marker
        self._clock = clock
        self._notifier = notifier
        self._cards = cards
        self._locks = locks if locks is not None else CHAT_LOCKS
        self._commitments = commitments  # None: the process default (installed by Composer)

    # --- outgoing -------------------------------------------------------------------------------

    async def handle_outgoing(self, event: OutgoingEvent) -> None:
        user_id = event.user_id
        async with self._locks.for_chat(user_id, event.chat_id):
            for attempt in range(1, _DROP_ATTEMPTS + 1):
                try:
                    outcome = await self._outgoing_once(event)
                except _DraftAppeared:
                    log.info("outgoing_retry_lock_order", user_id=str(user_id), attempt=attempt)
                    continue
                break
            else:
                log.error("outgoing_not_handled", user_id=str(user_id))
                return
        if outcome is None:
            return
        if outcome.agent_send:
            log.info("outgoing_agent_echo", user_id=str(user_id))
            return
        log.info("outgoing_manual_reply", user_id=str(user_id), dropped=len(outcome.dropped))
        if self._marker is not None:  # specs/009: the user has seen the chat up to here
            await self._marker.seen(user_id, event.chat_id, event.message_id, source="manual_reply")
        await self._after_drop(user_id, outcome.dropped)
        if outcome.new_manual_chat_id is not None:
            await self._extract_commitments(event, outcome.new_manual_chat_id)
        await self._classify_dropped(user_id, outcome.ungated)

    async def _classify_dropped(self, user_id: uuid.UUID, turn_ids: list[uuid.UUID]) -> None:
        if self._gate is None:
            return
        for turn_id in turn_ids:
            try:
                await self._gate.classify_dropped(user_id, turn_id)
            except Exception as exc:  # never affects intake
                log.warning(
                    "dropped_turn_classify_failed", turn_id=str(turn_id), error=type(exc).__name__
                )

    async def _extract_commitments(self, event: OutgoingEvent, work_chat_id: uuid.UUID) -> None:
        extractor = self._commitments or default_extractor()
        if extractor is None:
            return
        try:
            await extractor.submit(
                event.user_id, work_chat_id=work_chat_id, text=event.text, sent_at=event.sent_at
            )
        except Exception as exc:  # never affects intake
            log.warning(
                "commitment_extraction_failed", user_id=str(event.user_id), error=type(exc).__name__
            )

    async def _outgoing_once(self, event: OutgoingEvent) -> _Outcome | None:
        """One transaction: (agent send?, dropped turns), or None for a non-work chat."""
        user_id = event.user_id
        async with self._session_factory() as session:
            chat = await chats_repo.get_work_chat_by_telegram_id(session, user_id, event.chat_id)
            if chat is None:
                chat = await work_account.pick_up(
                    session,
                    user_id,
                    telegram_chat_id=event.chat_id,
                    title=event.chat_title,
                    chat_kind=event.chat_kind,
                    peer_kind=event.peer_kind,
                    direction="out",
                    now=self._clock(),
                    limits=self._limits,
                )
            if chat is None or not chat.enabled:
                return None  # FR-001: not a work chat, nothing stored
            # Draft rows first (lock order draft -> turn). This also waits for an in-flight
            # approved send in this chat, which holds its draft row lock.
            locked = await session.execute(
                select(Draft)
                .where(
                    Draft.user_id == user_id,
                    Draft.state == DraftState.pending,
                    Draft.conversation_turn_id.in_(_chat_turn_ids(user_id, chat)),
                )
                .order_by(Draft.id)
                .with_for_update()
            )
            drafts = {draft.conversation_turn_id: draft for draft in locked.scalars().all()}
            agent_send = await _is_agent_send(session, user_id, chat, event.message_id)
            already_stored = await messages_repo.get_by_telegram_id(
                session, user_id, chat.id, event.message_id
            )
            await messages_repo.add_message_if_absent(
                session,
                user_id,
                work_chat_id=chat.id,
                telegram_message_id=event.message_id,
                text=event.text,
                sent_at=event.sent_at,
                is_outgoing=True,
                has_media=event.has_media,
                reply_to_telegram_message_id=event.reply_to_id,
                is_forwarded=event.is_forwarded,
                forwarded_from=event.forwarded_from,
            )
            dropped: list[tuple[str | Msg, uuid.UUID | None]] = []
            ungated: list[uuid.UUID] = []
            if not agent_send:
                try:
                    dropped = await self._drop_open_turns(
                        session, chat, event.message_id, drafts, ungated
                    )
                except _DraftAppeared:
                    await session.rollback()
                    raise
            await session.commit()
        new_manual = (
            not agent_send
            and already_stored is None
            and not event.is_forwarded  # #47: a forward is not the user's own promise
            and bool(event.text.strip())
        )
        return _Outcome(agent_send, dropped, chat.id if new_manual else None, ungated)

    async def _drop_open_turns(
        self,
        session: AsyncSession,
        chat: WorkChat,
        replied_at_message_id: int,
        drafts: dict[uuid.UUID, Draft],
        ungated: list[uuid.UUID],
    ) -> list[tuple[str | Msg, uuid.UUID | None]]:
        """Drop the chat's open turns older than the manual reply; ``drafts`` are the chat's
        pending drafts, already locked. Returns (contact name, dropped draft id) per turn and
        adds the dropped turns the reply gate never decided to ``ungated``."""
        now = self._clock()
        turns = (
            (
                await session.execute(
                    select(ConversationTurn)
                    # A group turn pulled back to a forward starts, for this cut-off, at the
                    # message that opened it (issue #47).
                    .outerjoin(Message, Message.id == _turn_start)
                    .where(
                        ConversationTurn.user_id == chat.user_id,
                        ConversationTurn.work_chat_id == chat.id,
                        ConversationTurn.state.in_(OPEN_TURN_STATES),
                        or_(
                            Message.id.is_(None),
                            Message.telegram_message_id < replied_at_message_id,
                        ),
                    )
                    .order_by(ConversationTurn.id)
                    .with_for_update(of=ConversationTurn)
                )
            )
            .scalars()
            .all()
        )
        if turns:
            # A draft composed after the drafts were locked would now be locked after its turn
            # (turn -> draft): never do that, start over instead.
            pending = await session.execute(
                select(Draft.id, Draft.conversation_turn_id).where(
                    Draft.user_id == chat.user_id,
                    Draft.state == DraftState.pending,
                    Draft.conversation_turn_id.in_([turn.id for turn in turns]),
                )
            )
            for draft_id, turn_id in pending.all():
                if turn_id not in drafts or drafts[turn_id].id != draft_id:
                    raise _DraftAppeared
        dropped: list[tuple[str | Msg, uuid.UUID | None]] = []
        for turn in turns:
            draft = drafts.get(turn.id)
            if draft is not None and draft.state != DraftState.pending:
                draft = None
            if draft is not None:
                draft.state = DraftState.dropped
                draft.finalized_at = now
            turn.state = TurnState.dropped
            turn.dropped_reason = DroppedReason.manual_reply
            if turn.reply_decision is None and turn.skip_category is None:
                ungated.append(turn.id)
            await activity_repo.record(
                session,
                chat.user_id,
                ActivityKind.dropped,
                at=now,
                draft_id=draft.id if draft is not None else None,
                conversation_turn_id=turn.id,
                meta={"reason": DroppedReason.manual_reply.value},
            )
            contact = await chats_repo.get_contact(session, chat.user_id, turn.contact_id)
            name = contact.display_name if contact is not None and contact.display_name else ""
            dropped.append((name or chat.title or _THIS_CHAT, draft.id if draft else None))
        await session.flush()
        return dropped

    async def _after_drop(
        self, user_id: uuid.UUID, dropped: list[tuple[str | Msg, uuid.UUID | None]]
    ) -> None:
        for name in dict.fromkeys(name for name, _ in dropped):
            if self._notifier is None:
                break
            try:
                await self._notifier(user_id, Msg("notice.manual_reply", name=name))
            except Exception:
                log.exception("control_bot_notify_failed", user_id=str(user_id))
        for _, draft_id in dropped:
            if draft_id is None or self._cards is None:
                continue
            try:
                await self._cards.show_status(user_id, draft_id, MANUAL_REPLY_STATUS)
            except Exception as exc:
                log.warning(
                    "draft_card_edit_failed", draft_id=str(draft_id), error=type(exc).__name__
                )

    # --- read on another device -----------------------------------------------------------------

    async def handle_read_elsewhere(self, event: ReadEvent) -> None:
        user_id = event.user_id
        now = self._clock()
        async with self._session_factory() as session:
            chat = await chats_repo.get_work_chat_by_telegram_id(session, user_id, event.chat_id)
            if chat is None or not chat.enabled:
                return
            read_messages = select(Message.id).where(
                Message.user_id == user_id,
                Message.work_chat_id == chat.id,
                Message.telegram_message_id <= event.max_message_id,
            )
            result = await session.execute(
                update(ConversationTurn)
                .where(
                    ConversationTurn.user_id == user_id,
                    ConversationTurn.work_chat_id == chat.id,
                    ConversationTurn.state == TurnState.waiting,
                    ConversationTurn.due_at > now,
                    _turn_start.in_(read_messages),  # the opening message (issue #47)
                )
                .values(due_at=now)
                .returning(ConversationTurn.id)
            )
            turn_ids = list(result.scalars().all())
            for turn_id in turn_ids:  # explicit: re-planning keeps these due (FR-017)
                await activity_repo.record(
                    session,
                    user_id,
                    ActivityKind.read_now,
                    at=now,
                    conversation_turn_id=turn_id,
                    meta={"source": "read_elsewhere"},
                )
            shortened = len(turn_ids)
            await session.commit()
        if shortened:
            log.info("read_elsewhere_due_now", user_id=str(user_id), turns=shortened)
        if self._marker is not None:  # specs/009: the user has seen the chat up to here
            await self._marker.seen(
                user_id, event.chat_id, event.max_message_id, source="read_elsewhere"
            )


def _chat_turn_ids(user_id: uuid.UUID, chat: WorkChat) -> Select[uuid.UUID]:
    return select(ConversationTurn.id).where(
        ConversationTurn.user_id == user_id, ConversationTurn.work_chat_id == chat.id
    )


async def _is_agent_send(
    session: AsyncSession, user_id: uuid.UUID, chat: WorkChat, telegram_message_id: int
) -> bool:
    return bool(
        await session.scalar(select(drafts_repo.agent_send(user_id, chat.id, telegram_message_id)))
    )
