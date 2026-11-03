"""Telethon MTProto client for one user's own Telegram account (research R1/R3).

- Receiving updates never acknowledges reads: nothing here calls ``send_read_acknowledge``
  except :meth:`UserClient.mark_read`, which the scheduler invokes when a turn becomes due.
- ``NewMessage`` incoming -> ``on_new_message``; ``out=True`` -> ``on_outgoing``;
  ``UpdateReadHistoryInbox`` / ``UpdateReadChannelInbox`` -> ``on_read_elsewhere``.
- Flood waits up to ``max_flood_sleep_s`` (60 s) are slept through; longer ones raise
  ``GatewayBusy`` (logged) so the caller can reschedule.
- An unauthorised session (logged out / revoked in "Active sessions") raises
  ``SessionRevoked``.
- Search over earlier conversations (specs/004, research S3): ``search_chat`` /
  ``get_messages`` / ``get_usernames`` send raw requests. Telethon cannot skip its flood sleep
  per request (1.45 ignores ``flood_sleep_threshold`` on ``client(...)``), so a request type
  Telegram already told us to wait for raises ``GatewayBusy`` before it is sent, a wait longer
  than ``max_flood_sleep_s`` raises it after, and a first short wait is slept by Telethon until
  the caller's deadline (``EarlierSearch``, 6 s) cancels it.
- Unread marks (specs/009-mark-chats-unread): ``mark_unread`` sends one
  ``messages.markDialogUnread(unread=True)`` through the same no-sleep path (``GatewayBusy`` at
  once, no retry); it never touches read state. ``UpdateDialogUnreadMark`` with
  ``unread=False`` on a plain dialog peer (the user opened the chat or used "Mark as read" on
  another device) -> ``on_unread_mark_cleared``; ``unread=True`` (our own mark or the user's)
  is ignored.

Message text is passed to handlers but never logged.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import (
    AuthKeyDuplicatedError,
    FloodWaitError,
    RandomIdDuplicateError,
    UnauthorizedError,
)
from telethon.sessions import StringSession

from scribe.logging import get_logger
from scribe.telegram.gateway import (
    AlreadyDelivered,
    Dialog,
    DialogKind,
    FoundMessage,
    GatewayBusy,
    GatewayError,
    IncomingMessage,
    NewMessageHandler,
    OutgoingEvent,
    OutgoingHandler,
    PeerKind,
    ReadEvent,
    ReadHandler,
    SessionRevoked,
    UnreadMarkClearedEvent,
    UnreadMarkClearedHandler,
)

log = get_logger("scribe.telegram")

MAX_FLOOD_SLEEP_S = 60
SERVICE_USER_ID = 777000  # Telegram's notifications account (login codes)
# Shown in Telegram's "Active sessions" (research Q7); the same defaults as Settings.
DEFAULT_DEVICE_MODEL = "Scribe Agent"
DEFAULT_SYSTEM_VERSION = "server"
DEFAULT_APP_VERSION = importlib.metadata.version("scribe")
ClientFactory = Callable[..., Any]  # builds a TelegramClient-compatible object
Sleep = Callable[[float], Awaitable[None]]
# Recent messages searched for a send Telegram reports as already delivered (#54).
DELIVERED_LOOKUP_LIMIT = 50


# --- pure mapping helpers (unit-tested without Telegram) -----------------------------------------


def has_real_media(message: Any) -> bool:
    """Media other than a link preview (a web page preview is still a text message)."""
    media = getattr(message, "media", None)
    return media is not None and not isinstance(media, types.MessageMediaWebPage)


def reply_to_id(message: Any) -> int | None:
    reply = getattr(message, "reply_to", None)
    value = getattr(reply, "reply_to_msg_id", None) if reply is not None else None
    return int(value) if value is not None else None


def sent_message_id(result: Any, random_id: int) -> int:
    """The id of the message a ``messages.sendMessage`` with ``random_id`` created (#54).

    Private chats usually answer ``UpdateShortSentMessage``; other chats answer ``Updates``
    carrying ``UpdateMessageID(id, random_id)`` (Telethon's ``_get_response_message`` maps it the
    same way), with the new message itself as a fallback."""
    if isinstance(result, types.UpdateShortSentMessage):
        return int(result.id)
    if isinstance(result, types.UpdateShort):
        updates = [result.update]
    else:
        updates = list(getattr(result, "updates", None) or [])
    for update in updates:
        if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
            return int(update.id)
    for update in updates:
        if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            return int(update.message.id)
    raise GatewayError(f"no message id in the send result ({type(result).__name__})")


def mentions_me(message: Any) -> bool:
    """006 FR-002: the server set ``mentioned`` on the user's copy (an ``@username`` or name-link
    tag of the user) and the message is not a forward: a tag inside a forwarded copy does not
    count."""
    return bool(getattr(message, "mentioned", False)) and getattr(message, "fwd_from", None) is None


def is_forwarded(message: Any) -> bool:
    """The message is a forwarded copy (Telegram ``fwd_from`` header, #47)."""
    return getattr(message, "fwd_from", None) is not None


def forward_origin(message: Any) -> str | None:
    """A display label of a forward's origin, without any network call (#47).

    The hidden-account name the header carries (``fwd_from.from_name``) first, else the original
    sender or channel Telethon resolved from the update's entities (``message.forward``). None
    when the message is not a forward or the origin is unknown. The label is message content:
    stored encrypted, never logged."""
    header = getattr(message, "fwd_from", None)
    if header is None:
        return None
    name = getattr(header, "from_name", None)
    if name:
        return str(name)
    forward = getattr(message, "forward", None)
    for entity in (getattr(forward, "sender", None), getattr(forward, "chat", None)):
        if entity is not None:
            label = utils.get_display_name(entity)
            if label:
                return str(label)
    return None


def replies_to_message(message: Any, *, chat_id: int | None = None) -> bool:
    """The reply header points at a message in this chat (006 research R3).

    Not a reply: no header or a story header (no ``reply_to_msg_id``), the implicit header of a
    post in a forum topic (``forum_topic`` without ``reply_to_top_id``: it points at the topic's
    service message), or a reply to a message of another peer."""
    header = getattr(message, "reply_to", None)
    if header is None or getattr(header, "reply_to_msg_id", None) is None:
        return False
    if getattr(header, "forum_topic", False) and getattr(header, "reply_to_top_id", None) is None:
        return False
    peer = getattr(header, "reply_to_peer_id", None)
    return peer is None or chat_id is None or utils.get_peer_id(peer) == chat_id


def is_own_message(reply: Any) -> bool:
    """The replied-to message is an ordinary message the user sent (not a service message such
    as the creation of a topic the user opened)."""
    return reply is not None and bool(reply.out) and getattr(reply, "action", None) is None


def message_to_incoming(
    user_id: uuid.UUID,
    message: Any,
    *,
    chat_id: int,
    chat_kind: DialogKind,
    chat_title: str,
    sender_peer_id: int,
    sender_display_name: str,
    is_reply_to_me: bool = False,
    peer_kind: PeerKind | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        user_id=user_id,
        chat_id=chat_id,
        message_id=int(message.id),
        sender_peer_id=sender_peer_id,
        sender_display_name=sender_display_name,
        text=message.message or "",
        has_media=has_real_media(message),
        sent_at=message.date,
        is_outgoing=bool(message.out),
        reply_to_id=reply_to_id(message),
        mentions_me=mentions_me(message),
        is_reply_to_me=is_reply_to_me,
        chat_kind=chat_kind,
        chat_title=chat_title,
        peer_kind=peer_kind,
        is_forwarded=is_forwarded(message),
        forwarded_from=forward_origin(message),
    )


def message_to_outgoing(
    user_id: uuid.UUID,
    message: Any,
    *,
    chat_id: int,
    chat_kind: DialogKind = "private",
    peer_kind: PeerKind | None = None,
    chat_title: str = "",
) -> OutgoingEvent:
    return OutgoingEvent(
        user_id=user_id,
        chat_id=chat_id,
        message_id=int(message.id),
        text=message.message or "",
        sent_at=message.date,
        reply_to_id=reply_to_id(message),
        has_media=has_real_media(message),
        chat_kind=chat_kind,
        peer_kind=peer_kind,
        chat_title=chat_title,
        is_forwarded=is_forwarded(message),
        forwarded_from=forward_origin(message),
    )


def peer_kind_of(entity: Any) -> PeerKind | None:
    """specs/008 research R3: the other side of a private chat from its Telegram ``User``
    entity; ``None`` for anything that is not a user (groups, channels, a missing entity)."""
    if entity is None or not hasattr(entity, "bot"):
        return None
    if getattr(entity, "is_self", False):
        return "self"
    if getattr(entity, "bot", False):
        return "bot"
    if getattr(entity, "deleted", False):
        return "deleted"
    if getattr(entity, "support", False) or getattr(entity, "id", None) == SERVICE_USER_ID:
        return "service"
    return "user"


def read_event_from_update(user_id: uuid.UUID, update: Any) -> ReadEvent | None:
    """Our own read receipt from another device: marked chat id + max read message id."""
    if isinstance(update, types.UpdateReadHistoryInbox):
        chat_id = utils.get_peer_id(update.peer)
    elif isinstance(update, types.UpdateReadChannelInbox):
        chat_id = utils.get_peer_id(types.PeerChannel(update.channel_id))
    else:
        return None
    return ReadEvent(user_id=user_id, chat_id=int(chat_id), max_message_id=int(update.max_id))


def unread_mark_cleared_from_update(
    user_id: uuid.UUID, update: Any
) -> UnreadMarkClearedEvent | None:
    """The user's other session removed a chat's unread mark (specs/009 research U3).

    Only ``UpdateDialogUnreadMark`` with ``unread=False`` on a plain ``DialogPeer`` and without
    ``saved_peer_id`` counts; a set mark, folders and Saved Messages sub-dialogs are ignored.
    """
    if not isinstance(update, types.UpdateDialogUnreadMark):
        return None
    if update.unread is not False or update.saved_peer_id is not None:
        return None
    if not isinstance(update.peer, types.DialogPeer):
        return None
    return UnreadMarkClearedEvent(user_id=user_id, chat_id=int(utils.get_peer_id(update.peer.peer)))


async def _ignore_unread_cleared(_: UnreadMarkClearedEvent) -> None:
    return None


def dialog_kind(dialog: Any) -> DialogKind | None:
    """private / group, or None for dialogs we never offer (bots, Saved Messages, deleted and
    service accounts (008), broadcasts)."""
    if dialog.is_user:
        return "private" if peer_kind_of(dialog.entity) == "user" else None
    if dialog.is_group:
        return "group"
    return None  # broadcast channels: the user cannot reply there


_USERNAME_MENTION = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})")


