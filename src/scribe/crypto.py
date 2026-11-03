"""Application-level AES-256-GCM encryption for sensitive columns (research R7, issues #58, #59).

Two ciphertext formats, both ``:``-separated with a random 96-bit nonce and a ``ct`` that
includes the 128-bit GCM tag:

- ``e2:<key_id>:<nonce_b64>:<ct_b64>`` (every new write): the associated data is
  ``scribe:e2 \\0 <key_id> \\0 <context>``, where ``context`` is ``<table>:<column>`` for the
  encrypted column types (bound automatically when the column is attached to its table) and
  the caller's label (or ``""``) for direct :func:`encrypt` calls. A ciphertext copied into
  another column or table fails authentication.
- ``<key_id>:<nonce_b64>:<ct_b64>`` (legacy format, written before #59): the associated data is
  only the key id, so it decrypts in any column. Still read unless the keyring is *strict*;
  ``python -m scribe.jobs reencrypt_all`` upgrades every stored value.

The keyring holds the *current* key (every new encryption) and *previous* keys (decryption
only), by key id, so the master key can be rotated: ``SCRIBE_MASTER_KEY`` /
``SCRIBE_MASTER_KEY_ID`` / ``SCRIBE_MASTER_KEYS_PREVIOUS`` (``config.py``, docs/configuration.md).

A *strict* keyring (``SCRIBE_ENCRYPTION_STRICT``) refuses what an attacker with database write
access but without the key could plant: plaintext in the ``*FromPlaintext`` columns,
legacy-format ciphertexts (not bound to a column) and owner-bound values without an owner.

Ciphertexts are bound to a column, not to a row: a column type never sees its row. Values that
must not move between users are additionally sealed with their owner inside the plaintext
(:func:`seal_owner` / :func:`open_owner`; ``telegram_sessions.session_string``), and the
column carries ``info={OWNER_COLUMN_INFO: "user_id"}`` so ``reencrypt_all`` seals old values.

:func:`comment_hmac` fingerprints a Jira comment body without storing it (specs/003 J9): an
HMAC-SHA256 under a key derived from the current key with HKDF (``info=b"jira-comment-hmac"``).
After a key rotation the fingerprints change, so each comment is checked once more.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Column, Table, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import Text, TypeDecorator

from scribe.logging import get_logger

if TYPE_CHECKING:
    from scribe.config import Settings

KEY_BYTES = 32
_log = get_logger("scribe.crypto")
NONCE_BYTES = 12
TAG_BYTES = 16  # AES-GCM tag: every ciphertext is at least this long
DEFAULT_KEY_ID = "v1"
COMMENT_HMAC_INFO = b"jira-comment-hmac"
# Marker of the column-bound format; reserved, so never a key id.
FORMAT_V2 = "e2"
_AAD_V2 = b"scribe:e2"
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,32}")
# Column ``info`` key naming the column of the same row that owns a sealed value.
OWNER_COLUMN_INFO = "scribe_owner_column"
_OWNER_PREFIX = "scribe-owner:"


class DecryptionError(Exception):
    """Ciphertext is malformed, was tampered with or moved, its key is unknown, or a strict
    keyring refused a legacy value."""


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.b64decode(data, validate=True)


def valid_key_id(key_id: str) -> bool:
    return bool(KEY_ID_PATTERN.fullmatch(key_id)) and key_id != FORMAT_V2


@dataclass(frozen=True, slots=True)
class Token:
    """A parsed ciphertext; ``bound`` is False for the legacy format."""

    bound: bool
    key_id: str
    nonce: bytes
    ct: bytes


def parse_token(token: str) -> Token:
    parts = token.split(":")
    if len(parts) == 4 and parts[0] == FORMAT_V2:
        bound = True
        key_id, nonce_b64, ct_b64 = parts[1:]
    elif len(parts) == 3:
        bound = False
        key_id, nonce_b64, ct_b64 = parts
    else:
        raise DecryptionError("malformed ciphertext")
    if not key_id:
        raise DecryptionError("malformed ciphertext")
    try:
        nonce, ct = _b64d(nonce_b64), _b64d(ct_b64)
    except (binascii.Error, ValueError) as exc:
        raise DecryptionError("malformed ciphertext") from exc
    if len(nonce) != NONCE_BYTES or len(ct) < TAG_BYTES:
        raise DecryptionError("malformed ciphertext")
    return Token(bound, key_id, nonce, ct)


def _aad_v2(key_id: str, context: str) -> bytes:
    return b"\x00".join((_AAD_V2, key_id.encode("ascii"), context.encode("utf-8")))


class Keyring:
    """AES-256 keys by id: ``current`` encrypts, every key decrypts (previous keys after a
    rotation). ``strict`` refuses the legacy format (see the module docstring)."""

    def __init__(self, keys: Mapping[str, bytes], current: str, *, strict: bool = False) -> None:
        if current not in keys:
            raise ValueError(f"current key id {current!r} is not in the keyring")
        for key_id, key in keys.items():
            if not valid_key_id(key_id):
                raise ValueError(f"invalid key id {key_id!r}")
            if len(key) != KEY_BYTES:
                raise ValueError(f"key {key_id!r} must be {KEY_BYTES} bytes")
        self._aead = {key_id: AESGCM(key) for key_id, key in keys.items()}
        self.current = current
        self.strict = strict
        self._hmac_key = HKDF(
            algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=COMMENT_HMAC_INFO
        ).derive(keys[current])

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._aead)

    def comment_hmac(self, text: str) -> bytes:
        """HMAC-SHA256 of ``text`` under the derived fingerprint key (no plaintext kept)."""
        return hmac.new(self._hmac_key, text.encode("utf-8"), hashlib.sha256).digest()

    def encrypt(self, plaintext: str, context: str | None = None) -> str:
        """Column-bound ciphertext under the current key; ``context`` is ``<table>:<column>``."""
        nonce = os.urandom(NONCE_BYTES)
        aad = _aad_v2(self.current, context or "")
        ct = self._aead[self.current].encrypt(nonce, plaintext.encode("utf-8"), aad)
        return f"{FORMAT_V2}:{self.current}:{_b64e(nonce)}:{_b64e(ct)}"

    def decrypt(self, token: str, context: str | None = None, *, strict: bool | None = None) -> str:
        """Plaintext of ``token``; a column-bound one must have been written for ``context``.

        ``strict`` (default: the keyring's) refuses the legacy format."""
        parsed = parse_token(token)
        aead = self._aead.get(parsed.key_id)
        if aead is None:
            raise DecryptionError(f"unknown key id {parsed.key_id!r}")
        if parsed.bound:
            aad = _aad_v2(parsed.key_id, context or "")
        elif self.strict if strict is None else strict:
            raise DecryptionError("legacy ciphertext refused (strict mode)")
        else:
            aad = parsed.key_id.encode("ascii")
        try:
            return aead.decrypt(parsed.nonce, parsed.ct, aad).decode("utf-8")
        except InvalidTag as exc:
            raise DecryptionError("ciphertext failed authentication") from exc

    def is_current(self, token: str) -> bool:
        """True when ``token`` is column-bound and under the current key (nothing to rewrite)."""
        parsed = parse_token(token)
        return parsed.bound and parsed.key_id == self.current

    def needs_rotation(self, token: str) -> bool:
        """True when ``token`` was written with another key or in the legacy format."""
        return not self.is_current(token)

    def rotate(self, token: str, context: str | None = None) -> str:
        """Re-encrypt ``token`` under the current key, bound to ``context`` (no-op if current).

        A column-bound token is only re-encrypted for the context it was written for; a legacy
        one is adopted into ``context`` unless the keyring is strict (then it is refused, so a
        planted legacy token is never laundered into a column-bound one)."""
        if self.is_current(token):
            return token
        return self.encrypt(self.decrypt(token, context), context)


# --- process-wide keyring used by the SQLAlchemy column types ---------------------------------

_keyring: Keyring | None = None


def configure_keyring(keyring: Keyring) -> None:
    global _keyring
    _keyring = keyring


def keyring_from_master_key(
    master_key: bytes, key_id: str = DEFAULT_KEY_ID, *, strict: bool = False
) -> Keyring:
    return Keyring({key_id: master_key}, current=key_id, strict=strict)


def keyring_from_settings(settings: Settings) -> Keyring:
    """The current key plus the previous ones (decryption only) from the configuration."""
    keys = dict(settings.previous_master_keys)
    keys[settings.master_key_id] = settings.master_key_bytes
    return Keyring(keys, current=settings.master_key_id, strict=settings.encryption_strict)


def get_keyring() -> Keyring:
    if _keyring is None:
        raise RuntimeError("encryption keyring is not configured; call configure_keyring()")
    return _keyring


def encrypt(plaintext: str, context: str | None = None) -> str:
    return get_keyring().encrypt(plaintext, context)


def decrypt(token: str, context: str | None = None) -> str:
    return get_keyring().decrypt(token, context)


def comment_hmac(text: str) -> bytes:
    return get_keyring().comment_hmac(text)


def is_ciphertext(token: str) -> bool:
    """True when ``token`` has a ciphertext format (it may still fail authentication)."""
    try:
        parse_token(token)
    except DecryptionError:
        return False
    return True


# --- owner sealing (values that must not move between users) ---------------------------------


def seal_owner(owner: str, value: str) -> str:
    """``value`` with its owner (e.g. ``users.id``) in front, to be encrypted as a whole."""
    return f"{_OWNER_PREFIX}{owner}\n{value}"


def is_owner_sealed(stored: str) -> bool:
    return stored.startswith(_OWNER_PREFIX)


def open_owner(owner: str, stored: str, *, strict: bool | None = None) -> str:
    """The value :func:`seal_owner` sealed for ``owner``; another owner is refused. A value
    stored before sealing is returned as is unless strict (default: the process keyring's)."""
    if not is_owner_sealed(stored):
        if get_keyring().strict if strict is None else strict:
            raise DecryptionError("value without an owner refused (strict mode)")
        return stored
    sealed_for, sep, value = stored[len(_OWNER_PREFIX) :].partition("\n")
    if not sep or sealed_for != owner:
        raise DecryptionError("value belongs to another owner")
    return value


# --- SQLAlchemy column types -------------------------------------------------------------------


class EncryptedColumn:
    """Mixin of every encrypted column type: ``binding`` (``<table>:<column>``) is the AES-GCM
    context. It is set when the column is attached to its table; pass it explicitly only to
    keep values readable after renaming a table or column.

    ``storage`` tells ``reencrypt_all`` how the token is stored: ``"text"`` (the column holds
    it) or ``"packed_jsonb"`` (``{"enc": token}``). ``plaintext_allowed``: a value that is not
    in the stored layout is a legacy plaintext row (the ``*FromPlaintext`` types)."""

    storage: ClassVar[Literal["text", "packed_jsonb"]] = "text"
    plaintext_allowed: ClassVar[bool] = False
    binding: str | None

    # ``binding`` is a named parameter so SQLAlchemy puts it in the statement cache key.
    def __init__(self, binding: str | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.binding = binding
        self._explicit_binding = binding is not None

    @property
    def context(self) -> str:
        return self.binding or ""


@event.listens_for(Column, "after_parent_attach")
def _bind_encrypted_column(column: Column[Any], table: Any) -> None:
    """Bind an encrypted column type to ``<table>:<column>`` (a fresh copy per column, so a
    type instance shared by two columns never mixes their contexts)."""
    type_ = column.type
    if not isinstance(type_, EncryptedColumn) or not isinstance(table, Table):
        return
    binding = f"{table.name}:{column.name}"
    if type_.binding == binding or type_._explicit_binding:
        return
    bound = type_.copy()
    bound.__dict__.pop("_static_cache_key", None)  # memoised; the binding is part of it
    bound.binding = binding
    bound._explicit_binding = False
    column.type = bound


def _plaintext_refused() -> DecryptionError:
    return DecryptionError("plaintext value in an encrypted column refused (strict mode)")


class EncryptedText(EncryptedColumn, TypeDecorator[str]):
    """``str`` column stored as AES-GCM ciphertext text."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else encrypt(value, self.context)

    def process_result_value(self, value: Any | None, dialect: Dialect) -> str | None:
        return None if value is None else decrypt(str(value), self.context)


class EncryptedTextFromPlaintext(EncryptedText):
    """:class:`EncryptedText` for a column that used to hold plaintext (``commitments.what``).

    Same database type (``Text``, so no migration). A stored value that is not in the
    ciphertext format at all is a row written before the switch and is returned as is (refused
    by a strict keyring); it is encrypted on its next write or by ``reencrypt_all``. A value in
    the ciphertext format that fails authentication still raises :class:`DecryptionError`.
    """

    cache_ok = True
    plaintext_allowed = True

    def process_result_value(self, value: Any | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        token = str(value)
        if is_ciphertext(token):
            try:
                return decrypt(token, self.context)
            except DecryptionError:
                if get_keyring().strict:
                    raise
                # Not strict, so plaintext may still be here: a value such as a contact's name
                # can have the ciphertext format by chance or on purpose (a name chosen by
                # someone else). Returned as it is, so one row never breaks a whole list;
                # ``reencrypt_all --check`` reports it as failed.
                _log.warning("encrypted_value_unreadable", context=self.context)
                return token
        if get_keyring().strict:
            raise _plaintext_refused()
        return token


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class EncryptedJSON(EncryptedColumn, TypeDecorator[Any]):
    """JSON-serialisable value stored as AES-GCM ciphertext text."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any | None, dialect: Dialect) -> str | None:
        return None if value is None else encrypt(dump_json(value), self.context)

    def process_result_value(self, value: Any | None, dialect: Dialect) -> Any | None:
        return None if value is None else json.loads(decrypt(str(value), self.context))


PACKED_KEY = "enc"


def packed_token(value: Any) -> str | None:
    """The token of a stored ``{"enc": token}`` document; None for any other layout."""
    if isinstance(value, dict) and set(value) == {PACKED_KEY}:
        token = value[PACKED_KEY]
        if isinstance(token, str) and is_ciphertext(token):
            return token
    return None


class EncryptedJSONBFromPlaintext(EncryptedColumn, TypeDecorator[Any]):
    """A ``jsonb`` column whose value is stored encrypted inside the JSON (``style_profiles``
    rules, ``style_rule_proposals.rule_json``).

    Same database type (``JSONB``, so no migration): the stored document is
    ``{"enc": "<ciphertext of the JSON value>"}``, the layout ``drafting/stored_output.py`` uses
    for ``drafts.output_json``. A stored document that is not in this layout is a row written
    before the switch and is returned as is (refused by a strict keyring); it is encrypted on
    its next write or by ``reencrypt_all``. Never filtered or indexed in SQL.
    """

    impl = JSONB
    cache_ok = True
    storage = "packed_jsonb"
    plaintext_allowed = True

    def process_bind_param(self, value: Any | None, dialect: Dialect) -> Any | None:
        if value is None:
            return None
        return {PACKED_KEY: encrypt(dump_json(value), self.context)}

    def process_result_value(self, value: Any | None, dialect: Dialect) -> Any | None:
        if value is None:
            return None
        token = packed_token(value)
        if token is not None:
            return json.loads(decrypt(token, self.context))
        if get_keyring().strict:
            raise _plaintext_refused()
        return value
