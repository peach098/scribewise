"""``reencrypt_all`` coverage and per-value rewrites (issues #58, #59). Values are synthetic."""

from __future__ import annotations

import ast
import base64
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import TypeDecorator

import scribe.db.models  # noqa: F401  (registers every table)
from scribe import crypto
from scribe.crypto import (
    DecryptionError,
    EncryptedColumn,
    Keyring,
    open_owner,
    seal_owner,
)
from scribe.db.base import Base
from scribe.retention import reencrypt_all as job
from scribe.retention.reencrypt_all import EMBEDDED_FIELDS, EncryptedField, encrypted_fields

SRC = Path(__file__).resolve().parents[2] / "src" / "scribe"
OWNER = "00000000-0000-4000-8000-00000000000a"
# Modules that call crypto.encrypt directly, and the encrypted values they write: each must be
# rewritten by reencrypt_all (EMBEDDED_FIELDS), or the module only rewrites such values itself.
DIRECT_ENCRYPT_CALLERS = {
    "drafting/stored_output.py": "drafts.output_json",
    "retention/reencrypt.py": "drafts.output_json",
}


def _field(table: str, column: str) -> EncryptedField:
    (found,) = [f for f in encrypted_fields() if f.name == f"{table}.{column}"]
    return found


# --- coverage: every encrypted column is re-encrypted --------------------------------------------


def test_every_encrypted_column_type_is_an_encrypted_column() -> None:
    """A new encrypted type that does not use the EncryptedColumn mixin would be invisible to
    reencrypt_all (and unbound): every TypeDecorator column named like one must use it."""
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            type_ = column.type
            if isinstance(type_, TypeDecorator) and "encrypt" in type(type_).__name__.lower():
                assert isinstance(type_, EncryptedColumn), f"{table.name}.{column.name}"


def test_every_encrypted_column_is_covered() -> None:
    covered = {f.name for f in encrypted_fields()}
    expected = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, EncryptedColumn)
    }
    assert covered == expected
    # A floor, so discovery silently finding nothing fails too.
    assert {"telegram_sessions.session_string", "messages.text", "commitments.what"} <= covered


def test_every_covered_column_can_be_processed() -> None:
    for f in encrypted_fields():
        assert f.type.storage in {"text", "packed_jsonb"}, f.name
        assert len(f.table.primary_key.columns) == 1, f"{f.name}: keyset needs one pk column"
        assert f.type.binding == f"{f.table.name}:{f.column.name}", f.name
    assert _field("telegram_sessions", "session_string").owner is not None


def test_direct_encrypt_callers_are_registered() -> None:
    """``crypto.encrypt`` outside a column type writes ciphertext reencrypt_all cannot find by
    the column types: each such module must be listed with what it writes."""
    callers = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in {"crypto.py", "retention/reencrypt_all.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {  # local names of crypto.encrypt in this module
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "scribe.crypto"
            for alias in node.names
            if alias.name == "encrypt"
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in names:
                callers.add(rel)
            elif isinstance(func, ast.Attribute) and func.attr == "encrypt":
                target = func.value  # crypto.encrypt(...) / get_keyring().encrypt(...)
                if (isinstance(target, ast.Name) and target.id == "crypto") or (
                    isinstance(target, ast.Call)
                    and isinstance(target.func, ast.Name)
                    and target.func.id == "get_keyring"
                ):
                    callers.add(rel)
    assert callers == set(DIRECT_ENCRYPT_CALLERS)
    assert set(DIRECT_ENCRYPT_CALLERS.values()) <= set(EMBEDDED_FIELDS)


# --- one value ----------------------------------------------------------------------------------


def _rings() -> tuple[Keyring, Keyring]:
    old, new = os.urandom(32), os.urandom(32)
    return Keyring({"v1": old}, current="v1"), Keyring({"v1": old, "v2": new}, current="v2")


def test_text_value_under_previous_key_is_rotated() -> None:
    old, ring = _rings()
    f = _field("messages", "text")
    token = old.encrypt("synthetic", f.type.context)
    new = job._rewrite_text(ring, f, token, None)
    assert new.startswith("e2:v2:") and ring.decrypt(new, f.type.context) == "synthetic"
    with pytest.raises(job._Unchanged):
        job._rewrite_text(ring, f, new, None)  # idempotent


def _legacy(key: bytes, key_id: str, plaintext: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), key_id.encode())
    return f"{key_id}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"


