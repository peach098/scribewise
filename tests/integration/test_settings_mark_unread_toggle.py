"""specs/009 T025: the /settings switch for unread marks (contracts/control-chat.md), the setting
in the marking paths, and the new columns in /my_data and /delete_my_data. Synthetic data only.
"""

from __future__ import annotations

import pytest
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import User, UserState, WorkChat
from scribe.db.repos import users as users_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.retention.user_data import delete_user_data, export_user_data
from scribe.telegram.control_bot.render import format_callback
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import US2Harness, button_texts, callback_datas

TOGGLE = format_callback("settings", "mark_unread")
SEARCH_LINE = "🔎 Search earlier conversations: on"
SEARCH_BUTTON = "🔎 Search earlier: turn off"


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
    return US2Harness(
        session_factory, settings, llm_router, fake_llm, gateway, clock, with_unread_marker=True
    )


async def _stored(session_factory: async_sessionmaker[AsyncSession], user: User) -> User:
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None
        return stored


async def test_new_user_has_the_setting_on(make_user: MakeUser) -> None:
    user = await make_user()
    assert user.mark_unread is True


async def test_toggle_flips_the_setting_and_re_renders_in_place(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    assert TOGGLE == "settings:mark_unread"

    await harness.feed(text_update(chat_id, "/settings"))
    (menu,) = harness.messages()
    lines = menu.text.split("\n")
    assert "🔵 Mark chats unread after the agent: on" in lines
    assert lines.index("🔵 Mark chats unread after the agent: on") == lines.index(SEARCH_LINE) + 1
    assert TOGGLE in callback_datas(menu.reply_markup)
    buttons = button_texts(menu.reply_markup)
    assert buttons.index("🔵 Unread marks: turn off") == buttons.index(SEARCH_BUTTON) + 1

    await harness.feed(callback_update(chat_id, TOGGLE))
    assert (await _stored(session_factory, user)).mark_unread is False
    edit = harness.edits()[-1]
    lines = (edit.text or "").split("\n")
    assert "🔵 Mark chats unread after the agent: off" in lines
    assert SEARCH_LINE in lines  # the search-earlier line is unchanged
    assert "🔵 Unread marks: turn on" in button_texts(edit.reply_markup)
    assert SEARCH_BUTTON in button_texts(edit.reply_markup)
    answer = harness.api.of_type(AnswerCallbackQuery)[-1]
    assert answer.text is None  # no popup
    [event] = log_capture.events("settings_changed")
    assert event["fields"] == ["mark_unread"]

    await harness.feed(callback_update(chat_id, TOGGLE))
    assert (await _stored(session_factory, user)).mark_unread is True
    edit = harness.edits()[-1]
    assert "🔵 Mark chats unread after the agent: on" in (edit.text or "").split("\n")
    assert "🔵 Unread marks: turn off" in button_texts(edit.reply_markup)
    assert harness.gateway.mark_unread_calls == []  # the press itself marks nothing


async def test_toggle_without_a_message_sends_the_settings(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    await harness.feed(callback_update(user.telegram_control_chat_id, TOGGLE, with_message=False))
    assert (await _stored(session_factory, user)).mark_unread is False
    sent = harness.messages()[-1]
    assert "🔵 Mark chats unread after the agent: off" in sent.text
    assert "🔵 Unread marks: turn on" in button_texts(sent.reply_markup)


async def test_russian_texts(
    harness: US2Harness,
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active, language="ru")
    chat_id = user.telegram_control_chat_id
    await harness.feed(text_update(chat_id, "/settings"))
    menu = harness.messages()[-1]
    assert "🔵 Отмечать чаты непрочитанными после агента: вкл" in menu.text.split("\n")
    assert "🔵 Не отмечать непрочитанными" in button_texts(menu.reply_markup)

    await harness.feed(callback_update(chat_id, TOGGLE))
    edit = harness.edits()[-1]
    assert "🔵 Отмечать чаты непрочитанными после агента: выкл" in (edit.text or "").split("\n")
    assert "🔵 Отмечать непрочитанными" in button_texts(edit.reply_markup)


async def test_setting_off_stops_marks_and_on_never_backfills(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=0, delay_max_s=0)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    control = user.telegram_control_chat_id

    await harness.feed(callback_update(control, TOGGLE))  # off
    await harness.incoming(user, chat, contact, "Question?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    await harness.feed(callback_update(control, f"approve:{draft.id}"))
    assert len(gateway.mark_read_calls) == 1 and len(gateway.send_text_calls) == 1
    assert gateway.mark_unread_calls == []
    reasons = [e["reason"] for e in log_capture.events("unread_mark_skipped")]
    assert reasons == ["disabled", "disabled"]

    await harness.feed(callback_update(control, TOGGLE))  # on again: nothing is marked now
    await harness.tick()
    assert gateway.mark_unread_calls == []

    await harness.incoming(user, chat, contact, "Next question?")
    await harness.tick()
    assert gateway.mark_unread_calls == [(user.id, chat.telegram_chat_id)]


async def test_export_and_delete_cover_the_new_columns(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await make_user(mark_unread=False)
    chat = await make_chat(user)
    user_id = user.id
    chat.unread_mark_floor = 42
    await db_session.flush()

    async with session_factory() as fresh:
        export = await export_user_data(fresh, user_id, now=clock())
    assert export["users"][0]["mark_unread"] is False
    assert [row["unread_mark_floor"] for row in export["work_chats"]] == [42]

    async with session_factory() as fresh:
        await delete_user_data(fresh, user_id)
        await fresh.commit()
    async with session_factory() as fresh:
        assert await fresh.scalar(select(func.count()).where(User.id == user_id)) == 0
        assert await fresh.scalar(select(func.count()).where(WorkChat.user_id == user_id)) == 0
