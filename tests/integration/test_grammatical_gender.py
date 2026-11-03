"""Issue #46: the user's grammatical gender for drafts. Only the user sets it: every user is
asked once (at setup, or by the ``grammatical_gender_prompt`` job for users set up before it),
with the answer suggested by their own hand-written messages shown first; /settings shows and
changes it; it is sent with every draft outside the profile (global and contact profiles); part
of /my_data; never logged. Fake LLM, fake gateway, offline Bot API; synthetic data only."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    SendMessage,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ConversationTurn,
    Draft,
    DraftState,
    Message,
    ProfileScope,
    User,
    UserState,
)
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.intake import events as events_module
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import StyleProfileOutput
from scribe.llm.registry import LLMRouter
from scribe.release_notes.sender import RETRY_AFTER_MARGIN_S, backoff
from scribe.retention.user_data import data_summary, export_user_data
from scribe.style.grammatical_gender import own_texts
from scribe.telegram.control_bot import grammatical_gender as gender_bot
from scribe.telegram.control_bot.account_command import summary_text
from scribe.telegram.control_bot.grammatical_gender import GenderPrompter, ask_once, new_pacer
from scribe.telegram.control_bot.setup_flow import SetupStates
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import (
    RecordingSession,
    callback_update,
    offline_bot,
    text_update,
)
from tests.integration.test_us1_setup_flow import Setup, _rules
from tests.integration.us1_support import _block, grounded_reply
from tests.integration.us2_support import US2Harness, button_texts, callback_datas
from tests.integration.us4_support import add_own_messages, add_profile, make_harness

FEMININE = ["Я сделала, проверь", "я уже отправила отчёт", "я готова обсудить завтра"]
MASCULINE = ["Я сделал, проверь", "я уже отправил отчёт", "я готов обсудить завтра"]
METHOD = SendMessage(chat_id=1, text="synthetic")
NEUTRAL = ["Ок, гляну", "Привет! Отправлю вечером", "Сделано"]
QUESTION_EN = t("gender.question", "en")
PLAIN_BUTTONS_EN = ["Masculine: «я сделал»", "Feminine: «я сделала»"]
PLAIN_DATAS = ["setgender:masculine", "setgender:feminine"]


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


async def _stored(session_factory: async_sessionmaker[AsyncSession], user: User) -> User:
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None
        await session.refresh(stored)
        return stored


async def _profile_with_examples(
    session_factory: async_sessionmaker[AsyncSession], user: User, examples: list[str]
) -> None:
    async with session_factory() as session:
        await profiles_repo.create_profile(
            session,
            user.id,
            scope=ProfileScope.global_,
            rules_json={"formality": 2},
            summary_text="Synthetic summary.",
            examples=examples,
            sample_size=len(examples),
        )
        await session.commit()


async def _no_sleep(seconds: float) -> None:
    """Pacing in tests: the frozen clock never moves, so nothing to wait for."""


async def _claimed(
    session_factory: async_sessionmaker[AsyncSession], user: User, clock: FrozenClock
) -> datetime:
    token = await gender_bot.claim(session_factory, user.id, clock())
    assert token is not None
    return token


def _assert_value_not_logged(log_capture: LogCapture) -> None:
    raw = log_capture.raw
    assert "masculine" not in raw and "feminine" not in raw


def _assert_asked_without_value(user: User, clock: FrozenClock) -> None:
    assert user.grammatical_gender is None  # never stored without the user
    assert user.grammatical_gender_prompted_at == clock()
    assert user.grammatical_gender_prompt_not_before is None


# --- the question ---------------------------------------------------------------------------------


def test_question_with_and_without_a_suggestion() -> None:
    text, markup = gender_bot.question(None, "en")
    assert text == QUESTION_EN
    assert button_texts(markup) == PLAIN_BUTTONS_EN
    assert callback_datas(markup) == PLAIN_DATAS

    text, markup = gender_bot.question("feminine", "en")
    assert text == QUESTION_EN + "\nYour messages suggest the feminine form."
    assert button_texts(markup) == ["Feminine: «я сделала» · suggested", "Masculine: «я сделал»"]
    assert callback_datas(markup) == ["setgender:feminine", "setgender:masculine"]

    text, markup = gender_bot.question("masculine", "ru")
    assert text == t("gender.question", "ru") + "\nПо вашим сообщениям похоже на мужской род."
    assert button_texts(markup) == ["Мужской: «я сделал» · предлагаем", "Женский: «я сделала»"]


# --- /settings ------------------------------------------------------------------------------------


async def test_settings_show_and_change_the_gender_in_place(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id

    await harness.feed(text_update(chat_id, "/settings"))
    (menu,) = harness.messages()
    assert "👤 Gender in drafts: not set (drafts follow your messages)" in menu.text.split("\n")
    assert "👤 Gender in drafts" in button_texts(menu.reply_markup)

    # 👤 turns the /settings message into the two choices ...
    await harness.feed(callback_update(chat_id, "settings:gender"))
    (choice,) = harness.edits()
    assert choice.text == t("settings.gender_prompt", "en")
    assert callback_datas(choice.reply_markup) == [
        "setgender:masculine:settings",
        "setgender:feminine:settings",
    ]
    assert button_texts(choice.reply_markup) == PLAIN_BUTTONS_EN
    assert len(harness.messages()) == 1  # nothing new sent

    # ... and a press renders /settings again in place, like the other switches.
    await harness.feed(callback_update(chat_id, "setgender:feminine:settings"))
    stored = await _stored(session_factory, user)
    assert stored.grammatical_gender == "feminine"
    assert stored.grammatical_gender_prompted_at is not None  # the one-off question is moot
    rerendered = harness.edits()[-1]
    lines = (rerendered.text or "").split("\n")
    assert "👤 Gender in drafts: feminine («я сделала»)" in lines
    assert "👤 Gender in drafts" in button_texts(rerendered.reply_markup)
    assert harness.api.of_type(AnswerCallbackQuery)[-1].text is None  # no popup
    assert len(harness.messages()) == 1

    await harness.feed(callback_update(chat_id, "setgender:neutral:settings"))  # not a value
    assert (await _stored(session_factory, user)).grammatical_gender == "feminine"
    _assert_value_not_logged(log_capture)
    assert [e["fields"] for e in log_capture.events("settings_changed")] == [["grammatical_gender"]]


async def test_russian_settings(harness: US2Harness, make_user: MakeUser) -> None:
    user = await make_user(state=UserState.active, language="ru", grammatical_gender="feminine")
    chat_id = user.telegram_control_chat_id
    await harness.feed(text_update(chat_id, "/settings"))
    menu = harness.messages()[-1]
    assert "👤 Род в черновиках: женский («я сделала»)" in menu.text.split("\n")
    assert "👤 Род в черновиках" in button_texts(menu.reply_markup)
    await harness.feed(callback_update(chat_id, "settings:gender"))
    choice = harness.edits()[-1]
    assert button_texts(choice.reply_markup) == ["Мужской: «я сделал»", "✅ Женский: «я сделала»"]


# --- drafting ---------------------------------------------------------------------------------


async def test_every_draft_carries_the_gender_for_global_and_contact_profiles(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, grammatical_gender="feminine")
    ivan = await make_contact(user, display_name="Ivan")
    ivan_chat = await make_chat(user, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    anna = await make_contact(user, display_name="Anna")
    anna_chat = await make_chat(user, telegram_chat_id=anna.telegram_peer_id, title="Anna")
    global_profile = await add_profile(session_factory, user)
    ivan_profile = await add_profile(session_factory, user, contact=ivan)

    await harness.incoming(user, ivan_chat, ivan, "Synthetic question 1")
    await harness.tick()
    await harness.incoming(user, anna_chat, anna, "Synthetic question 2")
    await harness.tick()
    calls = fake_llm.calls_for("compose.draft")
    assert len(calls) == 2
    drafts = await harness.drafts(user)
    assert {d.style_profile_id for d in drafts} == {ivan_profile.id, global_profile.id}
    for call in calls:
        assert _block(call, "user") == {"grammatical_gender": "feminine"}
        assert "grammatical_gender" not in str(_block(call, "profile"))


# --- setup --------------------------------------------------------------------------------------


@pytest.fixture
def setup(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> Setup:
    fake_llm.set_response(
        "profile.build",
        StyleProfileOutput(rules_json=_rules(), summary_text="Short.", chosen_example_indices=[0]),
    )
    return Setup(session_factory, settings, llm_router, gateway, clock)


async def _finish_setup(
    setup: Setup,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    user: User,
    texts: list[str],
    *,
    forwarded: bool = False,
) -> None:
    gateway.own_history[(user.id, -1003)] = [
        IncomingMessage(
            user_id=user.id,
            chat_id=-1003,
            message_id=i + 1,
            sender_peer_id=1,
            sender_display_name="",
            text=text,
            has_media=False,
            sent_at=clock() - timedelta(days=i + 1),
            is_outgoing=True,
            is_forwarded=forwarded,
        )
        for i, text in enumerate(texts)
    ]
    context = setup.dispatcher.fsm.get_context(setup.bot, setup.chat_id, setup.chat_id)
    await context.set_state(SetupStates.pick_chats)
    await context.set_data(
        {"dialogs": [[-1003, "Synthetic chat", "private"]], "selected": [], "page": 0}
    )
    await setup.press("pickchat:-1003")
    await setup.press("pickdone:0")
    await setup.press("setuptz:keep")


@pytest.mark.parametrize(
    ("texts", "suggested"),
    [(FEMININE, "feminine"), (MASCULINE, "masculine")],
)
async def test_setup_asks_with_the_suggestion_first_and_stores_only_the_answer(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    log_capture: LogCapture,
    settings: Settings,
    texts: list[str],
    suggested: str,
) -> None:
    user = await make_user(control_chat_id=setup.chat_id)
    await _finish_setup(setup, gateway, clock, user, texts)
    stored = await _stored(session_factory, user)
    assert stored.state == UserState.active
    _assert_asked_without_value(stored, clock)
    assert "first draft will appear here" in setup.api.sent_texts[-2]
    question = setup.api.of_type(SendMessage)[-1]
    expected_text, expected_markup = gender_bot.question(suggested, "en")  # type: ignore[arg-type]
    assert question.text == expected_text
    assert callback_datas(question.reply_markup)[0] == f"setgender:{suggested}"
    assert button_texts(question.reply_markup)[0].endswith(" · suggested")
    _assert_value_not_logged(log_capture)
    (event,) = log_capture.events("grammatical_gender_prompt")
    assert event["suggested"] is True and event["source"] == "setup" and event["result"] == "sent"

    # The job never asks a user who went through setup with this feature.
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 0 and api.sent_texts == []

    other = "masculine" if suggested == "feminine" else "feminine"
    await setup.press(f"setgender:{other}")  # the user may pick the other one
    assert (await _stored(session_factory, user)).grammatical_gender == other
    assert setup.api.sent_texts[-1] == t(f"gender.saved.{other}", "en")
    edit = setup.api.of_type(EditMessageReplyMarkup)[-1]
    assert f"✅ {t(f'btn.gender.{other}', 'en')}" in button_texts(edit.reply_markup)


async def test_setup_without_a_signal_asks_the_plain_question(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user(control_chat_id=setup.chat_id, language="ru")
    await _finish_setup(setup, gateway, clock, user, [*NEUTRAL, MASCULINE[0], FEMININE[0]])
    _assert_asked_without_value(await _stored(session_factory, user), clock)
    question = setup.api.of_type(SendMessage)[-1]
    assert question.text == t("gender.question", "ru")
    assert callback_datas(question.reply_markup) == PLAIN_DATAS

    await setup.press("setgender:feminine")
    assert (await _stored(session_factory, user)).grammatical_gender == "feminine"
    assert setup.api.sent_texts[-1] == t("gender.saved.feminine", "ru")


async def test_setup_ignores_forwarded_messages(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    """#47: a message the user forwarded is someone else's words: no suggestion from it."""
    user = await make_user(control_chat_id=setup.chat_id)
    await _finish_setup(setup, gateway, clock, user, FEMININE * 2, forwarded=True)
    question = setup.api.of_type(SendMessage)[-1]
    assert question.text == QUESTION_EN
    assert callback_datas(question.reply_markup) == PLAIN_DATAS
    _assert_asked_without_value(await _stored(session_factory, user), clock)


