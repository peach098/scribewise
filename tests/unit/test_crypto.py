"""AES-256-GCM helpers and encrypted column types (issues #58, #59). All plaintexts are
synthetic."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.dialects import postgresql

from scribe import crypto
from scribe.crypto import (
    DecryptionError,
    EncryptedColumn,
    EncryptedJSON,
    EncryptedJSONBFromPlaintext,
    EncryptedText,
    EncryptedTextFromPlaintext,
    Keyring,
    configure_keyring,
    keyring_from_master_key,
    open_owner,
    seal_owner,
)

PLAINTEXT = "Synthetic message: отчёт будет в пятницу 🙂"
CONTEXT = "messages:text"


def _key() -> bytes:
    return os.urandom(32)


@pytest.fixture
def keyring() -> Keyring:
    return keyring_from_master_key(_key())


@pytest.fixture
def global_keyring(keyring: Keyring) -> Iterator[Keyring]:
    previous = crypto._keyring
    configure_keyring(keyring)
    yield keyring
    crypto._keyring = previous


@pytest.fixture
def strict_keyring(keyring: Keyring) -> Iterator[Keyring]:
    previous = crypto._keyring
    keyring.strict = True
    configure_keyring(keyring)
    yield keyring
    crypto._keyring = previous


def _swap_part(token: str, index: int, new: str) -> str:
    parts = token.split(":")
    parts[index] = new
    return ":".join(parts)


def _flip_first_byte(b64: str) -> str:
    raw = bytearray(base64.b64decode(b64))
    raw[0] ^= 0x01
    return base64.b64encode(bytes(raw)).decode()


def _legacy_token(key: bytes, key_id: str, plaintext: str) -> str:
    """A ciphertext in the format written before #59 (associated data = key id only)."""
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), key_id.encode())
    return f"{key_id}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"


# --- round trip / format ----------------------------------------------------------------------


def test_round_trip(keyring: Keyring) -> None:
    token = keyring.encrypt(PLAINTEXT, CONTEXT)
    assert keyring.decrypt(token, CONTEXT) == PLAINTEXT


def test_round_trip_without_context(keyring: Keyring) -> None:
    assert keyring.decrypt(keyring.encrypt(PLAINTEXT)) == PLAINTEXT
    assert keyring.decrypt(keyring.encrypt(""), None) == ""


def test_format_is_marker_key_id_nonce_ciphertext(keyring: Keyring) -> None:
    token = keyring.encrypt(PLAINTEXT, CONTEXT)
    marker, key_id, nonce_b64, ct_b64 = token.split(":")
    assert (marker, key_id) == ("e2", "v1")
    assert len(base64.b64decode(nonce_b64)) == 12  # 96-bit nonce
    assert PLAINTEXT not in token
    assert len(base64.b64decode(ct_b64)) == len(PLAINTEXT.encode()) + 16  # + GCM tag
    assert CONTEXT not in token  # bound, not stored


def test_nonce_is_random(keyring: Keyring) -> None:
    tokens = {keyring.encrypt(PLAINTEXT) for _ in range(50)}
    assert len(tokens) == 50
    assert len({t.split(":")[2] for t in tokens}) == 50


# --- column binding (#59) ---------------------------------------------------------------------


def test_ciphertext_moved_to_another_column_is_rejected(keyring: Keyring) -> None:
    token = keyring.encrypt(PLAINTEXT, "telegram_sessions:session_string")
    for other in ("telegram_sessions:account_label", "messages:text", "", None):
        with pytest.raises(DecryptionError, match="authentication"):
            keyring.decrypt(token, other)


def test_unbound_ciphertext_does_not_decrypt_in_a_column(keyring: Keyring) -> None:
    with pytest.raises(DecryptionError):
        keyring.decrypt(keyring.encrypt(PLAINTEXT), CONTEXT)


def test_legacy_ciphertext_still_decrypts_in_any_column() -> None:
    key = _key()
    ring = Keyring({"v1": key}, current="v1")
    old = _legacy_token(key, "v1", PLAINTEXT)
    assert ring.decrypt(old) == PLAINTEXT
    assert ring.decrypt(old, CONTEXT) == PLAINTEXT
    assert ring.needs_rotation(old)  # current key, but not column-bound


def test_legacy_ciphertext_is_refused_when_strict() -> None:
    key = _key()
    ring = Keyring({"v1": key}, current="v1", strict=True)
    old = _legacy_token(key, "v1", PLAINTEXT)
    with pytest.raises(DecryptionError, match="strict"):
        ring.decrypt(old, CONTEXT)
    with pytest.raises(DecryptionError, match="strict"):
        ring.rotate(old, CONTEXT)  # never laundered into a bound one
    assert ring.decrypt(old, CONTEXT, strict=False) == PLAINTEXT


