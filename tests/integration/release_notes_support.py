"""Helpers for the release notes tests (specs/011-release-notes): a notes directory, a fake
sleep that moves the frozen clock, access fixtures and a sender with an injected running
version. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal

from aiogram import Bot
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    BetaEntry,
    ReleaseAnnouncement,
    ReleaseDelivery,
    Subscription,
    User,
)
from scribe.release_notes.sender import ReleaseNotesSender
from scribe.release_notes.version import Version
from tests.conftest import FrozenClock, MakeUser, next_telegram_id
from tests.integration.bot_support import RecordingSession

AccessKind = Literal["beta", "subscribed", "operator", "suspended", "none"]


def write_notes(
    directory: Path, version: str, *, en: str | None = None, ru: str | None = None
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    sections = [f"## {lang}\n\n{text}\n" for lang, text in (("en", en), ("ru", ru)) if text]
    path = directory / f"{version}.md"
    path.write_text("<!-- synthetic notes -->\n\n" + "\n".join(sections), encoding="utf-8")
    return path


def notes_settings(settings: Settings, directory: Path, **updates: object) -> Settings:
    return settings.model_copy(update={"release_notes_dir": directory, **updates})


class FakeSleep:
    """Records each delay and moves the frozen clock forward by it."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.clock.advance(seconds=seconds)

    @property
    def total(self) -> float:
        return sum(self.delays)


def make_sender(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    clock: FrozenClock,
    *,
    running: Version | None = (0, 2, 0),
    sleep: FakeSleep | None = None,
) -> ReleaseNotesSender:
    return ReleaseNotesSender(
        session_factory,
        bot,
        settings,
        clock=clock,
        sleep=sleep or FakeSleep(clock),
        running=lambda: running,
    )


async def make_access_user(
    db: AsyncSession,
    make_user: MakeUser,
    clock: FrozenClock,
    kind: AccessKind,
    *,
    lang: str = "en",
    telegram_id: int | None = None,
) -> User:
    """An account with the given access (operators need the id in the settings)."""
    telegram_id = telegram_id or next_telegram_id()
    user = await make_user(control_chat_id=telegram_id, language=lang)
    now = clock.now
    if kind in ("beta", "suspended"):
        db.add(BetaEntry(telegram_id=telegram_id, added_by="operator", added_at=now))
    if kind == "subscribed":
        db.add(
            Subscription(
                telegram_id=telegram_id,
                starts_at=now - timedelta(days=1),
                ends_at=now + timedelta(days=30),
                granted_by="operator",
            )
        )
    if kind == "suspended":
        user.access_suspended_at = now
    await db.flush()
    return user


def sends_to(api: RecordingSession, chat_id: int) -> list[SendMessage]:
    return [r for r in api.of_type(SendMessage) if int(r.chat_id) == chat_id]


async def announcements(db: AsyncSession) -> list[ReleaseAnnouncement]:
    result = await db.scalars(
        select(ReleaseAnnouncement)
        .order_by(ReleaseAnnouncement.created_at)
        .execution_options(populate_existing=True)
    )
    return list(result.all())


async def deliveries(db: AsyncSession) -> list[ReleaseDelivery]:
    result = await db.scalars(
        select(ReleaseDelivery)
        .order_by(ReleaseDelivery.created_at)
        .execution_options(populate_existing=True)
    )
    return list(result.all())


async def delivery_of(db: AsyncSession, user: User) -> ReleaseDelivery:
    (row,) = [d for d in await deliveries(db) if d.user_id == user.id]
    return row
