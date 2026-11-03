"""Minimal setup flow (T040, 002 T020): /start -> method choice -> phone -> code -> 2FA -> pick
chats -> import -> active.

Telegram login runs against a fake login client (no real Telegram); the phone number, the
login code and the 2FA password must never appear in logs or in the persisted FSM data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    BotFsmState,
    LoginMethod,
    ProfileScope,
    SessionStatus,
    UserState,
    WorkChat,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import StyleProfileOutput, StyleRules
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.connect_flow import (
    login_activity_recorder,
)
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.control_bot.setup_flow import (
    SetupStates,
)
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import Dialog, IncomingMessage
from scribe.telegram.login import (
    InvalidCode,
    InvalidPassword,
    LoginManager,
    LoginResult,
    QrCode,
    QrOutcome,
)
from tests.conftest import FrozenClock, LogCapture, MakeUser, next_telegram_id
from tests.integration.bot_support import (
    RecordingSession,
    callback_update,
    offline_bot,
    text_update,
)
from tests.integration.us2_support import callback_datas

PHONE = "+7 900 555-01-23"
PHONE_DIGITS = "79005550123"
CODE = "62841"
PASSWORD = "synthetic-2fa-Pa55word"
QR_EXPIRES = datetime(2026, 10, 14, 10, 0, 30, tzinfo=UTC)


class FakeLoginClient:
    def __init__(self, *, needs_password: bool = True) -> None:
        self.needs_password = needs_password
        self.phone: str | None = None
        self.closed = False
        self.logged_out = False
        self.codes: list[str] = []

    async def send_code(self, phone: str) -> None:
        self.phone = phone

    async def sign_in_code(self, code: str) -> bool:
        self.codes.append(code)
        if code != CODE:
            raise InvalidCode()
        return self.needs_password

    async def sign_in_password(self, password: str) -> None:
        if password != PASSWORD:
            raise InvalidPassword()

    async def start_qr(self) -> QrCode:
        return QrCode(url="tg://login?token=SYNTHETIC", expires_at=QR_EXPIRES)

    async def refresh_qr(self) -> QrCode:
        return QrCode(url="tg://login?token=SYNTHETIC", expires_at=QR_EXPIRES)

    async def wait_qr(self, timeout_s: float) -> QrOutcome:
        return QrOutcome.expired

    async def log_out(self) -> None:
        self.logged_out = True

    async def complete(self) -> LoginResult:
        return LoginResult(
            session_string="synthetic-session-string",
            telegram_user_id=4242,
            display_name="Me",
            username="me",
            masked_phone="+7 ••• ••• •• 23",
        )

    async def close(self) -> None:
        self.closed = True


class Setup:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        llm_router: LLMRouter,
        gateway: FakeMessagingGateway,
        clock: FrozenClock,
    ) -> None:
        self.clients: list[FakeLoginClient] = []

        def factory() -> FakeLoginClient:
            client = FakeLoginClient()
            self.clients.append(client)
            return client

        self.bot, self.api = offline_bot()
        self.api_session: RecordingSession = self.api
        self.chat_id = next_telegram_id()
        self.dispatcher = create_dispatcher(
            session_factory=session_factory,
            settings=settings,
            storage=PostgresFSMStorage(session_factory),
            llm_router=llm_router,
            gateway=gateway,
            client_pool=gateway,
            clock=clock,
            telegram_login=LoginManager(
                factory, record=login_activity_recorder(session_factory), clock=clock
            ),
        )

    async def say(self, text: str) -> None:
        await self.dispatcher.feed_update(self.bot, text_update(self.chat_id, text))

    async def press(self, data: str) -> None:
        await self.dispatcher.feed_update(self.bot, callback_update(self.chat_id, data))

    def last_text(self) -> str:
        texts = [
            r.text
            for r in self.api.requests
            if isinstance(r, SendMessage | EditMessageText) and r.text
        ]
        return texts[-1]


def _rules() -> StyleRules:
    return StyleRules(
        languages=["ru"],
        formality=2,
        typical_length_words=10,
        greetings=[],
        sign_offs=[],
        emoji_usage="none",
        punctuation_habits=[],
        typical_phrases=[],
        never_does=[],
    )


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


async def test_full_setup_logs_in_picks_chats_and_builds_profile(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    log_capture: LogCapture,
) -> None:
    await setup.say("/start")
    assert t("connect.method_choice") in setup.api.sent_texts
    assert t("connect.phone_prompt") not in setup.api.sent_texts
    await setup.press("tglogin:0:phone")
    assert setup.api.sent_texts[-1] == t("connect.phone_prompt")
    async with session_factory() as session:
        user = await users_repo.get_by_control_chat(session, setup.chat_id)
    assert user is not None and user.state == UserState.setting_up

    dialogs = [
        Dialog(chat_id=-1000 - i, title=f"Synthetic chat {i}", kind="private") for i in range(12)
    ]
    dialogs.append(Dialog(chat_id=-2000, title="Synthetic group", kind="group"))
    gateway.dialogs[user.id] = dialogs
    gateway.own_history[(user.id, -1003)] = [
        IncomingMessage(
            user_id=user.id,
            chat_id=-1003,
            message_id=i,
            sender_peer_id=1,
            sender_display_name="",
            text=f"own synthetic {i}",
            has_media=False,
            sent_at=clock() - timedelta(days=i),
            is_outgoing=True,
        )
        for i in range(5)
    ]

    await setup.say(PHONE)
    (login,) = setup.clients
    assert login.phone == "+" + PHONE_DIGITS
    assert t("connect.code_prompt") in setup.api.sent_texts

    await setup.say("6 2 8 4 0")  # wrong code: retry allowed
    assert "code" in setup.last_text().lower()
    await setup.say("6 2 8 4 1")
    assert login.codes == ["62840", CODE]
    assert t("connect.password_prompt") in setup.api.sent_texts

    await setup.say("wrong-password")
    assert "password" in setup.last_text().lower()
    await setup.say(PASSWORD)

    assert login.closed
    assert user.id in gateway.started
    async with session_factory() as session:
        tg = await users_repo.get_telegram_session(session, user.id)
        refreshed = await users_repo.get_user(session, user.id)
        assert refreshed is not None
        await session.refresh(refreshed)
    assert tg is not None and tg.status == SessionStatus.active
    assert tg.session_string == "synthetic-session-string"
    assert tg.login_method == LoginMethod.phone_code and tg.connected_at == clock()
    assert refreshed is not None and refreshed.telegram_user_id == 4242
    # Code and password messages are deleted from the control chat.
    assert len(setup.api.of_type(DeleteMessage)) >= 2

    # Chat picker: 8 per page, toggles, paging, done.
    assert "Synthetic chat 0" in str(setup.api.requests[-1])
    await setup.press("pickchat:-1003")
    await setup.press("pickpage:1")
    assert "Synthetic group" in str(setup.api.requests[-1])
    await setup.press("pickchat:-2000")
    await setup.press("pickchat:-2000")  # toggled off again
    await setup.press("pickchat:-2000")
    await setup.press("pickdone:0")

    # Time zone step (issue #5): human input, confirmed with the local time; then import.
    tz_prompt = setup.api.of_type(SendMessage)[-1]
    assert tz_prompt.text == t("setup.tz_prompt")
    assert callback_datas(tz_prompt.reply_markup)[-1] == "setuptz:keep"
    await setup.say("atlantis")
    assert "don't know the time zone 'atlantis'" in setup.last_text()
    await setup.say("Москва")
    local = clock().astimezone(ZoneInfo("Europe/Moscow"))
    assert f"✅ Time zone set to Europe/Moscow (UTC+3, now {local:%H:%M} there)." in (
        setup.api.sent_texts
    )

    async with session_factory() as session:
        chats = (
            (await session.execute(select(WorkChat).where(WorkChat.user_id == user.id)))
            .scalars()
            .all()
        )
        profile = await profiles_repo.get_current_profile(
            session, user.id, scope=ProfileScope.global_
        )
        refreshed = await users_repo.get_user(session, user.id)
        assert refreshed is not None
        await session.refresh(refreshed)
        fsm = (
            await session.execute(select(BotFsmState).where(BotFsmState.chat_id == setup.chat_id))
        ).scalar_one_or_none()
    assert sorted((c.telegram_chat_id, c.kind.value, c.enabled) for c in chats) == [
        (-2000, "group", True),
        (-1003, "private", True),
    ]
    assert profile is not None and profile.sample_size == 5
    assert refreshed is not None and refreshed.state == UserState.active
    assert refreshed.timezone == "Europe/Moscow"
    assert fsm is None  # setup finished: FSM state and data cleared
    assert "first draft will appear here" in setup.api.sent_texts[-2]
    # Issue #46: the synthetic own messages have no gendered forms, so the user is asked once.
    assert setup.last_text() == t("gender.question")
    assert refreshed.grammatical_gender is None
    assert refreshed.grammatical_gender_prompted_at == clock()
    # One telegram_login activity row for the attempt (FR-003), metadata only.
    async with session_factory() as session:
        records = await activity_repo.list_recent(session, user.id)
    (login_record,) = [r for r in records if r.kind == ActivityKind.telegram_login]
    assert login_record.meta_json == {
        "method": "phone_code",
        "purpose": "setup",
        "outcome": "connected",
        "qr_codes_shown": 0,
        "password_needed": True,
        "duration_s": 0,
        "error_kind": None,
    }

    # Never log the phone number, the login code or the 2FA password.
    raw = log_capture.raw
    for secret in (PHONE_DIGITS, "900 555", CODE, "6 2 8 4 1", PASSWORD):
        assert secret not in raw
    assert log_capture.events()  # the flow did log (metadata only)


async def test_time_zone_step_buttons(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    """Quick buttons follow the Telegram language; ambiguous input offers choices; Keep."""
    user = await make_user(control_chat_id=setup.chat_id)  # setting_up, UTC
    context = setup.dispatcher.fsm.get_context(setup.bot, setup.chat_id, setup.chat_id)
    await context.set_state(SetupStates.pick_chats)
    dialogs = [[-1003, "Synthetic chat", "private"]]
    await context.set_data({"dialogs": dialogs, "selected": [], "page": 0})
    await setup.press("pickchat:-1003")
    await setup.dispatcher.feed_update(
        setup.bot, callback_update(setup.chat_id, "pickdone:0", language_code="ru")
    )
    datas = callback_datas(setup.api.of_type(SendMessage)[-1].reply_markup)
    assert datas[0] == "setuptz:Europe/Moscow"  # ru: Moscow first
    assert datas[-1] == "setuptz:keep"

    await setup.say("west")
    choices = callback_datas(setup.api.of_type(SendMessage)[-1].reply_markup)
    assert set(choices) == {"setuptz:Australia/West", "setuptz:Brazil/West"}
    await setup.press("setuptz:Mars/Olympus")  # refused: not a zone
    assert (await context.get_state()) == SetupStates.timezone.state
    await setup.press("setuptz:keep")
    assert "✅ Time zone set to UTC (UTC, now" in "\n".join(setup.api.sent_texts)
    async with session_factory() as session:
        refreshed = await users_repo.get_user(session, user.id)
        assert refreshed is not None
        await session.refresh(refreshed)
    assert refreshed.timezone == "UTC"
    assert refreshed.state == UserState.active
    assert await context.get_state() is None


async def test_cancel_keeps_setting_up_and_start_restarts(
    setup: Setup, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await setup.say("/start")
    await setup.press("tglogin:0:phone")
    await setup.say(PHONE)
    await setup.say("/cancel")
    (login,) = setup.clients
    assert login.closed
    async with session_factory() as session:
        user = await users_repo.get_by_control_chat(session, setup.chat_id)
        assert user is not None
        records = await activity_repo.list_recent(session, user.id)
        fsm = (
            await session.execute(select(BotFsmState).where(BotFsmState.chat_id == setup.chat_id))
        ).scalar_one_or_none()
    assert user.state == UserState.setting_up
    assert fsm is None or fsm.state is None
    assert [r.meta_json["outcome"] for r in records] == ["cancelled"]

    await setup.say("/start")  # restarts at the method choice
    assert setup.api.sent_texts[-1] == t("connect.method_choice")


async def test_cancel_button_ends_connect_step(
    setup: Setup, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await setup.say("/start")
    await setup.press("tglogin:0:cancel")
    assert "Setup cancelled" in setup.api.sent_texts[-1]
    async with session_factory() as session:
        fsm = (
            await session.execute(select(BotFsmState).where(BotFsmState.chat_id == setup.chat_id))
        ).scalar_one_or_none()
    assert fsm is None or fsm.state is None
    assert setup.clients == []  # no attempt was started, so nothing to record


async def test_account_taken_logs_out_and_offers_method_choice(
    setup: Setup,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    await make_user(telegram_user_id=4242)  # another Scribe user owns this Telegram account
    await setup.say("/start")
    await setup.press("tglogin:0:phone")
    await setup.say(PHONE)
    await setup.say(CODE)
    await setup.say(PASSWORD)
    (login,) = setup.clients
    assert login.logged_out and login.closed
    assert setup.api.sent_texts[-1] == t("connect.account_taken")
    async with session_factory() as session:
        user = await users_repo.get_by_control_chat(session, setup.chat_id)
        assert user is not None
        tg = await users_repo.get_telegram_session(session, user.id)
        records = await activity_repo.list_recent(session, user.id)
        fsm = (
            await session.execute(select(BotFsmState).where(BotFsmState.chat_id == setup.chat_id))
        ).scalar_one()
    assert tg is None and user.telegram_user_id is None
    assert [r.meta_json["outcome"] for r in records] == ["account_taken"]
    assert fsm.state is not None and fsm.state.endswith("connect_choose")


async def test_fsm_data_never_holds_login_secrets(
    setup: Setup, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await setup.say("/start")
    await setup.press("tglogin:0:phone")
    await setup.say(PHONE)
    await setup.say(CODE)
    async with session_factory() as session:
        fsm = (
            await session.execute(select(BotFsmState).where(BotFsmState.chat_id == setup.chat_id))
        ).scalar_one()
    serialized = str(fsm.data)
    assert PHONE_DIGITS not in serialized and CODE not in serialized
    assert fsm.state is not None and fsm.state.endswith("connect_password")
    assert fsm.data == {"purpose": "setup"}  # the only FSM data of the connect step
