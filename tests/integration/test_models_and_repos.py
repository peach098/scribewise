"""Schema constraints, cascades, encryption at rest and repo scoping (T009-T011). Synthetic data."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import (
    ActivityKind,
    BotFsmState,
    CommitmentState,
    ConversationTurn,
    DecisionSource,
    Draft,
    DraftState,
    DroppedReason,
    LoginMethod,
    Message,
    ProfileScope,
    ReplyDecision,
    SessionStatus,
    SkipCategory,
    TurnState,
    User,
    UserState,
    WorkChatOrigin,
)
from scribe.db.repos import activity, commitments, drafts, messages, profiles, turns, users
from scribe.db.session import create_engine
from tests.conftest import FROZEN_NOW, ROOT, MakeChat, MakeContact, MakeUser

NOW = FROZEN_NOW


async def _turn(
    db: AsyncSession, user: User, chat_id: uuid.UUID, contact_id: uuid.UUID
) -> ConversationTurn:
    return await turns.create_turn(
        db,
        user.id,
        work_chat_id=chat_id,
        contact_id=contact_id,
        first_message_id=None,
        arrived_at=NOW,
        delay_s=0,
        due_at=NOW,
    )


async def _expect_integrity_error(db: AsyncSession, statement: str, **params: object) -> None:
    savepoint = await db.begin_nested()
    with pytest.raises(IntegrityError):
        await db.execute(text(statement), params)
    await savepoint.rollback()


async def test_user_defaults_and_check_constraints(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    assert user.state == UserState.setting_up
    assert user.llm_tier == "quality"
    assert user.working_days == [0, 1, 2, 3, 4]

    for column, value in [
        ("delay_min_s", -1),
        ("delay_max_s", 86401),
    ]:
        await _expect_integrity_error(
            db_session, f"UPDATE users SET {column} = :v WHERE id = :id", v=value, id=user.id
        )
    await _expect_integrity_error(
        db_session,
        "UPDATE users SET delay_min_s = 500, delay_max_s = 100 WHERE id = :id",
        id=user.id,
    )
    await _expect_integrity_error(
        db_session,
        "UPDATE users SET work_start = :s, work_end = :e WHERE id = :id",
        s=time(19, 0),
        e=time(9, 0),
        id=user.id,
    )
    await _expect_integrity_error(
        db_session, "UPDATE users SET working_days = '{}' WHERE id = :id", id=user.id
    )


async def test_sent_requires_approval_and_one_pending_per_turn(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    contact = await make_contact(user)
    turn = await _turn(db_session, user, chat.id, contact.id)
    draft = await drafts.create_draft(
        db_session,
        user.id,
        conversation_turn_id=turn.id,
        text="synthetic draft",
        output_json={},
        prepared_at=NOW,
    )
    assert draft.remind_at == NOW + timedelta(hours=2)
    assert draft.expires_at == NOW + timedelta(hours=24)

    # Principle I at the database level.
    await _expect_integrity_error(
        db_session, "UPDATE drafts SET state = 'sent' WHERE id = :id", id=draft.id
    )
    # Only one pending draft per turn.
    await _expect_integrity_error(
        db_session,
        "INSERT INTO drafts (id, user_id, conversation_turn_id, version, output_json,"
        " prompt_version, llm_provider, llm_model, llm_usage_json, state, prepared_at,"
        " remind_at, expires_at, outdated)"
        " VALUES (gen_random_uuid(), :u, :t, 2, '{}', '', '', '', '{}', 'pending', now(),"
        " now(), now(), false)",
        u=user.id,
        t=turn.id,
    )

    await drafts.finalize(
        db_session, user.id, draft.id, DraftState.sent, at=NOW, approved_by_user_at=NOW
    )
    assert draft.finalized_at == NOW
    with pytest.raises(ValueError):
        await drafts.finalize(db_session, user.id, draft.id, DraftState.pending, at=NOW)


async def test_commitment_confidence_check(
    db_session: AsyncSession, make_user: MakeUser, make_contact: MakeContact
) -> None:
    user = await make_user()
    contact = await make_contact(user)
    ok = await commitments.add_commitment(
        db_session,
        user.id,
        contact_id=contact.id,
        what="send the synthetic deck",
        due=None,
        source_sent_at=NOW,
        confidence=0.7,
        source_message_text="I'll send it",
    )
    assert [c.id for c in await commitments.list_open(db_session, user.id)] == [ok.id]
    await commitments.close(db_session, user.id, ok.id, CommitmentState.resolved, at=NOW)
    assert ok.closed_at == NOW
    assert await commitments.list_open(db_session, user.id) == []

    async with db_session.begin_nested():
        with pytest.raises(IntegrityError):
            await commitments.add_commitment(
                db_session,
                user.id,
                contact_id=contact.id,
                what="low confidence",
                due=None,
                source_sent_at=NOW,
                confidence=0.69,
            )


async def test_encrypted_columns_are_ciphertext_at_rest(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    plaintext = "Synthetic: отчёт в пятницу"
    message = await messages.add_message(
        db_session,
        user.id,
        work_chat_id=chat.id,
        telegram_message_id=1,
        text=plaintext,
        sent_at=NOW,
    )
    await users.upsert_telegram_session(
        db_session,
        user.id,
        "synthetic-session-string",
        method=LoginMethod.phone_code,
        connected_at=NOW,
    )

    raw_text = (
        await db_session.execute(
            text("SELECT text FROM messages WHERE id = :id"), {"id": message.id}
        )
    ).scalar_one()
    raw_session = (
        await db_session.execute(
            text("SELECT session_string FROM telegram_sessions WHERE user_id = :id"),
            {"id": user.id},
        )
    ).scalar_one()
    assert raw_text.startswith("e2:v1:") and plaintext not in raw_text
    assert raw_session.startswith("e2:v1:") and "synthetic-session-string" not in raw_session

    db_session.expunge_all()
    loaded = await messages.get_message(db_session, user.id, message.id)
    assert loaded is not None and loaded.text == plaintext


async def test_repos_are_scoped_by_user(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    alice, bob = await make_user(), await make_user()
    chat = await make_chat(alice)
    contact = await make_contact(alice)
    turn = await _turn(db_session, alice, chat.id, contact.id)
    draft = await drafts.create_draft(
        db_session,
        alice.id,
        conversation_turn_id=turn.id,
        text="synthetic",
        output_json={},
        prepared_at=NOW,
    )
    assert await turns.get_turn(db_session, bob.id, turn.id) is None
    assert await drafts.get_draft(db_session, bob.id, draft.id) is None
    assert await drafts.get_pending_for_turn(db_session, alice.id, turn.id) is not None
    found = await turns.find_turn_in_state(
        db_session, alice.id, work_chat_id=chat.id, state=TurnState.waiting
    )
    assert found is not None and found.id == turn.id


async def test_profiles_and_activity(
    db_session: AsyncSession, make_user: MakeUser, make_contact: MakeContact
) -> None:
    user = await make_user()
    contact = await make_contact(user)
    global_profile = await profiles.create_profile(
        db_session,
        user.id,
        scope=ProfileScope.global_,
        rules_json={"formality": 2},
        summary_text="Short and friendly.",
        examples=["synthetic example"],
        sample_size=60,
    )
    await profiles.create_profile(
        db_session,
        user.id,
        scope=ProfileScope.contact,
        contact_id=contact.id,
        rules_json={},
        summary_text="",
        examples=[],
        sample_size=10,
    )
    current = await profiles.get_current_profile(db_session, user.id, scope=ProfileScope.global_)
    assert current is not None and current.id == global_profile.id
    contact_profile = await profiles.get_current_profile(
        db_session, user.id, scope=ProfileScope.contact, contact_id=contact.id
    )
    assert contact_profile is not None and contact_profile.scope == "contact"

    raw_scope = (
        await db_session.execute(
            text("SELECT scope::text FROM style_profiles WHERE id = :id"), {"id": global_profile.id}
        )
    ).scalar_one()
    assert raw_scope == "global"

    await activity.record(db_session, user.id, ActivityKind.draft_prepared, meta={"n": 1})
    recent = await activity.list_recent(db_session, user.id)
    assert [r.kind for r in recent] == [ActivityKind.draft_prepared]


async def test_delete_user_cascades_and_removes_fsm_state(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user = await make_user()
    other = await make_user()
    chat = await make_chat(user)
    contact = await make_contact(user)
    await messages.add_message(
        db_session,
        user.id,
        work_chat_id=chat.id,
        telegram_message_id=7,
        text="synthetic",
        sent_at=NOW,
        sender_contact_id=contact.id,
    )
    turn = await _turn(db_session, user, chat.id, contact.id)
    await drafts.create_draft(
        db_session, user.id, conversation_turn_id=turn.id, text="x", output_json={}, prepared_at=NOW
    )
    db_session.add_all(
        [
            BotFsmState(chat_id=user.telegram_control_chat_id, state="setup:phone", data={}),
            BotFsmState(chat_id=other.telegram_control_chat_id, state=None, data={}),
        ]
    )
    await db_session.flush()

    await users.delete_user(db_session, user.id)
    db_session.expunge_all()

    for model in (Message, ConversationTurn, Draft):
        count = await db_session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user.id)
        )
        assert count == 0, model.__tablename__
    remaining = (await db_session.execute(select(BotFsmState.chat_id))).scalars().all()
    assert remaining == [other.telegram_control_chat_id]


def test_migrations_match_models(migrated_database_url: str) -> None:
    """`alembic check` fails if the models drifted from the migrations."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=ROOT,
        env={**os.environ, "SCRIBE_DATABASE_URL": migrated_database_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_telegram_session_login_method_reconnect_and_delete(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user(telegram_user_id=4_200_001)
    first = await users.upsert_telegram_session(
        db_session, user.id, "synthetic-session-a", method=LoginMethod.phone_code, connected_at=NOW
    )
    assert first.login_method == LoginMethod.phone_code and first.connected_at == NOW
    await users.set_telegram_session_status(
        db_session, user.id, SessionStatus.error, last_error="AuthKeyUnregistered"
    )

    # Reconnect by QR: same row, every login field refreshed.
    later = NOW + timedelta(days=3)
    second = await users.upsert_telegram_session(
        db_session, user.id, "synthetic-session-b", method=LoginMethod.qr, connected_at=later
    )
    db_session.expunge_all()
    loaded = await users.get_telegram_session(db_session, user.id)
    assert loaded is not None and loaded.id == first.id == second.id
    assert loaded.session_string == "synthetic-session-b"
    assert loaded.login_method == LoginMethod.qr and loaded.connected_at == later
    assert loaded.status == SessionStatus.active and loaded.last_error is None
    raw_method = (
        await db_session.execute(
            text("SELECT login_method::text FROM telegram_sessions WHERE user_id = :id"),
            {"id": user.id},
        )
    ).scalar_one()
    assert raw_method == "qr"

    owner = await users.find_user_by_telegram_user_id(db_session, 4_200_001)
    assert owner is not None and owner.id == user.id
    assert await users.find_user_by_telegram_user_id(db_session, 4_200_999) is None

    await users.delete_telegram_session(db_session, user.id)
    assert await users.get_telegram_session(db_session, user.id) is None
    # Deleting a missing row is a no-op.
    await users.delete_telegram_session(db_session, user.id)


async def test_activity_record_telegram_login(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    meta = {"method": "qr", "purpose": "setup", "outcome": "connected", "qr_codes_shown": 1}
    await activity.record(db_session, user.id, ActivityKind.telegram_login, meta=meta)
    recent = await activity.list_recent(db_session, user.id)
    assert [(r.kind, r.meta_json) for r in recent] == [(ActivityKind.telegram_login, meta)]


def _alembic(database_url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "SCRIBE_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_migration_0002_backfills_and_downgrades(migrated_database_url: str) -> None:
    """0002 on a scratch database: backfill of an existing session row, then down and up again."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    created_at = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)
    try:
        _alembic(scratch_url, "upgrade", "0001")
        async with scratch.begin() as connection:
            user_id = (
                await connection.execute(
                    text(
                        "INSERT INTO users (id, telegram_control_chat_id, display_name, timezone,"
                        " working_days, work_start, work_end, delay_min_s, delay_max_s, state,"
                        " llm_tier) VALUES (gen_random_uuid(), 4200100, 'Synthetic', 'UTC',"
                        " '{0,1,2,3,4}', '09:00', '18:00', 0, 0, 'active', 'quality')"
                        " RETURNING id"
                    )
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO telegram_sessions (id, user_id, session_string, status,"
                    " created_at) VALUES (gen_random_uuid(), :u, 'v1:synthetic', 'active', :c)"
                ),
                {"u": user_id, "c": created_at},
            )

        _alembic(scratch_url, "upgrade", "0002")
        async with scratch.begin() as connection:
            method, connected_at = (
                await connection.execute(
                    text("SELECT login_method::text, connected_at FROM telegram_sessions")
                )
            ).one()
            assert (method, connected_at) == ("phone_code", created_at)
            nullable = (
                await connection.execute(
                    text(
                        "SELECT column_name, is_nullable FROM information_schema.columns"
                        " WHERE table_name = 'telegram_sessions'"
                        " AND column_name IN ('login_method', 'connected_at', 'account_label')"
                    )
                )
            ).all()
            assert sorted(nullable) == [
                ("account_label", "YES"),
                ("connected_at", "NO"),
                ("login_method", "NO"),
            ]
            await connection.execute(
                text(
                    "INSERT INTO activity_records (id, user_id, kind, at, meta_json)"
                    " VALUES (gen_random_uuid(), :u, 'telegram_login', now(), '{}')"
                ),
                {"u": user_id},
            )

        _alembic(scratch_url, "downgrade", "0001")
        async with scratch.begin() as connection:
            columns = (
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'telegram_sessions'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert not {"login_method", "connected_at", "account_label"} & set(columns)
            enum_exists = await connection.scalar(
                text("SELECT count(*) FROM pg_type WHERE typname = 'telegram_login_method'")
            )
            assert enum_exists == 0

        _alembic(scratch_url, "upgrade", "head")
        # 0003 (issue #8): existing users keep the English UI; only en / ru are allowed.
        async with scratch.begin() as connection:
            language = await connection.scalar(
                text("SELECT language FROM users WHERE id = :u"), {"u": user_id}
            )
            assert language == "en"
        with pytest.raises(IntegrityError):
            async with scratch.begin() as connection:
                await connection.execute(
                    text("UPDATE users SET language = 'de' WHERE id = :u"), {"u": user_id}
                )
        _alembic(scratch_url, "downgrade", "0002")
        async with scratch.begin() as connection:
            columns = (
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'users'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "language" not in columns
        _alembic(scratch_url, "upgrade", "head")
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


# --- 004: skip closing messages (migration 0008, reply-gate queries) ---------------------------


async def test_migration_0008_upgrade_downgrade(migrated_database_url: str) -> None:
    """0008 on a scratch database from 0007 (search earlier): head, down to 0007, head again."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)

    async def columns(table: str) -> set[str]:
        async with scratch.connect() as connection:
            rows = await connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            )
            return set(rows.scalars().all())

    async def index_def() -> str | None:
        async with scratch.connect() as connection:
            definition = await connection.scalar(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_turns_user_skipped'")
            )
            return None if definition is None else str(definition)

    added = {
        "reply_decision",
        "skip_category",
        "decision_source",
        "decision_confidence",
        "decided_at",
        "draft_anyway_at",
    }
    try:
        _alembic(scratch_url, "upgrade", "0007")
        assert "search_earlier_messages" in await columns("users")
        _alembic(scratch_url, "upgrade", "head")
        assert added <= await columns("conversation_turns")
        assert "skip_closing" in await columns("users")
        assert "skip_closing" in await columns("work_chats")
        assert "possibly_unnecessary" in await columns("drafts")
        definition = await index_def()
        assert definition is not None and "skipped" in definition and "DESC" in definition

        _alembic(scratch_url, "downgrade", "0007")
        assert not added & await columns("conversation_turns")
        assert "search_earlier_messages" in await columns("users")  # 0007 itself is untouched
        assert "skip_closing" not in await columns("users")
        assert "possibly_unnecessary" not in await columns("drafts")
        assert await index_def() is None
        async with scratch.connect() as connection:
            types = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_type WHERE typname IN"
                    " ('reply_decision', 'skip_category', 'decision_source')"
                )
            )
            assert types == 0

        _alembic(scratch_url, "upgrade", "head")
        assert added <= await columns("conversation_turns")
        assert await index_def() is not None
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_skip_requires_category_constraint(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user = await make_user()
    assert user.skip_closing is True
    chat, contact = await make_chat(user), await make_contact(user)
    assert chat.skip_closing is None
    turn = await _turn(db_session, user, chat.id, contact.id)
    await _expect_integrity_error(
        db_session,
        "UPDATE conversation_turns SET reply_decision = 'skip' WHERE id = :id",
        id=turn.id,
    )
    await _expect_integrity_error(
        db_session,
        "UPDATE conversation_turns SET decision_confidence = 1.5 WHERE id = :id",
        id=turn.id,
    )
    await db_session.execute(
        text(
            "UPDATE conversation_turns SET reply_decision = 'skip', skip_category = 'thanks',"
            " state = 'skipped', decision_confidence = 0.9 WHERE id = :id"
        ),
        {"id": turn.id},
    )


_tg_ids = iter(range(5_000_000, 6_000_000))


async def _message(
    db: AsyncSession,
    user: User,
    chat_id: uuid.UUID,
    *,
    at: datetime,
    outgoing: bool = False,
    sender: uuid.UUID | None = None,
    body: str = "synthetic",
) -> Message:
    return await messages.add_message(
        db,
        user.id,
        work_chat_id=chat_id,
        telegram_message_id=next(_tg_ids),
        text=body,
        sent_at=at,
        is_outgoing=outgoing,
        sender_contact_id=None if outgoing else sender,
    )


async def _closing_turn(
    db: AsyncSession,
    user: User,
    chat_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    at: datetime,
    state: TurnState = TurnState.skipped,
    category: SkipCategory | None = SkipCategory.thanks,
    **values: object,
) -> ConversationTurn:
    """A turn with one incoming message at ``at`` and the given gate outcome."""
    message = await _message(db, user, chat_id, at=at, sender=contact_id)
    turn = await turns.create_turn(
        db,
        user.id,
        work_chat_id=chat_id,
        contact_id=contact_id,
        first_message_id=message.id,
        arrived_at=at,
        delay_s=0,
        due_at=at,
    )
    turn.state = state
    turn.skip_category = category
    if category is not None:
        turn.reply_decision = ReplyDecision.skip if state == TurnState.skipped else None
        turn.decision_source = DecisionSource.model
        turn.decided_at = at
    for key, value in values.items():
        setattr(turn, key, value)
    await db.flush()
    return turn


async def test_habit_replied_count_branches(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, other_user = await make_user(), await make_user()
    chat, other_chat = await make_chat(user), await make_chat(user)
    contact = await make_contact(user)
    before = NOW
    reply_window, retention = timedelta(minutes=30), 30

    async def count(window: int = 5, chat_id: uuid.UUID = chat.id) -> int:
        return await turns.habit_replied_count(
            db_session,
            user.id,
            chat_id,
            before=before,
            window=window,
            reply_window=reply_window,
            retention_days=retention,
        )

    base = NOW - timedelta(days=3)
    # 1. answered by a sent draft -> counts
    await _closing_turn(db_session, user, chat.id, contact.id, at=base, state=TurnState.answered)
    assert await count() == 1
    # 2. dropped because of a manual reply -> counts
    await _closing_turn(
        db_session, user, chat.id, contact.id, at=base + timedelta(hours=1),
        state=TurnState.dropped, dropped_reason=DroppedReason.manual_reply,
    )  # fmt: skip
    assert await count() == 2
    # 3. outgoing 10 min after the turn's last message -> counts
    t3 = base + timedelta(hours=2)
    await _closing_turn(db_session, user, chat.id, contact.id, at=t3)
    await _message(db_session, user, chat.id, at=t3 + timedelta(minutes=10), outgoing=True)
    assert await count() == 3
    # 4. outgoing 40 min after -> does not count
    t4 = base + timedelta(hours=4)
    await _closing_turn(db_session, user, chat.id, contact.id, at=t4)
    await _message(db_session, user, chat.id, at=t4 + timedelta(minutes=40), outgoing=True)
    assert await count() == 3
    # 5. outgoing after the next incoming turn arrived -> does not count
    t5 = base + timedelta(hours=6)
    await _closing_turn(db_session, user, chat.id, contact.id, at=t5)
    await _closing_turn(
        db_session, user, chat.id, contact.id, at=t5 + timedelta(minutes=5), category=None,
        state=TurnState.rejected,
    )  # fmt: skip
    await _message(db_session, user, chat.id, at=t5 + timedelta(minutes=10), outgoing=True)
    assert await count() == 3
    # Only the last ``window`` closing turns are examined: turns 3, 4, 5 -> one answered.
    assert await count(window=3) == 1
    # Older than the retention window: ignored.
    await _closing_turn(
        db_session, user, chat.id, contact.id, at=NOW - timedelta(days=31),
        state=TurnState.answered,
    )  # fmt: skip
    assert await count(window=10) == 3
    # skip_category IS NULL: not a closing turn, ignored even when answered.
    await _closing_turn(
        db_session, user, chat.id, contact.id, at=base - timedelta(hours=1), category=None,
        state=TurnState.answered,
    )  # fmt: skip
    assert await count(window=10) == 3
    # Turns at or after ``before`` are ignored.
    await _closing_turn(db_session, user, chat.id, contact.id, at=NOW, state=TurnState.answered)
    assert await count(window=10) == 3
    # Another chat and another user's turns are ignored.
    await _closing_turn(
        db_session, user, other_chat.id, contact.id, at=base, state=TurnState.answered
    )
    other_contact = await make_contact(other_user)
    other_users_chat = await make_chat(other_user)
    await _closing_turn(
        db_session, other_user, other_users_chat.id, other_contact.id, at=base,
        state=TurnState.answered,
    )  # fmt: skip
    assert await count(window=10) == 3
    assert await count(chat_id=other_users_chat.id) == 0
    assert await count(chat_id=other_chat.id) == 1


async def test_set_decision_guarded_and_keeps_category(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, other = await make_user(), await make_user()
    chat, contact = await make_chat(user), await make_contact(user)
    turn = await _turn(db_session, user, chat.id, contact.id)  # waiting

    async def decide(**values: object) -> bool:
        return await turns.set_decision(
            db_session,
            values.pop("user_id", user.id),  # type: ignore[arg-type]
            turn.id,
            decision=values.pop("decision", ReplyDecision.draft),  # type: ignore[arg-type]
            source=values.pop("source", DecisionSource.fallback),  # type: ignore[arg-type]
            category=values.pop("category", SkipCategory.thanks),  # type: ignore[arg-type]
            confidence=values.pop("confidence", 0.7),  # type: ignore[arg-type]
            decided_at=NOW,
            **values,  # type: ignore[arg-type]
        )

    async def load() -> ConversationTurn:
        db_session.expunge_all()
        loaded = await turns.get_turn(db_session, user.id, turn.id)
        assert loaded is not None
        return loaded

    assert await decide() is False  # not reading
    turn.state = TurnState.reading
    await db_session.flush()
    assert await decide(user_id=other.id) is False  # foreign user
    assert await decide() is True
    loaded = await load()
    assert (loaded.reply_decision, loaded.decision_source) == ("draft", "fallback")
    assert loaded.skip_category == "thanks" and float(loaded.decision_confidence or 0) == 0.7
    assert loaded.decided_at == NOW and loaded.state == TurnState.reading

    # A later decision without a category keeps the stored one (habit rule input).
    assert await decide(source=DecisionSource.user, category=None, confidence=None) is True
    loaded = await load()
    assert loaded.skip_category == "thanks" and loaded.decision_source == "user"
    assert loaded.decision_confidence is None

    assert await decide(decision=ReplyDecision.skip, state=TurnState.skipped) is True
    assert (await load()).state == TurnState.skipped
    assert await decide() is False  # no longer reading


async def test_draft_anyway_available_rules(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user = await make_user()
    chat, other_chat = await make_chat(user), await make_chat(user)
    contact = await make_contact(user)
    t0 = NOW - timedelta(hours=5)
    turn = await _closing_turn(db_session, user, chat.id, contact.id, at=t0)
    assert await turns.draft_anyway_available(db_session, user.id, turn) is True

    # An outgoing message in another chat, or before the turn, does not matter.
    await _message(db_session, user, other_chat.id, at=t0 + timedelta(minutes=1), outgoing=True)
    await _message(db_session, user, chat.id, at=t0 - timedelta(minutes=1), outgoing=True)
    assert await turns.draft_anyway_available(db_session, user.id, turn) is True

    # A later answered turn in the chat removes the option.
    later = await _closing_turn(
        db_session, user, chat.id, contact.id, at=t0 + timedelta(hours=1), category=None,
        state=TurnState.answered,
    )  # fmt: skip
    assert await turns.draft_anyway_available(db_session, user.id, turn) is False
    later.state = TurnState.rejected
    await db_session.flush()
    assert await turns.draft_anyway_available(db_session, user.id, turn) is True

    # A manual reply after the turn removes it too.
    await _message(db_session, user, chat.id, at=t0 + timedelta(minutes=2), outgoing=True)
    assert await turns.draft_anyway_available(db_session, user.id, turn) is False

    # Purged messages: nothing to draft from.
    gone = await _closing_turn(db_session, user, other_chat.id, contact.id, at=t0)
    gone.first_message_id = gone.last_message_id = None
    await db_session.flush()
    assert await turns.draft_anyway_available(db_session, user.id, gone) is False


async def test_mark_draft_anyway_once(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, other = await make_user(), await make_user()
    chat, contact = await make_chat(user), await make_contact(user)
    turn = await _closing_turn(db_session, user, chat.id, contact.id, at=NOW - timedelta(hours=1))
    assert await turns.mark_draft_anyway(db_session, other.id, turn.id, now=NOW) is False
    assert await turns.mark_draft_anyway(db_session, user.id, turn.id, now=NOW) is True
    assert await turns.mark_draft_anyway(db_session, user.id, turn.id, now=NOW) is False
    db_session.expunge_all()
    loaded = await turns.get_turn(db_session, user.id, turn.id)
    assert loaded is not None
    assert (loaded.state, loaded.due_at, loaded.draft_anyway_at) == (TurnState.waiting, NOW, NOW)
    assert loaded.decision_source == "user" and loaded.skip_category == "thanks"


async def test_list_and_count_skipped(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, other = await make_user(), await make_user()
    chat, contact = await make_chat(user), await make_contact(user)
    newest = await _closing_turn(db_session, user, chat.id, contact.id, at=NOW - timedelta(days=1))
    older = await _closing_turn(db_session, user, chat.id, contact.id, at=NOW - timedelta(days=6))
    await _closing_turn(db_session, user, chat.id, contact.id, at=NOW - timedelta(days=8))
    await _closing_turn(
        db_session, user, chat.id, contact.id, at=NOW - timedelta(days=2), state=TurnState.answered
    )
    other_chat, other_contact = await make_chat(other), await make_contact(other)
    await _closing_turn(db_session, other, other_chat.id, other_contact.id, at=NOW)
    since = NOW - timedelta(days=7)

    listed = await turns.list_skipped(db_session, user.id, since=since, limit=10, offset=0)
    assert [t.id for t in listed] == [newest.id, older.id]
    assert await turns.count_skipped(db_session, user.id, since=since) == 2
    page2 = await turns.list_skipped(db_session, user.id, since=since, limit=1, offset=1)
    assert [t.id for t in page2] == [older.id]


# --- 009: mark chats unread (migration 0010) ------------------------------------------------------


async def test_migration_0010_mark_unread(migrated_database_url: str) -> None:
    """0010 on a scratch database from 0009: existing rows get the defaults, the floor check
    holds, and the downgrade drops both columns."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)

    async def columns(table: str) -> set[str]:
        async with scratch.connect() as connection:
            rows = await connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            )
            return set(rows.scalars().all())

    try:
        _alembic(scratch_url, "upgrade", "0009")
        async with scratch.begin() as connection:
            user_id = (
                await connection.execute(
                    text(
                        "INSERT INTO users (id, telegram_control_chat_id, display_name, timezone,"
                        " working_days, work_start, work_end, delay_min_s, delay_max_s, state,"
                        " llm_tier) VALUES (gen_random_uuid(), 4200900, 'Synthetic', 'UTC',"
                        " '{0,1,2,3,4}', '09:00', '18:00', 0, 0, 'active', 'quality')"
                        " RETURNING id"
                    )
                )
            ).scalar_one()
            chat_id = (
                await connection.execute(
                    text(
                        "INSERT INTO work_chats (id, user_id, telegram_chat_id, title, kind,"
                        " enabled) VALUES (gen_random_uuid(), :u, 777, 'Synthetic',"
                        " 'private', true) RETURNING id"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()

        _alembic(scratch_url, "upgrade", "0010")
        async with scratch.begin() as connection:
            mark_unread = await connection.scalar(
                text("SELECT mark_unread FROM users WHERE id = :u"), {"u": user_id}
            )
            floor = await connection.scalar(
                text("SELECT unread_mark_floor FROM work_chats WHERE id = :c"), {"c": chat_id}
            )
            assert mark_unread is True
            assert floor is None
        for bad in (0, -5):
            with pytest.raises(IntegrityError):
                async with scratch.begin() as connection:
                    await connection.execute(
                        text("UPDATE work_chats SET unread_mark_floor = :f WHERE id = :c"),
                        {"f": bad, "c": chat_id},
                    )
        async with scratch.begin() as connection:
            await connection.execute(
                text("UPDATE work_chats SET unread_mark_floor = 42 WHERE id = :c"), {"c": chat_id}
            )

        _alembic(scratch_url, "downgrade", "0009")
        assert "mark_unread" not in await columns("users")
        assert "unread_mark_floor" not in await columns("work_chats")
        assert "skip_closing" in await columns("users")  # earlier revisions are untouched
        assert "group_mode" not in await columns("work_chats")  # 0009 is still applied

        _alembic(scratch_url, "upgrade", "head")
        assert "mark_unread" in await columns("users")
        assert "unread_mark_floor" in await columns("work_chats")
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_mark_unread_defaults_on_new_rows(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    user = await make_user()
    assert user.mark_unread is True
    chat = await make_chat(user)
    assert chat.unread_mark_floor is None
    await _expect_integrity_error(
        db_session, "UPDATE work_chats SET unread_mark_floor = 0 WHERE id = :id", id=chat.id
    )


# --- specs/008 work account mode (T005) ----------------------------------------------------------


async def test_work_account_defaults_and_excluded_check(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    assert user.work_account is False and user.work_account_changed_at is None
    assert chat.origin == WorkChatOrigin.picked and chat.excluded is False
    # Server defaults: a row inserted without the new columns gets picked / false.
    raw = await db_session.execute(
        text(
            "INSERT INTO work_chats (id, user_id, telegram_chat_id, title, kind, enabled)"
            " VALUES (:id, :u, 900000777, '', 'private', true)"
            " RETURNING origin, excluded"
        ),
        {"id": uuid.uuid4(), "u": user.id},
    )
    assert tuple(raw.one()) == ("picked", False)
    await _expect_integrity_error(
        db_session, "UPDATE work_chats SET excluded = true WHERE id = :id", id=chat.id
    )
    await db_session.execute(
        text("UPDATE work_chats SET origin = 'auto', excluded = true WHERE id = :id"),
        {"id": chat.id},
    )
    for value in ("work_account_on", "work_account_off", "chat_excluded", "chat_included"):
        assert ActivityKind(value).value == value
    assert DroppedReason.work_account_off.value == "work_account_off"


async def test_migration_0013_upgrade_downgrade(migrated_database_url: str) -> None:
    """0013 on a scratch database from 0010: head, down to 0010, head again."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)

    async def columns(table: str) -> set[str]:
        async with scratch.connect() as connection:
            rows = await connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            )
            return set(rows.scalars().all())

    try:
        _alembic(scratch_url, "upgrade", "0010")
        assert "origin" not in await columns("work_chats")
        _alembic(scratch_url, "upgrade", "head")
        assert {"origin", "excluded"} <= await columns("work_chats")
        assert {"work_account", "work_account_changed_at"} <= await columns("users")
        _alembic(scratch_url, "downgrade", "0010")
        assert not {"origin", "excluded"} & await columns("work_chats")
        assert "work_account" not in await columns("users")
        async with scratch.connect() as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM pg_type WHERE typname = 'work_chat_origin'")
            )
            assert count == 0
        _alembic(scratch_url, "upgrade", "head")
        assert {"origin", "excluded"} <= await columns("work_chats")
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_migration_0014_downgrade_clears_forwarded_decisions(
    migrated_database_url: str,
) -> None:
    """#47: 0014 adds the forward columns and ``skip_category = 'forwarded'``; its downgrade
    clears the gate decision of turns using that value (older code cannot load it)."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)
    ids = {key: uuid.uuid4() for key in ("user", "chat", "contact", "forwarded", "thanks")}

    async def columns(table: str) -> set[str]:
        async with scratch.connect() as connection:
            rows = await connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            )
            return set(rows.scalars().all())

    try:
        _alembic(scratch_url, "upgrade", "head")
        assert {"is_forwarded", "forwarded_from"} <= await columns("messages")
        async with scratch.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, telegram_control_chat_id, display_name, timezone,"
                    " working_days, work_start, work_end, delay_min_s, delay_max_s, state)"
                    " VALUES (:user, 900000901, 'x', 'UTC', '{1,2,3,4,5}', '09:00', '18:00',"
                    " 60, 120, 'active')"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO work_chats (id, user_id, telegram_chat_id, title, kind, enabled)"
                    " VALUES (:chat, :user, 900000902, 'x', 'private', true)"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO contacts (id, user_id, telegram_peer_id, display_name, kind,"
                    " own_message_count_6m, pinned_profile)"
                    " VALUES (:contact, :user, 900000902, 'x', 'person', 0, false)"
                ),
                ids,
            )
            for turn, category in (("forwarded", "forwarded"), ("thanks", "thanks")):
                await connection.execute(
                    text(
                        "INSERT INTO conversation_turns (id, user_id, work_chat_id, contact_id,"
                        " arrived_at, delay_s, due_at, state, reply_decision, skip_category,"
                        " decision_source) VALUES (:id, :user, :chat, :contact, now(), 0, now(),"
                        " 'skipped', 'skip', CAST(:category AS skip_category), 'pregate')"
                    ),
                    {**ids, "id": ids[turn], "category": category},
                )
        _alembic(scratch_url, "downgrade", "0013")
        assert not {"is_forwarded", "forwarded_from"} & await columns("messages")
        async with scratch.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT id, state, reply_decision, skip_category, decision_source"
                    " FROM conversation_turns"
                )
            )
            by_id = {row[0]: tuple(row[1:]) for row in rows.all()}
        assert by_id[ids["forwarded"]] == ("skipped", None, None, None)
        assert by_id[ids["thanks"]] == ("skipped", "skip", "thanks", "pregate")
        _alembic(scratch_url, "upgrade", "head")
        assert {"is_forwarded", "forwarded_from"} <= await columns("messages")
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_migration_auto_send(migrated_database_url: str) -> None:
    """specs/014: the upgrade defaults auto-send off for existing rows, the checks hold (Principle
    I: no ``sent`` draft without an approval, no countdown without a posted card), the decisions
    table cascades, and the downgrade removes everything and restores the old check."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)
    ids = {key: uuid.uuid4() for key in ("user", "chat", "contact", "turn", "draft", "turn2")}

    async def exists(query: str) -> bool:
        async with scratch.connect() as connection:
            return bool((await connection.execute(text(query))).scalar())

    async def rejected(statement: str, **params: object) -> None:
        with pytest.raises(IntegrityError):
            async with scratch.begin() as connection:
                await connection.execute(text(statement), {**ids, **params})

    async def accepted(statement: str, **params: object) -> None:
        async with scratch.begin() as connection:
            await connection.execute(text(statement), {**ids, **params})

    def draft_sql(turn: str, state: str, extra: str = "", values: str = "") -> str:
        return (
            "INSERT INTO drafts (id, user_id, conversation_turn_id, version, output_json,"
            " prompt_version, llm_provider, llm_model, llm_usage_json, state, prepared_at,"
            f" remind_at, expires_at, outdated{extra}) VALUES (gen_random_uuid(), :user,"
            f" :{turn}, 1, '{{}}', '', '', '', '{{}}', '{state}', now(), now(), now(),"
            f" false{values})"
        )

    try:
        _alembic(scratch_url, "upgrade", "0020")
        await accepted(
            "INSERT INTO users (id, telegram_control_chat_id, display_name, timezone,"
            " working_days, work_start, work_end, delay_min_s, delay_max_s, state)"
            " VALUES (:user, 900001401, 'x', 'UTC', '{1,2,3,4,5}', '09:00', '18:00', 0, 0,"
            " 'active')"
        )
        await accepted(
            "INSERT INTO work_chats (id, user_id, telegram_chat_id, title, kind, enabled)"
            " VALUES (:chat, :user, 900001402, 'x', 'private', true)"
        )
        await accepted(
            "INSERT INTO contacts (id, user_id, telegram_peer_id, display_name, kind,"
            " own_message_count_6m, pinned_profile)"
            " VALUES (:contact, :user, 900001402, 'x', 'person', 0, false)"
        )
        for turn in ("turn", "turn2"):
            await accepted(
                "INSERT INTO conversation_turns (id, user_id, work_chat_id, contact_id,"
                " arrived_at, delay_s, due_at, state) VALUES (:id, :user, :chat, :contact,"
                " now(), 0, now(), 'pending_review')",
                id=ids[turn],
            )
        # A pending draft exists before the upgrade; the old check refuses a bare "sent".
        await accepted(draft_sql("turn", "pending").replace("gen_random_uuid()", ":draft", 1))
        await rejected("UPDATE drafts SET state = 'sent' WHERE id = :draft")

        _alembic(scratch_url, "upgrade", "head")

        async with scratch.connect() as connection:
            chat = (
                await connection.execute(
                    text(
                        "SELECT auto_send, auto_send_offer::text, auto_send_changed_at,"
                        " auto_send_source, auto_send_offer_not_before, auto_send_warned_at"
                        " FROM work_chats WHERE id = :chat"
                    ),
                    ids,
                )
            ).one()
            user = (
                await connection.execute(
                    text(
                        "SELECT auto_send_window_s, auto_send_last_offer_at FROM users"
                        " WHERE id = :user"
                    ),
                    ids,
                )
            ).one()
            draft = (
                await connection.execute(
                    text(
                        "SELECT auto_send_due_at, auto_send_stop, auto_approved_at FROM drafts"
                        " WHERE id = :draft"
                    ),
                    ids,
                )
            ).one()
        assert tuple(chat) == (False, "none", None, None, None, None)
        assert tuple(user) == (120, None)
        assert tuple(draft) == (None, None, None)

        for window in (59, 901):
            await rejected("UPDATE users SET auto_send_window_s = :w WHERE id = :user", w=window)
        await accepted("UPDATE users SET auto_send_window_s = 900 WHERE id = :user")
        await rejected("UPDATE work_chats SET auto_send = true WHERE id = :chat")
        await accepted(
            "UPDATE work_chats SET auto_send = true, auto_send_source = 'offer' WHERE id = :chat"
        )

        # Principle I at the database level: a sent draft needs one of the two approvals.
        await rejected("UPDATE drafts SET state = 'sent' WHERE id = :draft")
        await rejected("UPDATE drafts SET auto_approved_at = now() WHERE id = :draft")
        await rejected("UPDATE drafts SET auto_send_due_at = now() WHERE id = :draft")
        await rejected(
            "UPDATE drafts SET auto_send_due_at = now(), auto_approved_at = now(),"
            " state = 'sent' WHERE id = :draft"
        )
        await accepted("UPDATE drafts SET control_message_id = 77 WHERE id = :draft")
        await accepted("UPDATE drafts SET auto_send_due_at = now() WHERE id = :draft")
        await accepted(
            "UPDATE drafts SET auto_approved_at = now(), state = 'sent' WHERE id = :draft"
        )
        await accepted(draft_sql("turn2", "sent", ", approved_by_user_at", ", now()"))

        decision = (
            "INSERT INTO draft_decisions (id, user_id, work_chat_id, draft_id, decision,"
            " during_auto_send, at) VALUES (gen_random_uuid(), :user, :chat, :draft,"
            " 'auto_sent', true, now())"
        )
        await accepted(decision)
        async with scratch.begin() as connection:
            await connection.execute(text("DELETE FROM drafts WHERE id = :draft"), ids)
        async with scratch.connect() as connection:
            row = (await connection.execute(text("SELECT draft_id FROM draft_decisions"))).one()
        assert row[0] is None  # SET NULL: the decision outlives the draft
        async with scratch.begin() as connection:
            await connection.execute(text("DELETE FROM users WHERE id = :user"), ids)
        assert not await exists("SELECT count(*) > 0 FROM draft_decisions")

        _alembic(scratch_url, "downgrade", "0020")
        assert not await exists(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'draft_decisions'"
        )
        assert not await exists(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'drafts'"
            " AND column_name IN ('auto_send_due_at', 'auto_send_stop', 'auto_approved_at')"
        )
        assert not await exists(
            "SELECT 1 FROM information_schema.columns WHERE column_name LIKE 'auto_send%'"
            " AND table_name IN ('users', 'work_chats')"
        )
        assert not await exists(
            "SELECT 1 FROM pg_type WHERE typname IN ('auto_send_source',"
            " 'auto_send_offer_state', 'auto_send_stop', 'draft_decision')"
        )
        assert await exists(
            "SELECT pg_get_constraintdef(oid) LIKE '%approved_by_user_at%'"
            " AND pg_get_constraintdef(oid) NOT LIKE '%auto_approved_at%' FROM pg_constraint"
            " WHERE conname = 'ck_drafts_sent_requires_approval'"
        )
        _alembic(scratch_url, "upgrade", "head")
        assert await exists(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'draft_decisions'"
        )
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_migration_stars_payments(migrated_database_url: str) -> None:
    """specs/015: existing subscriptions become ``operator`` rows, the checks, unique keys and
    foreign keys of the three payment tables hold, the new event kinds are insertable, and the
    downgrade drops the tables, columns and enums."""
    name = f"scribe_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(scratch_url)
    ids = {
        key: uuid.uuid4()
        for key in ("sub_old", "offer", "offer2", "offer3", "stars", "stars2", "pay", "pay2")
    }

    async def exists(query: str) -> bool:
        async with scratch.connect() as connection:
            return bool((await connection.execute(text(query))).scalar())

    async def rejected(statement: str, **params: object) -> None:
        with pytest.raises(IntegrityError):
            async with scratch.begin() as connection:
                await connection.execute(text(statement), {**ids, **params})

    async def accepted(statement: str, **params: object) -> None:
        async with scratch.begin() as connection:
            await connection.execute(text(statement), {**ids, **params})

    def offer_sql(key: str, price: int = 150) -> str:
        return (
            "INSERT INTO payment_offers (id, telegram_id, price_stars, created_at)"
            f" VALUES (:{key}, 900001501, {price}, now())"
        )

    def stars_sql(key: str, offer: str, charge: str, state: str) -> str:
        return (
            "INSERT INTO stars_subscriptions (id, telegram_id, offer_id, first_charge_id,"
            " price_stars, started_at, period_end, state) VALUES"
            f" (:{key}, 900001501, :{offer}, '{charge}', 150, now(),"
            f" now() + interval '30 days', '{state}')"
        )

    def payment_sql(
        key: str, charge: str, amount: int = 150, end: str = "+ interval '30 days'"
    ) -> str:
        return (
            "INSERT INTO payments (id, telegram_id, charge_id, amount_stars, kind, paid_at,"
            " period_start, period_end, credited_via) VALUES"
            f" (:{key}, 900001501, '{charge}', {amount}, 'first', now(), now(), now() {end},"
            " 'update')"
        )

    try:
        _alembic(scratch_url, "upgrade", "0021")
        await accepted(
            "INSERT INTO subscriptions (id, telegram_id, starts_at, ends_at, granted_by)"
            " VALUES (:sub_old, 900001501, now(), now() + interval '30 days', 'operator')"
        )
        _alembic(scratch_url, "upgrade", "head")

        async with scratch.connect() as connection:
            old = (
                await connection.execute(
                    text("SELECT source::text, payment_id FROM subscriptions WHERE id = :sub_old"),
                    ids,
                )
            ).one()
        assert tuple(old) == ("operator", None)

        for price in (0, 10001):
            await rejected(offer_sql("offer", price))
        await accepted(offer_sql("offer"))
        await accepted(offer_sql("offer2"))
        await accepted(offer_sql("offer3"))

        await rejected(payment_sql("pay", "c-1", amount=0))
        await rejected(payment_sql("pay", "c-1", end="- interval '1 day'"))
        await accepted(payment_sql("pay", "c-1"))
        await rejected(payment_sql("pay2", "c-1"))  # charge_id is unique

        await accepted(stars_sql("stars", "offer", "c-1", "active"))
        await rejected(stars_sql("stars2", "offer2", "c-2", "canceled"))  # one running
        await accepted(stars_sql("stars2", "offer2", "c-2", "ended"))
        await rejected(stars_sql("stars2", "offer", "c-3", "ended"))  # one subscription per offer

        await accepted("UPDATE payments SET stars_subscription_id = :stars WHERE id = :pay")
        await accepted(
            "UPDATE subscriptions SET source = 'stars', payment_id = :pay WHERE id = :sub_old"
        )
        for kind in (
            "payment_received",
            "payment_renewed",
            "payment_refunded",
            "stars_canceled",
            "stars_resumed",
            "stars_failed",
            "stars_ended",
        ):
            await accepted(
                "INSERT INTO access_events (id, telegram_id, kind, actor, meta_json, at)"
                f" VALUES (gen_random_uuid(), 900001501, '{kind}', 'system', '{{}}', now())"
            )

        # Deleting the parent rows only unlinks the children.
        async with scratch.begin() as connection:
            await connection.execute(text("DELETE FROM stars_subscriptions WHERE id = :stars"), ids)
        async with scratch.connect() as connection:
            link = (
                await connection.execute(
                    text("SELECT stars_subscription_id FROM payments WHERE id = :pay"), ids
                )
            ).scalar()
        assert link is None
        async with scratch.begin() as connection:
            await connection.execute(text("DELETE FROM payments WHERE id = :pay"), ids)
        async with scratch.connect() as connection:
            link = (
                await connection.execute(
                    text("SELECT payment_id FROM subscriptions WHERE id = :sub_old"), ids
                )
            ).scalar()
        assert link is None

        _alembic(scratch_url, "downgrade", "0021")
        assert not await exists(
            "SELECT 1 FROM information_schema.tables WHERE table_name IN"
            " ('payments', 'stars_subscriptions', 'payment_offers')"
        )
        assert not await exists(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'subscriptions'"
            " AND column_name IN ('source', 'payment_id')"
        )
        assert not await exists(
            "SELECT 1 FROM pg_type WHERE typname IN ('stars_sub_state', 'stars_cancel_source',"
            " 'stars_end_reason', 'payment_kind', 'payment_source', 'subscription_source')"
        )
        assert not await exists(
            "SELECT 1 FROM access_events"
            " WHERE kind::text LIKE 'payment_%' OR kind::text LIKE 'stars_%'"
        )
        _alembic(scratch_url, "upgrade", "head")
        assert await exists("SELECT 1 FROM information_schema.tables WHERE table_name = 'payments'")
    finally:
        await scratch.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
