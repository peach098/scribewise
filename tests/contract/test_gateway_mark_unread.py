"""Gateway unread marks (specs/009 T006, T019) against a fake Telethon client.

Synthetic data only, no network: ``UserClient.mark_unread`` sends exactly one
``messages.markDialogUnread(unread=True)`` and never sleeps on a flood wait; the raw update
handler forwards only "the user cleared the mark" signals; ``ClientPool`` delegates and fans out.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from telethon import errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from scribe.db.models import SessionStatus
from scribe.telegram.client_pool import ClientPool
from scribe.telegram.gateway import (
    GatewayBusy,
    GatewayNotStarted,
    ReadEvent,
    SessionRevoked,
    UnreadMarkClearedEvent,
)
from scribe.telegram.user_client import UserClient
from tests.conftest import LogCapture

USER = uuid.uuid4()
ME = 42
PRIVATE = 1001
GROUP = -1001234567890


def _session() -> str:
    session = StringSession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(256))  # synthetic, never used on a network
    return str(session.save())


class FakeTelethon:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.errors: list[Exception] = []
        self.handlers: list[tuple[Any, Any]] = []
        self._flood_waited_requests: dict[int, float] = {}

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> Any:
        return SimpleNamespace(id=ME, username="owner_name")

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    async def get_input_entity(self, peer: int) -> Any:
        if peer == GROUP:
            return types.InputPeerChannel(channel_id=1234567890, access_hash=5)
        if peer == PRIVATE:
            return types.InputPeerUser(user_id=peer, access_hash=7)
        raise ValueError("Could not find the input entity")

    async def get_dialogs(self) -> list[Any]:
        return []

    async def __call__(self, request: Any, ordered: bool = False) -> Any:
        self.calls.append(request)
        if self.errors:
            raise self.errors.pop(0)
        return True


class Recorder:
    def __init__(self) -> None:
        self.reads: list[ReadEvent] = []
        self.cleared: list[UnreadMarkClearedEvent] = []
        self.fail_cleared = False

    async def ignore(self, _: Any) -> None: ...

    async def on_read(self, event: ReadEvent) -> None:
        self.reads.append(event)

    async def on_cleared(self, event: UnreadMarkClearedEvent) -> None:
        if self.fail_cleared:
            raise RuntimeError("handler broke")
        self.cleared.append(event)


async def _client(fake: FakeTelethon, recorder: Recorder | None = None) -> UserClient:
    recorder = recorder or Recorder()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = UserClient(
        USER,
        _session(),
        api_id=1,
        api_hash="hash",
        on_new_message=recorder.ignore,
        on_outgoing=recorder.ignore,
        on_read_elsewhere=recorder.on_read,
        on_unread_mark_cleared=recorder.on_cleared,
        client_factory=lambda *a, **k: fake,
        sleep=sleep,
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    await client.start()
    return client


# --- T006: UserClient.mark_unread ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("chat_id", "entity"),
    [
        (PRIVATE, types.InputPeerUser(user_id=PRIVATE, access_hash=7)),
        (GROUP, types.InputPeerChannel(channel_id=1234567890, access_hash=5)),
    ],
)
async def test_mark_unread_sends_one_mark_dialog_unread_request(chat_id: int, entity: Any) -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    await client.mark_unread(chat_id)
    [request] = fake.calls
    assert isinstance(request, functions.messages.MarkDialogUnreadRequest)
    assert request.peer == types.InputDialogPeer(peer=entity)
    assert request.unread is True
    assert request.parent_peer is None


@pytest.mark.parametrize("seconds", [3, 600])
async def test_mark_unread_flood_wait_raises_gateway_busy_without_sleeping(seconds: int) -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    fake.errors.append(errors.FloodWaitError(request=None, capture=seconds))
    with pytest.raises(GatewayBusy) as info:
        await client.mark_unread(PRIVATE)
    assert info.value.seconds == seconds
    assert client.sleeps == []  # type: ignore[attr-defined]
    assert len(fake.calls) == 1  # no retry


async def test_mark_unread_known_flood_wait_raises_before_sending() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    constructor = functions.messages.MarkDialogUnreadRequest.CONSTRUCTOR_ID
    fake._flood_waited_requests[constructor] = time.time() + 30
    with pytest.raises(GatewayBusy):
        await client.mark_unread(PRIVATE)
    assert fake.calls == []


async def test_mark_unread_unauthorized_raises_session_revoked() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    fake.errors.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(SessionRevoked):
        await client.mark_unread(PRIVATE)


async def test_mark_unread_never_touches_read_state() -> None:
    fake = FakeTelethon()
    client = await _client(fake)
    await client.mark_unread(PRIVATE)
    assert not any(
        isinstance(r, functions.messages.ReadHistoryRequest | functions.channels.ReadHistoryRequest)
        for r in fake.calls
    )


# --- T006: ClientPool.mark_unread ----------------------------------------------------------------


def _pool() -> ClientPool:
    return ClientPool(cast(Any, None), api_id=1, api_hash="hash")


async def test_pool_mark_unread_without_a_running_client_raises_not_started() -> None:
    with pytest.raises(GatewayNotStarted):
        await _pool().mark_unread(USER, PRIVATE)


class RevokedClient:
    def __init__(self) -> None:
        self.stopped = False
        self.calls: list[int] = []

    async def stop(self) -> None:
        self.stopped = True

    async def mark_unread(self, chat_id: int) -> None:
        self.calls.append(chat_id)
        raise SessionRevoked("AuthKeyUnregisteredError")


async def test_pool_mark_unread_with_a_revoked_session_marks_it_revoked() -> None:
    pool = _pool()
    client = RevokedClient()
    pool._clients[USER] = cast(Any, client)
    marked: list[tuple[uuid.UUID, SessionStatus]] = []

    async def mark(user_id: uuid.UUID, status: SessionStatus, reason: str, notice: Any) -> None:
        marked.append((user_id, status))

    pool._mark = mark  # type: ignore[method-assign]
    with pytest.raises(SessionRevoked):
        await pool.mark_unread(USER, PRIVATE)
    assert client.calls == [PRIVATE]
    assert client.stopped
    assert not pool.is_running(USER)
    assert marked == [(USER, SessionStatus.revoked)]


async def test_pool_mark_unread_delegates_to_the_users_client() -> None:
    pool = _pool()
    calls: list[int] = []

    class Client:
        async def mark_unread(self, chat_id: int) -> None:
            calls.append(chat_id)

    pool._clients[USER] = cast(Any, Client())
    await pool.mark_unread(USER, GROUP)
    assert calls == [GROUP]


# --- T019: UpdateDialogUnreadMark through the raw handler ----------------------------------------


def _raw_handler(fake: FakeTelethon) -> tuple[Any, Any]:
    [(callback, event)] = [
        (c, e)
        for c, e in fake.handlers
        if types.UpdateReadHistoryInbox in (getattr(e, "types", None) or ())
    ]
    return callback, event


async def test_raw_handler_listens_to_unread_mark_updates() -> None:
    fake = FakeTelethon()
    await _client(fake)
    _, event = _raw_handler(fake)
    assert set(event.types) == {
        types.UpdateReadHistoryInbox,
        types.UpdateReadChannelInbox,
        types.UpdateDialogUnreadMark,
    }


async def test_cleared_mark_is_forwarded_and_set_mark_is_not() -> None:
    fake = FakeTelethon()
    recorder = Recorder()
    await _client(fake, recorder)
    handler, _ = _raw_handler(fake)
    await handler(
        types.UpdateDialogUnreadMark(
            peer=types.DialogPeer(peer=types.PeerUser(PRIVATE)), unread=True
        )
    )
    assert recorder.cleared == []
    await handler(
        types.UpdateDialogUnreadMark(
            peer=types.DialogPeer(peer=types.PeerUser(PRIVATE)), unread=False
        )
    )
    assert recorder.cleared == [UnreadMarkClearedEvent(user_id=USER, chat_id=PRIVATE)]
    assert recorder.reads == []
    assert fake.calls == []  # receiving the update never marks or reads anything


async def test_read_updates_still_reach_on_read_elsewhere() -> None:
    fake = FakeTelethon()
    recorder = Recorder()
    await _client(fake, recorder)
    handler, _ = _raw_handler(fake)
    await handler(
        types.UpdateReadHistoryInbox(
            peer=types.PeerUser(PRIVATE), max_id=10, still_unread_count=0, pts=1, pts_count=1
        )
    )
    assert recorder.reads == [ReadEvent(user_id=USER, chat_id=PRIVATE, max_message_id=10)]
    assert recorder.cleared == []


async def test_cleared_handler_errors_are_logged_and_swallowed(log_capture: LogCapture) -> None:
    fake = FakeTelethon()
    recorder = Recorder()
    recorder.fail_cleared = True
    await _client(fake, recorder)
    handler, _ = _raw_handler(fake)
    await handler(
        types.UpdateDialogUnreadMark(
            peer=types.DialogPeer(peer=types.PeerUser(PRIVATE)), unread=False
        )
    )
    assert len(log_capture.events("telegram_read_handler_failed")) == 1


async def test_pool_fans_out_cleared_events_from_every_client() -> None:
    pool = _pool()
    received: list[tuple[str, UnreadMarkClearedEvent]] = []

    async def broken(event: UnreadMarkClearedEvent) -> None:
        raise RuntimeError("first handler broke")

    async def second(event: UnreadMarkClearedEvent) -> None:
        received.append(("second", event))

    pool.on_unread_mark_cleared(broken)
    pool.on_unread_mark_cleared(second)
    factory_kwargs: list[dict[str, Any]] = []

    def factory(user_id: uuid.UUID, session_string: str, **kwargs: Any) -> Any:
        factory_kwargs.append(kwargs)
        return SimpleNamespace()

    pool._factory = factory
    pool._new_client(USER, "synthetic")
    other = uuid.uuid4()
    pool._new_client(other, "synthetic")
    for user_id, kwargs in zip((USER, other), factory_kwargs, strict=True):
        await kwargs["on_unread_mark_cleared"](UnreadMarkClearedEvent(user_id=user_id, chat_id=5))
    assert received == [
        ("second", UnreadMarkClearedEvent(user_id=USER, chat_id=5)),
        ("second", UnreadMarkClearedEvent(user_id=other, chat_id=5)),
    ]
