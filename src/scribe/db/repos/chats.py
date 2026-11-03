"""``work_chats`` and ``contacts`` rows, always scoped by ``user_id``.

Chat titles and contact names are encrypted (issue #57), so lists are sorted in Python, never
with ``ORDER BY``. ``last_seen_at`` moves when a message of the chat or from the contact reaches
Scribe, at most once per :data:`LAST_SEEN_RESOLUTION` (one write a day, not one per message).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.clock import utcnow
from scribe.db.models import ChatKind, Contact, ContactKind, WorkChat, WorkChatOrigin

LAST_SEEN_RESOLUTION = timedelta(days=1)


def touch_last_seen(row: WorkChat | Contact, now: datetime | None = None) -> None:
    """Move ``row.last_seen_at`` to ``now`` unless it is already within a day of it."""
    now = now or utcnow()
    seen = row.last_seen_at
    if seen is None or seen < now - LAST_SEEN_RESOLUTION:
        row.last_seen_at = now


def _by_name(name: str, row_id: uuid.UUID) -> tuple[str, str, str]:
    return (name.casefold(), name, str(row_id))


async def get_work_chat(
    session: AsyncSession, user_id: uuid.UUID, work_chat_id: uuid.UUID
) -> WorkChat | None:
    result = await session.execute(
        select(WorkChat).where(WorkChat.user_id == user_id, WorkChat.id == work_chat_id)
    )
    return result.scalar_one_or_none()


async def get_work_chat_by_telegram_id(
    session: AsyncSession, user_id: uuid.UUID, telegram_chat_id: int
) -> WorkChat | None:
    result = await session.execute(
        select(WorkChat).where(
            WorkChat.user_id == user_id, WorkChat.telegram_chat_id == telegram_chat_id
        )
    )
    return result.scalar_one_or_none()


async def list_work_chats(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    enabled_only: bool = False,
    include_auto: bool = True,
) -> list[WorkChat]:
    """``include_auto=False`` leaves out chats picked up by work account mode (specs/008)."""
    query = select(WorkChat).where(WorkChat.user_id == user_id)
    if enabled_only:
        query = query.where(WorkChat.enabled.is_(True))
    if not include_auto:
        query = query.where(WorkChat.origin == WorkChatOrigin.picked)
    result = await session.execute(query)
    return sorted(result.scalars().all(), key=lambda chat: _by_name(chat.title, chat.id))


async def upsert_work_chat(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    telegram_chat_id: int,
    title: str,
    kind: ChatKind,
    **values: Any,
) -> WorkChat:
    chat = await get_work_chat_by_telegram_id(session, user_id, telegram_chat_id)
    if chat is None:
        chat = WorkChat(
            user_id=user_id, telegram_chat_id=telegram_chat_id, title=title, kind=kind, **values
        )
        session.add(chat)
    else:
        chat.title = title
        chat.kind = kind
        touch_last_seen(chat)
        for key, value in values.items():
            setattr(chat, key, value)
    await session.flush()
    return chat


@dataclass(frozen=True)
class NewWorkChat:
    """A chat to add as a work chat (specs/007): the picker snapshot of one Telegram dialog."""

    telegram_chat_id: int
    title: str
    kind: ChatKind


async def add_work_chats(
    session: AsyncSession, user_id: uuid.UUID, chats: Sequence[NewWorkChat]
) -> list[WorkChat]:
    """Insert ``chats`` as enabled work chats with the model defaults and return only the rows
    created. Never updates an existing row (unlike :func:`upsert_work_chat`): a chat that is
    already a work chat, enabled or disabled, keeps its row as it is, so a double Done or a
    stale picker adds nothing and never re-enables a disabled chat (research R4)."""
    if not chats:
        return []
    statement = (
        insert(WorkChat)
        .values(
            [
                {
                    "user_id": user_id,
                    "telegram_chat_id": chat.telegram_chat_id,
                    "title": chat.title,
                    "kind": chat.kind,
                    "enabled": True,
                }
                for chat in chats
            ]
        )
        .on_conflict_do_nothing(index_elements=[WorkChat.user_id, WorkChat.telegram_chat_id])
        .returning(WorkChat)
    )
    result = await session.execute(statement)
    return list(result.scalars().all())


async def create_auto_chat(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    telegram_chat_id: int,
    title: str,
    kind: ChatKind,
    enabled: bool,
    excluded: bool,
) -> tuple[WorkChat, bool]:
    """specs/008: insert a chat picked up by work account mode (``origin = 'auto'``) unless a
    row for it exists. Returns (the row, whether this call created it). Insert-only, so two
    concurrent first messages or a stale picker never change an existing row."""
    statement = (
        insert(WorkChat)
        .values(
            user_id=user_id,
            telegram_chat_id=telegram_chat_id,
            title=title,
            kind=kind,
            enabled=enabled,
            excluded=excluded,
            origin=WorkChatOrigin.auto,
        )
        .on_conflict_do_nothing(index_elements=[WorkChat.user_id, WorkChat.telegram_chat_id])
        .returning(WorkChat)
    )
    created = (await session.execute(statement)).scalar_one_or_none()
    if created is not None:
        return created, True
    existing = await get_work_chat_by_telegram_id(session, user_id, telegram_chat_id)
    if existing is None:  # deleted between the conflict and the read: nothing to return
        raise LookupError("work chat vanished during pick-up")
    return existing, False


def _excluded_clause() -> ColumnElement[bool]:
    """Excluded chats (specs/008 data-model.md): private ``auto`` chats the user excluded and
    private ``picked`` chats they disabled (spec FR-008)."""
    return and_(
        WorkChat.kind == ChatKind.private,
        or_(
            and_(WorkChat.origin == WorkChatOrigin.auto, WorkChat.excluded.is_(True)),
            and_(WorkChat.origin == WorkChatOrigin.picked, WorkChat.enabled.is_(False)),
        ),
    )


def _paused_clause() -> ColumnElement[bool]:
    """``auto`` chats paused because the mode is off (not excluded, not enabled)."""
    return and_(
        WorkChat.origin == WorkChatOrigin.auto,
        WorkChat.excluded.is_(False),
        WorkChat.enabled.is_(False),
    )


async def _count(session: AsyncSession, user_id: uuid.UUID, clause: ColumnElement[bool]) -> int:
    found = await session.scalar(
        select(func.count()).select_from(WorkChat).where(WorkChat.user_id == user_id, clause)
    )
    return int(found or 0)


async def count_excluded(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await _count(session, user_id, _excluded_clause())


async def count_paused(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await _count(session, user_id, _paused_clause())


async def count_auto(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Chats picked up by work account mode (any state)."""
    return await _count(session, user_id, WorkChat.origin == WorkChatOrigin.auto)


