"""Issue #57: personal data encrypted at rest, rewritten from plaintext once, still exported
decrypted, and stale chat / contact metadata purged. Synthetic data."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import applications
from scribe.config import Settings
from scribe.crypto import is_ciphertext
from scribe.db.models import (
    Commitment,
    CommitmentState,
    Contact,
    ContactKind,
    ConversationTurn,
    DraftState,
    LoginMethod,
    ProfileScope,
    SessionStatus,
    TaskProvider,
    TurnState,
    WorkChat,
    WorkChatOrigin,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments, drafts, messages, profiles, tasks_conn, turns
from scribe.db.repos import users as users_repo
from scribe.retention.purge import STALE_RETENTION, purge_messages
from scribe.retention.reencrypt import reencrypt_legacy_rows
from scribe.retention.user_data import export_user_data
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser, next_telegram_id

CIPHERTEXT = re.compile(r"^e2:[^:]+:[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$")  # column-bound (#59)


async def _raw(
    session_factory: async_sessionmaker[AsyncSession], sql: str, **params: object
) -> object:
    async with session_factory() as session:
        return await session.scalar(text(sql), params)


def _ciphertexts(value: object) -> list[str]:
    if isinstance(value, dict):
        return [found for item in value.values() for found in _ciphertexts(item)]
    if isinstance(value, list):
        return [found for item in value for found in _ciphertexts(item)]
    return [value] if isinstance(value, str) and is_ciphertext(value) else []


async def _personal_rows(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact,
    now: object,
) -> dict[str, uuid.UUID]:  # fmt: skip
    user = await make_user(display_name="Synthetic Owner")
    user.ooo_message = "Synthetic away note"
    chat = await make_chat(user, title="Synthetic Team Chat")
    contact = await make_contact(user, display_name="Synthetic Colleague")
    await users_repo.upsert_telegram_session(
        db_session,
        user.id,
        "synthetic-session",
        method=LoginMethod.qr,
        connected_at=now,  # type: ignore[arg-type]
    )
    await users_repo.set_telegram_session_status(
        db_session, user.id, SessionStatus.revoked, last_error="Synthetic error for +1555"
    )
    commitment = await commitments.add_commitment(
        db_session, user.id, contact_id=contact.id, what="Synthetic promise", due=None,
        source_sent_at=now, confidence=0.9, source_message_text="Synthetic source",  # type: ignore[arg-type]
        jira_issue_key="SYN-1", jira_comment_id="10001",
        jira_comment_url="https://synthetic.atlassian.net/browse/SYN-1?focusedCommentId=10001",
    )  # fmt: skip
    await tasks_conn.upsert_connection(
        db_session, user.id, provider=TaskProvider.jira, credentials={"token": "synthetic"},
        config_json={"identity": {"name": "synthetic@example.com"}, "project_keys": ["SYN"]},
    )  # fmt: skip
    app = await applications.submit(
        db_session, telegram_id=user.telegram_control_chat_id, username="Synthetic_Name",
        use_case="Synthetic use case " + "я" * 980, language="en", now=now,  # type: ignore[arg-type]
    )  # fmt: skip
    await db_session.commit()
    return {
        "user": user.id,
        "chat": chat.id,
        "contact": contact.id,
        "commitment": commitment.id,
        "app": app.id,
    }


async def test_personal_columns_are_ciphertext_and_export_decrypted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    ids = await _personal_rows(db_session, make_user, make_chat, make_contact, clock())
    raw = {
        "users.display_name": "SELECT display_name FROM users WHERE id = :id",
        "users.ooo_message": "SELECT ooo_message FROM users WHERE id = :id",
        "telegram_sessions.last_error":
            "SELECT last_error FROM telegram_sessions WHERE user_id = :id",
        "task_source_connections.config_json":
            "SELECT config_json->>'enc' FROM task_source_connections WHERE user_id = :id",
    }  # fmt: skip
    for name, sql in raw.items():
        value = await _raw(session_factory, sql, id=ids["user"])
        assert isinstance(value, str) and CIPHERTEXT.match(value), name
    for sql, key in [
        ("SELECT title FROM work_chats WHERE id = :id", "chat"),
        ("SELECT display_name FROM contacts WHERE id = :id", "contact"),
        ("SELECT jira_comment_url FROM commitments WHERE id = :id", "commitment"),
        ("SELECT username FROM beta_applications WHERE id = :id", "app"),
        ("SELECT use_case FROM beta_applications WHERE id = :id", "app"),
    ]:
        value = await _raw(session_factory, sql, id=ids[key])
        assert isinstance(value, str) and CIPHERTEXT.match(value), sql
    # Kept plaintext: matched in SQL.
    issue_key = await _raw(
        session_factory,
        "SELECT jira_issue_key FROM commitments WHERE id = :id",
        id=ids["commitment"],
    )
    assert issue_key == "SYN-1"

    async with session_factory() as session:
        export = await export_user_data(session, ids["user"], now=clock())
    assert export["users"][0]["display_name"] == "Synthetic Owner"
    assert export["users"][0]["ooo_message"] == "Synthetic away note"
    assert export["telegram_sessions"][0]["last_error"] == "Synthetic error for +1555"
    assert export["work_chats"][0]["title"] == "Synthetic Team Chat"
    assert export["contacts"][0]["display_name"] == "Synthetic Colleague"
    assert "last_seen_at" in export["contacts"][0]
    assert export["commitments"][0]["jira_comment_url"].startswith("https://synthetic.")
    assert export["task_source_connections"][0]["config_json"]["identity"] == {
        "name": "synthetic@example.com"
    }
    (app,) = export["access"]["applications"]
    assert app["username"] == "synthetic_name"
    assert app["use_case"].startswith("Synthetic use case")
    assert "enc" not in export["task_source_connections"][0]["config_json"]
    assert not _ciphertexts(export)  # every value is decrypted


async def test_reencrypt_rewrites_plaintext_personal_columns_once(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    ids = await _personal_rows(db_session, make_user, make_chat, make_contact, clock())
    config = {"identity": {"name": "legacy@example.com"}, "site": "https://legacy.example"}
    async with session_factory() as session:
        # Rows written before the columns were encrypted (plaintext, same column types).
        updates: list[tuple[str, dict[str, object]]] = [
            (
                "UPDATE users SET display_name = 'Legacy Owner', ooo_message = 'Legacy away' "
                "WHERE id = :id",
                {"id": ids["user"]},
            ),
            (
                "UPDATE telegram_sessions SET last_error = 'Legacy error' WHERE user_id = :id",
                {"id": ids["user"]},
            ),
            ("UPDATE work_chats SET title = 'Sync:2026:Q4' WHERE id = :id", {"id": ids["chat"]}),
            (
                "UPDATE contacts SET display_name = 'Legacy Contact' WHERE id = :id",
                {"id": ids["contact"]},
            ),
            (
                "UPDATE commitments SET jira_comment_url = 'https://legacy.example/c' "
                "WHERE id = :id",
                {"id": ids["commitment"]},
            ),
            (
                "UPDATE beta_applications SET username = 'legacy', use_case = 'Legacy case' "
                "WHERE id = :id",
                {"id": ids["app"]},
            ),
            (
                "UPDATE task_source_connections SET config_json = CAST(:c AS jsonb) "
                "WHERE user_id = :id",
                {"id": ids["user"], "c": json.dumps(config)},
            ),
        ]
        for sql, params in updates:
            await session.execute(text(sql), params)
        await session.commit()

    # Old plaintext rows read as they are before the job runs.
    async with session_factory() as session:
        chats = await chats_repo.list_work_chats(session, ids["user"])
        assert [chat.title for chat in chats] == ["Sync:2026:Q4"]  # colons, yet plaintext

    first = await reencrypt_legacy_rows(session_factory, batch_size=1)
    assert (
        first.users, first.telegram_sessions, first.work_chats, first.contacts,
        first.beta_applications, first.task_source_connections, first.commitments,
    ) == (1, 1, 1, 1, 1, 1, 1)  # fmt: skip
    second = await reencrypt_legacy_rows(session_factory, batch_size=1)
    assert second == type(second)()  # idempotent

    for sql, key in [
        ("SELECT display_name FROM users WHERE id = :id", "user"),
        ("SELECT ooo_message FROM users WHERE id = :id", "user"),
        ("SELECT last_error FROM telegram_sessions WHERE user_id = :id", "user"),
        ("SELECT title FROM work_chats WHERE id = :id", "chat"),
        ("SELECT display_name FROM contacts WHERE id = :id", "contact"),
        ("SELECT jira_comment_url FROM commitments WHERE id = :id", "commitment"),
        ("SELECT use_case FROM beta_applications WHERE id = :id", "app"),
        ("SELECT config_json->>'enc' FROM task_source_connections WHERE user_id = :id", "user"),
    ]:
        value = await _raw(session_factory, sql, id=ids[key])
        assert isinstance(value, str) and CIPHERTEXT.match(value), sql

    async with session_factory() as session:
        export = await export_user_data(session, ids["user"], now=clock())
    assert export["users"][0]["display_name"] == "Legacy Owner"
    assert export["users"][0]["ooo_message"] == "Legacy away"
    assert export["telegram_sessions"][0]["last_error"] == "Legacy error"
    assert export["work_chats"][0]["title"] == "Sync:2026:Q4"
    assert export["contacts"][0]["display_name"] == "Legacy Contact"
    assert export["commitments"][0]["jira_comment_url"] == "https://legacy.example/c"
    assert export["task_source_connections"][0]["config_json"] == config
    assert export["access"]["applications"][0]["use_case"] == "Legacy case"


async def test_lists_sort_by_decrypted_name(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user = await make_user()
    for title in ("beta", "Alpha", "gamma"):
        await make_chat(user, title=title)
        await make_contact(user, display_name=title)
    chats = await chats_repo.list_work_chats(db_session, user.id)
    contacts = await chats_repo.list_contacts(db_session, user.id)
    assert [chat.title for chat in chats] == ["Alpha", "beta", "gamma"]
    assert [contact.display_name for contact in contacts] == ["Alpha", "beta", "gamma"]


async def test_upsert_contact_moves_last_seen_at_most_daily(
    db_session: AsyncSession, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = await make_user()
    now = clock()
    contact = await chats_repo.upsert_contact(
        db_session,
        user.id,
        telegram_peer_id=next_telegram_id(),
        kind=ContactKind.person,
        display_name="Synthetic",
        now=now - timedelta(days=5),
    )
    assert contact.last_seen_at == now - timedelta(days=5)
    chats_repo.touch_last_seen(contact, now - timedelta(days=5, hours=-12))
    assert contact.last_seen_at == now - timedelta(days=5)  # within a day: no write
    chats_repo.touch_last_seen(contact, now)
    assert contact.last_seen_at == now


# --- retention -----------------------------------------------------------------------------------


async def test_purge_clears_old_source_text_of_open_commitments(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    now = clock()
    user = await make_user()
    contact = await make_contact(user)

    async def promise(sent_days_ago: int) -> Commitment:
        return await commitments.add_commitment(
            db_session, user.id, contact_id=contact.id, what="Synthetic promise",
            to_whom="Synthetic person", due=(now + timedelta(days=3)).date(),
            source_sent_at=now - timedelta(days=sent_days_ago), confidence=0.9,
            source_message_text="Synthetic source text",
        )  # fmt: skip

    old, recent = await promise(settings.message_retention_days + 1), await promise(1)
    await db_session.commit()

    stats = await purge_messages(session_factory, settings, now)
    assert stats.commitment_sources_cleared == 1
    async with session_factory() as session:
        rows = {
            row.id: row
            for row in (await session.execute(select(Commitment))).scalars()
            if row.id in (old.id, recent.id)
        }
    assert rows[old.id].source_message_text is None
    assert rows[old.id].state == CommitmentState.open  # the commitment itself stays
    assert (rows[old.id].what, rows[old.id].to_whom) == ("Synthetic promise", "Synthetic person")
    assert rows[old.id].due == (now + timedelta(days=3)).date()
    assert rows[recent.id].source_message_text == "Synthetic source text"


async def _age(
    session: AsyncSession, model: type[WorkChat] | type[Contact], row_id: object, at: object
) -> None:
    await session.execute(
        text(f"UPDATE {model.__tablename__} SET last_seen_at = :at WHERE id = :id"),
        {"at": at, "id": row_id},
    )


async def test_purge_removes_only_unconfigured_stale_chats_and_unreferenced_contacts(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    now = clock()
    stale = now - STALE_RETENTION - timedelta(days=1)
    user = await make_user(work_account=False)

    def paused(**values: object) -> dict[str, object]:
        return {"origin": WorkChatOrigin.auto, "enabled": False, "excluded": False, **values}

    # Chats that must stay.
    keep_chats = {
        "picked_enabled": await make_chat(user),
        "picked_disabled": await make_chat(user, enabled=False),
        "auto_excluded": await make_chat(
            user, origin=WorkChatOrigin.auto, enabled=False, excluded=True
        ),
        "auto_override": await make_chat(user, **paused(override_language="en")),
        "auto_skip_closing": await make_chat(user, **paused(skip_closing=False)),
        "auto_open_turn": await make_chat(user, **paused()),
        "auto_pending_draft": await make_chat(user, **paused()),
        "auto_recent": await make_chat(user, **paused()),
        "auto_formality": await make_chat(user, **paused(override_formality=3)),
        "auto_delay_min": await make_chat(user, **paused(override_delay_min_s=0)),
        "auto_delay_max": await make_chat(user, **paused(override_delay_max_s=60)),
        "auto_recent_turn": await make_chat(user, **paused()),
        "auto_recent_draft": await make_chat(user, **paused()),
    }  # fmt: skip
    # Chats that go: paused auto chats, one with an old finished turn.
    gone_plain = await make_chat(user, **paused())
    gone_with_turn = await make_chat(user, **paused())
    gone_contact = await make_contact(user, telegram_peer_id=gone_with_turn.telegram_chat_id)

    async def turn(
        chat: WorkChat, contact: Contact, state: TurnState, arrived_at: datetime = stale
    ) -> ConversationTurn:
        return await turns.create_turn(
            db_session, user.id, work_chat_id=chat.id, contact_id=contact.id,
            first_message_id=None, arrived_at=arrived_at, delay_s=0, due_at=stale, state=state,
        )  # fmt: skip

    finished = await turn(gone_with_turn, gone_contact, TurnState.answered)
    old_draft = await drafts.create_draft(
        db_session, user.id, conversation_turn_id=finished.id, text="synthetic",
        output_json={}, prepared_at=stale,
    )  # fmt: skip
    old_draft.state, old_draft.finalized_at = DraftState.rejected, stale
    open_contact = await make_contact(
        user, telegram_peer_id=keep_chats["auto_open_turn"].telegram_chat_id
    )
    await turn(keep_chats["auto_open_turn"], open_contact, TurnState.waiting)
    pending_contact = await make_contact(
        user, telegram_peer_id=keep_chats["auto_pending_draft"].telegram_chat_id
    )
    pending_turn = await turn(keep_chats["auto_pending_draft"], pending_contact, TurnState.answered)
    await drafts.create_draft(
        db_session, user.id, conversation_turn_id=pending_turn.id, text="synthetic",
        output_json={}, prepared_at=stale,
    )  # fmt: skip

    # A finished turn that arrived recently keeps its chat, and its contact (a group member
    # with no chat of its own, so only the turn references it).
    turn_only_contact = await make_contact(user, display_name="Synthetic member with a turn")
    await turn(keep_chats["auto_recent_turn"], turn_only_contact, TurnState.answered, now)
    # An old turn whose draft was finalized recently keeps its chat.
    recent_draft_contact = await make_contact(
        user, telegram_peer_id=keep_chats["auto_recent_draft"].telegram_chat_id
    )
    recent_draft_turn = await turn(
        keep_chats["auto_recent_draft"], recent_draft_contact, TurnState.answered
    )
    recent_draft = await drafts.create_draft(
        db_session, user.id, conversation_turn_id=recent_draft_turn.id, text="synthetic",
        output_json={}, prepared_at=stale,
    )  # fmt: skip
    recent_draft.state, recent_draft.finalized_at = DraftState.rejected, now - timedelta(days=1)

    # Contacts: unreferenced ones go, referenced or recent ones stay.
    gone_member = await make_contact(user, display_name="Synthetic group member")
    keep_contacts = {
        "recent": await make_contact(user),
        "pinned": await make_contact(user),
        "commitment": await make_contact(user),
        "profile": await make_contact(user),
        "message": await make_contact(user),
        "own_chat": await make_contact(
            user, telegram_peer_id=keep_chats["picked_enabled"].telegram_chat_id
        ),
    }
    keep_contacts["pinned"].pinned_profile = True
    await commitments.add_commitment(
        db_session, user.id, contact_id=keep_contacts["commitment"].id, what="Synthetic",
        due=None, source_sent_at=now, confidence=0.9,
    )  # fmt: skip
    await profiles.create_profile(
        db_session, user.id, scope=ProfileScope.contact, contact_id=keep_contacts["profile"].id,
        rules_json={}, summary_text="", examples=[], sample_size=0,
    )  # fmt: skip
    await messages.add_message(
        db_session, user.id, work_chat_id=keep_chats["picked_enabled"].id, telegram_message_id=1,
        text="synthetic", sent_at=now, sender_contact_id=keep_contacts["message"].id,
    )  # fmt: skip
    await db_session.flush()

    # A user in work account mode keeps even paused-looking chats (the invariant is repaired).
    mode_user = await make_user(work_account=True)
    mode_chat = await make_chat(mode_user, **paused())

    for chat in [*keep_chats.values(), gone_plain, gone_with_turn, mode_chat]:
        if chat is not keep_chats["auto_recent"]:
            await _age(db_session, WorkChat, chat.id, stale)
    for contact in [
        gone_contact, open_contact, pending_contact, gone_member, turn_only_contact,
        recent_draft_contact, *keep_contacts.values(),
    ]:  # fmt: skip
        if contact is not keep_contacts["recent"]:
            await _age(db_session, Contact, contact.id, stale)
    await db_session.commit()

    stats = await purge_messages(session_factory, settings, now)

    async with session_factory() as session:
        chat_ids = set((await session.execute(select(WorkChat.id))).scalars())
        contact_ids = set((await session.execute(select(Contact.id))).scalars())
        turn_ids = set((await session.execute(select(ConversationTurn.id))).scalars())
    assert {chat.id for chat in keep_chats.values()} | {mode_chat.id} <= chat_ids
    assert gone_plain.id not in chat_ids and gone_with_turn.id not in chat_ids
    assert finished.id not in turn_ids  # the finished turn went with its chat
    assert {contact.id for contact in keep_contacts.values()} <= contact_ids
    assert {open_contact.id, pending_contact.id, turn_only_contact.id} <= contact_ids
    assert recent_draft_contact.id in contact_ids
    assert gone_member.id not in contact_ids
    assert gone_contact.id not in contact_ids  # its chat went in the same run
    assert (stats.chats_deleted, stats.contacts_deleted) == (2, 2)

    again = await purge_messages(session_factory, settings, now)
    assert (again.chats_deleted, again.contacts_deleted) == (0, 0)