def test_rotate_upgrades_legacy_to_bound_format() -> None:
    key = _key()
    ring = Keyring({"v1": key}, current="v1")
    upgraded = ring.rotate(_legacy_token(key, "v1", PLAINTEXT), CONTEXT)
    assert upgraded.startswith("e2:v1:")
    assert ring.decrypt(upgraded, CONTEXT) == PLAINTEXT
    assert ring.rotate(upgraded, CONTEXT) == upgraded  # idempotent
    with pytest.raises(DecryptionError):
        ring.decrypt(upgraded, "commitments:what")


# --- tamper detection -------------------------------------------------------------------------


def test_tampered_ciphertext_is_rejected(keyring: Keyring) -> None:
    token = keyring.encrypt(PLAINTEXT, CONTEXT)
    tampered = _swap_part(token, 3, _flip_first_byte(token.split(":")[3]))
    with pytest.raises(DecryptionError):
        keyring.decrypt(tampered, CONTEXT)


def test_tampered_nonce_is_rejected(keyring: Keyring) -> None:
    token = keyring.encrypt(PLAINTEXT, CONTEXT)
    tampered = _swap_part(token, 2, _flip_first_byte(token.split(":")[2]))
    with pytest.raises(DecryptionError):
        keyring.decrypt(tampered, CONTEXT)


def test_relabelled_key_id_is_rejected() -> None:
    key = _key()
    ring = Keyring({"v1": key, "v2": key}, current="v1")
    token = ring.encrypt(PLAINTEXT, CONTEXT)
    # Same key under a different id: the key id is authenticated as associated data.
    with pytest.raises(DecryptionError):
        ring.decrypt(_swap_part(token, 1, "v2"), CONTEXT)


def test_bound_token_stripped_to_legacy_format_is_rejected(keyring: Keyring) -> None:
    """Dropping the ``e2:`` marker must not turn a bound token into an unbound one."""
    token = keyring.encrypt(PLAINTEXT, CONTEXT)
    with pytest.raises(DecryptionError):
        keyring.decrypt(token.removeprefix("e2:"), "commitments:what")


def test_wrong_key_is_rejected(keyring: Keyring) -> None:
    token = keyring.encrypt(PLAINTEXT)
    with pytest.raises(DecryptionError):
        keyring_from_master_key(_key()).decrypt(token)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "plaintext",
        "v1:abc",
        "v1:!!!:!!!",
        ":AAAAAAAAAAAAAAAA:AAAA",
        "v1:AAAA:AAAA",
        "a:b:c:d",
        "e2::AAAAAAAAAAAAAAAA:AAAA",
        "e2:v1:AAAA:AAAA",
        "e2:v1:x:y:z",
        # A 12-byte nonce but a ciphertext shorter than the 16-byte GCM tag.
        "v1:AAAAAAAAAAAAAAAA:AAAA",
        "e2:v1:AAAAAAAAAAAAAAAA:" + "A" * 20,
    ],
)
def test_malformed_token_is_rejected(keyring: Keyring, token: str) -> None:
    with pytest.raises(DecryptionError):
        keyring.decrypt(token)
    assert not crypto.is_ciphertext(token)


def test_unknown_key_id_fails_cleanly(keyring: Keyring) -> None:
    other = Keyring({"v7": _key()}, current="v7")
    with pytest.raises(DecryptionError, match="unknown key id 'v7'"):
        keyring.decrypt(other.encrypt(PLAINTEXT, CONTEXT), CONTEXT)


# --- key rotation (#58) -----------------------------------------------------------------------


def test_key_id_rotation_path() -> None:
    old_key, new_key = _key(), _key()
    old_ring = Keyring({"v1": old_key}, current="v1")
    old_token = old_ring.encrypt(PLAINTEXT, CONTEXT)
    legacy = _legacy_token(old_key, "v1", PLAINTEXT)

    rotated_ring = Keyring({"v1": old_key, "v2": new_key}, current="v2")
    # Old ciphertexts stay readable after rotation (the previous key decrypts).
    assert rotated_ring.decrypt(old_token, CONTEXT) == PLAINTEXT
    assert rotated_ring.decrypt(legacy, CONTEXT) == PLAINTEXT
    assert rotated_ring.needs_rotation(old_token)

    # New writes use the current key id.
    new_token = rotated_ring.encrypt(PLAINTEXT, CONTEXT)
    assert new_token.startswith("e2:v2:")
    assert not rotated_ring.needs_rotation(new_token)

    # Re-encryption moves the value to the current key; the old key can then be retired.
    migrated = rotated_ring.rotate(old_token, CONTEXT)
    assert migrated.startswith("e2:v2:")
    assert rotated_ring.rotate(migrated, CONTEXT) == migrated
    retired_ring = Keyring({"v2": new_key}, current="v2")
    assert retired_ring.decrypt(migrated, CONTEXT) == PLAINTEXT
    with pytest.raises(DecryptionError, match="unknown key id"):
        retired_ring.decrypt(old_token, CONTEXT)