def is_excluded(chat: WorkChat) -> bool:
    """The Python twin of the excluded clause, for one loaded row."""
    if chat.kind != ChatKind.private:
        return False
    if chat.origin == WorkChatOrigin.auto:
        return chat.excluded
    return not chat.enabled


async def set_enabled(
    session: AsyncSession, user_id: uuid.UUID, work_chat_id: uuid.UUID, enabled: bool
) -> None:
    chat = await get_work_chat(session, user_id, work_chat_id)
    if chat is not None:
        chat.enabled = enabled
        await session.flush()


# --- contacts -----------------------------------------------------------------------------------


async def get_contact(
    session: AsyncSession, user_id: uuid.UUID, contact_id: uuid.UUID
) -> Contact | None:
    result = await session.execute(
        select(Contact).where(Contact.user_id == user_id, Contact.id == contact_id)
    )
    return result.scalar_one_or_none()


async def get_contact_by_peer(
    session: AsyncSession, user_id: uuid.UUID, telegram_peer_id: int
) -> Contact | None:
    result = await session.execute(
        select(Contact).where(
            Contact.user_id == user_id, Contact.telegram_peer_id == telegram_peer_id
        )
    )
    return result.scalar_one_or_none()


async def upsert_contact(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    telegram_peer_id: int,
    kind: ContactKind,
    display_name: str,
    now: datetime | None = None,
) -> Contact:
    contact = await get_contact_by_peer(session, user_id, telegram_peer_id)
    if contact is None:
        contact = Contact(
            user_id=user_id,
            telegram_peer_id=telegram_peer_id,
            kind=kind,
            display_name=display_name,
            last_seen_at=now or utcnow(),
        )
        session.add(contact)
    else:
        if display_name and contact.display_name != display_name:
            contact.display_name = display_name
        touch_last_seen(contact, now)
    await session.flush()
    return contact


async def list_contacts(session: AsyncSession, user_id: uuid.UUID) -> list[Contact]:
    result = await session.execute(select(Contact).where(Contact.user_id == user_id))
    return sorted(
        result.scalars().all(), key=lambda contact: _by_name(contact.display_name, contact.id)
    )
