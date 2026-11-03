"""Connecting a Telegram account by QR code (002 US1, T021) and the password step (US2, T028).

The login client is a scripted fake: each ``wait_qr`` call takes the next outcome from a queue
(and blocks while the queue is empty, like a code nobody has scanned yet), so the QR loop is
driven by the test without real waiting. Synthetic values only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageMedia,
    SendMessage,
    SendPhoto,
)
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    BotFsmState,
    LoginMethod,
    SessionStatus,
    TelegramSession,
    UserState,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import t
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot import connect_flow
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.connect_flow import (
    ConnectStates,
    login_activity_recorder,
    on_late_scan,
    recover_interrupted_logins,
)
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.control_bot.render import (
    help_text,
)
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.login import (
    InvalidCode,
    InvalidPassword,
    LoginAttempt,
    LoginFlood,
    LoginManager,
    LoginResult,
    QrCode,
    QrOutcome,
)
from tests.conftest import FROZEN_NOW, FrozenClock, LogCapture, MakeUser, next_telegram_id
from tests.integration.bot_support import (
    RecordingSession,
    callback_update,
    offline_bot,
    text_update,
)

QR_TOKEN = "SYNTHETIC"
QR_URL = f"tg://login?token={QR_TOKEN}"
QR_EXPIRES = FROZEN_NOW + timedelta(seconds=30)
PHONE = "+7 900 555-01-23"
PHONE_DIGITS = "79005550123"
CODE = "62841"
PASSWORD = "synthetic-2fa-Pa55word"
TG_USER_ID = 4242

Script = QrOutcome | Exception


class FakeQrLoginClient:
    """``LoginClient`` whose ``wait_qr`` returns scripted outcomes (blocks when none is left)."""

    def __init__(self, script: list[Script], *, needs_password: bool = False) -> None:
        self.outcomes: asyncio.Queue[Script] = asyncio.Queue()
        for item in script:
            self.outcomes.put_nowait(item)
        self.needs_password = needs_password
        self.refreshes = 0
        self.waiting = False
        self.wait_timeouts: list[float] = []
        self.logged_out = False
        self.closed = False
        self.passwords: list[str] = []

    # --- phone method ---
    async def send_code(self, phone: str) -> None:
        return None

    async def sign_in_code(self, code: str) -> bool:
        if code != CODE:
            raise InvalidCode()
        return self.needs_password

    async def sign_in_password(self, password: str) -> None:
        self.passwords.append(password)
        if password != PASSWORD:
            raise InvalidPassword()

    # --- QR method ---
    async def start_qr(self) -> QrCode:
        return QrCode(url=QR_URL, expires_at=QR_EXPIRES)

    async def refresh_qr(self) -> QrCode:
        self.refreshes += 1
        return QrCode(url=f"{QR_URL}{self.refreshes}", expires_at=QR_EXPIRES)

    async def wait_qr(self, timeout_s: float) -> QrOutcome:
        self.wait_timeouts.append(timeout_s)
        self.waiting = True
        try:
            item = await self.outcomes.get()
        finally:
            self.waiting = False
        if isinstance(item, Exception):
            raise item
        return item

    async def log_out(self) -> None:
        self.logged_out = True

    async def complete(self) -> LoginResult:
        return LoginResult(
            session_string="synthetic-session-string",
            telegram_user_id=TG_USER_ID,
            display_name="Me",
            username="me",
            masked_phone="+7 ••• ••• •• 23",
        )

    async def close(self) -> None:
        self.closed = True


class FakeSleep:
    """Stand-in for the password timer's sleep: records the delay, waits for ``release``."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await self.release.wait()


async def wait_until(condition: Callable[[], bool], what: str = "condition") -> None:
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


