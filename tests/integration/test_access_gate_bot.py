"""Control-bot access gate (005 T009, T013, T015a; contracts/control-chat.md).

With ``SCRIBE_ACCESS_GATE`` on: strangers are refused (one reply per cooldown, no user row),
beta testers and operators get the normal service, and existing users without access are
restricted to ``/my_data`` and ``/delete_my_data``. Offline Bot API, fake gateway; synthetic
ids only.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from aiogram import Dispatcher, F, Router
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, AnswerInlineQuery, SendDocument, SendMessage
from aiogram.types import CallbackQuery, InlineQuery, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.gate import AccessGateMiddleware
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    BetaEntry,
    RefusedContact,
    User,
    UserState,
)
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.payments.states import PaySupportStates
from scribe.telegram.control_bot.bot import UserContextMiddleware, create_dispatcher
from scribe.telegram.control_bot.connect_flow import ConnectStates
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import (
    RecordingSession,
    callback_update,
    offline_bot,
    refunded_payment_update,
    successful_payment_update,
    text_update,
)

STRANGER = 900000001
TESTER = 900000002
OPERATOR = 900000003
EXISTING = 900000004
COOLDOWN_S = 3600
_inline_ids = itertools.count(1)


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


def _refused(settings: Settings, lang: str = "en") -> str:
    return t("access.refused", lang)


def _ended(settings: Settings, lang: str = "en") -> str:
    return t("access.ended_reply", lang)


def _inline_update(user_id: int) -> Update:
    return Update(
        update_id=900_000 + next(_inline_ids),
        inline_query=InlineQuery(
            id=str(next(_inline_ids)),
            from_user=TgUser(id=user_id, is_bot=False, first_name="Synthetic"),
            query="synthetic",
            offset="",
        ),
    )


async def _user_count(db: AsyncSession, chat_id: int) -> int:
    count = await db.scalar(
        select(func.count()).select_from(User).where(User.telegram_control_chat_id == chat_id)
    )
    return int(count or 0)


async def _refused_row(db: AsyncSession, telegram_id: int) -> RefusedContact | None:
    db.expire_all()
    return await db.get(RefusedContact, telegram_id)


async def _add_beta(
    db: AsyncSession, *, telegram_id: int | None = None, username: str | None = None
) -> BetaEntry:
    entry = BetaEntry(
        telegram_id=telegram_id, username=username, added_by="operator", added_at=FrozenClock().now
    )
    db.add(entry)
    await db.flush()
    return entry


def _non_answer_requests(api: RecordingSession) -> list[object]:
    return [r for r in api.requests if not isinstance(r, AnswerCallbackQuery)]


# --- Refuse (T009) --------------------------------------------------------------------------------


async def test_refuse_start_once_per_cooldown_without_user_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)

    await dispatcher.feed_update(bot, text_update(STRANGER, "/start"))
    assert api.sent_texts == [_refused(gated)]
    assert await _user_count(db_session, STRANGER) == 0
    fsm = await dispatcher.fsm.get_context(bot, STRANGER, STRANGER).get_state()
    assert fsm is None

    # Within the cooldown: nothing is sent.
    api.clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, "/help"))
    await dispatcher.feed_update(bot, text_update(STRANGER, "hello there"))
    assert api.requests == []

    # After the cooldown: the text again.
    clock.advance(seconds=COOLDOWN_S + 1)
    await dispatcher.feed_update(bot, text_update(STRANGER, "/settings"))
    assert api.sent_texts == [_refused(gated)]

    row = await _refused_row(db_session, STRANGER)
    assert row is not None and row.attempts == 4
    assert await _user_count(db_session, STRANGER) == 0


async def test_refuse_callback_is_answered_silently(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)

    await dispatcher.feed_update(bot, callback_update(STRANGER, "mydata:export"))
    (answer,) = api.of_type(AnswerCallbackQuery)
    assert answer.text is None
    # The first refusal in the cooldown also explains in the chat.
    assert api.sent_texts == [_refused(gated)]

    api.clear()
    await dispatcher.feed_update(bot, callback_update(STRANGER, f"approve:{uuid.uuid4()}"))
    (answer,) = api.of_type(AnswerCallbackQuery)
    assert answer.text is None
    assert _non_answer_requests(api) == []
    assert await _user_count(db_session, STRANGER) == 0


async def test_refuse_inline_query_gets_empty_result(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(bot, _inline_update(STRANGER))
    (answer,) = api.of_type(AnswerInlineQuery)
    assert answer.results == [] and answer.cache_time == 0
    assert api.of_type(SendMessage) == []


async def test_refuse_ignores_group_chats(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(-100900000001, "/start", chat_type="group")
    )
    assert api.requests == []
    assert await db_session.scalar(select(func.count()).select_from(RefusedContact)) == 0


async def test_refuse_never_carries_the_subscription_line(
    session_factory: async_sessionmaker[AsyncSession], gated: Settings, clock: FrozenClock
) -> None:
    """Since US5 the subscription contact goes with the **Subscription** button's placeholder
    (tests/integration/test_beta_apply_bot.py), not with the refusal."""
    contact = "https://example.invalid/subscribe"
    with_contact = gated.model_copy(update={"subscription_contact": contact})
    bot, api = offline_bot()
    await _dispatcher(session_factory, with_contact, clock).feed_update(
        bot, text_update(STRANGER, "/start")
    )
    assert api.sent_texts == [_refused(gated)]
    assert contact not in api.sent_texts[0]


async def test_refuse_in_russian(
    session_factory: async_sessionmaker[AsyncSession], gated: Settings, clock: FrozenClock
) -> None:
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(STRANGER, "/start", language_code="ru")
    )
    assert api.sent_texts == [_refused(gated, "ru")]
    assert "закрытой бете" in api.sent_texts[0]


async def test_gate_off_start_creates_user(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    await _dispatcher(session_factory, settings, clock).feed_update(
        bot, text_update(STRANGER, "/start")
    )
    assert await _user_count(db_session, STRANGER) == 1
    assert api.sent_texts[0] == t("start.welcome")
    assert await db_session.scalar(select(func.count()).select_from(RefusedContact)) == 0


# --- Beta testers and operators get in (T013) ----------------------------------------------------


async def test_beta_entry_by_id_lets_next_start_in(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)
    await dispatcher.feed_update(bot, text_update(TESTER, "/start"))
    assert api.sent_texts == [_refused(gated)]

    await _add_beta(db_session, telegram_id=TESTER)
    api.clear()
    await dispatcher.feed_update(bot, text_update(TESTER, "/start"))
    assert api.sent_texts[0] == t("start.welcome")
    user = await users_repo.get_by_control_chat(db_session, TESTER)
    assert user is not None and user.state == UserState.setting_up


async def test_beta_entry_by_username_links_ignoring_case(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    entry = await _add_beta(db_session, username="beta_tester_1")  # added as @Beta_Tester_1
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(TESTER, "/start", username="Beta_Tester_1")
    )
    assert api.sent_texts[0] == t("start.welcome")
    await db_session.refresh(entry)
    assert entry.telegram_id == TESTER and entry.linked_at == clock.now
    kinds = (
        await db_session.scalars(select(AccessEvent.kind).where(AccessEvent.telegram_id == TESTER))
    ).all()
    assert kinds == [AccessEventKind.beta_linked]


async def test_operator_passes_without_entry(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(OPERATOR, "/start")
    )
    assert api.sent_texts[0] == t("start.welcome")
    assert await _user_count(db_session, OPERATOR) == 1


async def test_beta_sender_in_russian_gets_russian_setup(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    await _add_beta(db_session, telegram_id=TESTER)
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(TESTER, "/start", language_code="ru")
    )
    assert api.sent_texts[0] == t("start.welcome", "ru")
    user = await users_repo.get_by_control_chat(db_session, TESTER)
    assert user is not None and user.language == "ru"


# --- Restricted (T015a) --------------------------------------------------------------------------


async def test_restricted_my_data_and_export_pass(
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user(control_chat_id=EXISTING, state=UserState.active)
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)

    await dispatcher.feed_update(bot, text_update(user.telegram_control_chat_id, "/my_data"))
    (summary,) = api.sent_texts
    assert summary.startswith(t("my_data.title"))

    await dispatcher.feed_update(
        bot, callback_update(user.telegram_control_chat_id, "mydata:export")
    )
    assert len(api.of_type(SendDocument)) == 1


async def test_restricted_delete_my_data_completes(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    await make_user(control_chat_id=EXISTING, state=UserState.active)
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)

    await dispatcher.feed_update(bot, text_update(EXISTING, "/delete_my_data"))
    assert "DELETE" in api.sent_texts[-1]
    await dispatcher.feed_update(bot, text_update(EXISTING, "DELETE"))
    assert api.sent_texts[-1] == t("delete.done")
    assert await _user_count(db_session, EXISTING) == 0

    # /cancel in the confirmation step passes too.
    await make_user(control_chat_id=EXISTING + 1, state=UserState.active)
    api.clear()
    await dispatcher.feed_update(bot, text_update(EXISTING + 1, "/delete_my_data"))
    await dispatcher.feed_update(bot, text_update(EXISTING + 1, "/cancel"))
    assert api.sent_texts[-1] == t("delete.cancelled")


@pytest.mark.parametrize("command", ["/my_data", "/delete_my_data"])
async def test_restricted_data_commands_work_mid_setup(
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    command: str,
) -> None:
    """A user who lost access in the middle of setup (e.g. at the Telegram connect step) can
    still reach their data: the unfinished flow is dropped first (FR-011)."""
    await make_user(control_chat_id=EXISTING, state=UserState.setting_up)
    storage = PostgresFSMStorage(session_factory)
    key = StorageKey(bot_id=123456, chat_id=EXISTING, user_id=EXISTING)
    await storage.set_state(key, ConnectStates.connect_choose)
    bot, api = offline_bot()
    dispatcher = create_dispatcher(
        session_factory=session_factory,
        settings=gated,
        storage=storage,
        clock=clock,
        gateway=FakeMessagingGateway(),
    )

    await dispatcher.feed_update(bot, text_update(EXISTING, command))

    expected = t("my_data.title") if command == "/my_data" else "DELETE"
    assert any(expected in text for text in api.sent_texts), api.sent_texts
    state = await storage.get_state(key)
    assert state != ConnectStates.connect_choose.state


async def test_restricted_other_commands_get_ended_reply_once(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    await make_user(control_chat_id=EXISTING, state=UserState.active)
    bot, api = offline_bot()
    dispatcher = _dispatcher(session_factory, gated, clock)

    await dispatcher.feed_update(bot, text_update(EXISTING, "/settings"))
    assert api.sent_texts == [_ended(gated)]
    api.clear()
    await dispatcher.feed_update(bot, text_update(EXISTING, "/settings"))
    await dispatcher.feed_update(bot, text_update(EXISTING, "/start"))
    assert api.requests == []

    # A review button is answered silently and the review handler does not run.
    await dispatcher.feed_update(bot, callback_update(EXISTING, f"approve:{uuid.uuid4()}"))
    (answer,) = api.of_type(AnswerCallbackQuery)
    assert answer.text is None
    assert _non_answer_requests(api) == []

    clock.advance(seconds=COOLDOWN_S + 1)
    await dispatcher.feed_update(bot, text_update(EXISTING, "/settings"))
    assert api.sent_texts == [_ended(gated)]
    row = await _refused_row(db_session, EXISTING)
    assert row is not None and row.attempts == 5


async def test_restricted_user_with_beta_entry_is_served(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    await make_user(control_chat_id=EXISTING, state=UserState.active)
    await _add_beta(db_session, telegram_id=EXISTING)
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(EXISTING, "/start")
    )
    assert api.sent_texts == [t("start.already_set_up")]


async def test_refused_rows_hold_no_content(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
) -> None:
    bot, _ = offline_bot()
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(STRANGER, "Synthetic secret text", username="beta_tester_9")
    )
    row = await _refused_row(db_session, STRANGER)
    assert row is not None
    assert row.first_seen_at == clock.now and row.attempts == 1
    assert await db_session.scalar(select(func.count()).select_from(AccessEvent)) == 0


# --- payments allow-list (specs/015 T014, research P7) -----------------------------------------


def _stub_dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    reached: list[str],
) -> Dispatcher:
    """Only the user lookup, the gate and catch-all stubs: what the gate lets through is
    recorded in ``reached``, whatever the real routers would do with it."""
    dispatcher = Dispatcher(storage=PostgresFSMStorage(session_factory))
    middleware = UserContextMiddleware(session_factory)
    gate = AccessGateMiddleware(session_factory, settings, clock=clock)
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.outer_middleware(middleware)
        observer.outer_middleware(gate)
    router = Router(name="stub")

    async def message_stub(message: Message) -> None:
        reached.append(
            "successful_payment"
            if message.successful_payment
            else "refunded_payment"
            if message.refunded_payment
            else message.text or ""
        )

    async def callback_stub(callback: CallbackQuery) -> None:
        reached.append(f"callback:{callback.data}")
        await callback.answer()

    router.message.register(message_stub)
    router.callback_query.register(callback_stub, F.data)
    dispatcher.include_router(router)
    return dispatcher


@pytest.mark.parametrize(
    "update_name",
    ["successful_payment", "refunded_payment", "pay:offer", "/paysupport", "/terms", "state"],
)
async def test_payment_updates_pass_the_gate_without_the_refusal_cooldown(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
    update_name: str,
) -> None:
    reached: list[str] = []
    bot, api = offline_bot()
    dispatcher = _stub_dispatcher(session_factory, gated, clock, reached)
    if update_name == "successful_payment":
        update = successful_payment_update(STRANGER, f"sub1:{uuid.uuid4()}", "charge-gate-1")
    elif update_name == "refunded_payment":
        update = refunded_payment_update(STRANGER, f"sub1:{uuid.uuid4()}", "charge-gate-1")
    elif update_name == "pay:offer":
        update = callback_update(STRANGER, "pay:offer")
    elif update_name == "state":
        storage = PostgresFSMStorage(session_factory)
        key = StorageKey(bot_id=bot.id, chat_id=STRANGER, user_id=STRANGER)
        await storage.set_state(key, PaySupportStates.describe)
        update = text_update(STRANGER, "my payment did not go through")
    else:
        update = text_update(STRANGER, update_name)
    await dispatcher.feed_update(bot, update)

    assert len(reached) == 1  # the handler ran
    assert _non_answer_requests(api) == []  # no refusal text
    assert await _refused_row(db_session, STRANGER) is None  # the cooldown was not touched

    # A plain message from the same sender (out of any payments state) is still refused.
    await dispatcher.fsm.get_context(bot, STRANGER, STRANGER).clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, "hello there"))
    assert len(reached) == 1
    assert api.sent_texts == [_refused(gated)]
    row = await _refused_row(db_session, STRANGER)
    assert row is not None and row.attempts == 1