async def test_setup_does_not_ask_when_a_value_is_set(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user(control_chat_id=setup.chat_id, grammatical_gender="masculine")
    await _finish_setup(setup, gateway, clock, user, FEMININE)
    stored = await _stored(session_factory, user)
    assert stored.grammatical_gender == "masculine"
    assert "first draft will appear here" in setup.api.sent_texts[-1]


async def test_setup_failure_leaves_the_user_to_the_job(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic failure before the send")

    monkeypatch.setattr(gender_bot, "question", broken)
    user = await make_user(control_chat_id=setup.chat_id)
    await _finish_setup(setup, gateway, clock, user, FEMININE)
    stored = await _stored(session_factory, user)
    assert stored.state == UserState.active
    assert stored.grammatical_gender_prompted_at is None
    assert stored.grammatical_gender_prompt_not_before is None  # released
    assert "first draft will appear here" in setup.api.sent_texts[-1]

    monkeypatch.undo()
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1 and api.sent_texts == [QUESTION_EN]
    _assert_asked_without_value(await _stored(session_factory, user), clock)


async def test_job_running_right_after_activation_does_not_ask_twice(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim goes with the switch to active, so the job skips the user in between."""
    job_runs: list[int] = []
    original = gender_bot.after_setup

    async def job_first(*args: object, **kwargs: object) -> gender_bot.Delivery:
        reached, api, _ = await _job(session_factory, settings, clock)
        job_runs.append(len(api.sent_texts))
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gender_bot, "after_setup", job_first)
    user = await make_user(control_chat_id=setup.chat_id)
    await _finish_setup(setup, gateway, clock, user, FEMININE)
    assert job_runs == [0]
    assert setup.api.sent_texts.count(gender_bot.question("feminine", "en")[0]) == 1


async def test_stale_button_during_setup_is_refused(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(control_chat_id=setup.chat_id)  # setting_up
    await setup.press("setgender:feminine")
    stored = await _stored(session_factory, user)
    assert stored.grammatical_gender is None and stored.grammatical_gender_prompted_at is None
    (answer,) = setup.api.of_type(AnswerCallbackQuery)
    assert answer.text == t("common.not_set_up", "en")
    assert setup.api.sent_texts == []


# --- users set up before this feature -------------------------------------------------------------


async def _job(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    api_outcomes: list[BaseException | None] | None = None,
) -> tuple[int, RecordingSession, list[float]]:
    bot, api = offline_bot()
    api.outcomes = list(api_outcomes or [])
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    pacer = new_pacer(settings, clock, sleep)
    asked = await GenderPrompter(session_factory, bot, pacer, clock=clock, sleep=sleep).run()
    return asked, api, slept


async def test_existing_user_is_asked_once_with_the_suggestion(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active, language="ru")
    chat = await make_chat(user)
    await add_own_messages(session_factory, user, chat, 2, at=clock(), text=FEMININE[0])
    await add_own_messages(session_factory, user, chat, 1, at=clock(), text=FEMININE[1])

    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1
    _assert_asked_without_value(await _stored(session_factory, user), clock)
    (question,) = api.of_type(SendMessage)
    assert int(question.chat_id) == user.telegram_control_chat_id
    assert question.text == gender_bot.question("feminine", "ru")[0]
    assert callback_datas(question.reply_markup) == ["setgender:feminine", "setgender:masculine"]

    assert (await _job(session_factory, settings, clock))[0] == 0  # once
    _assert_value_not_logged(log_capture)
    for text in FEMININE:
        assert text not in log_capture.raw


async def test_profile_examples_are_not_used(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    """A profile refresh learns from agent sends too: only stored hand-written messages count."""
    user = await make_user(state=UserState.active)
    await _profile_with_examples(session_factory, user, FEMININE)
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1
    (question,) = api.of_type(SendMessage)
    assert question.text == QUESTION_EN
    assert callback_datas(question.reply_markup) == PLAIN_DATAS


async def test_messages_sent_through_the_agent_are_not_counted(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Their wording was the drafting model's guess, the very thing the setting replaces."""
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    async with session_factory() as session:
        turn = ConversationTurn(
            user_id=user.id,
            work_chat_id=chat.id,
            contact_id=contact.id,
            arrived_at=clock(),
            delay_s=0,
            due_at=clock(),
        )
        session.add(turn)
        await session.flush()
        for n, text in enumerate(MASCULINE, start=1):
            session.add(
                Message(
                    user_id=user.id,
                    work_chat_id=chat.id,
                    telegram_message_id=n,
                    is_outgoing=True,
                    text=text,
                    sent_at=clock(),
                )
            )
            session.add(
                Draft(
                    user_id=user.id,
                    conversation_turn_id=turn.id,
                    version=n,
                    text=text,
                    state=DraftState.sent,
                    prepared_at=clock(),
                    remind_at=clock(),
                    expires_at=clock(),
                    approved_by_user_at=clock(),
                    sent_telegram_message_id=n,
                )
            )
        session.add(
            Message(
                user_id=user.id,
                work_chat_id=chat.id,
                telegram_message_id=100,
                is_outgoing=True,
                text=FEMININE[0],
                sent_at=clock(),
            )
        )
        await session.commit()
        texts = await own_texts(session, user.id)
    assert texts == [FEMININE[0]]


async def test_stored_forwarded_messages_are_not_counted(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.active)
    chat = await make_chat(user)
    async with session_factory() as session:
        for n, text in enumerate(FEMININE * 2, start=1):
            await messages_repo.add_message(
                session,
                user.id,
                work_chat_id=chat.id,
                telegram_message_id=n,
                text=f"{text} {n}",
                sent_at=clock(),
                is_outgoing=True,
                is_forwarded=True,
            )
        await session.commit()
        assert await own_texts(session, user.id) == []
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1
    (question,) = api.of_type(SendMessage)
    assert question.text == QUESTION_EN


async def test_agent_send_is_per_chat(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """The same Telegram message id in two chats: an agent send in one, written by hand in the
    other. Only the agent send is left out (review: the predicate once lost the chat)."""
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    agent_chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    hand_chat = await make_chat(user)
    async with session_factory() as session:
        turn = ConversationTurn(
            user_id=user.id,
            work_chat_id=agent_chat.id,
            contact_id=contact.id,
            arrived_at=clock(),
            delay_s=0,
            due_at=clock(),
        )
        session.add(turn)
        await session.flush()
        session.add(
            Draft(
                user_id=user.id,
                conversation_turn_id=turn.id,
                text=MASCULINE[0],
                state=DraftState.sent,
                prepared_at=clock(),
                remind_at=clock(),
                expires_at=clock(),
                approved_by_user_at=clock(),
                sent_telegram_message_id=7,
            )
        )
        for chat, text in ((agent_chat, MASCULINE[0]), (hand_chat, FEMININE[0])):
            await messages_repo.add_message(
                session,
                user.id,
                work_chat_id=chat.id,
                telegram_message_id=7,
                text=text,
                sent_at=clock(),
                is_outgoing=True,
            )
        await session.commit()
        assert await own_texts(session, user.id) == [FEMININE[0]]
        assert await events_module._is_agent_send(session, user.id, agent_chat, 7)
        assert not await events_module._is_agent_send(session, user.id, hand_chat, 7)


async def test_job_skips_users_not_due(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    await make_user()  # still setting up
    await make_user(state=UserState.active, grammatical_gender="masculine")  # set in /settings
    await make_user(state=UserState.active, grammatical_gender_prompted_at=clock())
    await make_user(state=UserState.active, access_suspended_at=clock())
    # a live claim or a pending backoff
    later = clock() + timedelta(minutes=1)
    await make_user(state=UserState.active, grammatical_gender_prompt_not_before=later)
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 0 and api.sent_texts == []


@pytest.mark.parametrize(
    ("error", "result"),
    [
        (TelegramForbiddenError(METHOD, "bot was blocked by the user"), "blocked"),
        (TelegramBadRequest(METHOD, "message is too long"), "failed"),
    ],
)
async def test_undeliverable_users_are_marked_and_never_retried(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    log_capture: LogCapture,
    error: BaseException,
    result: str,
) -> None:
    user = await make_user(state=UserState.active)
    reached, _, _ = await _job(session_factory, settings, clock, [error])
    assert reached == 0
    _assert_asked_without_value(await _stored(session_factory, user), clock)
    (event,) = log_capture.events("grammatical_gender_prompt")
    assert event["result"] == result
    clock.advance(days=2)
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 0 and api.sent_texts == []


@pytest.mark.parametrize(
    "error",
    [
        TelegramNetworkError(METHOD, "synthetic connection refused"),
        TelegramServerError(METHOD, "synthetic 502"),
    ],
)
async def test_retryable_errors_back_off_and_stop_the_run(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    error: BaseException,
) -> None:
    """A service that starts before the network is up asks nobody and marks nobody."""
    first = await make_user(state=UserState.active, created_at=clock() - timedelta(hours=1))
    second = await make_user(state=UserState.active)
    reached, api, _ = await _job(session_factory, settings, clock, [error])
    assert reached == 0 and len(api.failed) == 1 and api.sent_texts == []  # the run stopped
    failed = await _stored(session_factory, first)
    assert failed.grammatical_gender_prompted_at is None  # not asked
    assert failed.grammatical_gender_prompt_attempts == 1
    assert failed.grammatical_gender_prompt_not_before == clock() + backoff(1)
    untouched = await _stored(session_factory, second)
    assert untouched.grammatical_gender_prompt_not_before is None
    assert untouched.grammatical_gender_prompted_at is None

    # Next run: the second one is asked, the first one only after its backoff.
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1 and len(api.sent_texts) == 1
    clock.advance(seconds=backoff(1).total_seconds())
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1
    _assert_asked_without_value(await _stored(session_factory, first), clock)


async def test_flood_wait_hands_the_user_back_until_it_is_over(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    """A RetryAfter longer than the lease must not let another run ask too: the claim is
    handed back with not_before at the end of the wait, and the run stops."""
    first = await make_user(state=UserState.active, created_at=clock() - timedelta(hours=1))
    second = await make_user(state=UserState.active)
    wait_s = int(gender_bot.CLAIM_LEASE.total_seconds()) * 3
    retry = TelegramRetryAfter(METHOD, "Too Many Requests", retry_after=wait_s)
    reached, api, slept = await _job(session_factory, settings, clock, [retry])
    assert reached == 0 and api.sent_texts == [] and slept == []
    stored = await _stored(session_factory, first)
    assert stored.grammatical_gender_prompted_at is None
    assert stored.grammatical_gender_prompt_attempts == 0  # not an error of ours
    end = clock() + timedelta(seconds=wait_s + RETRY_AFTER_MARGIN_S)
    assert stored.grammatical_gender_prompt_not_before == end
    assert (await _stored(session_factory, second)).grammatical_gender_prompt_not_before is None

    clock.advance(seconds=gender_bot.CLAIM_LEASE.total_seconds() + 60)  # past a lease, not the wait
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1 and len(api.sent_texts) == 1  # only the second user
    assert (await _stored(session_factory, first)).grammatical_gender_prompted_at is None
    clock.advance(seconds=wait_s)
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1
    _assert_asked_without_value(await _stored(session_factory, first), clock)


async def test_job_runs_one_at_a_time(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    await make_user(state=UserState.active)
    bot, api = offline_bot()
    entered = asyncio.Event()
    go_on = asyncio.Event()

    class HeldPacer:
        async def wait(self, chat_id: int) -> None:
            entered.set()
            await go_on.wait()

    prompter = gender_bot.GenderPrompter(
        session_factory,
        bot,
        HeldPacer(),  # type: ignore[arg-type]
        clock=clock,
    )
    first = asyncio.create_task(prompter.run())
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert await prompter.run() == 0  # a run is in progress: skipped at once
    go_on.set()
    assert await first == 1
    assert len(api.sent_texts) == 1


async def test_expired_claim_is_not_used_to_send(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    """A sender whose lease expired (and another run claimed the user) sends nothing."""
    user = await make_user(state=UserState.active)
    bot, api = offline_bot()
    stale = await _claimed(session_factory, user, clock)

    class SlowPacer:
        async def wait(self, chat_id: int) -> None:  # longer than the lease; another run claims
            clock.advance(seconds=gender_bot.CLAIM_LEASE.total_seconds() + 1)
            assert await gender_bot.claim(session_factory, user.id, clock()) is not None

    delivery = await ask_once(
        session_factory,
        bot,
        user.id,
        None,
        stale,
        pacer=SlowPacer(),  # type: ignore[arg-type]
        clock=clock,
        source="stored",
    )
    assert delivery is gender_bot.Delivery.lost
    assert api.sent_texts == []
    stored = await _stored(session_factory, user)
    assert stored.grammatical_gender_prompted_at is None
    assert stored.grammatical_gender_prompt_not_before == clock() + gender_bot.CLAIM_LEASE  # theirs


@pytest.mark.parametrize("change", ["lease_expired", "suspended", "back_in_setup"])
async def test_claim_no_longer_valid_sends_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    change: str,
) -> None:
    """The check right before the request: a live lease and a user who is still due."""
    user = await make_user(state=UserState.active)
    bot, api = offline_bot()
    token = await _claimed(session_factory, user, clock)

    class ChangingPacer:
        async def wait(self, chat_id: int) -> None:
            if change == "lease_expired":  # nobody else claimed it
                clock.advance(seconds=gender_bot.CLAIM_LEASE.total_seconds())
                return
            async with session_factory() as session:
                stored = await session.get(User, user.id)
                assert stored is not None
                if change == "suspended":
                    stored.access_suspended_at = clock()
                else:
                    stored.state = UserState.setting_up
                await session.commit()

    delivery = await ask_once(
        session_factory,
        bot,
        user.id,
        None,
        token,
        pacer=ChangingPacer(),  # type: ignore[arg-type]
        clock=clock,
        source="stored",
    )
    assert delivery is gender_bot.Delivery.lost
    assert api.sent_texts == []
    stored = await _stored(session_factory, user)
    assert stored.grammatical_gender_prompted_at is None
    assert stored.grammatical_gender_prompt_not_before is None  # given back


async def test_failed_hand_back_still_stops_the_run(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await make_user(state=UserState.active, created_at=clock() - timedelta(hours=1))
    second = await make_user(state=UserState.active)

    async def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("synthetic DB timeout")

    monkeypatch.setattr(gender_bot, "_attempts", broken)
    error = TelegramNetworkError(METHOD, "synthetic connection refused")
    reached, api, _ = await _job(session_factory, settings, clock, [error])
    assert reached == 0 and len(api.failed) == 1 and api.sent_texts == []  # stopped
    assert (await _stored(session_factory, first)).grammatical_gender_prompted_at is None
    untouched = await _stored(session_factory, second)
    assert untouched.grammatical_gender_prompt_not_before is None  # never tried


@pytest.mark.parametrize(
    ("retry_afters", "waited"), [([30], True), ([120], False), ([30, 30], False)]
)
async def test_setup_waits_out_a_short_flood_wait(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    retry_afters: list[int],
    waited: bool,
) -> None:
    """Setup keeps its in-memory suggestion through a flood wait of up to a minute."""
    user = await make_user(state=UserState.active)
    async with session_factory() as session:
        token = await gender_bot.claim_in(session, user.id, clock())
        await session.commit()
    assert token is not None
    bot, api = offline_bot()
    api.outcomes = [
        TelegramRetryAfter(METHOD, "Too Many Requests", retry_after=seconds)
        for seconds in retry_afters
    ]
    retry_after = retry_afters[-1]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    delivery = await gender_bot.after_setup(
        session_factory, bot, user.id, FEMININE, token, clock=clock, sleep=sleep
    )
    stored = await _stored(session_factory, user)
    if waited:
        assert delivery is gender_bot.Delivery.sent
        assert slept == [retry_after + RETRY_AFTER_MARGIN_S]
        assert api.sent_texts == [gender_bot.question("feminine", "en")[0]]
        _assert_asked_without_value(stored, clock)
    else:
        assert delivery is gender_bot.Delivery.flood_wait
        # at most one inline wait (a short one), then the user is left to the job
        assert slept == [s + RETRY_AFTER_MARGIN_S for s in retry_afters[:-1]]
        assert api.sent_texts == []
        end = clock() + timedelta(seconds=retry_after + RETRY_AFTER_MARGIN_S)
        assert stored.grammatical_gender_prompt_not_before == end  # left to the job


async def test_value_chosen_before_the_request_is_not_asked(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active)
    bot, api = offline_bot()

    class ChoosingPacer:
        async def wait(self, chat_id: int) -> None:  # the user picks in /settings meanwhile
            await gender_bot.choose(session_factory, user.id, "masculine", clock())

    delivery = await ask_once(
        session_factory,
        bot,
        user.id,
        "feminine",
        await _claimed(session_factory, user, clock),
        pacer=ChoosingPacer(),  # type: ignore[arg-type]
        clock=clock,
        source="stored",
    )
    assert delivery is gender_bot.Delivery.chosen
    assert api.sent_texts == []
    stored = await _stored(session_factory, user)
    assert stored.grammatical_gender == "masculine"
    assert stored.grammatical_gender_prompt_not_before is None
    assert [e["result"] for e in log_capture.events("grammatical_gender_prompt")] == ["chosen"]


async def test_failure_while_reading_releases_the_claim(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user(state=UserState.active)

    async def broken(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("synthetic decrypt error")

    monkeypatch.setattr(gender_bot, "own_texts", broken)
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 0 and api.sent_texts == []
    stored = await _stored(session_factory, user)
    assert stored.grammatical_gender_prompt_not_before is None
    assert stored.grammatical_gender_prompted_at is None
    monkeypatch.undo()
    assert (await _job(session_factory, settings, clock))[0] == 1


async def test_cancelled_before_the_request_releases_the_claim(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    class CancelledPacer:
        async def wait(self, chat_id: int) -> None:
            raise asyncio.CancelledError

    user = await make_user(state=UserState.active)
    bot, api = offline_bot()
    with pytest.raises(asyncio.CancelledError):
        await ask_once(
            session_factory,
            bot,
            user.id,
            "feminine",
            await _claimed(session_factory, user, clock),
            pacer=CancelledPacer(),  # type: ignore[arg-type]
            clock=clock,
            source="stored",
        )
    stored = await _stored(session_factory, user)
    assert api.sent_texts == []
    assert stored.grammatical_gender_prompt_not_before is None
    assert stored.grammatical_gender_prompted_at is None


async def test_cancelled_during_the_request_counts_as_asked(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active)
    bot, api = offline_bot()
    api.outcomes = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await ask_once(
            session_factory,
            bot,
            user.id,
            None,
            await _claimed(session_factory, user, clock),
            pacer=new_pacer(settings, clock, _no_sleep),
            clock=clock,
            source="stored",
        )
    _assert_asked_without_value(await _stored(session_factory, user), clock)
    assert [e["result"] for e in log_capture.events("grammatical_gender_prompt")] == ["unknown"]


async def test_marking_after_a_send_survives_cancellation(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user(state=UserState.active)
    bot, api = offline_bot()
    original = gender_bot._mark_asked
    marked = asyncio.Event()
    task: asyncio.Task[gender_bot.Delivery] | None = None

    async def slow(*args: object, **kwargs: object) -> None:
        assert task is not None
        task.cancel()  # shutdown right after Telegram accepted the question
        await asyncio.sleep(0.05)
        await original(*args, **kwargs)  # type: ignore[arg-type]
        marked.set()

    monkeypatch.setattr(gender_bot, "_mark_asked", slow)
    task = asyncio.create_task(
        ask_once(
            session_factory,
            bot,
            user.id,
            None,
            await _claimed(session_factory, user, clock),
            pacer=new_pacer(settings, clock, _no_sleep),
            clock=clock,
            source="stored",
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(marked.wait(), timeout=5)
    assert api.sent_texts == [QUESTION_EN]
    _assert_asked_without_value(await _stored(session_factory, user), clock)


async def test_marking_failure_after_the_send_asks_again_after_the_lease(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user(state=UserState.active)
    attempts: list[int] = []

    async def broken(*args: object, **kwargs: object) -> None:
        attempts.append(1)
        raise RuntimeError("synthetic DB timeout")

    monkeypatch.setattr(gender_bot, "_mark_asked", broken)
    reached, api, slept = await _job(session_factory, settings, clock)
    assert reached == 0 and len(api.sent_texts) == 1
    assert len(attempts) == gender_bot.STORE_ATTEMPTS and len(slept) >= 2
    stored = await _stored(session_factory, user)
    assert stored.grammatical_gender_prompted_at is None  # not marked as asked
    assert stored.grammatical_gender_prompt_not_before == clock() + gender_bot.CLAIM_LEASE
    assert (await _job(session_factory, settings, clock))[0] == 0  # live claim

    monkeypatch.undo()
    clock.advance(seconds=gender_bot.CLAIM_LEASE.total_seconds())
    reached, api, _ = await _job(session_factory, settings, clock)
    assert reached == 1
    _assert_asked_without_value(await _stored(session_factory, user), clock)


# --- /my_data ------------------------------------------------------------------------------------


async def test_my_data_shows_and_exports_the_gender(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active, grammatical_gender="feminine")
    async with session_factory() as session:
        export = await export_user_data(session, user.id, now=clock())
        summary = await data_summary(session, user.id, message_retention_days=30, now=clock())
    (row,) = export["users"]
    assert row["grammatical_gender"] == "feminine"
    assert summary.grammatical_gender == "feminine"
    text = summary_text(summary, "UTC", 30, "en")
    assert "👤 Gender in drafts: feminine («я сделала»)" in text.split("\n")