class Harness:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        llm_router: LLMRouter,
        gateway: FakeMessagingGateway,
        clock: FrozenClock,
    ) -> None:
        self.session_factory = session_factory
        self.clients: list[FakeQrLoginClient] = []
        self.next_script: list[Script] = []
        self.needs_password = False
        self.loop: tuple[asyncio.Task[None], Any] | None = None

        def factory() -> FakeQrLoginClient:
            client = FakeQrLoginClient(self.next_script, needs_password=self.needs_password)
            self.next_script = []
            self.clients.append(client)
            return client

        self.bot, self.api = offline_bot()
        self.api_session: RecordingSession = self.api
        self.chat_id = next_telegram_id()
        self.login = LoginManager(
            factory,
            record=login_activity_recorder(session_factory),
            clock=clock,
            on_late_scan=on_late_scan(self.bot, session_factory),
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

    async def say(self, text: str, language_code: str | None = None) -> None:
        update = text_update(self.chat_id, text, language_code=language_code)
        await self.dispatcher.feed_update(self.bot, update)

    async def press(self, data: str, **kwargs: Any) -> None:
        await self.dispatcher.feed_update(self.bot, callback_update(self.chat_id, data, **kwargs))
        attempt = self.attempt()
        if attempt is not None and attempt.task is not None:
            self.loop = (attempt.task, attempt.client)  # the QR loop just started

    def attempt(self) -> LoginAttempt | None:
        return self.login.get(self.chat_id)

    async def settle(self) -> None:
        """Wait until the QR loop has finished or blocks on an unscanned code."""

        def idle() -> bool:
            if self.loop is None:
                return True
            task, client = self.loop
            return task.done() or (isinstance(client, FakeQrLoginClient) and client.waiting)

        await wait_until(idle, "the QR loop to settle")
        await asyncio.sleep(0)

    def message_with(self, text: str) -> SendMessage:
        return next(r for r in self.api.of_type(SendMessage) if r.text == text)

    def deleted_ids(self) -> list[int]:
        return [int(r.message_id) for r in self.api.of_type(DeleteMessage)]

    async def user_id(self) -> Any:
        async with self.session_factory() as session:
            user = await users_repo.get_by_control_chat(session, self.chat_id)
        assert user is not None
        return user.id

    async def outcomes(self) -> list[dict[str, Any]]:
        user_id = await self.user_id()
        async with self.session_factory() as session:
            records = await activity_repo.list_recent(session, user_id)
        return [dict(r.meta_json) for r in records if r.kind == ActivityKind.telegram_login]

    async def fsm(self) -> BotFsmState | None:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(BotFsmState).where(BotFsmState.chat_id == self.chat_id)
                )
            ).scalar_one_or_none()


def callbacks(markup: Any) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data or f"url:{b.url}" for row in markup.inline_keyboard for b in row]


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> Harness:
    return Harness(session_factory, settings, llm_router, gateway, clock)


@pytest.fixture
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> FakeSleep:
    sleep = FakeSleep()
    monkeypatch.setattr(connect_flow, "_sleep", sleep)
    return sleep


# --- US1 ----------------------------------------------------------------------------------------


async def test_start_offers_qr_first(harness: Harness) -> None:
    await harness.say("/start")
    choice = harness.message_with(t("connect.method_choice"))
    assert isinstance(choice.reply_markup, InlineKeyboardMarkup)
    first = choice.reply_markup.inline_keyboard[0][0]
    assert first.callback_data == "tglogin:0:qr"
    assert "recommended" in first.text
    assert callbacks(choice.reply_markup)[1] == "tglogin:0:phone"


