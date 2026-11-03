"""The startup check (specs/011-release-notes US1 scenarios 1-6, US4, research R3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import ReleaseDeliveryState, ReleaseTrigger
from scribe.release_notes import announce
from scribe.release_notes.notes import read_notes
from tests.conftest import FrozenClock, LogCapture, MakeUser
from tests.integration.bot_support import offline_bot
from tests.integration.release_notes_support import (
    announcements,
    deliveries,
    make_sender,
    notes_settings,
    write_notes,
)


async def _announce(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    running: tuple[int, int, int] | None,
) -> object:
    return await announce.ensure_auto_announcement(
        session_factory, settings, now=clock.now, running=running
    )


async def test_first_deploy_announces_only_the_running_version(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    write_notes(tmp_path, "0.1.0", en="Old.", ru="Старое.")
    write_notes(tmp_path, "0.2.0", en="New.", ru="Новое.")
    alice = await make_user(language="en")
    bob = await make_user(language="ru")
    configured = notes_settings(settings, tmp_path)

    created = await _announce(session_factory, configured, clock, (0, 2, 0))

    assert created is not None
    (row,) = await announcements(db_session)
    assert (row.trigger, row.release_version, row.versions) == (
        ReleaseTrigger.auto,
        "0.2.0",
        ["0.2.0"],
    )
    assert row.notes_json == {"0.2.0": {"en": "New.", "ru": "Новое."}}
    assert row.recipients == 2 and row.finished_at is None
    rows = await deliveries(db_session)
    assert {d.user_id for d in rows} == {alice.id, bob.id}
    assert all(d.state is ReleaseDeliveryState.pending for d in rows)


async def test_newer_version_covers_every_version_since_the_last(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    await make_user()
    write_notes(tmp_path, "0.2.0", en="Two.")
    await _announce(session_factory, configured, clock, (0, 2, 0))
    write_notes(tmp_path, "0.3.0", en="Three.")
    write_notes(tmp_path, "0.3.1")  # no text: left out
    write_notes(tmp_path, "0.4.0", en="Four.")
    write_notes(tmp_path, "0.5.0", en="Future.")  # newer than the running version
    clock.advance(minutes=1)

    await _announce(session_factory, configured, clock, (0, 4, 0))

    first, second = await announcements(db_session)
    assert (second.release_version, second.versions) == ("0.4.0", ["0.3.0", "0.4.0"])
    assert set(second.notes_json) == {"0.3.0", "0.4.0"}
    assert second.recipients == 1


async def test_same_version_again_writes_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    await make_user()
    write_notes(tmp_path, "0.2.0", en="Two.")
    assert await _announce(session_factory, configured, clock, (0, 2, 0)) is not None
    assert await _announce(session_factory, configured, clock, (0, 2, 0)) is None
    assert len(await announcements(db_session)) == 1
    assert len(await deliveries(db_session)) == 1


async def test_racing_starts_write_one_row(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    write_notes(tmp_path, "0.2.0", en="Two.")
    results = await asyncio.gather(
        *(_announce(committed_session_factory, configured, clock, (0, 2, 0)) for _ in range(3))
    )
    assert sum(result is not None for result in results) == 1
    async with committed_session_factory() as session:
        assert len(await announcements(session)) == 1


async def test_version_without_notes_is_recorded_finished(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    await make_user()
    write_notes(tmp_path, "0.2.0", en="Two.")
    await _announce(session_factory, configured, clock, (0, 2, 0))
    clock.advance(minutes=1)

    await _announce(session_factory, configured, clock, (0, 2, 1))  # no 0.2.1.md

    _, row = await announcements(db_session)
    assert (row.release_version, row.versions, row.recipients) == ("0.2.1", [], 0)
    assert row.finished_at == clock.now
    assert len(await deliveries(db_session)) == 1  # only 0.2.0's


async def test_rollback_writes_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    await make_user()
    write_notes(tmp_path, "0.1.0", en="One.")
    write_notes(tmp_path, "0.2.0", en="Two.")
    await _announce(session_factory, configured, clock, (0, 2, 0))
    assert await _announce(session_factory, configured, clock, (0, 1, 0)) is None
    assert len(await announcements(db_session)) == 1


async def test_auto_off_writes_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    await make_user()
    write_notes(tmp_path, "0.2.0", en="Two.")
    configured = notes_settings(settings, tmp_path, release_notes_auto=False)
    assert await _announce(session_factory, configured, clock, (0, 2, 0)) is None
    assert await announcements(db_session) == []


async def test_invalid_notes_file_writes_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
    log_capture: LogCapture,
) -> None:
    await make_user()
    (tmp_path / "0.2.0.md").write_text("intro text\n## en\nSecret words.\n", encoding="utf-8")
    configured = notes_settings(settings, tmp_path)
    assert await _announce(session_factory, configured, clock, (0, 2, 0)) is None
    assert await announcements(db_session) == []
    (event,) = log_capture.events("release_notes_invalid")
    assert "0.2.0.md" in event["error"]
    assert "Secret words" not in log_capture.raw


async def test_unparsed_running_version_writes_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    await make_user()
    write_notes(tmp_path, "0.2.0", en="Two.")
    configured = notes_settings(settings, tmp_path)
    assert await _announce(session_factory, configured, clock, None) is None
    assert await announcements(db_session) == []


async def test_announced_log_has_no_text(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
    log_capture: LogCapture,
) -> None:
    await make_user()
    write_notes(tmp_path, "0.2.0", en="Unique announcement words.")
    await _announce(session_factory, notes_settings(settings, tmp_path), clock, (0, 2, 0))
    (event,) = log_capture.events("release_notes_announced")
    assert (event["release"], event["versions"], event["recipients"]) == ("0.2.0", ["0.2.0"], 1)
    assert "Unique announcement words" not in log_capture.raw


# --- US4: automatic sending off, and manual sends (T034) ---------------------------------------


async def test_auto_off_then_manual_send_is_delivered(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    user = await make_user(language="en")
    write_notes(tmp_path, "0.2.0", en="Two.", ru="Два.")
    configured = notes_settings(settings, tmp_path, release_notes_auto=False)
    bot, api = offline_bot()
    sender = make_sender(session_factory, bot, configured, clock)

    await sender.run_once()
    assert api.sent_texts == [] and await announcements(db_session) == []

    async with session_factory() as session:
        result = await announce.queue_manual(
            session, configured, read_notes(tmp_path / "0.2.0.md"), now=clock.now
        )
        await session.commit()
    assert result.recipients == 1
    await sender.run_once()
    assert api.sent_texts == ["What's new in Scribe 0.2.0\n\nTwo."]
    assert [int(r.chat_id) for r in api.of_type(SendMessage)] == [user.telegram_control_chat_id]


async def test_manual_send_before_the_start_covers_it(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    await make_user()
    write_notes(tmp_path, "0.3.0", en="Three.")
    configured = notes_settings(settings, tmp_path)
    async with session_factory() as session:
        await announce.queue_manual(
            session, configured, read_notes(tmp_path / "0.3.0.md"), now=clock.now
        )
        await session.commit()
    clock.advance(minutes=5)

    assert await _announce(session_factory, configured, clock, (0, 3, 0)) is None
    (row,) = await announcements(db_session)
    assert row.trigger is ReleaseTrigger.manual
