"""specs/004 T026: the /settings toggle for automatic search over earlier conversations
(contracts/control-chat.md "Settings"; research S9)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import User, UserState
from scribe.db.repos import users as users_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot.render import format_callback
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, LogCapture, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import US2Harness, button_texts, callback_datas

TOGGLE = format_callback("settings", "search_earlier")


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
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    return US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _stored(session_factory: async_sessionmaker[AsyncSession], user: User) -> User:
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None
        return stored


async def test_toggle_flips_the_setting_and_re_renders(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    assert TOGGLE == "settings:search_earlier"

    await harness.feed(text_update(chat_id, "/settings"))
    (menu,) = harness.messages()
    assert "🔎 Search earlier conversations: on" in menu.text.split("\n")
    assert TOGGLE in callback_datas(menu.reply_markup)
    assert "🔎 Search earlier: turn off" in button_texts(menu.reply_markup)

    await harness.feed(callback_update(chat_id, TOGGLE))
    assert (await _stored(session_factory, user)).search_earlier_messages is False
    edit = harness.edits()[-1]
    assert "🔎 Search earlier conversations: off" in (edit.text or "").split("\n")
    assert "🔎 Search earlier: turn on" in button_texts(edit.reply_markup)
    assert TOGGLE in callback_datas(edit.reply_markup)
    [event] = log_capture.events("settings_changed")
    assert event["fields"] == ["search_earlier_messages"]

    # Each press flips the value.
    await harness.feed(callback_update(chat_id, TOGGLE))
    assert (await _stored(session_factory, user)).search_earlier_messages is True
    edit = harness.edits()[-1]
    assert "🔎 Search earlier conversations: on" in (edit.text or "").split("\n")
    assert "🔎 Search earlier: turn off" in button_texts(edit.reply_markup)
    assert len(log_capture.events("settings_changed")) == 2


async def test_toggle_without_a_message_sends_the_settings(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await harness.feed(callback_update(chat_id, TOGGLE, with_message=False))
    assert (await _stored(session_factory, user)).search_earlier_messages is False
    sent = harness.messages()[-1]
    assert "🔎 Search earlier conversations: off" in sent.text
    assert "🔎 Search earlier: turn on" in button_texts(sent.reply_markup)


async def test_russian_texts(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active, language="ru")
    chat_id = user.telegram_control_chat_id

    await harness.feed(text_update(chat_id, "/settings"))
    menu = harness.messages()[-1]
    assert "🔎 Поиск по прошлой переписке: вкл" in menu.text.split("\n")
    assert "🔎 Выключить поиск" in button_texts(menu.reply_markup)

    await harness.feed(callback_update(chat_id, TOGGLE))
    edit = harness.edits()[-1]
    assert "🔎 Поиск по прошлой переписке: выкл" in (edit.text or "").split("\n")
    assert "🔎 Включить поиск" in button_texts(edit.reply_markup)
