"""US2 /settings and /chats (T045): validated delay range, hours, days and time zone; changed
working time re-plans waiting turns; per-chat delay override; callbacks are owner-only."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import EditMessageReplyMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import User, UserState, WorkChat
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
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
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    return US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _user(session_factory: async_sessionmaker[AsyncSession], user: User) -> User:
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None
        return stored


async def _chat(session_factory: async_sessionmaker[AsyncSession], chat: WorkChat) -> WorkChat:
    async with session_factory() as session:
        stored = await chats_repo.get_work_chat(session, chat.user_id, chat.id)
        assert stored is not None
        return stored


async def test_settings_menu_validates_and_saves(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=120, delay_max_s=600)
    chat_id = user.telegram_control_chat_id

    await harness.feed(text_update(chat_id, "/settings"))
    (menu,) = harness.messages()
    assert "⏱ Reading delay: 2 min–10 min" in menu.text
    assert "🌍 Time zone: UTC" in menu.text
    assert callback_datas(menu.reply_markup) == [
        "settings:delay",
        "settings:hours",
        "settings:days",
        "settings:tz",
        "settings:lang",
        "settings:search_earlier",
        "settings:mark_unread",  # specs/009
        "settings:work_account",  # specs/008
        "settings:gender",  # issue #46
        "settings:auto_send_window",  # specs/014
    ]
    assert "🌐 Language: English" in menu.text

    # Delay: an invalid range is refused and the question stays open; then a fixed delay.
    await harness.feed(callback_update(chat_id, "settings:delay"))
    await harness.feed(text_update(chat_id, "10-2 min"))
    assert "minimum must not be larger" in harness.bot_texts()[-1]
    await harness.feed(text_update(chat_id, "5 min"))
    stored = await _user(session_factory, user)
    assert (stored.delay_min_s, stored.delay_max_s) == (300, 300)
    assert "5 min (fixed)" in harness.bot_texts()[-1]

    # Time zone and hours.
    await harness.feed(callback_update(chat_id, "settings:tz"))
    await harness.feed(text_update(chat_id, "Mars/Olympus"))
    assert "don't know the time zone" in harness.bot_texts()[-1]
    await harness.feed(text_update(chat_id, "Asia/Almaty"))
    await harness.feed(callback_update(chat_id, "settings:hours"))
    await harness.feed(text_update(chat_id, "18:00-10:00"))
    await harness.feed(text_update(chat_id, "08:30-17:00"))
    stored = await _user(session_factory, user)
    assert stored.timezone == "Asia/Almaty"
    assert (stored.work_start, stored.work_end) == (time(8, 30), time(17, 0))

    # /cancel keeps the value.
    await harness.feed(callback_update(chat_id, "settings:delay"))
    await harness.feed(text_update(chat_id, "/cancel"))
    await harness.feed(text_update(chat_id, "1-2 h"))  # no longer an answer to the question
    stored = await _user(session_factory, user)
    assert (stored.delay_min_s, stored.delay_max_s) == (300, 300)

    # Days: toggled in place; the last working day cannot be removed.
    await harness.feed(callback_update(chat_id, "settings:days"))
    for day in (0, 1, 2, 3):
        await harness.feed(callback_update(chat_id, f"setday:{day}"))
    assert len(harness.api.of_type(EditMessageReplyMarkup)) == 4
    await harness.feed(callback_update(chat_id, "setday:4"))  # refused: Friday is the last one
    stored = await _user(session_factory, user)
    assert stored.working_days == [4]


async def test_time_zone_accepts_human_input(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    """Issue #5: aliases, offsets (Etc/GMT sign inverted), choices for ambiguous input, quick
    buttons by Telegram language, and a confirmation with the local time."""
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    local = clock().astimezone(ZoneInfo("Europe/Moscow"))

    await harness.feed(callback_update(chat_id, "settings:tz", language_code="ru"))
    prompt = harness.messages()[-1]
    assert "Send your city" in prompt.text
    assert callback_datas(prompt.reply_markup)[0] == "settz:Europe/Moscow"
    await harness.feed(text_update(chat_id, "МСК"))
    stored = await _user(session_factory, user)
    assert stored.timezone == "Europe/Moscow"
    assert harness.bot_texts()[-1].startswith(
        f"✅ Time zone set to Europe/Moscow (UTC+3, now {local:%H:%M} there)."
    )

    # A fixed offset: Etc/GMT-3 is UTC+3; the reply suggests a city (DST).
    await harness.feed(callback_update(chat_id, "settings:tz"))
    await harness.feed(text_update(chat_id, "UTC+3"))
    stored = await _user(session_factory, user)
    assert stored.timezone == "Etc/GMT-3"
    reply = harness.bot_texts()[-1]
    assert "💡" in reply and "🌍 Time zone: Etc/GMT-3 (UTC+3)" in reply

    # Ambiguous: choices as buttons; the question stays open until one is chosen.
    await harness.feed(callback_update(chat_id, "settings:tz"))
    await harness.feed(text_update(chat_id, "west"))
    choices = harness.messages()[-1]
    assert "Several time zones match 'west'" in choices.text
    assert set(callback_datas(choices.reply_markup)) == {
        "settz:Australia/West",
        "settz:Brazil/West",
    }
    await harness.feed(callback_update(chat_id, "settz:Mars/Olympus"))  # not a zone: ignored
    assert (await _user(session_factory, user)).timezone == "Etc/GMT-3"
    await harness.feed(callback_update(chat_id, "settz:Australia/West"))
    assert (await _user(session_factory, user)).timezone == "Australia/West"
    await harness.feed(text_update(chat_id, "moscow"))  # the question was answered
    assert (await _user(session_factory, user)).timezone == "Australia/West"

    # Unknown input shows examples and keeps the question open.
    await harness.feed(callback_update(chat_id, "settings:tz"))
    await harness.feed(text_update(chat_id, "atlantis"))
    assert "Send a city (Moscow, Berlin, New York)" in harness.bot_texts()[-1]
    await harness.feed(text_update(chat_id, "europe/moscow"))
    assert (await _user(session_factory, user)).timezone == "Europe/Moscow"


async def test_changed_working_hours_replan_waiting_turns(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """quickstart US2 step 6: hours exclude now -> unread; hours back -> read within a tick."""
    user = await make_user(state=UserState.active, work_start=time(14), work_end=time(19))
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    clock.set(datetime(2026, 10, 14, 10, 0, tzinfo=UTC))
    await harness.incoming(user, chat, contact, "Synthetic question")
    (turn,) = await harness.turns(user)
    assert turn.due_at == datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
    await harness.at(clock() + timedelta(minutes=5))
    assert gateway.mark_read_calls == []

    await harness.feed(callback_update(user.telegram_control_chat_id, "settings:hours"))
    await harness.feed(text_update(user.telegram_control_chat_id, "09:00-19:00"))
    (turn,) = await harness.turns(user)
    assert turn.due_at == clock()
    await harness.tick()
    assert len(gateway.mark_read_calls) == 1


async def test_per_chat_delay_override(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, delay_min_s=0, delay_max_s=0)
    contact = await make_contact(user)
    chat = await make_chat(user, title="Synthetic ops", telegram_chat_id=contact.telegram_peer_id)
    other_user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id

    await harness.feed(text_update(chat_id, "/chats"))
    # specs/007: ➕ Add work chats under the list.
    assert callback_datas(harness.messages()[-1].reply_markup) == [
        f"chatcfg:{chat.id}",
        "chatadd:0",
    ]
    await harness.feed(callback_update(chat_id, f"chatcfg:{chat.id}"))
    card = harness.messages()[-1]
    assert "Reading delay: global (0 s (fixed))" in card.text
    # US4 (T067) adds language / formality / enable buttons after the delay row.
    assert callback_datas(card.reply_markup)[0] == f"chatdelay:{chat.id}"
    assert f"chatdelay:{chat.id}:clear" not in callback_datas(card.reply_markup)

    # Another user's callback for this chat does nothing.
    await harness.feed(callback_update(other_user.telegram_control_chat_id, f"chatdelay:{chat.id}"))
    await harness.feed(text_update(other_user.telegram_control_chat_id, "5-15 min"))
    assert (await _chat(session_factory, chat)).override_delay_min_s is None

    await harness.feed(callback_update(chat_id, f"chatdelay:{chat.id}"))
    await harness.feed(text_update(chat_id, "5-15 min"))
    stored = await _chat(session_factory, chat)
    assert (stored.override_delay_min_s, stored.override_delay_max_s) == (300, 900)

    await harness.incoming(user, chat, contact, "Synthetic question")
    (turn,) = await harness.turns(user)
    assert 300 <= turn.delay_s <= 900  # the chat's range, not the global 0-0

    await harness.feed(callback_update(chat_id, f"chatdelay:{chat.id}:clear"))
    stored = await _chat(session_factory, chat)
    assert stored.override_delay_min_s is None and stored.override_delay_max_s is None


async def test_commands_while_editing_a_draft_are_never_sent(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Synthetic question")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    control = user.telegram_control_chat_id

    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    await harness.feed(text_update(control, "/waiting"))
    assert "Nothing is waiting" in harness.bot_texts()[-1]
    await harness.feed(text_update(control, "/profile"))  # answers; never the reply text
    assert gateway.send_text_calls == []
    assert "No style profile yet" in harness.bot_texts()[-1]

    await harness.feed(text_update(control, "My own version"))
    (sent,) = gateway.send_text_calls
    assert sent.text == "My own version"
