"""✏️ Edit without retyping (issue #7; contracts/control-chat.md "Draft card").

On ✏️ Edit the bot answers with a prompt that brings the draft text to the user:

- a draft of at most :data:`PREFILL_MAX_CHARS` characters (the Bot API limit of both button
  kinds) gets one button: **✏️ Edit in input field** (``switch_inline_query_current_chat``),
  which fills the input field with ``@<bot> <draft>``, when the bot has inline mode; otherwise
  **📋 Copy draft** (``copy_text``), which copies the draft to the clipboard;
- a longer draft is shown in a ``<code>`` block (one tap copies it) with ``ForceReply``, so the
  user's reply lands as an answer to that message.

``SCRIBE_EDIT_PREFILL`` picks the short-draft button: ``auto`` (default) asks ``getMe`` whether
inline mode is on, ``inline`` / ``copy`` force one. The edited text is whatever text the user
sends next while editing; :func:`strip_bot_mention` removes the ``@<bot>`` prefix the inline
button leaves, so a saved edit never contains it. Inline queries are answered with no results:
the button is only a way to fill the input field, and inline mode never shows anyone a draft.
"""

from __future__ import annotations

import html
import re

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CopyTextButton,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
)

from scribe.config import Settings
from scribe.i18n import t
from scribe.logging import get_logger
from scribe.telegram.control_bot.render import TELEGRAM_TEXT_MAX, keyboard

log = get_logger("scribe.control_bot")

PREFILL_MAX_CHARS = 256  # switch_inline_query_current_chat and copy_text.text limit


async def inline_mode_on(bot: Bot, settings: Settings) -> bool:
    """Whether the short-draft button is the inline one (``SCRIBE_EDIT_PREFILL``)."""
    if settings.edit_prefill != "auto":
        return settings.edit_prefill == "inline"
    try:
        return bool((await bot.me()).supports_inline_queries)
    except Exception as exc:  # network: the copy button works either way
        log.warning("get_me_failed", error=type(exc).__name__)
        return False


def _force_reply(lang: str) -> ForceReply:
    return ForceReply(force_reply=True, input_field_placeholder=t("edit.placeholder", lang))


def edit_prompt(
    draft_text: str, *, inline: bool, lang: str
) -> tuple[str, InlineKeyboardMarkup | ForceReply, str | None]:
    """The prompt's text, markup and parse mode for a draft of this text, in ``lang``."""
    force_reply = _force_reply(lang)
    if not draft_text.strip():
        return t("edit.prompt", lang), force_reply, None
    if len(draft_text) <= PREFILL_MAX_CHARS:
        if inline:
            fill = InlineKeyboardButton(
                text=t("btn.edit_inline", lang), switch_inline_query_current_chat=draft_text
            )
            return t("edit.inline_prompt", lang), keyboard([[fill]]), None
        copy = InlineKeyboardButton(
            text=t("btn.copy_draft", lang), copy_text=CopyTextButton(text=draft_text)
        )
        return t("edit.copy_prompt", lang), keyboard([[copy]]), None
    long_prompt = t("edit.long_prompt", lang)
    if len(long_prompt) + 2 + len(draft_text) > TELEGRAM_TEXT_MAX:
        return t("edit.prompt", lang), force_reply, None  # a cut copy would be worse than none
    text = f"{long_prompt}\n\n<code>{html.escape(draft_text, quote=False)}</code>"
    return text, force_reply, "HTML"


async def send_edit_prompt(
    bot: Bot,
    settings: Settings,
    chat_id: int,
    draft_text: str,
    *,
    reply_to_message_id: int | None,
    lang: str,
) -> None:
    inline = await inline_mode_on(bot, settings)
    text, markup, parse_mode = edit_prompt(draft_text, inline=inline, lang=lang)
    try:
        await bot.send_message(
            chat_id,
            text,
            reply_markup=markup,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
        )
    except TelegramBadRequest as exc:  # e.g. inline button while inline mode is off
        log.warning("edit_prompt_failed", error=type(exc).__name__)
        await bot.send_message(
            chat_id,
            t("edit.prompt", lang),
            reply_markup=_force_reply(lang),
            reply_to_message_id=reply_to_message_id,
        )


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    """``@<bot> <text>`` (what the inline button leaves in the input field) -> ``<text>``."""
    if not bot_username:
        return text
    match = re.match(rf"@{re.escape(bot_username)}(?![A-Za-z0-9_])[ \t ]*\n?", text, re.I)
    return text[match.end() :] if match else text


async def edited_text_of(bot: Bot, text: str) -> str:
    """The user's edited text without the inline button's ``@<bot>`` prefix."""
    if not text.startswith("@"):
        return text
    try:
        username = (await bot.me()).username
    except Exception as exc:
        log.warning("get_me_failed", error=type(exc).__name__)
        return text
    return strip_bot_mention(text, username)


async def on_inline_query(inline_query: InlineQuery) -> None:
    """No results: the inline button only fills the input field; the user sends the text."""
    await inline_query.answer([], cache_time=0, is_personal=True)


def create_router() -> Router:
    router = Router(name="edit_prefill")
    router.inline_query.register(on_inline_query)
    return router
