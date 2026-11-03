"""Per-chat ordering of gateway events (US2, FR-015).

Telethon runs update handlers concurrently. Incoming messages (``IntakeRouter``) and outgoing
messages (``ChatEventHandler.handle_outgoing``) of the same (user, chat) are serialised on one
:class:`asyncio.Lock` from a shared :class:`ChatLocks` registry, so a manual reply and a
message that arrives together are handled one after the other, never interleaved. Both
handlers default to the process-wide :data:`CHAT_LOCKS`, so they share it without extra wiring.

The lock is only ever taken *before* any database row lock and never while another chat lock
is held, so it cannot take part in a deadlock with the draft -> turn row-lock order.

The registry holds its locks weakly (#60): a lock lives while a handler holds it or waits for it
(each keeps a reference for the length of its ``async with``) and is dropped once the last one
lets go, so chats that only ever pass through (non-work chats, channels) do not accumulate
locks in a long-running process. Two handlers of one chat that overlap always get the same
lock; one that comes after the lock was dropped gets a fresh one, which orders it just as well.

The locks are process-local: only one Scribe instance may run against a database
(``scribe.instance_lock``).
"""

from __future__ import annotations

import asyncio
import uuid
import weakref


class ChatLocks:
    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[tuple[uuid.UUID, int], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def for_chat(self, user_id: uuid.UUID, telegram_chat_id: int) -> asyncio.Lock:
        """The chat's lock; keep the returned object referenced while holding or awaiting it."""
        key = (user_id, telegram_chat_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def __len__(self) -> int:
        return len(self._locks)


CHAT_LOCKS = ChatLocks()
