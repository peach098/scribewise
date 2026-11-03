"""ClientPool against Postgres with fake user clients (T019). Synthetic data, no Telegram."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.db.models import LoginMethod, SessionStatus, TelegramSession
from scribe.db.repos import users as users_repo
from scribe.i18n import Msg
from scribe.telegram.client_pool import (
    ERROR_NOTICE,
    REVOKED_NOTICE,
    ClientPool,
    KeyboardFactory,
)
from scribe.telegram.gateway import (
    Dialog,
    FoundMessage,
    GatewayNotStarted,
    IncomingMessage,
    MessagingGateway,
    ReadEvent,
    SessionRevoked,
)
from tests.conftest import FROZEN_NOW, MakeUser

BEHAVIOUR: dict[str, Exception | None] = {}
LOG_OUT_BEHAVIOUR: dict[str, Exception | None] = {}
CONNECTED_AT = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


class FakeUserClient:
    instances: list[FakeUserClient] = []

    def __init__(self, user_id: uuid.UUID, session_string: str, **kwargs: Any) -> None:
        self.user_id = user_id
        self.session_string = session_string
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.logged_out = False
        self.send_error: Exception | None = None
        FakeUserClient.instances.append(self)

    async def start(self) -> None:
        error = BEHAVIOUR.get(self.session_string)
        if error is not None:
            raise error
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def log_out(self) -> None:
        error = LOG_OUT_BEHAVIOUR.get(self.session_string)
        if error is not None:
            raise error
        self.logged_out = True

    async def list_dialogs(self) -> list[Dialog]:
        return [Dialog(chat_id=1, title="Synthetic", kind="private")]

    async def fetch_own_history(
        self, chat_id: int, since: datetime
    ) -> AsyncIterator[IncomingMessage]:
        if False:  # pragma: no cover - empty async generator
            yield

    async def mark_read(self, chat_id: int, up_to_message_id: int) -> None:
        return None

    async def send_text(
        self, chat_id: int, text: str, reply_to: int | None, *, random_id: int
    ) -> int:
        if self.send_error is not None:
            raise self.send_error
        return 99

    async def mark_unread(self, chat_id: int) -> None:
        return None

    async def search_chat(self, chat_id: int, query: str, *, limit: int = 20) -> list[FoundMessage]:
        return []

    async def get_messages(self, chat_id: int, message_ids: Sequence[int]) -> list[FoundMessage]:
        return []

    async def get_usernames(self, peer_ids: Sequence[int]) -> dict[int, str]:
        return {}


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeUserClient.instances.clear()
    BEHAVIOUR.clear()
    LOG_OUT_BEHAVIOUR.clear()


def _pool(
    session_factory: async_sessionmaker[AsyncSession],
    notices: list[tuple[uuid.UUID, str]],
    markups: list[InlineKeyboardMarkup | None] | None = None,
) -> ClientPool:
    async def notify(
        user_id: uuid.UUID, text: Msg, reply_markup: KeyboardFactory | None = None
    ) -> None:
        notices.append((user_id, text.render("en")))
        if markups is not None:
            markups.append(reply_markup("en") if reply_markup is not None else None)

    return ClientPool(
        session_factory,
        api_id=1,
        api_hash="hash",
        notifier=notify,
        user_client_factory=FakeUserClient,
    )


async def _store_session(db: AsyncSession, user_id: uuid.UUID, session_string: str) -> None:
    await users_repo.upsert_telegram_session(
        db, user_id, session_string, method=LoginMethod.qr, connected_at=CONNECTED_AT
    )


async def _status(db: AsyncSession, user_id: uuid.UUID) -> SessionStatus:
    query = select(TelegramSession.status).where(TelegramSession.user_id == user_id)
    return SessionStatus((await db.execute(query)).scalar_one())


async def test_start_all_starts_active_sessions_and_marks_failures(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    good, revoked, broken, flaky, inactive = [await make_user() for _ in range(5)]
    for user, session_string in [
        (good, "good"),
        (revoked, "revoked"),
        (broken, "broken"),
        (flaky, "flaky"),
        (inactive, "inactive"),
    ]:
        await users_repo.upsert_telegram_session(
            db_session,
            user.id,
            session_string,
            method=LoginMethod.phone_code,
            connected_at=FROZEN_NOW,
        )
    await users_repo.set_telegram_session_status(db_session, inactive.id, SessionStatus.revoked)
    await db_session.commit()
    BEHAVIOUR.update(
        revoked=SessionRevoked("AuthKeyUnregisteredError"),
        broken=RuntimeError("unexpected"),
        flaky=ConnectionError("network down"),
    )

    notices: list[tuple[uuid.UUID, str]] = []
    pool = _pool(session_factory, notices)
    assert await pool.start_all() == 1

    assert pool.running_user_ids == {good.id}
    assert {c.session_string for c in FakeUserClient.instances} == {
        "good",
        "revoked",
        "broken",
        "flaky",
    }  # the inactive session is never started
    assert await _status(db_session, revoked.id) == SessionStatus.revoked
    assert await _status(db_session, broken.id) == SessionStatus.error
    assert await _status(db_session, flaky.id) == SessionStatus.active  # transient: retry later
    assert sorted(notices, key=lambda n: n[1]) == sorted(
        [(revoked.id, REVOKED_NOTICE.render("en")), (broken.id, ERROR_NOTICE.render("en"))],
        key=lambda n: n[1],
    )

    await pool.start(good.id)  # idempotent
    assert len([c for c in FakeUserClient.instances if c.session_string == "good"]) == 1
    await pool.stop_all()
    assert pool.running_user_ids == set()


async def test_revocation_during_operation_stops_client_and_notifies(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await users_repo.upsert_telegram_session(
        db_session, user.id, "good", method=LoginMethod.phone_code, connected_at=FROZEN_NOW
    )
    await db_session.commit()
    notices: list[tuple[uuid.UUID, str]] = []
    pool = _pool(session_factory, notices)
    gateway: MessagingGateway = pool  # the pool is the production gateway
    await gateway.start(user.id)

    assert await gateway.send_text(user.id, 5, "Synthetic reply", None, random_id=7) == 99
    assert (await gateway.list_dialogs(user.id))[0].title == "Synthetic"

    client = FakeUserClient.instances[0]
    client.send_error = SessionRevoked("SessionRevokedError")
    with pytest.raises(SessionRevoked):
        await gateway.send_text(user.id, 5, "Synthetic reply", None, random_id=7)
    assert client.stopped
    assert not pool.is_running(user.id)
    assert await _status(db_session, user.id) == SessionStatus.revoked
    assert notices == [(user.id, REVOKED_NOTICE.render("en"))]
    with pytest.raises(GatewayNotStarted):
        await gateway.mark_read(user.id, 5, 1)


async def test_revoked_notice_points_to_telegram_with_reconnect_button(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await _store_session(db_session, user.id, "revoked")
    await db_session.commit()
    BEHAVIOUR.update(revoked=SessionRevoked("AuthKeyUnregisteredError"))
    notices: list[tuple[uuid.UUID, str]] = []
    markups: list[InlineKeyboardMarkup | None] = []
    pool = _pool(session_factory, notices, markups)

    await pool.start(user.id)

    ((_, text),) = notices
    assert "/telegram" in text and "/start" not in text
    (markup,) = markups
    assert markup is not None
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["tgreconnect:0"]


async def test_user_clients_receive_device_params(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await _store_session(db_session, user.id, "good")
    await db_session.commit()
    pool = ClientPool(
        session_factory,
        api_id=1,
        api_hash="hash",
        user_client_factory=FakeUserClient,
        device_model="Synthetic Device",
        system_version="synthetic-os",
        app_version="0.0.0-test",
    )

    await pool.start(user.id)

    (client,) = FakeUserClient.instances
    assert client.kwargs["device_model"] == "Synthetic Device"
    assert client.kwargs["system_version"] == "synthetic-os"
    assert client.kwargs["app_version"] == "0.0.0-test"
    await pool.stop_all()


async def test_events_fan_out_to_all_handlers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pool = _pool(session_factory, [])
    seen: list[str] = []

    async def first(event: ReadEvent) -> None:
        seen.append("first")
        raise RuntimeError("handler bug must not break the others")

    async def second(event: ReadEvent) -> None:
        seen.append("second")

    pool.on_read_elsewhere(first)
    pool.on_read_elsewhere(second)
    await pool._dispatch_read(ReadEvent(user_id=uuid.uuid4(), chat_id=1, max_message_id=2))
    assert seen == ["first", "second"]


async def test_log_out_running_client_removes_it_from_the_pool(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await users_repo.upsert_telegram_session(
        db_session, user.id, "good", method=LoginMethod.phone_code, connected_at=FROZEN_NOW
    )
    await db_session.commit()
    notices: list[tuple[uuid.UUID, str]] = []
    pool = _pool(session_factory, notices)
    gateway: MessagingGateway = pool
    await gateway.start(user.id)

    await gateway.log_out(user.id)

    (client,) = FakeUserClient.instances
    assert client.logged_out
    assert not pool.is_running(user.id)
    assert notices == []


async def test_log_out_without_running_client_uses_temporary_client(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user, no_session = await make_user(), await make_user()
    await users_repo.upsert_telegram_session(
        db_session, user.id, "stored", method=LoginMethod.phone_code, connected_at=FROZEN_NOW
    )
    await users_repo.set_telegram_session_status(db_session, user.id, SessionStatus.error)
    await db_session.commit()
    notices: list[tuple[uuid.UUID, str]] = []
    pool = _pool(session_factory, notices)

    await pool.log_out(user.id)
    await pool.log_out(no_session.id)  # nothing stored: nothing to do

    (client,) = FakeUserClient.instances
    assert client.session_string == "stored"  # decrypted from the stored row
    assert client.logged_out and not client.started
    assert not pool.is_running(user.id)
    assert notices == []


async def test_log_out_revoked_session_counts_as_success_without_notice(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user, flaky = await make_user(), await make_user()
    await users_repo.upsert_telegram_session(
        db_session, user.id, "revoked", method=LoginMethod.phone_code, connected_at=FROZEN_NOW
    )
    await users_repo.upsert_telegram_session(
        db_session, flaky.id, "flaky", method=LoginMethod.phone_code, connected_at=FROZEN_NOW
    )
    await db_session.commit()
    LOG_OUT_BEHAVIOUR.update(
        revoked=SessionRevoked("AuthKeyUnregisteredError"),
        flaky=ConnectionError("network down"),
    )
    notices: list[tuple[uuid.UUID, str]] = []
    pool = _pool(session_factory, notices)

    await pool.log_out(user.id)
    assert notices == []
    assert await _status(db_session, user.id) == SessionStatus.active  # the caller decides

    with pytest.raises(ConnectionError):
        await pool.log_out(flaky.id)
    assert notices == []


async def test_suspended_users_are_not_started(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    """specs/005 T021: a user whose access ended keeps the session but gets no client."""
    serving = await make_user()
    suspended = await make_user(access_suspended_at=FROZEN_NOW)
    await _store_session(db_session, serving.id, "serving")
    await _store_session(db_session, suspended.id, "suspended")
    await db_session.commit()
    notices: list[tuple[uuid.UUID, str]] = []
    pool = _pool(session_factory, notices)

    assert await pool.start_all() == 1
    assert pool.running_user_ids == {serving.id}

    await pool.start(suspended.id)
    assert not pool.is_running(suspended.id)
    assert [c.session_string for c in FakeUserClient.instances] == ["serving"]
    assert await _status(db_session, suspended.id) == SessionStatus.active  # left untouched
    assert notices == []
