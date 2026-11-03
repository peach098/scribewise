"""The context selection a draft was composed with, kept for 🔁 Regenerate (issue #73).

A regenerate of a turn whose newest message is unchanged has the same inputs as the draft it
replaces, so the filter, the search over earlier conversations and its ranking would give the
same answer again. The composer keeps what they selected in ``drafts.llm_usage_json`` under
:data:`SELECTION_KEY` and, on a regenerate with the same ``turn.last_message_id``, only the
drafting call runs again (with the same selection; a new draft text comes from the drafting
model itself).

``llm_usage_json`` is plain JSONB, so the selection holds identifiers only, never content:

- ``history`` / ``commitments``: ``msg:<uuid>`` / ``commit:<uuid>`` refs (database ids);
- ``tasks``: keyed fingerprints of the ``task:<id>`` refs (HMAC under the keyring's HMAC key,
  :func:`task_fingerprint`), because tracker ids count as content (``stored_output``);
- ``earlier``: ``{ref, chat_id, message_id, link}`` of the ranked earlier matches (as in
  ``output_json.earlier_messages``); their text is fetched again by id on reuse;
- ``warnings``: the selection-stage system warnings to show again (``earlier_search_off``,
  ``earlier_search_no_match``);
- ``search``: whether the search over earlier conversations was available for the draft.

:data:`REGENERATIONS_KEY` counts the regenerates of a turn on the same messages; review.py
refuses another one at ``settings.regenerate_max_per_draft`` (an outdated draft, which needs a
regenerate to cover the new messages, is never refused and starts the count again).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast, get_args

from scribe.context.earlier import EarlierRef, LinkReason
from scribe.crypto import comment_hmac

SELECTION_KEY = "selection"
REGENERATIONS_KEY = "regenerations"
_FINGERPRINT_DOMAIN = "draft-selection-task:"


def task_fingerprint(ref: str) -> str:
    """A keyed fingerprint of a ``task:<id>`` ref (no tracker id in the clear)."""
    return comment_hmac(_FINGERPRINT_DOMAIN + ref).hex()[:32]


def _link(value: Any) -> LinkReason:
    if value not in get_args(LinkReason):
        raise ValueError("unknown link reason")
    return cast(LinkReason, value)


@dataclass(frozen=True)
class StoredSelection:
    history_ids: tuple[str, ...] = ()
    task_fingerprints: tuple[str, ...] = ()
    commitment_ids: tuple[str, ...] = ()
    earlier: tuple[EarlierRef, ...] = ()
    warnings: tuple[str, ...] = ()
    search: bool = False
    last_message_id: str | None = None  # the turn's newest message the selection covers

    def to_json(self) -> dict[str, Any]:
        return {
            "last_message_id": self.last_message_id,
            "history": list(self.history_ids),
            "tasks": list(self.task_fingerprints),
            "commitments": list(self.commitment_ids),
            "earlier": [
                {"ref": e.ref, "chat_id": e.chat_id, "message_id": e.message_id, "link": e.link}
                for e in self.earlier
            ],
            "warnings": list(self.warnings),
            "search": self.search,
        }

    @classmethod
    def from_json(cls, data: Any) -> StoredSelection | None:
        """None for anything that is not a well-formed stored selection."""
        if not isinstance(data, Mapping):
            return None
        try:
            return cls(
                history_ids=tuple(str(i) for i in data["history"]),
                task_fingerprints=tuple(str(i) for i in data["tasks"]),
                commitment_ids=tuple(str(i) for i in data["commitments"]),
                earlier=tuple(
                    EarlierRef(
                        ref=str(e["ref"]),
                        chat_id=int(e["chat_id"]),
                        message_id=int(e["message_id"]),
                        link=_link(e["link"]),
                    )
                    for e in data["earlier"]
                ),
                warnings=tuple(str(w) for w in data.get("warnings") or ()),
                search=bool(data["search"]),
                last_message_id=(
                    str(data["last_message_id"]) if data.get("last_message_id") else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


def stored_selection(usage: Mapping[str, Any] | None) -> StoredSelection | None:
    return StoredSelection.from_json((usage or {}).get(SELECTION_KEY))


def regenerations(usage: Mapping[str, Any] | None) -> int:
    value = (usage or {}).get(REGENERATIONS_KEY, 0)
    return value if isinstance(value, int) and value >= 0 else 0


def carried(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """The regenerate keys a new version of the same draft keeps (filled placeholders)."""
    return {k: v for k, v in (usage or {}).items() if k in (SELECTION_KEY, REGENERATIONS_KEY)}


__all__ = [
    "REGENERATIONS_KEY",
    "SELECTION_KEY",
    "EarlierRef",
    "StoredSelection",
    "carried",
    "regenerations",
    "stored_selection",
    "task_fingerprint",
]
