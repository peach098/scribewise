"""``messages`` rows (30-day window), always scoped by ``user_id``."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import Message


async def add_message(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    work_chat_id: uuid.UUID,
    telegram_message_id: int,
    text: str,
    sent_at: datetime,
    is_outgoing: bool = False,
    has_media: bool = False,
    sender_contact_id: uuid.UUID | None = None,
    reply_to_telegram_message_id: int | None = None,
    is_forwarded: bool = False,
    forwarded_from: str | None = None,
) -> Message:
    message = Message(
        user_id=user_id,
        work_chat_id=work_chat_id,
        telegram_message_id=telegram_message_id,
        text=text,
        sent_at=sent_at,
        is_outgoing=is_outgoing,
        has_media=has_media,
        sender_contact_id=sender_contact_id,
        reply_to_telegram_message_id=reply_to_telegram_message_id,
        is_forwarded=is_forwarded,
        forwarded_from=forwarded_from,
    )
    session.add(message)
    await session.flush()
    return message


async def add_message_if_absent(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    work_chat_id: uuid.UUID,
    telegram_message_id: int,
    text: str,
    sent_at: datetime,
    is_outgoing: bool = False,
    has_media: bool = False,
    sender_contact_id: uuid.UUID | None = None,
    reply_to_telegram_message_id: int | None = None,
    is_forwarded: bool = False,
    forwarded_from: str | None = None,
) -> Message:
    """Insert unless ``(work_chat_id, telegram_message_id)`` already exists; return the stored row.

    Idempotent on that key: Telegram may deliver an update twice, and Telethon may echo the
    agent's own sends as outgoing events, so every writer of ``messages`` that can race uses this.
    The first writer wins; a later call never overwrites the stored row.
    """
    await session.execute(
        insert(Message)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            work_chat_id=work_chat_id,
            telegram_message_id=telegram_message_id,
            text=text,
            sent_at=sent_at,
            is_outgoing=is_outgoing,
            has_media=has_media,
            sender_contact_id=sender_contact_id,
            reply_to_telegram_message_id=reply_to_telegram_message_id,
            is_forwarded=is_forwarded,
            forwarded_from=forwarded_from,
        )
        .on_conflict_do_nothing(index_elements=[Message.work_chat_id, Message.telegram_message_id])
    )
    stored = await get_by_telegram_id(session, user_id, work_chat_id, telegram_message_id)
    if stored is None:  # the key exists under another user: impossible, work chats are per user
        raise LookupError("message key belongs to another user")
    return stored


async def get_message(
    session: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID
) -> Message | None:
    result = await session.execute(
        select(Message).where(Message.user_id == user_id, Message.id == message_id)
    )
    return result.scalar_one_or_none()


async def get_by_telegram_id(
    session: AsyncSession, user_id: uuid.UUID, work_chat_id: uuid.UUID, telegram_message_id: int
) -> Message | None:
    result = await session.execute(
        select(Message).where(
            Message.user_id == user_id,
            Message.work_chat_id == work_chat_id,
            Message.telegram_message_id == telegram_message_id,
        )
    )
    return result.scalar_one_or_none()


async def list_recent_in_chat(
    session: AsyncSession,
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    *,
    since: datetime,
    limit: int,
    before: datetime | None = None,
) -> list[Message]:
    """Most recent first (only those sent before ``before`` when given)."""
    query = select(Message).where(
        Message.user_id == user_id,
        Message.work_chat_id == work_chat_id,
        Message.sent_at >= since,
    )
    if before is not None:
        query = query.where(Message.sent_at < before)
    result = await session.execute(query.order_by(Message.sent_at.desc()).limit(limit))
    return list(result.scalars().all())


async def list_in_chat_between(
    session: AsyncSession,
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    *,
    start: datetime,
    end: datetime,
) -> list[Message]:
    """Messages with ``start <= sent_at <= end``, oldest first."""
    result = await session.execute(
        select(Message)
        .where(
            Message.user_id == user_id,
            Message.work_chat_id == work_chat_id,
            Message.sent_at >= start,
            Message.sent_at <= end,
        )
        .order_by(Message.sent_at, Message.telegram_message_id)
    )
    return list(result.scalars().all())


async def get_messages(
    session: AsyncSession, user_id: uuid.UUID, message_ids: list[uuid.UUID]
) -> list[Message]:
    if not message_ids:
        return []
    result = await session.execute(
        select(Message).where(Message.user_id == user_id, Message.id.in_(message_ids))
    )
    return list(result.scalars().all())


async def list_with_contact(
    session: AsyncSession,
    user_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    since: datetime,
    limit: int,
    exclude_chat_id: uuid.UUID | None = None,
) -> list[Message]:
    """Messages sent by ``contact_id``, most recent first (outside ``exclude_chat_id`` when
    given)."""
    query = select(Message).where(
        Message.user_id == user_id,
        Message.sender_contact_id == contact_id,
        Message.sent_at >= since,
    )
    if exclude_chat_id is not None:
        query = query.where(Message.work_chat_id != exclude_chat_id)
    result = await session.execute(query.order_by(Message.sent_at.desc()).limit(limit))
    return list(result.scalars().all())


async def count_in_chat(session: AsyncSession, user_id: uuid.UUID, work_chat_id: uuid.UUID) -> int:
    """All stored messages of the chat (no time bound: the count only moves when a message
    arrives or the retention purge deletes old ones)."""
    result = await session.execute(
        select(func.count())
        .select_from(Message)
        .where(Message.user_id == user_id, Message.work_chat_id == work_chat_id)
    )
    return int(result.scalar_one())


async def count_with_contact(
    session: AsyncSession,
    user_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    exclude_chat_id: uuid.UUID | None = None,
) -> int:
    """All stored messages sent by ``contact_id`` (outside ``exclude_chat_id`` when given)."""
    query = (
        select(func.count())
        .select_from(Message)
        .where(Message.user_id == user_id, Message.sender_contact_id == contact_id)
    )
    if exclude_chat_id is not None:
        query = query.where(Message.work_chat_id != exclude_chat_id)
    result = await session.execute(query)
    return int(result.scalar_one())
