"""Reconnect, disconnect and the ``/telegram`` status card (002 US3 T032, US4 T037).

Telegram login runs against a fake login client; the user's Telegram client is the fake
gateway. Synthetic data only. Most reconnect tests use the phone method; the QR variant is
``test_reconnect_by_qr_keeps_state_and_data``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import EditMessageText, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    LoginMethod,
    ProfileScope,
    SessionStatus,
    StyleProfile,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.connect_flow import (
    ConnectStates,
    login_activity_recorder,
)
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.login import (
    InvalidCode,
    LoginManager,
    LoginResult,
    QrCode,
    QrOutcome,
)
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser, next_telegram_id
from tests.integration.bot_support import (
    RecordingSession,
    callback_update,
    offline_bot,
    text_update,
)

PHONE = "+7 900 555-01-77"
CODE = "73915"
TG_USER_ID = 5151
OTHER_TG_USER_ID = 6262
QR_EXPIRES = datetime(2026, 10, 14, 10, 0, 30, tzinfo=UTC)


class FakeLoginClient:
    """``wait_qr`` takes the next scripted outcome; with none left the code just expires."""

    def __init__(
        self, telegram_user_id: int, display_name: str, qr_script: list[QrOutcome]
    ) -> None:
        self.telegram_user_id = telegram_user_id
        self.display_name = display_name
        self.qr_script = qr_script
        self.logged_out = False
        self.closed = False

    async def send_code(self, phone: str) -> None:
        return None

    async def sign_in_code(self, code: str) -> bool:
        if code != CODE:
            raise InvalidCode()
        return False

    async def sign_in_password(self, password: str) -> None:
        return None

    async def start_qr(self) -> QrCode:
        return QrCode(url="tg://login?token=SYNTHETIC", expires_at=QR_EXPIRES)

    async def refresh_qr(self) -> QrCode:
        return QrCode(url="tg://login?token=SYNTHETIC", expires_at=QR_EXPIRES)

    async def wait_qr(self, timeout_s: float) -> QrOutcome:
        return self.qr_script.pop(0) if self.qr_script else QrOutcome.expired

    async def log_out(self) -> None:
        self.logged_out = True

    async def complete(self) -> LoginResult:
        return LoginResult(
            session_string="synthetic-reconnected-session",
            telegram_user_id=self.telegram_user_id,
            display_name=self.display_name,
            username="synthetic_me",
            masked_phone="+7 ••• ••• •• 77",
        )

    async def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        llm_router: LLMRouter,
        gateway: FakeMessagingGateway,
        clock: FrozenClock,
    ) -> None:
        self.clients: list[FakeLoginClient] = []
        self.login_tg_user_id = TG_USER_ID
        self.login_display_name = "Synthetic Me"
        self.qr_script: list[QrOutcome] = []

        def factory() -> FakeLoginClient:
            client = FakeLoginClient(self.login_tg_user_id, self.login_display_name, self.qr_script)
            self.qr_script = []
            self.clients.append(client)
            return client

        self.bot, self.api = offline_bot()
        self.api_session: RecordingSession = self.api
        self.chat_id = next_telegram_id()
        self.login = LoginManager(
            factory, record=login_activity_recorder(session_factory), clock=clock
        )
        self.dispatcher = create_dispatcher(
            session_factory=session_factory,
            settings=settings,
            storage=PostgresFSMStorage(session_factory),
            llm_router=llm_router,
            gateway=gateway,
            client_pool=gateway,
            clock=clock,
            telegram_login=self.login,
        )

    async def say(self, text: str) -> None:
        await self.dispatcher.feed_update(self.bot, text_update(self.chat_id, text))

    async def press(self, data: str) -> None:
        await self.dispatcher.feed_update(self.bot, callback_update(self.chat_id, data))

    def sent(self) -> list[SendMessage]:
        return self.api.of_type(SendMessage)

    def last_text(self) -> str:
        texts = [
            r.text
            for r in self.api.requests
            if isinstance(r, SendMessage | EditMessageText) and r.text
        ]
        return texts[-1]


def callbacks(markup: object) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data or "" for row in markup.inline_keyboard for b in row]


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> Harness:
    return Harness(session_factory, settings, llm_router, gateway, clock)


async def _set_up_user(
    harness: Harness,
    db_session: AsyncSession,
    make_user: MakeUser,
    *,
    state: UserState = UserState.active,
    status: SessionStatus = SessionStatus.revoked,
    method: LoginMethod = LoginMethod.phone_code,
    connected_at: datetime = datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    timezone: str = "UTC",
    account_label: str | None = "Synthetic Me (@synthetic_me)",
) -> User:
    user = await make_user(
        control_chat_id=harness.chat_id,
        telegram_user_id=TG_USER_ID,
        display_name="Synthetic Me",
        state=state,
        timezone=timezone,
    )
    await users_repo.upsert_telegram_session(
        db_session,
        user.id,
        "synthetic-old-session",
        method=method,
        connected_at=connected_at,
        account_label=account_label,
    )
    if status != SessionStatus.active:
        await users_repo.set_telegram_session_status(db_session, user.id, status, "synthetic")
    await db_session.flush()
    return user


async def _reconnect_by_phone(harness: Harness) -> None:
    await harness.press("tgreconnect:0")
    assert harness.sent()[-1].text == t("connect.method_choice")
    await harness.press("tglogin:0:phone")
    assert harness.sent()[-1].text == t("connect.phone_prompt")
    await harness.say(PHONE)
    assert t("connect.code_prompt") in harness.api.sent_texts
    await harness.say(CODE)


async def _fresh_user(session: AsyncSession, user: User) -> User:
    row = await users_repo.get_user(session, user.id)
    assert row is not None
    await session.refresh(row)
    return row


# --- US3: reconnect ------------------------------------------------------------------------------


async def test_reconnect_by_phone_keeps_state_and_data(
    harness: Harness,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _set_up_user(harness, db_session, make_user)
    chat = await make_chat(user, title="Synthetic work chat")
    profile = await profiles_repo.create_profile(
        db_session,
        user.id,
        scope=ProfileScope.global_,
        rules_json={"formality": 2},
        summary_text="Short.",
        examples=["synthetic example"],
        sample_size=12,
    )
    old_tg = await users_repo.get_telegram_session(db_session, user.id)
    assert old_tg is not None
    old_tg_id = old_tg.id
    await db_session.flush()

    await _reconnect_by_phone(harness)

    assert (
        t("connect.reconnected", account="Synthetic Me (@synthetic_me)") in harness.api.sent_texts
    )
    assert t("setup.pick_prompt") not in harness.api.sent_texts
    assert user.id in gateway.started
    async with session_factory() as session:
        tg = await users_repo.get_telegram_session(session, user.id)
        assert tg is not None
        await session.refresh(tg)
        refreshed = await _fresh_user(session, user)
        chats = (
            (await session.execute(select(WorkChat).where(WorkChat.user_id == user.id)))
            .scalars()
            .all()
        )
        profiles = (
            (await session.execute(select(StyleProfile).where(StyleProfile.user_id == user.id)))
            .scalars()
            .all()
        )
        records = await activity_repo.list_recent(session, user.id)
    assert tg.id == old_tg_id
    assert tg.status == SessionStatus.active and tg.last_error is None
    assert tg.login_method == LoginMethod.phone_code and tg.connected_at == clock()
    assert tg.session_string == "synthetic-reconnected-session"
    assert refreshed.state == UserState.active
    assert [(c.id, c.title, c.enabled) for c in chats] == [(chat.id, "Synthetic work chat", True)]
    assert [(p.id, p.sample_size, p.is_current) for p in profiles] == [(profile.id, 12, True)]
    (record,) = [r for r in records if r.kind == ActivityKind.telegram_login]
    assert record.meta_json["purpose"] == "reconnect"
    assert record.meta_json["outcome"] == "connected"
    assert record.meta_json["method"] == "phone_code"


async def test_reconnect_by_qr_keeps_state_and_data(
    harness: Harness,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _set_up_user(harness, db_session, make_user, state=UserState.paused)
    chat = await make_chat(user, title="Synthetic work chat")
    old_tg = await users_repo.get_telegram_session(db_session, user.id)
    assert old_tg is not None
    old_tg_id = old_tg.id
    await db_session.flush()

    await harness.press("tgreconnect:0")
    assert harness.sent()[-1].text == t("connect.method_choice")
    harness.qr_script = [QrOutcome.expired, QrOutcome.authorized]  # scanned the second code
    await harness.press("tglogin:0:qr")
    attempt = harness.login.get(harness.chat_id)
    assert attempt is not None and attempt.task is not None
    await asyncio.wait({attempt.task})  # the QR loop runs to the end of the finish step

    assert (
        t("connect.reconnected", account="Synthetic Me (@synthetic_me)") in harness.api.sent_texts
    )

    assert len(harness.api.of_type(SendPhoto)) == 1
    assert t("setup.pick_prompt") not in harness.api.sent_texts
    assert user.id in gateway.started
    async with session_factory() as session:
        tg = await users_repo.get_telegram_session(session, user.id)
        assert tg is not None
        await session.refresh(tg)
        refreshed = await _fresh_user(session, user)
        chats = (
            (await session.execute(select(WorkChat).where(WorkChat.user_id == user.id)))
            .scalars()
            .all()
        )
        records = await activity_repo.list_recent(session, user.id)
    assert tg.id == old_tg_id
    assert tg.status == SessionStatus.active and tg.last_error is None
    assert tg.login_method == LoginMethod.qr and tg.connected_at == clock()
    assert tg.session_string == "synthetic-reconnected-session"
    assert refreshed.state == UserState.paused
    assert [c.id for c in chats] == [chat.id]
    (record,) = [r for r in records if r.kind == ActivityKind.telegram_login]
    assert record.meta_json["purpose"] == "reconnect"
    assert record.meta_json["outcome"] == "connected"
    assert record.meta_json["method"] == "qr" and record.meta_json["qr_codes_shown"] == 2
    (login,) = harness.clients
    assert login.closed and not login.logged_out


async def test_cancel_during_reconnect_points_to_telegram(
    harness: Harness, db_session: AsyncSession, make_user: MakeUser
) -> None:
    await _set_up_user(harness, db_session, make_user)
    await harness.press("tgreconnect:0")
    await harness.press("tglogin:0:phone")
    await harness.say("/cancel")
    assert harness.api.sent_texts[-1] == t("setup.reconnect_cancelled")
    assert "/telegram" in t("setup.reconnect_cancelled") and "/start" not in t(
        "setup.reconnect_cancelled"
    )


async def test_start_during_reconnect_restarts_with_same_purpose(
    harness: Harness,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _set_up_user(harness, db_session, make_user)
    await harness.press("tgreconnect:0")
    await harness.press("tglogin:0:phone")
    await harness.say(PHONE)
    await harness.say("/start")

    assert harness.api.sent_texts[-1] == t("connect.method_choice")
    (login,) = harness.clients
    assert login.closed
    async with session_factory() as session:
        records = await activity_repo.list_recent(session, user.id)
    assert [r.meta_json["outcome"] for r in records if r.kind == ActivityKind.telegram_login] == [
        "cancelled"
    ]
    state = harness.dispatcher.fsm.get_context(harness.bot, harness.chat_id, harness.chat_id)
    assert await state.get_state() == ConnectStates.connect_choose.state
    assert await state.get_data() == {"purpose": "reconnect"}
    # The reconnect still finishes as a reconnect.
    await harness.press("tglogin:0:phone")
    await harness.say(PHONE)
    await harness.say(CODE)
    assert harness.api.sent_texts[-1].startswith("✅ Reconnected as")


async def test_client_start_failure_on_reconnect_points_to_telegram(
    harness: Harness,
    db_session: AsyncSession,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_up_user(harness, db_session, make_user)

    async def failing_start(user_id: object) -> None:
        raise ConnectionError("synthetic")

    monkeypatch.setattr(gateway, "start", failing_start)
    await _reconnect_by_phone(harness)
    text = harness.api.sent_texts[-1]
    assert text.startswith(t("connect.telegram_unavailable"))
    assert "/telegram" in text and "/start" not in text


async def test_reconnect_keeps_paused_state(
    harness: Harness,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _set_up_user(harness, db_session, make_user, state=UserState.paused)
    await _reconnect_by_phone(harness)
    assert harness.api.sent_texts[-1].startswith("✅ Reconnected as")
    async with session_factory() as session:
        refreshed = await _fresh_user(session, user)
    assert refreshed.state == UserState.paused


async def test_reconnect_with_different_account_is_refused(
    harness: Harness,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    user = await _set_up_user(harness, db_session, make_user)
    harness.login_tg_user_id = OTHER_TG_USER_ID
    harness.login_display_name = "Other Person"

    await _reconnect_by_phone(harness)

    (login,) = harness.clients
    assert login.logged_out and login.closed
    last = harness.sent()[-1]
    assert last.text == t(
        "connect.wrong_account",
        other="Other Person (@synthetic_me)",
        expected="Synthetic Me (@synthetic_me)",
    )
    assert callbacks(last.reply_markup) == ["tglogin:0:qr", "tglogin:0:phone"]
    assert user.id not in gateway.started
    async with session_factory() as session:
        tg = await users_repo.get_telegram_session(session, user.id)
        assert tg is not None
        await session.refresh(tg)
        refreshed = await _fresh_user(session, user)
        records = await activity_repo.list_recent(session, user.id)
    assert tg.status == SessionStatus.revoked
    assert tg.session_string == "synthetic-old-session"
    assert refreshed.telegram_user_id == TG_USER_ID
    (record,) = [r for r in records if r.kind == ActivityKind.telegram_login]
    assert record.meta_json["outcome"] == "wrong_account"


async def test_reconnect_button_ignored_when_connected(
    harness: Harness, db_session: AsyncSession, make_user: MakeUser
) -> None:
    await _set_up_user(harness, db_session, make_user, status=SessionStatus.active)
    await harness.press("tgreconnect:0")
    assert t("connect.method_choice") not in harness.api.sent_texts


async def test_reconnect_reports_backlog(
    harness: Harness,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user = await _set_up_user(harness, db_session, make_user)
    chat = await make_chat(user)
    contact = await make_contact(user)
    now = clock()
    for due_at, state in (
        (now - timedelta(minutes=30), TurnState.waiting),
        (now - timedelta(minutes=5), TurnState.waiting),
        (now + timedelta(minutes=5), TurnState.waiting),  # not due yet
        (now - timedelta(minutes=5), TurnState.answered),  # not waiting
    ):
        await turns_repo.create_turn(
            db_session,
            user.id,
            work_chat_id=chat.id,
            contact_id=contact.id,
            first_message_id=None,
            arrived_at=due_at - timedelta(minutes=1),
            delay_s=60,
            due_at=due_at,
            state=state,
        )
    await db_session.flush()

    await _reconnect_by_phone(harness)

    texts = harness.api.sent_texts
    assert texts[-2].startswith("✅ Reconnected as")
    assert texts[-1] == t("connect.backlog", n=2)


async def test_reconnect_without_backlog_sends_no_backlog_notice(
    harness: Harness, db_session: AsyncSession, make_user: MakeUser
) -> None:
    await _set_up_user(harness, db_session, make_user)
    await _reconnect_by_phone(harness)
    assert harness.api.sent_texts[-1].startswith("✅ Reconnected as")
    assert not any(t.startswith("📥") for t in harness.api.sent_texts)


# --- US3: /start and Disconnect -----------------------------------------------------------------


async def test_start_for_set_up_user_with_revoked_session_points_to_telegram(
    harness: Harness, db_session: AsyncSession, make_user: MakeUser
) -> None:
    await _set_up_user(harness, db_session, make_user, status=SessionStatus.error)
    await harness.say("/start")
    last = harness.sent()[-1]
    assert last.text == t("telegram.disconnected_start")
    assert "/telegram" in last.text
    assert callbacks(last.reply_markup) == ["tgreconnect:0"]


async def test_start_for_connected_set_up_user_is_unchanged(
    harness: Harness, db_session: AsyncSession, make_user: MakeUser
) -> None:
    await _set_up_user(harness, db_session, make_user, status=SessionStatus.active)
    await harness.say("/start")
    assert "already set up" in harness.api.sent_texts[-1]


async def test_disconnect_two_step_keeps_user_state(
    harness: Harness,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    user = await _set_up_user(
        harness, db_session, make_user, state=UserState.paused, status=SessionStatus.active
    )
    gateway.started.add(user.id)

    await harness.say("/telegram")
    card = harness.sent()[-1]
    assert card.text is not None and "✅ connected" in card.text
    assert callbacks(card.reply_markup) == ["tgdisconnect:0"]

    await harness.press("tgdisconnect:0")  # first step only asks
    assert gateway.logged_out == []
    assert callbacks(harness.sent()[-1].reply_markup) == ["tgdisconnect:1"]

    await harness.press("tgdisconnect:1")
    assert gateway.logged_out == [user.id]
    assert user.id not in gateway.started
    async with session_factory() as session:
        tg = await users_repo.get_telegram_session(session, user.id)
        assert tg is not None
        await session.refresh(tg)
        refreshed = await _fresh_user(session, user)
    assert tg.status == SessionStatus.revoked
    assert refreshed.state == UserState.paused
    last = harness.sent()[-1]
    assert "/telegram" in (last.text or "")
    assert callbacks(last.reply_markup) == ["tgreconnect:0"]

    # A stale confirm button does nothing more.
    await harness.press("tgdisconnect:1")
    assert gateway.logged_out == [user.id]


async def test_start_after_disconnect_does_not_rerun_setup(
    harness: Harness,
    db_session: AsyncSession,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    await _set_up_user(harness, db_session, make_user, status=SessionStatus.active)
    await harness.press("tgdisconnect:0")
    await harness.press("tgdisconnect:1")
    harness.api.clear()

    await harness.say("/start")
    texts = harness.api.sent_texts
    assert texts == [t("telegram.disconnected_start")]
    assert "Use /telegram to reconnect" in texts[0]
    assert callbacks(harness.sent()[-1].reply_markup) == ["tgreconnect:0"]
    assert t("connect.method_choice") not in texts and t("setup.pick_prompt") not in texts
    assert harness.clients == []


async def test_status_card_for_revoked_session_offers_reconnect(
    harness: Harness, db_session: AsyncSession, make_user: MakeUser
) -> None:
    await _set_up_user(harness, db_session, make_user, status=SessionStatus.revoked)
    await harness.say("/telegram")
    card = harness.sent()[-1]
    assert card.text is not None
    assert "📡 Telegram connection" in card.text
    assert "Account: Synthetic Me" in card.text
    assert "⚠️ disconnected" in card.text
    assert callbacks(card.reply_markup) == ["tgreconnect:0"]


async def test_status_card_without_session_points_to_start(
    harness: Harness, make_user: MakeUser
) -> None:
    await make_user(control_chat_id=harness.chat_id)
    await harness.say("/telegram")
    assert "/start" in harness.api.sent_texts[-1]


# --- US4: method and time on the status card ----------------------------------------------------


@pytest.mark.parametrize(
    ("method", "label"),
    [(LoginMethod.qr, "via QR code"), (LoginMethod.phone_code, "via phone and code")],
)
async def test_status_card_shows_method_and_time(
    harness: Harness,
    db_session: AsyncSession,
    make_user: MakeUser,
    method: LoginMethod,
    label: str,
) -> None:
    await _set_up_user(
        harness,
        db_session,
        make_user,
        status=SessionStatus.active,
        method=method,
        connected_at=datetime(2026, 10, 20, 11, 3, tzinfo=UTC),
        timezone="Europe/Moscow",  # UTC+3
    )
    await harness.say("/telegram")
    text = harness.sent()[-1].text
    assert text is not None
    assert f"Connected: 20 Oct 2026, 14:03 · {label}" in text
    assert 'Telegram → Settings → Devices → "Scribe Agent" → Terminate session' in text


async def test_wrong_account_names_control_chat_user_for_unlabelled_session(
    harness: Harness, db_session: AsyncSession, make_user: MakeUser
) -> None:
    # Sessions stored before migration 0002 have no account label.
    await _set_up_user(harness, db_session, make_user, account_label=None)
    harness.login_tg_user_id = OTHER_TG_USER_ID
    harness.login_display_name = "Other Person"

    await _reconnect_by_phone(harness)

    assert harness.sent()[-1].text == t(
        "connect.wrong_account", other="Other Person (@synthetic_me)", expected="Synthetic Me"
    )


async def test_status_card_shows_telegram_account_not_control_chat_name(
    harness: Harness,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _set_up_user(
        harness, db_session, make_user, account_label="Old Label (+7 ••• ••• •• 11)"
    )
    await harness.say("/telegram")
    card = harness.sent()[-1].text or ""
    assert "Account: Old Label (+7 ••• ••• •• 11)" in card

    harness.login_display_name = "Renamed Me"
    await _reconnect_by_phone(harness)  # the label is refreshed on every login
    async with session_factory() as session:
        tg = await users_repo.get_telegram_session(session, user.id)
        assert tg is not None
        await session.refresh(tg)
    assert tg.account_label == "Renamed Me (@synthetic_me)"

    await harness.say("/telegram")
    card = harness.sent()[-1].text or ""
    assert "Account: Renamed Me (@synthetic_me)" in card
    assert "Synthetic Me" not in card


async def test_status_card_falls_back_to_control_chat_name(
    harness: Harness, db_session: AsyncSession, make_user: MakeUser
) -> None:
    await _set_up_user(harness, db_session, make_user, account_label=None)
    await harness.say("/telegram")
    assert "Account: Synthetic Me\n" in (harness.sent()[-1].text or "")
