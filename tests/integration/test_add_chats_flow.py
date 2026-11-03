"""➕ Add work chats after setup (specs/007 T012, T013, T017, T020, T021, T025, T028): the
button in /chats, the shared picker in ``add`` mode, Done, the profile update, leaving the
picker, Telegram problems, /history and the access gate. Fake LLM, fake gateway, offline Bot
API; synthetic data only."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    SendMessage,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    BetaEntry,
    ChatKind,
    LoginMethod,
    ProfileScope,
    ProfileState,
    SessionStatus,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.jobs import refresh_user
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.style.profiles import last_undoable_refresh, profile_build_lock
from scribe.telegram.control_bot.add_chats import AddChatsStates
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.control_bot.render import help_text
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import Dialog, GatewayNotStarted, IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, offline_bot, text_update
from tests.integration.us1_support import grounded_reply, next_message_id
from tests.integration.us2_support import US2Harness, callback_datas
from tests.integration.us4_support import (
    add_own_messages,
    add_profile,
    by_scope,
    make_harness,
    profiles_of,
    style_output,
)

PROJECT_X = -100900000001
DESIGN = -100900000002
ANNA = 900000003
PETR = 900000004
OLD = -100900000005


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
    fake_llm.set_response(
        "profile.build", by_scope(style_output("New global."), style_output("New contact."))
    )
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


# --- helpers ------------------------------------------------------------------------------------


async def _connect(
    session_factory: async_sessionmaker[AsyncSession], user: User, clock: FrozenClock
) -> None:
    async with session_factory() as session:
        await users_repo.upsert_telegram_session(
            session, user.id, "synthetic-session", method=LoginMethod.qr, connected_at=clock()
        )
        await session.commit()


async def _user(
    make_user: MakeUser,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    *,
    state: UserState = UserState.active,
    connected: bool = True,
    **values: Any,
) -> User:
    user = await make_user(state=state, **values)
    if connected:
        await _connect(session_factory, user, clock)
    return user


async def _five_dialogs(
    gateway: FakeMessagingGateway, user: User, make_chat: MakeChat
) -> tuple[WorkChat, WorkChat]:
    """Five Telegram dialogs; Anna is an enabled work chat, Old a disabled one."""
    gateway.dialogs[user.id] = [
        Dialog(chat_id=PROJECT_X, title="Project X (synthetic)", kind="group"),
        Dialog(chat_id=ANNA, title="Anna (synthetic)", kind="private"),
        Dialog(chat_id=DESIGN, title="Design (synthetic)", kind="group"),
        Dialog(chat_id=OLD, title="Old (synthetic)", kind="group"),
        Dialog(chat_id=PETR, title="Petr (synthetic)", kind="private"),
    ]
    anna = await make_chat(user, telegram_chat_id=ANNA, title="Anna (synthetic)")
    old = await make_chat(
        user, telegram_chat_id=OLD, title="Old (synthetic)", kind=ChatKind.group, enabled=False
    )
    return anna, old


async def _fsm(harness: US2Harness, user: User) -> tuple[str | None, dict[str, Any]]:
    chat = user.telegram_control_chat_id
    context = harness.dispatcher.fsm.get_context(harness.bot, chat, chat)
    return await context.get_state(), await context.get_data()


async def _open(harness: US2Harness, user: User) -> int:
    """Tap ➕ and return the picker's message id."""
    await harness.feed(callback_update(user.telegram_control_chat_id, "chatadd:0"))
    state, data = await _fsm(harness, user)
    assert state == AddChatsStates.picking.state
    return int(data["message_id"])


async def _press(harness: US2Harness, user: User, data: str, message_id: int = 1) -> None:
    await harness.feed(callback_update(user.telegram_control_chat_id, data, message_id=message_id))


async def _work_chats(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> dict[int, WorkChat]:
    async with session_factory() as session:
        return {c.telegram_chat_id: c for c in await chats_repo.list_work_chats(session, user.id)}


async def _added_records(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> list[ActivityRecord]:
    async with session_factory() as session:
        result = await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user.id,
                ActivityRecord.kind == ActivityKind.chats_added,
            )
        )
        return list(result.scalars().all())


