"""Re-encrypt every stored ciphertext to the current key and format (issues #58, #59).

``python -m scribe.jobs reencrypt_all`` (and the daily ``reencrypt_all`` scheduled job) rewrites
every value that is not yet a column-bound (``e2``) ciphertext under the current key:

- every column whose type is an encrypted column type (:class:`scribe.crypto.EncryptedColumn`),
  found on the SQLAlchemy metadata, so a newly encrypted column is covered without a change
  here (``tests/unit/test_reencrypt_all.py`` fails if one is not);
- the encrypted strings inside ``drafts.output_json`` (``enc`` and every
  ``earlier_messages[].enc``), the one place that calls ``crypto.encrypt`` directly
  (:data:`EMBEDDED_FIELDS`).

Values under a previous key or in the legacy format are decrypted and encrypted again for their
``<table>:<column>``; plaintext left in a ``*FromPlaintext`` column is encrypted; an owner-bound
value (``info={OWNER_COLUMN_INFO: ...}``) is sealed with the row's owner. With a strict keyring
(``SCRIBE_ENCRYPTION_STRICT``) legacy values are refused instead of adopted, so plaintext or a
legacy ciphertext planted in the database is never laundered; only previous-key ``e2`` values
are rotated then.

Rows are read and written raw (the column types are bypassed), locked ``FOR UPDATE`` per batch
of :data:`BATCH_SIZE` in primary-key order and committed per batch; ``updated_at`` is kept.
Resumable and idempotent: the SQL filter selects only rows that still hold a value to rewrite,
so a run interrupted at any point continues where it stopped and a second run finds nothing.
A value that cannot be read (unknown key id, failed authentication, plaintext where none is
allowed) is left as it is, logged with its table, column and row id, and counted as failed.

``--check`` writes nothing and counts what a run would rewrite (``pending``) and what it could
not read (``failed``): the verification step of the key rotation runbook
(docs/configuration.md). Logs carry table and column names, row ids and counts only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    false,
    func,
    literal,
    not_,
    or_,
    select,
    type_coerce,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from scribe.crypto import (
    FORMAT_V2,
    OWNER_COLUMN_INFO,
    PACKED_KEY,
    DecryptionError,
    EncryptedColumn,
    Keyring,
    dump_json,
    get_keyring,
    is_ciphertext,
    is_owner_sealed,
    open_owner,
    packed_token,
    seal_owner,
)
from scribe.db.base import Base
from scribe.db.models import Draft
from scribe.drafting.stored_output import EARLIER_CONTEXT, EARLIER_KEY, ENC_KEY, OUTPUT_CONTEXT
from scribe.logging import get_logger
from scribe.retention.reencrypt import legacy_drafts_condition, reencrypt_legacy_rows

log = get_logger("scribe.retention")

BATCH_SIZE = 200
# ``drafts.output_json`` is a plain JSONB column holding encrypted strings at these places.
EMBEDDED_FIELDS = {"drafts.output_json": (OUTPUT_CONTEXT, EARLIER_CONTEXT)}


@dataclass(frozen=True)
class EncryptedField:
    """One encrypted column; ``owner`` is the column of the same row a value is sealed with."""

    table: Table
    column: Column[Any]
    owner: Column[Any] | None

    @property
    def name(self) -> str:
        return f"{self.table.name}.{self.column.name}"

    @property
    def type(self) -> EncryptedColumn:
        type_ = self.column.type
        assert isinstance(type_, EncryptedColumn)
        return type_


def encrypted_fields(metadata: MetaData = Base.metadata) -> list[EncryptedField]:
    """Every column of ``metadata`` whose type is an encrypted column type."""
    fields = []
    for table in metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, EncryptedColumn):
                owner_name = column.info.get(OWNER_COLUMN_INFO)
                owner = table.columns[owner_name] if owner_name else None
                fields.append(EncryptedField(table, column, owner))
    return fields


@dataclass
class FieldCounts:
    pending: int = 0  # rewritten (or, with --check, to be rewritten)
    failed: int = 0  # unreadable, left as it is


@dataclass
class ReencryptAllReport:
    fields: dict[str, FieldCounts] = field(default_factory=dict)
    legacy_drafts: int = 0  # drafts in the pre-encryption output_json layout (--check only)
    checked: bool = False

    def counts(self, name: str) -> FieldCounts:
        return self.fields.setdefault(name, FieldCounts())

    @property
    def pending(self) -> int:
        return sum(c.pending for c in self.fields.values()) + self.legacy_drafts

    @property
    def failed(self) -> int:
        return sum(c.failed for c in self.fields.values())

    def lines(self) -> list[str]:
        out = [f"{name}: pending={c.pending} failed={c.failed}" for name, c in self.fields.items()]
        if self.checked:
            out.append(f"drafts.output_json (plaintext layout): pending={self.legacy_drafts}")
        verb = "to rewrite" if self.checked else "rewritten"
        out.append(f"total: {verb}={self.pending} failed={self.failed}")
        return out


# --- one value --------------------------------------------------------------------------------


class _Unchanged(Exception):
    pass


def _rewrite_token(keyring: Keyring, token: str, context: str) -> str:
    if keyring.is_current(token):
        raise _Unchanged
    return keyring.rotate(token, context)


def _rewrite_text(keyring: Keyring, f: EncryptedField, raw: str, owner: Any) -> str:
    """The new stored token for ``raw``; raises :class:`_Unchanged` or DecryptionError."""
    context = f.type.context
    if is_ciphertext(raw):
        if keyring.is_current(raw):
            raise _Unchanged
        plaintext = keyring.decrypt(raw, context)
    elif f.type.plaintext_allowed and not keyring.strict:
        plaintext = raw
    else:
        raise DecryptionError("plaintext in an encrypted column")
    return keyring.encrypt(_owned(keyring, f, plaintext, owner), context)


def _rewrite_packed(keyring: Keyring, f: EncryptedField, raw: Any, owner: Any) -> Any:
    token = packed_token(raw)
    if token is not None:
        if keyring.is_current(token):
            raise _Unchanged
        plaintext = keyring.decrypt(token, f.type.context)
    elif f.type.plaintext_allowed and not keyring.strict:
        plaintext = dump_json(raw)
    else:
        raise DecryptionError("plaintext in an encrypted column")
    return {PACKED_KEY: keyring.encrypt(_owned(keyring, f, plaintext, owner), f.type.context)}


def _owned(keyring: Keyring, f: EncryptedField, plaintext: str, owner: Any) -> str:
    if f.owner is None:
        return plaintext
    if owner is None:
        raise DecryptionError("owner column is empty")
    if is_owner_sealed(plaintext):
        open_owner(str(owner), plaintext, strict=True)  # sealed for this row's owner
        return plaintext
    if keyring.strict:
        raise DecryptionError("value without an owner refused (strict mode)")
    return seal_owner(str(owner), plaintext)


# --- SQL filters: rows still holding a value to rewrite ----------------------------------------


def _current_prefix(keyring: Keyring) -> str:
    return f"{FORMAT_V2}:{keyring.current}:"


def _raw(f: EncryptedField) -> ColumnElement[Any]:
    return type_coerce(f.column, JSONB if f.type.storage == "packed_jsonb" else Text)


def _needs_rewrite(f: EncryptedField, prefix: str) -> ColumnElement[bool]:
    raw = _raw(f)
    if f.type.storage == "text":
        return and_(raw.is_not(None), not_(raw.startswith(prefix, autoescape=True)))
    packed_current = case(
        (
            func.jsonb_typeof(raw) == "object",
            and_(
                raw.op("-", return_type=JSONB)(literal(PACKED_KEY, Text))
                == type_coerce(literal({}, JSONB), JSONB),
                func.coalesce(raw[PACKED_KEY].astext.startswith(prefix, autoescape=True), false()),
            ),
        ),
        else_=false(),
    )
    return and_(raw.is_not(None), not_(packed_current))


def _draft_needs_rewrite(prefix: str) -> ColumnElement[bool]:
    output = Draft.output_json
    stale_earlier = func.jsonb_path_exists(
        output,
        literal(f"$.{EARLIER_KEY}[*].{ENC_KEY} ? (!(@ starts with $p))", JSONPATH),
        func.jsonb_build_object("p", literal(prefix, String)),
    )
    output_current = output[ENC_KEY].astext.startswith(prefix, autoescape=True)
    return or_(
        and_(output.has_key(ENC_KEY), not_(func.coalesce(output_current, false()))),
        stale_earlier,
    )


# --- batches ------------------------------------------------------------------------------------


async def _scan(
    session_factory: async_sessionmaker[AsyncSession],
    table: Table,
    columns: Iterable[ColumnElement[Any]],
    condition: ColumnElement[bool],
    rewrite_row: Callable[[Any], dict[Any, Any]],
    *,
    batch_size: int,
    dry_run: bool,
) -> None:
    """Keyset over ``table``'s primary key: lock a batch of rows matching ``condition``, write
    what ``rewrite_row`` returns (column -> raw value), commit."""
    (pk,) = table.primary_key.columns
    keep = {c: c for c in table.columns if c.onupdate is not None}  # e.g. updated_at
    after: Any = None
    while True:
        async with session_factory() as session:
            query = select(pk, *columns).where(condition).order_by(pk).limit(batch_size)
            if after is not None:
                query = query.where(pk > after)
            if not dry_run:
                query = query.with_for_update()
            rows = list((await session.execute(query)).all())
            if not rows:
                return
            for row in rows:
                values = rewrite_row(row)
                if values and not dry_run:
                    await session.execute(
                        update(table).where(pk == row[0]).values({**keep, **values})
                    )
            if not dry_run:
                await session.commit()
        after = rows[-1][0]


async def _reencrypt_table(
    session_factory: async_sessionmaker[AsyncSession],
    keyring: Keyring,
    table: Table,
    fields: list[EncryptedField],
    report: ReencryptAllReport,
    *,
    batch_size: int,
    dry_run: bool,
) -> None:
    prefix = _current_prefix(keyring)
    owners: dict[str, Column[Any]] = {f.owner.name: f.owner for f in fields if f.owner is not None}
    # Row layout: primary key, the raw fields, then the owner columns.
    owner_index = {name: len(fields) + 1 + i for i, name in enumerate(owners)}

    def rewrite_row(row: Any) -> dict[Any, Any]:
        values: dict[Any, Any] = {}
        for index, f in enumerate(fields, start=1):
            raw = row[index]
            if raw is None:
                continue
            owner = row[owner_index[f.owner.name]] if f.owner is not None else None
            counts = report.counts(f.name)
            try:
                if f.type.storage == "text":
                    values[f.column] = literal(_rewrite_text(keyring, f, str(raw), owner), Text)
                else:
                    values[f.column] = literal(_rewrite_packed(keyring, f, raw, owner), JSONB)
            except _Unchanged:
                continue
            except DecryptionError as exc:
                counts.failed += 1
                log.warning(
                    "reencrypt_value_failed",
                    table=table.name,
                    column=f.column.name,
                    row=str(row[0]),
                    reason=str(exc),
                )
                continue
            counts.pending += 1
        return values

    for f in fields:
        report.counts(f.name)
    await _scan(
        session_factory,
        table,
        [*(_raw(f) for f in fields), *owners.values()],
        or_(*(_needs_rewrite(f, prefix) for f in fields)),
        rewrite_row,
        batch_size=batch_size,
        dry_run=dry_run,
    )


async def _reencrypt_drafts_output(
    session_factory: async_sessionmaker[AsyncSession],
    keyring: Keyring,
    report: ReencryptAllReport,
    *,
    batch_size: int,
    dry_run: bool,
) -> None:
    name = "drafts.output_json"
    counts = report.counts(name)
    table = Draft.__table__
    assert isinstance(table, Table)

    def rewrite_row(row: Any) -> dict[Any, Any]:
        data = dict(row[1] or {})
        changed = failed = 0
        places: list[tuple[dict[str, Any], str]] = []
        if isinstance(data.get(ENC_KEY), str):
            places.append((data, OUTPUT_CONTEXT))
        earlier = data.get(EARLIER_KEY)
        if isinstance(earlier, list):
            data[EARLIER_KEY] = earlier = [dict(e) if isinstance(e, dict) else e for e in earlier]
            places += [(e, EARLIER_CONTEXT) for e in earlier if isinstance(e, dict)]
        for holder, context in places:
            token = holder.get(ENC_KEY)
            if not isinstance(token, str):
                continue
            try:
                holder[ENC_KEY] = _rewrite_token(keyring, token, context)
                changed += 1
            except _Unchanged:
                continue
            except DecryptionError as exc:
                failed += 1
                log.warning(
                    "reencrypt_value_failed",
                    table="drafts",
                    column="output_json",
                    row=str(row[0]),
                    reason=str(exc),
                )
        counts.failed += failed
        if not changed:
            return {}
        counts.pending += 1
        return {table.c.output_json: literal(data, JSONB)}

    await _scan(
        session_factory,
        table,
        [table.c.output_json],
        _draft_needs_rewrite(_current_prefix(keyring)),
        rewrite_row,
        batch_size=batch_size,
        dry_run=dry_run,
    )


async def reencrypt_all(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    keyring: Keyring | None = None,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
) -> ReencryptAllReport:
    """Rewrite every encrypted value to the current key and format (``dry_run``: count only)."""
    keyring = keyring or get_keyring()
    report = ReencryptAllReport(checked=dry_run)
    by_table: dict[str, list[EncryptedField]] = {}
    tables: dict[str, Table] = {}
    for f in encrypted_fields():
        by_table.setdefault(f.table.name, []).append(f)
        tables[f.table.name] = f.table
    for table_name, fields in by_table.items():
        await _reencrypt_table(
            session_factory,
            keyring,
            tables[table_name],
            fields,
            report,
            batch_size=batch_size,
            dry_run=dry_run,
        )
    if dry_run:
        async with session_factory() as session:
            count = select(func.count()).select_from(Draft).where(legacy_drafts_condition())
            report.legacy_drafts = int((await session.execute(count)).scalar_one())
    elif not keyring.strict:
        # The pre-encryption drafts layout (and any plaintext left); after the columns, so the
        # drafts it loads through the column types are readable even in the new format only.
        await reencrypt_legacy_rows(session_factory, batch_size=batch_size)
    await _reencrypt_drafts_output(
        session_factory, keyring, report, batch_size=batch_size, dry_run=dry_run
    )
    log.info(
        "reencrypt_all_finished",
        checked=dry_run,
        key_id=keyring.current,
        pending=report.pending,
        failed=report.failed,
    )
    return report


__all__ = [
    "EMBEDDED_FIELDS",
    "EncryptedField",
    "ReencryptAllReport",
    "encrypted_fields",
    "reencrypt_all",
]
