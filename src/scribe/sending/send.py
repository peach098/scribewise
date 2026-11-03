"""The only place a reply is sent (Constitution Principle I, FR-012 to FR-014).

``send_approved_draft`` is called only from the approve/edit callbacks in
``telegram/control_bot/review.py``. It refuses unless the draft is ``pending``, not expired,
explicitly approved (``approved_by_user_at`` set by that callback), not already sent, its turn
still ``pending_review``, and not ``outdated`` unless the caller passes ``allow_outdated``
(the "Send anyway" button or the user's own edited text); the database check constraint
``sent_requires_approval`` backs this up. A user whose access is suspended
(``users.access_suspended_at``, specs/005) gets ``AccessSuspended`` and nothing is sent.

Idempotency (#54): once every check passes for the first time, the draft gets
``send_random_id`` (a random int64, the MTProto ``random_id``) and, for an edited send, the
edited text in ``edited_text``, committed in their own transaction; the checks then run again
under fresh locks and every attempt passes that same value to ``gateway.send_text``. If Telegram
delivers the message but the call fails afterwards (an RPC timeout, or the commit below fails),
the draft stays ``pending`` and the user can tap Send again: Telegram deduplicates the retry by
``random_id`` instead of delivering it twice. Because a deduplicated retry delivers nothing new,
a draft with a key (``send_attempt_unresolved``) only ever sends the text of its first attempt:
another text raises ``SendAttemptUnresolved``, and the review handlers refuse to edit,
regenerate or fill it (the database would otherwise record a text the contact never got).

The draft row and then its turn row are locked (``FOR UPDATE``, the documented draft -> turn
order) *before* Telegram is called and stay locked until commit, so a double tap cannot send
twice (the second call waits, then sees ``sent``) and a concurrent manual-reply drop or
outdated flag waits for the send instead of deadlocking with it. The reply quotes the message
the draft was composed for (``drafts.output_json.reply_to_message_id``), not one that joined
the turn afterwards; that is the contact's last own message, not a forward after it (#47). The
outgoing message is stored in
``messages`` keyed by (work_chat_id, telegram_message_id) with an idempotent insert, because
Telethon may echo the agent's own send as an outgoing event; US2's manual-reply detection tells
agent sends from manual ones by ``drafts.sent_telegram_message_id``.

US3 (FR-031): after the commit, the sent text goes to commitment extraction
(``context/commitments.py``), here and only here for agent sends; the echo is not extracted
again. The extraction's database reads are awaited, the LLM call runs in the background, and any
failure is logged and swallowed: the reply has already been sent and recorded.

specs/009 (mark chats unread): with an ``UnreadMarker`` (``marker``), the chat is marked unread
right after the commit, before commitment extraction, so the chat list still shows where the
agent answered. It runs after the draft and turn locks are released, is bounded (5 s) and never
raises: a failed mark never affects the send.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.context.commitments import CommitmentExtractor, default_extractor
from scribe.db.models import (
    ActivityKind,
    ChatKind,
    ConversationTurn,
    Draft,
    DraftState,
    Message,
    TurnState,
    User,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import messages as messages_repo
from scribe.drafting.stored_output import REPLY_TO_KEY as REPLY_TO_KEY  # re-export
from scribe.drafting.stored_output import load_stored_output
from scribe.logging import get_logger
from scribe.telegram.gateway import MessagingGateway
from scribe.telegram.unread_mark import UnreadMarker

log = get_logger("scribe.sending")


class SendRefused(Exception):
    """The draft may not be sent (not pending, expired, not approved, already sent)."""


class DraftOutdated(SendRefused):
    """New messages arrived after the draft: only an explicit "Send anyway" may send it."""


class AccessSuspended(SendRefused):
    """The user's access has ended (specs/005)."""


class SendAttemptUnresolved(SendRefused):
    """An earlier attempt may have been delivered: only its exact text can be retried (#54)."""


def send_attempt_unresolved(draft: Draft) -> bool:
    """A send of this pending draft was attempted and may have reached the chat (#54)."""
    return draft.state == DraftState.pending and draft.send_random_id is not None


def attempted_text(draft: Draft) -> str | None:
    """The text a draft with a send key was (or is being) sent with."""
    return draft.edited_text if draft.edited_text is not None else draft.text