def test_rotation_keeps_the_column_binding() -> None:
    old_key, new_key = _key(), _key()
    old_token = Keyring({"v1": old_key}, current="v1").encrypt(PLAINTEXT, CONTEXT)
    ring = Keyring({"v1": old_key, "v2": new_key}, current="v2")
    with pytest.raises(DecryptionError):
        ring.rotate(old_token, "commitments:what")  # cannot be re-bound elsewhere


@pytest.mark.parametrize(
    ("keys", "current"),
    [
        ({"v1": b"short"}, "v1"),
        ({"v1": b"x" * 32}, "v2"),
        ({"v:1": b"x" * 32}, "v:1"),
        ({"": b"x" * 32}, ""),
        ({"e2": b"x" * 32}, "e2"),  # the format marker
        ({"ключ": b"x" * 32}, "ключ"),
        ({"v 1": b"x" * 32}, "v 1"),  # outside KEY_ID_PATTERN, like the settings
        ({"v\x001": b"x" * 32}, "v\x001"),
    ],
)
def test_invalid_keyring_is_rejected(keys: dict[str, bytes], current: str) -> None:
    with pytest.raises(ValueError):
        Keyring(keys, current=current)


def test_keyring_error_never_contains_key_material() -> None:
    key = b"\x01" * 31
    with pytest.raises(ValueError) as excinfo:
        Keyring({"v1": key}, current="v1")
    assert base64.b64encode(key).decode() not in str(excinfo.value)
    assert repr(key) not in str(excinfo.value)


# --- owner sealing (#59: rows of one column) ---------------------------------------------------


def test_owner_sealed_value_opens_only_for_its_owner(global_keyring: Keyring) -> None:
    stored = seal_owner("user-a", "synthetic-session")
    assert open_owner("user-a", stored) == "synthetic-session"
    with pytest.raises(DecryptionError, match="another owner"):
        open_owner("user-b", stored)
    with pytest.raises(DecryptionError):
        open_owner("user-a", stored.replace("\n", " "))


def test_unsealed_value_is_legacy_and_refused_when_strict(global_keyring: Keyring) -> None:
    assert open_owner("user-a", "synthetic-session") == "synthetic-session"
    with pytest.raises(DecryptionError, match="strict"):
        open_owner("user-a", "synthetic-session", strict=True)


# --- SQLAlchemy column types ------------------------------------------------------------------

DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def _bound(type_: Any, column: str = "c") -> Any:
    table = Table("t", MetaData(), Column("id", Integer, primary_key=True), Column(column, type_))
    bound = table.c[column].type
    assert isinstance(bound, EncryptedColumn)
    return bound


def test_column_types_are_bound_to_table_and_column() -> None:
    shared = EncryptedText()
    table = Table(
        "notes",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("a", shared),
        Column("b", shared),
    )
    a, b = table.c.a.type, table.c.b.type
    assert isinstance(a, EncryptedColumn) and isinstance(b, EncryptedColumn)
    assert (a.binding, b.binding) == ("notes:a", "notes:b")  # a shared instance never mixes
    assert a._static_cache_key != b._static_cache_key


def test_explicit_binding_survives_a_rename() -> None:
    bound = _bound(EncryptedText(binding="old_table:old_column"), "renamed")
    assert bound.binding == "old_table:old_column"


def test_model_columns_are_bound() -> None:
    from scribe.db.models import Commitment, Message, TelegramSession

    for column, binding in (
        (Message.__table__.c.text, "messages:text"),
        (Commitment.__table__.c.what, "commitments:what"),
        (TelegramSession.__table__.c.session_string, "telegram_sessions:session_string"),
    ):
        assert isinstance(column.type, EncryptedColumn)
        assert column.type.binding == binding


def test_encrypted_text_type_round_trip(global_keyring: Keyring) -> None:
    col = _bound(EncryptedText())
    stored = col.process_bind_param(PLAINTEXT, DIALECT)
    assert stored is not None
    assert stored.startswith("e2:v1:")
    assert PLAINTEXT not in stored
    assert col.process_result_value(stored, DIALECT) == PLAINTEXT


def test_encrypted_text_type_refuses_another_columns_value(global_keyring: Keyring) -> None:
    a, b = _bound(EncryptedText(), "a"), _bound(EncryptedText(), "b")
    stored = a.process_bind_param(PLAINTEXT, DIALECT)
    with pytest.raises(DecryptionError):
        b.process_result_value(stored, DIALECT)