async def test_qr_happy_path_without_password(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.authorized]
    await harness.press("tglogin:0:qr")
    await harness.settle()

    (photo,) = harness.api.of_type(SendPhoto)
    assert photo.caption is not None and "Code 1 of 5" in photo.caption
    assert "url:" not in " ".join(callbacks(photo.reply_markup))  # same-device link is off
    assert callbacks(photo.reply_markup) == ["tglogin:0:phone", "tglogin:0:cancel"]
    (photo_id,) = harness.api.photo_message_ids
    assert photo_id in harness.deleted_ids()  # no scannable code is left in the chat

    connected = harness.message_with("✅ Connected as Me (@me).")
    assert callbacks(connected.reply_markup) == ["tglogin:0:notme"]
    assert harness.api.sent_texts[-1] == t("setup.pick_prompt")
    texts = harness.api.sent_texts
    assert texts.index(connected.text or "") < texts.index(t("setup.pick_prompt"))

    user_id = await harness.user_id()
    async with session_factory() as session:
        tg = await users_repo.get_telegram_session(session, user_id)
    assert tg is not None and tg.login_method == LoginMethod.qr
    assert tg.account_label == "Me (@me)"
    assert user_id in gateway.started
    (record,) = await harness.outcomes()
    assert record["outcome"] == "connected" and record["method"] == "qr"
    assert record["qr_codes_shown"] == 1 and record["password_needed"] is False
    (client,) = harness.clients
    assert client.closed and not client.logged_out


async def test_qr_refreshes_and_stops_after_max_codes(harness: Harness) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.expired] * 5
    await harness.press("tglogin:0:qr")
    await harness.settle()

    (client,) = harness.clients
    assert client.refreshes == 4
    edits = harness.api.of_type(EditMessageMedia)
    assert len(edits) == 4
    assert all(e.message_id == harness.api.photo_message_ids[0] for e in edits)
    assert harness.api.photo_message_ids[0] in harness.deleted_ids()
    expired = harness.message_with(t("connect.qr_expired"))
    assert callbacks(expired.reply_markup) == ["tglogin:0:qr", "tglogin:0:phone"]
    (record,) = await harness.outcomes()
    assert record["outcome"] == "expired" and record["qr_codes_shown"] == 5
    assert client.closed

    # "Show a new code" starts a new round with a new client.
    await harness.press("tglogin:0:qr")
    await harness.settle()
    assert len(harness.api.of_type(SendPhoto)) == 2
    assert len(harness.clients) == 2


async def test_switch_to_phone_cancels_and_drains_qr(harness: Harness) -> None:
    await harness.say("/start")
    await harness.press("tglogin:0:qr")
    await harness.settle()
    (client,) = harness.clients
    assert client.waiting

    await harness.press("tglogin:0:phone")
    assert harness.api.sent_texts[-1] == t("connect.phone_prompt")
    prompt = harness.message_with(t("connect.phone_prompt"))
    assert callbacks(prompt.reply_markup) == ["tglogin:0:qr"]
    assert harness.api.photo_message_ids[0] in harness.deleted_ids()
    assert [r["outcome"] for r in await harness.outcomes()] == ["cancelled"]
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state is not None and fsm.state.endswith("connect_phone")

    # The old client keeps listening until its code expires (research Q4).
    await wait_until(lambda: client.waiting, "the drain to wait")
    client.outcomes.put_nowait(QrOutcome.expired)
    await wait_until(lambda: client.closed, "the drained client to close")
    assert not client.logged_out


