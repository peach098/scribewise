"""Profile jobs (FR-037): the weekly ``refresh_profiles`` scheduled job and its manual CLI.

- :func:`register_profile_jobs` registers ``refresh_profiles`` (weekly, Sunday 02:00 UTC) in the
  ``scheduled_jobs`` mechanism (``Scheduler.register_job``). Each run refreshes every set-up user
  (``style.profiles.refresh_profiles``) and posts the "🔄 Profiles refreshed … Changes: …" note
  with ↩️ Undo (``undo_refresh:<batch_id>``) in the user's control chat. A user whose run of this
  week completed (a ``profile_refreshed`` record since the scheduled time, not ``pending``) is
  skipped, so a run interrupted by a restart (requeued at startup by ``Scheduler.ensure_jobs``)
  resumes with the users not refreshed yet.
- It also registers ``reencrypt_profiles`` (``retention/reencrypt.py``): due at once the first
  time the service starts with it, then daily at 04:00 UTC (idempotent; finds nothing once the
  legacy plaintext rows are rewritten). Columns encrypted later (issue #57) are picked up by its
  next daily run, or at once with the CLI below.
- It registers ``reencrypt_all`` (``retention/reencrypt_all.py``, issues #58, #59): daily at
  04:30 UTC, it rewrites every encrypted value still under a previous key or in the legacy
  format (finds nothing when there is none), so a key rotation completes by itself within a day.
- ``python -m scribe.jobs refresh_profiles --user <user id>`` runs the refresh for one user now
  (quickstart US4 step 6), ``python -m scribe.jobs reencrypt_profiles`` the re-encryption of
  legacy plaintext rows, and ``python -m scribe.jobs reencrypt_all [--check]`` the full
  re-encryption (``--check``: count only; exit status 1 while anything is pending or failed),
  with the service's configuration (``SCRIBE_*`` environment).

Logs carry ids and counts only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, time, timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.crypto import get_keyring
from scribe.db.models import ActivityKind, ActivityRecord, User, UserState
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.llm.registry import LLMRouter
from scribe.logging import get_logger
from scribe.retention.reencrypt import reencrypt_legacy_rows
from scribe.retention.reencrypt_all import BATCH_SIZE as REENCRYPT_ALL_BATCH
from scribe.retention.reencrypt_all import reencrypt_all
from scribe.scheduling.scheduler import Daily, Scheduler, Weekly, previous_run
from scribe.style.profiles import RefreshResult, refresh_note, refresh_profiles
from scribe.telegram.control_bot.render import button, format_callback, keyboard

log = get_logger("scribe.jobs")

REFRESH_JOB = "refresh_profiles"
REFRESH_SCHEDULE = Weekly(weekday=6, at=time(2, 0))  # Sunday, UTC, low-traffic hour
REFRESH_CONCURRENCY = 4
REFRESHED_STATES = (UserState.active, UserState.paused, UserState.out_of_office)
REENCRYPT_JOB = "reencrypt_profiles"
REENCRYPT_SCHEDULE = Daily(at=time(4, 0))  # UTC; due at once the first time (run_at_start)
REENCRYPT_ALL_JOB = "reencrypt_all"
REENCRYPT_ALL_SCHEDULE = Daily(at=time(4, 30))  # UTC


async def refresh_user(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    bot: Bot | None,
    user_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Clock = utcnow,
) -> RefreshResult:
    """Refresh one user's profiles and post the note (when something changed and a bot is set)."""
    result = await refresh_profiles(
        session_factory, llm_router, user_id, settings=settings, clock=clock
    )
    if result.batch_id is None or bot is None:
        return result
    async with session_factory() as session:
        user = await users_repo.get_user(session, user_id)
    if user is not None:
        lang = user.language
        await bot.send_message(
            user.telegram_control_chat_id,
            refresh_note(result, lang),
            reply_markup=keyboard(
                [[button(t("btn.undo", lang), format_callback("undo_refresh", result.batch_id))]]
            ),
        )
    return result


async def refresh_all_users(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    bot: Bot | None,
    *,
    settings: Settings,
    clock: Clock = utcnow,
    concurrency: int = REFRESH_CONCURRENCY,
) -> int:
    """The weekly job: every user past setup whose refresh of this week did not complete yet
    (so a run interrupted by a restart resumes); one user's failure never stops the others."""
    run_start = _run_start(clock())
    async with session_factory() as session:
        done = (
            select(ActivityRecord.user_id)
            .where(
                ActivityRecord.kind == ActivityKind.profile_refreshed,
                ActivityRecord.at >= run_start,
                ~ActivityRecord.meta_json.contains({"pending": True}),
            )
            .distinct()
        )
        result = await session.execute(
            select(User.id).where(
                User.state.in_(REFRESHED_STATES),
                User.access_suspended_at.is_(None),  # access ended (specs/005)
                User.id.not_in(done),
            )
        )
        user_ids = list(result.scalars().all())
    semaphore = asyncio.Semaphore(concurrency)
    refreshed = 0

    async def one(user_id: uuid.UUID) -> None:
        nonlocal refreshed
        async with semaphore:
            try:
                outcome = await refresh_user(
                    session_factory, llm_router, bot, user_id, settings=settings, clock=clock
                )
            except Exception as exc:
                if not await _user_exists(session_factory, user_id):  # deleted meanwhile
                    log.info("profile_refresh_abandoned", user_id=str(user_id))
                    return
                log.error("profile_refresh_failed", user_id=str(user_id), error=type(exc).__name__)
                return
            refreshed += int(outcome.changed)

    await asyncio.gather(*(one(user_id) for user_id in user_ids))
    log.info("profile_refresh_job_finished", users=len(user_ids), refreshed=refreshed)
    return refreshed


