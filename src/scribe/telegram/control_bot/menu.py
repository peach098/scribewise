"""The control bot's ``/`` command menu in the user's language (issue #8).

- :func:`set_bot_commands` (at start-up): English as the default list, Russian for Telegram
  clients set to Russian (``language_code="ru"``).
- :func:`set_chat_commands`: one control chat's menu in its chosen language
  (``BotCommandScopeChat``), after the manual switch in /settings or when the default for the
  user's Telegram language differs from the client's menu (``uk``, ``be``, ``kk`` -> Russian).
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat

from scribe.logging import get_logger
from scribe.telegram.control_bot.render import commands

log = get_logger("scribe.control_bot")


def bot_commands(lang: str) -> list[BotCommand]:
    return [BotCommand(command=c, description=d) for c, d in commands(lang)]


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(bot_commands("en"))
    await bot.set_my_commands(bot_commands("ru"), language_code="ru")


async def set_chat_commands(bot: Bot, chat_id: int, lang: str) -> None:
    """Best effort: a failure is logged; the default menus still work."""
    try:
        await bot.set_my_commands(bot_commands(lang), scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception as exc:
        log.warning("set_chat_commands_failed", error=type(exc).__name__)