async def test_late_scan_after_cancel_is_logged_out(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await harness.say("/start")
    await harness.press("tglogin:0:qr")
    await harness.settle()
    await harness.say("/cancel")
    (client,) = harness.clients
    await wait_until(lambda: client.waiting, "the drain to wait")
    client.outcomes.put_nowait(QrOutcome.authorized)  # the old code is scanned after all
    await wait_until(lambda: client.closed, "the drained client to close")

    assert client.logged_out
    assert t("connect.late_scan") in harness.api.sent_texts
    user_id = await harness.user_id()
    async with session_factory() as session:
        assert await users_repo.get_telegram_session(session, user_id) is None
    assert [r["outcome"] for r in await harness.outcomes()] == ["cancelled"]


async def test_second_start_cancels_first_attempt(harness: Harness) -> None:
    """FR-009: starting a new attempt (QR button again) ends and drains the previous one."""
    await harness.say("/start")
    await harness.press("tglogin:0:qr")
    await harness.settle()
    harness.next_script = [QrOutcome.authorized]
    await harness.press("tglogin:0:qr")
    await harness.settle()

    first, second = harness.clients
    assert len(harness.api.of_type(SendPhoto)) == 2
    assert harness.api.photo_message_ids[0] in harness.deleted_ids()
    assert sorted(r["outcome"] for r in await harness.outcomes()) == ["cancelled", "connected"]
    assert second.closed and not second.logged_out
    # The first client drains until its code expires.
    await wait_until(lambda: first.waiting, "the first client's drain")
    first.outcomes.put_nowait(QrOutcome.expired)
    await wait_until(lambda: first.closed, "the first client to close")
    assert not first.logged_out


async def test_not_my_account_logs_out_and_returns_to_choice(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.authorized]
    await harness.press("tglogin:0:qr")
    await harness.settle()
    user_id = await harness.user_id()

    await harness.press("tglogin:0:notme")

    assert gateway.logged_out == [user_id]
    async with session_factory() as session:
        assert await users_repo.get_telegram_session(session, user_id) is None
        user = await users_repo.get_user(session, user_id)
        assert user is not None
        await session.refresh(user)
    assert user.telegram_user_id is None and user.state == UserState.setting_up
    done = harness.message_with(t("connect.not_my_account_done"))
    assert callbacks(done.reply_markup) == ["tglogin:0:qr", "tglogin:0:phone"]
    assert sorted(r["outcome"] for r in await harness.outcomes()) == [
        "connected",
        "not_my_account",
    ]
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state is not None and fsm.state.endswith("connect_choose")
    assert fsm.data == {"purpose": "setup"}


async def test_not_my_account_ignored_after_setup(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.authorized]
    await harness.press("tglogin:0:qr")
    await harness.settle()
    user_id = await harness.user_id()
    async with session_factory() as session:
        await users_repo.set_state(session, user_id, UserState.active)
        await session.commit()

    await harness.press("tglogin:0:notme")
    assert gateway.logged_out == []
    async with session_factory() as session:
        assert await users_repo.get_telegram_session(session, user_id) is not None


async def test_account_taken_is_logged_out(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser
) -> None:
    await make_user(telegram_user_id=TG_USER_ID)  # another Scribe user owns this account
    await harness.say("/start")
    harness.next_script = [QrOutcome.authorized]
    await harness.press("tglogin:0:qr")
    await harness.settle()

    (client,) = harness.clients
    assert client.logged_out and client.closed
    assert harness.api.sent_texts[-1] == t("connect.account_taken")
    user_id = await harness.user_id()
    async with session_factory() as session:
        assert await users_repo.get_telegram_session(session, user_id) is None
    assert [r["outcome"] for r in await harness.outcomes()] == ["account_taken"]


async def test_flood_offers_other_method(harness: Harness) -> None:
    await harness.say("/start")
    harness.next_script = [LoginFlood(42)]
    await harness.press("tglogin:0:qr")
    await harness.settle()

    flood = harness.message_with(t("connect.rate_limited", seconds=42))
    assert callbacks(flood.reply_markup) == ["tglogin:0:phone"]
    assert harness.api.photo_message_ids[0] in harness.deleted_ids()
    (record,) = await harness.outcomes()
    assert record["outcome"] == "failed" and record["error_kind"] == "LoginFlood"
    # The other method still works from here.
    await harness.press("tglogin:0:phone")
    assert harness.api.sent_texts[-1] == t("connect.phone_prompt")


async def test_same_device_link_behind_setting(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> None:
    linked = settings.model_copy(update={"tg_qr_same_device_link": True})
    harness = Harness(session_factory, linked, llm_router, gateway, clock)
    await harness.say("/start")
    harness.next_script = [QrOutcome.expired]
    await harness.press("tglogin:0:qr")
    await harness.settle()

    (photo,) = harness.api.of_type(SendPhoto)
    assert callbacks(photo.reply_markup)[0] == f"url:{QR_URL}"
    (edit,) = harness.api.of_type(EditMessageMedia)
    assert callbacks(edit.reply_markup)[0] == f"url:{QR_URL}1"  # follows the refreshed token
    await harness.say("/cancel")


async def test_no_secrets_in_logs_or_fsm(harness: Harness, log_capture: LogCapture) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.expired, QrOutcome.authorized]
    await harness.press("tglogin:0:qr")
    await harness.settle()
    fsm_after_qr = await harness.fsm()
    await harness.press("tglogin:0:notme")
    await harness.press("tglogin:0:phone")
    await harness.say(PHONE)
    fsm_after_phone = await harness.fsm()
    await harness.say("/cancel")

    raw = log_capture.raw
    for secret in (QR_TOKEN, "tg://login", PHONE_DIGITS, "900 555", "•• 23"):
        assert secret not in raw
    for fsm in (fsm_after_qr, fsm_after_phone):
        assert fsm is not None
        for secret in (QR_TOKEN, PHONE_DIGITS):
            assert secret not in str(fsm.data)
    assert fsm_after_phone is not None and fsm_after_phone.data == {"purpose": "setup"}
    steps = [e.get("step") for e in log_capture.events("login_step")]
    assert {"choose", "qr_shown", "qr_refreshed", "scanned", "finished"} <= set(steps)
    ended = log_capture.events("login_ended")
    assert {e["outcome"] for e in ended} >= {"connected", "not_my_account", "cancelled"}
    assert all(set(e) >= {"user_id", "method", "purpose", "duration_s"} for e in ended)


# --- US2 ----------------------------------------------------------------------------------------


async def test_qr_with_password_wrong_then_right(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_sleep: FakeSleep,
) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.password_needed]
    await harness.press("tglogin:0:qr")
    await harness.settle()

    assert harness.api.sent_texts[-1] == t("connect.password_prompt")
    assert harness.api.photo_message_ids[0] in harness.deleted_ids()
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state is not None and fsm.state.endswith("connect_password")
    deletes_before = len(harness.api.of_type(DeleteMessage))

    await harness.say("wrong-synthetic-password")
    assert harness.api.sent_texts[-1] == t("connect.invalid_password")
    await harness.say(PASSWORD)

    (client,) = harness.clients
    assert client.passwords == ["wrong-synthetic-password", PASSWORD]
    assert len(harness.api.of_type(DeleteMessage)) == deletes_before + 2
    assert "✅ Connected as Me (@me)." in harness.api.sent_texts
    assert harness.api.sent_texts[-1] == t("setup.pick_prompt")
    (record,) = await harness.outcomes()
    assert record["outcome"] == "connected" and record["password_needed"] is True
    assert record["method"] == "qr"
    # The timer was started (and restarted after the wrong password); none fired.
    assert fake_sleep.calls and all(c == 600 for c in fake_sleep.calls)
    assert t("connect.login_timeout") not in harness.api.sent_texts