def message_text(message: Any) -> str:
    """Text, else media caption (the same field), else a document's file name."""
    text = getattr(message, "message", None) or ""
    if text:
        return str(text)
    media = getattr(message, "media", None)
    document = getattr(media, "document", None) if media is not None else None
    for attribute in getattr(document, "attributes", None) or []:
        if isinstance(attribute, types.DocumentAttributeFilename):
            return str(attribute.file_name or "")
    return ""


def found_messages(result: Any, chat_id: int, me_id: int | None) -> list[FoundMessage]:
    """Map a messages.search / messages.getMessages result; empty and textless messages are
    skipped. In a private chat a message without ``from_id`` is the other side's."""
    names: dict[int, str] = {}
    for entity in [*getattr(result, "users", []), *getattr(result, "chats", [])]:
        names[int(utils.get_peer_id(entity))] = utils.get_display_name(entity)
    found: list[FoundMessage] = []
    for message in getattr(result, "messages", []):
        if not isinstance(message, types.Message):
            continue
        text = message_text(message)
        if not text:
            continue
        out = bool(message.out)
        if message.from_id is not None:
            sender = int(utils.get_peer_id(message.from_id))
        elif out and me_id is not None:
            sender = me_id
        else:
            sender = chat_id
        reply = message.reply_to if isinstance(message.reply_to, types.MessageReplyHeader) else None
        mentioned_ids = frozenset(
            int(e.user_id)
            for e in message.entities or []
            if isinstance(e, types.MessageEntityMentionName)
        )
        found.append(
            FoundMessage(
                chat_id=chat_id,
                message_id=int(message.id),
                sender_peer_id=sender,
                sender_display_name="" if sender == me_id else names.get(sender, ""),
                is_outgoing=out or (me_id is not None and sender == me_id),
                text=text,
                sent_at=message.date,
                reply_to_id=reply.reply_to_msg_id if reply is not None else None,
                thread_top_id=reply.reply_to_top_id if reply is not None else None,
                mentioned_peer_ids=mentioned_ids,
                mentioned_usernames=frozenset(m.lower() for m in _USERNAME_MENTION.findall(text)),
                is_forwarded=message.fwd_from is not None,
                forwarded_from=found_forward_origin(message.fwd_from, names),
            )
        )
    return found


