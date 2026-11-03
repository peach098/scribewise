"""Telegram connection texts and keyboards (T008, 002 contracts/control-chat.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import InlineKeyboardMarkup

from scribe.i18n import t
from scribe.telegram.control_bot.render import (
    CALLBACK_DATA_MAX_BYTES,
    account_label,
    method_choice_keyboard,
    not_my_account_keyboard,
    parse_callback,
    qr_caption,
    qr_expired_keyboard,
    qr_keyboard,
    reconnect_keyboard,
    use_qr_keyboard,
)

MASKED = "+7 ••• ••• •• 34"


def _buttons(markup: InlineKeyboardMarkup) -> list[tuple[str, str | None, str | None]]:
    return [(b.text, b.callback_data, b.url) for row in markup.inline_keyboard for b in row]


def _callbacks(markup: InlineKeyboardMarkup) -> list[str | None]:
    return [data for _, data, _ in _buttons(markup)]


# --- account label ------------------------------------------------------------------------------


def test_account_label_prefers_username() -> None:
    assert account_label("Anna Petrova", "anna_p", MASKED, "en") == "Anna Petrova (@anna_p)"
    assert account_label("Anna Petrova", "@anna_p", None, "en") == "Anna Petrova (@anna_p)"


def test_account_label_falls_back_to_masked_phone() -> None:
    assert account_label("Anna Petrova", None, MASKED, "en") == f"Anna Petrova ({MASKED})"
    assert account_label("Anna Petrova", "", MASKED, "en") == f"Anna Petrova ({MASKED})"


def test_account_label_falls_back_to_name() -> None:
    assert account_label("Anna Petrova", None, None, "en") == "Anna Petrova"


def test_account_label_without_name() -> None:
    assert account_label(None, "anna_p", MASKED, "en") == "@anna_p"
    assert account_label("", None, None, "en") == "Unknown account"


# --- QR caption ---------------------------------------------------------------------------------

EXPIRES = datetime(2026, 10, 20, 11, 3, 27, tzinfo=UTC)


def test_qr_caption_text() -> None:
    caption = qr_caption(2, 5, EXPIRES, "Europe/Moscow", "en")  # UTC+3
    assert caption.startswith("📷 Scan this code to connect\n")
    assert "1. Open Telegram on another device where you're signed in" in caption
    assert "2. Go to Settings → Devices → Link Desktop Device." in caption
    assert "3. Point the camera at this code." in caption
    assert 'No "Link Desktop Device"? Update Telegram, or use your phone number instead.' in caption
    assert caption.endswith("\nCode 2 of 5 · valid until 14:03:27")


def test_qr_caption_accepts_zoneinfo() -> None:
    assert qr_caption(1, 5, EXPIRES, ZoneInfo("UTC"), "en").endswith(
        "Code 1 of 5 · valid until 11:03:27"
    )


def test_qr_caption_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        qr_caption(1, 5, datetime(2026, 10, 20, 11, 3, 27), "UTC", "en")


# --- texts --------------------------------------------------------------------------------------


def test_texts_with_placeholders() -> None:
    account = "Anna Petrova (@anna_p)"
    assert t("connect.connected", account=account) == "✅ Connected as Anna Petrova (@anna_p)."
    assert t("connect.reconnected", account=account).startswith(
        "✅ Reconnected as Anna Petrova (@anna_p). "
    )
    assert t("connect.backlog", n=3) == (
        "📥 3 messages arrived while you were disconnected — I'm processing them now."
    )
    assert t(
        "connect.wrong_account", other="Ivan K. (@ivan_k)", expected="Anna Petrova (@anna_p)"
    ) == (
        "That's a different Telegram account: Ivan K. (@ivan_k). Reconnect with the account you "
        "used before (Anna Petrova (@anna_p)), or use /delete_my_data and set up again to switch "
        "accounts. Nothing was connected."
    )
    assert "QR code (recommended)" in t("connect.method_choice")


# --- keyboards ----------------------------------------------------------------------------------


def test_method_choice_keyboard_qr_first() -> None:
    buttons = _buttons(method_choice_keyboard("en"))
    assert [data for _, data, _ in buttons] == ["tglogin:0:qr", "tglogin:0:phone"]
    assert "recommended" in buttons[0][0]


def test_qr_keyboard_without_same_device_link() -> None:
    assert _callbacks(qr_keyboard("en")) == ["tglogin:0:phone", "tglogin:0:cancel"]


def test_qr_keyboard_with_same_device_link_first() -> None:
    buttons = _buttons(qr_keyboard("en", "tg://login?token=abc"))
    assert buttons[0] == ("📲 Approve on this device", None, "tg://login?token=abc")
    assert [data for _, data, _ in buttons[1:]] == ["tglogin:0:phone", "tglogin:0:cancel"]


def test_small_keyboards() -> None:
    assert _callbacks(qr_expired_keyboard("en")) == ["tglogin:0:qr", "tglogin:0:phone"]
    assert _callbacks(use_qr_keyboard("en")) == ["tglogin:0:qr"]
    assert _callbacks(not_my_account_keyboard("en")) == ["tglogin:0:notme"]
    assert _callbacks(reconnect_keyboard("en")) == ["tgreconnect:0"]


@pytest.mark.parametrize(
    "markup",
    [
        method_choice_keyboard("en"),
        qr_keyboard("en", "tg://login?token=abc"),
        qr_expired_keyboard("en"),
        use_qr_keyboard("en"),
        not_my_account_keyboard("en"),
        reconnect_keyboard("en"),
    ],
)
def test_callback_data_is_valid(markup: InlineKeyboardMarkup) -> None:
    for data in _callbacks(markup):
        if data is None:
            continue
        assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES
        parsed = parse_callback(data)
        assert parsed.pack() == data


def test_parse_three_part_login_callback() -> None:
    parsed = parse_callback("tglogin:0:qr")
    assert (parsed.action, parsed.entity_id, parsed.extra) == ("tglogin", "0", "qr")
