"""Key rotation and column binding against Postgres (issues #58, #59): ``reencrypt_all``
rewrites every encrypted value to the current key and the column-bound format, resumably and
idempotently; afterwards the old key can go and strict mode reads everything. Synthetic data."""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import crypto
from scribe.crypto import DecryptionError, Keyring, configure_keyring
from scribe.db.models import (
    Commitment,
    LoginMethod,
    Message,
    ProfileScope,
    StyleProfile,
    User,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.stored_output import EARLIER_CONTEXT, OUTPUT_CONTEXT
from scribe.retention.reencrypt import reencrypt_legacy_rows
from scribe.retention.reencrypt_all import encrypted_fields, reencrypt_all
from tests.conftest import TEST_MASTER_KEY, FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser

NEW_KEY = os.urandom(32)
OLD_RING = Keyring({"v1": TEST_MASTER_KEY}, current="v1")
ROTATED_RING = Keyring({"v1": TEST_MASTER_KEY, "v2": NEW_KEY}, current="v2")
RETIRED_STRICT_RING = Keyring({"v2": NEW_KEY}, current="v2", strict=True)


@pytest.fixture(autouse=True)
def _restore_keyring() -> Iterator[None]:
    previous = crypto._keyring
    configure_keyring(OLD_RING)
    yield
    crypto._keyring = previous


def _legacy(plaintext: str, key: bytes = TEST_MASTER_KEY, key_id: str = "v1") -> str:
    """A ciphertext in the format written before #59 (associated data = key id only)."""
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), key_id.encode())
    return f"{key_id}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"


async def _raw(
    session_factory: async_sessionmaker[AsyncSession], sql: str, **params: Any
) -> list[Any]:
    async with session_factory() as session:
        return list((await session.execute(text(sql), params)).all())


