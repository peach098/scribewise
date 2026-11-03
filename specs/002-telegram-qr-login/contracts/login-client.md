# Contract: Login Client and Login Manager (delta)

Extends `src/scribe/telegram/login.py`. The control bot depends only on these
interfaces; tests use a fake client.

## LoginClient (Protocol) — extended

Existing methods are unchanged: `send_code(phone)`, `sign_in_code(code) -> bool`,
`sign_in_password(password)`, `complete() -> LoginResult`, `close()`.

New methods:

```python
@dataclass(frozen=True)
class QrCode:
    url: str  # tg://login?token=...; never logged or stored
    expires_at: datetime  # UTC, from Telegram


class LoginClient(Protocol):
    async def start_qr(self) -> QrCode:
        """Connect if needed and export the first login token."""

    async def refresh_qr(self) -> QrCode:
        """Export a new token for the same client (previous one expires on its own)."""

    async def wait_qr(self, timeout_s: float) -> QrOutcome:
        """Wait until the current token is approved or expires.

        Returns QrOutcome.authorized, QrOutcome.password_needed or QrOutcome.expired.
        Raises LoginFlood on FloodWait, LoginError for anything else.
        """

    async def log_out(self) -> None:
        """Terminate the just-authorised session (wrong account, taken, not-my-account,
        or a drained attempt that got scanned late)."""
```

`QrOutcome` is an enum: `authorized`, `password_needed`, `expired`.

`LoginResult` gains (still no phone number stored anywhere):

```python
@dataclass(frozen=True)
class LoginResult:
    session_string: str
    telegram_user_id: int
    display_name: str
    username: str | None  # NEW
    masked_phone: str | None  # NEW, e.g. "+7 ••• ••• •• 34"; for display only
```

`TelethonLoginClient` implements these with `client.qr_login()`, `QRLogin.recreate()`,
`QRLogin.wait()` (`asyncio.TimeoutError` → `expired`, `SessionPasswordNeededError` →
`password_needed`) and `client.log_out()`. It creates its `TelegramClient` with the
`device_model` / `system_version` / `app_version` settings (research Q7).

## LoginManager — extended

```python
@dataclass
class LoginAttempt:
    user_id: uuid.UUID
    purpose: Literal["setup", "reconnect"]
    method: Literal["qr", "phone_code"]
    client: LoginClient
    started_at: datetime
    qr_codes_shown: int = 0
    qr_message_id: int | None = None
    qr_expires_at: datetime | None = None  # expiry of the last shown token; used by drain
    password_needed: bool = False
    task: asyncio.Task[None] | None = None


class LoginManager:
    async def begin(self, chat_id: int, *, user_id, purpose, method) -> LoginAttempt:
        """Ends (and drains) any previous attempt for this chat first (FR-009)."""

    def get(self, chat_id: int) -> LoginAttempt | None: ...

    async def end(self, chat_id: int, outcome: str, *, drain: bool = False) -> None:
        """Cancel the task, record the activity row (data-model.md), close the client.
        With drain=True and a QR token still valid, keep listening until it expires and
        log_out() if it gets approved (research Q4)."""

    async def close_all(self) -> None:
        """Shutdown: end every attempt with outcome 'interrupted'."""
```

The attempt is recorded exactly once, in `end()`. Callers never write the
`telegram_login` activity row themselves.

## Finish step (shared by both methods and both purposes)

`connect_flow.finish_login(bot, chat_id, attempt, ...)`:

1. `result = await attempt.client.complete()`.
2. Reconnect and `result.telegram_user_id != users.telegram_user_id` → `log_out()`,
   outcome `wrong_account`.
3. Another user has this `telegram_user_id` → `log_out()`, outcome `account_taken`.
4. Upsert `telegram_sessions` with `login_method` and `connected_at`; set
   `users.telegram_user_id`; commit.
5. `client_pool.start(user_id)`; outcome `connected`.
6. Setup → "Connected as …" + **This is not my account** + chat picker.
   Reconnect → "Reconnected as …", clear FSM, keep `users.state`.

## MessagingGateway.log_out (new)

Added to the `MessagingGateway` Protocol (`src/scribe/telegram/gateway.py`), `ClientPool`,
the per-user client in `user_client.py` and `FakeMessagingGateway`:

```python
async def log_out(self, user_id: uuid.UUID) -> None:
    """Terminate the user's Telegram session (auth.logOut) and stop their client.

    If no client is running, a temporary client is started from the stored session string
    just to log out. A session that is already revoked counts as success. Never raises for
    an unauthorised session; transient network errors propagate.
    """
```

Used by **This is not my account** (setup), `/telegram` → **Disconnect**, and 001's
`/delete_my_data` (T069).

## QR image

`src/scribe/telegram/qr_image.py`: `render_qr_png(url: str) -> bytes` (segno, error
correction M, scale so the PNG is ~512 px). Pure function, unit-tested by decoding the
PNG size and checking it is deterministic for the same input. The URL is passed only to
this function and to the optional URL button.
