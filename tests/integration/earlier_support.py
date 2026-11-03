"""Search over earlier conversations (specs/004) for tests: synthetic ``FoundMessage`` builders,
a seeded user with an enabled private chat, an enabled group and a disabled chat, and a helper
that loads a canned ``search_index`` into ``FakeMessagingGateway``. Synthetic data only
(Principle II): every name, id and text below is made up."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scribe.context.earlier import SearchChat, SearchScope
from scribe.db.models import ChatKind, Contact, ContactKind, User, WorkChat
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import messages as messages_repo
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import FoundMessage

EARLIER_NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
USER_PEER_ID = 7_000_001  # the user's own Telegram id
SENDER_PEER_ID = 7_000_002  # the person the draft answers
THIRD_PEER_ID = 7_000_003  # an unrelated group member
PRIVATE_CHAT_ID = SENDER_PEER_ID  # a private chat's id is the peer id
GROUP_CHAT_ID = -1_000_000_000_101
OTHER_GROUP_CHAT_ID = -1_000_000_000_102
DISABLED_CHAT_ID = -1_000_000_000_199

_message_ids = itertools.count(5_000)


def next_message_id() -> int:
    return next(_message_ids)


def found(
    chat_id: int,
    text: str,
    *,
    message_id: int | None = None,
    sender_peer_id: int = SENDER_PEER_ID,
    sender_display_name: str = "Anna Testova",
    is_outgoing: bool = False,
    sent_at: datetime | None = None,
    days_ago: float = 40,
    reply_to_id: int | None = None,
    thread_top_id: int | None = None,
    mentioned_peer_ids: Iterable[int] = (),
    mentioned_usernames: Iterable[str] = (),
) -> FoundMessage:
    """One synthetic search hit; by default an incoming message from the sender 40 days ago."""
    if is_outgoing:
        sender_peer_id = USER_PEER_ID
        sender_display_name = "Test User"
    return FoundMessage(
        chat_id=chat_id,
        message_id=message_id if message_id is not None else next_message_id(),
        sender_peer_id=sender_peer_id,
        sender_display_name=sender_display_name,
        is_outgoing=is_outgoing,
        text=text,
        sent_at=sent_at if sent_at is not None else EARLIER_NOW - timedelta(days=days_ago),
        reply_to_id=reply_to_id,
        thread_top_id=thread_top_id,
        mentioned_peer_ids=frozenset(mentioned_peer_ids),
        mentioned_usernames=frozenset(n.lower().lstrip("@") for n in mentioned_usernames),
    )


def private_found(text: str, **values: Any) -> FoundMessage:
    """A hit in the private chat with the sender."""
    return found(PRIVATE_CHAT_ID, text, **values)


def group_found(text: str, *, chat_id: int = GROUP_CHAT_ID, **values: Any) -> FoundMessage:
    """A hit in a group chat (by the sender unless ``sender_peer_id`` says otherwise)."""
    return found(chat_id, text, **values)


def load_search_index(
    gateway: FakeMessagingGateway,
    messages: Iterable[FoundMessage],
    *,
    usernames: dict[int, str] | None = None,
) -> FakeMessagingGateway:
    """Put canned messages into the fake's server-side index, grouped by chat."""
    for message in messages:
        gateway.search_index.setdefault(message.chat_id, []).append(message)
    if usernames:
        gateway.usernames.update(usernames)
    return gateway


def private_chat() -> SearchChat:
    return SearchChat(PRIVATE_CHAT_ID, "Anna Testova", ChatKind.private)


def group_chat(chat_id: int = GROUP_CHAT_ID, title: str = "Synthetic team") -> SearchChat:
    return SearchChat(chat_id, title, ChatKind.group)


def make_scope(
    *,
    user_id: uuid.UUID | None = None,
    turn_chat_id: int = PRIVATE_CHAT_ID,
    sender_peer_id: int | None = SENDER_PEER_ID,
    user_peer_id: int | None = USER_PEER_ID,
    incoming_thread_top_id: int | None = None,
    known: Iterable[tuple[int, int]] = (),
) -> SearchScope:
    return SearchScope(
        user_id=user_id or uuid.uuid4(),
        turn_id=None,
        turn_chat_id=turn_chat_id,
        sender_peer_id=sender_peer_id,
        user_peer_id=user_peer_id,
        incoming_thread_top_id=incoming_thread_top_id,
        known=frozenset(known),
    )


@dataclass
class EarlierSeed:
    user: User
    sender: Contact
    private: WorkChat
    group: WorkChat
    disabled: WorkChat


async def seed_earlier_user(
    session: AsyncSession, user: User, *, now: datetime = EARLIER_NOW
) -> EarlierSeed:
    """The sender's enabled private chat, an enabled group where the sender wrote recently and a
    disabled group where the sender also wrote (it must never be searched)."""
    sender = await chats_repo.upsert_contact(
        session,
        user.id,
        telegram_peer_id=SENDER_PEER_ID,
        kind=ContactKind.person,
        display_name="Anna Testova",
    )
    private = await chats_repo.upsert_work_chat(
        session,
        user.id,
        telegram_chat_id=PRIVATE_CHAT_ID,
        title="Anna Testova",
        kind=ChatKind.private,
        enabled=True,
    )
    group = await chats_repo.upsert_work_chat(
        session,
        user.id,
        telegram_chat_id=GROUP_CHAT_ID,
        title="Synthetic team",
        kind=ChatKind.group,
        enabled=True,
    )
    disabled = await chats_repo.upsert_work_chat(
        session,
        user.id,
        telegram_chat_id=DISABLED_CHAT_ID,
        title="Disabled synthetic group",
        kind=ChatKind.group,
        enabled=False,
    )
    for chat in (group, disabled):
        await store_message(session, user, chat, sender, sent_at=now - timedelta(days=2))
    return EarlierSeed(user, sender, private, group, disabled)


async def store_message(
    session: AsyncSession,
    user: User,
    chat: WorkChat,
    sender: Contact | None,
    *,
    sent_at: datetime,
    text: str = "synthetic stored message",
) -> None:
    """A stored ``messages`` row (what ``chats_for`` looks at to pick extra groups)."""
    await messages_repo.add_message(
        session,
        user.id,
        work_chat_id=chat.id,
        telegram_message_id=next_message_id(),
        text=text,
        sent_at=sent_at,
        sender_contact_id=sender.id if sender is not None else None,
    )
