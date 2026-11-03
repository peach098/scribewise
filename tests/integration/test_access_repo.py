"""Access repository and migration 0005 (005 T004, T007, T008). Synthetic values only."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.access import repo
from scribe.access.policy import AccessKind
from scribe.config import Settings
from scribe.db.models import AccessEvent, AccessEventKind, BetaEntry, RefusedContact, Subscription
from scribe.db.session import create_engine
from tests.conftest import FROZEN_NOW, ROOT

NOW = FROZEN_NOW
TELEGRAM_ID = 900000001
OTHER_ID = 900000002
COOLDOWN_S = 3600


async def _add_beta(
    db: AsyncSession, *, telegram_id: int | None = None, username: str | None = None
) -> BetaEntry:
    entry = BetaEntry(
        telegram_id=telegram_id,
        username=username,
        added_by="operator",
        added_at=NOW - timedelta(days=1),
    )
    db.add(entry)
    await db.flush()
    return entry


async def _events(db: AsyncSession, telegram_id: int) -> list[AccessEvent]:
    result = await db.scalars(select(AccessEvent).where(AccessEvent.telegram_id == telegram_id))
    return list(result.all())


# --- find_beta ------------------------------------------------------------------------------------


async def test_find_beta_by_telegram_id(db_session: AsyncSession) -> None:
    entry = await _add_beta(db_session, telegram_id=TELEGRAM_ID)
    assert await repo.find_beta(db_session, TELEGRAM_ID, None, now=NOW) is entry
    assert await repo.find_beta(db_session, OTHER_ID, None, now=NOW) is None
    assert await _events(db_session, TELEGRAM_ID) == []


async def test_username_links_once_ignoring_case(db_session: AsyncSession) -> None:
    entry = await _add_beta(db_session, username="beta_tester_1")

    found = await repo.find_beta(db_session, TELEGRAM_ID, "@Beta_Tester_1", now=NOW)
    assert found is not None and found.id == entry.id
    await db_session.refresh(entry)
    assert (entry.telegram_id, entry.linked_at) == (TELEGRAM_ID, NOW)

    events = await _events(db_session, TELEGRAM_ID)
    assert [(e.kind, e.actor, e.at) for e in events] == [
        (AccessEventKind.beta_linked, "system", NOW)
    ]
    assert events[0].meta_json == {"beta_entry_id": str(entry.id)}

    # Same person, later and with a new username: matched by id, not linked again.
    later = NOW + timedelta(hours=1)
    again = await repo.find_beta(db_session, TELEGRAM_ID, "@renamed_tester", now=later)
    assert again is not None and again.id == entry.id
    assert len(await _events(db_session, TELEGRAM_ID)) == 1
    # Someone else now using the old username does not get the linked entry.
    assert await repo.find_beta(db_session, OTHER_ID, "@beta_tester_1", now=later) is None


async def test_unknown_username_finds_nothing(db_session: AsyncSession) -> None:
    await _add_beta(db_session, username="beta_tester_2")
    assert await repo.find_beta(db_session, TELEGRAM_ID, "@someone_else", now=NOW) is None
    assert await repo.find_beta(db_session, TELEGRAM_ID, None, now=NOW) is None
    assert await repo.find_beta(db_session, TELEGRAM_ID, "@", now=NOW) is None


async def test_beta_constraints(db_session: AsyncSession) -> None:
    await _add_beta(db_session, username="beta_tester_3")
    savepoint = await db_session.begin_nested()
    with pytest.raises(IntegrityError):
        await _add_beta(db_session)  # neither id nor username
    await savepoint.rollback()
    savepoint = await db_session.begin_nested()
    with pytest.raises(IntegrityError):
        await _add_beta(db_session, username="beta_tester_3")  # duplicate unlinked username
    await savepoint.rollback()


# --- access_status / record_event ----------------------------------------------------------------


async def test_access_status_combines_rows(db_session: AsyncSession, settings: Settings) -> None:
    status = await repo.access_status(db_session, settings, TELEGRAM_ID, None, now=NOW)
    assert status.kind is AccessKind.none

    ends_at = NOW + timedelta(days=30)
    db_session.add(
        Subscription(
            telegram_id=TELEGRAM_ID,
            starts_at=NOW - timedelta(days=1),
            ends_at=ends_at,
            granted_by="operator",
        )
    )
    await _add_beta(db_session, username="beta_tester_4")
    status = await repo.access_status(db_session, settings, TELEGRAM_ID, "beta_tester_4", now=NOW)
    assert (status.kind, status.until) == (AccessKind.beta_and_subscribed, ends_at)

    operator = settings.model_copy(update={"operator_telegram_ids": [OTHER_ID]})
    status = await repo.access_status(db_session, operator, OTHER_ID, None, now=NOW)
    assert status.kind is AccessKind.operator


async def test_record_event_truncates_reason(db_session: AsyncSession) -> None:
    event = await repo.record_event(
        db_session,
        TELEGRAM_ID,
        AccessEventKind.subscription_granted,
        actor="operator",
        reason="x" * 250,
        meta={"ends_at": NOW},
        now=NOW,
    )
    assert event.reason == "x" * 200
    assert event.meta_json == {"ends_at": NOW.isoformat()}


# --- touch_refused --------------------------------------------------------------------------------


async def test_touch_refused_cooldown(db_session: AsyncSession) -> None:
    assert await repo.touch_refused(db_session, TELEGRAM_ID, now=NOW, cooldown_s=COOLDOWN_S)
    second = NOW + timedelta(seconds=10)
    assert not await repo.touch_refused(db_session, TELEGRAM_ID, now=second, cooldown_s=COOLDOWN_S)

    row = await db_session.get(RefusedContact, TELEGRAM_ID, populate_existing=True)
    assert row is not None
    assert (row.attempts, row.first_seen_at, row.last_seen_at, row.last_answered_at) == (
        2,
        NOW,
        second,
        NOW,
    )

    third = NOW + timedelta(seconds=COOLDOWN_S + 1)
    assert await repo.touch_refused(db_session, TELEGRAM_ID, now=third, cooldown_s=COOLDOWN_S)
    await db_session.refresh(row)
    assert (row.attempts, row.last_answered_at) == (3, third)


async def test_touch_refused_same_instant_answers_once(db_session: AsyncSession) -> None:
    assert await repo.touch_refused(db_session, TELEGRAM_ID, now=NOW, cooldown_s=COOLDOWN_S)
    assert not await repo.touch_refused(db_session, TELEGRAM_ID, now=NOW, cooldown_s=COOLDOWN_S)
    # Other senders have their own cooldown.
    assert await repo.touch_refused(db_session, OTHER_ID, now=NOW, cooldown_s=COOLDOWN_S)


# --- migration 0005 -------------------------------------------------------------------------------


def _alembic(database_url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "SCRIBE_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


ACCESS_TABLES = {"beta_entries", "subscriptions", "refused_contacts", "access_events"}
APPLICATION_KINDS = {"application_submitted", "application_approved", "application_rejected"}


async def test_migrations_0005_0006_downgrade_and_upgrade(migrated_database_url: str) -> None:
    """upgrade head -> downgrade 0005 -> downgrade 0004 -> upgrade head on a scratch database.

    0006 (beta applications) drops its table and status enum on downgrade but leaves the
    values it added to ``access_event_kind`` (Postgres cannot drop enum values)."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)

    async def schema() -> tuple[set[str], bool, int, int]:
        async with scratch.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
            column = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_name = 'users' AND column_name = 'access_suspended_at'"
                )
            )
            enum_count = await connection.scalar(
                text("SELECT count(*) FROM pg_type WHERE typname = 'access_event_kind'")
            )
            status_enum = await connection.scalar(
                text("SELECT count(*) FROM pg_type WHERE typname = 'beta_application_status'")
            )
            known = tables & (ACCESS_TABLES | {"beta_applications"})
            return known, bool(column), int(enum_count or 0), int(status_enum or 0)

    async def event_kinds() -> set[str]:
        async with scratch.connect() as connection:
            rows = await connection.execute(
                text("SELECT unnest(enum_range(NULL::access_event_kind))::text")
            )
            return set(rows.scalars().all())

    try:
        _alembic(scratch_url, "upgrade", "head")
        assert await schema() == (ACCESS_TABLES | {"beta_applications"}, True, 1, 1)
        assert await event_kinds() >= APPLICATION_KINDS
        _alembic(scratch_url, "downgrade", "0005")
        assert await schema() == (ACCESS_TABLES, True, 1, 0)
        assert await event_kinds() >= APPLICATION_KINDS
        _alembic(scratch_url, "upgrade", "head")  # ADD VALUE IF NOT EXISTS: no error
        assert await schema() == (ACCESS_TABLES | {"beta_applications"}, True, 1, 1)
        _alembic(scratch_url, "downgrade", "0004")
        assert await schema() == (set(), False, 0, 0)
        _alembic(scratch_url, "upgrade", "head")
        assert await schema() == (ACCESS_TABLES | {"beta_applications"}, True, 1, 1)
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