async def _all_raw_tokens(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """Every stored ciphertext (columns and drafts.output_json), read past the column types."""
    tokens: list[str] = []
    for f in encrypted_fields():
        cast = "::text" if f.type.storage == "text" else "->>'enc'"
        rows = await _raw(
            session_factory,
            f"SELECT {f.column.name}{cast} FROM {f.table.name} WHERE {f.column.name} IS NOT NULL",
        )
        tokens += [row[0] for row in rows]
    for (output,) in await _raw(session_factory, "SELECT output_json FROM drafts"):
        if "enc" in output:
            tokens.append(output["enc"])
        tokens += [e["enc"] for e in output.get("earlier_messages", [])]
    return tokens


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> dict[str, Any]:
    """Rows under the old key, in both formats, plus plaintext legacy rows."""
    alice, bob = await make_user(), await make_user()
    contact = await make_contact(alice, display_name="Synthetic Anna")
    chat = await make_chat(alice, telegram_chat_id=contact.telegram_peer_id, title="Anna")
    async with session_factory() as session:
        messages = [
            await messages_repo.add_message(
                session,
                alice.id,
                work_chat_id=chat.id,
                telegram_message_id=n,
                text=f"Synthetic message {n}",
                sent_at=clock(),
                sender_contact_id=contact.id,
            )  # fmt: skip
            for n in range(1, 6)
        ]
        turn = await turns_repo.create_turn(
            session, alice.id, work_chat_id=chat.id, contact_id=contact.id,
            first_message_id=messages[0].id, arrived_at=clock(), delay_s=0, due_at=clock(),
        )  # fmt: skip
        output_json = {
            "language": "en",
            "enc": crypto.encrypt(json.dumps({"placeholders": []}), OUTPUT_CONTEXT),
            "earlier_messages": [
                {"ref": "earlier:1", "enc": crypto.encrypt('{"excerpt":"x"}', EARLIER_CONTEXT)}
            ],
        }
        draft = await drafts_repo.create_draft(
            session, alice.id, conversation_turn_id=turn.id, text="Synthetic draft",
            output_json=output_json, prepared_at=clock(),
        )  # fmt: skip
        profile = await profiles_repo.create_profile(
            session, alice.id, scope=ProfileScope.global_, rules_json={"formality": 2},
            summary_text="Synthetic summary", examples=["Synthetic example"], sample_size=1,
        )  # fmt: skip
        commitment = await commitments_repo.add_commitment(
            session, alice.id, contact_id=contact.id, what="Synthetic promise", due=None,
            source_sent_at=clock(), confidence=0.9,
        )  # fmt: skip
        for user, session_string in ((alice, "synthetic-session-a"), (bob, "synthetic-session-b")):
            await users_repo.upsert_telegram_session(
                session, user.id, session_string, method=LoginMethod.qr, connected_at=clock()
            )
        await session.commit()
        # Rows written before #59 / before encryption.
        await session.execute(
            text("UPDATE messages SET text = :t WHERE id = :id"),
            {"t": _legacy("Synthetic legacy message"), "id": messages[0].id},
        )
        await session.execute(
            text("UPDATE telegram_sessions SET session_string = :t WHERE user_id = :u"),
            {"t": _legacy("synthetic-session-b"), "u": bob.id},  # not sealed with its owner
        )
        await session.execute(
            text("UPDATE commitments SET what = :w WHERE id = :id"),
            {"w": "Synthetic plaintext promise", "id": commitment.id},
        )
        await session.commit()
    return {
        "alice": alice,
        "bob": bob,
        "messages": messages,
        "draft": draft,
        "profile": profile,
        "commitment": commitment,
    }


async def _read_everything(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, Any]
) -> None:
    """Every seeded value reads back through the column types with the active keyring."""
    alice: User = seeded["alice"]
    bob: User = seeded["bob"]
    async with session_factory() as session:
        texts = (
            await session.execute(
                select(Message.text)
                .where(Message.user_id == alice.id)
                .order_by(Message.telegram_message_id)
            )
        ).scalars()
        assert list(texts) == ["Synthetic legacy message"] + [
            f"Synthetic message {n}" for n in range(2, 6)
        ]
        a = await users_repo.get_telegram_session(session, alice.id)
        b = await users_repo.get_telegram_session(session, bob.id)
        assert a is not None and b is not None
        assert (a.session_string, b.session_string) == (
            "synthetic-session-a",
            "synthetic-session-b",
        )
        commitment = await session.get(Commitment, seeded["commitment"].id)
        assert commitment is not None and commitment.what == "Synthetic plaintext promise"
        profile = await session.get(StyleProfile, seeded["profile"].id)
        assert profile is not None and profile.rules_json == {"formality": 2}
        assert profile.summary_text == "Synthetic summary"
        assert profile.examples == ["Synthetic example"]
        draft = await drafts_repo.get_draft(session, alice.id, seeded["draft"].id)
        assert draft is not None and draft.text == "Synthetic draft"
        output = draft.output_json
        assert json.loads(crypto.decrypt(output["enc"], OUTPUT_CONTEXT)) == {"placeholders": []}
        (earlier,) = output["earlier_messages"]
        assert json.loads(crypto.decrypt(earlier["enc"], EARLIER_CONTEXT)) == {"excerpt": "x"}


