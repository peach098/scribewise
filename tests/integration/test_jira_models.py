"""Jira schema (specs/003-jira-commitments T006): migration 0004, new columns, cursor table and
the repo helpers built on them. Synthetic data only."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context import comment_commitments
from scribe.db.models import (
    Commitment,
    CommitmentAutoNote,
    CommitmentClosedBy,
    CommitmentSourceKind,
    CommitmentState,
    JiraCommentCursor,
    JiraCursorOutcome,
    TaskProvider,
)
from scribe.db.repos import commitments, jira_cursor, tasks_conn, users
from scribe.db.session import create_engine
from scribe.retention.purge import purge_messages
from tests.conftest import FROZEN_NOW, MakeContact, MakeUser
from tests.integration.test_models_and_repos import _alembic

NOW = FROZEN_NOW


async def _jira_commitment(
    session: AsyncSession, user_id: uuid.UUID, comment_id: str = "10001", **values: object
) -> Commitment:
    return await commitments.add_commitment(
        session,
        user_id,
        contact_id=None,
        what="fix on staging",
        due=date(2026, 10, 21),
        source_sent_at=NOW,
        confidence=0.9,
        to_whom="Anna Smith",
        source_kind=CommitmentSourceKind.jira_comment,
        jira_issue_key="PAY-142",
        jira_comment_id=comment_id,
        jira_comment_url=f"https://acme.atlassian.net/browse/PAY-142?focusedCommentId={comment_id}",
        **values,  # type: ignore[arg-type]
    )


async def test_jira_commitment_without_contact_round_trip(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    stored = await _jira_commitment(db_session, user.id)
    db_session.expunge_all()
    loaded = await commitments.get_commitment(db_session, user.id, stored.id)
    assert loaded is not None
    assert loaded.contact_id is None and loaded.to_whom == "Anna Smith"
    assert loaded.source_kind == CommitmentSourceKind.jira_comment
    assert (loaded.restricted, loaded.user_modified) == (False, False)
    raw = (
        await db_session.execute(
            text("SELECT to_whom FROM commitments WHERE id = :id"), {"id": stored.id}
        )
    ).scalar_one()
    assert "Anna" not in raw  # encrypted at rest


async def test_one_open_commitment_per_jira_comment(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    first = await _jira_commitment(db_session, user.id)
    await commitments.close(
        db_session, user.id, first.id, CommitmentState.resolved, at=NOW,
        closed_by=CommitmentClosedBy.system, auto_note=CommitmentAutoNote.issue_done,
    )  # fmt: skip
    await _jira_commitment(db_session, user.id)  # the closed one does not block a new open one
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _jira_commitment(db_session, user.id)


async def test_reopen_only_system_closed(db_session: AsyncSession, make_user: MakeUser) -> None:
    user = await make_user()
    by_system = await _jira_commitment(db_session, user.id, "1")
    by_user = await _jira_commitment(db_session, user.id, "2")
    await commitments.close(
        db_session, user.id, by_system.id, CommitmentState.resolved, at=NOW,
        closed_by=CommitmentClosedBy.system, auto_note=CommitmentAutoNote.source_removed,
    )  # fmt: skip
    await commitments.close(db_session, user.id, by_user.id, CommitmentState.deleted, at=NOW)
    closed = await commitments.list_recently_closed_by_system(
        db_session, user.id, since=NOW - timedelta(days=30)
    )
    assert [c.id for c in closed] == [by_system.id]
    assert await commitments.reopen(db_session, user.id, by_user.id) is None
    reopened = await commitments.reopen(db_session, user.id, by_system.id)
    assert reopened is not None and reopened.state == CommitmentState.open
    assert reopened.user_modified and reopened.auto_note is None and reopened.closed_at is None
    other = await make_user()
    assert await commitments.reopen(db_session, other.id, by_system.id) is None
    assert [c.id for c in await commitments.get_by_comment(db_session, user.id, "2")] == []
    all_rows = await commitments.get_by_comment(db_session, user.id, "2", open_only=False)
    assert [c.closed_by for c in all_rows] == [CommitmentClosedBy.user]


async def test_issue_and_contactless_lookups(
    db_session: AsyncSession, make_user: MakeUser, make_contact: MakeContact
) -> None:
    user = await make_user()
    contact = await make_contact(user)
    jira = await _jira_commitment(db_session, user.id)
    telegram = await commitments.add_commitment(
        db_session, user.id, contact_id=contact.id, what="send the logs", due=None,
        source_sent_at=NOW, confidence=0.8, jira_issue_key="CORE-7",
    )  # fmt: skip
    assert {
        c.id for c in await commitments.list_open_for_issues(db_session, user.id, ["PAY-142"])
    } == {jira.id}  # noqa: E501
    assert [c.id for c in await commitments.list_open_jira(db_session, user.id)] == [jira.id]
    assert [c.id for c in await commitments.list_open_without_contact(db_session, user.id)] == [
        jira.id
    ]
    edited = await commitments.update_open(db_session, user.id, telegram.id, what="send logs")
    assert edited is not None and edited.user_modified


async def test_cursor_upsert_purge_and_cascade(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    await jira_cursor.upsert(
        db_session, user.id, issue_key="PAY-1", comment_id="c1", comment_updated_at=NOW,
        body_hmac=b"\x01" * 32, outcome=JiraCursorOutcome.no_commitment,
    )  # fmt: skip
    await jira_cursor.upsert(
        db_session, user.id, issue_key="PAY-1", comment_id="c1",
        comment_updated_at=NOW + timedelta(hours=1), body_hmac=b"\x02" * 32,
        outcome=JiraCursorOutcome.commitment,
    )  # fmt: skip
    rows = await jira_cursor.get_many(db_session, user.id, ["c1", "missing"])
    assert list(rows) == ["c1"]
    assert (
        rows["c1"].body_hmac == b"\x02" * 32 and rows["c1"].outcome == JiraCursorOutcome.commitment
    )
    assert await jira_cursor.count_for_user(db_session, user.id) == 1
    assert await jira_cursor.delete_older_than(db_session, NOW) == 0
    assert await jira_cursor.delete_older_than(db_session, NOW + timedelta(days=1)) == 1

    await jira_cursor.upsert(
        db_session, user.id, issue_key="PAY-1", comment_id="c2", comment_updated_at=NOW,
        body_hmac=b"\x03" * 32, outcome=JiraCursorOutcome.skipped,
    )  # fmt: skip
    await users.delete_user(db_session, user.id)
    count = await db_session.scalar(select(func.count()).select_from(JiraCommentCursor))
    assert count == 0


async def test_retention_purges_old_cursors(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    make_user: MakeUser,
) -> None:
    user = await make_user()
    for comment_id, age in (("old", 31), ("recent", 29)):
        await jira_cursor.upsert(
            db_session, user.id, issue_key="PAY-1", comment_id=comment_id,
            comment_updated_at=NOW - timedelta(days=age), body_hmac=b"\x01" * 32,
            outcome=JiraCursorOutcome.no_commitment,
        )  # fmt: skip
    await db_session.commit()
    stats = await purge_messages(session_factory, settings, NOW)
    assert stats.jira_cursors_deleted == 1
    assert list(await jira_cursor.get_many(db_session, user.id, ["old", "recent"])) == ["recent"]


async def test_claim_due_jira_and_delete_connection(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    jira_user, trello_user = await make_user(), await make_user()
    await tasks_conn.upsert_connection(
        db_session, jira_user.id, provider=TaskProvider.jira, credentials={"pat": "x"}
    )
    await tasks_conn.upsert_connection(
        db_session, trello_user.id, provider=TaskProvider.trello, credentials={"key": "k"}
    )
    interval = timedelta(minutes=5)
    assert await tasks_conn.claim_due_jira(db_session, NOW, interval=interval) == [jira_user.id]
    assert await tasks_conn.claim_due_jira(db_session, NOW, interval=interval) == []
    assert await tasks_conn.claim_due_jira(db_session, NOW + interval, interval=interval) == [
        jira_user.id
    ]
    await tasks_conn.set_sync_state(db_session, jira_user.id, watermark=NOW)
    await tasks_conn.set_sync_state(db_session, jira_user.id, watermark=NOW - timedelta(hours=1))
    conn = await tasks_conn.get_connection(db_session, jira_user.id)
    assert conn is not None and conn.sync_watermark == NOW  # never moves backwards
    assert await tasks_conn.list_jira_user_ids(db_session) == [jira_user.id]

    await jira_cursor.upsert(
        db_session, jira_user.id, issue_key="PAY-1", comment_id="c1", comment_updated_at=NOW,
        body_hmac=b"\x01" * 32, outcome=JiraCursorOutcome.no_commitment,
    )  # fmt: skip
    await tasks_conn.delete_connection(db_session, jira_user.id)
    assert await jira_cursor.count_for_user(db_session, jira_user.id) == 0


async def test_migration_0004_backfills_and_downgrades(migrated_database_url: str) -> None:
    """0004 on a scratch database: existing commitments keep working, closed ones get
    ``closed_by='user'``; the downgrade removes contactless rows and the new objects."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    sent_at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)
    try:
        _alembic(scratch_url, "upgrade", "0003")
        async with scratch.begin() as connection:
            user_id = (
                await connection.execute(
                    text(
                        "INSERT INTO users (id, telegram_control_chat_id, display_name, timezone,"
                        " working_days, work_start, work_end, delay_min_s, delay_max_s, state,"
                        " llm_tier) VALUES (gen_random_uuid(), 4300100, 'Synthetic', 'UTC',"
                        " '{0,1,2,3,4}', '09:00', '18:00', 0, 0, 'active', 'quality')"
                        " RETURNING id"
                    )
                )
            ).scalar_one()
            contact_id = (
                await connection.execute(
                    text(
                        "INSERT INTO contacts (id, user_id, telegram_peer_id, kind, display_name,"
                        " own_message_count_6m, pinned_profile) VALUES (gen_random_uuid(), :u,"
                        " 4300101, 'person', 'Synthetic', 0, false) RETURNING id"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()
            for state, closed_at in (("open", None), ("resolved", sent_at)):
                await connection.execute(
                    text(
                        "INSERT INTO commitments (id, user_id, contact_id, what, due,"
                        " source_sent_at, confidence, state, closed_at) VALUES"
                        " (gen_random_uuid(), :u, :c, 'send the deck', NULL, :s, 0.9,"
                        " CAST(:state AS commitment_state), :closed)"
                    ),
                    {
                        "u": user_id,
                        "c": contact_id,
                        "s": sent_at,
                        "state": state,
                        "closed": closed_at,
                    },
                )

        _alembic(scratch_url, "upgrade", "0004")
        async with scratch.begin() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT state::text, closed_by::text, source_kind::text, user_modified,"
                        " restricted FROM commitments ORDER BY state"
                    )
                )
            ).all()
            assert rows == [
                ("open", None, "telegram_message", False, False),
                ("resolved", "user", "telegram_message", False, False),
            ]
            await connection.execute(
                text(
                    "INSERT INTO commitments (id, user_id, contact_id, what, source_sent_at,"
                    " confidence, state, source_kind, jira_comment_id) VALUES"
                    " (gen_random_uuid(), :u, NULL, 'fix', :s, 0.9, 'open', 'jira_comment', 'c1')"
                ),
                {"u": user_id, "s": sent_at},
            )

        _alembic(scratch_url, "downgrade", "0003")
        async with scratch.begin() as connection:
            count = await connection.scalar(text("SELECT count(*) FROM commitments"))
            assert count == 2  # the contactless Jira row is gone
            tables = await connection.scalar(
                text("SELECT count(*) FROM pg_tables WHERE tablename = 'jira_comment_cursor'")
            )
            assert tables == 0
            enums = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_type WHERE typname IN ('commitment_source_kind',"
                    " 'commitment_auto_note', 'commitment_closed_by', 'jira_cursor_outcome')"
                )
            )
            assert enums == 0

        _alembic(scratch_url, "upgrade", "head")
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_jira_data_summary_counts(db_session: AsyncSession, make_user: MakeUser) -> None:
    user = await make_user()
    await _jira_commitment(db_session, user.id, "1")
    await jira_cursor.upsert(
        db_session, user.id, issue_key="PAY-1", comment_id="c1", comment_updated_at=NOW,
        body_hmac=b"\x01" * 32, outcome=JiraCursorOutcome.commitment,
    )  # fmt: skip
    assert await comment_commitments.jira_data_summary(db_session, user.id) == {
        "jira_commitments": 1,
        "jira_comment_markers": 1,
    }