def _alerts(harness: US2Harness) -> list[str | None]:
    return [a.text for a in harness.api.of_type(AnswerCallbackQuery)]


def _own(user: User, chat_id: int, n: int, clock: FrozenClock) -> list[IncomingMessage]:
    return [
        IncomingMessage(
            user_id=user.id, chat_id=chat_id, message_id=next_message_id(), sender_peer_id=0,
            sender_display_name="me", text=f"Synthetic own text {i}", has_media=False,
            sent_at=clock() - timedelta(days=3), is_outgoing=True,
        )
        for i in range(n)
    ]  # fmt: skip


def _incoming(user: User, chat_id: int, clock: FrozenClock, **values: Any) -> IncomingMessage:
    kind = values.pop("chat_kind", "private")
    return IncomingMessage(
        user_id=user.id,
        chat_id=chat_id,
        message_id=next_message_id(),
        sender_peer_id=values.pop("sender_peer_id", chat_id),
        sender_display_name="Synthetic Sender",
        text=values.pop("text", "Synthetic question?"),
        has_media=False,
        sent_at=clock(),
        chat_kind=kind,
        **values,
    )


# --- US1: entry point and Done ------------------------------------------------------------------


async def test_chats_shows_add_button_only_after_setup(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await make_chat(user, title="Anna (synthetic)")
    await harness.feed(text_update(user.telegram_control_chat_id, "/chats"))
    listing = harness.messages()[-1]
    assert listing.text == t("chats.list")
    assert callback_datas(listing.reply_markup)[-1] == "chatadd:0"
    assert listing.reply_markup.inline_keyboard[-1][0].text == "➕ Add work chats"  # type: ignore[union-attr]

    empty = await _user(make_user, session_factory, clock)
    await harness.feed(text_update(empty.telegram_control_chat_id, "/chats"))
    assert harness.messages()[-1].text == t("chats.none")
    assert callback_datas(harness.messages()[-1].reply_markup) == ["chatadd:0"]

    setting_up = await _user(make_user, session_factory, clock, state=UserState.setting_up)
    await make_chat(setting_up, title="Picked at setup")
    await harness.feed(text_update(setting_up.telegram_control_chat_id, "/chats"))
    assert "chatadd:0" not in callback_datas(harness.messages()[-1].reply_markup)
    await harness.feed(callback_update(setting_up.telegram_control_chat_id, "chatadd:0"))
    assert _alerts(harness)[-1] == t("common.not_set_up")
    assert (await _fsm(harness, setting_up))[0] is None


async def test_add_lists_only_new_chats_and_done_adds_them(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)

    picker_id = await _open(harness, user)
    picker = harness.messages()[-1]
    assert picker.text == t("chats.add_prompt")
    # The enabled (Anna) and the disabled (Old) work chat are not offered.
    assert callback_datas(picker.reply_markup) == [
        f"pickchat:{PROJECT_X}",
        f"pickchat:{DESIGN}",
        f"pickchat:{PETR}",
        "pickdone:0",
    ]
    await _press(harness, user, f"pickchat:{PETR}", picker_id)
    await _press(harness, user, f"pickchat:{PROJECT_X}", picker_id)
    harness.api.clear()
    await _press(harness, user, "pickdone:0", picker_id)

    chats = await _work_chats(session_factory, user)
    assert set(chats) == {ANNA, OLD, PETR, PROJECT_X}
    assert chats[PETR].enabled and chats[PETR].kind == ChatKind.private
    assert chats[PROJECT_X].enabled and chats[PROJECT_X].kind == ChatKind.group
    assert chats[PROJECT_X].title == "Project X (synthetic)"
    assert not chats[OLD].enabled
    edit = harness.edits()[0]
    assert edit.message_id == picker_id and edit.reply_markup is None
    assert edit.text == t("chats.added", "en", n=2)
    assert "in the background" in edit.text
    # The /chats list follows, then the one profile note (no own messages there: nothing new).
    listing, note = harness.messages()
    assert listing.text == t("chats.list")
    assert callback_datas(listing.reply_markup)[-1] == "chatadd:0"
    assert len(callback_datas(listing.reply_markup)) == 5
    assert note.text == t("chats.add_nothing_to_learn", "en", months=6)
    assert fake_llm.calls_for("profile.build") == []
    assert t("profile.rebuilding") not in harness.bot_texts()
    assert (await _fsm(harness, user))[0] is None

    (record,) = await _added_records(session_factory, user)
    assert set(record.meta_json) == {"work_chat_ids"}
    assert set(record.meta_json["work_chat_ids"]) == {str(chats[PETR].id), str(chats[PROJECT_X].id)}
    assert "synthetic" not in str(record.meta_json).lower()


async def test_added_chat_is_monitored_from_now_on(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)
    # Before the add: not a work chat, nothing is stored and nothing is drafted later.
    await gateway.emit_new_message(_incoming(user, PETR, clock, text="Before the add?"))
    assert await harness.turns(user) == []

    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{PETR}", picker_id)
    await _press(harness, user, f"pickchat:{DESIGN}", picker_id)
    await _press(harness, user, "pickdone:0", picker_id)
    assert await harness.turns(user) == []

    await gateway.emit_new_message(_incoming(user, PETR, clock, text="After the add?"))
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.waiting

    # A group chat added here follows the group rule: no tag, no reply -> no turn.
    await gateway.emit_new_message(
        _incoming(user, DESIGN, clock, chat_kind="group", sender_peer_id=900000077)
    )
    assert len(await harness.turns(user)) == 1
    await gateway.emit_new_message(
        _incoming(user, DESIGN, clock, chat_kind="group", sender_peer_id=900000077,
                  mentions_me=True)
    )  # fmt: skip
    assert len(await harness.turns(user)) == 2


async def test_nothing_selected_double_done_and_stale_snapshot_add_nothing(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)

    # Done with nothing selected.
    picker_id = await _open(harness, user)
    await _press(harness, user, "pickdone:0", picker_id)
    assert harness.edits()[-1].text == t("chats.add_nothing")
    assert set(await _work_chats(session_factory, user)) == {ANNA, OLD}
    assert (await _fsm(harness, user))[0] is None
    assert await _added_records(session_factory, user) == []

    # A stale snapshot: Design became a (disabled) work chat after the picker opened.
    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{DESIGN}", picker_id)
    await make_chat(user, telegram_chat_id=DESIGN, title="Design", kind=ChatKind.group,
                    enabled=False)  # fmt: skip
    await _press(harness, user, "pickdone:0", picker_id)
    assert harness.edits()[-1].text == t("chats.add_nothing")
    design = (await _work_chats(session_factory, user))[DESIGN]
    assert not design.enabled and design.title == "Design"
    assert await _added_records(session_factory, user) == []

    # A double Done: the second one (same picker data) adds nothing and records nothing.
    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{PETR}", picker_id)
    _, data = await _fsm(harness, user)
    await _press(harness, user, "pickdone:0", picker_id)
    chat = user.telegram_control_chat_id
    context = harness.dispatcher.fsm.get_context(harness.bot, chat, chat)
    await context.set_state(AddChatsStates.picking)
    await context.set_data(data)
    await _press(harness, user, "pickdone:0", picker_id)
    assert harness.edits()[-1].text == t("chats.add_nothing")
    assert len([c for c in (await _work_chats(session_factory, user)) if c == PETR]) == 1
    assert len(await _added_records(session_factory, user)) == 1
    # A third tap on the closed picker: "list closed", nothing changes.
    harness.api.clear()
    await _press(harness, user, "pickdone:0", picker_id)
    assert _alerts(harness) == [t("alert.picker_closed")]
    assert harness.edits() == []


# --- US2: the profile update --------------------------------------------------------------------


async def test_profiles_learn_the_new_chat(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: FakeMessagingGateway,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)
    global_v1 = await add_profile(session_factory, user, summary="Old global.")
    # An undoable weekly refresh before the add (FR-016: the update ends it, like 🔄 Rebuild).
    anna_contact = await make_contact(user, telegram_peer_id=ANNA, display_name="Anna")
    anna = (await _work_chats(session_factory, user))[ANNA]
    await add_profile(session_factory, user, contact=anna_contact, summary="Old Anna.")
    clock.advance(days=2)
    await add_own_messages(session_factory, user, anna, 14, at=clock() - timedelta(days=1))
    refreshed = await refresh_user(
        session_factory, llm_router, harness.bot, user.id, settings=settings, clock=clock
    )
    assert refreshed.global_refreshed
    async with session_factory() as session:
        assert await last_undoable_refresh(session, user.id) is not None
    gateway.own_history[(user.id, PETR)] = _own(user, PETR, 30, clock)
    fake_llm.calls.clear()
    harness.api.clear()

    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{PETR}", picker_id)
    await _press(harness, user, "pickdone:0", picker_id)

    current = await profiles_of(session_factory, user, state=ProfileState.current)
    by = {p.scope: p for p in current if p.scope == ProfileScope.global_}
    assert by[ProfileScope.global_].version == 3 and by[ProfileScope.global_].id != global_v1.id
    assert by[ProfileScope.global_].sample_size == 30
    async with session_factory() as session:
        petr = await chats_repo.get_contact_by_peer(session, user.id, PETR)
        assert await last_undoable_refresh(session, user.id) is None
    assert petr is not None and petr.own_message_count_6m == 30
    assert any(p.contact_id == petr.id for p in current)
    note = harness.messages()[-1].text
    assert note.startswith("✅ Rebuilt: global profile from 30 of your messages")
    assert t("profile.rebuilding") not in harness.bot_texts()
    assert sum(text.startswith("✅ Rebuilt") for text in harness.bot_texts()) == 1


async def test_failed_update_keeps_chats_and_profiles(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)
    global_v1 = await add_profile(session_factory, user, summary="Old global.")
    gateway.own_history[(user.id, PETR)] = _own(user, PETR, 30, clock)
    fake_llm.set_response("profile.build", RuntimeError("synthetic model outage"))

    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{PETR}", picker_id)
    await _press(harness, user, "pickdone:0", picker_id)

    assert harness.messages()[-1].text == t("chats.add_learn_failed")
    assert (await _work_chats(session_factory, user))[PETR].enabled
    (current,) = await profiles_of(session_factory, user, state=ProfileState.current)
    assert current.id == global_v1.id


async def test_update_waits_for_a_running_rebuild(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)
    gateway.own_history[(user.id, PETR)] = _own(user, PETR, 30, clock)
    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{PETR}", picker_id)

    lock = profile_build_lock(user.id)
    await lock.acquire()
    try:
        done = asyncio.create_task(_press(harness, user, "pickdone:0", picker_id))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if t("chats.list") in harness.bot_texts():
                break
        assert (await _work_chats(session_factory, user))[PETR].enabled  # added at once
        assert not any(text.startswith("✅ Rebuilt") for text in harness.bot_texts())
        assert not done.done()
    finally:
        lock.release()
    await done
    assert sum(text.startswith("✅ Rebuilt") for text in harness.bot_texts()) == 1


async def test_update_ends_silently_when_the_user_was_deleted(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)
    gateway.own_history[(user.id, PETR)] = _own(user, PETR, 30, clock)
    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{PETR}", picker_id)

    lock = profile_build_lock(user.id)
    await lock.acquire()
    try:
        done = asyncio.create_task(_press(harness, user, "pickdone:0", picker_id))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if t("chats.list") in harness.bot_texts():
                break
        async with session_factory() as session:
            await users_repo.delete_user(session, user.id)
            await session.commit()
        sent = len(harness.messages())
    finally:
        lock.release()
    await done
    assert len(harness.messages()) == sent
    assert fake_llm.calls_for("profile.build") == []


# --- US3: leaving the picker, Telegram problems -------------------------------------------------


async def test_cancel_and_other_commands_close_the_picker(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    control = user.telegram_control_chat_id
    await _five_dialogs(gateway, user, make_chat)

    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{PETR}", picker_id)
    await harness.feed(text_update(control, "some free text"))
    assert harness.bot_texts()[-1] == t("chats.add_hint")
    await harness.feed(text_update(control, "/cancel"))
    assert harness.bot_texts()[-1] == t("chats.add_cancelled")
    (removed,) = harness.api.of_type(EditMessageReplyMarkup)[-1:]
    assert removed.message_id == picker_id and removed.reply_markup is None
    assert (await _fsm(harness, user))[0] is None

    for command, expected in (("/settings", None), ("/help", help_text("en"))):
        picker_id = await _open(harness, user)
        await _press(harness, user, f"pickchat:{PETR}", picker_id)
        await harness.feed(text_update(control, command))
        assert (await _fsm(harness, user))[0] is None
        if expected is not None:
            assert harness.bot_texts()[-1] == expected
        else:
            assert "settings:delay" in callback_datas(harness.messages()[-1].reply_markup)
        harness.api.clear()
        await _press(harness, user, "pickdone:0", picker_id)
        assert _alerts(harness) == [t("alert.picker_closed")]
    assert set(await _work_chats(session_factory, user)) == {ANNA, OLD}


async def test_stale_picker_buttons_change_nothing(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)
    first = await _open(harness, user)
    second = await _open(harness, user)
    assert first != second
    harness.api.clear()
    await _press(harness, user, f"pickchat:{PETR}", first)
    await _press(harness, user, "pickpage:1", first)
    await _press(harness, user, "pickdone:0", first)
    assert _alerts(harness) == [t("alert.picker_closed")] * 3
    _, data = await _fsm(harness, user)
    assert data["selected"] == [] and data["message_id"] == second
    assert set(await _work_chats(session_factory, user)) == {ANNA, OLD}

    # No picker open at all (also a setup picker message after setup): "list closed".
    other = await _user(make_user, session_factory, clock)
    harness.api.clear()
    await harness.feed(callback_update(other.telegram_control_chat_id, "pickchat:-1003"))
    await harness.feed(callback_update(other.telegram_control_chat_id, "pickdone:0"))
    assert _alerts(harness) == [t("alert.picker_closed")] * 2
    assert await _work_chats(session_factory, other) == {}


@pytest.mark.parametrize("status", [None, SessionStatus.revoked, SessionStatus.error])
async def test_disconnected_telegram_gives_the_reconnect_text(
    status: SessionStatus | None,
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock, connected=status is not None)
    if status is not None:
        async with session_factory() as session:
            await users_repo.set_telegram_session_status(session, user.id, status)
            await session.commit()
    await _five_dialogs(gateway, user, make_chat)
    await harness.feed(callback_update(user.telegram_control_chat_id, "chatadd:0"))
    reply = harness.messages()[-1]
    assert reply.text == t("telegram.disconnected_start")
    assert callback_datas(reply.reply_markup) == ["tgreconnect:0"]
    assert (await _fsm(harness, user))[0] is None
    assert set(await _work_chats(session_factory, user)) == {ANNA, OLD}


@pytest.mark.parametrize("error", [GatewayNotStarted("synthetic"), RuntimeError("synthetic")])
async def test_unreachable_telegram_gives_the_retry_text(
    error: Exception,
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)
    gateway.fail_list_dialogs = error
    await harness.feed(callback_update(user.telegram_control_chat_id, "chatadd:0"))
    problem = t("connect.telegram_unavailable")
    expected = t("common.send_to_continue", problem=problem, command="/chats")
    assert harness.bot_texts()[-1] == expected
    assert (await _fsm(harness, user))[0] is None
    assert set(await _work_chats(session_factory, user)) == {ANNA, OLD}


async def test_nothing_left_to_add(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    await _five_dialogs(gateway, user, make_chat)
    gateway.dialogs[user.id] = [d for d in gateway.dialogs[user.id] if d.chat_id in (ANNA, OLD)]
    await harness.feed(callback_update(user.telegram_control_chat_id, "chatadd:0"))
    assert harness.bot_texts()[-1] == t("chats.add_none_left")
    assert (await _fsm(harness, user))[0] is None


# --- US4: /history ------------------------------------------------------------------------------


async def test_history_shows_the_added_chats(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await _user(make_user, session_factory, clock)
    control = user.telegram_control_chat_id
    await _five_dialogs(gateway, user, make_chat)
    picker_id = await _open(harness, user)
    await _press(harness, user, f"pickchat:{PROJECT_X}", picker_id)
    await _press(harness, user, f"pickchat:{PETR}", picker_id)
    await _press(harness, user, "pickdone:0", picker_id)

    await harness.feed(text_update(control, "/history"))
    lines = harness.bot_texts()[-1].splitlines()
    (line,) = [line for line in lines if "Work chats added" in line]
    assert line.endswith("· ➕ Work chats added · Project X (synthetic), Petr (synthetic)")

    # A chat deleted since is left out; with none left, the label stands alone.
    async with session_factory() as session:
        chat = await chats_repo.get_work_chat_by_telegram_id(session, user.id, PETR)
        assert chat is not None
        await session.delete(chat)
        await session.commit()
    await harness.feed(text_update(control, "/history"))
    (line,) = [x for x in harness.bot_texts()[-1].splitlines() if "Work chats added" in x]
    assert line.endswith("· ➕ Work chats added · Project X (synthetic)")


async def test_history_caps_titles_in_russian(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, clock, language="ru")
    gateway.dialogs[user.id] = [
        Dialog(chat_id=-100900000100 - i, title=f"Чат {i}", kind="group") for i in range(5)
    ]
    picker_id = await _open(harness, user)
    assert harness.messages()[-1].text == t("chats.add_prompt", "ru")
    for i in range(5):
        await _press(harness, user, f"pickchat:{-100900000100 - i}", picker_id)
    await _press(harness, user, "pickdone:0", picker_id)
    assert harness.edits()[-1].text == t("chats.added", "ru", n=5)
    await harness.feed(text_update(user.telegram_control_chat_id, "/history"))
    (line,) = [x for x in harness.bot_texts()[-1].splitlines() if "Добавлены рабочие чаты" in x]
    assert line.endswith("· ➕ Добавлены рабочие чаты · Чат 0, Чат 1, Чат 2, ещё 2")


# --- access gate (005) --------------------------------------------------------------------------


async def test_access_gate_applies_to_the_button_and_the_picker(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    gated = settings.model_copy(update={"access_gate": True})
    bot, api = offline_bot()
    dispatcher = create_dispatcher(
        session_factory=session_factory,
        settings=gated,
        storage=PostgresFSMStorage(session_factory),
        clock=clock,
        gateway=gateway,
    )
    user = await _user(make_user, session_factory, clock)
    control = user.telegram_control_chat_id
    await _five_dialogs(gateway, user, make_chat)

    await dispatcher.feed_update(bot, callback_update(control, "chatadd:0"))
    assert all(r.text != t("chats.add_prompt") for r in api.of_type(SendMessage))
    context = dispatcher.fsm.get_context(bot, control, control)
    assert await context.get_state() is None
    # A picker that was open when access ended adds nothing.
    await context.set_state(AddChatsStates.picking)
    await context.set_data(
        {"purpose": "add", "dialogs": [[PETR, "Petr", "private"]], "selected": [PETR],
         "page": 0, "message_id": 1}
    )  # fmt: skip
    await dispatcher.feed_update(bot, callback_update(control, "pickdone:0"))
    assert set(await _work_chats(session_factory, user)) == {ANNA, OLD}

    # With access, the button works.
    db_session.add(BetaEntry(telegram_id=control, added_by="operator", added_at=clock()))
    await db_session.flush()
    await context.clear()
    api.clear()
    await dispatcher.feed_update(bot, callback_update(control, "chatadd:0"))
    assert api.sent_texts == [t("chats.add_prompt")]
    assert await context.get_state() == AddChatsStates.picking.state