async def test_password_timeout_after_10_minutes(harness: Harness, fake_sleep: FakeSleep) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.password_needed]
    await harness.press("tglogin:0:qr")
    await harness.settle()
    await wait_until(lambda: bool(fake_sleep.calls), "the password timer")
    assert fake_sleep.calls == [600]

    fake_sleep.release.set()
    await wait_until(lambda: t("connect.login_timeout") in harness.api.sent_texts, "the timeout")
    timeout = harness.message_with(t("connect.login_timeout"))
    assert callbacks(timeout.reply_markup) == ["tglogin:0:qr", "tglogin:0:phone"]
    (record,) = await harness.outcomes()
    assert record["outcome"] == "expired" and record["password_needed"] is True
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state is not None and fsm.state.endswith("connect_choose")
    (client,) = harness.clients
    assert client.closed

    # A late password is not used: no attempt is active any more.
    await harness.say(PASSWORD)
    assert client.passwords == []


async def test_phone_flow_password_also_times_out(harness: Harness, fake_sleep: FakeSleep) -> None:
    harness.needs_password = True
    await harness.say("/start")
    await harness.press("tglogin:0:phone")
    await harness.say(PHONE)
    await harness.say(CODE)
    assert harness.api.sent_texts[-1] == t("connect.password_prompt")
    await wait_until(lambda: bool(fake_sleep.calls), "the password timer")

    fake_sleep.release.set()
    await wait_until(lambda: t("connect.login_timeout") in harness.api.sent_texts, "the timeout")
    (record,) = await harness.outcomes()
    assert record["outcome"] == "expired" and record["method"] == "phone_code"
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state is not None and fsm.state.endswith("connect_choose")


