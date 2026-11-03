"""``drafts.output_json``: what the composer stores next to the draft (Constitution II, R7, R10).

``output_json`` is a plain JSONB column, so it holds **metadata only** in the clear:

- the non-content ``DraftOutput`` fields ``language``, ``needs_info``, ``tone_shift_applied``,
  ``context_warnings``;
- the system fields ``system_warnings`` and ``reply_to_message_id`` (a ``messages.id``);
- ``creates_commitment`` / ``sensitive``: the boolean flag values;
- ``enc``: one AES-GCM string (``crypto.encrypt``, the process keyring) holding the JSON of
  every content-bearing field: ``placeholders`` (markers, questions), ``sources`` (ids, incl.
  Trello card ids, and ``used_for`` notes), ``flags`` (the commitment's ``what``/``due``, the
  sensitive categories) and ``conflicts`` (descriptions, which may quote a task title, and ids).

- ``earlier_messages`` (specs/004 research S7): the earlier messages found by search that the
  draft cites, each ``{ref, chat_id, message_id, sent_at, enc}`` with ``enc`` holding the
  encrypted chat title, sender and an excerpt of at most 200 characters.

The ``enc`` strings are bound to :data:`OUTPUT_CONTEXT` / :data:`EARLIER_CONTEXT` (issue #59), so
one cannot be moved into an encrypted column or between the two places; ``reencrypt_all``
(``retention/reencrypt_all.py``) re-encrypts them with the encrypted columns.

The draft text itself is never stored here: ``drafts.text`` (encrypted) holds it.

:func:`load_stored_output` is the one reader (card, Sources, send, confirmations). It also reads
rows written before this layout, whose content keys (``text``, ``placeholders``, ``sources``,
``flags``, ``conflicts``) are plaintext; the retention job strips :data:`CONTENT_KEYS` from
``output_json`` together with ``drafts.text`` 30 days after ``finalized_at``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from scribe.crypto import DecryptionError, decrypt, encrypt, get_keyring
from scribe.llm.provider import DraftOutput

ENC_KEY = "enc"
REPLY_TO_KEY = "reply_to_message_id"
SYSTEM_WARNINGS_KEY = "system_warnings"
EARLIER_KEY = "earlier_messages"
EXCERPT_CHARS = 200
# AES-GCM contexts of the encrypted strings inside ``drafts.output_json``.
OUTPUT_CONTEXT = "drafts:output_json"
EARLIER_CONTEXT = "drafts:output_json:earlier_messages"
PLAIN_OUTPUT_FIELDS = ("language", "needs_info", "tone_shift_applied", "context_warnings")
ENCRYPTED_OUTPUT_FIELDS = ("placeholders", "sources", "flags", "conflicts")
# Everything the retention job removes: the encrypted blob and the plaintext keys of rows
# written before it existed.
CONTENT_KEYS = (ENC_KEY, "text", EARLIER_KEY, *ENCRYPTED_OUTPUT_FIELDS)


@dataclass(frozen=True)
class StoredEarlier:
    """One cited earlier message (decrypted); ``ref`` is the ``earlier:<n>`` source id."""

    ref: str
    chat_id: int
    message_id: int
    sent_at: datetime
    chat_title: str
    sender: str
    excerpt: str


@dataclass(frozen=True)
class StoredOutput:
    """The decrypted ``DraftOutput`` (None: purged, unreadable or never stored) plus the system
    fields."""

    output: DraftOutput | None
    system_warnings: list[str] = field(default_factory=list)
    reply_to_message_id: str | None = None
    # False for drafts stored before the composer recorded the quoted message.
    reply_to_recorded: bool = False
    earlier_messages: list[StoredEarlier] = field(default_factory=list)


def pack_output(
    output: DraftOutput,
    system_warnings: list[str],
    reply_to_message_id: str | None = None,
    earlier_messages: Iterable[StoredEarlier] = (),
) -> dict[str, Any]:
    data = output.model_dump(mode="json")
    content = {key: data[key] for key in ENCRYPTED_OUTPUT_FIELDS}
    earlier = [_pack_earlier(item) for item in earlier_messages]
    return {
        **({EARLIER_KEY: earlier} if earlier else {}),
        **{key: data[key] for key in PLAIN_OUTPUT_FIELDS},
        "creates_commitment": output.flags.creates_commitment.value,
        "sensitive": output.flags.sensitive.value,
        SYSTEM_WARNINGS_KEY: list(system_warnings),
        REPLY_TO_KEY: reply_to_message_id,
        ENC_KEY: encrypt(
            json.dumps(content, ensure_ascii=False, separators=(",", ":")), OUTPUT_CONTEXT
        ),
    }


def _pack_earlier(item: StoredEarlier) -> dict[str, Any]:
    secret = {
        "chat_title": item.chat_title,
        "sender": item.sender,
        "excerpt": item.excerpt[:EXCERPT_CHARS],
    }
    return {
        "ref": item.ref,
        "chat_id": item.chat_id,
        "message_id": item.message_id,
        "sent_at": item.sent_at.isoformat(),
        ENC_KEY: encrypt(
            json.dumps(secret, ensure_ascii=False, separators=(",", ":")), EARLIER_CONTEXT
        ),
    }


def _load_earlier(raw: Any) -> list[StoredEarlier]:
    """Entries that cannot be read (purged, corrupt, foreign key) are skipped."""
    items: list[StoredEarlier] = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            secret = json.loads(decrypt(str(entry[ENC_KEY]), EARLIER_CONTEXT))
            items.append(
                StoredEarlier(
                    ref=str(entry["ref"]),
                    chat_id=int(entry["chat_id"]),
                    message_id=int(entry["message_id"]),
                    sent_at=datetime.fromisoformat(str(entry["sent_at"])),
                    chat_title=str(secret.get("chat_title", "")),
                    sender=str(secret.get("sender", "")),
                    excerpt=str(secret.get("excerpt", "")),
                )
            )
        except (DecryptionError, ValueError, KeyError, TypeError):
            continue
    return items


def load_stored_output(output_json: dict[str, Any] | None, text: str | None) -> StoredOutput:
    """Read ``drafts.output_json`` (either layout); ``text`` is ``drafts.text``."""
    data = dict(output_json or {})
    warnings = [str(w) for w in data.get(SYSTEM_WARNINGS_KEY) or []]
    earlier = _load_earlier(data.get(EARLIER_KEY))
    raw_reply_to = data.get(REPLY_TO_KEY)
    reply_to = str(raw_reply_to) if raw_reply_to else None
    recorded = REPLY_TO_KEY in data
    content: dict[str, Any] | None
    if ENC_KEY in data:
        try:
            content = json.loads(decrypt(str(data[ENC_KEY]), OUTPUT_CONTEXT))
        except (DecryptionError, ValueError):
            content = None
    elif all(key in data for key in ENCRYPTED_OUTPUT_FIELDS) and not get_keyring().strict:
        # legacy plaintext row (refused by a strict keyring)
        content = {key: data[key] for key in ENCRYPTED_OUTPUT_FIELDS}
    else:
        content = None
    draft_text = text if text is not None else data.get("text")
    if not isinstance(content, dict) or draft_text is None:
        return StoredOutput(None, warnings, reply_to, recorded, earlier)
    candidate = {
        **{key: data.get(key) for key in PLAIN_OUTPUT_FIELDS},
        **content,
        "text": draft_text,
    }
    try:
        output = DraftOutput.model_validate(candidate)
    except ValidationError:
        return StoredOutput(None, warnings, reply_to, recorded, earlier)
    return StoredOutput(output, warnings, reply_to, recorded, earlier)


__all__ = [
    "CONTENT_KEYS",
    "EARLIER_CONTEXT",
    "EARLIER_KEY",
    "ENC_KEY",
    "EXCERPT_CHARS",
    "OUTPUT_CONTEXT",
    "REPLY_TO_KEY",
    "StoredEarlier",
    "StoredOutput",
    "load_stored_output",
    "pack_output",
]
