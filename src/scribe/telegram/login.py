"""Interactive Telegram user login: QR code or phone -> code, then optional 2FA password.

The control bot drives a :class:`LoginClient`; the production implementation wraps Telethon
(research R1, 002 research Q1). A login spans several bot messages, so the unauthorised client
lives in a :class:`LoginManager` keyed by the control chat id, in memory only: the phone number,
code, password and QR login token are never persisted or logged. After a restart mid-login the
user simply starts again.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from telethon import TelegramClient, utils
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from scribe.clock import Clock, utcnow
from scribe.logging import get_logger

log = get_logger("scribe.telegram.login")


class LoginError(Exception):
    """Base class; messages never contain the phone, code or password."""


class InvalidPhone(LoginError):
    pass


class InvalidCode(LoginError):
    pass


class CodeExpired(LoginError):
    pass


class InvalidPassword(LoginError):
    pass


class LoginFlood(LoginError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"telegram asks to wait {seconds}s")
        self.seconds = seconds


@dataclass(frozen=True)
class LoginResult:
    session_string: str
    telegram_user_id: int
    display_name: str
    username: str | None = None
    masked_phone: str | None = None  # e.g. "+7 ••• ••• •• 34"; for display only, never stored


@dataclass(frozen=True)
class QrCode:
    url: str  # tg://login?token=...; never logged or stored
    expires_at: datetime  # UTC, set by Telegram


class QrOutcome(enum.StrEnum):
    authorized = "authorized"
    password_needed = "password_needed"
    expired = "expired"


class LoginClient(Protocol):
    async def send_code(self, phone: str) -> None: ...
    async def sign_in_code(self, code: str) -> bool:
        """True when the account has a 2FA password that is needed next."""
        ...

    async def sign_in_password(self, password: str) -> None: ...
    async def start_qr(self) -> QrCode:
        """Connect if needed and export the first login token."""
        ...

    async def refresh_qr(self) -> QrCode:
        """Export a new token for the same client (the previous one expires on its own)."""
        ...

    async def wait_qr(self, timeout_s: float) -> QrOutcome:
        """Wait until the current token is approved or expires.

        Raises :class:`LoginFlood` on FloodWait, :class:`LoginError` for anything else.
        """
        ...

    async def log_out(self) -> None:
        """Terminate the just-authorised session (wrong account, taken, late scan)."""
        ...

    async def complete(self) -> LoginResult: ...
    async def close(self) -> None: ...


# Country calling codes of one and two digits (ITU-T E.164); every other code has three.
_CC1 = frozenset({"1", "7"})
_CC2 = frozenset(
    {"20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46"}
    | {"47", "48", "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62"}
    | {"63", "64", "65", "66", "81", "82", "84", "86", "90", "91", "92", "93", "94", "95", "98"}
)


def mask_phone(phone: str | None) -> str | None:
    """Country code and last two digits only, e.g. ``79005550134`` -> ``+7 ••• ••• •• 34``."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    cc_len = 1 if digits[:1] in _CC1 else 2 if digits[:2] in _CC2 else 3
    hidden = max(len(digits) - cc_len - 2, 0)
    groups = ["•" * min(3, hidden - i) for i in range(0, hidden, 3)]
    return " ".join([f"+{digits[:cc_len]}", *groups, digits[-2:]])


LoginClientFactory = Callable[[], LoginClient]
ActivityRecorder = Callable[[uuid.UUID, dict[str, Any]], Awaitable[None]]
LoginPurpose = Literal["setup", "reconnect"]
LoginMethod = Literal["qr", "phone_code"]
LoginOutcome = Literal[
    "connected",
    "expired",
    "cancelled",
    "wrong_account",
    "account_taken",
    "not_my_account",
    "failed",
    "interrupted",
]


@dataclass
class LoginAttempt:
    """One login in progress for a control chat (data-model.md "Login attempt")."""

    user_id: uuid.UUID
    purpose: LoginPurpose
    method: LoginMethod
    client: LoginClient
    started_at: datetime
    qr_codes_shown: int = 0
    qr_message_id: int | None = None
    qr_expires_at: datetime | None = None  # expiry of the last shown token; used by drain
    password_needed: bool = False
    task: asyncio.Task[None] | None = None


