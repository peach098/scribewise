"""Re-encrypt rows written before their columns were encrypted (Constitution II, research R7).

The encrypted column types that replaced plaintext ones keep the database type (no migration)
and read old plaintext values as they are (``crypto.EncryptedJSONBFromPlaintext``,
``EncryptedTextFromPlaintext``; ``drafting/stored_output.py`` for ``drafts.output_json``). Most
later writes to an old row change only its state, so the plaintext would stay. This routine
rewrites every such row once, in batches:

- ``style_profiles.rules_json``, ``user_rules_json`` (not ``{"enc": …}``) and ``summary_text``
  (not ciphertext);
- ``style_rule_proposals.rule_json``;
- ``commitments.what`` and ``jira_comment_url``;
- personal data encrypted since issue #57 (``EncryptedTextFromPlaintext``, not ciphertext):
  ``users.display_name`` / ``ooo_message``, ``telegram_sessions.last_error``,
  ``work_chats.title``, ``contacts.display_name``, ``beta_applications.username`` /
  ``use_case``; and ``task_source_connections.config_json`` (not ``{"enc": …}``);
- ``drafts.output_json`` with plaintext content keys (``placeholders``, ``sources``, ``flags``,
  ``conflicts``, ``text``) and no ``enc`` blob: the content moves into ``enc``, a plaintext
  ``text`` key into the encrypted ``drafts.text`` (when that is empty) and is removed.

Rows are found in SQL by their stored layout (never by content), read through the column types
and written back with ``flag_modified``, so the column types encrypt them. Idempotent: a second
run finds nothing. It runs as the ``reencrypt_profiles`` scheduled job (due once at the first
start that has it, then daily as a safety net) and as ``python -m scribe.jobs reencrypt_profiles``.
``reencrypt_all`` (``retention/reencrypt_all.py``) runs it too; with a strict keyring
(``SCRIBE_ENCRYPTION_STRICT``) neither does, since plaintext is then refused, not adopted.
Logs carry counts only.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Text, and_, cast, literal, literal_column, not_, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, array
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql.elements import ColumnElement

from scribe.crypto import PACKED_KEY, DecryptionError, encrypt, is_ciphertext
from scribe.db.models import (
    BetaApplication,
    Commitment,
    Contact,
    Draft,
    StyleProfile,
    StyleRuleProposal,
    TaskSourceConnection,
    TelegramSession,
    User,
    WorkChat,
)
from scribe.drafting.stored_output import ENC_KEY, ENCRYPTED_OUTPUT_FIELDS, OUTPUT_CONTEXT
from scribe.logging import get_logger

log = get_logger("scribe.retention")

BATCH_SIZE = 200
# The ciphertext layouts ``[e2:]<key_id>:<nonce_b64>:<ct_b64>`` (crypto.parse_token). The nonce
# is 12 bytes (16 base64 characters) and the ciphertext holds at least the 16-byte tag, so
# plaintext such as "09:00:00" or "a:b:c" is not taken for ciphertext.
_CIPHERTEXT_SQL = r"^(e2:)?[^:]+:[A-Za-z0-9+/]{16}:[A-Za-z0-9+/=]{24,}$"
_LEGACY_DRAFT_KEYS = ("text", *ENCRYPTED_OUTPUT_FIELDS)


@dataclass(frozen=True)
class ReencryptStats:
    profiles: int = 0
    proposals: int = 0
    commitments: int = 0
    drafts: int = 0
    # issue #57
    users: int = 0
    telegram_sessions: int = 0
    work_chats: int = 0
    contacts: int = 0
    beta_applications: int = 0
    task_source_connections: int = 0


# issue #57: text columns that held plaintext before (``EncryptedTextFromPlaintext``).
PERSONAL_TEXT_COLUMNS: dict[type[Any], tuple[str, ...]] = {
    User: ("display_name", "ooo_message"),
    TelegramSession: ("last_error",),
    WorkChat: ("title",),
    Contact: ("display_name",),
    BetaApplication: ("username", "use_case"),
}


def _not_packed(column: Any) -> ColumnElement[bool]:
    """The stored JSON is not exactly ``{"enc": "<ciphertext>"}`` (the cast bypasses the
    column type, so the WHERE clause sees the stored layout, never decrypted content)."""
    raw = cast(column, JSONB)
    return or_(
        not_(raw.has_key(PACKED_KEY)),
        raw.op("-", return_type=JSONB)(literal(PACKED_KEY, Text)) != literal_column("'{}'::jsonb"),
        not_(raw[PACKED_KEY].astext.regexp_match(_CIPHERTEXT_SQL)),
    )


def _not_ciphertext(column: Any) -> ColumnElement[bool]:
    return not_(cast(column, Text).regexp_match(_CIPHERTEXT_SQL))


async def _batches(
    session_factory: async_sessionmaker[AsyncSession],
    model: Any,
    condition: ColumnElement[bool],
    rewrite: Any,
    batch_size: int,
) -> int:
    """Rewrite the rows matching ``condition``, a batch per transaction. A row that cannot be
    read (a value under an unknown key, or one that fails authentication) is logged with its
    table and id and left as it is; the others go on."""
    done = 0
    after: uuid.UUID | None = None
    while True:
        async with session_factory() as session:
            query = select(model.id).where(condition).order_by(model.id).limit(batch_size)
            if after is not None:
                query = query.where(model.id > after)
            ids = list((await session.execute(query.with_for_update(skip_locked=True))).scalars())
            if not ids:
                return done
            for row_id in ids:
                try:
                    row = (
                        await session.execute(select(model).where(model.id == row_id))
                    ).scalar_one()
                except DecryptionError:
                    log.warning(
                        "reencrypt_row_unreadable", table=model.__tablename__, row=str(row_id)
                    )
                    continue
                rewrite(row)
                done += 1
            await session.commit()
        after = ids[-1]


def _flag_plaintext(row: Any, keys: tuple[str, ...]) -> None:
    """Mark ``keys`` for rewriting, except a value that still has the ciphertext format after
    loading: the column type could not read it (logged there) and kept it as it is, so writing
    it back would wrap it in another layer of encryption."""
    for key in keys:
        value = getattr(row, key)
        if isinstance(value, str) and is_ciphertext(value):
            continue
        flag_modified(row, key)


def _rewrite_profile(row: StyleProfile) -> None:
    _flag_plaintext(row, ("rules_json", "user_rules_json", "summary_text"))


def _rewrite_proposal(row: StyleRuleProposal) -> None:
    flag_modified(row, "rule_json")


def _rewrite_commitment(row: Commitment) -> None:
    _flag_plaintext(row, ("what", "jira_comment_url"))


def _rewrite_columns(columns: tuple[str, ...]) -> Any:
    def rewrite(row: Any) -> None:
        _flag_plaintext(row, columns)

    return rewrite


def _rewrite_connection(row: TaskSourceConnection) -> None:
    flag_modified(row, "config_json")


def _rewrite_draft(row: Draft) -> None:
    data = dict(row.output_json or {})
    if ENC_KEY in data:
        return
    content = {key: data.pop(key) for key in ENCRYPTED_OUTPUT_FIELDS if key in data}
    legacy_text = data.pop("text", None)
    if isinstance(legacy_text, str) and row.text is None:
        row.text = legacy_text
    if content:
        data[ENC_KEY] = encrypt(
            json.dumps(content, ensure_ascii=False, separators=(",", ":")), OUTPUT_CONTEXT
        )
    row.output_json = data


def legacy_drafts_condition() -> ColumnElement[bool]:
    """Drafts whose ``output_json`` still has the plaintext layout (no ``enc`` blob)."""
    legacy_keys = cast(array(list(_LEGACY_DRAFT_KEYS)), ARRAY(Text))
    return and_(
        not_(Draft.output_json.has_key(ENC_KEY)),
        Draft.output_json.has_any(legacy_keys),
    )


async def reencrypt_legacy_rows(
    session_factory: async_sessionmaker[AsyncSession], *, batch_size: int = BATCH_SIZE
) -> ReencryptStats:
    profiles = await _batches(
        session_factory,
        StyleProfile,
        or_(
            _not_packed(StyleProfile.rules_json),
            _not_packed(StyleProfile.user_rules_json),
            _not_ciphertext(StyleProfile.summary_text),
        ),
        _rewrite_profile,
        batch_size,
    )
    proposals = await _batches(
        session_factory,
        StyleRuleProposal,
        _not_packed(StyleRuleProposal.rule_json),
        _rewrite_proposal,
        batch_size,
    )
    commitments = await _batches(
        session_factory,
        Commitment,
        or_(_not_ciphertext(Commitment.what), _not_ciphertext(Commitment.jira_comment_url)),
        _rewrite_commitment,
        batch_size,
    )
    drafts = await _batches(
        session_factory, Draft, legacy_drafts_condition(), _rewrite_draft, batch_size
    )
    personal: dict[str, int] = {}
    for model, columns in PERSONAL_TEXT_COLUMNS.items():
        personal[model.__tablename__] = await _batches(
            session_factory,
            model,
            or_(*(_not_ciphertext(getattr(model, key)) for key in columns)),
            _rewrite_columns(columns),
            batch_size,
        )
    connections = await _batches(
        session_factory,
        TaskSourceConnection,
        _not_packed(TaskSourceConnection.config_json),
        _rewrite_connection,
        batch_size,
    )
    stats = ReencryptStats(
        profiles,
        proposals,
        commitments,
        drafts,
        users=personal[User.__tablename__],
        telegram_sessions=personal[TelegramSession.__tablename__],
        work_chats=personal[WorkChat.__tablename__],
        contacts=personal[Contact.__tablename__],
        beta_applications=personal[BetaApplication.__tablename__],
        task_source_connections=connections,
    )
    log.info("legacy_rows_reencrypted", **asdict(stats))
    return stats


__all__ = ["ReencryptStats", "legacy_drafts_condition", "reencrypt_legacy_rows"]