async def test_password_never_in_logs_or_fsm(
    harness: Harness, log_capture: LogCapture, fake_sleep: FakeSleep
) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.password_needed]
    await harness.press("tglogin:0:qr")
    await harness.settle()
    await harness.say("wrong-synthetic-password")
    fsm = await harness.fsm()
    await harness.say(PASSWORD)

    assert fsm is not None and fsm.data == {"purpose": "setup"}
    assert PASSWORD not in log_capture.raw and "wrong-synthetic-password" not in log_capture.raw
    assert "password_asked" in [e.get("step") for e in log_capture.events("login_step")]


async def test_help_and_other_commands_are_not_login_input(
    harness: Harness, fake_sleep: FakeSleep
) -> None:
    await harness.say("/start")
    harness.next_script = [QrOutcome.password_needed]
    await harness.press("tglogin:0:qr")
    await harness.settle()
    deletes_before = len(harness.api.of_type(DeleteMessage))

    await harness.say("/help")
    assert harness.api.sent_texts[-1] == help_text("en")
    await harness.say("/pause")  # not a command the login knows: deleted, not used
    assert harness.api.sent_texts[-1] == t("connect.finish_connecting")
    assert len(harness.api.of_type(DeleteMessage)) == deletes_before + 1

    (client,) = harness.clients
    assert client.passwords == []
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state == ConnectStates.connect_password.state
    await harness.say("/cancel")


async def test_commands_in_phone_and_code_steps(harness: Harness) -> None:
    await harness.say("/start")
    await harness.press("tglogin:0:phone")
    await harness.say("/settings")
    assert harness.api.sent_texts[-1] == t("connect.finish_connecting")
    await harness.say("/telegram")  # still answers in any state
    assert harness.api.sent_texts[-1] == t("telegram.not_connected")
    assert harness.attempt() is None  # nothing was sent to Telegram as a phone number
    await harness.say(PHONE)
    await harness.say("/settings")
    assert harness.api.sent_texts[-1] == t("connect.finish_connecting")
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state == ConnectStates.connect_code.state
    assert t("connect.code_prompt") in harness.api.sent_texts


async def test_start_during_login_cancels_attempt_and_shows_choice(harness: Harness) -> None:
    """Spec edge case: starting a second attempt cancels the first."""
    await harness.say("/start")
    await harness.press("tglogin:0:qr")
    await harness.settle()
    (client,) = harness.clients

    await harness.say("/start")

    assert harness.api.sent_texts[-1] == t("connect.method_choice")
    assert harness.attempt() is None
    assert harness.api.photo_message_ids[0] in harness.deleted_ids()
    assert [r["outcome"] for r in await harness.outcomes()] == ["cancelled"]
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state == ConnectStates.connect_choose.state
    assert fsm.data == {"purpose": "setup"}
    await wait_until(lambda: client.waiting, "the drain to wait")
    client.outcomes.put_nowait(QrOutcome.expired)
    await wait_until(lambda: client.closed, "the drained client to close")


# --- restart and callback ownership (T041, T042) ------------------------------------------------