def test_encrypted_text_type_reads_legacy_tokens(global_keyring: Keyring) -> None:
    key = _key()
    ring = Keyring({"old": key, "v1": _key()}, current="v1")
    configure_keyring(ring)
    col = _bound(EncryptedText())
    assert col.process_result_value(_legacy_token(key, "old", PLAINTEXT), DIALECT) == PLAINTEXT


def test_encrypted_types_pass_none_through(global_keyring: Keyring) -> None:
    for col in (EncryptedText(), EncryptedJSON()):
        assert col.process_bind_param(None, DIALECT) is None
        assert col.process_result_value(None, DIALECT) is None


def test_encrypted_json_type_round_trip(global_keyring: Keyring) -> None:
    value = {"examples": ["synthetic one", "синтетика два"], "n": 2, "nested": {"ok": True}}
    col = _bound(EncryptedJSON())
    stored = col.process_bind_param(value, DIALECT)
    assert stored is not None
    assert "synthetic one" not in stored
    assert col.process_result_value(stored, DIALECT) == value


def test_column_types_require_configured_keyring() -> None:
    previous = crypto._keyring
    crypto._keyring = None
    try:
        with pytest.raises(RuntimeError):
            EncryptedText().process_bind_param(PLAINTEXT, DIALECT)
    finally:
        crypto._keyring = previous


def test_encrypted_jsonb_type_round_trip_and_legacy_rows(global_keyring: Keyring) -> None:
    col = EncryptedJSONBFromPlaintext()
    value = {"rules": [{"text": "синтетика: no emojis"}], "formality": 2}
    stored = col.process_bind_param(value, DIALECT)
    assert isinstance(stored, dict)
    assert set(stored) == {"enc"} and stored["enc"].startswith("e2:v1:")
    assert "синтетика" not in stored["enc"]
    assert col.process_result_value(stored, DIALECT) == value
    # A row written before encryption is read as is (and encrypted on its next write).
    assert col.process_result_value({"formality": 2}, DIALECT) == {"formality": 2}
    assert col.process_result_value({"enc": "not ciphertext"}, DIALECT) == {"enc": "not ciphertext"}
    assert col.process_bind_param(None, DIALECT) is None
    tampered = {"enc": stored["enc"][:-4] + "AAAA"}
    with pytest.raises(crypto.DecryptionError):
        col.process_result_value(tampered, DIALECT)


def test_from_plaintext_types_refuse_plaintext_when_strict(strict_keyring: Keyring) -> None:
    text_col = _bound(EncryptedTextFromPlaintext())
    jsonb_col = _bound(EncryptedJSONBFromPlaintext())
    with pytest.raises(DecryptionError, match="strict"):
        text_col.process_result_value("planted plaintext", DIALECT)
    with pytest.raises(DecryptionError, match="strict"):
        jsonb_col.process_result_value({"formality": 2}, DIALECT)
    # Values written now still round-trip.
    stored = text_col.process_bind_param(PLAINTEXT, DIALECT)
    assert text_col.process_result_value(stored, DIALECT) == PLAINTEXT


def test_from_plaintext_types_read_plaintext_when_not_strict(global_keyring: Keyring) -> None:
    text_col = _bound(EncryptedTextFromPlaintext())
    assert text_col.process_result_value("old row", DIALECT) == "old row"


def test_from_plaintext_text_keeps_an_unreadable_lookalike_unless_strict(
    global_keyring: Keyring,
) -> None:
    """A plaintext name can have the ciphertext format (other people choose names): one such
    row must not break every query that loads it. Strict mode refuses it."""
    text_col = _bound(EncryptedTextFromPlaintext())
    lookalike = _legacy_token(_key(), "v1", "chosen by someone else")
    assert text_col.process_result_value(lookalike, DIALECT) == lookalike
    crypto.configure_keyring(Keyring({"v1": _key()}, current="v1", strict=True))
    with pytest.raises(DecryptionError):
        text_col.process_result_value(lookalike, DIALECT)


# --- comment fingerprints (specs/003-jira-commitments J9) ---------------------------------------


def test_comment_hmac_is_stable_and_keyed(global_keyring: Keyring) -> None:
    first = crypto.comment_hmac(PLAINTEXT)
    assert first == crypto.comment_hmac(PLAINTEXT)
    assert len(first) == 32
    assert first != crypto.comment_hmac(PLAINTEXT + " ")
    assert PLAINTEXT.encode("utf-8") not in first
    other = keyring_from_master_key(_key())
    assert other.comment_hmac(PLAINTEXT) != first


def test_comment_hmac_key_differs_from_the_encryption_key() -> None:
    key = _key()
    raw = hmac.new(key, PLAINTEXT.encode(), hashlib.sha256).digest()
    assert keyring_from_master_key(key).comment_hmac(PLAINTEXT) != raw
