"""LoginManager: one attempt per chat, one activity record per attempt, drain of QR tokens."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest

from scribe.telegram.login import (
    LoginAttempt,
    LoginClient,
    LoginManager,
    LoginResult,
    QrCode,
    QrOutcome,
)
from tests.conftest import FrozenClock

USER_ID = uuid.UUID(int=7)
CHAT_ID = 1001
RECORD_KEYS = {
    "method",
    "purpose",
    "outcome",
    "qr_codes_shown",
    "password_needed",
    "duration_s",
    "error_kind",
}


class StubClient:
    def __init__(self, n: int) -> None:
        self.n = n
        self.closed = asyncio.Event()
        self.logged_out = False
        self.wait_outcome = QrOutcome.expired
        self.wait_timeouts: list[float] = []
        self.sent_codes: list[str] = []

    async def send_code(self, phone: str) -> None:
        self.sent_codes.append(phone)

    async def sign_in_code(self, code: str) -> bool:
        return False

    async def sign_in_password(self, password: str) -> None:
        return None

    async def complete(self) -> LoginResult:
        return LoginResult("synthetic-session", 4242, "Synthetic User")

    async def start_qr(self) -> QrCode:
        raise NotImplementedError

    async def refresh_qr(self) -> QrCode:
        raise NotImplementedError

    async def wait_qr(self, timeout_s: float) -> QrOutcome:
        self.wait_timeouts.append(timeout_s)
        return self.wait_outcome

    async def log_out(self) -> None:
        self.logged_out = True

    async def close(self) -> None:
        self.closed.set()
        raise RuntimeError("closing never fails a step")


class Harness:
    def __init__(self, clock: FrozenClock) -> None:
        self.clients: list[StubClient] = []
        self.records: list[tuple[uuid.UUID, dict[str, Any]]] = []
        self.late_scans: list[int] = []
        self.manager = LoginManager(
            self.factory, record=self.record, clock=clock, on_late_scan=self.on_late_scan
        )

    def factory(self) -> LoginClient:
        client = StubClient(len(self.clients))
        self.clients.append(client)
        return client

    async def record(self, user_id: uuid.UUID, meta: dict[str, Any]) -> None:
        self.records.append((user_id, meta))

    async def on_late_scan(self, chat_id: int) -> None:
        self.late_scans.append(chat_id)

    async def begin(self, method: str = "qr") -> LoginAttempt:
        return await self.manager.begin(
            CHAT_ID,
            user_id=USER_ID,
            purpose="setup",
            method=method,  # type: ignore[arg-type]
        )


@pytest.fixture
def harness(clock: FrozenClock) -> Harness:
    return Harness(clock)


async def _closed(client: StubClient) -> None:
    await asyncio.wait_for(client.closed.wait(), timeout=1)


async def test_begin_replaces_previous_attempt(harness: Harness) -> None:
    first = await harness.begin()
    second = await harness.begin(method="phone_code")
    assert harness.manager.get(CHAT_ID) is second and first is not second
    await _closed(harness.clients[0])
    assert not harness.clients[1].closed.is_set()
    ((user_id, meta),) = harness.records
    assert user_id == USER_ID
    assert meta["outcome"] == "cancelled" and meta["method"] == "qr"


async def test_end_records_exactly_once(harness: Harness, clock: FrozenClock) -> None:
    attempt = await harness.begin()
    attempt.qr_codes_shown = 3
    attempt.password_needed = True
    clock.advance(seconds=42.6)
    await harness.manager.end(CHAT_ID, "failed", error_kind="LoginFlood")
    await harness.manager.end(CHAT_ID, "failed")  # no attempt any more: nothing recorded
    assert harness.manager.get(CHAT_ID) is None
    await _closed(harness.clients[0])
    ((user_id, meta),) = harness.records
    assert user_id == USER_ID
    assert set(meta) == RECORD_KEYS
    assert meta == {
        "method": "qr",
        "purpose": "setup",
        "outcome": "failed",
        "qr_codes_shown": 3,
        "password_needed": True,
        "duration_s": 43,
        "error_kind": "LoginFlood",
    }


async def test_end_cancels_task(harness: Harness) -> None:
    attempt = await harness.begin()
    blocker = asyncio.Event()

    async def forever() -> None:
        await blocker.wait()

    attempt.task = asyncio.create_task(forever())
    await harness.manager.end(CHAT_ID, "cancelled")
    assert attempt.task.cancelled()


async def test_end_from_own_task_does_not_cancel_itself(harness: Harness) -> None:
    attempt = await harness.begin()

    async def loop() -> str:
        await harness.manager.end(CHAT_ID, "connected")
        return "finished"

    task = asyncio.create_task(loop())
    attempt.task = task  # type: ignore[assignment]
    assert await task == "finished"
    assert harness.records[0][1]["outcome"] == "connected"


async def test_record_failure_still_closes_client(clock: FrozenClock) -> None:
    clients: list[StubClient] = []

    def factory() -> LoginClient:
        clients.append(StubClient(0))
        return clients[-1]

    async def broken(user_id: uuid.UUID, meta: dict[str, Any]) -> None:
        raise RuntimeError("database down")

    manager = LoginManager(factory, record=broken, clock=clock)
    await manager.begin(CHAT_ID, user_id=USER_ID, purpose="reconnect", method="phone_code")
    await manager.end(CHAT_ID, "cancelled")
    await _closed(clients[0])


async def test_drain_logs_out_late_scan(harness: Harness, clock: FrozenClock) -> None:
    attempt = await harness.begin()
    attempt.qr_expires_at = clock() + timedelta(seconds=20)
    client = harness.clients[0]
    client.wait_outcome = QrOutcome.authorized
    clock.advance(seconds=5)
    await harness.manager.end(CHAT_ID, "cancelled", drain=True)
    assert harness.manager.get(CHAT_ID) is None
    assert len(harness.records) == 1  # recorded at end(), not again by the drain
    await _closed(client)
    assert client.wait_timeouts == [15.0]
    assert client.logged_out and harness.late_scans == [CHAT_ID]


async def test_drain_until_expiry_without_scan(harness: Harness, clock: FrozenClock) -> None:
    attempt = await harness.begin()
    attempt.qr_expires_at = clock() + timedelta(seconds=20)
    await harness.manager.end(CHAT_ID, "cancelled", drain=True)
    client = harness.clients[0]
    await _closed(client)
    assert client.wait_timeouts == [20.0]
    assert not client.logged_out and harness.late_scans == []


@pytest.mark.parametrize("expires_in", [None, 0, -5])
async def test_no_drain_without_live_token(
    harness: Harness, clock: FrozenClock, expires_in: int | None
) -> None:
    attempt = await harness.begin()
    if expires_in is not None:
        attempt.qr_expires_at = clock() + timedelta(seconds=expires_in)
    await harness.manager.end(CHAT_ID, "cancelled", drain=True)
    client = harness.clients[0]
    assert client.closed.is_set()  # closed right away, no waiting
    assert client.wait_timeouts == []


async def test_begin_drains_previous_qr_attempt(harness: Harness, clock: FrozenClock) -> None:
    attempt = await harness.begin()
    attempt.qr_expires_at = clock() + timedelta(seconds=10)
    harness.clients[0].wait_outcome = QrOutcome.authorized
    await harness.begin(method="phone_code")
    await _closed(harness.clients[0])
    assert harness.clients[0].logged_out and harness.late_scans == [CHAT_ID]


async def test_close_all_interrupts_attempts_and_drains(
    harness: Harness, clock: FrozenClock
) -> None:
    draining = await harness.begin()
    draining.qr_expires_at = clock() + timedelta(seconds=30)
    blocked = asyncio.Event()

    async def wait_forever(timeout_s: float) -> QrOutcome:
        await blocked.wait()
        return QrOutcome.authorized

    harness.clients[0].wait_qr = wait_forever  # type: ignore[method-assign]
    await harness.manager.end(CHAT_ID, "cancelled", drain=True)
    await harness.manager.begin(2002, user_id=USER_ID, purpose="reconnect", method="qr")
    await harness.manager.close_all()
    assert harness.manager.get(2002) is None
    assert [meta["outcome"] for _, meta in harness.records] == ["cancelled", "interrupted"]
    assert harness.records[1][1]["purpose"] == "reconnect"
    assert all(c.closed.is_set() for c in harness.clients)
    assert not harness.clients[0].logged_out and harness.late_scans == []