class LoginManager:
    """Pending logins per control chat (in memory); records each attempt once, in :meth:`end`."""

    def __init__(
        self,
        factory: LoginClientFactory,
        *,
        record: ActivityRecorder,
        clock: Clock = utcnow,
        on_late_scan: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._factory = factory
        self._record = record
        self._clock = clock
        self._on_late_scan = on_late_scan
        self._pending: dict[int, LoginAttempt] = {}
        self._drains: dict[asyncio.Task[None], LoginClient] = {}

    async def begin(
        self,
        chat_id: int,
        *,
        user_id: uuid.UUID,
        purpose: LoginPurpose,
        method: LoginMethod,
    ) -> LoginAttempt:
        """Start a new attempt; any previous attempt for this chat is cancelled and drained."""
        await self.end(chat_id, "cancelled", drain=True)
        attempt = LoginAttempt(
            user_id=user_id,
            purpose=purpose,
            method=method,
            client=self._factory(),
            started_at=self._clock(),
        )
        self._pending[chat_id] = attempt
        return attempt

    def get(self, chat_id: int) -> LoginAttempt | None:
        return self._pending.get(chat_id)

    async def end(
        self,
        chat_id: int,
        outcome: LoginOutcome,
        *,
        drain: bool = False,
        error_kind: str | None = None,
    ) -> None:
        """Cancel the attempt's task, record it, and close (or drain) its client.

        With ``drain=True`` and a QR token still valid, the client keeps listening until the
        token expires and logs out if it gets approved after all (research Q4).
        """
        attempt = self._pending.pop(chat_id, None)
        if attempt is None:
            return
        task = attempt.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.wait({task})
        await self._record_attempt(attempt, outcome, error_kind)
        now = self._clock()
        if drain and attempt.qr_expires_at is not None and attempt.qr_expires_at > now:
            remaining = (attempt.qr_expires_at - now).total_seconds()
            drain_task = asyncio.create_task(self._drain(chat_id, attempt.client, remaining))
            self._drains[drain_task] = attempt.client
            drain_task.add_done_callback(lambda t: self._drains.pop(t, None))
            return
        await _close_quietly(attempt.client)

    async def close_all(self) -> None:
        """Shutdown: end every attempt as ``interrupted`` and stop running drains."""
        for chat_id in list(self._pending):
            await self.end(chat_id, "interrupted")
        drains = dict(self._drains)
        for task in drains:
            task.cancel()
        if drains:
            await asyncio.wait(drains)
        # A drain cancelled before it first ran never reaches its own ``finally``.
        for client in drains.values():
            await _close_quietly(client)

    async def _record_attempt(
        self, attempt: LoginAttempt, outcome: LoginOutcome, error_kind: str | None
    ) -> None:
        duration = (self._clock() - attempt.started_at).total_seconds()
        meta: dict[str, Any] = {
            "method": attempt.method,
            "purpose": attempt.purpose,
            "outcome": outcome,
            "qr_codes_shown": attempt.qr_codes_shown,
            "password_needed": attempt.password_needed,
            "duration_s": max(round(duration), 0),
            "error_kind": error_kind,
        }
        try:
            await self._record(attempt.user_id, meta)
        except Exception as exc:  # a lost history row must never fail a login step
            log.error("login_record_failed", user_id=str(attempt.user_id), error=type(exc).__name__)

    async def _drain(self, chat_id: int, client: LoginClient, timeout_s: float) -> None:
        try:
            if await client.wait_qr(timeout_s) is not QrOutcome.authorized:
                return
            # The old code was scanned after it was replaced: undo the authorisation.
            await client.log_out()
            log.info("login_late_scan_logged_out")
            if self._on_late_scan is not None:
                await self._on_late_scan(chat_id)
        except Exception as exc:
            log.warning("login_drain_failed", error=type(exc).__name__)
        finally:
            await _close_quietly(client)


async def _close_quietly(client: LoginClient) -> None:
    # Closing a half-open login must never fail a setup step.
    with contextlib.suppress(Exception):
        await client.close()


class TelethonLoginClient:
    """Telethon implementation; a fresh ``StringSession`` per login attempt."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        *,
        device_model: str = "Scribe Agent",
        system_version: str = "server",
        app_version: str | None = None,
        client_factory: Callable[..., Any] = TelegramClient,
    ) -> None:
        # The device fields name the session in Telegram's "Active sessions" (research Q7).
        self._client = client_factory(
            StringSession(),
            api_id,
            api_hash,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
        )
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self._qr: Any = None

    async def _connect(self) -> None:
        if not self._client.is_connected():
            await self._client.connect()

    async def send_code(self, phone: str) -> None:
        await self._connect()
        try:
            sent = await self._client.send_code_request(phone)
        except (PhoneNumberInvalidError, PhoneNumberBannedError):
            raise InvalidPhone("invalid phone number") from None
        except FloodWaitError as exc:
            raise LoginFlood(int(exc.seconds)) from None
        self._phone = phone
        self._phone_code_hash = sent.phone_code_hash

    async def sign_in_code(self, code: str) -> bool:
        if self._phone is None:
            raise CodeExpired("no code was requested")
        try:
            await self._client.sign_in(
                phone=self._phone, code=code, phone_code_hash=self._phone_code_hash
            )
        except SessionPasswordNeededError:
            return True
        except PhoneCodeInvalidError:
            raise InvalidCode("invalid code") from None
        except PhoneCodeExpiredError:
            raise CodeExpired("code expired") from None
        except PhoneNumberUnoccupiedError:
            raise InvalidPhone("no Telegram account for this number") from None
        except FloodWaitError as exc:
            raise LoginFlood(int(exc.seconds)) from None
        return False

    async def sign_in_password(self, password: str) -> None:
        try:
            await self._client.sign_in(password=password)
        except PasswordHashInvalidError:
            raise InvalidPassword("invalid password") from None
        except FloodWaitError as exc:
            raise LoginFlood(int(exc.seconds)) from None

    async def start_qr(self) -> QrCode:
        await self._connect()
        try:
            self._qr = await self._client.qr_login()
        except FloodWaitError as exc:
            raise LoginFlood(int(exc.seconds)) from None
        return self._qr_code()

    async def refresh_qr(self) -> QrCode:
        if self._qr is None:
            return await self.start_qr()
        try:
            await self._qr.recreate()
        except FloodWaitError as exc:
            raise LoginFlood(int(exc.seconds)) from None
        return self._qr_code()

    def _qr_code(self) -> QrCode:
        return QrCode(url=str(self._qr.url), expires_at=self._qr.expires)

    async def wait_qr(self, timeout_s: float) -> QrOutcome:
        if self._qr is None:
            raise LoginError("no QR code was requested")
        try:
            await self._qr.wait(timeout_s)
        except TimeoutError:  # asyncio.TimeoutError is the same class since Python 3.11
            return QrOutcome.expired
        except SessionPasswordNeededError:
            return QrOutcome.password_needed
        except FloodWaitError as exc:
            raise LoginFlood(int(exc.seconds)) from None
        except Exception as exc:
            raise LoginError(f"QR login failed: {type(exc).__name__}") from None
        return QrOutcome.authorized

    async def log_out(self) -> None:
        await self._client.log_out()

    async def complete(self) -> LoginResult:
        me = await self._client.get_me()
        session_string = self._client.session.save()
        return LoginResult(
            session_string=str(session_string),
            telegram_user_id=int(me.id),
            display_name=utils.get_display_name(me),
            username=me.username or None,
            masked_phone=mask_phone(me.phone),
        )

    async def close(self) -> None:
        await self._client.disconnect()


def telethon_login_factory(
    api_id: int,
    api_hash: str,
    *,
    device_model: str = "Scribe Agent",
    system_version: str = "server",
    app_version: str | None = None,
) -> LoginClientFactory:
    def factory() -> LoginClient:
        return TelethonLoginClient(
            api_id,
            api_hash,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
        )

    return factory