def _run_start(now: datetime) -> datetime:
    """The scheduled time of the weekly run ``now`` belongs to (the latest one at or before)."""
    return previous_run(REFRESH_SCHEDULE, now + timedelta(microseconds=1))


async def _user_exists(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> bool:
    try:
        async with session_factory() as session:
            return await users_repo.get_user(session, user_id) is not None
    except Exception:
        return True  # unknown: report the failure


def register_profile_jobs(
    scheduler: Scheduler,
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    bot: Bot,
    settings: Settings,
    clock: Clock = utcnow,
) -> None:
    async def job() -> None:
        await refresh_all_users(session_factory, llm_router, bot, settings=settings, clock=clock)

    async def reencrypt() -> None:
        if get_keyring().strict:  # plaintext is refused then, never adopted
            return
        await reencrypt_legacy_rows(session_factory)

    async def reencrypt_everything() -> None:
        await reencrypt_all(session_factory)

    scheduler.register_job(REFRESH_JOB, REFRESH_SCHEDULE, job)
    scheduler.register_job(REENCRYPT_JOB, REENCRYPT_SCHEDULE, reencrypt, run_at_start=True)
    scheduler.register_job(REENCRYPT_ALL_JOB, REENCRYPT_ALL_SCHEDULE, reencrypt_everything)


# --- CLI ----------------------------------------------------------------------------------------


async def _run_reencrypt(settings: Settings) -> int:
    from scribe.crypto import configure_keyring, keyring_from_settings
    from scribe.db.session import create_engine_from_settings, create_session_factory

    configure_keyring(keyring_from_settings(settings))
    engine = create_engine_from_settings(settings)
    try:
        stats = await reencrypt_legacy_rows(create_session_factory(engine))
    finally:
        await engine.dispose()
    counts = " ".join(f"{name}={count}" for name, count in asdict(stats).items())
    print(f"{REENCRYPT_JOB}: {counts}")
    return 0


async def _run_reencrypt_all(settings: Settings, *, check: bool, batch_size: int) -> int:
    from scribe.crypto import configure_keyring, keyring_from_settings
    from scribe.db.session import create_engine_from_settings, create_session_factory

    configure_keyring(keyring_from_settings(settings))
    engine = create_engine_from_settings(settings)
    try:
        report = await reencrypt_all(
            create_session_factory(engine), batch_size=batch_size, dry_run=check
        )
    finally:
        await engine.dispose()
    print(f"{REENCRYPT_ALL_JOB} ({'check' if check else 'run'}, key {settings.master_key_id}):")
    for line in report.lines():
        print(f"  {line}")
    if report.failed:
        print(
            f"{REENCRYPT_ALL_JOB}: {report.failed} value(s) could not be read; see the "
            "reencrypt_value_failed log lines (table, column, row id)",
            file=sys.stderr,
        )
    return 1 if report.failed or (check and report.pending) else 0


async def _run_refresh(settings: Settings, user_id: uuid.UUID) -> int:
    import os

    from scribe.__main__ import ConfigError, build_llm_router
    from scribe.crypto import configure_keyring, keyring_from_settings
    from scribe.db.repos.llm_spend import SpendRecorder
    from scribe.db.session import create_engine_from_settings, create_session_factory
    from scribe.telegram.control_bot.bot import create_bot

    configure_keyring(keyring_from_settings(settings))
    try:
        llm_router = build_llm_router(settings, os.environ)
    except ConfigError as exc:
        print(f"scribe.jobs: invalid LLM configuration: {exc}", file=sys.stderr)
        return 2
    engine = create_engine_from_settings(settings)
    bot = create_bot(settings)
    try:
        session_factory = create_session_factory(engine)
        llm_router.spend_sink = SpendRecorder(session_factory)  # issue #67
        async with session_factory() as session:
            if await users_repo.get_user(session, user_id) is None:
                print(f"scribe.jobs: no user {user_id}", file=sys.stderr)
                return 1
        result = await refresh_user(session_factory, llm_router, bot, user_id, settings=settings)
    finally:
        await bot.session.close()
        await engine.dispose()
    print(
        "refresh_profiles: "
        + (
            f"global={result.global_refreshed} contacts={result.contacts_refreshed} "
            f"retired={result.retired} batch_id={result.batch_id}"
            if result.changed
            else "nothing new to learn from"
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scribe.jobs")
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser(REFRESH_JOB, help="refresh one user's style profiles now")
    refresh.add_argument("--user", required=True, type=uuid.UUID, help="users.id")
    commands.add_parser(REENCRYPT_JOB, help="re-encrypt rows stored before encryption (once)")
    everything = commands.add_parser(
        REENCRYPT_ALL_JOB, help="re-encrypt every encrypted value to the current key and format"
    )
    everything.add_argument(
        "--check", action="store_true", help="write nothing; count what is pending or unreadable"
    )
    everything.add_argument("--batch-size", type=int, default=REENCRYPT_ALL_BATCH)
    args = parser.parse_args(argv)

    from scribe.config import get_settings
    from scribe.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, libraries_debug=settings.log_libraries_debug)
    if args.command == REENCRYPT_JOB:
        return asyncio.run(_run_reencrypt(settings))
    if args.command == REENCRYPT_ALL_JOB:
        return asyncio.run(
            _run_reencrypt_all(settings, check=args.check, batch_size=max(1, args.batch_size))
        )
    return asyncio.run(_run_refresh(settings, args.user))


if __name__ == "__main__":
    sys.exit(main())
