"""MessagingGateway: the narrow interface between the agent and Telegram user accounts.

Production: ``client_pool.ClientPool`` (one Telethon client per user). Tests:
``fake_gateway.FakeMessagingGateway``.

Rules (contracts/internal-interfaces.md):
- receiving updates MUST NOT mark anything read; only ``mark_read`` does;
- ``send_text`` is called exclusively by ``sending/send.py`` after explicit approval, with the
  draft's ``send_random_id`` so a retry can never deliver the reply twice (#54).
- ``search_chat`` / ``get_messages`` / ``get_usernames`` (specs/004-conversation-search) are
  read-only, called only by ``context/earlier.EarlierSearch`` for the user's enabled work
  chats; a known flood wait raises ``GatewayBusy`` at once and the caller bounds the rest with
  its own deadline.
- ``mark_unread`` (specs/009-mark-chats-unread) is called exclusively by
  ``telegram/unread_mark.UnreadMarker``; it never sleeps through a flood wait and never removes a
  mark.
- Receiving ``UpdateDialogUnreadMark`` never marks or reads anything; only ``unread=False`` on a
  plain ``DialogPeer`` without ``saved_peer_id`` is forwarded (``on_unread_mark_cleared``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

DialogKind = Literal["private", "group", "channel_comments"]
# specs/008: who is on the other side of a private chat. Only "user" (a real person) can become a
# work chat by work account mode; ``None`` (groups, or unknown) is never eligible.
PeerKind = Literal["user", "bot", "self", "service", "deleted"]


class GatewayError(Exception):
    """Base class for gateway failures."""


class GatewayBusy(GatewayError):
    """Telegram asked us to wait longer than we are willing to sleep (flood wait)."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait of {seconds}s")
        self.seconds = seconds


class AlreadyDelivered(GatewayError):
    """Telegram reports the send's ``random_id`` as used (an earlier attempt was delivered), and
    the delivered message could not be found to record its id (#54). Nothing was sent again."""


class GatewayNotStarted(GatewayError):
    """No running client for this user."""


class SessionRevoked(GatewayError):
    """The user's Telegram session is no longer authorised (logged out / revoked)."""


@dataclass(frozen=True)
class Dialog:
    chat_id: int
    title: str
    kind: DialogKind
    unread_count: int = 0


@dataclass(frozen=True)
class IncomingMessage:
    user_id: uuid.UUID  # the agent user whose account received it
    chat_id: int
    message_id: int
    sender_peer_id: int
    sender_display_name: str
    text: str
    has_media: bool
    sent_at: datetime
    is_outgoing: bool = False
    reply_to_id: int | None = None  # raw reply_to_msg_id, stored as is
    # The server flagged the user's copy as mentioning the user (an @username or name-link tag)
    # and the message is not a forward (006 data-model.md).
    mentions_me: bool = False
    # Groups only: a real reply in this chat (not the implicit forum-topic header, not a reply
    # to another peer) to an ordinary message the user sent (006 data-model.md).
    is_reply_to_me: bool = False
    chat_kind: DialogKind = "private"
    chat_title: str = ""
    peer_kind: PeerKind | None = None  # 008: private chats only; None is never eligible
    # A forwarded copy (Telegram ``fwd_from``): its text is not the sender's own words (#47).
    # ``forwarded_from`` is a display label of the origin (a name or channel title) when known;
    # it is message content (stored encrypted, never logged).
    is_forwarded: bool = False
    forwarded_from: str | None = None


@dataclass(frozen=True)
class ReadEvent:
    """The user read a chat on another device, up to ``max_message_id``."""

    user_id: uuid.UUID
    chat_id: int
    max_message_id: int


@dataclass(frozen=True)
class UnreadMarkClearedEvent:
    """The user's other session removed the chat's unread mark (opened it / "Mark as read")."""

    user_id: uuid.UUID
    chat_id: int


@dataclass(frozen=True)
class OutgoingEvent:
    """The user (or the agent on their behalf) sent a message from their account."""

    user_id: uuid.UUID
    chat_id: int
    message_id: int
    text: str
    sent_at: datetime
    reply_to_id: int | None = None
    has_media: bool = False
    # 008: what the chat is, for picking up a chat the user writes to first.
    chat_kind: DialogKind = "private"
    peer_kind: PeerKind | None = None  # None is never eligible
    chat_title: str = ""
    is_forwarded: bool = False  # the user forwarded it: not their writing (#47)
    forwarded_from: str | None = None


