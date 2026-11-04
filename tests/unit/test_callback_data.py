"""Callback-data format <action>:<entity_id>[:<extra>] <= 64 bytes (T020)."""

from __future__ import annotations

import uuid

import pytest

from scribe.telegram.control_bot.render import (
    COMMAND_NAMES,
    CallbackData,
    InvalidCallbackData,
    button,
    format_callback,
    help_text,
    keyboard,
    parse_callback,
)


def test_round_trip_with_uuid_entities() -> None:
    draft_id = uuid.uuid4()
    data = format_callback("approve", draft_id)
    assert data == f"approve:{draft_id}"
    assert parse_callback(data) == CallbackData("approve", str(draft_id))
    longest = format_callback("rule_dismiss", draft_id, "x" * 10)
    assert len(longest.encode()) <= 64
    assert parse_callback(longest).extra == "x" * 10


@pytest.mark.parametrize(
    ("action", "entity", "extra"),
    [
        ("Approve", "1", None),
        ("approve!", "1", None),
        ("approve", "", None),
        ("approve", "a:b", None),
        ("approve", "1", ""),
        ("approve", "1", "x:y"),
        ("approve", "1", "x" * 60),
    ],
)
def test_format_rejects_invalid(action: str, entity: str, extra: str | None) -> None:
    with pytest.raises(InvalidCallbackData):
        format_callback(action, entity, extra)


@pytest.mark.parametrize(
    "data", [None, "", "approve", "approve:", ":1", "a:b:c:d", "APPROVE:1", "approve:" + "1" * 64]
)
def test_parse_rejects_invalid(data: str | None) -> None:
    with pytest.raises(InvalidCallbackData):
        parse_callback(data)


def test_parse_counts_bytes_not_characters() -> None:
    with pytest.raises(InvalidCallbackData):
        parse_callback("sources:" + "ж" * 30)  # 8 + 60 bytes


def test_keyboard_helpers() -> None:
    markup = keyboard([[button("✅ Send", CallbackData("approve", "d1"))]])
    assert markup.inline_keyboard[0][0].callback_data == "approve:d1"
    with pytest.raises(InvalidCallbackData):
        button("bad", "no-separator")


def test_help_lists_every_command() -> None:
    for lang in ("en", "ru"):
        text = help_text(lang)
        for name in COMMAND_NAMES:
            assert f"/{name}" in text
    assert {"start", "help", "delete_my_data", "telegram", "my_data"} <= set(COMMAND_NAMES)
    assert help_text("ru").startswith("Scribe готовит черновики")


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("autostop", None),
        ("autooffer", "later"),
        ("autooffer", "never"),
        ("autoconfirm", "manual"),
        ("autoconfirm", "offer"),
        ("autowarn", "keep"),
        ("chatauto", "reset"),
        ("chatauto", "off"),
    ],
)
def test_auto_send_callbacks_fit_with_a_uuid(action: str, extra: str | None) -> None:
    data = format_callback(action, uuid.uuid4(), extra)
    assert len(data.encode()) <= 64
    assert parse_callback(data).action == action


def test_auto_send_window_callback_carries_seconds() -> None:
    assert parse_callback(format_callback("autowin", 900)) == CallbackData("autowin", "900")
