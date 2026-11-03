"""UnreadMarker: mark work chats unread after the agent reads or sends there (specs/009).

After the agent reads new messages in a work chat, or an approved reply is sent, the chat is
marked unread in the user's Telegram (``MessagingGateway.mark_unread``), so the chat list still
shows where something happened. One number per work chat, ``work_chats.unread_mark_floor``
(the newest Telegram message id the chat was marked for or the user is known to have seen),
makes the mark happen at most once per new message or reply and never for something the user
has already seen (research U4):

- **Decide before the agent's read, commit after.** ``prepare(n)`` checks ``floor < n`` without
  writing; the caller reads; ``commit(ticket)`` moves the floor to ``n`` only if it is still
  ``<= n`` and then marks. If Telegram echoes the agent's own read back as a ``ReadEvent``, the
  echo moves the floor to ``n`` at most, which still passes; a real read by the user that went
  further (or happened before the agent's turn) blocks the mark. Retries, restarts and
  re-reads of the same message find ``floor == n`` in ``prepare`` and mark nothing.
- ``after_send(s)``: the same with a strict ``floor < s``.
- ``seen(m)``: seen signals from the user's own sessions (read elsewhere, mark cleared, manual
  reply) raise the floor to ``m``, whatever the setting is, so turning the setting on later can
  never mark messages the user has already seen.

Guarantees (contracts/internal-interfaces.md): no public method raises (every failure is logged
as ``unread_mark_failed`` and swallowed, FR-004); the floor update is its own short transaction
committed **before** ``gateway.mark_unread`` is awaited; the mark is bounded by ``timeout_s``
and never retried; this class never reads, drafts or sends and never calls ``mark_read``
(FR-008). Logs carry ids, causes and reasons only, never message text or chat titles (FR-012).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.db.models import Message, User, WorkChat
from scribe.logging import get_logger
from scribe.telegram.gateway import MessagingGateway, UnreadMarkClearedEvent

log = get_logger("scribe.telegram.unread_mark")

DEFAULT_TIMEOUT_S = 5.0
Cause = Literal["read", "send"]
SeenSource = Literal["read_elsewhere", "mark_cleared", "manual_reply"]


@dataclass(frozen=True)
class MarkTicket:
    """``prepare``'s decision to mark after a read up to ``up_to``; never stored."""

    user_id: uuid.UUID
    work_chat_id: uuid.UUID
    telegram_chat_id: int
    up_to: int


