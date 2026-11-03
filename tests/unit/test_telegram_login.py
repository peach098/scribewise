"""Telethon login client: error mapping and session export, against a fake Telethon client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import types
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from scribe.telegram.login import (
    CodeExpired,
    InvalidCode,
    InvalidPassword,
    InvalidPhone,
    LoginError,
    LoginFlood,
    QrCode,
    QrOutcome,
    TelethonLoginClient,
    mask_phone,
)

EXPIRES = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)


class FakeQRLogin:
    """Stands in for ``telethon.tl.custom.QRLogin``; tokens are synthetic."""

    def __init__(self) -> None:
        self.tokens = 1
        self.expires = EXPIRES
        self.wait_result: Any = None
        self.wait_error: BaseException | None = None
        self.timeouts: list[float | None] = []

    @property
    def url(self) -> str:
        return f"tg://login?token=synthetic-{self.tokens}"

    async def recreate(self) -> None:
        self.tokens += 1
        self.expires += timedelta(seconds=30)

    async def wait(self, timeout: float | None = None) -> Any:  # noqa: ASYNC109 (Telethon API)
        self.timeouts.append(timeout)
        if self.wait_error is not None:
            error, self.wait_error = self.wait_error, None
            raise error
        return self.wait_result


class FakeTelethon:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.connected = False
        self.sign_in_error: Exception | None = None
        self.send_code_error: Exception | None = None
        self.sign_ins: list[dict[str, Any]] = []
        self.session = SimpleNamespace(save=lambda: "synthetic-string-session")
        self.args = args
        self.kwargs = kwargs
        self.qr = FakeQRLogin()
        self.qr_logins = 0
        self.logged_out = False
        self.me_phone: str | None = None
        self.me_username: str | None = None

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send_code_request(self, phone: str) -> Any:
        if self.send_code_error is not None:
            raise self.send_code_error
        return SimpleNamespace(phone_code_hash="synthetic-hash")

    async def sign_in(self, **kwargs: Any) -> None:
        self.sign_ins.append(kwargs)
        if self.sign_in_error is not None:
            error, self.sign_in_error = self.sign_in_error, None
            raise error

    async def get_me(self) -> Any:
        return types.User(
            id=4242,
            first_name="Synthetic",
            last_name="User",
            username=self.me_username,
            phone=self.me_phone,
        )

    async def qr_login(self) -> FakeQRLogin:
        self.qr_logins += 1
        return self.qr

    async def log_out(self) -> bool:
        self.logged_out = True
        return True


def _client(**kwargs: Any) -> tuple[TelethonLoginClient, FakeTelethon]:
    holder: list[FakeTelethon] = []

    def factory(*args: Any, **kw: Any) -> FakeTelethon:
        holder.append(FakeTelethon(*args, **kw))
        return holder[0]

    client = TelethonLoginClient(1, "hash", client_factory=factory, **kwargs)
    return client, holder[0]


async def test_happy_path_with_password() -> None:
    client, fake = _client()
    await client.send_code("+79000000000")
    assert fake.connected
    fake.sign_in_error = SessionPasswordNeededError(request=None)
    assert await client.sign_in_code("12345") is True
    await client.sign_in_password("synthetic")
    result = await client.complete()
    assert result.session_string == "synthetic-string-session"
    assert result.telegram_user_id == 4242 and result.display_name == "Synthetic User"
    assert fake.sign_ins[0]["phone_code_hash"] == "synthetic-hash"
    await client.close()
    assert not fake.connected


async def test_code_without_password() -> None:
    client, _ = _client()
    await client.send_code("+79000000000")
    assert await client.sign_in_code("12345") is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PhoneCodeInvalidError(request=None), InvalidCode),
        (PhoneCodeExpiredError(request=None), CodeExpired),
        (FloodWaitError(request=None, capture=30), LoginFlood),
    ],
)
async def test_code_errors(error: Exception, expected: type[Exception]) -> None:
    client, fake = _client()
    await client.send_code("+79000000000")
    fake.sign_in_error = error
    with pytest.raises(expected):
        await client.sign_in_code("12345")


async def test_phone_and_password_errors() -> None:
    client, fake = _client()
    fake.send_code_error = PhoneNumberInvalidError(request=None)
    with pytest.raises(InvalidPhone):
        await client.send_code("+1")
    with pytest.raises(CodeExpired):  # no code requested yet
        await client.sign_in_code("12345")
    fake.send_code_error = None
    await client.send_code("+79000000000")
    fake.sign_in_error = PasswordHashInvalidError(request=None)
    with pytest.raises(InvalidPassword):
        await client.sign_in_password("wrong")


# --- QR login -----------------------------------------------------------------------------------


async def test_start_qr_connects_and_returns_code() -> None:
    client, fake = _client()
    code = await client.start_qr()
    assert fake.connected and fake.qr_logins == 1
    assert code == QrCode(url="tg://login?token=synthetic-1", expires_at=EXPIRES)


async def test_refresh_qr_recreates_token() -> None:
    client, fake = _client()
    await client.start_qr()
    code = await client.refresh_qr()
    assert fake.qr_logins == 1  # same QRLogin, new token
    assert code.url == "tg://login?token=synthetic-2"
    assert code.expires_at == EXPIRES + timedelta(seconds=30)


async def test_refresh_qr_without_start_starts() -> None:
    client, fake = _client()
    code = await client.refresh_qr()
    assert fake.qr_logins == 1 and code.url == "tg://login?token=synthetic-1"


async def test_wait_qr_outcomes() -> None:
    client, fake = _client()
    await client.start_qr()
    fake.qr.wait_result = types.User(id=4242, first_name="Synthetic")
    assert await client.wait_qr(12.5) is QrOutcome.authorized
    assert fake.qr.timeouts == [12.5]
    fake.qr.wait_error = TimeoutError()
    assert await client.wait_qr(1) is QrOutcome.expired


async def test_wait_qr_flood() -> None:
    client, fake = _client()
    await client.start_qr()
    fake.qr.wait_error = FloodWaitError(request=None, capture=17)
    with pytest.raises(LoginFlood) as exc_info:
        await client.wait_qr(1)
    assert exc_info.value.seconds == 17


async def test_wait_qr_before_start_fails() -> None:
    client, _ = _client()
    with pytest.raises(LoginError):
        await client.wait_qr(1)


async def test_qr_with_password_then_complete() -> None:
    client, fake = _client()
    fake.me_phone = "79005550134"
    fake.me_username = "synthetic_user"
    await client.start_qr()
    fake.qr.wait_error = SessionPasswordNeededError(request=None)
    assert await client.wait_qr(1) is QrOutcome.password_needed
    await client.sign_in_password("synthetic")
    assert fake.sign_ins == [{"password": "synthetic"}]
    result = await client.complete()
    assert result.session_string == "synthetic-string-session"
    assert result.telegram_user_id == 4242
    assert result.username == "synthetic_user"
    assert result.masked_phone == "+7 ••• ••• •• 34"


async def test_complete_hidden_phone() -> None:
    client, _ = _client()
    result = await client.complete()
    assert result.masked_phone is None and result.username is None


async def test_log_out() -> None:
    client, fake = _client()
    await client.log_out()
    assert fake.logged_out


async def test_device_settings_passed_to_telethon() -> None:
    _, fake = _client(device_model="Scribe Agent", system_version="server", app_version="9.9.9")
    assert fake.kwargs == {
        "device_model": "Scribe Agent",
        "system_version": "server",
        "app_version": "9.9.9",
    }


@pytest.mark.parametrize(
    ("phone", "masked"),
    [
        ("79005550134", "+7 ••• ••• •• 34"),
        ("+7 900 555-01-34", "+7 ••• ••• •• 34"),
        ("15550100199", "+1 ••• ••• •• 99"),
        ("491510000042", "+49 ••• ••• •• 42"),
        ("380500000077", "+380 ••• ••• • 77"),
        (None, None),
        ("", None),
    ],
)
def test_mask_phone(phone: str | None, masked: str | None) -> None:
    assert mask_phone(phone) == masked
