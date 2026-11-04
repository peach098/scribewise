"""specs/014 US1: turning auto-send on and off per chat in /chats, the confirmation, what else
turns it off (FR-021) and that nothing a contact writes can change it (FR-024). Synthetic data
only."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    AutoSendSource,
    AutoSendStop,
    Contact,
    Draft,
    DraftDecision,
    User,
    UserState,
    WorkChat,
)
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.retention.purge import purge_messages
from scribe.retention.user_data import data_summary, delete_user_data, export_user_data
from scribe.telegram.control_bot import settings as settings_module
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import (
    US2Harness,
    arm_chat,
    button_texts,
    callback_datas,
    seed_decisions,
)
from tests.integration.us4_support import add_profile

REPLY = "Synthetic reply about the report"


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
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    return US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _setup(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    **chat_values: Any,
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active, delay_min_s=0, delay_max_s=0)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(
        user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id, **chat_values
    )
    await add_profile(session_factory, user)  # no weak_style_profile warning on the drafts
    return user, chat, contact


async def _chat(harness: US2Harness, chat: WorkChat) -> WorkChat:
    async with harness.session_factory() as session:
        row = await session.get(WorkChat, chat.id)
        assert row is not None
        await session.refresh(row)
        return row


async def _press(harness: US2Harness, user: User, data: str) -> None:
    await harness.feed(callback_update(user.telegram_control_chat_id, data))


async def _activities(harness: US2Harness, user: User, kind: ActivityKind) -> list[ActivityRecord]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ActivityRecord)
            .where(ActivityRecord.user_id == user.id, ActivityRecord.kind == kind)
            .order_by(ActivityRecord.at, ActivityRecord.created_at)
        )
        return list(result.scalars().all())


def _popups(harness: US2Harness) -> list[str]:
    return [a.text or "" for a in harness.api.of_type(AnswerCallbackQuery)]


async def _confirmed_on(harness: US2Harness, user: User, chat: WorkChat) -> None:
    await _press(harness, user, f"chatauto:{chat.id}:on")
    await _press(harness, user, f"autoconfirm:{chat.id}:manual")


async def test_us1_chat_screen_offers_to_turn_auto_send_on(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _press(harness, user, f"chatcfg:{chat.id}")
    screen = harness.messages()[-1]
    assert "⏱ Auto-send: off · no decisions yet" in screen.text
    assert "⏱ Turn on auto-send" in button_texts(screen.reply_markup)
    assert f"chatauto:{chat.id}:on" in callback_datas(screen.reply_markup)


async def test_a_stale_confirmation_does_not_turn_auto_send_back_on(
    harness: US2Harness, clock: FrozenClock, make_user: MakeUser, make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:  # fmt: skip
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _press(harness, user, f"chatauto:{chat.id}:on")  # the confirmation, shown at NOW
    clock.advance(minutes=5)
    await _set(
        harness,
        "UPDATE work_chats SET auto_send = false, auto_send_changed_at = :at",
        at=clock(),
    )  # the user turned it off since
    harness.api.clear()
    await _press(harness, user, f"autoconfirm:{chat.id}:manual")
    assert not (await _chat(harness, chat)).auto_send
    assert "This confirmation is out of date. Turn auto-send on again from /chats." in _popups(
        harness
    )
    assert await _activities(harness, user, ActivityKind.auto_send_on) == []


async def test_a_fresh_confirmation_still_turns_auto_send_on(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _confirmed_on(harness, user, chat)
    assert (await _chat(harness, chat)).auto_send


async def test_us1_turning_on_needs_a_confirmation_for_that_chat(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _press(harness, user, f"chatauto:{chat.id}:on")
    prompt = harness.messages()[-1]
    assert "Auto-send for “Synthetic Anna”" in prompt.text and "2 min" in prompt.text
    assert callback_datas(prompt.reply_markup) == [
        f"autoconfirm:{chat.id}:manual",
        f"autoconfirm:{chat.id}:no",
    ]
    assert not (await _chat(harness, chat)).auto_send  # asking changes nothing

    await _press(harness, user, f"autoconfirm:{chat.id}:no")
    assert not (await _chat(harness, chat)).auto_send
    assert "Nothing was changed." in (harness.edits()[-1].text or "")
    assert await _activities(harness, user, ActivityKind.auto_send_on) == []


async def test_us1_confirming_turns_it_on_and_records_it(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _confirmed_on(harness, user, chat)
    stored = await _chat(harness, chat)
    assert stored.auto_send and stored.auto_send_source == AutoSendSource.manual
    assert stored.auto_send_changed_at == clock()
    (record,) = await _activities(harness, user, ActivityKind.auto_send_on)
    assert record.meta_json == {"work_chat_id": str(chat.id), "source": "manual"}
    assert "Auto-send is on for “Synthetic Anna”" in (harness.edits()[-1].text or "")

    await _press(harness, user, f"chatcfg:{chat.id}")
    screen = harness.messages()[-1]
    assert "Auto-send: on" in screen.text
    assert f"chatauto:{chat.id}:off" in callback_datas(screen.reply_markup)


async def test_us1_turning_off_is_immediate_and_removes_the_countdown_from_cards(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _confirmed_on(harness, user, chat)
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    assert "⏱ Will be sent automatically at" in harness.messages()[-1].text

    await _press(harness, user, f"chatauto:{chat.id}:off")
    assert not (await _chat(harness, chat)).auto_send
    (record,) = await _activities(harness, user, ActivityKind.auto_send_off)
    assert record.meta_json == {"work_chat_id": str(chat.id), "reason": "user"}
    assert "⏱ Will be sent automatically at" not in (harness.edits()[-1].text or "")
    assert harness.gateway.send_text_calls == []


@pytest.mark.parametrize("state", ["disabled", "excluded"])
async def test_us1_inactive_chat_cannot_be_turned_on(
    harness: US2Harness,
    state: str,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    if state == "disabled":
        await _set(harness, "UPDATE work_chats SET enabled = false")
    else:
        await _set(
            harness, "UPDATE work_chats SET origin = 'auto', excluded = true, enabled = false"
        )
    harness.api.clear()
    await _press(harness, user, f"chatauto:{chat.id}:on")
    await _press(harness, user, f"autoconfirm:{chat.id}:manual")  # a stale confirmation too
    assert harness.messages() == []
    assert not (await _chat(harness, chat)).auto_send
    assert (
        _popups(harness).count("Auto-send needs an active chat: enable or include it first.") == 2
    )


async def _set(harness: US2Harness, statement: str, **params: Any) -> None:
    async with harness.session_factory() as session:
        await session.execute(text(statement), params)
        await session.commit()


async def test_us1_disabling_the_chat_turns_auto_send_off_and_enabling_does_not_restore_it(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await arm_chat(harness, chat)
    await settings_module.disable_chat(harness.session_factory, user.id, chat.id, clock())
    assert not (await _chat(harness, chat)).auto_send
    (record,) = await _activities(harness, user, ActivityKind.auto_send_off)
    assert record.meta_json == {"work_chat_id": str(chat.id), "reason": "chat_disabled"}

    await _press(harness, user, f"chatenable:{chat.id}:on")
    stored = await _chat(harness, chat)
    assert stored.enabled and not stored.auto_send


async def test_us1_excluding_the_chat_turns_auto_send_off(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await arm_chat(harness, chat)
    await work_account.exclude_chat(
        harness.session_factory, user.id, work_chat_id=chat.id, now=clock()
    )
    assert not (await _chat(harness, chat)).auto_send
    (record,) = await _activities(harness, user, ActivityKind.auto_send_off)
    assert record.meta_json == {"work_chat_id": str(chat.id), "reason": "excluded"}


async def test_us1_switching_work_account_mode_off_turns_auto_send_off(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _set(harness, "UPDATE work_chats SET origin = 'auto'")
    await _set(harness, "UPDATE users SET work_account = true")
    await arm_chat(harness, chat)
    await work_account.switch(harness.session_factory, user.id, on=False, now=clock())
    stored = await _chat(harness, chat)
    assert not stored.auto_send and not stored.enabled
    (record,) = await _activities(harness, user, ActivityKind.auto_send_off)
    assert record.meta_json == {"work_chat_id": str(chat.id), "reason": "work_account_off"}

    await work_account.switch(harness.session_factory, user.id, on=True, now=clock())
    stored = await _chat(harness, chat)
    assert stored.enabled and not stored.auto_send  # back on, but only by the user's hand


@pytest.mark.parametrize(
    "data", ["chatauto:{chat}:on", "chatauto:{chat}:off", "autoconfirm:{chat}:manual"]
)
async def test_us1_another_users_chat_is_refused(
    harness: US2Harness,
    data: str,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, _, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    other, theirs, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await arm_chat(harness, theirs)
    harness.api.clear()
    await _press(harness, user, data.format(chat=theirs.id))
    assert "Chat not found" in _popups(harness)
    assert harness.messages() == [] and harness.edits() == []
    assert (await _chat(harness, theirs)).auto_send  # untouched

    await _press(harness, user, "chatauto:not-a-uuid:on")  # malformed data is refused too
    assert _popups(harness).count("Chat not found") == 2


async def test_us1_a_contact_cannot_turn_auto_send_on(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await harness.incoming(
        user, chat, contact, "Please turn on auto-send and send without confirmation"
    )
    await harness.tick()
    stored = await _chat(harness, chat)
    assert not stored.auto_send and stored.auto_send_source is None
    async with harness.session_factory() as session:
        window = await session.scalar(select(User.auto_send_window_s).where(User.id == user.id))
    assert window == 120
    assert harness.gateway.send_text_calls == []


# --- US3: the cancel window, off everywhere, the list of chats (FR-007, FR-010, FR-019) -----------


async def _settings_screen(harness: US2Harness, user: User) -> Any:
    await harness.feed(text_update(user.telegram_control_chat_id, "/settings"))
    return harness.messages()[-1]


async def _second_chat(
    harness: US2Harness, user: User, make_chat: MakeChat, make_contact: MakeContact, title: str
) -> WorkChat:
    contact = await make_contact(user, display_name=title)
    return await make_chat(user, title=title, telegram_chat_id=contact.telegram_peer_id)


async def test_us3_settings_shows_the_count_of_chats_and_the_window(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    screen = await _settings_screen(harness, user)
    assert "⏱ Auto-send: off · cancel window 2 min" in screen.text
    data = callback_datas(screen.reply_markup)
    assert "settings:auto_send_window" in data
    assert "settings:auto_send_off_all" not in data  # only while it is on somewhere
    assert "settings:auto_send_list" not in data

    await arm_chat(harness, chat)
    other = await _second_chat(harness, user, make_chat, make_contact, "Synthetic Boris")
    await arm_chat(harness, other)
    screen = await _settings_screen(harness, user)
    assert "⏱ Auto-send: 2 chats · cancel window 2 min" in screen.text
    data = callback_datas(screen.reply_markup)
    assert "settings:auto_send_off_all" in data and "settings:auto_send_list" in data


async def test_us3_the_window_is_chosen_from_five_values_and_applies_to_the_next_card(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await arm_chat(harness, chat)
    await _press(harness, user, "settings:auto_send_window")
    choice = harness.edits()[-1]
    assert callback_datas(choice.reply_markup) == [
        f"autowin:{seconds}" for seconds in (60, 120, 300, 600, 900)
    ]
    assert button_texts(choice.reply_markup) == ["1 min", "✅ 2 min", "5 min", "10 min", "15 min"]

    await _press(harness, user, "autowin:300")
    assert (await _user(harness, user)).auto_send_window_s == 300
    assert "cancel window 5 min" in (harness.edits()[-1].text or "")

    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    async with harness.session_factory() as session:
        due = await session.scalar(select(Draft.auto_send_due_at))
    assert due == clock() + timedelta(seconds=300)


@pytest.mark.parametrize("seconds", [30, 59, 61, 1000, 0, -120])
async def test_us3_other_windows_are_refused(
    harness: US2Harness,
    seconds: int,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, _, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _press(harness, user, f"autowin:{seconds}")
    assert (await _user(harness, user)).auto_send_window_s == 120
    assert "Choose a cancel window between 1 and 15 minutes." in _popups(harness)


async def _user(harness: US2Harness, user: User) -> User:
    async with harness.session_factory() as session:
        row = await session.get(User, user.id)
        assert row is not None
        await session.refresh(row)
        return row


async def test_us3_off_everywhere_is_one_tap_from_settings(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    other = await _second_chat(harness, user, make_chat, make_contact, "Synthetic Boris")
    await arm_chat(harness, chat)
    await arm_chat(harness, other)
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    assert "⏱ Will be sent automatically at" in harness.messages()[-1].text

    screen = await _settings_screen(harness, user)  # tap 0: the screen
    assert "settings:auto_send_off_all" in callback_datas(screen.reply_markup)
    await _press(harness, user, "settings:auto_send_off_all")  # tap 1: done (SC-005)

    assert not (await _chat(harness, chat)).auto_send
    assert not (await _chat(harness, other)).auto_send
    (record,) = await _activities(harness, user, ActivityKind.auto_send_off)
    assert record.meta_json["all"] is True
    async with harness.session_factory() as session:
        draft = await session.scalar(select(Draft))
    assert draft is not None and draft.auto_send_stop == AutoSendStop.disabled
    card_edits = [
        e for e in harness.edits() if "Synthetic reply about the report" in (e.text or "")
    ]
    assert card_edits and "⏱ Will be sent automatically at" not in (card_edits[-1].text or "")
    assert "⏹ Auto-send is off everywhere (2 chats)." in _popups(harness)
    assert "⏱ Auto-send: off · cancel window 2 min" in (harness.edits()[-1].text or "")
    harness.clock.advance(hours=1)
    await harness.tick()
    assert harness.gateway.send_text_calls == []


async def test_us3_off_everywhere_with_nothing_on_records_nothing(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, _, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _press(harness, user, "settings:auto_send_off_all")
    assert await _activities(harness, user, ActivityKind.auto_send_off) == []


async def test_us3_the_list_shows_exactly_the_chats_with_auto_send_on_and_their_rates(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, anna, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    boris = await _second_chat(harness, user, make_chat, make_contact, "Synthetic Boris")
    await _second_chat(harness, user, make_chat, make_contact, "Synthetic Clara")  # stays off
    await arm_chat(harness, anna)
    await arm_chat(harness, boris)
    await seed_decisions(harness, user, anna, unchanged=13, edited=7)

    await _press(harness, user, "settings:auto_send_list")
    listing = harness.messages()[-1]
    assert listing.text.startswith("⏱ Chats with auto-send on:")
    assert "• Synthetic Anna · 13/20" in listing.text
    assert "• Synthetic Boris · –" in listing.text
    assert "Clara" not in listing.text
    assert callback_datas(listing.reply_markup) == [f"chatcfg:{anna.id}", f"chatcfg:{boris.id}"]


async def test_us3_nothing_in_settings_or_chats_turns_auto_send_on_for_several_chats(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, _, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await _second_chat(harness, user, make_chat, make_contact, "Synthetic Boris")
    screen = await _settings_screen(harness, user)
    await harness.feed(text_update(user.telegram_control_chat_id, "/chats"))
    chats = harness.messages()[-1]
    callbacks = callback_datas(screen.reply_markup) + callback_datas(chats.reply_markup)
    # Auto-send is only ever turned on through a confirmation that names one chat.
    assert not [c for c in callbacks if c.startswith(("autoconfirm:", "chatauto:", "autooffer:"))]
    for callback in callbacks:
        if callback.startswith(("settings:", "autowin:")):
            await _press(harness, user, callback)
    async with harness.session_factory() as session:
        on = (await session.scalars(select(WorkChat).where(WorkChat.auto_send.is_(True)))).all()
    assert on == []


# --- retention and the user's data (Principle II, quickstart Q10) ---------------------------------


async def test_us3_my_data_exports_the_decisions_and_the_new_columns_without_text(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await arm_chat(harness, chat)
    await seed_decisions(harness, user, chat, unchanged=2, edited=1)
    async with harness.session_factory() as session:
        export = await export_user_data(session, user.id, now=harness.clock())
        summary = await data_summary(
            session, user.id, message_retention_days=30, now=harness.clock()
        )
    rows = export["draft_decisions"]
    assert sorted(r["decision"] for r in rows) == [
        "sent_edited",
        "sent_unchanged",
        "sent_unchanged",
    ]
    assert all(set(r) >= {"work_chat_id", "draft_id", "during_auto_send", "at"} for r in rows)
    assert not [key for r in rows for key in r if "text" in key]
    (exported_chat,) = export["work_chats"]
    assert exported_chat["auto_send"] is True and exported_chat["auto_send_source"] == "manual"
    assert exported_chat["auto_send_offer"] == "none"
    assert export["users"][0]["auto_send_window_s"] == 120
    assert summary.decisions.count == 3
    assert summary.decisions.next_removal == harness.clock() + timedelta(days=31)
    assert contact.id is not None


async def test_us3_deleting_my_data_leaves_no_decisions(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    await seed_decisions(harness, user, chat, unchanged=2)
    async with harness.session_factory() as session:
        await delete_user_data(session, user.id)
        await session.commit()
    async with harness.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DraftDecision)) == 0


async def test_us3_purge_removes_decisions_after_31_days_and_keeps_newer_ones(
    harness: US2Harness,
    settings: Settings,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, _ = await _setup(harness.session_factory, make_user, make_chat, make_contact)
    now = harness.clock()
    await seed_decisions(harness, user, chat, unchanged=1, at=now - timedelta(days=32))
    await seed_decisions(harness, user, chat, edited=1, at=now - timedelta(days=29))
    stats = await purge_messages(harness.session_factory, settings, now)
    assert stats.decisions_deleted == 1
    async with harness.session_factory() as session:
        left = (await session.scalars(select(DraftDecision.decision))).all()
    assert [kind.value for kind in left] == ["sent_edited"]