class UnreadMarker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: MessagingGateway,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._timeout_s = timeout_s

    # --- agent reads ----------------------------------------------------------------------------

    async def prepare(
        self, user_id: uuid.UUID, telegram_chat_id: int, up_to: int
    ) -> MarkTicket | None:
        """Before the agent reads up to ``up_to``: a ticket if the setting is on, the chat is an
        enabled work chat and its floor is NULL or < up_to. Reads only; never raises."""
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(WorkChat.id, WorkChat.enabled, WorkChat.unread_mark_floor)
                        .add_columns(User.mark_unread)
                        .join(User, User.id == WorkChat.user_id)
                        .where(
                            WorkChat.user_id == user_id,
                            WorkChat.telegram_chat_id == telegram_chat_id,
                        )
                    )
                ).one_or_none()
            if row is None or not row.enabled:
                _skipped(user_id, row.id if row is not None else None, "read", "not_work_chat")
                return None
            if not row.mark_unread:
                _skipped(user_id, row.id, "read", "disabled")
                return None
            if row.unread_mark_floor is not None and row.unread_mark_floor >= up_to:
                _skipped(user_id, row.id, "read", "seen")
                return None
            return MarkTicket(user_id, row.id, telegram_chat_id, up_to)
        except Exception as exc:
            _failed(user_id, None, "read", exc)
            return None

    async def commit(self, ticket: MarkTicket | None) -> None:
        """After a successful read: move the floor to ``up_to`` if it is NULL or <= up_to, then
        mark the chat. No-op for None. Never raises."""
        if ticket is None:
            return
        try:
            async with self._session_factory() as session:
                moved = await session.scalar(
                    update(WorkChat)
                    .where(
                        WorkChat.id == ticket.work_chat_id,
                        WorkChat.user_id == ticket.user_id,
                        (WorkChat.unread_mark_floor.is_(None))
                        | (WorkChat.unread_mark_floor <= ticket.up_to),
                    )
                    .values(unread_mark_floor=ticket.up_to)
                    .returning(WorkChat.id)
                )
                await session.commit()
        except Exception as exc:
            _failed(ticket.user_id, ticket.work_chat_id, "read", exc)
            return
        if moved is None:
            _skipped(ticket.user_id, ticket.work_chat_id, "read", "seen")
            return
        await self._mark(
            ticket.user_id, ticket.work_chat_id, ticket.telegram_chat_id, "read", ticket.up_to
        )

    # --- approved sends -------------------------------------------------------------------------

    async def after_send(
        self, user_id: uuid.UUID, work_chat_id: uuid.UUID, sent_message_id: int
    ) -> None:
        """After an approved reply was sent and committed: if the setting is on and the floor is
        NULL or < sent_message_id, move it and mark the chat. Never raises."""
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(WorkChat.telegram_chat_id, User.mark_unread)
                        .join(User, User.id == WorkChat.user_id)
                        .where(WorkChat.id == work_chat_id, WorkChat.user_id == user_id)
                    )
                ).one_or_none()
                if row is None:
                    _skipped(user_id, work_chat_id, "send", "not_work_chat")
                    return
                if not row.mark_unread:
                    _skipped(user_id, work_chat_id, "send", "disabled")
                    return
                moved = await session.scalar(
                    update(WorkChat)
                    .where(
                        WorkChat.id == work_chat_id,
                        WorkChat.user_id == user_id,
                        (WorkChat.unread_mark_floor.is_(None))
                        | (WorkChat.unread_mark_floor < sent_message_id),
                    )
                    .values(unread_mark_floor=sent_message_id)
                    .returning(WorkChat.id)
                )
                await session.commit()
        except Exception as exc:
            _failed(user_id, work_chat_id, "send", exc)
            return
        if moved is None:
            _skipped(user_id, work_chat_id, "send", "not_newer")
            return
        await self._mark(user_id, work_chat_id, int(row.telegram_chat_id), "send", sent_message_id)

    # --- seen signals ---------------------------------------------------------------------------

    async def seen(
        self,
        user_id: uuid.UUID,
        telegram_chat_id: int,
        up_to: int | None,
        *,
        source: SeenSource,
    ) -> None:
        """A seen signal from the user's own session: floor = max(floor, up_to). ``up_to=None``
        means "everything stored so far" (newest ``messages.telegram_message_id`` of the chat).
        Enabled work chats only; applies whatever the setting is. Never raises."""
        work_chat_id: uuid.UUID | None = None
        try:
            async with self._session_factory() as session:
                chat = (
                    await session.execute(
                        select(WorkChat.id, WorkChat.enabled).where(
                            WorkChat.user_id == user_id,
                            WorkChat.telegram_chat_id == telegram_chat_id,
                        )
                    )
                ).one_or_none()
                if chat is None or not chat.enabled:
                    return
                work_chat_id = chat.id
                if up_to is None:
                    up_to = await session.scalar(
                        select(func.max(Message.telegram_message_id)).where(
                            Message.user_id == user_id, Message.work_chat_id == chat.id
                        )
                    )
                if up_to is None or up_to < 1:
                    return
                await session.execute(
                    update(WorkChat)
                    .where(WorkChat.id == chat.id, WorkChat.user_id == user_id)
                    .values(
                        unread_mark_floor=func.greatest(
                            func.coalesce(WorkChat.unread_mark_floor, 0), up_to
                        )
                    )
                )
                await session.commit()
        except Exception as exc:
            log.warning(
                "unread_mark_failed",
                user_id=str(user_id),
                work_chat_id=str(work_chat_id) if work_chat_id else None,
                cause="seen",
                source=source,
                error=type(exc).__name__,
            )
            return
        log.info(
            "unread_mark_seen",
            user_id=str(user_id),
            work_chat_id=str(work_chat_id),
            source=source,
            up_to=up_to,
        )

    async def handle_unread_mark_cleared(self, event: UnreadMarkClearedEvent) -> None:
        """Gateway handler: the user cleared the mark elsewhere, so everything stored is seen."""
        await self.seen(event.user_id, event.chat_id, None, source="mark_cleared")

    # --- internals ------------------------------------------------------------------------------

    async def _mark(
        self,
        user_id: uuid.UUID,
        work_chat_id: uuid.UUID,
        telegram_chat_id: int,
        cause: Cause,
        up_to: int,
    ) -> None:
        try:
            await asyncio.wait_for(
                self._gateway.mark_unread(user_id, telegram_chat_id), self._timeout_s
            )
        except Exception as exc:
            _failed(user_id, work_chat_id, cause, exc)
            return
        log.info(
            "unread_mark_set",
            user_id=str(user_id),
            work_chat_id=str(work_chat_id),
            cause=cause,
            up_to=up_to,
        )


def _skipped(user_id: uuid.UUID, work_chat_id: uuid.UUID | None, cause: Cause, reason: str) -> None:
    log.info(
        "unread_mark_skipped",
        user_id=str(user_id),
        work_chat_id=str(work_chat_id) if work_chat_id else None,
        cause=cause,
        reason=reason,
    )


def _failed(
    user_id: uuid.UUID, work_chat_id: uuid.UUID | None, cause: Cause, exc: BaseException
) -> None:
    log.warning(
        "unread_mark_failed",
        user_id=str(user_id),
        work_chat_id=str(work_chat_id) if work_chat_id else None,
        cause=cause,
        error=type(exc).__name__,
    )


__all__ = ["MarkTicket", "UnreadMarker"]
