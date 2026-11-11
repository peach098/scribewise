"""Notice decoration (docs/plugins.md): a plugin's ``wrap_budget_notifier`` decorators change the
"drafting paused" notice and the "access ends soon" reminder and may add buttons; without one
both are sent as before. Offline Bot API, no database."""

from __future__ import annotations

import uuid
from datetime import timedelta

from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

from scribe.access.reconcile import _Reminder, _send_reminder
from scribe.config import Settings
from scribe.drafting.budget import MarkupFactory
from scribe.i18n import Msg, t
from scribe.plugins import PluginHost
from scribe.telegram.control_bot.render import button, keyboard
from tests.conftest import FROZEN_NOW
from tests.integration.bot_support import offline_bot

USER_ID = uuid.UUID(int=5)
CHAT_ID = 900000005


def _buttons(lang: str) -> InlineKeyboardMarkup:
    return keyboard([[button(f"More ({lang})", "example:more")]])


async def _hint(user_id: uuid.UUID, message: Msg) -> tuple[Msg, MarkupFactory | None]:
    return Msg("example.with_hint", base=message), _buttons


async def _no_buttons(user_id: uuid.UUID, message: Msg) -> tuple[Msg, MarkupFactory | None]:
    return message, None


def _reminder() -> _Reminder:
    return _Reminder(CHAT_ID, "en", "UTC", FROZEN_NOW + timedelta(days=2), USER_ID)


async def test_reminder_without_a_decorator_is_unchanged(settings: Settings) -> None:
    bot, api = offline_bot()
    await _send_reminder(bot, settings, _reminder())
    (sent,) = api.of_type(SendMessage)
    assert sent.text.startswith(t("access.sub_reminder", "en", date="16 Oct 2026", renew="x")[:10])
    assert sent.reply_markup is None


async def test_reminder_goes_through_the_decorator(settings: Settings) -> None:
    host = PluginHost(settings)
    host.add_messages("en", {"example.with_hint": "{base}\n\nA synthetic hint."})
    host.wrap_budget_notifier(_hint)
    decorate = host.hooks.notice_decorator()
    bot, api = offline_bot()

    await _send_reminder(bot, settings, _reminder(), decorate)

    (sent,) = api.of_type(SendMessage)
    assert sent.text.endswith("A synthetic hint.")
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    assert sent.reply_markup.inline_keyboard[0][0].text == "More (en)"


async def test_a_failing_decorator_skips_only_that_reminder(settings: Settings) -> None:
    async def broken(user_id: uuid.UUID, message: Msg) -> tuple[Msg, MarkupFactory | None]:
        raise RuntimeError("synthetic")

    bot, api = offline_bot()
    await _send_reminder(bot, settings, _reminder(), broken)  # logged, never raised
    assert api.of_type(SendMessage) == []


async def test_decorators_apply_in_order_and_keep_the_last_buttons(settings: Settings) -> None:
    host = PluginHost(settings)
    host.add_messages("en", {"example.with_hint": "{base} +hint"})
    host.wrap_budget_notifier(_hint)
    host.wrap_budget_notifier(_no_buttons)
    decorate = host.hooks.notice_decorator()
    assert decorate is not None

    message, markup = await decorate(USER_ID, Msg("budget.paused_notice", time="03:00"))

    assert message.render("en").endswith("+hint")
    assert markup is _buttons
