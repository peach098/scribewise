"""Who gets which notes: the startup check, recipients, manual sends and status lines
(specs/011-release-notes research R3, R4, R10).

- :func:`ensure_auto_announcement`: once per start. When the running version is newer than the
  last handled one, one ``auto`` announcement covers every version with notes in
  ``(last handled, running]`` (only the running version on the very first run, FR-007), with a
  ``pending`` delivery per recipient. A version without notes is still recorded, with no
  versions and finished at once (FR-005). Restarts, same-version redeploys and rollbacks write
  nothing.
- :func:`recipients`: every account with access now (gate on: not suspended and
  :func:`scribe.access.policy.status` has access; gate off: every account). ``users.state`` is
  ignored. People without an account are never candidates.
- :func:`queue_manual`: the CLI's ``release-notes send``, in the caller's transaction.

Logs carry versions and counts only, never the notes text (FR-023).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import repo as access_repo
from scribe.config import Settings
from scribe.db.models import (
    ReleaseAnnouncement,
    ReleaseDeliveryState,
    ReleaseTrigger,
    User,
)
from scribe.i18n import normalize_lang
from scribe.logging import get_logger
from scribe.release_notes import repo
from scribe.release_notes import version as versions
from scribe.release_notes.notes import NotesError, ReleaseNotes, load_notes, snapshot
from scribe.release_notes.version import Version

log = get_logger("scribe.release_notes")

TIME_FORMAT = "%Y-%m-%d %H:%M"


async def recipients(session: AsyncSession, settings: Settings, now: datetime) -> list[User]:
    """Accounts that get an announcement created now (research R4), oldest first."""
    users = list((await session.scalars(select(User).order_by(User.created_at, User.id))).all())
    if not settings.access_gate:
        return users
    candidates = [user for user in users if user.access_suspended_at is None]
    statuses = await access_repo.bulk_status(session, settings, candidates, now=now)
    return [user for user in candidates if statuses[user.id].has_access]


async def ensure_auto_announcement(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime,
    running: Version | None,
) -> ReleaseAnnouncement | None:
    """The startup check (plan.md "Startup check"); the new announcement, or ``None``."""
    if not settings.release_notes_auto or running is None:
        return None
    async with session_factory() as session:
        last = await repo.last_handled_version(session)
        if last is not None and running <= last:
            return None
        try:
            notes = load_notes(settings.release_notes_dir)
        except NotesError as exc:  # names the file and the problem, never the text
            log.error("release_notes_invalid", error=str(exc))
            return None
        covered = [
            item
            for version, item in sorted(notes.items())
            if item.has_text and (version == running if last is None else last < version <= running)
        ]
        announcement = await repo.create_announcement(
            session,
            release_version=running,
            covered=[item.version for item in covered],
            notes_json=snapshot(covered),
            trigger=ReleaseTrigger.auto,
            now=now,
        )
        if announcement is None:  # another start of this version won the race
            await session.rollback()
            return None
        users = await recipients(session, settings, now) if covered else []
        announcement.recipients = await repo.add_deliveries(
            session, announcement, [user.id for user in users], now
        )
        if not announcement.recipients:
            announcement.finished_at = now
        await session.commit()
    log.info(
        "release_notes_announced",
        release=announcement.release_version,
        versions=list(announcement.versions),
        recipients=announcement.recipients,
        trigger=announcement.trigger.value,
    )
    return announcement


@dataclass(frozen=True)
class QueueResult:
    recipients: int
    by_lang: dict[str, int]
    already: int  # users with access who already received the version
    announcement: ReleaseAnnouncement | None  # None when nobody is left


async def queue_manual(
    session: AsyncSession,
    settings: Settings,
    notes: ReleaseNotes,
    *,
    now: datetime,
) -> QueueResult:
    """A ``manual`` announcement of exactly ``notes.version`` for every recipient who has not
    received it yet (research R10). Nothing is written when nobody is left. Never commits: the
    caller commits, or rolls back for a dry run."""
    due: list[User] = []
    already = 0
    for user in await recipients(session, settings, now):
        if notes.version in await repo.received_versions(session, user.id):
            already += 1
        else:
            due.append(user)
    by_lang = {"en": 0, "ru": 0}
    for user in due:
        by_lang[normalize_lang(user.language)] += 1
    announcement = None
    if due:
        announcement = await repo.create_announcement(
            session,
            release_version=notes.version,
            covered=[notes.version],
            notes_json=snapshot([notes]),
            trigger=ReleaseTrigger.manual,
            now=now,
        )
        assert announcement is not None  # manual rows have no unique index
        announcement.recipients = await repo.add_deliveries(
            session, announcement, [user.id for user in due], now
        )
        await session.flush()
    # Not logged here: a dry run rolls this back, and the CLI prints the result itself.
    return QueueResult(len(due), by_lang, already, announcement)


def _time(moment: datetime | None) -> str:
    return moment.strftime(TIME_FORMAT) if moment is not None else "-"


async def status_lines(
    session: AsyncSession, *, show_all: bool, running: Version | None, settings: Settings
) -> list[str]:
    """``release-notes status`` (contracts/operator-cli.md)."""
    last = await repo.last_handled_version(session)
    lines = [
        f"running={versions.format(running) if running is not None else '-'} "
        f"last_handled={versions.format(last) if last is not None else '-'} "
        f"auto={'on' if settings.release_notes_auto else 'off'}"
    ]
    rows = await repo.announcements(session, limit=None if show_all else 1)
    if not rows:
        return [*lines, "no announcements"]
    for row in rows:
        counts = await repo.counts_by_state(session, row.id)
        pending = counts[ReleaseDeliveryState.pending] + counts[ReleaseDeliveryState.sending]
        lines.append(
            f"{_time(row.created_at)} {row.trigger.value} release={row.release_version} "
            f"versions={','.join(row.versions) or '-'} recipients={row.recipients} "
            f"sent={counts[ReleaseDeliveryState.sent]} pending={pending} "
            f"blocked={counts[ReleaseDeliveryState.blocked]} "
            f"failed={counts[ReleaseDeliveryState.failed]} "
            f"skipped={counts[ReleaseDeliveryState.skipped]} "
            f"unknown={counts[ReleaseDeliveryState.unknown]} finished={_time(row.finished_at)}"
        )
    return lines
