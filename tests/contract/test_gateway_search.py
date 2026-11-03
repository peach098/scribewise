"""Gateway search for earlier conversations (specs/004 T004) against a fake Telethon client.

Synthetic data only, no network: ``UserClient.search_chat`` / ``get_messages`` /
``get_usernames`` map raw MTProto results into ``FoundMessage`` and never sleep on a flood wait.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from scribe.telegram.client_pool import ClientPool
from scribe.telegram.gateway import FoundMessage, GatewayBusy, GatewayNotStarted, SessionRevoked
from scribe.telegram.user_client import UserClient

USER = uuid.uuid4()
ME = 42
PRIVATE = 1001  # a private chat with user 1001
GROUP = -1001234567890  # a supergroup (marked id)
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def _session() -> str:
    session = StringSession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(256))  # synthetic, never used on a network
    return str(session.save())


def _user(user_id: int, first: str, username: str | None = None) -> types.User:
    return types.User(id=user_id, first_name=first, username=username, access_hash=7)


def _msg(message_id: int, text: str, **extra: Any) -> types.Message:
    peer = extra.pop("peer", types.PeerUser(PRIVATE))
    return types.Message(id=message_id, peer_id=peer, date=T0, message=text, **extra)


class FakeTelethon:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, int | None]] = []
        self.results: list[Any] = []
        self.errors: list[Exception] = []
        self.users = {1001: _user(1001, "Synthetic Client", "Client_One")}
        self._flood_waited_requests: dict[int, float] = {}

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> Any:
        return SimpleNamespace(id=ME, username="Owner_Name")

    def add_event_handler(self, callback: Any, event: Any) -> None: ...

    async def get_input_entity(self, peer: int) -> Any:
        if peer == GROUP:
            return types.InputPeerChannel(channel_id=1234567890, access_hash=5)
        if peer in self.users or peer == PRIVATE:
            return types.InputPeerUser(user_id=peer, access_hash=7)
        raise ValueError("Could not find the input entity")

    async def __call__(
        self, request: Any, ordered: bool = False, flood_sleep_threshold: int | None = None
    ) -> Any:
        self.calls.append((request, flood_sleep_threshold))
        if self.errors:
            raise self.errors.pop(0)
        if isinstance(request, functions.users.GetUsersRequest):
            ids = [u.user_id for u in request.id]
            return [self.users[i] for i in ids if i in self.users]
        return self.results.pop(0)


async def _client(fake: FakeTelethon) -> UserClient:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def ignore(_: Any) -> None: ...

    client = UserClient(
        USER,
        _session(),
        api_id=1,
        api_hash="hash",
        on_new_message=ignore,
        on_outgoing=ignore,
        on_read_elsewhere=ignore,
        client_factory=lambda *a, **k: fake,
        sleep=sleep,
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    await client.start()
    return client


def _result(messages: list[Any], users: list[Any] | None = None) -> Any:
    return SimpleNamespace(messages=messages, users=users or [], chats=[])


async def test_search_chat_maps_messages_and_uses_no_flood_sleep() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    reply = types.MessageReplyHeader(reply_to_msg_id=10, reply_to_top_id=5)
    fake.results.append(
        _result(
            [
                _msg(
                    12,
                    "@client_one the invoice is ready",
                    from_id=types.PeerUser(ME),
                    out=True,
                    reply_to=reply,
                    entities=[types.MessageEntityMentionName(offset=0, length=11, user_id=1001)],
                ),
                _msg(11, "Send the same invoice", from_id=types.PeerUser(1001)),
                _msg(9, "", from_id=types.PeerUser(1001)),  # no text, no caption: skipped
                types.MessageEmpty(id=8, peer_id=types.PeerUser(PRIVATE)),
            ],
            [fake.users[1001]],
        )
    )
    found = await client.search_chat(PRIVATE, "invoice", limit=20)

    request, threshold = fake.calls[0]
    assert isinstance(request, functions.messages.SearchRequest)
    assert request.q == "invoice" and request.limit == 20
    assert isinstance(request.filter, types.InputMessagesFilterEmpty)
    assert [f.message_id for f in found] == [12, 11]
    own, theirs = found
    assert own == FoundMessage(
        chat_id=PRIVATE,
        message_id=12,
        sender_peer_id=ME,
        sender_display_name="",
        is_outgoing=True,
        text="@client_one the invoice is ready",
        sent_at=T0,
        reply_to_id=10,
        thread_top_id=5,
        mentioned_peer_ids=frozenset({1001}),
        mentioned_usernames=frozenset({"client_one"}),
    )
    assert theirs.sender_peer_id == 1001
    assert theirs.sender_display_name == "Synthetic Client"
    assert not theirs.is_outgoing


async def test_private_message_without_from_id_uses_the_chat_peer() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    fake.results.append(_result([_msg(3, "Price agreed")], [fake.users[1001]]))
    [found] = await client.search_chat(PRIVATE, "price")
    assert found.sender_peer_id == PRIVATE
    assert found.sender_display_name == "Synthetic Client"


async def test_document_without_text_is_found_by_file_name() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    document = types.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=T0,
        mime_type="application/pdf",
        size=10,
        dc_id=2,
        attributes=[types.DocumentAttributeFilename(file_name="invoice-123.pdf")],
    )
    media = types.MessageMediaDocument(document=document)
    fake.results.append(_result([_msg(4, "", media=media, from_id=types.PeerUser(1001))]))
    [found] = await client.search_chat(PRIVATE, "invoice")
    assert found.text == "invoice-123.pdf"


async def test_caption_is_the_text_of_a_media_message() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))
    fake.results.append(_result([_msg(6, "Invoice photo", media=media)]))
    [found] = await client.search_chat(PRIVATE, "invoice")
    assert found.text == "Invoice photo"


async def test_forwarded_search_results_carry_the_origin() -> None:
    """#47: a forwarded copy is flagged, with its origin from the header or the result."""
    fake = FakeTelethon()
    client = await _client(fake)
    origin = _user(2002, "Synthetic Origin")
    by_user = types.MessageFwdHeader(date=T0, from_id=types.PeerUser(2002))
    hidden = types.MessageFwdHeader(date=T0, from_name="Synthetic Hidden")
    unknown = types.MessageFwdHeader(date=T0, from_id=types.PeerUser(3003))
    fake.results.append(
        _result(
            [
                _msg(21, "Invoice from the origin", from_id=types.PeerUser(1001), fwd_from=by_user),
                _msg(22, "Invoice from a hidden account", fwd_from=hidden),
                _msg(23, "Invoice from someone unknown", fwd_from=unknown),
                _msg(24, "My own invoice note", from_id=types.PeerUser(1001)),
            ],
            [fake.users[1001], origin],
        )
    )
    found = await client.search_chat(PRIVATE, "invoice")
    assert [(f.is_forwarded, f.forwarded_from) for f in found] == [
        (True, "Synthetic Origin"),
        (True, "Synthetic Hidden"),
        (True, None),
        (False, None),
    ]