async def test_migration_0007_search_earlier(migrated_database_url: str) -> None:
    """specs/004 T011: existing users get search on; the downgrade drops the column."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)
    try:
        _alembic(scratch_url, "upgrade", "0006")
        async with scratch.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, telegram_control_chat_id, display_name, timezone,"
                    " working_days, work_start, work_end, delay_min_s, delay_max_s, state,"
                    " llm_tier) VALUES (gen_random_uuid(), 4300200, 'Synthetic', 'UTC',"
                    " '{0,1,2,3,4}', '09:00', '18:00', 0, 0, 'active', 'quality')"
                )
            )
        _alembic(scratch_url, "upgrade", "0007")
        async with scratch.begin() as connection:
            value = await connection.scalar(text("SELECT search_earlier_messages FROM users"))
            assert value is True
        _alembic(scratch_url, "downgrade", "0006")
        async with scratch.begin() as connection:
            columns = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name = 'users'"
                    " AND column_name = 'search_earlier_messages'"
                )
            )
            assert columns == 0
        _alembic(scratch_url, "upgrade", "head")
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_migration_0009_drops_group_mode(migrated_database_url: str) -> None:
    """specs/006 T015: the group mode column and type go, the chats stay; the downgrade brings
    the column back NOT NULL with every chat at ``mentions_only`` and no server default."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)
    column_query = text(
        "SELECT is_nullable, column_default FROM information_schema.columns"
        " WHERE table_name = 'work_chats' AND column_name = 'group_mode'"
    )
    type_query = text("SELECT count(*) FROM pg_type WHERE typname = 'group_mode'")
    try:
        _alembic(scratch_url, "upgrade", "0011")
        async with scratch.begin() as connection:
            user_id = (
                await connection.execute(
                    text(
                        "INSERT INTO users (id, telegram_control_chat_id, display_name, timezone,"
                        " working_days, work_start, work_end, delay_min_s, delay_max_s, state,"
                        " llm_tier) VALUES (gen_random_uuid(), 4300300, 'Synthetic', 'UTC',"
                        " '{0,1,2,3,4}', '09:00', '18:00', 0, 0, 'active', 'quality')"
                        " RETURNING id"
                    )
                )
            ).scalar_one()
            for chat_id, mode in ((-4300301, "always"), (-4300302, "mentions_only")):
                await connection.execute(
                    text(
                        "INSERT INTO work_chats (id, user_id, telegram_chat_id, title, kind,"
                        " enabled, group_mode) VALUES (gen_random_uuid(), :u, :c, 'Synthetic',"
                        " 'group', true, CAST(:mode AS group_mode))"
                    ),
                    {"u": user_id, "c": chat_id, "mode": mode},
                )

        _alembic(scratch_url, "upgrade", "0009")
        async with scratch.begin() as connection:
            assert (await connection.execute(column_query)).all() == []
            assert await connection.scalar(type_query) == 0
            assert await connection.scalar(text("SELECT count(*) FROM work_chats")) == 2

        _alembic(scratch_url, "downgrade", "0011")
        async with scratch.begin() as connection:
            assert [tuple(r) for r in (await connection.execute(column_query)).all()] == [
                ("NO", None)
            ]
            modes = (
                await connection.execute(
                    text("SELECT group_mode::text FROM work_chats ORDER BY telegram_chat_id")
                )
            ).scalars()
            assert list(modes) == ["mentions_only", "mentions_only"]

        _alembic(scratch_url, "upgrade", "head")
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
