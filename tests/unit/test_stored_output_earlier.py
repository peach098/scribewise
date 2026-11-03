"""Cited earlier messages inside ``drafts.output_json`` (specs/004 T012, research S7)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from scribe.drafting.stored_output import (
    CONTENT_KEYS,
    EARLIER_KEY,
    StoredEarlier,
    load_stored_output,
    pack_output,
)
from tests.integration.us1_support import draft_output

SENT = datetime(2026, 7, 1, 10, 30, tzinfo=UTC)


def _earlier(excerpt: str = "The pilot price is 120 000, agreed") -> StoredEarlier:
    return StoredEarlier(
        ref="earlier:1",
        chat_id=-1001234567890,
        message_id=77,
        sent_at=SENT,
        chat_title="Synthetic Project",
        sender="Synthetic Client",
        excerpt=excerpt,
    )


def test_round_trip_keeps_cited_earlier_messages() -> None:
    output = draft_output("Yes, 120 000 as agreed.")
    packed = pack_output(output, [], None, [_earlier()])
    stored = load_stored_output(packed, "Yes, 120 000 as agreed.")
    assert stored.output is not None
    assert stored.earlier_messages == [_earlier()]


def test_content_is_encrypted_metadata_is_clear() -> None:
    packed = pack_output(draft_output("x"), [], None, [_earlier()])
    raw = json.dumps(packed, ensure_ascii=False)
    for secret in ("Synthetic Project", "Synthetic Client", "120 000"):
        assert secret not in raw
    [entry] = packed[EARLIER_KEY]
    assert entry["ref"] == "earlier:1" and entry["message_id"] == 77
    assert entry["chat_id"] == -1001234567890 and entry["sent_at"].startswith("2026-07-01")


def test_excerpt_is_cut_to_200_chars() -> None:
    packed = pack_output(draft_output("x"), [], None, [_earlier("y" * 500)])
    [item] = load_stored_output(packed, "x").earlier_messages
    assert item.excerpt == "y" * 200


def test_old_drafts_and_unreadable_entries_load_empty() -> None:
    packed = pack_output(draft_output("x"), [])
    assert EARLIER_KEY not in packed
    assert load_stored_output(packed, "x").earlier_messages == []
    packed[EARLIER_KEY] = [{"ref": "earlier:1", "enc": "not-a-ciphertext"}]
    assert load_stored_output(packed, "x").earlier_messages == []


def test_retention_strips_earlier_messages_with_the_content() -> None:
    assert EARLIER_KEY in CONTENT_KEYS


def test_purged_draft_still_reports_no_output() -> None:
    packed = pack_output(draft_output("x"), ["earlier_search_no_match"], None, [_earlier()])
    stored = load_stored_output(packed, None)
    assert stored.output is None
    assert stored.system_warnings == ["earlier_search_no_match"]
