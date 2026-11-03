"""Message context for a draft (FR-005, FR-020): the turn's messages plus candidate history.

Candidates are the most recent messages in the chat and the most recent messages the same
contact wrote in other chats (e.g. group chats), all inside the retention window, decrypted,
deduplicated, oldest first.

Both windows are *anchored* (issue #70) so that consecutive turns send a byte-identical head and
the provider's prefix cache (DeepSeek automatic, Anthropic breakpoints) can reuse it. Instead of
"the newest 40", a window starts at a message whose position in the stream (counted over all
stored messages of the chat, or of the contact elsewhere) is a multiple of ``WINDOW_STEP``; new
messages only append at the tail, and the head moves by ``WINDOW_STEP`` messages once the window
would exceed its maximum. Sizes:

- chat: ``RECENT_IN_CHAT`` (40) to ``RECENT_IN_CHAT + WINDOW_STEP - 1`` (59) messages, the turn's
  own messages included (they are left out of the history afterwards);
- the contact elsewhere: ``PRIOR_WITH_CONTACT`` (10) to ``PRIOR_WITH_CONTACT + WINDOW_STEP - 1``
  (29) messages.

On average that is about 50 + 20 candidates (before: exactly 40 + 20), at most 59 + 29. Messages
older than the retention window are still left out, so in a quiet chat the head can move when
its oldest message ages out, and the daily retention purge shifts the count once a day; both
only cost one cache miss. The short per-request ids of the filter prompt (``llm/aliases.py``)
number history oldest first from the window's head, so they stay stable with it.

Every item has a stable id ``msg:<uuid>`` that the draft must cite (the prompts carry it as a
short per-request alias, ``llm/aliases.py``; issue #71). A forwarded message is marked
``forwarded`` (with ``forwarded_from`` when the origin is known): its text is not the sender's
own words (#47).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import ConversationTurn, Message
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import messages as messages_repo
from scribe.llm.provider import HistoryItem, MessageItem

RECENT_IN_CHAT = 40  # smallest chat window
PRIOR_WITH_CONTACT = 10  # smallest window of the contact's messages in other chats
WINDOW_STEP = 20  # the head of a window moves by this many messages at a time
USER_SENDER = "me"
MESSAGE_PREFIX = "msg:"


def message_ref(message_id: uuid.UUID) -> str:
    return f"{MESSAGE_PREFIX}{message_id}"


def forward_fields(message: Message) -> dict[str, Any]:
    """``forwarded`` / ``forwarded_from`` of a context item for ``message`` (#47)."""
    return {"forwarded": message.is_forwarded, "forwarded_from": message.forwarded_from}


def reply_target(messages: list[Message]) -> Message | None:
    """The message a reply answers: the sender's last own (not forwarded) message in the
    turn; the last message when the turn holds forwards only ("Draft anyway", #47)."""
    own = [m for m in messages if not m.is_forwarded]
    if own:
        return own[-1]
    return messages[-1] if messages else None


def parse_message_ref(ref: str) -> uuid.UUID | None:
    if not ref.startswith(MESSAGE_PREFIX):
        return None
    try:
        return uuid.UUID(ref[len(MESSAGE_PREFIX) :])
    except ValueError:
        return None


def anchored_window[T](newest_first: list[T], total: int, *, minimum: int, step: int) -> list[T]:
    """The newest messages of a stream whose head is anchored (issue #70).

    ``total`` is the number of messages in the whole stream, ``newest_first`` its newest ones
    (``minimum + step - 1`` of them is enough). The window holds the messages at positions
    ``>= start``, where ``start`` is the largest multiple of ``step`` that still leaves at least
    ``minimum`` messages: between ``minimum`` and ``minimum + step - 1`` messages, and the same
    head until the stream has grown by ``step``."""
    start = max(0, (total - minimum) // step * step)
    return newest_first[: max(0, total - start)]


@dataclass(frozen=True)
class Candidates:
    incoming: list[MessageItem]
    history: list[HistoryItem]
    last_incoming: Message | None  # the reply target (``reply_target``): what the draft answers
    # (telegram chat id, telegram message id) of every incoming and history item: a search of
    # earlier conversations skips these (specs/004 research S6).
    telegram_ids: frozenset[tuple[int, int]] = frozenset()


async def turn_messages(session: AsyncSession, turn: ConversationTurn) -> list[Message]:
    """The incoming messages grouped into ``turn`` (same sender, first..last), oldest first;
    before ``turn.opening_message_id`` (when set) only forwarded messages count."""
    if turn.first_message_id is None or turn.last_message_id is None:
        return []
    first = await messages_repo.get_message(session, turn.user_id, turn.first_message_id)
    last = await messages_repo.get_message(session, turn.user_id, turn.last_message_id)
    if first is None or last is None:
        return []
    between = await messages_repo.list_in_chat_between(
        session, turn.user_id, turn.work_chat_id, start=first.sent_at, end=last.sent_at
    )
    messages = [
        m for m in between if not m.is_outgoing and m.sender_contact_id == last.sender_contact_id
    ]
    if turn.opening_message_id is None:
        return messages
    # #47, groups: the turn starts at the sender's forward(s) before the message that opened
    # it; the sender's other messages before it (to other people) stay context only.
    opening = next((i for i, m in enumerate(messages) if m.id == turn.opening_message_id), None)
    if opening is None:
        return messages
    return [m for m in messages[:opening] if m.is_forwarded] + messages[opening:]


async def load_candidates(
    session: AsyncSession,
    turn: ConversationTurn,
    *,
    now: datetime,
    retention_days: int = 30,
    timezone: str = "UTC",
) -> Candidates:
    """History and the turn's incoming messages; ``sent_at`` in the user's ``timezone`` (the
    drafting model resolves relative dates against it)."""
    tz = ZoneInfo(timezone)
    since = now - timedelta(days=retention_days)
    in_turn = await turn_messages(session, turn)
    turn_ids = {m.id for m in in_turn}
    last = reply_target(in_turn)

    names: dict[uuid.UUID, str] = {}

    async def sender_name(contact_id: uuid.UUID | None) -> str:
        if contact_id is None:
            return USER_SENDER
        if contact_id not in names:
            contact = await chats_repo.get_contact(session, turn.user_id, contact_id)
            names[contact_id] = contact.display_name if contact is not None else ""
        return names[contact_id]

    recent = await messages_repo.list_recent_in_chat(
        session, turn.user_id, turn.work_chat_id, since=since, limit=RECENT_IN_CHAT + WINDOW_STEP
    )
    in_chat = await messages_repo.count_in_chat(session, turn.user_id, turn.work_chat_id)
    window = anchored_window(recent, in_chat, minimum=RECENT_IN_CHAT, step=WINDOW_STEP)
    selected: dict[uuid.UUID, Message] = {m.id: m for m in window if m.id not in turn_ids}

    sender_id = last.sender_contact_id if last is not None else None
    if sender_id is not None:
        prior = await messages_repo.list_with_contact(
            session,
            turn.user_id,
            sender_id,
            since=since,
            limit=PRIOR_WITH_CONTACT + WINDOW_STEP,
            exclude_chat_id=turn.work_chat_id,
        )
        elsewhere = await messages_repo.count_with_contact(
            session, turn.user_id, sender_id, exclude_chat_id=turn.work_chat_id
        )
        window = anchored_window(prior, elsewhere, minimum=PRIOR_WITH_CONTACT, step=WINDOW_STEP)
        for message in window:
            selected.setdefault(message.id, message)

    chat_ids: dict[uuid.UUID, int] = {}

    async def telegram_id(message: Message) -> tuple[int, int]:
        if message.work_chat_id not in chat_ids:
            chat = await chats_repo.get_work_chat(session, turn.user_id, message.work_chat_id)
            chat_ids[message.work_chat_id] = chat.telegram_chat_id if chat is not None else 0
        return chat_ids[message.work_chat_id], message.telegram_message_id

    history = []
    for message in sorted(selected.values(), key=lambda m: (m.sent_at, m.telegram_message_id)):
        if not message.text.strip():
            continue  # media without caption carries nothing to cite
        history.append(
            HistoryItem(
                id=message_ref(message.id),
                is_user=message.is_outgoing,
                sender=await sender_name(
                    None if message.is_outgoing else message.sender_contact_id
                ),
                text=message.text,
                sent_at=message.sent_at.astimezone(tz),
                **forward_fields(message),
            )
        )
    incoming = [
        MessageItem(
            id=message_ref(m.id),
            sender=await sender_name(m.sender_contact_id),
            text=m.text,
            sent_at=m.sent_at.astimezone(tz),
            **forward_fields(m),
        )
        for m in in_turn
    ]
    known = frozenset(
        [
            *[await telegram_id(m) for m in selected.values()],
            *[await telegram_id(m) for m in in_turn],
        ]
    )
    return Candidates(incoming=incoming, history=history, last_incoming=last, telegram_ids=known)