@dataclass(frozen=True)
class SendResult:
    telegram_message_id: int
    sent_at: datetime
    edited: bool


def reply_to_for(chat_kind: ChatKind, last_incoming: Message | None) -> int | None:
    """FR-012 "as a reply where appropriate": quote the message in group chats, or when the
    incoming message was itself a reply; a plain private conversation needs no quote."""
    if last_incoming is None:
        return None
    if chat_kind != ChatKind.private or last_incoming.reply_to_telegram_message_id is not None:
        return last_incoming.telegram_message_id
    return None


async def send_approved_draft(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    *,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    edited_text: str | None = None,
    allow_outdated: bool = False,
    clock: Clock = utcnow,
    commitments: CommitmentExtractor | None = None,
    marker: UnreadMarker | None = None,
) -> SendResult:
    # At most two passes: the first may only reserve the send key (#54), the second sends.
    for _ in range(2):
        result = await _send_once(
            session_factory,
            gateway,
            user_id=user_id,
            draft_id=draft_id,
            edited_text=edited_text,
            allow_outdated=allow_outdated,
            clock=clock,
            commitments=commitments,
            marker=marker,
        )
        if result is not None:
            return result
    raise SendRefused("draft has no send key")  # unreachable: the first pass committed one


async def _send_once(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    *,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    edited_text: str | None,
    allow_outdated: bool,
    clock: Clock,
    commitments: CommitmentExtractor | None,
    marker: UnreadMarker | None,
) -> SendResult | None:
    """One locked pass: ``None`` when it only committed the draft's new send key."""
    async with session_factory() as session:
        draft = await _lock_draft(session, user_id, draft_id)
        user = await session.get(User, user_id)
        if user is None or user.access_suspended_at is not None:
            # Defence in depth for the race between a tap and access_reconcile (research Q6).
            raise AccessSuspended("access has ended")
        now = clock()
        _check_sendable(draft, now)
        if draft.outdated and not allow_outdated:
            raise DraftOutdated("new messages arrived after this draft")
        # Lock order draft -> turn, both before Telegram is called.
        turn = (
            await session.execute(
                select(ConversationTurn)
                .where(
                    ConversationTurn.user_id == user_id,
                    ConversationTurn.id == draft.conversation_turn_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if turn is None:
            raise SendRefused("conversation turn is gone")
        if turn.state != TurnState.pending_review:
            raise SendRefused(f"conversation turn is {turn.state.value}")
        chat = await chats_repo.get_work_chat(session, user_id, turn.work_chat_id)
        if chat is None:
            raise SendRefused("work chat is gone")
        last_incoming = await _drafted_for(session, user_id, draft, turn)
        text = edited_text if edited_text is not None else draft.text
        if not text or not text.strip():
            raise SendRefused("nothing to send")
        reply_to = reply_to_for(chat.kind, last_incoming)
        if draft.send_random_id is None:
            # First attempt: commit the key (and the text it goes with) before Telegram is
            # called, so a failed attempt cannot roll it back; then check again and send.
            draft.send_random_id = new_random_id()
            if edited_text is not None:
                draft.edited_text = edited_text
            await session.commit()
            return None
        if text != attempted_text(draft):
            # Telegram would drop this retry as a duplicate of the earlier attempt, which may
            # have been delivered with the other text.
            raise SendAttemptUnresolved("an earlier attempt with another text may be delivered")

        # Principle I: reached only with state=pending and approved_by_user_at set (checked
        # above, with the row locked so the check cannot go stale before the send).
        telegram_message_id = await gateway.send_text(
            user_id, chat.telegram_chat_id, text, reply_to, random_id=draft.send_random_id
        )

        sent_at = clock()
        draft_commitment = _unchanged_draft_commitment(draft, text)  # before finalize updates it
        await drafts_repo.finalize(
            session,
            user_id,
            draft.id,
            DraftState.sent,
            at=sent_at,
            sent_telegram_message_id=telegram_message_id,
            **({"edited_text": edited_text} if edited_text is not None else {}),
        )
        turn.state = TurnState.answered
        await messages_repo.add_message_if_absent(
            session,
            user_id,
            work_chat_id=chat.id,
            telegram_message_id=telegram_message_id,
            text=text,
            sent_at=sent_at,
            is_outgoing=True,
            reply_to_telegram_message_id=reply_to,
        )
        await activity_repo.record(
            session,
            user_id,
            ActivityKind.sent,
            at=sent_at,
            draft_id=draft.id,
            conversation_turn_id=turn.id,
            meta={
                "edited": edited_text is not None,
                "approved_at": draft.approved_by_user_at.isoformat()
                if draft.approved_by_user_at
                else None,
                "telegram_message_id": telegram_message_id,
                "reply": reply_to is not None,
            },
        )
        work_chat_id, contact_id = chat.id, turn.contact_id
        await session.commit()
    log.info(
        "sent",
        user_id=str(user_id),
        draft_id=str(draft_id),
        edited=edited_text is not None,
        reply=reply_to is not None,
    )
    if marker is not None:  # outside the row locks (specs/009); never raises
        await marker.after_send(user_id, work_chat_id, telegram_message_id)
    await _extract_commitments(
        commitments or default_extractor(),
        user_id=user_id,
        work_chat_id=work_chat_id,
        contact_id=contact_id,
        text=text,
        sent_at=sent_at,
        draft_commitment=draft_commitment,
    )
    return SendResult(
        telegram_message_id=telegram_message_id, sent_at=sent_at, edited=edited_text is not None
    )


async def _extract_commitments(
    extractor: CommitmentExtractor | None,
    *,
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    contact_id: uuid.UUID,
    text: str,
    sent_at: datetime,
    draft_commitment: bool | None = None,
) -> None:
    """After the send is committed; can never fail or undo it."""
    if extractor is None:
        return
    try:
        await extractor.submit(
            user_id,
            work_chat_id=work_chat_id,
            text=text,
            sent_at=sent_at,
            contact_id=contact_id,
            draft_commitment=draft_commitment,
        )
    except Exception as exc:
        log.warning("commitment_extraction_failed", user_id=str(user_id), error=type(exc).__name__)


def _unchanged_draft_commitment(draft: Draft, sent_text: str) -> bool | None:
    """Issue #72: the draft's ``creates_commitment`` flag when the sent text is the draft as
    composed (no edit, no filled placeholder); None otherwise or when the flag is not stored.
    The flag is the plaintext boolean of ``drafts.output_json`` (``stored_output.pack_output``):
    the composer already merged the model's flag with the keyword detector on this very text."""
    if sent_text != draft.text:
        return None
    flag = (draft.output_json or {}).get("creates_commitment")
    return flag if isinstance(flag, bool) else None


async def _drafted_for(
    session: AsyncSession, user_id: uuid.UUID, draft: Draft, turn: ConversationTurn
) -> Message | None:
    """The newest incoming message the draft was composed for (falls back to the turn's last
    message for drafts stored before the composer recorded it)."""
    stored = load_stored_output(draft.output_json, draft.text)
    message_id: uuid.UUID | None = turn.last_message_id
    if stored.reply_to_recorded:
        try:
            message_id = (
                uuid.UUID(stored.reply_to_message_id) if stored.reply_to_message_id else None
            )
        except ValueError:
            message_id = None
    if message_id is None:
        return None
    return await messages_repo.get_message(session, user_id, message_id)


def new_random_id() -> int:
    """A random non-zero signed int64, as Telethon draws ``random_id`` itself."""
    while True:
        value = int.from_bytes(secrets.token_bytes(8), "big", signed=True)
        if value:
            return value


async def _lock_draft(session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID) -> Draft:
    draft = (
        await session.execute(
            select(Draft).where(Draft.user_id == user_id, Draft.id == draft_id).with_for_update()
        )
    ).scalar_one_or_none()
    if draft is None:
        raise SendRefused("draft not found")
    return draft


def _check_sendable(draft: Draft, now: datetime) -> None:
    if draft.state != DraftState.pending:
        raise SendRefused(f"draft is {draft.state.value}")
    if draft.expires_at <= now:
        raise SendRefused("draft has expired")
    if draft.approved_by_user_at is None:
        raise SendRefused("draft was not approved")
    if draft.sent_telegram_message_id is not None:
        raise SendRefused("draft was already sent")