async def test_rotation_rewrites_everything_and_the_old_key_can_go(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
    log_capture: LogCapture,
) -> None:
    seeded = await _seed(session_factory, make_user, make_chat, make_contact, clock)
    updated_before = await _raw(session_factory, "SELECT id, updated_at FROM messages ORDER BY id")

    # The operator sets the new key as current and the old one as previous, and restarts.
    configure_keyring(ROTATED_RING)
    await _read_everything(session_factory, seeded)  # nothing became unreadable

    check = await reencrypt_all(session_factory, dry_run=True)
    assert check.failed == 0
    assert check.fields["messages.text"].pending == 5
    assert check.fields["telegram_sessions.session_string"].pending == 2
    assert check.fields["commitments.what"].pending == 1
    assert check.fields["drafts.output_json"].pending == 1
    assert check.legacy_drafts == 0
    assert all(not t.startswith("e2:v2:") for t in await _all_raw_tokens(session_factory))

    report = await reencrypt_all(session_factory, batch_size=2)
    assert report.failed == 0 and report.pending == check.pending
    tokens = await _all_raw_tokens(session_factory)
    assert tokens and all(t.startswith("e2:v2:") for t in tokens)
    assert (await reencrypt_all(session_factory, batch_size=2)).pending == 0  # idempotent
    final_check = await reencrypt_all(session_factory, dry_run=True)
    assert (final_check.pending, final_check.failed) == (0, 0)
    # A rotation is not a change of the row.
    assert await _raw(session_factory, "SELECT id, updated_at FROM messages ORDER BY id") == (
        updated_before
    )

    # The old key is removed and strict mode turned on: everything still reads.
    configure_keyring(RETIRED_STRICT_RING)
    await _read_everything(session_factory, seeded)
    assert "Synthetic" not in log_capture.raw and "synthetic-session" not in log_capture.raw