@dataclass(frozen=True)
class FoundMessage:
    """One message returned by a server-side search or lookup (specs/004 research S3)."""

    chat_id: int
    message_id: int
    sender_peer_id: int
    sender_display_name: str
    is_outgoing: bool
    text: str  # text, else media caption, else the document file name
    sent_at: datetime
    reply_to_id: int | None = None
    thread_top_id: int | None = None
    mentioned_peer_ids: frozenset[int] = frozenset()
    mentioned_usernames: frozenset[str] = frozenset()  # lower-case, without "@"
    # A forwarded copy and its origin label (#47), as on ``IncomingMessage``.
    is_forwarded: bool = False
    forwarded_from: str | None = None


NewMessageHandler = Callable[[IncomingMessage], Awaitable[None]]
ReadHandler = Callable[[ReadEvent], Awaitable[None]]
OutgoingHandler = Callable[[OutgoingEvent], Awaitable[None]]
UnreadMarkClearedHandler = Callable[[UnreadMarkClearedEvent], Awaitable[None]]


class MessagingGateway(Protocol):
    async def start(self, user_id: uuid.UUID) -> None: ...
    async def stop(self, user_id: uuid.UUID) -> None: ...
    async def log_out(self, user_id: uuid.UUID) -> None:
        """Terminate the user's Telegram session (auth.logOut) and stop their client.

        If no client is running, a temporary client is started from the stored session
        string just to log out. A session that is already revoked counts as success.
        Never raises for an unauthorised session; transient network errors propagate.
        """
        ...

    async def list_dialogs(self, user_id: uuid.UUID) -> list[Dialog]: ...
    def fetch_own_history(
        self, user_id: uuid.UUID, chat_id: int, since: datetime
    ) -> AsyncIterator[IncomingMessage]: ...
    async def mark_read(self, user_id: uuid.UUID, chat_id: int, up_to_message_id: int) -> None: ...
    async def send_text(
        self, user_id: uuid.UUID, chat_id: int, text: str, reply_to: int | None, *, random_id: int
    ) -> int:
        """Send plain text (no entities). ``random_id`` is the draft's idempotency key (#54):
        every attempt for one draft passes the same value, so Telegram never delivers it twice.
        Returns the sent message's id; ``AlreadyDelivered`` when an earlier attempt was delivered
        but its id cannot be found."""
        ...

    async def search_chat(
        self, user_id: uuid.UUID, chat_id: int, query: str, *, limit: int = 20
    ) -> list[FoundMessage]:
        """Server-side search in ONE chat (messages.search), newest first.

        A flood wait Telegram already announced for this request type raises
        ``GatewayBusy(seconds)`` without sending; callers bound the call with a deadline. Raises
        ``GatewayNotStarted`` when the user's client is not running and ``SessionRevoked``
        when the session is gone.
        """
        ...

    async def get_messages(
        self, user_id: uuid.UUID, chat_id: int, message_ids: Sequence[int]
    ) -> list[FoundMessage]:
        """One batched lookup by id (reply targets); missing ids are skipped. Same error
        contract as ``search_chat``."""
        ...

    async def get_usernames(self, user_id: uuid.UUID, peer_ids: Sequence[int]) -> dict[int, str]:
        """Lower-case usernames (without "@") of the given users; peers without one are
        omitted. The account owner's own id is answered from ``get_me``. Same error contract
        as ``search_chat``."""
        ...

    async def mark_unread(self, user_id: uuid.UUID, chat_id: int) -> None:
        """Set the chat's unread mark (messages.markDialogUnread, unread=True).

        Never sleeps through a flood wait: raises ``GatewayBusy`` at once. Raises
        ``GatewayNotStarted`` / ``SessionRevoked`` like the other operations. Never touches
        read state and never removes a mark. Called only by ``UnreadMarker``.
        """
        ...

    def on_new_message(self, handler: NewMessageHandler) -> None: ...
    def on_read_elsewhere(self, handler: ReadHandler) -> None: ...
    def on_outgoing(self, handler: OutgoingHandler) -> None: ...
    def on_unread_mark_cleared(self, handler: UnreadMarkClearedHandler) -> None: ...
