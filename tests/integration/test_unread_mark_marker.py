"""UnreadMarker on its own (specs/009 T008, T016, T022): real Postgres, fake gateway.

Synthetic users, chats and message ids only.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import ChatKind, User, WorkChat
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import users as users_repo
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import (
    GatewayBusy,
    GatewayNotStarted,
    SessionRevoked,
    UnreadMarkClearedEvent,
)
from scribe.telegram.unread_mark import MarkTicket, UnreadMarker
from tests.conftest import FROZEN_NOW, LogCapture, MakeChat, MakeUser, next_telegram_id


async def _floor(
    session_factory: async_sessionmaker[AsyncSession], chat_id: uuid.UUID
) -> int | None:
    async with session_factory() as session:
        return await session.scalar(
            select(WorkChat.unread_mark_floor).where(WorkChat.id == chat_id)
        )


async def _set_floor(db: AsyncSession, chat: WorkChat, value: int | None) -> None:
    await db.execute(
        text("UPDATE work_chats SET unread_mark_floor = :v WHERE id = :id"),
        {"v": value, "id": chat.id},
    )
    await db.flush()


@pytest.fixture
def marker(
    session_factory: async_sessionmaker[AsyncSession], gateway: FakeMessagingGateway
) -> UnreadMarker:
    return UnreadMarker(session_factory, gateway, timeout_s=0.2)


# --- prepare --------------------------------------------------------------------------------------


async def test_prepare_returns_a_ticket_for_a_new_message_and_writes_nothing(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    ticket = await marker.prepare(user.id, chat.telegram_chat_id, 10)
    assert ticket == MarkTicket(user.id, chat.id, chat.telegram_chat_id, 10)
    assert await _floor(session_factory, chat.id) is None

    await _set_floor(db_session, chat, 9)
    assert await marker.prepare(user.id, chat.telegram_chat_id, 10) is not None
    assert await _floor(session_factory, chat.id) == 9
    assert gateway.mark_unread_calls == []


async def test_prepare_skips_disabled_setting_unknown_chat_and_seen(
    marker: UnreadMarker,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    disabled_chat = await make_chat(user, enabled=False)

    assert await marker.prepare(user.id, next_telegram_id(), 10) is None
    assert await marker.prepare(user.id, disabled_chat.telegram_chat_id, 10) is None
    other = await make_user()
    assert await marker.prepare(other.id, chat.telegram_chat_id, 10) is None  # not theirs

    await _set_floor(db_session, chat, 10)
    assert await marker.prepare(user.id, chat.telegram_chat_id, 10) is None
    await _set_floor(db_session, chat, 11)
    assert await marker.prepare(user.id, chat.telegram_chat_id, 10) is None

    await _set_floor(db_session, chat, None)
    user.mark_unread = False
    await db_session.flush()
    assert await marker.prepare(user.id, chat.telegram_chat_id, 10) is None

    reasons = [e["reason"] for e in log_capture.events("unread_mark_skipped")]
    assert reasons == ["not_work_chat"] * 3 + ["seen", "seen", "disabled"]


# --- commit ---------------------------------------------------------------------------------------


async def test_commit_moves_the_floor_and_marks_once(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    ticket = await marker.prepare(user.id, chat.telegram_chat_id, 10)
    await marker.commit(ticket)
    assert await _floor(session_factory, chat.id) == 10
    assert gateway.mark_unread_calls == [(user.id, chat.telegram_chat_id)]
    [event] = log_capture.events("unread_mark_set")
    assert event["cause"] == "read" and event["up_to"] == 10
    assert event["work_chat_id"] == str(chat.id)
    assert gateway.mark_read_calls == [] and gateway.send_text_calls == []

    await marker.commit(None)  # no-op
    assert len(gateway.mark_unread_calls) == 1
    # A re-read of the same message: prepare says no.
    assert await marker.prepare(user.id, chat.telegram_chat_id, 10) is None


async def test_commit_accepts_an_echo_of_the_agents_own_read(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    ticket = await marker.prepare(user.id, chat.telegram_chat_id, 10)
    await _set_floor(db_session, chat, 10)  # the echo: seen up to exactly 10
    await marker.commit(ticket)
    assert gateway.mark_unread_calls == [(user.id, chat.telegram_chat_id)]
    assert await _floor(session_factory, chat.id) == 10


async def test_commit_skips_when_the_user_read_further_meanwhile(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    ticket = await marker.prepare(user.id, chat.telegram_chat_id, 10)
    await _set_floor(db_session, chat, 12)
    await marker.commit(ticket)
    assert gateway.mark_unread_calls == []
    assert await _floor(session_factory, chat.id) == 12
    assert [e["reason"] for e in log_capture.events("unread_mark_skipped")] == ["seen"]


@pytest.mark.parametrize(
    "error",
    [GatewayBusy(30), GatewayNotStarted("no client"), SessionRevoked("revoked"), RuntimeError()],
)
async def test_gateway_failures_are_swallowed_and_logged(
    error: Exception,
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    gateway.fail_mark_unread_with = error
    await marker.commit(await marker.prepare(user.id, chat.telegram_chat_id, 10))
    [event] = log_capture.events("unread_mark_failed")
    assert event["error"] == type(error).__name__ and event["cause"] == "read"
    # The floor was committed first, so the failed mark is never retried by a re-read.
    assert await _floor(session_factory, chat.id) == 10
    assert await marker.prepare(user.id, chat.telegram_chat_id, 10) is None


async def test_a_hanging_mark_is_bounded_by_the_timeout(
    marker: UnreadMarker,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    gateway.mark_unread_delay_s = 10
    ticket = await marker.prepare(user.id, chat.telegram_chat_id, 10)
    started = time.monotonic()
    await marker.commit(ticket)
    assert time.monotonic() - started < 2
    [event] = log_capture.events("unread_mark_failed")
    assert event["error"] == "TimeoutError"
    assert gateway.mark_unread_calls == []


async def test_database_errors_are_swallowed(
    gateway: FakeMessagingGateway, log_capture: LogCapture
) -> None:
    def broken_factory() -> AsyncSession:
        raise RuntimeError("database down")

    marker = UnreadMarker(broken_factory, gateway)  # type: ignore[arg-type]
    user_id, chat_id = uuid.uuid4(), uuid.uuid4()
    assert await marker.prepare(user_id, 5, 10) is None
    await marker.commit(MarkTicket(user_id, chat_id, 5, 10))
    await marker.after_send(user_id, chat_id, 11)
    await marker.seen(user_id, 5, 12, source="read_elsewhere")
    assert gateway.mark_unread_calls == []
    assert len(log_capture.events("unread_mark_failed")) == 4


async def test_floor_is_committed_before_the_mark(
    committed_session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    async with committed_session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings, display_name="Synthetic"
        )
        chat = await chats_repo.upsert_work_chat(
            session,
            user.id,
            telegram_chat_id=next_telegram_id(),
            title="Synthetic chat",
            kind=ChatKind.private,
            enabled=True,
        )
        await session.commit()
    seen_in_hook: list[int | None] = []

    async def hook(user_id: uuid.UUID, chat_id: int) -> None:
        async with committed_session_factory() as fresh:
            seen_in_hook.append(
                await fresh.scalar(select(WorkChat.unread_mark_floor).where(WorkChat.id == chat.id))
            )

    gateway.on_mark_unread = hook
    marker = UnreadMarker(committed_session_factory, gateway)
    await marker.commit(await marker.prepare(user.id, chat.telegram_chat_id, 10))
    await marker.after_send(user.id, chat.id, 11)
    assert seen_in_hook == [10, 11]


# --- after_send -----------------------------------------------------------------------------------


async def test_after_send_marks_when_newer_and_skips_otherwise(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    await marker.after_send(user.id, chat.id, 20)
    assert gateway.mark_unread_calls == [(user.id, chat.telegram_chat_id)]
    assert await _floor(session_factory, chat.id) == 20
    [event] = log_capture.events("unread_mark_set")
    assert event["cause"] == "send" and event["up_to"] == 20

    await marker.after_send(user.id, chat.id, 20)  # same id again: strict
    await _set_floor(db_session, chat, 30)
    await marker.after_send(user.id, chat.id, 25)  # older than what the user saw
    assert len(gateway.mark_unread_calls) == 1
    reasons = [e["reason"] for e in log_capture.events("unread_mark_skipped")]
    assert reasons == ["not_newer", "not_newer"]


async def test_after_send_respects_the_setting_and_ownership(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user, other = await make_user(), await make_user()
    chat = await make_chat(user)
    await marker.after_send(other.id, chat.id, 20)  # not the owner
    user.mark_unread = False
    await db_session.flush()
    await marker.after_send(user.id, chat.id, 21)
    assert gateway.mark_unread_calls == []
    assert await _floor(session_factory, chat.id) is None
    reasons = [e["reason"] for e in log_capture.events("unread_mark_skipped")]
    assert reasons == ["not_work_chat", "disabled"]


async def test_after_send_failure_is_swallowed(
    marker: UnreadMarker,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    gateway.fail_mark_unread_with = GatewayBusy(30)
    await marker.after_send(user.id, chat.id, 20)
    [event] = log_capture.events("unread_mark_failed")
    assert event["cause"] == "send" and event["error"] == "GatewayBusy"


# --- seen -----------------------------------------------------------------------------------------


async def test_seen_raises_the_floor_and_never_lowers_it(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    await marker.seen(user.id, chat.telegram_chat_id, 15, source="read_elsewhere")
    assert await _floor(session_factory, chat.id) == 15
    await marker.seen(user.id, chat.telegram_chat_id, 12, source="manual_reply")
    assert await _floor(session_factory, chat.id) == 15
    sources = [(e["source"], e["up_to"]) for e in log_capture.events("unread_mark_seen")]
    assert sources == [("read_elsewhere", 15), ("manual_reply", 12)]
    assert gateway.mark_unread_calls == [] and gateway.mark_read_calls == []


async def test_seen_applies_even_with_the_setting_off(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(mark_unread=False)
    chat = await make_chat(user)
    await marker.seen(user.id, chat.telegram_chat_id, 15, source="read_elsewhere")
    assert await _floor(session_factory, chat.id) == 15


async def test_seen_ignores_unknown_and_disabled_chats(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    disabled = await make_chat(user, enabled=False)
    await marker.seen(user.id, disabled.telegram_chat_id, 15, source="read_elsewhere")
    await marker.seen(user.id, next_telegram_id(), 15, source="read_elsewhere")
    assert await _floor(session_factory, disabled.id) is None
    assert log_capture.events("unread_mark_seen") == []


async def test_mark_cleared_uses_the_newest_stored_message(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    other_chat = await make_chat(user)
    event = UnreadMarkClearedEvent(user_id=user.id, chat_id=chat.telegram_chat_id)

    await marker.handle_unread_mark_cleared(event)  # nothing stored yet: no change
    assert await _floor(session_factory, chat.id) is None

    for chat_row, message_id, outgoing in [
        (chat, 40, False),
        (chat, 41, True),
        (other_chat, 99, False),
    ]:
        await messages_repo.add_message(
            db_session,
            user.id,
            work_chat_id=chat_row.id,
            telegram_message_id=message_id,
            text="synthetic",
            sent_at=FROZEN_NOW + timedelta(seconds=message_id),
            is_outgoing=outgoing,
            sender_contact_id=None,
        )
    await marker.handle_unread_mark_cleared(event)
    assert await _floor(session_factory, chat.id) == 41
    [seen] = log_capture.events("unread_mark_seen")
    assert seen["source"] == "mark_cleared" and seen["up_to"] == 41
    assert gateway.mark_unread_calls == [] and gateway.mark_read_calls == []


async def test_user_row_is_untouched_by_marking(
    marker: UnreadMarker,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    await marker.commit(await marker.prepare(user.id, chat.telegram_chat_id, 10))
    async with session_factory() as session:
        assert await session.scalar(select(User.mark_unread).where(User.id == user.id)) is True
