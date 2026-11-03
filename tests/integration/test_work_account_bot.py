"""Work account mode in the control chat (specs/008 T018, T027, T032, T037, T038, T039): the setup
picker's 💼 switch, /settings with its confirmations, the chat screen's Exclude / Include
again, /chats (header, marks, paging, the exclusion picker), ➕ with the mode on, /history and
/my_data. Fake LLM, fake gateway, offline Bot API; synthetic data only."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ChatKind,
    DraftState,
    LoginMethod,
    User,
    UserState,
    WorkChat,
    WorkChatOrigin,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.retention.user_data import delete_user_data, export_user_data
from scribe.telegram.control_bot import setup_flow
from scribe.telegram.control_bot.exclusions import ChatsStates
from scribe.telegram.control_bot.setup_flow import SetupStates
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import Dialog, IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, next_message_id
from tests.integration.us2_support import US2Harness, button_texts, callback_datas
from tests.integration.us4_support import make_harness

A, B, C = 900000101, 900000102, 900000103
TEAM = -100900000201


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


# --- helpers ------------------------------------------------------------------------------------


async def _active(
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    *,
    on: bool = False,
    **values: Any,
) -> User:
    user = await make_user(state=UserState.active, **values)
    async with session_factory() as session:
        await users_repo.upsert_telegram_session(
            session, user.id, "synthetic-session", method=LoginMethod.qr, connected_at=clock()
        )
        await session.commit()
    if on:
        await work_account.switch(session_factory, user.id, on=True, now=clock())
    return user


async def _press(harness: US2Harness, user: User, data: str, message_id: int = 1) -> None:
    await harness.feed(callback_update(user.telegram_control_chat_id, data, message_id=message_id))


async def _say(harness: US2Harness, user: User, text: str) -> None:
    await harness.feed(text_update(user.telegram_control_chat_id, text))


async def _fsm(harness: US2Harness, user: User) -> tuple[str | None, dict[str, Any]]:
    chat = user.telegram_control_chat_id
    context = harness.dispatcher.fsm.get_context(harness.bot, chat, chat)
    return await context.get_state(), await context.get_data()


async def _chats(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> dict[int, WorkChat]:
    async with session_factory() as session:
        return {c.telegram_chat_id: c for c in await chats_repo.list_work_chats(session, user.id)}


async def _fresh(session_factory: async_sessionmaker[AsyncSession], user: User) -> User:
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None
        await session.refresh(stored)
        return stored


async def _write(
    harness: US2Harness, user: User, chat_id: int, name: str = "Test Contact A"
) -> None:
    await harness.intake.route(
        IncomingMessage(
            user_id=user.id,
            chat_id=chat_id,
            message_id=next_message_id(),
            sender_peer_id=chat_id,
            sender_display_name=name,
            text="Synthetic question?",
            has_media=False,
            sent_at=harness.clock(),
            peer_kind="user",
        )
    )


def _alerts(harness: US2Harness) -> list[str | None]:
    return [a.text for a in harness.api.of_type(AnswerCallbackQuery)]


def _last_text(harness: US2Harness) -> str:
    return harness.messages()[-1].text


async def _auto(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    chat_id: int,
    title: str,
    *,
    enabled: bool = True,
    excluded: bool = False,
) -> WorkChat:
    async with session_factory() as session:
        chat, _ = await chats_repo.create_auto_chat(
            session,
            user.id,
            telegram_chat_id=chat_id,
            title=title,
            kind=ChatKind.private,
            enabled=enabled,
            excluded=excluded,
        )
        await session.commit()
    return chat


# --- US1: the setup picker ------------------------------------------------------------------------


async def _open_setup(harness: US2Harness, user: User, gateway: FakeMessagingGateway) -> None:
    gateway.dialogs[user.id] = [
        Dialog(chat_id=A, title="Test Contact A", kind="private"),
        Dialog(chat_id=B, title="Test Contact B", kind="private"),
        Dialog(chat_id=TEAM, title="Team (synthetic)", kind="group"),
    ]
    chat = user.telegram_control_chat_id
    context = harness.dispatcher.fsm.get_context(harness.bot, chat, chat)
    await setup_flow.show_chat_picker(harness.bot, chat, user, context, gateway)


async def test_setup_with_the_mode_on_and_nothing_ticked(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await make_user()  # setting_up
    await _open_setup(harness, user, gateway)
    picker = harness.messages()[-1]
    assert picker.text == t("setup.pick_prompt")
    assert callback_datas(picker.reply_markup)[0] == "pickwa:on"

    await _press(harness, user, "pickwa:on")
    switched = harness.api.of_type(EditMessageText)[-1]
    assert switched.text == t("setup.pick_prompt_work_account")
    assert callback_datas(switched.reply_markup)[0] == "pickwa:off"
    await _press(harness, user, f"pickchat:{B}")  # private: exclusion while the mode is on
    marked = harness.api.of_type(EditMessageReplyMarkup)[-1]
    assert button_texts(marked.reply_markup)[1:4] == [
        "✅ Test Contact A",
        "🚫 Test Contact B",
        "⬜ Team (synthetic)",
    ]
    await _press(harness, user, f"pickchat:{TEAM}")
    await _press(harness, user, "pickchat:123")  # not in the snapshot: ignored
    state, data = await _fsm(harness, user)
    assert (data["excluded"], data["selected"]) == ([B], [TEAM])

    await _press(harness, user, "pickdone:0")
    assert (await _fsm(harness, user))[0] == SetupStates.timezone.state
    saved = [e.text for e in harness.api.of_type(EditMessageText)]
    assert t("setup.chats_saved_work_account", excluded=1, groups=1) in saved
    stored = await _fresh(session_factory, user)
    assert stored.work_account and stored.work_account_changed_at == clock()
    chats = await _chats(session_factory, user)
    assert set(chats) == {B, TEAM}  # A gets a row only when it writes
    assert (chats[B].origin, chats[B].excluded, chats[B].enabled) == (
        WorkChatOrigin.auto,
        True,
        False,
    )
    assert (chats[TEAM].origin, chats[TEAM].enabled) == (WorkChatOrigin.picked, True)


async def test_setup_switch_off_again_and_nothing_ticked_still_refused(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await make_user(language="ru")
    await _open_setup(harness, user, gateway)
    await _press(harness, user, "pickwa:on")
    await _press(harness, user, f"pickchat:{A}")
    await _press(harness, user, "pickwa:off")
    _, data = await _fsm(harness, user)
    assert data["work_account"] is False and data["excluded"] == []
    await _press(harness, user, "pickdone:0")
    assert _alerts(harness)[-1] == t("setup.pick_none", "ru")
    assert (await _fsm(harness, user))[0] == SetupStates.pick_chats.state
    assert not (await _fresh(session_factory, user)).work_account


# --- US3: /settings -------------------------------------------------------------------------------


async def test_settings_turn_on_and_off_with_confirmations(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await _active(make_user, session_factory, clock)
    await _say(harness, user, "/settings")
    shown = harness.messages()[-1]
    assert t("settings.work_account.off") in shown.text
    assert callback_datas(shown.reply_markup)[-2:] == ["settings:work_account", "settings:gender"]

    await _press(harness, user, "settings:work_account")
    confirm = harness.messages()[-1]
    assert confirm.text == t("settings.work_account_confirm_on", n=50)
    assert callback_datas(confirm.reply_markup) == ["wacct:on", "wacct:cancel"]
    await _press(harness, user, "wacct:cancel")
    assert _alerts(harness)[-1] == t("settings.work_account_cancelled")
    assert not (await _fresh(session_factory, user)).work_account

    await _press(harness, user, "wacct:on")
    assert (await _fresh(session_factory, user)).work_account
    texts = harness.bot_texts()
    assert t("settings.work_account_now_on") in texts
    assert t("settings.work_account.on", n=0) in texts[-1]
    await _press(harness, user, "wacct:on")  # an old confirmation: nothing changes
    assert _alerts(harness)[-1] == t("settings.work_account_cancelled")

    await _write(harness, user, A)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    await _press(harness, user, "settings:work_account")
    assert _last_text(harness) == t(
        "settings.work_account_confirm_off", chats=1, drafts=1, waiting=0
    )
    harness.api.clear()
    await _press(harness, user, "wacct:off")
    assert not (await _fresh(session_factory, user)).work_account
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.dropped
    assert any(t("status.work_account_off") in (e.text or "") for e in harness.edits())
    assert t("settings.work_account_now_off", n=1) in harness.bot_texts()
    async with session_factory() as session:
        kinds = [r.kind for r, _ in await _history_rows(session, user)]
    assert kinds.count(ActivityKind.work_account_on) == 1
    assert kinds.count(ActivityKind.work_account_off) == 1


async def _history_rows(session: AsyncSession, user: User) -> list[Any]:
    from scribe.telegram.control_bot.settings import _recent_activity

    return await _recent_activity(session, user.id, 50)


async def test_settings_line_counts_exclusions_in_russian(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _active(make_user, session_factory, clock, on=True, language="ru")
    await _auto(session_factory, user, A, "Test Contact A", enabled=False, excluded=True)
    await make_chat(user, telegram_chat_id=B, enabled=False)  # disabled picked: counts too
    await make_chat(user, telegram_chat_id=TEAM, kind=ChatKind.group, enabled=False)  # not
    await _say(harness, user, "/settings")
    assert t("settings.work_account.on", "ru", n=2) in _last_text(harness)


# --- US2: the chat screen -------------------------------------------------------------------------


async def test_chat_screen_exclude_and_include_again(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _active(make_user, session_factory, clock, on=True)
    team = await make_chat(user, telegram_chat_id=TEAM, title="Team", kind=ChatKind.group)
    await _write(harness, user, A)
    await harness.tick()
    chat = (await _chats(session_factory, user))[A]

    await _press(harness, user, f"chatcfg:{chat.id}")
    screen = harness.messages()[-1]
    assert t("settings.chat_origin.auto") in screen.text
    assert t("btn.chat_exclude") in button_texts(screen.reply_markup)
    assert f"chatenable:{chat.id}:off" in callback_datas(screen.reply_markup)

    harness.api.clear()
    await _press(harness, user, f"chatenable:{chat.id}:off")
    stored = (await _chats(session_factory, user))[A]
    assert (stored.excluded, stored.enabled) == (True, False)
    assert any(t("status.chat_excluded") in (e.text or "") for e in harness.edits())
    screen = harness.messages()[-1]
    assert t("settings.chat_origin.excluded") in screen.text
    assert t("btn.chat_include") in button_texts(screen.reply_markup)

    await _press(harness, user, f"chatenable:{chat.id}:on")
    stored = (await _chats(session_factory, user))[A]
    assert (stored.excluded, stored.enabled) == (False, True)

    await _press(harness, user, f"chatcfg:{team.id}")  # a group: unchanged screen
    screen = harness.messages()[-1]
    assert t("btn.chat_disable") in button_texts(screen.reply_markup)
    assert t("settings.chat_enabled") in screen.text


# --- US4: /chats ----------------------------------------------------------------------------------


async def test_chats_header_marks_and_paging(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _active(make_user, session_factory, clock, on=True)
    await make_chat(user, telegram_chat_id=TEAM, title="Team", kind=ChatKind.group)
    await _auto(session_factory, user, A, "Zed excluded", enabled=False, excluded=True)
    for i in range(18):
        await _auto(session_factory, user, 900000300 + i, f"Contact {i:02d}")

    await _say(harness, user, "/chats")
    listing = harness.messages()[-1]
    assert listing.text.splitlines()[0] == t("chats.header_work_account", n=1)
    texts = button_texts(listing.reply_markup)
    assert texts[0] == "⚙️ Contact 00 · 💼"
    datas = callback_datas(listing.reply_markup)
    assert "chatspage:1" in datas and "wxopen:0" in datas and datas[-1] == "chatadd:0"
    assert "1/3" in texts

    await _press(harness, user, "chatspage:2", message_id=7)
    page = harness.api.of_type(EditMessageText)[-1]
    assert page.message_id == 7
    assert "🚫 Zed excluded · 💼" in button_texts(page.reply_markup)
    assert "⚙️ Team" in button_texts(page.reply_markup)  # enabled first, then by title
    assert "3/3" in button_texts(page.reply_markup)


async def test_chats_with_the_mode_off_hides_paused_chats(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _active(make_user, session_factory, clock)
    await make_chat(user, telegram_chat_id=TEAM, title="Team", kind=ChatKind.group)
    await _auto(session_factory, user, A, "Test Contact A", enabled=False)
    await _auto(session_factory, user, B, "Test Contact B", enabled=False)
    await _say(harness, user, "/chats")
    listing = harness.messages()[-1]
    assert listing.text == t("chats.list") + "\n" + t("chats.paused_footer", n=2)
    assert button_texts(listing.reply_markup)[0] == "⚙️ Team"
    assert "wxopen:0" not in callback_datas(listing.reply_markup)

    empty = await _active(make_user, session_factory, clock, on=True)
    await _say(harness, empty, "/chats")
    listing = harness.messages()[-1]
    assert listing.text == t("chats.header_work_account", n=0)
    assert callback_datas(listing.reply_markup) == ["wxopen:0", "chatadd:0"]


async def test_exclusion_picker(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await _active(make_user, session_factory, clock, on=True)
    gateway.dialogs[user.id] = [
        Dialog(chat_id=A, title="Test Contact A", kind="private"),
        Dialog(chat_id=B, title="Test Contact B", kind="private"),
        Dialog(chat_id=TEAM, title="Team (synthetic)", kind="group"),
    ]
    await _auto(session_factory, user, A, "Test Contact A", enabled=False, excluded=True)

    await _press(harness, user, "wxopen:0")
    picker = harness.messages()[-1]
    assert picker.text == t("chats.exclude_prompt")
    assert button_texts(picker.reply_markup) == ["🚫 Test Contact A", "✅ Test Contact B", "Done"]
    state, data = await _fsm(harness, user)
    assert state == ChatsStates.exclude.state
    picker_id = int(data["message_id"])
    await _press(harness, user, f"pickchat:{TEAM}", picker_id)  # not offered: ignored
    await _press(harness, user, f"pickchat:{A}", picker_id)
    await _press(harness, user, f"pickchat:{B}", picker_id)
    await _press(harness, user, "pickdone:0", picker_id)

    assert (await _fsm(harness, user))[0] is None
    edits = [e.text for e in harness.api.of_type(EditMessageText)]
    assert t("chats.exclude_done", excluded=1, included=1) in edits
    chats = await _chats(session_factory, user)
    assert (chats[A].excluded, chats[A].enabled) == (False, True)
    assert (chats[B].origin, chats[B].excluded, chats[B].enabled) == (
        WorkChatOrigin.auto,
        True,
        False,
    )
    assert TEAM not in chats

    await _press(harness, user, "wxopen:0")
    await _say(harness, user, "/cancel")
    assert _last_text(harness) == t("chats.exclude_cancelled")
    assert (await _fsm(harness, user))[0] is None

    gateway.fail_list_dialogs = RuntimeError("synthetic: Telegram is down")
    await _press(harness, user, "wxopen:0")
    problem = t("connect.telegram_unavailable")
    assert _last_text(harness) == t("common.send_to_continue", problem=problem, command="/chats")
    assert (await _fsm(harness, user))[0] is None

    off = await _active(make_user, session_factory, clock)
    await _press(harness, off, "wxopen:0")
    assert _alerts(harness)[-1] == t("chats.exclude_mode_off")


async def test_add_picker_lists_only_groups_with_the_mode_on(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await _active(make_user, session_factory, clock, on=True)
    gateway.dialogs[user.id] = [
        Dialog(chat_id=A, title="Test Contact A", kind="private"),
        Dialog(chat_id=TEAM, title="Team (synthetic)", kind="group"),
    ]
    await _press(harness, user, "chatadd:0")
    picker = harness.messages()[-1]
    assert picker.text == t("chats.add_prompt_groups")
    assert callback_datas(picker.reply_markup) == [f"pickchat:{TEAM}", "pickdone:0"]


# --- US4: /history and /my_data -------------------------------------------------------------------


async def test_history_and_my_data_show_the_mode(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await _active(make_user, session_factory, clock, on=True)
    await _write(harness, user, A)
    chat = (await _chats(session_factory, user))[A]
    clock.advance(minutes=1)
    await work_account.exclude_chat(session_factory, user.id, work_chat_id=chat.id, now=clock())
    clock.advance(minutes=1)
    await work_account.include_chat(session_factory, user.id, chat.id, now=clock())

    await _say(harness, user, "/history")
    history = _last_text(harness)
    assert f"{t('history.chat_picked_up')} · Test Contact A" in history
    assert f"{t('history.kind.chat_excluded')} · Test Contact A" in history
    assert f"{t('history.kind.chat_included')} · Test Contact A" in history
    assert t("history.kind.work_account_on") in history

    await _say(harness, user, "/my_data")
    summary = harness.messages()[-1].text
    assert "💼 Work account mode: on (since" in summary
    assert "picked-up chats: 1 · excluded: 0" in summary

    async with session_factory() as session:
        export = await export_user_data(session, user.id, now=clock())
        assert export["users"][0]["work_account"] is True
        assert export["users"][0]["work_account_changed_at"] is not None
        (row,) = export["work_chats"]
        assert (row["origin"], row["excluded"]) == ("auto", False)
        await delete_user_data(session, user.id)
        await session.commit()
    async with session_factory() as session:
        assert await chats_repo.count_auto(session, user.id) == 0