async def test_get_messages_batches_one_call_and_skips_missing() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    fake.results.append(
        _result(
            [
                _msg(
                    10,
                    "Budget question",
                    peer=types.PeerChannel(1234567890),
                    from_id=types.PeerUser(1001),
                ),
                types.MessageEmpty(id=11, peer_id=types.PeerChannel(1234567890)),
            ],
            [fake.users[1001]],
        )
    )
    found = await client.get_messages(GROUP, [10, 11])
    request, threshold = fake.calls[0]
    assert isinstance(request, functions.channels.GetMessagesRequest)
    assert [m.id for m in request.id] == [10, 11]
    assert [(f.chat_id, f.message_id) for f in found] == [(GROUP, 10)]


async def test_get_messages_in_a_private_chat_uses_messages_namespace() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    fake.results.append(_result([_msg(10, "Earlier note")]))
    await client.get_messages(PRIVATE, [10])
    assert isinstance(fake.calls[0][0], functions.messages.GetMessagesRequest)


async def test_get_usernames_from_cache_and_own_username() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    names = await client.get_usernames([1001, ME, 5555])  # 5555 is unknown: omitted
    assert names == {1001: "client_one", ME: "owner_name"}
    [(request, _)] = fake.calls
    assert isinstance(request, functions.users.GetUsersRequest)


async def test_get_usernames_of_only_me_makes_no_request() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    assert await client.get_usernames([ME]) == {ME: "owner_name"}
    assert fake.calls == []


@pytest.mark.parametrize("seconds", [3, 600])
async def test_flood_wait_raises_gateway_busy_without_sleeping(seconds: int) -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    fake.errors.append(errors.FloodWaitError(request=None, capture=seconds))
    with pytest.raises(GatewayBusy) as info:
        await client.search_chat(PRIVATE, "invoice")
    assert info.value.seconds == seconds
    assert client.sleeps == []  # type: ignore[attr-defined]
    assert len(fake.calls) == 1  # no retry


async def test_unauthorized_raises_session_revoked() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    fake.errors.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(SessionRevoked):
        await client.search_chat(PRIVATE, "invoice")


async def test_pool_without_a_running_client_raises_not_started() -> None:
    pool = ClientPool.__new__(ClientPool)
    pool._clients = {}
    with pytest.raises(GatewayNotStarted):
        await pool.search_chat(USER, PRIVATE, "invoice")
    with pytest.raises(GatewayNotStarted):
        await pool.get_messages(USER, PRIVATE, [1])
    with pytest.raises(GatewayNotStarted):
        await pool.get_usernames(USER, [1001])


async def test_known_flood_wait_raises_before_sending() -> None:
    """Telethon would sleep through a short wait it already knows about: we refuse first."""
    fake = FakeTelethon()
    client = await _client(fake)
    constructor = functions.messages.SearchRequest.CONSTRUCTOR_ID
    fake._flood_waited_requests[constructor] = time.time() + 30
    with pytest.raises(GatewayBusy) as info:
        await client.search_chat(PRIVATE, "invoice")
    assert 29 <= info.value.seconds <= 30
    assert fake.calls == []  # nothing sent
    fake._flood_waited_requests[constructor] = time.time() - 1  # expired: sent normally
    fake.results.append(_result([]))
    assert await client.search_chat(PRIVATE, "invoice") == []
