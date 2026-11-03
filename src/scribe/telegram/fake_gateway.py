"""In-memory MessagingGateway for tests: records calls and lets tests inject events."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from scribe.telegram.gateway import (
    Dialog,
    FoundMessage,
    GatewayError,
    IncomingMessage,
    NewMessageHandler,
    OutgoingEvent,
    OutgoingHandler,
    ReadEvent,
    ReadHandler,
    UnreadMarkClearedEvent,
    UnreadMarkClearedHandler,
)


@dataclass(frozen=True)
class MarkReadCall:
    user_id: uuid.UUID
    chat_id: int
    up_to_message_id: int


@dataclass(frozen=True)
class SendTextCall:
    user_id: uuid.UUID
    chat_id: int
    text: str
    reply_to: int | None
    message_id: int
    random_id: int = 0


@dataclass
class FakeMessagingGateway:
    dialogs: dict[uuid.UUID, list[Dialog]] = field(default_factory=dict)
    own_history: dict[tuple[uuid.UUID, int], list[IncomingMessage]] = field(default_factory=dict)
    started: set[uuid.UUID] = field(default_factory=set)
    logged_out: list[uuid.UUID] = field(default_factory=list)
    mark_read_calls: list[MarkReadCall] = field(default_factory=list)
    send_text_calls: list[SendTextCall] = field(default_factory=list)
    fail_send_with: Exception | None = None
    # #54: delivers the message (recorded, ``random_id`` remembered), then raises this once, as
    # Telegram does when the RPC times out after delivery.
    fail_send_after_delivery_with: Exception | None = None
    # Every send attempt's random_id, including deduplicated and failed ones.
    send_attempt_random_ids: list[int] = field(default_factory=list)
    # random_id -> delivered message id: a repeated random_id is not delivered again (MTProto).
    _delivered: dict[tuple[uuid.UUID, int, int], int] = field(default_factory=dict)
    fail_mark_read_with: Exception | None = None
    fail_list_dialogs: Exception | None = None  # 007: list_dialogs raises it when set
    # Search over earlier conversations (specs/004): chat id -> messages in that chat.
    search_index: dict[int, list[FoundMessage]] = field(default_factory=dict)
    usernames: dict[int, str] = field(default_factory=dict)
    fail_search: Exception | None = None
    search_delay_s: float = 0.0
    search_calls: list[tuple[uuid.UUID, int, str]] = field(default_factory=list)
    get_calls: list[tuple[uuid.UUID, int, tuple[int, ...]]] = field(default_factory=list)
    # Unread marks (specs/009).
    mark_unread_calls: list[tuple[uuid.UUID, int]] = field(default_factory=list)
    fail_mark_unread_with: Exception | None = None
    mark_unread_delay_s: float = 0.0  # awaited before recording (timeout tests)
    # Optional hook awaited inside ``mark_unread`` before it records the call.
    on_mark_unread: Callable[[uuid.UUID, int], Awaitable[None]] | None = None
    # ("mark_read" | "send_text" | "mark_unread", chat_id) in call order.
    call_order: list[tuple[str, int]] = field(default_factory=list)
    # Optional hook awaited inside ``mark_read`` after it records the call (e.g. to emit an
    # echo ``ReadEvent`` of the agent's own read).
    on_mark_read: Callable[[MarkReadCall], Awaitable[None]] | None = None
    _new_message_handlers: list[NewMessageHandler] = field(default_factory=list)
    _read_handlers: list[ReadHandler] = field(default_factory=list)
    _outgoing_handlers: list[OutgoingHandler] = field(default_factory=list)
    _unread_cleared_handlers: list[UnreadMarkClearedHandler] = field(default_factory=list)
    _message_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(900_000))

    # --- MessagingGateway ------------------------------------------------------------------------

    async def start(self, user_id: uuid.UUID) -> None:
        self.started.add(user_id)

    async def stop(self, user_id: uuid.UUID) -> None:
        self.started.discard(user_id)

    async def log_out(self, user_id: uuid.UUID) -> None:
        self.started.discard(user_id)
        self.logged_out.append(user_id)

    async def list_dialogs(self, user_id: uuid.UUID) -> list[Dialog]:
        if self.fail_list_dialogs is not None:
            raise self.fail_list_dialogs
        return list(self.dialogs.get(user_id, []))

    async def fetch_own_history(
        self, user_id: uuid.UUID, chat_id: int, since: datetime
    ) -> AsyncIterator[IncomingMessage]:
        for message in self.own_history.get((user_id, chat_id), []):
            if message.is_outgoing and message.sent_at >= since:
                yield message

    async def mark_read(self, user_id: uuid.UUID, chat_id: int, up_to_message_id: int) -> None:
        if self.fail_mark_read_with is not None:
            raise self.fail_mark_read_with
        call = MarkReadCall(user_id, chat_id, up_to_message_id)
        self.mark_read_calls.append(call)
        self.call_order.append(("mark_read", chat_id))
        if self.on_mark_read is not None:
            await self.on_mark_read(call)

    async def send_text(
        self, user_id: uuid.UUID, chat_id: int, text: str, reply_to: int | None, *, random_id: int
    ) -> int:
        self.send_attempt_random_ids.append(random_id)
        if self.fail_send_with is not None:
            raise self.fail_send_with
        key = (user_id, chat_id, random_id)
        if key in self._delivered:  # Telegram deduplicates by random_id
            return self._delivered[key]
        message_id = next(self._message_ids)
        self._delivered[key] = message_id
        self.send_text_calls.append(
            SendTextCall(user_id, chat_id, text, reply_to, message_id, random_id)
        )
        self.call_order.append(("send_text", chat_id))
        if self.fail_send_after_delivery_with is not None:
            exc, self.fail_send_after_delivery_with = self.fail_send_after_delivery_with, None
            raise exc
        return message_id

    async def mark_unread(self, user_id: uuid.UUID, chat_id: int) -> None:
        if self.mark_unread_delay_s:
            await asyncio.sleep(self.mark_unread_delay_s)
        if self.fail_mark_unread_with is not None:
            raise self.fail_mark_unread_with
        if self.on_mark_unread is not None:
            await self.on_mark_unread(user_id, chat_id)
        self.mark_unread_calls.append((user_id, chat_id))
        self.call_order.append(("mark_unread", chat_id))

    async def search_chat(
        self, user_id: uuid.UUID, chat_id: int, query: str, *, limit: int = 20
    ) -> list[FoundMessage]:
        self.search_calls.append((user_id, chat_id, query))
        if self.search_delay_s:
            await asyncio.sleep(self.search_delay_s)
        if self.fail_search is not None:
            raise self.fail_search
        needle = query.casefold()
        hits = [m for m in self.search_index.get(chat_id, []) if needle in m.text.casefold()]
        return sorted(hits, key=lambda m: m.sent_at, reverse=True)[:limit]

    async def get_messages(
        self, user_id: uuid.UUID, chat_id: int, message_ids: Sequence[int]
    ) -> list[FoundMessage]:
        self.get_calls.append((user_id, chat_id, tuple(message_ids)))
        if self.fail_search is not None:
            raise self.fail_search
        wanted = set(message_ids)
        return [m for m in self.search_index.get(chat_id, []) if m.message_id in wanted]

    async def get_usernames(self, user_id: uuid.UUID, peer_ids: Sequence[int]) -> dict[int, str]:
        return {p: self.usernames[p] for p in peer_ids if p in self.usernames}

    def on_new_message(self, handler: NewMessageHandler) -> None:
        self._new_message_handlers.append(handler)

    def on_read_elsewhere(self, handler: ReadHandler) -> None:
        self._read_handlers.append(handler)

    def on_outgoing(self, handler: OutgoingHandler) -> None:
        self._outgoing_handlers.append(handler)

    def on_unread_mark_cleared(self, handler: UnreadMarkClearedHandler) -> None:
        self._unread_cleared_handlers.append(handler)

    # --- test helpers ----------------------------------------------------------------------------

    async def emit_new_message(self, message: IncomingMessage) -> None:
        for handler in self._new_message_handlers:
            await handler(message)

    async def emit_read_elsewhere(self, event: ReadEvent) -> None:
        for handler in self._read_handlers:
            await handler(event)

    async def emit_outgoing(self, event: OutgoingEvent) -> None:
        for handler in self._outgoing_handlers:
            await handler(event)

    async def emit_unread_mark_cleared(self, event: UnreadMarkClearedEvent) -> None:
        for handler in self._unread_cleared_handlers:
            await handler(event)

    def reset_calls(self) -> None:
        self.mark_read_calls.clear()
        self.send_text_calls.clear()
        self.send_attempt_random_ids.clear()
        self.search_calls.clear()
        self.get_calls.clear()
        self.mark_unread_calls.clear()
        self.call_order.clear()


__all__ = ["FakeMessagingGateway", "GatewayError", "MarkReadCall", "SendTextCall"]
