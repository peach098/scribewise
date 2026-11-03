"""Delivering release notes (specs/011-release-notes US1, US2, US3; research R6-R9)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from aiogram.client.default import Default
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    BetaEntry,
    ReleaseAnnouncement,
    ReleaseDelivery,
    ReleaseDeliveryState,
    ReleaseTrigger,
    ScheduledJob,
    User,
)
from scribe.db.repos import users as users_repo
from scribe.release_notes import repo, version
from scribe.release_notes.announce import ensure_auto_announcement
from scribe.release_notes.notes import MAX_SECTION_CHARS
from scribe.release_notes.sender import (
    JOB_NAME,
    recover_interrupted,
    register_release_notes_job,
)
from scribe.scheduling.scheduler import Every, Scheduler
from tests.conftest import FrozenClock, LogCapture, MakeUser, next_telegram_id
from tests.integration.bot_support import offline_bot
from tests.integration.release_notes_support import (
    FakeSleep,
    announcements,
    deliveries,
    delivery_of,
    make_access_user,
    make_sender,
    notes_settings,
    sends_to,
    write_notes,
)

TEXT_EN = "- Synthetic feature, release words."
TEXT_RU = "- Синтетическая функция."
METHOD = SendMessage(chat_id=1, text="x")


async def _announce(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    await ensure_auto_announcement(session_factory, settings, now=clock.now, running=(0, 2, 0))


async def _manual(
    session_factory: async_sessionmaker[AsyncSession],
    users: list[User],
    notes: dict[str, dict[str, str]],
    clock: FrozenClock,
) -> ReleaseAnnouncement:
    """An announcement of ``notes`` (``{"0.2.0": {"en": ...}}``) for ``users``."""
    async with session_factory() as session:
        row = await repo.create_announcement(
            session,
            release_version=max(version.parse(v) for v in notes),
            covered=[version.parse(v) for v in notes],
            notes_json=notes,
            trigger=ReleaseTrigger.manual,
            now=clock.now,
        )
        assert row is not None
        row.recipients = await repo.add_deliveries(session, row, [u.id for u in users], clock.now)
        await session.commit()
    return row


# --- US1: the happy path (T016) -----------------------------------------------------------------


async def test_each_user_gets_one_silent_plain_message_in_their_language(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
    log_capture: LogCapture,
) -> None:
    alice = await make_user(language="en")
    boris = await make_user(language="ru")
    write_notes(tmp_path, "0.2.0", en=TEXT_EN, ru=TEXT_RU)
    configured = notes_settings(settings, tmp_path)
    bot, api = offline_bot()
    sender = make_sender(session_factory, bot, configured, clock)

    await sender.run_once()

    (to_alice,) = sends_to(api, alice.telegram_control_chat_id)
    (to_boris,) = sends_to(api, boris.telegram_control_chat_id)
    assert to_alice.text == f"What's new in Scribe 0.2.0\n\n{TEXT_EN}"
    assert to_boris.text == f"Что нового в Scribe 0.2.0\n\n{TEXT_RU}"
    for request in (to_alice, to_boris):
        assert request.disable_notification is True
        assert request.parse_mode is None or isinstance(request.parse_mode, Default)
        assert request.reply_markup is None
    assert bot.default.parse_mode is None  # nothing in the text is interpreted

    rows = await deliveries(db_session)
    assert {(d.state, d.parts_sent, d.parts_total) for d in rows} == {
        (ReleaseDeliveryState.sent, 1, 1)
    }
    assert all(d.delivered_at is not None for d in rows)
    (announcement,) = await announcements(db_session)
    assert announcement.finished_at is not None

    api.clear()
    await sender.run_once()  # nothing left, and no second startup check
    await make_sender(session_factory, bot, configured, clock).run_once()  # a restart
    assert api.sent_texts == []

    assert "release words" not in log_capture.raw and "Синтетическая" not in log_capture.raw
    assert str(alice.telegram_control_chat_id) not in log_capture.raw
    results = [e["result"] for e in log_capture.events("release_notes_delivery")]
    assert results == ["sent", "sent"]
    (finished,) = log_capture.events("release_notes_finished")
    assert finished["sent"] == 2


async def test_two_versions_in_one_digest(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user(language="en")
    await _manual(
        session_factory, [user], {"0.2.0": {"en": "Two."}, "0.3.0": {"en": "Three."}}, clock
    )
    bot, api = offline_bot()
    await make_sender(session_factory, bot, settings, clock, running=None).drain()
    assert api.sent_texts == [
        "What's new in Scribe 0.2.0\n\nTwo.\n\nWhat's new in Scribe 0.3.0\n\nThree."
    ]


async def test_versions_already_received_are_left_out(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user(language="en")
    bot, api = offline_bot()
    sender = make_sender(session_factory, bot, settings, clock, running=None)
    await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}}, clock)
    await sender.drain()
    clock.advance(minutes=1)
    await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}, "0.3.0": {"en": "3"}}, clock)
    await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}}, clock)
    api.clear()
    await sender.drain()
    assert api.sent_texts == ["What's new in Scribe 0.3.0\n\n3"]
    reasons = sorted(str(d.skip_reason) for d in await deliveries(db_session))
    assert reasons == ["None", "None", "already_received"]


# --- US2: access is checked again before each send (T023) --------------------------------------


async def test_user_who_lost_access_is_skipped(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path, access_gate=True)
    stays = await make_access_user(db_session, make_user, clock, "beta")
    leaves = await make_access_user(db_session, make_user, clock, "beta")
    write_notes(tmp_path, "0.2.0", en=TEXT_EN)
    await _announce(session_factory, configured, clock)
    await db_session.execute(
        delete(BetaEntry).where(BetaEntry.telegram_id == leaves.telegram_control_chat_id)
    )
    await db_session.flush()
    bot, api = offline_bot()

    await make_sender(session_factory, bot, configured, clock, running=None).drain()

    assert sends_to(api, leaves.telegram_control_chat_id) == []
    assert len(sends_to(api, stays.telegram_control_chat_id)) == 1
    skipped = await delivery_of(db_session, leaves)
    assert (skipped.state, skipped.skip_reason) == (ReleaseDeliveryState.skipped, "no_access")


async def test_suspended_user_is_skipped(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path, access_gate=True)
    user = await make_access_user(db_session, make_user, clock, "beta")
    write_notes(tmp_path, "0.2.0", en=TEXT_EN)
    await _announce(session_factory, configured, clock)
    user.access_suspended_at = clock.now
    await db_session.flush()
    bot, api = offline_bot()
    await make_sender(session_factory, bot, configured, clock, running=None).drain()
    assert api.sent_texts == []
    assert (await delivery_of(db_session, user)).skip_reason == "no_access"


async def test_deleted_user_gets_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    user = await make_user()
    write_notes(tmp_path, "0.2.0", en=TEXT_EN)
    configured = notes_settings(settings, tmp_path)
    await _announce(session_factory, configured, clock)
    await users_repo.delete_user(db_session, user.id)
    await db_session.flush()
    assert await deliveries(db_session) == []  # cascade
    bot, api = offline_bot()
    await make_sender(session_factory, bot, configured, clock, running=None).drain()
    assert api.sent_texts == []


# --- US3: Telegram errors (T027) ----------------------------------------------------------------


async def test_retry_after_pauses_and_resends_the_same_part(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}}, clock)
    bot, api = offline_bot()
    api.outcomes = [TelegramRetryAfter(METHOD, "flood", retry_after=3)]
    sleep = FakeSleep(clock)

    await make_sender(session_factory, bot, settings, clock, running=None, sleep=sleep).drain()

    assert max(sleep.delays) >= 4
    assert len(api.failed) == 1 and api.sent_texts == ["What's new in Scribe 0.2.0\n\nTwo."]
    row = await delivery_of(db_session, user)
    assert (row.state, row.attempts) == (ReleaseDeliveryState.sent, 0)


async def test_blocked_user_is_recorded_and_others_still_delivered(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    first = await make_user()
    second = await make_user()
    await _manual(session_factory, [first, second], {"0.2.0": {"en": "Two."}}, clock)
    bot, api = offline_bot()
    api.outcomes = [TelegramForbiddenError(METHOD, "bot was blocked by the user")]

    await make_sender(session_factory, bot, settings, clock, running=None).drain()

    states = sorted(d.state.value for d in await deliveries(db_session))
    assert states == ["blocked", "sent"]
    rows = await deliveries(db_session)
    (blocked,) = [d for d in rows if d.state is ReleaseDeliveryState.blocked]
    assert blocked.error == "TelegramForbiddenError"
    assert len(api.sent_texts) == 1
    for chat_id in (first.telegram_control_chat_id, second.telegram_control_chat_id):
        assert str(chat_id) not in log_capture.raw
    assert "Two." not in log_capture.raw


async def test_chat_not_found_counts_as_blocked(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}}, clock)
    bot, api = offline_bot()
    api.outcomes = [TelegramBadRequest(METHOD, "Bad Request: chat not found")]
    await make_sender(session_factory, bot, settings, clock, running=None).drain()
    assert (await delivery_of(db_session, user)).state is ReleaseDeliveryState.blocked


async def test_other_bad_request_fails_at_once(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    announcement = await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}}, clock)
    bot, api = offline_bot()
    api.outcomes = [TelegramBadRequest(METHOD, "Bad Request: message is too long")]
    await make_sender(session_factory, bot, settings, clock, running=None).drain()
    row = await delivery_of(db_session, user)
    assert (row.state, row.error, row.attempts) == (
        ReleaseDeliveryState.failed,
        "TelegramBadRequest",
        0,
    )
    (stored,) = [a for a in await announcements(db_session) if a.id == announcement.id]
    assert stored.finished_at is not None


async def test_network_errors_back_off_then_fail(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    configured = settings.model_copy(update={"release_notes_max_attempts": 3})
    await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}}, clock)
    bot, api = offline_bot()
    api.outcomes = [TelegramNetworkError(METHOD, "timeout") for _ in range(3)]
    sender = make_sender(session_factory, bot, configured, clock, running=None)

    await sender.drain()
    row = await delivery_of(db_session, user)
    assert (row.state, row.attempts, row.error) == (
        ReleaseDeliveryState.pending,
        1,
        "TelegramNetworkError",
    )
    assert row.next_attempt_at == clock.now + timedelta(seconds=30)

    await sender.drain()  # not due yet
    assert len(api.failed) == 1

    clock.advance(seconds=30)
    await sender.drain()
    row = await delivery_of(db_session, user)
    assert (row.state, row.attempts) == (ReleaseDeliveryState.pending, 2)
    assert row.next_attempt_at == clock.now + timedelta(minutes=2)

    clock.advance(minutes=2)
    await sender.drain()
    row = await delivery_of(db_session, user)
    assert (row.state, row.attempts) == (ReleaseDeliveryState.failed, 3)
    assert api.sent_texts == []


# --- US3: crash recovery (T028) -----------------------------------------------------------------


async def _set(db: AsyncSession, row: ReleaseDelivery, **values: object) -> None:
    for key, value in values.items():
        setattr(row, key, value)
    await db.flush()


async def test_one_part_left_in_sending_becomes_unknown_and_is_not_resent(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}}, clock)
    row = await delivery_of(db_session, user)
    await _set(db_session, row, state=ReleaseDeliveryState.sending, parts_total=1, parts_sent=0)

    assert await recover_interrupted(session_factory, now=clock.now) == 1
    bot, api = offline_bot()
    await make_sender(session_factory, bot, settings, clock, running=None).drain()
    assert api.sent_texts == []
    row = await delivery_of(db_session, user)
    assert row.state is ReleaseDeliveryState.unknown

    clock.advance(minutes=1)  # a later announcement covering 0.2.0 skips it
    await _manual(session_factory, [user], {"0.2.0": {"en": "Two."}}, clock)
    await make_sender(session_factory, bot, settings, clock, running=None).drain()
    assert api.sent_texts == []
    reasons = [d.skip_reason for d in await deliveries(db_session)]
    assert "already_received" in reasons


def _two_part_notes() -> dict[str, dict[str, str]]:
    return {
        "0.2.0": {"en": "a" * MAX_SECTION_CHARS},
        "0.3.0": {"en": "b" * MAX_SECTION_CHARS},
    }


async def test_two_part_digest_crash_on_part_one_sends_only_part_two(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await _manual(session_factory, [user], _two_part_notes(), clock)
    row = await delivery_of(db_session, user)
    await _set(db_session, row, state=ReleaseDeliveryState.sending, parts_total=2, parts_sent=0)

    await recover_interrupted(session_factory, now=clock.now)
    row = await delivery_of(db_session, user)
    assert (row.state, row.parts_sent) == (ReleaseDeliveryState.pending, 1)

    bot, api = offline_bot()
    await make_sender(session_factory, bot, settings, clock, running=None).drain()
    (part,) = api.sent_texts
    assert part.startswith("What's new in Scribe 0.3.0")
    row = await delivery_of(db_session, user)
    assert (row.state, row.parts_sent, row.parts_total) == (ReleaseDeliveryState.sent, 2, 2)


async def test_two_part_digest_crash_on_part_two_is_unknown(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await _manual(session_factory, [user], _two_part_notes(), clock)
    row = await delivery_of(db_session, user)
    await _set(db_session, row, state=ReleaseDeliveryState.sending, parts_total=2, parts_sent=1)
    await recover_interrupted(session_factory, now=clock.now)
    bot, api = offline_bot()
    await make_sender(session_factory, bot, settings, clock, running=None).drain()
    assert api.sent_texts == []
    assert (await delivery_of(db_session, user)).state is ReleaseDeliveryState.unknown


async def test_two_parts_are_spaced_per_chat(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    await _manual(session_factory, [user], _two_part_notes(), clock)
    bot, api = offline_bot()
    sleep = FakeSleep(clock)
    await make_sender(session_factory, bot, settings, clock, running=None, sleep=sleep).drain()
    assert len(api.sent_texts) == 2
    assert sleep.total >= 1.1


async def test_crash_halfway_then_restart_delivers_the_rest_once(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    users = [await make_user() for _ in range(20)]
    await _manual(session_factory, users, {"0.2.0": {"en": "Two."}}, clock)
    bot, api = offline_bot()
    api.outcomes = [None] * 7 + [asyncio.CancelledError()]

    with pytest.raises(asyncio.CancelledError):
        await make_sender(session_factory, bot, settings, clock, running=None).drain()
    assert len(api.sent_texts) == 7

    await recover_interrupted(session_factory, now=clock.now)  # the restart
    await make_sender(session_factory, bot, settings, clock, running=None).drain()

    states = [d.state.value for d in await deliveries(db_session)]
    assert sorted(states).count("sent") == 19 and states.count("unknown") == 1
    chats = [int(r.chat_id) for r in api.of_type(SendMessage)]
    assert len(chats) == 19 and len(set(chats)) == 19  # nobody got two
    (in_doubt,) = api.failed
    assert isinstance(in_doubt, SendMessage) and int(in_doubt.chat_id) not in chats


# --- US3: one sender at a time, and the scheduler keeps going (T029) -----------------------------


class GatedSleep(FakeSleep):
    """The first sleep waits until ``release`` is set."""

    def __init__(self, clock: FrozenClock) -> None:
        super().__init__(clock)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        if not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        await super().__call__(seconds)


async def test_second_run_returns_while_a_drain_is_running(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    users = [await make_user() for _ in range(2)]
    await _manual(session_factory, users, {"0.2.0": {"en": "Two."}}, clock)
    bot, api = offline_bot()
    sleep = GatedSleep(clock)
    sender = make_sender(session_factory, bot, settings, clock, running=None, sleep=sleep)

    first = asyncio.create_task(sender.run_once())
    await asyncio.wait_for(sleep.entered.wait(), timeout=5)  # paused before the 2nd send
    await asyncio.wait_for(sender.run_once(), timeout=1)  # returns at once
    assert len(api.sent_texts) == 1
    sleep.release.set()
    await first
    assert len(api.sent_texts) == 2


async def test_shutdown_while_waiting_leaves_the_row_pending(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    """Cancelled before a part went out: nothing is in doubt, so it is sent after a restart."""
    users = [await make_user() for _ in range(2)]
    await _manual(session_factory, users, {"0.2.0": {"en": "Two."}}, clock)
    bot, api = offline_bot()
    sleep = GatedSleep(clock)
    task = asyncio.create_task(
        make_sender(session_factory, bot, settings, clock, running=None, sleep=sleep).drain()
    )
    await asyncio.wait_for(sleep.entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    states = sorted(d.state.value for d in await deliveries(db_session))
    assert states == ["pending", "sent"]

    assert await recover_interrupted(session_factory, now=clock.now) == 0
    await make_sender(session_factory, bot, settings, clock, running=None).drain()
    assert len({int(r.chat_id) for r in api.of_type(SendMessage)}) == 2
    assert len(api.sent_texts) == 2


async def test_long_drain_does_not_block_other_jobs(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    tmp_path: Path,
) -> None:
    async with committed_session_factory() as session:
        for _ in range(2):
            await users_repo.create_user(
                session, control_chat_id=next_telegram_id(), settings=settings
            )
        await session.commit()
    write_notes(tmp_path, "0.2.0", en="Two.")
    configured = notes_settings(settings, tmp_path)
    bot, api = offline_bot()
    scheduler = Scheduler(committed_session_factory, tick_s=5, clock=clock)
    sender = register_release_notes_job(
        scheduler, committed_session_factory, bot, configured, clock
    )
    sleep = GatedSleep(clock)
    sender._sleep = sleep  # noqa: SLF001 - test hook
    sender.pacer._sleep = sleep  # noqa: SLF001
    sender._running = lambda: (0, 2, 0)  # noqa: SLF001
    other_runs: list[int] = []

    async def other() -> None:
        other_runs.append(1)

    scheduler.register_job("other_job", Every(timedelta(seconds=5)), other, run_at_start=True)
    await scheduler.ensure_jobs()
    await scheduler.tick()
    await asyncio.wait_for(sleep.entered.wait(), timeout=5)
    await asyncio.sleep(0.2)
    assert other_runs == [1]  # ran while the drain is paused
    assert len(api.sent_texts) == 1
    sleep.release.set()
    await scheduler.wait_idle()
    assert len(api.sent_texts) == 2
    async with committed_session_factory() as session:
        names = (await session.scalars(select(ScheduledJob.name))).all()
    assert JOB_NAME in names