def _restarted(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> Harness:
    """A new process for the same chat: fresh dispatcher and login manager, same database."""
    restarted = Harness(session_factory, settings, llm_router, gateway, clock)
    restarted.chat_id = harness.chat_id
    return restarted


async def test_restart_mid_login_is_cleaned_up(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> None:
    await harness.say("/start")
    await harness.press("tglogin:0:qr")
    await harness.settle()
    # The process dies: the QR loop stops and the in-memory attempt is lost, nothing recorded.
    assert harness.loop is not None
    harness.loop[0].cancel()
    await asyncio.wait({harness.loop[0]})

    restarted = _restarted(harness, session_factory, settings, llm_router, gateway, clock)
    assert (
        await recover_interrupted_logins(restarted.bot, restarted.dispatcher, session_factory) == 1
    )

    notice = restarted.message_with(t("connect.restarted"))
    assert int(notice.chat_id) == harness.chat_id
    assert callbacks(notice.reply_markup) == ["tglogin:0:qr", "tglogin:0:phone"]
    (record,) = await harness.outcomes()
    assert record["outcome"] == "interrupted"
    assert record["method"] == "qr" and record["purpose"] == "setup"
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state == ConnectStates.connect_choose.state
    assert fsm.data == {"purpose": "setup"}
    user_id = await harness.user_id()
    async with session_factory() as session:
        rows = (
            await session.execute(select(TelegramSession).where(TelegramSession.user_id == user_id))
        ).all()
    assert rows == []

    # The buttons work after the restart.
    restarted.next_script = [QrOutcome.authorized]
    await restarted.press("tglogin:0:qr")
    await restarted.settle()
    assert "✅ Connected as Me (@me)." in restarted.api.sent_texts


@pytest.mark.parametrize(
    ("step", "purpose", "method"),
    [
        (ConnectStates.connect_qr, "setup", "qr"),
        (ConnectStates.connect_phone, "setup", "phone_code"),
        (ConnectStates.connect_code, "reconnect", "phone_code"),
        (ConnectStates.connect_password, "reconnect", None),
    ],
)
async def test_restart_cleanup_infers_method_and_keeps_purpose(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    step: Any,
    purpose: str,
    method: str | None,
) -> None:
    await make_user(control_chat_id=harness.chat_id)
    context = harness.dispatcher.fsm.get_context(harness.bot, harness.chat_id, harness.chat_id)
    await context.set_state(step)
    await context.set_data({"purpose": purpose})

    assert await recover_interrupted_logins(harness.bot, harness.dispatcher, session_factory) == 1

    (record,) = await harness.outcomes()
    assert record["outcome"] == "interrupted"
    assert record["method"] == method and record["purpose"] == purpose
    assert record["password_needed"] is (step == ConnectStates.connect_password)
    assert harness.api.sent_texts == [t("connect.restarted")]
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state == ConnectStates.connect_choose.state
    assert fsm.data == {"purpose": purpose}


async def test_restart_after_graceful_stop_records_once(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> None:
    await harness.say("/start")
    await harness.press("tglogin:0:qr")
    await harness.settle()
    await harness.login.close_all()  # graceful shutdown already records "interrupted"

    restarted = _restarted(harness, session_factory, settings, llm_router, gateway, clock)
    assert (
        await recover_interrupted_logins(restarted.bot, restarted.dispatcher, session_factory) == 1
    )

    assert t("connect.restarted") in restarted.api.sent_texts
    assert [r["outcome"] for r in await harness.outcomes()] == ["interrupted"]


async def test_restart_cleanup_leaves_method_choice_alone(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser
) -> None:
    # No login runs in the method choice and its buttons survive a restart, so repeated
    # restarts must not repeat the notice or record another "interrupted" attempt.
    await make_user(control_chat_id=harness.chat_id)
    context = harness.dispatcher.fsm.get_context(harness.bot, harness.chat_id, harness.chat_id)
    await context.set_state(ConnectStates.connect_choose)
    await context.set_data({"purpose": "reconnect"})

    assert await recover_interrupted_logins(harness.bot, harness.dispatcher, session_factory) == 0
    assert harness.api.sent_texts == []
    assert await harness.outcomes() == []
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state == ConnectStates.connect_choose.state


async def test_restart_cleanup_leaves_other_states_alone(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser
) -> None:
    await make_user(control_chat_id=harness.chat_id)
    context = harness.dispatcher.fsm.get_context(harness.bot, harness.chat_id, harness.chat_id)
    await context.set_state("SetupStates:pick_chats")
    await context.set_data({"dialogs": [], "selected": [], "page": 0})

    assert await recover_interrupted_logins(harness.bot, harness.dispatcher, session_factory) == 0
    assert harness.api.sent_texts == []
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state == "SetupStates:pick_chats"


@pytest.mark.parametrize(
    "data", ["tglogin:0:qr", "tglogin:0:phone", "tgreconnect:0", "tgdisconnect:0", "tgdisconnect:1"]
)
async def test_login_buttons_only_work_for_the_chat_owner(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    data: str,
) -> None:
    await harness.say("/start")
    harness.api.clear()

    await harness.press(data, from_user_id=next_telegram_id())  # someone else's press
    await harness.press(data, with_message=False)  # no chat to check against

    answers = harness.api.of_type(AnswerCallbackQuery)
    assert [a.text for a in answers] == [t("connect.not_your_button"), t("connect.not_your_button")]
    assert harness.api.sent_texts == [] and harness.api.of_type(SendPhoto) == []
    assert harness.clients == [] and gateway.logged_out == []
    fsm = await harness.fsm()
    assert fsm is not None and fsm.state == ConnectStates.connect_choose.state


async def test_disconnect_by_owner_still_works(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
) -> None:
    """The ownership check lets the owner's own presses through."""
    await harness.say("/start")
    harness.next_script = [QrOutcome.authorized]
    await harness.press("tglogin:0:qr")
    await harness.settle()
    user_id = await harness.user_id()

    await harness.press("tgdisconnect:1")
    assert gateway.logged_out == [user_id]
    async with session_factory() as session:
        tg = await users_repo.get_telegram_session(session, user_id)
        assert tg is not None
        await session.refresh(tg)
    assert tg.status == SessionStatus.revoked


# --- issue #8: the login flow in Russian --------------------------------------------------------


def _button_texts(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_qr_login_in_russian(harness: Harness) -> None:
    await harness.say("/start", language_code="ru")
    choice = harness.message_with(t("connect.method_choice", "ru"))
    assert _button_texts(choice.reply_markup)[0] == "📷 Сканировать QR-код (рекомендуется)"
    harness.next_script = [QrOutcome.authorized]
    await harness.press("tglogin:0:qr")
    await harness.settle()

    (photo,) = harness.api.of_type(SendPhoto)
    assert photo.caption is not None and photo.caption.startswith("📷 Отсканируйте код")
    assert "Код 1 из 5 · действует до " in photo.caption
    assert _button_texts(photo.reply_markup) == ["📱 Войти по номеру телефона", "✖️ Отмена"]
    connected = harness.message_with("✅ Подключён аккаунт Me (@me).")
    assert _button_texts(connected.reply_markup) == ["Это не мой аккаунт"]
    picker = harness.api.of_type(SendMessage)[-1]
    assert picker.text == t("setup.pick_prompt", "ru")
    assert _button_texts(picker.reply_markup)[-1] == "Готово (выбрано: 0)"


async def test_expired_qr_and_phone_steps_in_russian(harness: Harness) -> None:
    await harness.say("/start", language_code="ru")
    harness.next_script = [QrOutcome.expired] * 5
    await harness.press("tglogin:0:qr")
    await harness.settle()
    expired = harness.message_with(t("connect.qr_expired", "ru"))
    assert _button_texts(expired.reply_markup) == [
        "🔄 Показать новый код",
        "📱 Войти по номеру телефона",
    ]

    await harness.press("tglogin:0:phone")
    assert harness.api.sent_texts[-1] == t("connect.phone_prompt", "ru")
    await harness.say("12")
    assert harness.api.sent_texts[-1] == t("connect.invalid_phone", "ru")
    await harness.say("/settings")
    assert harness.api.sent_texts[-1] == "Сначала завершите подключение или отправьте /cancel."
    await harness.say("/cancel")
    assert harness.api.sent_texts[-1] == t("setup.cancelled", "ru")
