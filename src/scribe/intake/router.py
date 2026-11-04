"""Message intake (FR-001, FR-002, FR-003): which incoming messages are stored and become turns.

- A message in a chat that is not an enabled work chat is dropped before anything is stored.
  Work account mode (specs/008): a message in an eligible private chat without a work chat row
  first gets an ``auto`` row when the user's mode is on (``chats.work_account.pick_up``, in the
  same transaction, under the per-chat lock), then follows the normal path; bots, Saved
  Messages, service accounts and unpicked groups are still dropped with nothing stored.
- In group chats a turn is created only when the message addresses the user: it tags the user
  or replies to one of the user's messages (specs/006-group-mention-only). No chat setting
  changes this. Other group messages are stored as context only. A turn's contact in a group is
  the group as a whole (one contact / profile slot), while each message keeps its person sender.
- Consecutive messages from the same sender within ``grouping_window_s`` join the open waiting
  turn, addressed or not (``follow_up``); ``due_at`` is not moved (except to hold a
  forwards-only turn, below). The join is a guarded update: if the turn left ``waiting`` since
  it was looked up (the scheduler claimed or skipped it), the message is routed again as if
  there were no open turn.
- Every decision but ``ignore`` is logged as ``intake`` with its ``reason``
  (:class:`IntakeReason`).
- A forwarded message (issue #47) is never addressed by itself and never gets a draft by itself.
  In a private chat it opens a waiting turn (or joins the sender's open one) so the contact's
  own message, sent before or after it within ``grouping_window_s``, lands in the same turn and
  is answered with the forward as context; a turn that is still forwards-only when due is
  skipped by the reply gate (reason ``forwarded``, no model call) and listed in ``/skipped``.
  A forwards-only turn is never due before ``grouping_window_s`` after its last forward
  (``forwards_hold_until``, kept by a replan), so the contact's own message can still join it;
  when it does, the hold is cleared and the normal reading delay applies again. A turn the user
  asked for ("Draft anyway", "read now") is not held. In a group
  a forward is stored as context only unless it joins the sender's open waiting turn; when the
  same sender then tags or replies to the user within ``grouping_window_s``, the new turn
  starts at that forward (lower Telegram id; ``opening_message_id`` marks the message that
  opened it, and of the sender's messages before it only the forwards belong to the turn). A
  forward is never judged as a closing message, and a forwarded photo is not "media only". A
  forward arriving while a draft is pending review joins that turn and marks the draft
  outdated, like any new message from the contact.
- Media-only messages are never guessed at: the turn goes straight to ``handle_manually`` and
  the user is told in the control chat.
- While a draft for the same chat and contact is pending review (US2, FR-015), a new addressed
  message marks that draft ``outdated`` and the card is re-rendered with the "New messages
  arrived" banner. A text message from the turn's sender joins the pending turn
  (``last_message_id``) so a regenerated draft covers it; anything else follows the rules above.
- A message that only closes the exchange ("thanks", "ok", "bye"; specs/004-skip-closing-messages
  FR-011) does not leave the pending draft outdated: the draft is flagged as above inside the
  intake transaction (so a Send tapped meanwhile still asks first), then, after the commit and
  outside the chat lock, the reply gate (``ReplyGate.decide_for_pending``) judges the message.
  ``draft`` (or any failure) keeps the flag; ``skip`` clears it again unless the draft was
  already outdated or a newer message arrived meanwhile, and ``skip`` with ``resolves_pending``
  sets ``drafts.possibly_unnecessary`` (the card's 💤 banner). Only pre-gate candidates (short,
  no question, no media) are judged; the card is re-rendered once, after the decision.
- While the contact's turn is ``reading`` (being read and drafted, no draft yet), a text message
  from the turn's sender joins that turn instead of opening a parallel one; the composer sees
  that ``last_message_id`` moved during drafting and stores the draft ``outdated`` (banner).
- A message older than a manual reply already stored for the chat (the outgoing update was
  handled first: Telethon runs handlers concurrently) is answered: its new turn is created
  ``dropped`` (``manual_reply``) and never read or drafted. Incoming and outgoing handling of a
  chat share one lock (``intake/locks.py``).

The decision itself is the pure :func:`decide`; :class:`IntakeRouter` loads and stores rows.
Message text is never logged.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple, Protocol

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.context.history import turn_messages
from scribe.db.models import (
    ActivityKind,
    ChatKind,
    Contact,
    ContactKind,
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
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.reply_gate import GateDecision, is_forwards_only, is_skip_candidate
from scribe.i18n import Msg
from scribe.intake.locks import CHAT_LOCKS, ChatLocks
from scribe.intake.waiting import (
    delay_range,
    pick_delay,
    planned_reading_time,
    read_now_turn_ids,
)
from scribe.logging import get_logger
from scribe.telegram.gateway import IncomingMessage

log = get_logger("scribe.intake")

# (user_id, message): rendered by the notifier in the user's stored language.
Notifier = Callable[[uuid.UUID, Msg], Awaitable[None]]
OutdatedHandler = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]  # (user_id, draft_id)


class PendingGate(Protocol):
    """``scribe.drafting.reply_gate.ReplyGate`` (004 contracts/reply-gate.md)."""

    async def decide_for_pending(
        self, user_id: uuid.UUID, message_id: uuid.UUID
    ) -> GateDecision: ...


class FlaggedDraft(NamedTuple):
    """A pending draft this message flagged outdated (FR-015)."""

    draft_id: uuid.UUID
    was_outdated: bool  # already outdated before this message
    message_id: uuid.UUID  # the stored message (messages.id)
    work_chat_id: uuid.UUID
    telegram_message_id: int


class IntakeReason(enum.StrEnum):
    """Why a message was (or was not) addressed to the user (006 FR-011). Logged, never stored."""

    private = "private"  # private work chat: always addressed
    reply = "reply"  # group: replies to one of the user's messages (wins over ``tagged``)
    tagged = "tagged"  # group: tags the user
    follow_up = "follow_up"  # group: not addressed, joins the sender's open waiting turn
    not_addressed = "not_addressed"  # group: not addressed, stored as context only
    # #47: a forwarded copy outside an open turn: a forwards-only turn in a private chat (never
    # drafted by itself), context only in a group.
    forwarded = "forwarded"


class Action(enum.StrEnum):
    ignore = "ignore"  # not a work chat (or not intake at all): nothing stored
    store_only = "store_only"  # context only (group message not addressed to the user)
    attach = "attach"  # joins the open waiting turn of the same sender
    new_turn = "new_turn"
    handle_manually = "handle_manually"  # media only
    duplicate = "duplicate"  # already stored (redelivered update)
    attach_pending = "attach_pending"  # joins the turn whose draft is pending review (outdated)
    attach_reading = "attach_reading"  # joins the turn being read / drafted (draft -> outdated)
    answered = "answered"  # older than a manual reply already handled: turn created dropped


@dataclass(frozen=True)
class ChatRule:
    enabled: bool
    kind: ChatKind

    @classmethod
    def of(cls, chat: WorkChat) -> ChatRule:
        return cls(enabled=chat.enabled, kind=chat.kind)


@dataclass(frozen=True)
class OpenTurn:
    """The latest ``waiting`` turn of the same sender in the same chat."""

    turn_id: uuid.UUID
    last_message_at: datetime

    def within(self, message: IncomingMessage, window_s: int) -> bool:
        return message.sent_at - self.last_message_at <= timedelta(seconds=window_s)


@dataclass(frozen=True)
class IntakeDecision:
    action: Action
    turn_id: uuid.UUID | None = None
    reason: IntakeReason | None = None  # None for ``ignore`` / ``duplicate``


def is_media_only(message: IncomingMessage) -> bool:
    return message.has_media and not message.text.strip()


def address_reason(message: IncomingMessage, chat: ChatRule) -> IntakeReason | None:
    """006 FR-001/FR-002: private chats always; groups only when the message replies to the
    user's message (checked first) or tags the user. None: not addressed; a forwarded copy never
    is, in any chat (#47)."""
    if message.is_forwarded:
        return None
    if chat.kind == ChatKind.private:
        return IntakeReason.private
    if message.is_reply_to_me:
        return IntakeReason.reply
    if message.mentions_me:
        return IntakeReason.tagged
    return None


def decide(
    message: IncomingMessage,
    chat: ChatRule | None,
    open_turn: OpenTurn | None,
    *,
    grouping_window_s: int,
) -> IntakeDecision:
    if chat is None or not chat.enabled or message.is_outgoing:
        return IntakeDecision(Action.ignore)
    reason = address_reason(message, chat)
    joins = open_turn is not None and open_turn.within(message, grouping_window_s)
    if message.is_forwarded:  # #47: never addressed; media or not, it is never media-only
        if joins and open_turn is not None:
            return IntakeDecision(Action.attach, open_turn.turn_id, IntakeReason.follow_up)
        if chat.kind == ChatKind.private:
            return IntakeDecision(Action.new_turn, reason=IntakeReason.forwarded)
        return IntakeDecision(Action.store_only, reason=IntakeReason.forwarded)
    if reason is None and not joins:
        return IntakeDecision(Action.store_only, reason=IntakeReason.not_addressed)
    if is_media_only(message):
        return IntakeDecision(Action.handle_manually, reason=reason)
    if joins and open_turn is not None:
        return IntakeDecision(Action.attach, open_turn.turn_id, reason or IntakeReason.follow_up)
    return IntakeDecision(Action.new_turn, reason=reason)


class IntakeRouter:
    """``MessagingGateway.on_new_message`` handler."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        clock: Clock = utcnow,
        notifier: Notifier | None = None,
        locks: ChatLocks | None = None,
        gate: PendingGate | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._clock = clock
        self._notifier = notifier
        self._gate = gate
        self._on_outdated: OutdatedHandler | None = None
        # Telethon runs update handlers concurrently: serialise per chat (shared with the
        # outgoing-message handler) so two messages arriving together cannot both open a new
        # turn, and a manual reply is never handled in the middle of an incoming message.
        self._locks = locks if locks is not None else CHAT_LOCKS

    def on_draft_outdated(self, handler: OutdatedHandler) -> None:
        """Called after commit with (user_id, draft_id) when a pending draft became outdated."""
        self._on_outdated = handler

    async def handle_new_message(self, message: IncomingMessage) -> None:
        await self.route(message)

    async def route(self, message: IncomingMessage) -> IntakeDecision:
        lock = self._locks.for_chat(message.user_id, message.chat_id)
        notice: Msg | None = None
        async with lock, self._session_factory() as session:
            decision, chat_kind, contact, outdated_draft = await self._handle(session, message)
            await session.commit()
        if decision.action == Action.handle_manually and contact is not None:
            notice = Msg("notice.media_only", name=contact.display_name or Msg("common.someone"))
        if decision.action != Action.ignore:
            log.info(
                "intake",
                user_id=str(message.user_id),
                action=decision.action.value,
                reason=decision.reason.value if decision.reason else None,
                chat_kind=chat_kind,
                turn_id=str(decision.turn_id) if decision.turn_id else None,
            )
        if notice is not None and self._notifier is not None:
            try:
                await self._notifier(message.user_id, notice)
            except Exception:
                log.exception("control_bot_notify_failed", user_id=str(message.user_id))
        if outdated_draft is not None:
            await self._settle_pending(message, outdated_draft)
            if self._on_outdated is not None:
                try:
                    await self._on_outdated(message.user_id, outdated_draft.draft_id)
                except Exception as exc:
                    log.warning(
                        "draft_card_edit_failed",
                        draft_id=str(outdated_draft.draft_id),
                        error=type(exc).__name__,
                    )
        return decision

    async def _settle_pending(self, message: IncomingMessage, flagged: FlaggedDraft) -> None:
        """004 FR-011: a closing message does not leave the pending draft outdated by itself.

        Asks the reply gate about a pre-gate candidate only; ``draft`` or any failure keeps the
        outdated flag set in the intake transaction. Never raises."""
        if (
            self._gate is None
            or message.is_forwarded
            # ``has_rules=True``: the widest pre-check; the gate itself knows the user's rules.
            or not is_skip_candidate(
                [message.text],
                has_rules=True,
                max_chars=self._settings.skip_candidate_max_chars,
            )
        ):
            return
        try:
            decision = await self._gate.decide_for_pending(message.user_id, flagged.message_id)
            if decision.decision != "skip":
                return
            async with self._session_factory() as session:
                draft = (
                    await session.execute(
                        select(Draft)
                        .where(
                            Draft.user_id == message.user_id,
                            Draft.id == flagged.draft_id,
                            Draft.state == DraftState.pending,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if draft is None:
                    return
                newer = await session.scalar(
                    select(
                        exists().where(
                            Message.user_id == message.user_id,
                            Message.work_chat_id == flagged.work_chat_id,
                            Message.is_outgoing.is_(False),
                            Message.telegram_message_id > flagged.telegram_message_id,
                        )
                    )
                )
                cleared = draft.outdated and not flagged.was_outdated and not newer
                if cleared:
                    draft.outdated = False
                if decision.resolves_pending:
                    draft.possibly_unnecessary = True
                await session.commit()
            log.info(
                "pending_draft_closing_message",
                user_id=str(message.user_id),
                draft_id=str(flagged.draft_id),
                outdated_cleared=cleared,
                possibly_unnecessary=decision.resolves_pending,
            )
        except Exception as exc:
            log.warning(
                "pending_gate_failed", draft_id=str(flagged.draft_id), error=type(exc).__name__
            )

    async def _handle(
        self, session: AsyncSession, message: IncomingMessage
    ) -> tuple[IntakeDecision, str | None, Contact | None, FlaggedDraft | None]:
        """(decision, chat kind, contact to tell about a media-only message, outdated draft)."""
        user_id = message.user_id
        chat = await chats_repo.get_work_chat_by_telegram_id(session, user_id, message.chat_id)
        if chat is None and not message.is_outgoing:
            chat = await work_account.pick_up(
                session,
                user_id,
                telegram_chat_id=message.chat_id,
                title=message.chat_title or message.sender_display_name,
                chat_kind=message.chat_kind,
                peer_kind=message.peer_kind,
                direction="in",
                now=self._clock(),
            )
        rule = ChatRule.of(chat) if chat is not None else None
        if chat is None or rule is None or not rule.enabled or message.is_outgoing:
            return IntakeDecision(Action.ignore), None, None, None  # FR-001: nothing stored
        if await messages_repo.get_by_telegram_id(session, user_id, chat.id, message.message_id):
            return IntakeDecision(Action.duplicate), chat.kind.value, None, None

        now = self._clock()
        chats_repo.touch_last_seen(chat, now)  # issue #57: the retention job's "unseen" clock
        sender = await chats_repo.upsert_contact(
            session,
            user_id,
            telegram_peer_id=message.sender_peer_id,
            kind=ContactKind.person,
            display_name=message.sender_display_name,
            now=now,
        )
        turn_contact = sender
        if chat.kind != ChatKind.private:
            turn_contact = await chats_repo.upsert_contact(
                session,
                user_id,
                telegram_peer_id=chat.telegram_chat_id,
                kind=ContactKind.group,
                display_name=chat.title or message.chat_title,
                now=now,
            )

        open_turn = await self._open_turn(session, chat, turn_contact, sender)
        decision = decide(
            message, rule, open_turn, grouping_window_s=self._settings.grouping_window_s
        )
        if decision.action == Action.ignore:
            return decision, chat.kind.value, None, None

        stored = await messages_repo.add_message_if_absent(
            session,
            user_id,
            work_chat_id=chat.id,
            telegram_message_id=message.message_id,
            text=message.text,
            sent_at=message.sent_at,
            has_media=message.has_media,
            sender_contact_id=sender.id,
            reply_to_telegram_message_id=message.reply_to_id,
            is_forwarded=message.is_forwarded,
            forwarded_from=message.forwarded_from,
        )

        outdated: FlaggedDraft | None = None
        if decision.action == Action.attach and decision.turn_id is not None:
            if await turns_repo.attach_message(
                session, user_id, decision.turn_id, stored.id, expected_state=TurnState.waiting
            ):
                if message.is_forwarded:
                    await self._hold_forwards_only(session, user_id, decision.turn_id, message)
                else:
                    await self._release_hold(session, user_id, decision.turn_id)
                return decision, chat.kind.value, None, None
            # The turn left ``waiting`` after it was looked up (the scheduler claimed or
            # skipped it): route the message again as if there were no open turn.
            decision = decide(
                message, rule, None, grouping_window_s=self._settings.grouping_window_s
            )

        if decision.action in (Action.new_turn, Action.handle_manually):
            if await self._answered_later(session, chat, message):
                turn = await self._create_answered_turn(session, chat, turn_contact, stored)
                return (
                    IntakeDecision(Action.answered, turn.id, decision.reason),
                    chat.kind.value,
                    None,
                    None,
                )
            joinable = message.is_forwarded or not is_media_only(message)
            outdated, joined = await self._flag_pending_draft(
                session, chat, turn_contact, sender, stored, joinable=joinable
            )
            if joined is not None:
                return (
                    IntakeDecision(Action.attach_pending, joined, decision.reason),
                    chat.kind.value,
                    None,
                    outdated,
                )
            if outdated is None and joinable:
                reading = await self._join_reading_turn(session, chat, turn_contact, sender, stored)
                if reading is not None:
                    return (
                        IntakeDecision(Action.attach_reading, reading, decision.reason),
                        chat.kind.value,
                        None,
                        None,
                    )
                # The reading turn may have just become pending_review (its draft committed
                # while we waited for the turn row): flag that draft instead.
                outdated, joined = await self._flag_pending_draft(
                    session, chat, turn_contact, sender, stored, joinable=joinable
                )
                if joined is not None:
                    return (
                        IntakeDecision(Action.attach_pending, joined, decision.reason),
                        chat.kind.value,
                        None,
                        outdated,
                    )

        if decision.action == Action.new_turn:
            user = await users_repo.get_user(session, user_id)
            if user is None:
                return IntakeDecision(Action.ignore), chat.kind.value, None, outdated
            delay_s = pick_delay(*delay_range(user, chat))
            due_at = planned_reading_time(message.sent_at, delay_s, user)
            first = stored
            hold: datetime | None = None
            if message.is_forwarded:
                # #47: not due before the grouping window has passed (kept by a replan, cleared
                # when the contact's own message joins).
                hold = self._hold_until(message)
                due_at = max(due_at, planned_reading_time(hold, 0, user))
            elif chat.kind != ChatKind.private:
                first = await self._preceding_forward(session, chat, turn_contact, sender, stored)
            turn = await turns_repo.create_turn(
                session,
                user_id,
                work_chat_id=chat.id,
                contact_id=turn_contact.id,
                first_message_id=first.id,
                arrived_at=message.sent_at,
                delay_s=delay_s,
                due_at=due_at,
            )
            turn.forwards_hold_until = hold
            if first.id != stored.id:  # the turn starts at the sender's preceding forward
                turn.last_message_id = stored.id
                turn.opening_message_id = stored.id
            decision = IntakeDecision(Action.new_turn, turn.id, decision.reason)
        elif decision.action == Action.handle_manually:
            turn = await turns_repo.create_turn(
                session,
                user_id,
                work_chat_id=chat.id,
                contact_id=turn_contact.id,
                first_message_id=stored.id,
                arrived_at=message.sent_at,
                delay_s=0,
                due_at=self._clock(),
                state=TurnState.handle_manually,
            )
            turn.dropped_reason = DroppedReason.media_only
            decision = IntakeDecision(Action.handle_manually, turn.id, decision.reason)
            return decision, chat.kind.value, sender, outdated
        return decision, chat.kind.value, None, outdated

    def _hold_until(self, message: IncomingMessage) -> datetime:
        return message.sent_at + timedelta(seconds=self._settings.grouping_window_s)

    async def _hold_forwards_only(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        turn_id: uuid.UUID,
        message: IncomingMessage,
    ) -> None:
        """A forward joined a waiting turn: while the turn is still forwards-only, it is not
        due before the grouping window after this forward (issue #47). One guarded UPDATE on
        the row's current values, so a concurrent replan is not overwritten. A turn the user
        asked for ("Draft anyway", "read now", read elsewhere) is never held back."""
        turn = (
            await session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.user_id == user_id, ConversationTurn.id == turn_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if turn is None or turn.draft_anyway_at is not None:
            return
        if not is_forwards_only(await turn_messages(session, turn)):
            return
        if await read_now_turn_ids(session, user_id, [turn_id]):
            return
        user = await users_repo.get_user(session, user_id)
        if user is None:
            return
        hold = self._hold_until(message)
        await session.execute(
            update(ConversationTurn)
            .where(
                ConversationTurn.user_id == user_id,
                ConversationTurn.id == turn_id,
                ConversationTurn.state == TurnState.waiting,
                ConversationTurn.draft_anyway_at.is_(None),
            )
            .values(
                forwards_hold_until=func.greatest(
                    func.coalesce(ConversationTurn.forwards_hold_until, hold), hold
                ),
                due_at=func.greatest(ConversationTurn.due_at, planned_reading_time(hold, 0, user)),
            )
            .execution_options(synchronize_session=False)
        )

    async def _release_hold(
        self, session: AsyncSession, user_id: uuid.UUID, turn_id: uuid.UUID
    ) -> None:
        """The contact's own message joined: a forwards-only hold no longer applies, and the
        turn is due by its normal reading delay again (never later than it already was, so a
        "read now" stays). One guarded UPDATE (issue #47)."""
        row = (
            await session.execute(
                select(
                    ConversationTurn.arrived_at,
                    ConversationTurn.delay_s,
                    ConversationTurn.forwards_hold_until,
                ).where(ConversationTurn.user_id == user_id, ConversationTurn.id == turn_id)
            )
        ).one_or_none()
        if row is None or row.forwards_hold_until is None:
            return
        user = await users_repo.get_user(session, user_id)
        if user is None:
            return
        planned = planned_reading_time(row.arrived_at, row.delay_s, user)
        await session.execute(
            update(ConversationTurn)
            .where(
                ConversationTurn.user_id == user_id,
                ConversationTurn.id == turn_id,
                ConversationTurn.state == TurnState.waiting,
                ConversationTurn.forwards_hold_until.is_not(None),
            )
            .values(
                forwards_hold_until=None,
                due_at=func.least(ConversationTurn.due_at, planned),
            )
            .execution_options(synchronize_session=False)
        )

    async def _preceding_forward(
        self,
        session: AsyncSession,
        chat: WorkChat,
        turn_contact: Contact,
        sender: Contact,
        stored: Message,
    ) -> Message:
        """Group chats: the first message of a new turn is the sender's earliest forward sent
        just before ``stored`` (lower Telegram id, within the grouping window) that no turn
        covers yet; a group stores a forward as context only, and "forward, then tag the user"
        is one turn (issue #47). Else ``stored``."""
        since = stored.sent_at - timedelta(seconds=self._settings.grouping_window_s)
        covered = (
            select(func.max(Message.telegram_message_id))
            .select_from(ConversationTurn)
            .join(Message, Message.id == ConversationTurn.last_message_id)
            .where(
                ConversationTurn.user_id == chat.user_id,
                ConversationTurn.work_chat_id == chat.id,
                ConversationTurn.contact_id == turn_contact.id,
                Message.sender_contact_id == sender.id,
            )
            .scalar_subquery()
        )
        earliest = await session.scalar(
            select(Message)
            .where(
                Message.user_id == chat.user_id,
                Message.work_chat_id == chat.id,
                Message.sender_contact_id == sender.id,
                Message.is_outgoing.is_(False),
                Message.is_forwarded.is_(True),
                Message.telegram_message_id < stored.telegram_message_id,
                Message.sent_at >= since,
                Message.sent_at <= stored.sent_at,
                or_(covered.is_(None), Message.telegram_message_id > covered),
            )
            .order_by(Message.telegram_message_id)
            .limit(1)
        )
        return earliest if earliest is not None else stored

    @staticmethod
    async def _flag_pending_draft(
        session: AsyncSession,
        chat: WorkChat,
        turn_contact: Contact,
        sender: Contact,
        stored: Message,
        *,
        joinable: bool,
    ) -> tuple[FlaggedDraft | None, uuid.UUID | None]:
        """FR-015: mark the pending draft for this chat and contact outdated.

        Returns (the flagged draft, turn id the message joined). The draft row is updated first
        (lock order draft -> turn): if an approved send holds it, this waits and then sees it is
        no longer pending, and the message starts a new turn instead.
        """
        row = (
            await session.execute(
                select(
                    Draft.id, Draft.outdated, ConversationTurn.id, ConversationTurn.last_message_id
                )
                .join(ConversationTurn, ConversationTurn.id == Draft.conversation_turn_id)
                .where(
                    Draft.user_id == chat.user_id,
                    Draft.state == DraftState.pending,
                    ConversationTurn.work_chat_id == chat.id,
                    ConversationTurn.contact_id == turn_contact.id,
                    ConversationTurn.state == TurnState.pending_review,
                )
                .order_by(Draft.prepared_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None, None
        draft_id, was_outdated, turn_id, last_message_id = row
        flagged = await session.execute(
            update(Draft)
            .where(Draft.id == draft_id, Draft.state == DraftState.pending)
            .values(outdated=True)
            .returning(Draft.id)
        )
        if flagged.scalar_one_or_none() is None:
            return None, None
        flag = FlaggedDraft(
            draft_id, bool(was_outdated), stored.id, chat.id, stored.telegram_message_id
        )
        last = (
            await messages_repo.get_message(session, chat.user_id, last_message_id)
            if last_message_id is not None
            else None
        )
        if not joinable or last is None or last.sender_contact_id != sender.id:
            return flag, None
        joined = await turns_repo.attach_message(
            session, chat.user_id, turn_id, stored.id, expected_state=TurnState.pending_review
        )
        return flag, turn_id if joined else None

    @staticmethod
    async def _join_reading_turn(
        session: AsyncSession,
        chat: WorkChat,
        turn_contact: Contact,
        sender: Contact,
        stored: Message,
    ) -> uuid.UUID | None:
        """Join the contact's turn that is being read / drafted when the message comes from the
        sender of its last message. Returns the turn id, or None (no such turn, or it left
        ``reading`` while we waited for its row)."""
        turn = await turns_repo.find_turn_in_state(
            session,
            chat.user_id,
            work_chat_id=chat.id,
            state=TurnState.reading,
            contact_id=turn_contact.id,
        )
        if turn is None or turn.last_message_id is None:
            return None
        last = await messages_repo.get_message(session, chat.user_id, turn.last_message_id)
        if last is None or last.sender_contact_id != sender.id:
            return None
        joined = await turns_repo.attach_message(
            session, chat.user_id, turn.id, stored.id, expected_state=TurnState.reading
        )
        return turn.id if joined else None

    @staticmethod
    async def _answered_later(
        session: AsyncSession, chat: WorkChat, message: IncomingMessage
    ) -> bool:
        """A manual reply (stored outgoing message that is not an agent send) newer than this
        message (higher Telegram id, not sent before it) already exists in the chat: the user
        answered from another device."""
        agent_send = drafts_repo.agent_send(chat.user_id, chat.id, Message.telegram_message_id)
        found = await session.execute(
            select(Message.id)
            .where(
                Message.user_id == chat.user_id,
                Message.work_chat_id == chat.id,
                Message.is_outgoing.is_(True),
                Message.telegram_message_id > message.message_id,
                Message.sent_at >= message.sent_at,
                ~agent_send,
            )
            .limit(1)
        )
        return found.scalar_one_or_none() is not None

    async def _create_answered_turn(
        self, session: AsyncSession, chat: WorkChat, turn_contact: Contact, stored: Message
    ) -> ConversationTurn:
        """The turn is recorded, already ``dropped`` (``manual_reply``): never read or drafted."""
        now = self._clock()
        turn = await turns_repo.create_turn(
            session,
            chat.user_id,
            work_chat_id=chat.id,
            contact_id=turn_contact.id,
            first_message_id=stored.id,
            arrived_at=stored.sent_at,
            delay_s=0,
            due_at=now,
            state=TurnState.dropped,
        )
        turn.dropped_reason = DroppedReason.manual_reply
        await activity_repo.record(
            session,
            chat.user_id,
            ActivityKind.dropped,
            at=now,
            conversation_turn_id=turn.id,
            meta={"reason": DroppedReason.manual_reply.value, "answered_before_intake": True},
        )
        return turn

    @staticmethod
    async def _open_turn(
        session: AsyncSession, chat: WorkChat, turn_contact: Contact, sender: Contact
    ) -> OpenTurn | None:
        turn = await turns_repo.find_turn_in_state(
            session,
            chat.user_id,
            work_chat_id=chat.id,
            state=TurnState.waiting,
            contact_id=turn_contact.id,
        )
        if turn is None or turn.last_message_id is None:
            return None
        last = await messages_repo.get_message(session, chat.user_id, turn.last_message_id)
        if last is None or last.sender_contact_id != sender.id:
            return None
        return OpenTurn(turn_id=turn.id, last_message_at=last.sent_at)
