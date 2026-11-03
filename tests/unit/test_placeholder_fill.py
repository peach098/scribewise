"""Filling needs-info placeholders from the user's reply (issue #2)."""

from __future__ import annotations

import pytest

from scribe.drafting.placeholders import (
    Slot,
    ValueCountMismatch,
    fill,
    fill_slots,
    parse_values,
)
from scribe.llm.provider import Placeholder


def _ph(marker: str, question: str) -> Placeholder:
    return Placeholder(marker=marker, question=question)


def test_slots_follow_the_listed_order_and_skip_markers_not_in_text() -> None:
    text = "Отчёт будет [[день]] в [[время]]."
    slots = fill_slots(
        text,
        [
            _ph("[[время]]", "What time?"),
            _ph("[[gone]]", "Not in the text"),
            _ph("[[день]]", "Which day?"),
        ],
    )
    assert slots == [Slot("[[время]]", "What time?"), Slot("[[день]]", "Which day?")]


def test_unlisted_markers_come_last_and_repeated_markers_are_one_slot() -> None:
    text = "[[a]] and [[b]], again [[a]]"
    assert fill_slots(text, [_ph("[[b]]", "B?")]) == [Slot("[[b]]", "B?"), Slot("[[a]]", "a")]
    assert fill_slots("no markers", [_ph("[[x]]", "X?")]) == []


def test_single_slot_takes_the_whole_reply() -> None:
    assert parse_values("  в пятницу,\nпосле обеда  ", 1) == ["в пятницу,\nпосле обеда"]
    with pytest.raises(ValueCountMismatch):
        parse_values("   ", 1)


def test_several_slots_take_one_value_per_line() -> None:
    assert parse_values("пятница\n\n 15:00 \n", 2) == ["пятница", "15:00"]
    with pytest.raises(ValueCountMismatch) as exc:
        parse_values("пятница", 2)
    assert (exc.value.expected, exc.value.got) == (2, 1)
    with pytest.raises(ValueCountMismatch):
        parse_values("a\nb\nc", 2)


def test_fill_replaces_every_occurrence_in_one_pass() -> None:
    slots = [Slot("[[a]]", "A?"), Slot("[[b]]", "B?")]
    assert fill("[[a]], [[b]], [[a]]", slots, ["[[b]]", "2"]) == "[[b]], 2, [[b]]"
    assert fill("[[a]] [[c]]", slots[:1], ["1"]) == "1 [[c]]"
    with pytest.raises(ValueCountMismatch):
        fill("[[a]]", slots, ["1"])