def found_forward_origin(header: Any, names: dict[int, str]) -> str | None:
    """The origin label of a forward in a search result (#47): the hidden-account name the
    header carries, else the original sender or channel among the result's users and chats.
    None when not a forward or unknown. Message content: never logged."""
    if header is None:
        return None
    name = getattr(header, "from_name", None)
    if name:
        return str(name)
    peer = getattr(header, "from_id", None)
    if peer is None:
        return None
    return names.get(int(utils.get_peer_id(peer))) or None


def username_of(user: Any) -> str | None:
    """The user's username, or the first active collectible one."""
    name = getattr(user, "username", None)
    if not name:
        for item in getattr(user, "usernames", None) or []:
            if getattr(item, "active", True) and getattr(item, "username", None):
                name = item.username
                break
    return str(name).lower() if name else None


# --- client -------------------------------------------------------------------------------------


class UserClient:
    """One running Telethon client; handlers receive mapped gateway events."""

    def __init__(
        self,
        user_id: uuid.UUID,
        session_string: str,
        *,
        api_id: int,
        api_hash: str,
        on_new_message: NewMessageHandler,
        on_outgoing: OutgoingHandler,
        on_read_elsewhere: ReadHandler,
        on_unread_mark_cleared: UnreadMarkClearedHandler = _ignore_unread_cleared,
        device_model: str = DEFAULT_DEVICE_MODEL,
        system_version: str = DEFAULT_SYSTEM_VERSION,
        app_version: str = DEFAULT_APP_VERSION,
        client_factory: ClientFactory = TelegramClient,
        max_flood_sleep_s: int = MAX_FLOOD_SLEEP_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.user_id = user_id
        self._session_string = session_string
        self._api_id = api_id
        self._api_hash = api_hash
        self._on_new_message = on_new_message
        self._on_outgoing = on_outgoing
        self._on_read_elsewhere = on_read_elsewhere
        self._on_unread_mark_cleared = on_unread_mark_cleared
        self._device_model = device_model
        self._system_version = system_version
        self._app_version = app_version
        self._client_factory = client_factory
        self._max_flood_sleep_s = max_flood_sleep_s
        self._sleep = sleep
        self._client: Any = None
        self.me_id: int | None = None
        self.me_username: str | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("user client is not started")
        return self._client

    async def start(self) -> None:
        try:
            session = StringSession(self._session_string)
        except ValueError:
            raise SessionRevoked("stored telegram session string is invalid") from None
        client = self._new_telethon_client(session)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise SessionRevoked("telegram session is not authorised")
            me = await client.get_me()
        except (UnauthorizedError, AuthKeyDuplicatedError) as exc:
            await client.disconnect()
            raise SessionRevoked(type(exc).__name__) from None
        except SessionRevoked:
            await client.disconnect()
            raise
        self.me_id = int(me.id)
        self.me_username = username_of(me)
        client.add_event_handler(self._handle_new_message, events.NewMessage())
        client.add_event_handler(
            self._handle_raw,
            events.Raw(
                types=[
                    types.UpdateReadHistoryInbox,
                    types.UpdateReadChannelInbox,
                    types.UpdateDialogUnreadMark,
                ]
            ),
        )
        self._client = client
        log.info("telegram_client_started", user_id=str(self.user_id))

    async def stop(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.disconnect()
            log.info("telegram_client_stopped", user_id=str(self.user_id))

    async def log_out(self) -> None:
        """Terminate the session on Telegram's side (auth.logOut), then disconnect.

        A client that was never started connects just for this, without update handlers.
        An already unauthorised session (revoked, invalid string) counts as success;
        transient network errors propagate.
        """
        client, self._client = self._client, None
        temporary = client is None
        if client is None:
            try:
                client = self._new_telethon_client(StringSession(self._session_string))
            except ValueError:
                log.info("telegram_log_out_already_revoked", user_id=str(self.user_id))
                return
        try:
            if temporary:
                await client.connect()
                if not await client.is_user_authorized():
                    raise SessionRevoked("telegram session is not authorised")
            await self._guarded("log_out", client.log_out)
        except (SessionRevoked, UnauthorizedError, AuthKeyDuplicatedError):
            log.info("telegram_log_out_already_revoked", user_id=str(self.user_id))
        else:
            log.info("telegram_client_logged_out", user_id=str(self.user_id))
        finally:
            await client.disconnect()

    # --- operations -----------------------------------------------------------------------------

    async def list_dialogs(self) -> list[Dialog]:
        async def collect() -> list[Dialog]:
            dialogs: list[Dialog] = []
            async for dialog in self.client.iter_dialogs():
                kind = dialog_kind(dialog)
                if kind is not None:
                    dialogs.append(
                        Dialog(
                            chat_id=int(dialog.id),
                            title=dialog.name or "",
                            kind=kind,
                            unread_count=int(dialog.unread_count or 0),
                        )
                    )
            return dialogs

        return await self._guarded("list_dialogs", collect)

    async def fetch_own_history(
        self, chat_id: int, since: datetime
    ) -> AsyncIterator[IncomingMessage]:
        """The user's own text messages in ``chat_id`` sent at or after ``since``, newest first."""
        entity = await self._resolve(chat_id)
        async for message in self.client.iter_messages(entity, from_user="me"):
            if message.date < since:
                break
            if not message.out or not message.message:
                continue
            yield message_to_incoming(
                self.user_id,
                message,
                chat_id=chat_id,
                chat_kind="private",
                chat_title="",
                sender_peer_id=self.me_id or 0,
                sender_display_name="",
            )

    async def mark_read(self, chat_id: int, up_to_message_id: int) -> None:
        entity = await self._resolve(chat_id)

        async def acknowledge() -> None:
            await self.client.send_read_acknowledge(entity, max_id=up_to_message_id)

        await self._guarded("mark_read", acknowledge)

    async def send_text(
        self, chat_id: int, text: str, reply_to: int | None, *, random_id: int
    ) -> int:
        """One ``messages.sendMessage`` with the caller's ``random_id`` (#54).

        Telethon's ``send_message`` draws a fresh ``random_id`` per call, so a retry after an
        ambiguous failure (delivered, then a timeout) would send the text twice. The raw request
        is what ``send_message(entity, text, reply_to=..., parse_mode=None)`` builds, with the
        caller's ``random_id``: plain text without entities (Telethon parses Markdown by
        default, so the contact would get something other than the text the user approved),
        link previews on (``send_message``'s default).

        Telegram deduplicates by ``random_id``: a retry of a delivered message either returns
        the original or fails with ``RANDOM_ID_DUPLICATE``; then the delivered message is looked
        up among the user's recent outgoing messages in the chat by its text, and
        ``AlreadyDelivered`` is raised when it cannot be found. Nothing is ever sent twice.
        """
        entity = await self._resolve(chat_id)
        request = functions.messages.SendMessageRequest(
            peer=entity,
            message=text,
            entities=[],
            no_webpage=False,
            reply_to=None if reply_to is None else types.InputReplyToMessage(reply_to),
            random_id=random_id,
        )

        async def send() -> int:
            try:
                result = await self.client(request)
            except RandomIdDuplicateError:
                return await self._delivered_message_id(entity, text)
            return sent_message_id(result, random_id)

        return await self._guarded("send_text", send)

    async def _delivered_message_id(self, entity: Any, text: str) -> int:
        # Telegram trims surrounding whitespace and normalises line breaks in what it stores.
        wanted = _comparable(text)
        async for message in self.client.iter_messages(entity, limit=DELIVERED_LOOKUP_LIMIT):
            if getattr(message, "out", False) and _comparable(message.message or "") == wanted:
                log.info("telegram_send_already_delivered", user_id=str(self.user_id))
                return int(message.id)
        raise AlreadyDelivered("message delivered earlier, but its id was not found")

    async def mark_unread(self, chat_id: int) -> None:
        """Set the chat's unread mark (specs/009): one request, no flood sleep, no retry."""
        entity = await self._resolve(chat_id)
        request = functions.messages.MarkDialogUnreadRequest(
            peer=types.InputDialogPeer(peer=entity), unread=True
        )
        await self._no_sleep(request)

    async def search_chat(self, chat_id: int, query: str, *, limit: int = 20) -> list[FoundMessage]:
        entity = await self._resolve(chat_id)
        request = functions.messages.SearchRequest(
            peer=entity,
            q=query,
            filter=types.InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_id=0,
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=0,
            hash=0,
        )
        result = await self._no_sleep(request)
        return found_messages(result, chat_id, self.me_id)

    async def get_messages(self, chat_id: int, message_ids: Sequence[int]) -> list[FoundMessage]:
        if not message_ids:
            return []
        entity = await self._resolve(chat_id)
        ids = [types.InputMessageID(id=int(i)) for i in message_ids]
        request: Any
        if isinstance(entity, types.InputPeerChannel):
            channel = types.InputChannel(
                channel_id=entity.channel_id, access_hash=entity.access_hash
            )
            request = functions.channels.GetMessagesRequest(channel=channel, id=ids)
        else:
            request = functions.messages.GetMessagesRequest(id=ids)
        result = await self._no_sleep(request)
        return found_messages(result, chat_id, self.me_id)

    async def get_usernames(self, peer_ids: Sequence[int]) -> dict[int, str]:
        names: dict[int, str] = {}
        wanted: list[Any] = []
        for peer_id in dict.fromkeys(int(p) for p in peer_ids):
            if peer_id == self.me_id:
                if self.me_username:
                    names[peer_id] = self.me_username
                continue
            try:
                entity = await self.client.get_input_entity(peer_id)  # session cache only
            except ValueError:
                continue
            if isinstance(entity, types.InputPeerUser):
                wanted.append(
                    types.InputUser(user_id=entity.user_id, access_hash=entity.access_hash)
                )
        if wanted:
            for user in await self._no_sleep(functions.users.GetUsersRequest(id=wanted)):
                name = username_of(user)
                if name:
                    names[int(user.id)] = name
        return names

    async def _no_sleep(self, request: Any) -> Any:
        """One raw request for the search and unread-mark paths: no retry loop of our own,
        ``GatewayBusy`` as soon as Telegram's wait for this request type is known (see the module
        docstring)."""
        waited = getattr(self.client, "_flood_waited_requests", None) or {}
        due = waited.get(getattr(request, "CONSTRUCTOR_ID", None))
        if due is not None and (remaining := round(due - time.time())) > 0:
            raise GatewayBusy(remaining)
        try:
            return await self.client(request)
        except FloodWaitError as exc:
            log.info(
                "telegram_flood_wait_busy",
                user_id=str(self.user_id),
                operation=type(request).__name__,
                seconds=int(exc.seconds),
            )
            raise GatewayBusy(int(exc.seconds)) from None
        except (UnauthorizedError, AuthKeyDuplicatedError) as exc:
            raise SessionRevoked(type(exc).__name__) from None

    # --- internals ------------------------------------------------------------------------------

    def _new_telethon_client(self, session: StringSession) -> Any:
        return self._client_factory(
            session,
            self._api_id,
            self._api_hash,
            flood_sleep_threshold=self._max_flood_sleep_s,
            device_model=self._device_model,
            system_version=self._system_version,
            app_version=self._app_version,
        )

    async def _resolve(self, chat_id: int) -> Any:
        """Input entity for a marked chat id; the in-memory entity cache is filled from the
        dialog list when a chat has not been seen since the client started."""
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            await self._guarded("get_dialogs", self.client.get_dialogs)
            return await self.client.get_input_entity(chat_id)

    async def _guarded[T](self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(2):
            try:
                return await call()
            except FloodWaitError as exc:
                seconds = int(exc.seconds)
                if attempt == 0 and seconds <= self._max_flood_sleep_s:
                    log.info(
                        "telegram_flood_wait_sleep",
                        user_id=str(self.user_id),
                        operation=operation,
                        seconds=seconds,
                    )
                    await self._sleep(seconds)
                    continue
                log.warning(
                    "telegram_flood_wait_busy",
                    user_id=str(self.user_id),
                    operation=operation,
                    seconds=seconds,
                )
                raise GatewayBusy(seconds) from None
            except (UnauthorizedError, AuthKeyDuplicatedError) as exc:
                raise SessionRevoked(type(exc).__name__) from None
        raise AssertionError("unreachable")

    async def _handle_new_message(self, event: Any) -> None:
        try:
            message = event.message
            chat_id = int(event.chat_id)
            chat_kind: DialogKind = "private" if event.is_private else "group"
            if message.out:
                peer = await event.get_chat()
                await self._on_outgoing(
                    message_to_outgoing(
                        self.user_id,
                        message,
                        chat_id=chat_id,
                        chat_kind=chat_kind,
                        peer_kind=peer_kind_of(peer) if chat_kind == "private" else None,
                        chat_title=utils.get_display_name(peer) if peer is not None else "",
                    )
                )
                return
            sender = await event.get_sender()
            sender_name = utils.get_display_name(sender) if sender is not None else ""
            chat_title = sender_name
            is_reply_to_me = False
            if chat_kind == "group":
                chat = await event.get_chat()
                chat_title = utils.get_display_name(chat) if chat is not None else ""
                if replies_to_message(message, chat_id=chat_id):
                    is_reply_to_me = is_own_message(await event.get_reply_message())
            incoming = message_to_incoming(
                self.user_id,
                message,
                chat_id=chat_id,
                chat_kind=chat_kind,
                chat_title=chat_title,
                sender_peer_id=int(event.sender_id or chat_id),
                sender_display_name=sender_name,
                is_reply_to_me=is_reply_to_me,
                # In a private chat the incoming sender is the other side (008).
                peer_kind=peer_kind_of(sender) if chat_kind == "private" else None,
            )
            await self._on_new_message(incoming)
        except Exception:
            log.exception("telegram_update_handler_failed", user_id=str(self.user_id))

    async def _handle_raw(self, update: Any) -> None:
        try:
            event = read_event_from_update(self.user_id, update)
            if event is not None:
                await self._on_read_elsewhere(event)
                return
            cleared = unread_mark_cleared_from_update(self.user_id, update)
            if cleared is not None:
                await self._on_unread_mark_cleared(cleared)
        except Exception:
            log.exception("telegram_read_handler_failed", user_id=str(self.user_id))


def _comparable(text: str) -> str:
    """A message text as Telegram stores it, for the delivered-message lookup."""
    return text.replace("\r\n", "\n").strip()
