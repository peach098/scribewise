"""Issue #8: the control bot in Russian. The default comes from the Telegram language at /start,
/settings → 🌐 Language switches at once and persists, background notices use the stored
language, and the / menu follows. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeChat
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import User, UserState
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot.menu import set_bot_commands
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser, next_telegram_id
from tests.integration.bot_support import callback_update, offline_bot, text_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import US2Harness, callback_datas


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply("Синтетический ответ"))
    return US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _stored(session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> User:
    async with session_factory() as session:
        user = await users_repo.get_by_control_chat(session, chat_id)
    assert user is not None
    return user


def _button_texts(markup: object) -> list[str]:
    rows = getattr(markup, "inline_keyboard", [])
    return [button.text for row in rows for button in row]


# --- /start -------------------------------------------------------------------------------------


async def test_russian_telegram_user_gets_the_russian_bot_from_start(
    harness: US2Harness, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    chat_id = next_telegram_id()

    await harness.feed(text_update(chat_id, "/start", language_code="ru"))

    user = await _stored(session_factory, chat_id)
    assert user.language == "ru"
    welcome, choice = harness.messages()
    assert welcome.text == t("start.welcome", "ru")
    assert welcome.text.startswith("👋 Привет! Я Scribe.")
    assert choice.text == t("connect.method_choice", "ru")
    assert _button_texts(choice.reply_markup) == [
        "📷 Сканировать QR-код (рекомендуется)",
        "📱 Номер телефона и код",
    ]
    # Russian clients already get the Russian default menu: no per-chat menu needed.
    assert harness.api.of_type(SetMyCommands) == []

    harness.api.clear()
    await harness.feed(text_update(chat_id, "/help", language_code="ru"))
    (help_text,) = harness.bot_texts()
    assert help_text.startswith(t("help.intro", "ru") + "\n\n/start — ")
    assert "/settings — Задержка чтения, рабочие часы и дни, часовой пояс, язык" in help_text


@pytest.mark.parametrize("code", ["uk", "be", "kk-KZ"])
async def test_ukrainian_belarusian_kazakh_default_to_russian_with_a_chat_menu(
    harness: US2Harness, session_factory: async_sessionmaker[AsyncSession], code: str
) -> None:
    chat_id = next_telegram_id()

    await harness.feed(text_update(chat_id, "/start", language_code=code))

    assert (await _stored(session_factory, chat_id)).language == "ru"
    assert harness.bot_texts()[0] == t("start.welcome", "ru")
    (menu,) = harness.api.of_type(SetMyCommands)
    assert menu.scope == BotCommandScopeChat(chat_id=chat_id)
    assert {c.command: c.description for c in menu.commands}["help"] == "Этот список"


@pytest.mark.parametrize("code", [None, "en", "de"])
async def test_other_languages_get_english(
    harness: US2Harness, session_factory: async_sessionmaker[AsyncSession], code: str | None
) -> None:
    chat_id = next_telegram_id()

    await harness.feed(text_update(chat_id, "/start", language_code=code))

    assert (await _stored(session_factory, chat_id)).language == "en"
    assert harness.bot_texts() == [t("start.welcome", "en"), t("connect.method_choice", "en")]
    assert harness.api.of_type(SetMyCommands) == []


async def test_stored_language_wins_over_the_telegram_language(
    harness: US2Harness, make_user: MakeUser
) -> None:
    user = await make_user(state=UserState.active, language="en")

    await harness.feed(text_update(user.telegram_control_chat_id, "/start", language_code="ru"))

    assert harness.bot_texts() == [t("start.already_set_up", "en")]


async def test_someone_without_an_account_is_answered_in_their_language(
    harness: US2Harness,
) -> None:
    await harness.feed(text_update(next_telegram_id(), "/settings", language_code="ru"))
    assert harness.bot_texts() == ["Сначала отправьте /start."]


# --- /settings → 🌐 Language --------------------------------------------------------------------


async def test_language_switch_applies_at_once_and_persists(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=120, delay_max_s=600)
    chat_id = user.telegram_control_chat_id

    await harness.feed(text_update(chat_id, "/settings"))
    (menu,) = harness.messages()
    assert "🌐 Language: English" in menu.text
    assert "settings:lang" in callback_datas(menu.reply_markup)

    harness.api.clear()
    await harness.feed(callback_update(chat_id, "settings:lang"))
    (prompt,) = harness.messages()
    assert prompt.text == t("settings.language_prompt", "en")
    assert callback_datas(prompt.reply_markup) == ["setlang:en", "setlang:ru"]
    assert _button_texts(prompt.reply_markup) == ["✅ English", "Русский"]

    harness.api.clear()
    await harness.feed(callback_update(chat_id, "setlang:ru"))

    assert (await _stored(session_factory, chat_id)).language == "ru"
    (saved,) = harness.messages()
    assert saved.text.startswith("✅ Сохранено.\n\n⚙️ Настройки\n")
    assert "⏱ Задержка чтения: 2 мин–10 мин, случайно для каждого сообщения" in saved.text
    assert "📅 Рабочие дни: Пн, Вт, Ср, Чт, Пт" in saved.text
    assert "🌐 Язык: Русский" in saved.text
    assert _button_texts(saved.reply_markup)[-6:] == [
        "🌐 Язык",
        "🔎 Выключить поиск",
        "🔵 Не отмечать непрочитанными",
        "💼 Включить рабочий аккаунт",  # specs/008
        "👤 Род в черновиках",  # issue #46
        "⏱ Окно отмены",  # specs/014
    ]
    assert "👤 Род в черновиках: не задан (как в ваших сообщениях)" in saved.text
    (menu_update,) = harness.api.of_type(SetMyCommands)
    assert menu_update.scope == BotCommandScopeChat(chat_id=chat_id)
    descriptions = {c.command: c.description for c in menu_update.commands}
    assert descriptions["settings"] == "Задержка чтения, рабочие часы и дни, часовой пояс, язык"

    # Every later message is Russian, also replies to commands and button alerts.
    harness.api.clear()
    await harness.feed(text_update(chat_id, "/waiting"))
    assert harness.bot_texts() == ["✅ Ничего не ждёт прочтения."]
    await harness.feed(text_update(chat_id, "/pause"))
    assert harness.bot_texts()[-1] == t("settings.paused", "ru")
    await harness.feed(text_update(chat_id, "/resume"))
    assert harness.bot_texts()[-1].startswith("▶️ Продолжаем. Ждут прочтения: 0;")

    # And back.
    harness.api.clear()
    await harness.feed(callback_update(chat_id, "setlang:en"))
    assert (await _stored(session_factory, chat_id)).language == "en"
    assert harness.bot_texts()[-1].startswith("✅ Saved.\n\n⚙️ Settings\n")


async def test_unknown_language_callback_changes_nothing(
    harness: US2Harness, session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser
) -> None:
    user = await make_user(state=UserState.active)
    await harness.feed(callback_update(user.telegram_control_chat_id, "setlang:de"))
    assert (await _stored(session_factory, user.telegram_control_chat_id)).language == "en"
    assert harness.bot_texts() == []


async def test_settings_inputs_and_errors_in_russian(
    harness: US2Harness, session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser
) -> None:
    user = await make_user(state=UserState.active, language="ru")
    chat_id = user.telegram_control_chat_id

    await harness.feed(callback_update(chat_id, "settings:delay"))
    assert harness.bot_texts()[-1] == t("settings.delay_prompt", "ru")
    await harness.feed(text_update(chat_id, "10-2 мин"))
    assert harness.bot_texts()[-1] == (
        "⚠️ Минимум не может быть больше максимума. Попробуйте ещё раз или отправьте /cancel."
    )
    await harness.feed(text_update(chat_id, "1-3 мин"))
    assert harness.bot_texts()[-1].startswith("✅ Сохранено.")
    stored = await _stored(session_factory, chat_id)
    assert (stored.delay_min_s, stored.delay_max_s) == (60, 180)

    await harness.feed(callback_update(chat_id, "settings:tz", language_code="ru"))
    tz_prompt = harness.messages()[-1]
    assert tz_prompt.text == t("settings.tz_prompt", "ru")
    assert _button_texts(tz_prompt.reply_markup)[0] == "Москва (UTC+3)"
    await harness.feed(text_update(chat_id, "Атлантида"))
    assert harness.bot_texts()[-1].startswith("⚠️ Не знаю часовой пояс «Атлантида».")
    await harness.feed(text_update(chat_id, "Москва"))
    assert harness.bot_texts()[-1].startswith("✅ Часовой пояс: Europe/Moscow (UTC+3, там сейчас")


# --- draft cards and background notices in the stored language ----------------------------------


async def test_draft_card_reminder_and_expiry_in_russian(
    harness: US2Harness,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, language="ru", timezone="Europe/Moscow")
    contact = await make_contact(user, display_name="Синтетическая Анна")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Как дела с миграцией?")
    await harness.tick()

    (draft,) = await harness.drafts(user)
    card = harness.messages()[-1]
    lines = card.text.split("\n")
    assert lines[0].startswith("💬 Синтетическая Анна   ·   личный чат   ·   прочитано в ")
    assert any(line.startswith("✍️ Черновик (") and "сдвиг тона: нет)" in line for line in lines)
    assert lines[-1].startswith("⏳ Истекает завтра в ")
    assert _button_texts(card.reply_markup) == [
        "✅ Отправить",
        "✏️ Изменить",
        "🔁 Заново",
        "❌ Отклонить",
        "🧾 Источники",
    ]

    prepared = draft.prepared_at
    await harness.at(prepared + timedelta(hours=2, minutes=1))
    assert "⏰ Этот черновик всё ещё ждёт вашего решения." in harness.bot_texts()

    await harness.at(prepared + timedelta(hours=24, minutes=1))
    (expired,) = [e.text for e in harness.edits() if e.text and "⌛" in e.text]
    assert expired.endswith("\n\n⌛ Истёк — ответьте вручную")


async def test_manual_reply_notice_in_russian(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user = await make_user(state=UserState.active, language="ru")
    contact = await make_contact(user, display_name="Синтетическая Анна")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Привет!")
    await harness.manual_reply(user, chat)

    assert harness.notifications[-1] == (
        user.id,
        "✅ Вы ответили сами (Синтетическая Анна) — ожидающее сообщение или черновик сняты.",
    )


# --- the default / menu -------------------------------------------------------------------------


async def test_default_menu_is_english_with_a_russian_variant() -> None:
    bot, api = offline_bot()

    await set_bot_commands(bot)

    default, russian = api.of_type(SetMyCommands)
    assert default.language_code is None and russian.language_code == "ru"
    assert [c.command for c in default.commands] == [c.command for c in russian.commands]
    assert {c.command: c.description for c in default.commands}["help"] == "This list"
    assert {c.command: c.description for c in russian.commands}["help"] == "Этот список"
