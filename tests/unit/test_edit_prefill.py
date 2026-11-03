"""✏️ Edit prompt: input-field / copy button by length and mode, @bot prefix stripping (issue #7)."""

from __future__ import annotations

from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.types import ForceReply, InlineKeyboardMarkup, User

from scribe.config import Settings
from scribe.i18n import t
from scribe.telegram.control_bot.edit_prefill import (
    PREFILL_MAX_CHARS,
    edit_prompt,
    edited_text_of,
    inline_mode_on,
    strip_bot_mention,
)
from scribe.telegram.control_bot.render import TELEGRAM_TEXT_MAX

SHORT = "Привет! Отчёт будет в пятницу."


class _StubBot:
    def __init__(self, *, inline: bool | None = None, fail: bool = False) -> None:
        self.inline, self.fail, self.calls = inline, fail, 0

    async def me(self) -> User:
        self.calls += 1
        if self.fail:
            raise ConnectionError("offline")
        return User(
            id=1,
            is_bot=True,
            first_name="Scribe",
            username="Scribe_Test_Bot",
            supports_inline_queries=self.inline,
        )


def _bot(**values: Any) -> Bot:
    return cast(Bot, _StubBot(**values))


def _button(markup: Any) -> Any:
    assert isinstance(markup, InlineKeyboardMarkup)
    ((button,),) = markup.inline_keyboard
    return button


def test_short_draft_inline_button_fills_input_field() -> None:
    text, markup, parse_mode = edit_prompt(SHORT, inline=True, lang="en")
    button = _button(markup)
    assert button.switch_inline_query_current_chat == SHORT
    assert button.copy_text is None and button.callback_data is None
    assert "/cancel" in text and parse_mode is None


def test_short_draft_copy_button_without_inline_mode() -> None:
    text, markup, parse_mode = edit_prompt(SHORT, inline=False, lang="en")
    button = _button(markup)
    assert button.copy_text is not None and button.copy_text.text == SHORT
    assert button.switch_inline_query_current_chat is None
    assert "/cancel" in text and parse_mode is None


@pytest.mark.parametrize("inline", [True, False])
def test_length_switch_at_the_256_char_limit(inline: bool) -> None:
    at_limit = "a" * PREFILL_MAX_CHARS
    _, markup, _ = edit_prompt(at_limit, inline=inline, lang="en")
    button = _button(markup)
    assert (button.switch_inline_query_current_chat or button.copy_text.text) == at_limit

    over = "a" * (PREFILL_MAX_CHARS + 1)
    text, markup, parse_mode = edit_prompt(over, inline=inline, lang="en")
    assert isinstance(markup, ForceReply) and markup.input_field_placeholder == t(
        "edit.placeholder"
    )
    assert parse_mode == "HTML"
    assert text == f"{t('edit.long_prompt')}\n\n<code>{over}</code>"


def test_long_draft_code_block_is_escaped() -> None:
    draft = "<b>не жирный</b> & [[дата]] " * 20
    text, _, parse_mode = edit_prompt(draft, inline=True, lang="en")
    assert parse_mode == "HTML" and "<b>" not in text
    assert "&lt;b&gt;не жирный&lt;/b&gt; &amp; [[дата]]" in text


def test_too_long_for_a_message_falls_back_to_plain_force_reply() -> None:
    text, markup, parse_mode = edit_prompt("a" * TELEGRAM_TEXT_MAX, inline=True, lang="en")
    assert (text, parse_mode) == (t("edit.prompt"), None) and isinstance(markup, ForceReply)
    text, markup, _ = edit_prompt("   ", inline=True, lang="en")
    assert text == t("edit.prompt") and isinstance(markup, ForceReply)


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ("@Scribe_Test_Bot Отчёт в пятницу.", "Отчёт в пятницу."),
        ("@scribe_test_bot Отчёт в пятницу.", "Отчёт в пятницу."),  # case-insensitive
        ("@Scribe_Test_Bot   two spaces", "two spaces"),
        ("@Scribe_Test_Bot\nfirst line\nsecond", "first line\nsecond"),
        ("@Scribe_Test_BotОтчёт", "Отчёт"),  # space deleted by the user
        ("@Scribe_Test_Bot", ""),
        ("Отчёт в пятницу.", "Отчёт в пятницу."),
        ("@Scribe_Test_Bot_2 hi", "@Scribe_Test_Bot_2 hi"),  # another account
        ("@anna, see you", "@anna, see you"),
        ("Ping @Scribe_Test_Bot later", "Ping @Scribe_Test_Bot later"),  # only a prefix
    ],
)
def test_strip_bot_mention(sent: str, expected: str) -> None:
    assert strip_bot_mention(sent, "Scribe_Test_Bot") == expected


def test_strip_without_username_keeps_text() -> None:
    assert strip_bot_mention("@Scribe_Test_Bot hi", None) == "@Scribe_Test_Bot hi"


async def test_edited_text_of_strips_prefix_and_skips_get_me_when_not_needed() -> None:
    stub = _StubBot()
    bot = cast(Bot, stub)
    assert await edited_text_of(bot, "plain text") == "plain text"
    assert stub.calls == 0
    assert await edited_text_of(bot, "@Scribe_Test_Bot plain text") == "plain text"
    assert await edited_text_of(_bot(fail=True), "@Scribe_Test_Bot hi") == "@Scribe_Test_Bot hi"


async def test_inline_mode_detection(settings: Settings) -> None:
    assert await inline_mode_on(_bot(inline=True), settings) is True
    assert await inline_mode_on(_bot(inline=None), settings) is False
    assert await inline_mode_on(_bot(fail=True), settings) is False
    forced_copy = settings.model_copy(update={"edit_prefill": "copy"})
    assert await inline_mode_on(_bot(inline=True), forced_copy) is False
    forced_inline = settings.model_copy(update={"edit_prefill": "inline"})
    assert await inline_mode_on(_bot(fail=True), forced_inline) is True
