"""Beta applications inside the bot (005 T036, US5; contracts/control-chat.md "Beta application
flow").

With the gate on, the "no access" reply offers **Join the beta** and **Subscription**. A person
without access describes their use case, sends the application, and every operator gets it
with **Approve** / **Reject**. Offline Bot API, fake gateway; synthetic ids and texts only.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, timedelta

import pytest
from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, InlineKeyboardMarkup, Message, PhotoSize, Sticker, Update
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import ApplyStates
from scribe.access.reconcile import reconcile_access
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    BetaApplication,
    BetaApplicationStatus,
    BetaEntry,
    BotFsmState,
    RefusedContact,
    User,
    UserState,
)
from scribe.db.repos import users as users_repo
from scribe.i18n import format_day_month_year, t
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import (
    NOW,
    RecordingSession,
    callback_update,
    offline_bot,
    text_update,
)

STRANGER = 900000101
SECOND = 900000102
TESTER = 900000103
OPERATOR = 900000104
OPERATOR_2 = 900000105
EXISTING = 900000106
COOLDOWN_S = 3600
LIMITS = {"access.apply_too_short": 10, "access.apply_too_long": 1000}
USE_CASE = "Synthetic use case: replies in two synthetic work chats about synthetic tasks."
_ids = itertools.count(1)


@pytest.fixture
def gated(settings: Settings) -> Settings:
    return settings.model_copy(update={"access_gate": True, "operator_telegram_ids": [OPERATOR]})


def _dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    gateway: FakeMessagingGateway | None = None,
) -> Dispatcher:
    return create_dispatcher(
        session_factory=session_factory,
        settings=settings,
        storage=PostgresFSMStorage(session_factory),
        clock=clock,
        gateway=gateway or FakeMessagingGateway(),
    )


def _media_update(chat_id: int, kind: str) -> Update:
    """A photo or a sticker (no text) from ``chat_id``."""
    extra: dict[str, object] = {}
    if kind == "photo":
        extra["photo"] = [PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)]
    else:
        extra["sticker"] = Sticker(
            file_id="s",
            file_unique_id="s",
            type="regular",
            width=1,
            height=1,
            is_animated=False,
            is_video=False,
        )
    return Update(
        update_id=800_000 + next(_ids),
        message=Message(
            message_id=800_000 + next(_ids),
            date=NOW,
            chat=Chat(id=chat_id, type="private"),
            from_user=TgUser(id=chat_id, is_bot=False, first_name="Synthetic"),
            **extra,  # type: ignore[arg-type]
        ),
    )


def _buttons(markup: object) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data or "" for row in markup.inline_keyboard for b in row]


def _urls(markup: object) -> list[str]:
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


def _sent(api: RecordingSession, chat_id: int | None = None) -> list[SendMessage]:
    return [m for m in api.of_type(SendMessage) if chat_id is None or m.chat_id == chat_id]


async def _applications(db: AsyncSession) -> list[BetaApplication]:
    result = await db.scalars(
        select(BetaApplication)
        .order_by(BetaApplication.consent_at)
        .execution_options(populate_existing=True)
    )
    return list(result.all())


async def _kinds(db: AsyncSession, telegram_id: int) -> list[AccessEventKind]:
    result = await db.scalars(
        select(AccessEvent.kind)
        .where(AccessEvent.telegram_id == telegram_id)
        .order_by(AccessEvent.at, AccessEvent.kind)
    )
    return list(result.all())


async def _state(dispatcher: Dispatcher, bot: object, chat_id: int) -> str | None:
    return await dispatcher.fsm.get_context(bot, chat_id, chat_id).get_state()  # type: ignore[arg-type]


async def _apply(
    dispatcher: Dispatcher,
    bot: object,
    chat_id: int,
    *,
    lang: str | None = None,
    username: str | None = "Synthetic_Applicant",
    use_case: str = USE_CASE,
) -> None:
    """Join the beta -> use case -> Send application."""
    feed = dispatcher.feed_update
    await feed(bot, callback_update(chat_id, "apply:start", language_code=lang))  # type: ignore[arg-type]
    await feed(bot, text_update(chat_id, use_case, language_code=lang))  # type: ignore[arg-type]
    await feed(
        bot,  # type: ignore[arg-type]
        callback_update(chat_id, "apply:send", language_code=lang, username=username),
    )


def _when(clock: FrozenClock) -> str:
    return f"{clock.now.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def _again(clock: FrozenClock, lang: str = "en") -> str:
    return format_day_month_year((clock.now + timedelta(days=30)).date(), lang)


# --- the refusal and the subscription placeholder ---------------------------------------------


async def test_refusal_has_two_buttons_and_no_link(
    session_factory: async_sessionmaker[AsyncSession], gated: Settings, clock: FrozenClock
) -> None:
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(STRANGER, "/start")
    )
    (sent,) = _sent(api)
    assert sent.text == t("access.refused")
    assert _buttons(sent.reply_markup) == ["apply:start", "apply:subinfo"]
    assert _urls(sent.reply_markup) == []
    assert "http" not in sent.text and "t.me" not in sent.text


async def test_subscription_placeholder_stores_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:subinfo"))
    assert len(api.of_type(AnswerCallbackQuery)) == 1
    assert api.sent_texts == [t("access.subscription_placeholder")]
    assert await _applications(db_session) == []
    assert await db_session.scalar(select(func.count()).select_from(RefusedContact)) == 0
    assert await db_session.get(BotFsmState, STRANGER) is None

    contact = "https://example.invalid/subscribe"
    with_contact = gated.model_copy(update={"subscription_contact": contact})
    bot, api = offline_bot()
    await _dispatcher(session_factory, with_contact, clock).feed_update(
        bot, callback_update(STRANGER, "apply:subinfo")
    )
    line = t("access.subscription_line", subscription_contact=contact)
    assert api.sent_texts == [f"{t('access.subscription_placeholder')}\n{line}"]
    # The refusal itself never carries the subscription line.
    api.clear()
    await _dispatcher(session_factory, with_contact, clock).feed_update(
        bot, text_update(SECOND, "/start")
    )
    assert api.sent_texts == [t("access.refused")]


# --- the flow ------------------------------------------------------------------------------------


async def test_use_case_validation_and_review(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)

    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:start"))
    (ask,) = _sent(api)
    assert ask.text == t("access.apply_ask")
    assert _buttons(ask.reply_markup) == ["apply:cancel"]
    assert await _state(dispatcher, bot, STRANGER) == ApplyStates.use_case.state

    for update, key in [
        (text_update(STRANGER, "short"), "access.apply_too_short"),
        (_media_update(STRANGER, "sticker"), "access.apply_text_only"),
        (_media_update(STRANGER, "photo"), "access.apply_text_only"),
        (text_update(STRANGER, "x" * 1001), "access.apply_too_long"),
    ]:
        api.clear()
        await dispatcher.feed_update(bot, update)
        assert api.sent_texts == [t(key, n=LIMITS.get(key, 0))], key
        assert await _state(dispatcher, bot, STRANGER) == ApplyStates.use_case.state

    api.clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, USE_CASE))
    (review,) = _sent(api)
    assert USE_CASE in review.text
    assert t("access.apply_consent") in review.text
    assert review.text == t("access.apply_review", use_case=USE_CASE) + "\n\n" + t(
        "access.apply_consent"
    )
    assert review.parse_mode is None
    assert _buttons(review.reply_markup) == ["apply:send", "apply:edit", "apply:cancel"]
    assert await _state(dispatcher, bot, STRANGER) == ApplyStates.review.state
    # Not stored before consent; the gate counted none of it.
    assert await _applications(db_session) == []
    assert await db_session.scalar(select(func.count()).select_from(RefusedContact)) == 0

    # Edit: back to the question.
    api.clear()
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:edit"))
    assert api.sent_texts == [t("access.apply_ask")]
    assert await _state(dispatcher, bot, STRANGER) == ApplyStates.use_case.state

    # Exactly 10 and 1000 characters are accepted.
    for text in ("x" * 10, "y" * 1000):
        await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:edit"))
        api.clear()
        await dispatcher.feed_update(bot, text_update(STRANGER, text))
        assert await _state(dispatcher, bot, STRANGER) == ApplyStates.review.state


async def test_cancel_by_button_and_command_stores_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)

    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:start"))
    await dispatcher.feed_update(bot, text_update(STRANGER, USE_CASE))
    api.clear()
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:cancel"))
    assert api.sent_texts == [t("access.apply_cancelled")]
    assert await _state(dispatcher, bot, STRANGER) is None

    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:start"))
    api.clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, "/cancel"))
    assert api.sent_texts == [t("access.apply_cancelled")]
    assert await _state(dispatcher, bot, STRANGER) is None

    # A stale Send button after the cancel does nothing.
    api.clear()
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:send"))
    assert _sent(api) == []
    assert await _applications(db_session) == []
    assert await _kinds(db_session, STRANGER) == []


async def test_other_commands_mid_flow_stay_refused(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    """Only text and /cancel reach the flow: /start never creates an account (FR-002)."""
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:start"))
    api.clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, "/start"))
    assert api.sent_texts == [t("access.refused")]
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.telegram_control_chat_id == STRANGER)
    )
    assert count == 0
    assert await _state(dispatcher, bot, STRANGER) == ApplyStates.use_case.state


async def test_send_stores_application_and_notifies_operator(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await dispatcher.feed_update(bot, text_update(STRANGER, "/start"))  # the refusal
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:start"))
    await dispatcher.feed_update(bot, text_update(STRANGER, USE_CASE))
    api.clear()
    await dispatcher.feed_update(
        bot, callback_update(STRANGER, "apply:send", username="Synthetic_Applicant")
    )

    (app,) = await _applications(db_session)
    assert app.telegram_id == STRANGER
    assert app.username == "synthetic_applicant"
    assert app.use_case == USE_CASE
    assert app.language == "en"
    assert app.consent_at == clock.now
    assert app.status == BetaApplicationStatus.pending
    assert app.decided_at is None and app.decided_by is None
    assert await _kinds(db_session, STRANGER) == [AccessEventKind.application_submitted]
    assert await _state(dispatcher, bot, STRANGER) is None

    assert [m.text for m in _sent(api, STRANGER)] == [t("access.apply_sent")]
    (to_operator,) = _sent(api, OPERATOR)
    assert to_operator.text == t(
        "access.apply_operator",
        who="@synthetic_applicant",
        telegram_id=STRANGER,
        language="en",
        use_case=USE_CASE,
    )
    assert to_operator.parse_mode is None
    assert _buttons(to_operator.reply_markup) == [
        f"betaapp:approve:{app.id}",
        f"betaapp:reject:{app.id}",
    ]
    assert app.operator_messages
    ((chat_id, message_id),) = app.operator_messages
    assert chat_id == OPERATOR and isinstance(message_id, int)

    # While pending: the pending notice (no buttons) under the cooldown, and no new application.
    clock.advance(seconds=COOLDOWN_S + 1)
    api.clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, "hello"))
    (pending,) = _sent(api)
    assert pending.text == t("access.apply_pending")
    assert pending.reply_markup is None
    api.clear()
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:start"))
    assert api.sent_texts == [t("access.apply_pending")]
    assert await _state(dispatcher, bot, STRANGER) is None
    assert len(await _applications(db_session)) == 1


async def test_send_without_username_and_without_operators(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    """No operator configured: the application is still stored (Edge Cases)."""
    no_operator = settings.model_copy(update={"access_gate": True})
    bot, api = offline_bot()
    await _apply(_dispatcher(session_factory, no_operator, clock), bot, STRANGER, username=None)
    (app,) = await _applications(db_session)
    assert app.username is None and app.operator_messages == []
    assert api.sent_texts[-1] == t("access.apply_sent")


async def test_approve_adds_beta_and_start_begins_setup(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await _apply(dispatcher, bot, STRANGER)
    (app,) = await _applications(db_session)
    ((_, op_message),) = app.operator_messages
    clock.advance(seconds=60)
    api.clear()

    await dispatcher.feed_update(
        bot, callback_update(OPERATOR, f"betaapp:approve:{app.id}", message_id=op_message)
    )

    (app,) = await _applications(db_session)
    assert app.status == BetaApplicationStatus.approved
    assert (app.decided_by, app.decided_at) == (OPERATOR, clock.now)
    entry = await db_session.scalar(select(BetaEntry).where(BetaEntry.telegram_id == STRANGER))
    assert entry is not None and entry.added_by == "operator" and entry.note == "application"
    first, *decision = await _kinds(db_session, STRANGER)
    assert first == AccessEventKind.application_submitted
    assert set(decision) == {AccessEventKind.application_approved, AccessEventKind.beta_added}
    added = await db_session.scalar(
        select(AccessEvent).where(AccessEvent.kind == AccessEventKind.beta_added)
    )
    assert added is not None and added.actor == "operator"

    assert [m.text for m in _sent(api, STRANGER)] == [t("access.apply_approved")]
    (edit,) = api.of_type(EditMessageText)
    assert (edit.chat_id, edit.message_id) == (OPERATOR, op_message)
    assert edit.reply_markup is None and edit.parse_mode is None
    decided = t("access.apply_operator_approved", by=str(OPERATOR), when=_when(clock))
    assert (edit.text or "").endswith(decided)
    assert USE_CASE in (edit.text or "")

    api.clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, "/start"))
    assert api.sent_texts[0] == t("start.welcome")
    user = await users_repo.get_by_control_chat(db_session, STRANGER)
    assert user is not None and user.state == UserState.setting_up


async def test_reject_blocks_for_30_days(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await _apply(dispatcher, bot, SECOND)
    (app,) = await _applications(db_session)
    api.clear()

    await dispatcher.feed_update(bot, callback_update(OPERATOR, f"betaapp:reject:{app.id}"))
    rejected_at = clock.now
    (app,) = await _applications(db_session)
    assert app.status == BetaApplicationStatus.rejected
    assert app.decided_by == OPERATOR
    assert await db_session.scalar(select(BetaEntry).where(BetaEntry.telegram_id == SECOND)) is None
    assert AccessEventKind.application_rejected in await _kinds(db_session, SECOND)
    assert [m.text for m in _sent(api, SECOND)] == [t("access.apply_rejected", date=_again(clock))]
    (edit,) = api.of_type(EditMessageText)
    assert (edit.text or "").endswith(
        t("access.apply_operator_rejected", by=str(OPERATOR), when=_when(clock))
    )

    clock.advance(seconds=COOLDOWN_S + 1)
    api.clear()
    await dispatcher.feed_update(bot, text_update(SECOND, "/start"))
    (sent,) = _sent(api)
    again = format_day_month_year((rejected_at + timedelta(days=30)).date(), "en")
    assert sent.text == t("access.apply_rejected_recently", date=again)
    assert sent.reply_markup is None
    api.clear()
    await dispatcher.feed_update(bot, callback_update(SECOND, "apply:start"))
    assert api.sent_texts == [t("access.apply_rejected_recently", date=again)]
    assert await _state(dispatcher, bot, SECOND) is None

    clock.now = rejected_at + timedelta(days=30, seconds=1)
    api.clear()
    await dispatcher.feed_update(bot, text_update(SECOND, "/start"))
    (sent,) = _sent(api)
    assert sent.text == t("access.refused")
    assert _buttons(sent.reply_markup) == ["apply:start", "apply:subinfo"]
    api.clear()
    await dispatcher.feed_update(bot, callback_update(SECOND, "apply:start"))
    assert api.sent_texts == [t("access.apply_ask")]
    await dispatcher.feed_update(bot, text_update(SECOND, USE_CASE))
    await dispatcher.feed_update(bot, callback_update(SECOND, "apply:send"))
    assert len(await _applications(db_session)) == 2


async def test_only_operators_decide(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    db_session.add(BetaEntry(telegram_id=TESTER, added_by="operator", added_at=clock.now))
    await db_session.commit()
    await _apply(dispatcher, bot, STRANGER)
    (app,) = await _applications(db_session)

    for presser in (TESTER, SECOND, STRANGER):
        api.clear()
        await dispatcher.feed_update(bot, callback_update(presser, f"betaapp:approve:{app.id}"))
        (answer,) = api.of_type(AnswerCallbackQuery)
        assert answer.text is None
        assert api.of_type(EditMessageText) == []
        # A beta tester's press reaches no handler; people without access may get the gate's
        # reply (the pending notice for the applicant), never a decision.
        assert t("access.apply_approved") not in api.sent_texts
        if presser == TESTER:
            assert _sent(api) == []
    (app,) = await _applications(db_session)
    assert app.status == BetaApplicationStatus.pending
    assert (
        await db_session.scalar(select(BetaEntry).where(BetaEntry.telegram_id == STRANGER)) is None
    )

    # An unknown application id: answered, nothing else.
    api.clear()
    await dispatcher.feed_update(bot, callback_update(OPERATOR, f"betaapp:approve:{uuid.uuid4()}"))
    assert len(api.of_type(AnswerCallbackQuery)) == 1
    assert _sent(api) == [] and api.of_type(EditMessageText) == []


async def test_two_operators_first_decision_wins(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    two = gated.model_copy(update={"operator_telegram_ids": [OPERATOR, OPERATOR_2]})
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, two, clock)
    await _apply(dispatcher, bot, STRANGER)
    (app,) = await _applications(db_session)
    copies = {chat_id: message_id for chat_id, message_id in app.operator_messages}
    assert set(copies) == {OPERATOR, OPERATOR_2}

    api.clear()
    await dispatcher.feed_update(
        bot, callback_update(OPERATOR, f"betaapp:approve:{app.id}", message_id=copies[OPERATOR])
    )
    edits = api.of_type(EditMessageText)
    assert {(e.chat_id, e.message_id) for e in edits} == set(copies.items())
    decided = t("access.apply_operator_approved", by=str(OPERATOR), when=_when(clock))
    assert all((e.text or "").endswith(decided) for e in edits)

    clock.advance(seconds=30)
    api.clear()
    await dispatcher.feed_update(
        bot,
        callback_update(OPERATOR_2, f"betaapp:reject:{app.id}", message_id=copies[OPERATOR_2]),
    )
    (app,) = await _applications(db_session)
    assert app.status == BetaApplicationStatus.approved and app.decided_by == OPERATOR
    assert _sent(api) == []
    (edit,) = api.of_type(EditMessageText)
    assert (edit.chat_id, edit.message_id) == (OPERATOR_2, copies[OPERATOR_2])
    assert (edit.text or "").endswith(decided) and edit.reply_markup is None
    assert (await _kinds(db_session, STRANGER)).count(AccessEventKind.application_approved) == 1
    assert AccessEventKind.application_rejected not in await _kinds(db_session, STRANGER)


async def test_approve_when_already_on_the_beta_list(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    """Added with ``beta add`` meanwhile: the application closes as approved, no second
    entry and no second ``beta_added``."""
    bot, _ = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await _apply(dispatcher, bot, STRANGER)
    (app,) = await _applications(db_session)
    db_session.add(BetaEntry(telegram_id=STRANGER, added_by="operator", added_at=clock.now))
    await db_session.commit()

    await dispatcher.feed_update(bot, callback_update(OPERATOR, f"betaapp:approve:{app.id}"))
    (app,) = await _applications(db_session)
    assert app.status == BetaApplicationStatus.approved
    count = await db_session.scalar(
        select(func.count()).select_from(BetaEntry).where(BetaEntry.telegram_id == STRANGER)
    )
    assert count == 1
    assert AccessEventKind.beta_added not in await _kinds(db_session, STRANGER)


async def test_restricted_user_applies_and_is_restored(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    gateway = FakeMessagingGateway()
    user = await make_user(control_chat_id=EXISTING, state=UserState.active)
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock, gateway)
    await reconcile_access(session_factory, gateway, bot, gated, clock=clock)  # suspends
    await db_session.refresh(user)
    assert user.access_suspended_at is not None

    clock.advance(seconds=COOLDOWN_S + 1)
    api.clear()
    await dispatcher.feed_update(bot, text_update(EXISTING, "/settings"))
    (sent,) = _sent(api)
    assert sent.text == t("access.ended_reply")
    assert _buttons(sent.reply_markup) == ["apply:start", "apply:subinfo"]

    await dispatcher.feed_update(bot, callback_update(EXISTING, "apply:start"))
    # /my_data still answers mid-application, and the application survives it.
    api.clear()
    await dispatcher.feed_update(bot, text_update(EXISTING, "/my_data"))
    assert api.sent_texts and api.sent_texts[0].startswith(t("my_data.title"))
    assert await _state(dispatcher, bot, EXISTING) == ApplyStates.use_case.state
    await dispatcher.feed_update(bot, text_update(EXISTING, USE_CASE))
    await dispatcher.feed_update(bot, callback_update(EXISTING, "apply:send"))
    (app,) = await _applications(db_session)
    assert app.telegram_id == EXISTING

    await dispatcher.feed_update(bot, callback_update(OPERATOR, f"betaapp:approve:{app.id}"))
    api.clear()
    result = await reconcile_access(session_factory, gateway, bot, gated, clock=clock)
    assert result.regained == 1
    await db_session.refresh(user)
    assert user.access_suspended_at is None
    assert t("access.restored_notice") in api.sent_texts[0]


async def test_flow_in_russian(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await dispatcher.feed_update(bot, text_update(STRANGER, "/start", language_code="ru"))
    assert api.sent_texts == [t("access.refused", "ru")]
    api.clear()
    await dispatcher.feed_update(
        bot, callback_update(STRANGER, "apply:subinfo", language_code="ru")
    )
    assert api.sent_texts == [t("access.subscription_placeholder", "ru")]
    api.clear()
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:start", language_code="ru"))
    assert api.sent_texts == [t("access.apply_ask", "ru")]
    api.clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, "коротко", language_code="ru"))
    assert api.sent_texts == [t("access.apply_too_short", "ru", n=10)]
    api.clear()
    use_case = "Синтетический сценарий: ответы в двух рабочих чатах о синтетических задачах."
    await dispatcher.feed_update(bot, text_update(STRANGER, use_case, language_code="ru"))
    assert t("access.apply_consent", "ru") in api.sent_texts[0]
    api.clear()
    await dispatcher.feed_update(bot, callback_update(STRANGER, "apply:send", language_code="ru"))
    assert api.sent_texts[0] == t("access.apply_sent", "ru")
    (app,) = await _applications(db_session)
    assert app.language == "ru"
    (to_operator,) = _sent(api, OPERATOR)
    assert "· ru" in to_operator.text and use_case in to_operator.text

    api.clear()
    await dispatcher.feed_update(bot, callback_update(OPERATOR, f"betaapp:reject:{app.id}"))
    assert [m.text for m in _sent(api, STRANGER)] == [
        t("access.apply_rejected", "ru", date=_again(clock, "ru"))
    ]


async def test_gate_off_apply_buttons_do_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, settings, clock)
    for data in ("apply:start", "apply:subinfo", f"betaapp:approve:{uuid.uuid4()}"):
        api.clear()
        await dispatcher.feed_update(bot, callback_update(STRANGER, data))
        assert _sent(api) == []
    assert await _state(dispatcher, bot, STRANGER) is None
    assert await _applications(db_session) == []


async def test_member_pressing_join_is_told_they_have_access(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    db_session.add(BetaEntry(telegram_id=TESTER, added_by="operator", added_at=clock.now))
    await db_session.commit()
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await dispatcher.feed_update(bot, callback_update(TESTER, "apply:start"))
    assert api.sent_texts == [t("access.apply_has_access")]
    assert await _state(dispatcher, bot, TESTER) is None