async def test_moved_ciphertexts_are_refused(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    seeded = await _seed(session_factory, make_user, make_chat, make_contact, clock)
    await reencrypt_all(session_factory)
    alice, bob = seeded["alice"], seeded["bob"]
    async with session_factory() as session:
        # Bob's Telegram session copied into Alice's row (same column, another row).
        await session.execute(
            text(
                "UPDATE telegram_sessions SET session_string = "
                "(SELECT session_string FROM telegram_sessions WHERE user_id = :b) "
                "WHERE user_id = :a"
            ),
            {"a": alice.id, "b": bob.id},
        )
        # A message copied into a draft (another table and column).
        await session.execute(
            text(
                "UPDATE drafts SET text = (SELECT text FROM messages WHERE id = :m) WHERE id = :d"
            ),
            {"m": seeded["messages"][1].id, "d": seeded["draft"].id},
        )
        await session.commit()
    async with session_factory() as session:
        row = await users_repo.get_telegram_session(session, alice.id)
        assert row is not None
        with pytest.raises(DecryptionError, match="another owner"):
            _ = row.session_string
    with pytest.raises(DecryptionError, match="authentication"):
        async with session_factory() as session:
            await drafts_repo.get_draft(session, alice.id, seeded["draft"].id)


async def test_interrupted_run_resumes_and_unreadable_values_are_left_alone(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
    log_capture: LogCapture,
) -> None:
    seeded = await _seed(session_factory, make_user, make_chat, make_contact, clock)
    stranger = Keyring({"v9": os.urandom(32)}, current="v9").encrypt("x", "messages:text")
    broken_id: uuid.UUID = seeded["messages"][4].id
    async with session_factory() as session:
        await session.execute(
            text("UPDATE messages SET text = :t WHERE id = :id"), {"t": stranger, "id": broken_id}
        )
        await session.commit()
    configure_keyring(ROTATED_RING)

    class Interrupted(Exception):
        pass

    opened = 0

    def failing_factory() -> AsyncSession:
        nonlocal opened
        opened += 1
        if opened > 4:  # the process dies part-way (after a few committed batches)
            raise Interrupted
        return session_factory()

    with pytest.raises(Interrupted):
        await reencrypt_all(failing_factory, batch_size=1)  # type: ignore[arg-type]
    tokens = await _all_raw_tokens(session_factory)
    done = [t for t in tokens if t.startswith("e2:v2:")]
    assert 0 < len(done) < len(tokens)  # committed batches stay done

    report = await reencrypt_all(session_factory, batch_size=1)
    assert report.failed == 1 and report.fields["messages.text"].failed == 1
    ((left,),) = await _raw(session_factory, "SELECT text FROM messages WHERE id = :i", i=broken_id)
    assert left == stranger  # untouched
    others = [t for t in await _all_raw_tokens(session_factory) if t != stranger]
    assert all(t.startswith("e2:v2:") for t in others)
    check = await reencrypt_all(session_factory, dry_run=True)
    assert (check.pending, check.failed) == (0, 1)
    events = log_capture.events("reencrypt_value_failed")
    assert len(events) == 2  # the run and the check
    for event in events:
        assert (event["table"], event["column"]) == ("messages", "text")
        assert event["row"] == str(broken_id) and event["reason"] == "unknown key id 'v9'"


async def test_strict_run_refuses_planted_plaintext_instead_of_encrypting_it(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    seeded = await _seed(session_factory, make_user, make_chat, make_contact, clock)
    await reencrypt_all(session_factory)
    async with session_factory() as session:
        await session.execute(
            text("UPDATE commitments SET what = :w WHERE id = :id"),
            {"w": "planted plaintext", "id": seeded["commitment"].id},
        )
        await session.commit()
    configure_keyring(Keyring({"v1": TEST_MASTER_KEY}, current="v1", strict=True))
    report = await reencrypt_all(session_factory)
    assert report.fields["commitments.what"].failed == 1
    ((what,),) = await _raw(
        session_factory, "SELECT what FROM commitments WHERE id = :i", i=seeded["commitment"].id
    )
    assert what == "planted plaintext"  # not laundered into a valid ciphertext
    with pytest.raises(DecryptionError, match="strict"):
        async with session_factory() as session:
            await session.get(Commitment, seeded["commitment"].id)


async def test_legacy_job_finds_nothing_after_reencrypt_all(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    """The legacy-row job recognises column-bound (``e2:``) values, including the columns
    encrypted by #57, so it does not rewrite every row on every run."""
    await _seed(session_factory, make_user, make_chat, make_contact, clock)
    configure_keyring(ROTATED_RING)
    await reencrypt_all(session_factory)
    stats = await reencrypt_legacy_rows(session_factory)
    assert all(count == 0 for count in asdict(stats).values()), stats


async def test_a_name_that_looks_like_ciphertext_breaks_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    """A contact's name is chosen by someone else: a plaintext name stored before #57 in the
    ciphertext format must not break the contact list or the jobs, and is never re-wrapped."""
    user = await make_user()
    nonce = base64.b64encode(os.urandom(12)).decode()
    looks_like = f"v1:{nonce}:{base64.b64encode(os.urandom(24)).decode()}"
    plain = await make_contact(user, display_name="Synthetic plain")
    odd = await make_contact(user, display_name="Synthetic odd")
    async with session_factory() as session:
        for contact_id, name in ((plain.id, "Synthetic plain"), (odd.id, looks_like)):
            await session.execute(
                text("UPDATE contacts SET display_name = :n WHERE id = :i"),
                {"n": name, "i": contact_id},
            )
        await session.commit()

    async with session_factory() as session:  # the list still loads
        names = {c.id: c.display_name for c in await chats_repo.list_contacts(session, user.id)}
    assert names[odd.id] == looks_like and names[plain.id] == "Synthetic plain"

    await reencrypt_legacy_rows(session_factory)  # adopts the plaintext row, skips the odd one
    report = await reencrypt_all(session_factory)
    assert report.fields["contacts.display_name"].failed == 1
    ((stored,),) = await _raw(
        session_factory, "SELECT display_name FROM contacts WHERE id = :i", i=odd.id
    )
    assert stored == looks_like  # left as it is, not encrypted a second time
    ((adopted,),) = await _raw(
        session_factory, "SELECT display_name FROM contacts WHERE id = :i", i=plain.id
    )
    assert adopted.startswith("e2:v1:")
    configure_keyring(Keyring({"v1": TEST_MASTER_KEY}, current="v1", strict=True))
    with pytest.raises(DecryptionError):
        async with session_factory() as session:
            await chats_repo.list_contacts(session, user.id)
