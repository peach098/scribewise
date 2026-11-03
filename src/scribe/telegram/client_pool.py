"""One Telethon client per user with an active session; the production MessagingGateway.

- ``start_all()`` at boot starts every user whose ``telegram_sessions.status = 'active'``;
  ``start``/``stop`` are called on login/logout. A user whose access is suspended
  (``users.access_suspended_at``, specs/005) is never started.
- A revoked session (``SessionRevoked``: AuthKeyUnregistered, logged out, invalid string) at
  start or during any operation marks the row ``revoked``, stops the client and notifies the
  user through the control bot (with a Reconnect button). Other start failures mark it
  ``error``; transient network errors only log, so the next start attempt can succeed.
- Events from all clients fan out to the handlers registered with ``on_*``, including
  ``on_unread_mark_cleared`` (specs/009: the user cleared a chat's unread mark elsewhere).
- ``mark_unread`` (specs/009) is delegated like the other operations; a revoked session found
  by it is handled the same way.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Protocol

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.crypto import DecryptionError
from scribe.db.models import SessionStatus
from scribe.db.repos import users as users_repo
from scribe.i18n import Msg
from scribe.logging import get_logger
from scribe.telegram.control_bot.render import reconnect_keyboard
from scribe.telegram.gateway import (
    Dialog,
    FoundMessage,
    GatewayNotStarted,
    IncomingMessage,
    NewMessageHandler,
    OutgoingEvent,
    OutgoingHandler,
    ReadEvent,
    ReadHandler,
    SessionRevoked,
    UnreadMarkClearedEvent,
    UnreadMarkClearedHandler,
)
from scribe.telegram.user_client import (
    DEFAULT_APP_VERSION,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_SYSTEM_VERSION,
    UserClient,
)

log = get_logger("scribe.telegram")

# A keyboard built in the recipient's language (``users.language``).
KeyboardFactory = Callable[[str], InlineKeyboardMarkup]
# (user_id, message, keyboard): a message to the user's control chat, rendered by the notifier
# in the user's stored language (there is no incoming update to take it from).
Notifier = Callable[[uuid.UUID, Msg, KeyboardFactory | None], Awaitable[None]]

REVOKED_NOTICE = Msg("notice.session_revoked")
ERROR_NOTICE = Msg("notice.session_error")
TRANSIENT_ERRORS = (OSError, ConnectionError, asyncio.TimeoutError)


class UserClientLike(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def log_out(self) -> None: ...
    async def list_dialogs(self) -> list[Dialog]: ...
    def fetch_own_history(
        self, chat_id: int, since: datetime
    ) -> AsyncIterator[IncomingMessage]: ...
    async def mark_read(self, chat_id: int, up_to_message_id: int) -> None: ...
    async def send_text(
        self, chat_id: int, text: str, reply_to: int | None, *, random_id: int
    ) -> int: ...
    async def mark_unread(self, chat_id: int) -> None: ...
    async def search_chat(
        self, chat_id: int, query: str, *, limit: int = 20
    ) -> list[FoundMessage]: ...
    async def get_messages(
        self, chat_id: int, message_ids: Sequence[int]
    ) -> list[FoundMessage]: ...
    async def get_usernames(self, peer_ids: Sequence[int]) -> dict[int, str]: ...


UserClientFactory = Callable[..., UserClientLike]


class ClientPool:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        api_id: int,
        api_hash: str,
        notifier: Notifier | None = None,
        user_client_factory: UserClientFactory = UserClient,
        device_model: str = DEFAULT_DEVICE_MODEL,
        system_version: str = DEFAULT_SYSTEM_VERSION,
        app_version: str = DEFAULT_APP_VERSION,
    ) -> None:
        self._session_factory = session_factory
        self._api_id = api_id
        self._api_hash = api_hash
        self._notifier = notifier
        self._factory = user_client_factory
        self._device = {
            "device_model": device_model,
            "system_version": system_version,
            "app_version": app_version,
        }
        self._clients: dict[uuid.UUID, UserClientLike] = {}
        self._lock = asyncio.Lock()
        self._new_message_handlers: list[NewMessageHandler] = []
        self._read_handlers: list[ReadHandler] = []
        self._outgoing_handlers: list[OutgoingHandler] = []
        self._unread_cleared_handlers: list[UnreadMarkClearedHandler] = []

    # --- lifecycle ------------------------------------------------------------------------------

    @property
    def running_user_ids(self) -> set[uuid.UUID]:
        return set(self._clients)

    def is_running(self, user_id: uuid.UUID) -> bool:
        return user_id in self._clients

    async def start_all(self) -> int:
        async with self._session_factory() as session:
            user_ids = await users_repo.list_user_ids_with_session_status(
                session, SessionStatus.active
            )
        for user_id in user_ids:
            await self.start(user_id)
        log.info("telegram_pool_started", users=len(user_ids), running=len(self._clients))
        return len(self._clients)

    async def start(self, user_id: uuid.UUID) -> None:
        async with self._lock:
            if user_id in self._clients:
                return
            try:
                async with self._session_factory() as session:
                    user = await users_repo.get_user(session, user_id)
                    row = await users_repo.get_telegram_session(session, user_id)
                    session_string = row.session_string if row is not None else None
                    status = row.status if row is not None else None
            except DecryptionError as exc:
                # A session string that cannot be read (unknown key, moved from another user's
                # row, or unsealed in strict mode, #59): this user only, the others still start.
                await self._mark(user_id, SessionStatus.error, type(exc).__name__, ERROR_NOTICE)
                return
            if user is not None and user.access_suspended_at is not None:
                log.info("client_start_skipped_suspended", user_id=str(user_id))
                return
            if session_string is None or status != SessionStatus.active:
                log.info("telegram_client_not_started", user_id=str(user_id), status=status)
                return
            client = self._new_client(user_id, session_string)
            try:
                await client.start()
            except SessionRevoked as exc:
                await self._mark(user_id, SessionStatus.revoked, str(exc), REVOKED_NOTICE)
                return
            except TRANSIENT_ERRORS as exc:
                log.warning(
                    "telegram_client_start_failed", user_id=str(user_id), error=type(exc).__name__
                )
                return
            except Exception as exc:
                await self._mark(user_id, SessionStatus.error, type(exc).__name__, ERROR_NOTICE)
                return
            self._clients[user_id] = client

    async def stop(self, user_id: uuid.UUID) -> None:
        async with self._lock:
            client = self._clients.pop(user_id, None)
        if client is not None:
            await client.stop()

    async def log_out(self, user_id: uuid.UUID) -> None:
        """Log the user's session out on Telegram's side and drop their client.

        Without a running client, a temporary one is built from the stored session just to
        log out. An already revoked session counts as success; the session row and the user
        are left to the caller (no ``_mark``, no notice). Transient network errors propagate.
        """
        async with self._lock:
            client = self._clients.pop(user_id, None)
            if client is None:
                async with self._session_factory() as session:
                    row = await users_repo.get_telegram_session(session, user_id)
                    session_string = row.session_string if row is not None else None
                if session_string is None:
                    log.info("telegram_log_out_no_session", user_id=str(user_id))
                    return
                client = self._new_client(user_id, session_string)
            try:
                await client.log_out()
            except SessionRevoked:
                log.info("telegram_log_out_already_revoked", user_id=str(user_id))

    async def stop_all(self) -> None:
        for user_id in list(self._clients):
            try:
                await self.stop(user_id)
            except Exception:
                log.exception("telegram_client_stop_failed", user_id=str(user_id))

    # --- MessagingGateway -----------------------------------------------------------------------

    async def list_dialogs(self, user_id: uuid.UUID) -> list[Dialog]:
        return await self._call(user_id, lambda c: c.list_dialogs())

    async def fetch_own_history(
        self, user_id: uuid.UUID, chat_id: int, since: datetime
    ) -> AsyncIterator[IncomingMessage]:
        client = self._client(user_id)
        try:
            async for message in client.fetch_own_history(chat_id, since):
                yield message
        except SessionRevoked as exc:
            await self._revoked_during_operation(user_id, exc)
            raise

    async def mark_read(self, user_id: uuid.UUID, chat_id: int, up_to_message_id: int) -> None:
        await self._call(user_id, lambda c: c.mark_read(chat_id, up_to_message_id))

    async def send_text(
        self, user_id: uuid.UUID, chat_id: int, text: str, reply_to: int | None, *, random_id: int
    ) -> int:
        return await self._call(
            user_id, lambda c: c.send_text(chat_id, text, reply_to, random_id=random_id)
        )

    async def mark_unread(self, user_id: uuid.UUID, chat_id: int) -> None:
        await self._call(user_id, lambda c: c.mark_unread(chat_id))

    async def search_chat(
        self, user_id: uuid.UUID, chat_id: int, query: str, *, limit: int = 20
    ) -> list[FoundMessage]:
        return await self._call(user_id, lambda c: c.search_chat(chat_id, query, limit=limit))

    async def get_messages(
        self, user_id: uuid.UUID, chat_id: int, message_ids: Sequence[int]
    ) -> list[FoundMessage]:
        return await self._call(user_id, lambda c: c.get_messages(chat_id, message_ids))

    async def get_usernames(self, user_id: uuid.UUID, peer_ids: Sequence[int]) -> dict[int, str]:
        return await self._call(user_id, lambda c: c.get_usernames(peer_ids))

    def on_new_message(self, handler: NewMessageHandler) -> None:
        self._new_message_handlers.append(handler)

    def on_read_elsewhere(self, handler: ReadHandler) -> None:
        self._read_handlers.append(handler)

    def on_outgoing(self, handler: OutgoingHandler) -> None:
        self._outgoing_handlers.append(handler)

    def on_unread_mark_cleared(self, handler: UnreadMarkClearedHandler) -> None:
        self._unread_cleared_handlers.append(handler)

    # --- internals ------------------------------------------------------------------------------

    def _new_client(self, user_id: uuid.UUID, session_string: str) -> UserClientLike:
        return self._factory(
            user_id,
            session_string,
            api_id=self._api_id,
            api_hash=self._api_hash,
            on_new_message=self._dispatch_new_message,
            on_outgoing=self._dispatch_outgoing,
            on_read_elsewhere=self._dispatch_read,
            on_unread_mark_cleared=self._dispatch_unread_cleared,
            **self._device,
        )

    def _client(self, user_id: uuid.UUID) -> UserClientLike:
        client = self._clients.get(user_id)
        if client is None:
            raise GatewayNotStarted(f"no Telegram client running for user {user_id}")
        return client

    async def _call[T](
        self, user_id: uuid.UUID, operation: Callable[[UserClientLike], Awaitable[T]]
    ) -> T:
        client = self._client(user_id)
        try:
            return await operation(client)
        except SessionRevoked as exc:
            await self._revoked_during_operation(user_id, exc)
            raise

    async def _revoked_during_operation(self, user_id: uuid.UUID, exc: SessionRevoked) -> None:
        async with self._lock:
            client = self._clients.pop(user_id, None)
        if client is not None:
            try:
                await client.stop()
            except Exception:
                log.exception("telegram_client_stop_failed", user_id=str(user_id))
        await self._mark(user_id, SessionStatus.revoked, str(exc), REVOKED_NOTICE)

    async def _mark(
        self, user_id: uuid.UUID, status: SessionStatus, reason: str, notice: Msg
    ) -> None:
        log.warning("telegram_session_unusable", user_id=str(user_id), status=status, reason=reason)
        async with self._session_factory() as session:
            await users_repo.set_telegram_session_status(session, user_id, status, reason)
            await session.commit()
        if self._notifier is not None:
            try:
                await self._notifier(user_id, notice, reconnect_keyboard)
            except Exception:
                log.exception("control_bot_notify_failed", user_id=str(user_id))

    async def _fan_out(self, handlers: list[Any], event: Any) -> None:
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.exception("gateway_handler_failed", user_id=str(event.user_id))

    async def _dispatch_new_message(self, message: IncomingMessage) -> None:
        await self._fan_out(self._new_message_handlers, message)

    async def _dispatch_outgoing(self, event: OutgoingEvent) -> None:
        await self._fan_out(self._outgoing_handlers, event)

    async def _dispatch_read(self, event: ReadEvent) -> None:
        await self._fan_out(self._read_handlers, event)

    async def _dispatch_unread_cleared(self, event: UnreadMarkClearedEvent) -> None:
        await self._fan_out(self._unread_cleared_handlers, event)
