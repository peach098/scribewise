"""Who gets an announcement (specs/011-release-notes US2, research R4)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import repo as access_repo
from scribe.config import Settings
from scribe.db.models import (
    BetaApplication,
    BetaApplicationStatus,
    RefusedContact,
    UserState,
)
from scribe.release_notes import announce
from tests.conftest import FrozenClock, MakeUser, next_telegram_id
from tests.integration.release_notes_support import (
    deliveries,
    make_access_user,
    notes_settings,
    write_notes,
)

OPERATOR_ID = 900000101


async def _seed_people(db: AsyncSession, clock: FrozenClock) -> None:
    """A refused sender and an applicant: neither has an account."""
    applicant = next_telegram_id()
    refused = next_telegram_id()
    db.add(
        RefusedContact(
            telegram_id=refused, first_seen_at=clock.now, last_seen_at=clock.now, attempts=1
        )
    )
    db.add(
        BetaApplication(
            telegram_id=applicant,
            language="en",
            consent_at=clock.now,
            status=BetaApplicationStatus.pending,
        )
    )
    await db.flush()


async def test_gate_on_only_people_with_access(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(
        settings, tmp_path, access_gate=True, operator_telegram_ids=[OPERATOR_ID]
    )
    beta = await make_access_user(db_session, make_user, clock, "beta")
    subscribed = await make_access_user(db_session, make_user, clock, "subscribed", lang="ru")
    operator = await make_access_user(
        db_session, make_user, clock, "operator", telegram_id=OPERATOR_ID
    )
    await make_access_user(db_session, make_user, clock, "suspended")
    # No access, but access_reconcile has not suspended them yet.
    await make_access_user(db_session, make_user, clock, "none")
    paused = await make_access_user(db_session, make_user, clock, "beta")
    paused.state = UserState.paused
    setting_up = await make_access_user(db_session, make_user, clock, "beta")
    setting_up.state = UserState.setting_up
    ooo = await make_access_user(db_session, make_user, clock, "beta")
    ooo.state = UserState.out_of_office
    await _seed_people(db_session, clock)
    await db_session.flush()
    write_notes(tmp_path, "0.2.0", en="Two.")

    await announce.ensure_auto_announcement(
        session_factory, configured, now=clock.now, running=(0, 2, 0)
    )

    got = {d.user_id for d in await deliveries(db_session)}
    assert got == {beta.id, subscribed.id, operator.id, paused.id, setting_up.id, ooo.id}


async def test_gate_off_every_account(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path, access_gate=False)
    users = [
        await make_access_user(db_session, make_user, clock, kind)
        for kind in ("beta", "none", "suspended")  # a stale suspension still gets it
    ]
    await _seed_people(db_session, clock)
    write_notes(tmp_path, "0.2.0", en="Two.")

    await announce.ensure_auto_announcement(
        session_factory, configured, now=clock.now, running=(0, 2, 0)
    )

    assert {d.user_id for d in await deliveries(db_session)} == {user.id for user in users}


async def test_user_created_after_the_announcement_gets_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    first = await make_user()
    write_notes(tmp_path, "0.2.0", en="Two.")
    await announce.ensure_auto_announcement(
        session_factory, configured, now=clock.now, running=(0, 2, 0)
    )
    await make_user()
    await announce.ensure_auto_announcement(
        session_factory, configured, now=clock.now, running=(0, 2, 0)
    )
    assert [d.user_id for d in await deliveries(db_session)] == [first.id]


async def test_bulk_status_matches_access_status(
    db_session: AsyncSession,
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    """The shared helper (T010) agrees with the per-person lookup."""
    configured = settings.model_copy(update={"operator_telegram_ids": [OPERATOR_ID]})
    users = [
        await make_access_user(db_session, make_user, clock, "beta"),
        await make_access_user(db_session, make_user, clock, "subscribed"),
        await make_access_user(db_session, make_user, clock, "none"),
        await make_access_user(db_session, make_user, clock, "operator", telegram_id=OPERATOR_ID),
    ]
    statuses = await access_repo.bulk_status(db_session, configured, users, now=clock.now)
    for user in users:
        single = await access_repo.access_status(
            db_session, configured, user.telegram_control_chat_id, None, now=clock.now
        )
        assert statuses[user.id] == single