def test_legacy_text_value_is_upgraded_and_bound() -> None:
    key = os.urandom(32)
    ring = Keyring({"v1": key}, current="v1")
    f = _field("messages", "text")
    new = job._rewrite_text(ring, f, _legacy(key, "v1", "synthetic"), None)
    assert new.startswith("e2:v1:") and ring.decrypt(new, "messages:text") == "synthetic"
    with pytest.raises(DecryptionError):
        ring.decrypt(new, "drafts:text")
    ring.strict = True  # strict: a legacy token is refused, not adopted
    with pytest.raises(DecryptionError, match="strict"):
        job._rewrite_text(ring, f, _legacy(key, "v1", "synthetic"), None)


def test_bound_value_stripped_of_its_marker_stays_unreadable() -> None:
    old, ring = _rings()
    f = _field("messages", "text")
    stripped = old.encrypt("synthetic", f.type.context).split(":", 1)[1]
    with pytest.raises(DecryptionError):
        job._rewrite_text(ring, f, stripped, None)


def test_plaintext_is_encrypted_only_where_allowed_and_not_strict() -> None:
    _, ring = _rings()
    what = _field("commitments", "what")
    assert ring.decrypt(job._rewrite_text(ring, what, "send the deck", None), "commitments:what")
    with pytest.raises(DecryptionError, match="plaintext"):
        job._rewrite_text(ring, _field("messages", "text"), "planted", None)
    ring.strict = True
    with pytest.raises(DecryptionError, match="plaintext"):
        job._rewrite_text(ring, what, "planted", None)


def test_packed_jsonb_value_is_rotated_and_plaintext_adopted() -> None:
    old, ring = _rings()
    f = _field("style_profiles", "rules_json")
    stored = {"enc": old.encrypt('{"formality":2}', f.type.context)}
    new = job._rewrite_packed(ring, f, stored, None)
    assert set(new) == {"enc"} and ring.decrypt(new["enc"], f.type.context) == '{"formality":2}'
    with pytest.raises(job._Unchanged):
        job._rewrite_packed(ring, f, new, None)
    adopted = job._rewrite_packed(ring, f, {"formality": 3}, None)
    assert ring.decrypt(adopted["enc"], f.type.context) == '{"formality":3}'


def test_session_string_is_sealed_with_its_owner() -> None:
    old, ring = _rings()
    f = _field("telegram_sessions", "session_string")
    token = old.encrypt("synthetic-session", f.type.context)
    new = job._rewrite_text(ring, f, token, OWNER)
    previous = crypto._keyring
    crypto.configure_keyring(ring)
    try:
        assert open_owner(OWNER, ring.decrypt(new, f.type.context)) == "synthetic-session"
    finally:
        crypto._keyring = previous
    # Sealed for another owner: refused, left as it is.
    foreign = old.encrypt(seal_owner("someone-else", "x"), f.type.context)
    with pytest.raises(DecryptionError, match="another owner"):
        job._rewrite_text(ring, f, foreign, OWNER)
    # Strict: an unsealed one is refused, never sealed for whoever owns the row now.
    ring.strict = True
    with pytest.raises(DecryptionError, match="strict"):
        job._rewrite_text(ring, f, token, OWNER)


def test_unknown_key_id_fails_the_value_only() -> None:
    _, ring = _rings()
    f = _field("messages", "text")
    stranger = Keyring({"v9": os.urandom(32)}, current="v9").encrypt("x", f.type.context)
    with pytest.raises(DecryptionError, match="unknown key id 'v9'"):
        job._rewrite_text(ring, f, stranger, None)


def test_report_lines_carry_counts_only() -> None:
    report = job.ReencryptAllReport(checked=True)
    report.counts("messages.text").pending = 2
    report.counts("messages.text").failed = 1
    report.legacy_drafts = 3
    assert report.pending == 5 and report.failed == 1
    assert report.lines()[-1] == "total: to rewrite=5 failed=1"
