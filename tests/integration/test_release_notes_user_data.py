"""Release notes in /my_data, the export and /delete_my_data (specs/011-release-notes FR-024,
research R11)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.release_notes.announce import ensure_auto_announcement
from scribe.retention.user_data import data_summary, delete_user_data, export_user_data
from scribe.telegram.control_bot.account_command import summary_text
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import offline_bot
from tests.integration.release_notes_support import (
    announcements,
    deliveries,
    make_sender,
    notes_settings,
    write_notes,
)


async def test_export_summary_and_deletion(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    user = await make_user(language="ru")
    other = await make_user()
    write_notes(tmp_path, "0.2.0", en="Two.", ru="Два.")
    configured = notes_settings(settings, tmp_path)
    await ensure_auto_announcement(session_factory, configured, now=clock.now, running=(0, 2, 0))
    bot, _ = offline_bot()
    await make_sender(session_factory, bot, configured, clock, running=None).drain()

    async with session_factory() as session:
        export = await export_user_data(session, user.id, now=clock.now)
        summary = await data_summary(session, user.id, message_retention_days=30, now=clock.now)
    (item,) = export["release_notes"]
    assert item["versions"] == ["0.2.0"] and item["state"] == "sent"
    assert item["delivered_at"] is not None
    assert set(item) == {"versions", "state", "delivered_at"}  # no text, no chat id
    assert summary.release_notes == 1
    assert "📣 Получено обновлений: 1" in summary_text(summary, "UTC", 30, "ru")
    assert "📣 Release notes received: 1" in summary_text(summary, "UTC", 30, "en")

    async with session_factory() as session:
        await delete_user_data(session, user.id)
        await session.commit()
    assert [d.user_id for d in await deliveries(db_session)] == [other.id]
    assert len(await announcements(db_session)) == 1  # kept: no personal data


async def test_summary_without_release_notes(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    async with session_factory() as session:
        export = await export_user_data(session, user.id, now=clock.now)
        summary = await data_summary(session, user.id, message_retention_days=30)
    assert export["release_notes"] == []
    assert summary.release_notes == 0
