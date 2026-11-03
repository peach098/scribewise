---

description: "Task list for Telegram QR-Code Login"
---

# Tasks: Telegram QR-Code Login

**Input**: Design documents from `/specs/002-telegram-qr-login/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/control-chat.md, contracts/login-client.md, quickstart.md

**Tests**: Included. Constitution Principle V requires automated tests for deterministic
code, written before or together with it. Integration tests follow the patterns in
`tests/integration/test_us1_setup_flow.py` (`FakeLoginClient`, `offline_bot()`,
`text_update`/`callback_update` from `tests/integration/bot_support.py`, `FrozenClock`,
`LogCapture` from `tests/conftest.py`, `PostgresFSMStorage`). All fixtures use synthetic
values only (no real phone numbers, tokens or passwords).

**Organization**: Tasks are grouped by user story so each story can be implemented and
tested on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story the task belongs to (US1–US4 from spec.md)

## Path Conventions

Single project: `src/scribe/`, `tests/`, `alembic/versions/` at the repository root.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: New dependency and settings

- [X] T001 Add `segno` to `[project].dependencies` in `pyproject.toml` and run `uv lock` to update `uv.lock` (research Q2: pure Python, no transitive deps; never send the token to an external QR service)
- [X] T002 [P] Add settings to `Settings` in `src/scribe/config.py` (env prefix `SCRIBE_`): `tg_qr_max_codes: int = Field(default=5, ge=1)`, `tg_login_password_timeout_s: int = Field(default=600, gt=0)`, `tg_qr_same_device_link: bool = False`, `tg_device_model: str = "Scribe Agent"`, `tg_system_version: str = "server"`, `tg_app_version: str` defaulting to the installed package version (`importlib.metadata.version("scribe")`); extend `tests/unit/test_config.py` with defaults and env overrides (e.g. `SCRIBE_TG_QR_MAX_CODES=3`)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schema, login client, login manager and the shared connect module. The
existing phone flow keeps working through the new module before any story starts.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests for the foundation (write first, expect them to fail)

- [X] T003 [P] Extend `tests/integration/test_models_and_repos.py`: migration `0002` applies and downgrades; an existing `telegram_sessions` row gets `login_method='phone_code'` and `connected_at = created_at`; `upsert_telegram_session(..., method=LoginMethod.qr, connected_at=t)` updates in place keeping the row id; `delete_telegram_session` removes the row; `find_user_by_telegram_user_id` returns the owner or `None`; an `activity_records` row with kind `telegram_login` can be inserted
- [X] T004 [P] Extend `tests/unit/test_telegram_login.py` (existing `FakeTelethon`, add `qr_login()` returning a fake `QRLogin` with `url`, `expires`, `recreate()`, `wait()`, plus `log_out()`): `start_qr` connects and returns `QrCode(url, expires_at)`; `refresh_qr` calls `recreate()` and returns the new URL; `wait_qr` maps success → `QrOutcome.authorized`, `asyncio.TimeoutError` → `QrOutcome.expired`, `SessionPasswordNeededError` → `QrOutcome.password_needed`, `FloodWaitError(seconds=N)` → `LoginFlood(N)`; after `password_needed`, `sign_in_password` works as in the phone flow; `complete()` returns `username` and `masked_phone` (e.g. phone `79005550134` → `"+7 ••• ••• •• 34"`; `None` when the phone is hidden); `log_out()` calls the client's `log_out`; the `TelegramClient` factory receives `device_model`, `system_version`, `app_version` kwargs
- [X] T005 [P] Create `tests/unit/test_login_manager.py` with a stub `LoginClient` and `FrozenClock`: `begin()` for a chat that already has an attempt ends the old one with outcome `cancelled` and closes its client (FR-009); `end()` cancels the attempt's task, closes the client and writes exactly one `telegram_login` activity record via the injected recorder with keys `method`, `purpose`, `outcome`, `qr_codes_shown`, `password_needed`, `duration_s`, `error_kind` and nothing else; `end(..., drain=True)` with a QR token still valid keeps waiting until `expires_at` and, if the stub reports `authorized`, calls `log_out()` and calls the `on_late_scan(chat_id)` callback; `close_all()` ends every attempt with outcome `interrupted`
- [X] T006 [P] Add log-out tests per contracts/login-client.md "MessagingGateway.log_out": in `tests/unit/test_user_client.py` (`log_out()` calls Telethon `log_out()` and disconnects; an unauthorised session counts as success), and in `tests/integration/test_client_pool.py` (`ClientPool.log_out(user_id)` with a running client logs out and removes it from the pool; with no running client starts a temporary client from the stored session just to log out; a revoked session counts as success and does not send the revoked notice)
- [X] T007 [P] Create `tests/unit/test_qr_image.py`: `render_qr_png("tg://login?token=abc")` returns bytes starting with the PNG signature, image width between 400 and 700 px, identical output for identical input
- [X] T008 [P] Create `tests/unit/test_render_connect.py`: `account_label(display_name, username, masked_phone)` → `"Anna Petrova (@anna_p)"`, falls back to `"Anna Petrova (+7 ••• ••• •• 34)"`, then to the name alone; `mask_phone` keeps country code and last 2 digits only; `qr_caption(n, total, expires_at, tz)` contains the 3 steps, the line `No "Link Desktop Device"? Update Telegram, or use your phone number instead.` and `"Code 2 of 5 · valid until HH:MM:SS"` exactly as in contracts/control-chat.md "QR step"

### Implementation for the foundation

- [X] T009 Update `src/scribe/db/models.py`: add `LoginMethod(enum.StrEnum)` with values `phone_code`, `qr`; add to `TelegramSession` `login_method: Mapped[LoginMethod]` (pg enum `telegram_login_method`, "not null") and `connected_at: Mapped[datetime]` (`DateTime(timezone=True)`, "not null"); add `telegram_login = "telegram_login"` to `ActivityKind`
- [X] T010 Create `alembic/versions/0002_telegram_login_method.py` (down_revision = the 0001 revision id): create enum `telegram_login_method ('phone_code','qr')`; add `telegram_sessions.login_method` and `connected_at` as nullable, backfill "existing rows with `phone_code`" and `connected_at = created_at`, then set both `NOT NULL`; `ALTER TYPE activity_kind ADD VALUE 'telegram_login'` (outside a transaction block via `op.get_context().autocommit_block()`); downgrade drops the columns and the enum type (enum value removal on `activity_kind` is a documented no-op)
- [X] T011 Update `src/scribe/db/repos/users.py`: `upsert_telegram_session(session, user_id, session_string, *, method: LoginMethod, connected_at: datetime)` (sets both fields on insert and update, keeps the row id, resets `status=active`, `last_error=None`); add `delete_telegram_session(session, user_id)` and `find_user_by_telegram_user_id(session, telegram_user_id) -> User | None`; update the existing caller in `src/scribe/telegram/control_bot/setup_flow.py` to pass `method=LoginMethod.phone_code`
- [X] T012 [P] Create `src/scribe/telegram/qr_image.py`: `render_qr_png(url: str) -> bytes` with segno, error correction `"m"`, border 4, scale chosen so the PNG is ~512 px wide; written into a `BytesIO`; the URL is never logged (makes T007 pass)
- [X] T013 Extend `src/scribe/telegram/login.py` per contracts/login-client.md: add `QrCode` (frozen dataclass `url: str`, `expires_at: datetime`), `QrOutcome` (`authorized`, `password_needed`, `expired`), `LoginResult.username: str | None` and `LoginResult.masked_phone: str | None`; extend the `LoginClient` Protocol with `start_qr`, `refresh_qr`, `wait_qr(timeout_s)`, `log_out`; implement them in `TelethonLoginClient` using `client.qr_login()`, `QRLogin.recreate()`, `QRLogin.wait(timeout)` (`asyncio.TimeoutError` → `expired`, `SessionPasswordNeededError` → `password_needed`, `FloodWaitError` → `LoginFlood`) and `client.log_out()`; `TelethonLoginClient.__init__` and `telethon_login_factory` take `device_model`, `system_version`, `app_version` and pass them to the `TelegramClient` factory; phone masking lives in a module-level `mask_phone(phone: str | None) -> str | None` (makes T004 pass)
- [X] T014 Rewrite `LoginManager` in `src/scribe/telegram/login.py` per contracts/login-client.md: `LoginAttempt` dataclass (`user_id`, `purpose: Literal["setup","reconnect"]`, `method: Literal["qr","phone_code"]`, `client`, `started_at`, `qr_codes_shown=0`, `qr_message_id=None`, `qr_expires_at=None`, `password_needed=False`, `task=None`); constructor `LoginManager(factory, *, record: Callable[[uuid.UUID, dict[str, Any]], Awaitable[None]], clock: Clock = utcnow, on_late_scan: Callable[[int], Awaitable[None]] | None = None)`; `begin(chat_id, *, user_id, purpose, method)`, `get(chat_id)`, `end(chat_id, outcome, *, drain=False, error_kind=None)`, `close_all()`; recording happens only in `end()`; outcome values exactly `connected`, `expired`, `cancelled`, `wrong_account`, `account_taken`, `not_my_account`, `failed`, `interrupted`; drain runs as a detached task tracked by the manager so `close_all()` also stops it (makes T005 pass)
- [X] T015 Add `log_out(user_id)` per contracts/login-client.md "MessagingGateway.log_out" to the `MessagingGateway` Protocol in `src/scribe/telegram/gateway.py`, `FakeMessagingGateway` in `src/scribe/telegram/fake_gateway.py` (records calls in `logged_out: list[uuid.UUID]`), the per-user client in `src/scribe/telegram/user_client.py` (`await client.log_out()` then disconnect; `AuthKeyUnregisteredError` → success) and `ClientPool` in `src/scribe/telegram/client_pool.py` (pop + log out the running client, or start a temporary client from the decrypted stored session just to log out; does not call `_mark` or the notifier) (makes T006 pass)
- [X] T016 [P] Add to `src/scribe/telegram/control_bot/render.py`: `account_label(display_name, username, masked_phone)`, `qr_caption(code_no, total, expires_at, tz)` (including the "Update Telegram" line from contracts/control-chat.md "QR step"), `BACKLOG_TEXT` ("📥 {n} messages arrived while you were disconnected — I'm processing them now."), and the connect texts from contracts/control-chat.md as module constants (`METHOD_CHOICE_TEXT`, `QR_EXPIRED_TEXT`, `PASSWORD_PROMPT_V2`, `CONNECTED_TEXT`, `RECONNECTED_TEXT`, `WRONG_ACCOUNT_TEXT`, `ACCOUNT_TAKEN_TEXT`, `LATE_SCAN_TEXT`, `RESTARTED_TEXT`, `LOGIN_TIMEOUT_TEXT`) and `method_choice_keyboard()` with buttons `tglogin:0:qr` and `tglogin:0:phone` (QR first, labelled "(recommended)") (makes T008 pass)
- [X] T017 Create `src/scribe/telegram/control_bot/connect_flow.py` and move the phone login into it: `ConnectStates` (`connect_choose`, `connect_qr`, `connect_phone`, `connect_code`, `connect_password`); move `phone_step`, `code_step`, `password_step`, `_delete_quietly`, `normalize_phone`, and the phone prompts/errors from `setup_flow.py` unchanged in behaviour; FSM data holds only `purpose` (never phone, code, token or password); `begin_connect(bot, chat_id, user, state, purpose)` entry point; shared `finish_login(bot, chat_id, attempt, state, *, session_factory, gateway, client_pool, clock)` implementing contracts/login-client.md "Finish step" steps 1–5 (reconnect with a different `telegram_user_id` → `log_out()` + outcome `wrong_account`; id owned by another user (check with `find_user_by_telegram_user_id` and still catch `IntegrityError`) → `log_out()` + outcome `account_taken`; otherwise upsert session with `method` and `connected_at=clock()`, set `users.telegram_user_id`, commit, start the client, outcome `connected`), then for `purpose="setup"` hand over to `setup_flow.show_chat_picker(...)`; `create_router()` registering the phone/code/password handlers on the new states
- [X] T018 Update `src/scribe/telegram/control_bot/setup_flow.py`: `/start` (new user, or `setting_up` user with no FSM state) calls `connect_flow.begin_connect(..., purpose="setup")`; extract the dialog-listing + picker message from the old `_finish_login` into `show_chat_picker(bot, chat_id, user, state, gateway)`; remove the moved login handlers and the `phone/code/password` states from `SetupStates`; keep `/cancel` working for both state groups (it now calls `telegram_login.end(chat_id, "cancelled", drain=True)`)
- [X] T019 Wire the foundation in `src/scribe/telegram/control_bot/bot.py` (include `connect_flow.create_router()` before `setup_flow`) and `src/scribe/__main__.py` (build `LoginManager` with `telethon_login_factory(api_id, api_hash, device_model=settings.tg_device_model, system_version=settings.tg_system_version, app_version=settings.tg_app_version)`, a `record` function that inserts an `activity_records` row of kind `telegram_login` with the given `meta_json`, and pass `client_pool` into the dispatcher workflow data; call `telegram_login.close_all()` on shutdown)
- [X] T020 Update `tests/integration/test_us1_setup_flow.py` for the new structure: imports from `connect_flow`; `FakeLoginClient` gets `start_qr/refresh_qr/wait_qr/log_out` stubs and returns `username`/`masked_phone`; the existing phone scenario now starts by pressing `tglogin:0:phone` after `/start`; assert the stored session has `login_method='phone_code'` and a `telegram_login` activity row with outcome `connected` (FR-003)

**Checkpoint**: `make test-fast test-integration` green; phone login works through `connect_flow`; QR code not yet offered to users.

---

## Phase 3: User Story 1 - Connect my Telegram account by scanning a QR code (Priority: P1) 🎯 MVP

**Goal**: During setup, the user picks "Scan a QR code", scans it from another signed-in device, and continues to choosing work chats without sharing a phone number or code.

**Independent Test**: `/start` → **Scan a QR code** → scan from a second device → "Connected as …" within 10 s → chat picker; no phone or code asked (quickstart scenarios 1–4, 7, 11).

### Tests for User Story 1 (write first, expect them to fail)

- [X] T021 [P] [US1] Create `tests/integration/test_connect_flow.py` with a scripted `FakeQrLoginClient` (queue of `QrOutcome`s per `wait_qr` call, counts `refresh_qr`, records `log_out`) and the `Setup` harness pattern from `test_us1_setup_flow.py`; tests: `test_start_offers_qr_first` (method choice keyboard: first button `tglogin:0:qr`, label contains "recommended"); `test_qr_happy_path_without_password` (photo sent via `SendPhoto`, then caption/`EditMessageMedia` removed or replaced by text, "Connected as Me (@me)" with `tglogin:0:notme` button, then chat picker; session row `login_method='qr'`; activity outcome `connected`, `qr_codes_shown=1`); `test_qr_refreshes_and_stops_after_max_codes` (5 × `expired` → 4 `EditMessageMedia` calls, then the image is replaced by `QR_EXPIRED_TEXT` with **Show a new code**; activity outcome `expired`, `qr_codes_shown=5`; pressing `tglogin:0:qr` starts a new round); `test_switch_to_phone_cancels_and_drains_qr` (press `tglogin:0:phone` while waiting → phone prompt, previous attempt `cancelled`, QR message no longer has a photo); `test_late_scan_after_cancel_is_logged_out` (drain gets `authorized` → `log_out()` called, `LATE_SCAN_TEXT` sent, no `telegram_sessions` row); `test_second_start_cancels_first_attempt` (FR-009); `test_not_my_account_logs_out_and_returns_to_choice` (FR-012: session row deleted, `users.telegram_user_id` cleared, method choice shown, activity outcome `not_my_account`); `test_account_taken_is_logged_out` (FR-013: second user, same `telegram_user_id` → `log_out()`, `ACCOUNT_TAKEN_TEXT`, no row); `test_flood_offers_other_method`; `test_no_secrets_in_logs_or_fsm` (with `LogCapture` and a direct read of `bot_fsm_state`: the fake token URL `tg://login?token=SYNTHETIC` and the masked/unmasked fake phone never appear)

### Implementation for User Story 1

- [X] T022 [US1] Implement the method choice in `src/scribe/telegram/control_bot/connect_flow.py`: `begin_connect` sets `connect_choose`, sends `METHOD_CHOICE_TEXT` with `method_choice_keyboard()`; callback handler for `tglogin:0:qr` / `tglogin:0:phone` / `tglogin:0:cancel` (valid in any `ConnectStates` state); `tglogin:0:phone` ends any QR attempt with `cancelled` + `drain=True`, then shows the phone prompt with an extra **📷 Use a QR code instead** button (`tglogin:0:qr`); add the same button to the code prompt
- [X] T023 [US1] Implement the QR round in `src/scribe/telegram/control_bot/connect_flow.py`: on `tglogin:0:qr` → `telegram_login.begin(..., method="qr")`, `start_qr()`, send the PNG from `render_qr_png` with `bot.send_photo(BufferedInputFile(...), caption=qr_caption(...), reply_markup=...)`, store `qr_message_id`; start `attempt.task = asyncio.create_task(_qr_loop(...))` which repeats `wait_qr(timeout until expires_at)`: `expired` → if `qr_codes_shown < settings.tg_qr_max_codes` then `refresh_qr()` + `bot.edit_message_media(InputMediaPhoto(...))` else edit the message to `QR_EXPIRED_TEXT` (text only, buttons **Show a new code** `tglogin:0:qr` and **Use phone number instead**) and `end(chat_id, "expired")`; `authorized` → remove the photo (edit to "✅ Code accepted.") and `finish_login(...)`; `password_needed` → hand over to the password step (US2; until then treat as `failed` with a clear message); `LoginFlood` → text "Telegram asks to wait N s…" + other method button and outcome `failed` with `error_kind="LoginFlood"`; any other exception → `TELEGRAM_UNAVAILABLE_TEXT`, outcome `failed`; the loop gets the chat's FSM context via `dispatcher.fsm.get_context(bot, chat_id, user_id)`
- [X] T024 [US1] Implement the same-device link in `src/scribe/telegram/control_bot/connect_flow.py`: when `settings.tg_qr_same_device_link` is true, the QR message keyboard gets a first row URL button **📲 Approve on this device** with `url=qr.url`, updated on each refresh; when false, no such button (research Q5, FR-010)
- [X] T025 [US1] Implement "Connected as" and **This is not my account** in `src/scribe/telegram/control_bot/connect_flow.py`: after `finish_login` succeeds with `purpose="setup"`, send `CONNECTED_TEXT.format(account=account_label(...))` with button `tglogin:0:notme`, then the chat picker; handler for `tglogin:0:notme` accepted only while the user is `setting_up`: `gateway.log_out(user_id)` (stops the client and ends the Telegram session), `delete_telegram_session`, clear `users.telegram_user_id`, record a `telegram_login` activity row with outcome `not_my_account`, clear FSM and show the method choice again
- [X] T026 [US1] Implement `on_late_scan(bot)` factory in `src/scribe/telegram/control_bot/connect_flow.py` returning a callback that sends `LATE_SCAN_TEXT` to the chat; `LoginManager` must have called `log_out()` on the drained client before invoking it (research Q4); pass it as `LoginManager(..., on_late_scan=...)` in `src/scribe/__main__.py`
- [X] T027 [US1] Add structured logs in `src/scribe/telegram/control_bot/connect_flow.py`: `login_step` (`user_id`, `method`, `step` in `choose|qr_shown|qr_refreshed|scanned|password_asked|finished`, `code_no`) and `login_ended` (`user_id`, `method`, `purpose`, `outcome`, `duration_s`); never the URL, token, phone or password (Constitution VI, FR-008)

**Checkpoint**: A new user can connect by QR code end-to-end (accounts without 2FA). Phone flow still works. MVP shippable if 2FA accounts are told to use the phone method.

---

## Phase 4: User Story 2 - Complete the QR login when my account has two-step verification (Priority: P1)

**Goal**: After the scan, accounts with two-step verification are asked for the password, which is deleted from the chat and never stored; the attempt times out after 10 minutes.

**Independent Test**: With a 2FA account: scan → password prompt → wrong password → right password → connected; both password messages deleted (quickstart scenarios 5–6).

### Tests for User Story 2 (write first, expect them to fail)

- [X] T028 [P] [US2] Add to `tests/integration/test_connect_flow.py`: `test_qr_with_password_wrong_then_right` (`wait_qr` → `password_needed`; prompt equals `PASSWORD_PROMPT_V2`; wrong password → "Wrong password. Please send it again."; right password → connected; every password message deleted via `DeleteMessage`; activity `password_needed=true`, outcome `connected`); `test_password_timeout_after_10_minutes` (advance `FrozenClock`/patch the timeout to `settings.tg_login_password_timeout_s`; `LOGIN_TIMEOUT_TEXT` + method choice; outcome `expired`; the password state is cleared); `test_phone_flow_password_also_times_out`; `test_password_never_in_logs_or_fsm`

### Implementation for User Story 2

- [X] T029 [US2] In `src/scribe/telegram/control_bot/connect_flow.py`, route `QrOutcome.password_needed` from the QR loop to `connect_password`: set `attempt.password_needed = True`, set FSM state `connect_password`, send `PASSWORD_PROMPT_V2` (contracts/control-chat.md "Password step"); the existing `password_step` handler serves both methods (it reads the attempt from `telegram_login.get(chat_id)` and calls `sign_in_password`, then `finish_login`)
- [X] T030 [US2] Implement the password timeout in `src/scribe/telegram/control_bot/connect_flow.py`: when entering `connect_password` (either method), replace `attempt.task` with a timer task sleeping `settings.tg_login_password_timeout_s` that then calls `telegram_login.end(chat_id, "expired")`, clears the FSM state and sends `LOGIN_TIMEOUT_TEXT` with the method choice keyboard; a successful or failed password submission cancels the timer
- [X] T031 [US2] Verify `_delete_quietly` runs for every message received in `connect_password` before any other processing (including when the attempt is gone), in `src/scribe/telegram/control_bot/connect_flow.py`

**Checkpoint**: QR login works for accounts with and without two-step verification.

---

## Phase 5: User Story 3 - Reconnect my Telegram account with a QR code (Priority: P2)

**Goal**: A user whose connection ended reconnects from the control chat with either method; settings, chats and profiles are untouched; a different account is refused. Implements 001 task T071 (`/telegram`).

**Independent Test**: Connected user → session terminated from Telegram → notice with **Reconnect** → QR → "Reconnected as …"; `/chats`, `/settings`, `/profile` unchanged; a new message is drafted (quickstart scenarios 8–10).

### Tests for User Story 3 (write first, expect them to fail)

- [X] T032 [P] [US3] Create `tests/integration/test_reconnect_flow.py`: `test_revoked_notice_has_reconnect_button` (`ClientPool` marks a session `revoked` → notifier message text mentions `/telegram` and has `tgreconnect:0`); `test_reconnect_by_qr_keeps_state_and_data` (user `active` with work chats and a global profile; session `revoked`; press `tgreconnect:0` → method choice; QR authorized with the same `telegram_user_id` → `RECONNECTED_TEXT`; session `status=active`, `login_method='qr'`, same row id; `users.state` still `active`; work chats and profile rows unchanged; `client_pool.start` called; activity `purpose=reconnect`, outcome `connected`); `test_reconnect_by_phone_works`; `test_reconnect_with_different_account_is_refused` (FR-011: `log_out()` called, `WRONG_ACCOUNT_TEXT`, session row unchanged and still `revoked`, outcome `wrong_account`); `test_reconnect_keeps_paused_state`; `test_start_for_set_up_user_with_revoked_session_points_to_telegram`; `test_disconnect_two_step_keeps_user_state` (paused user: `/telegram` → **Disconnect** → confirm → `gateway.logged_out == [user_id]`, session `status=revoked`, `users.state` still `paused`); `test_start_after_disconnect_does_not_rerun_setup` (after disconnect, `/start` → "Use /telegram to reconnect" + `tgreconnect:0`; no method choice, no chat picker); `test_reconnect_reports_backlog` (2 `waiting` turns with `due_at` in the past → after `RECONNECTED_TEXT` a message equal to `BACKLOG_TEXT.format(n=2)`; with none, no backlog message)

### Implementation for User Story 3

- [X] T033 [US3] Extend `Notifier` in `src/scribe/telegram/client_pool.py` to `Callable[[uuid.UUID, str, InlineKeyboardMarkup | None], Awaitable[None]]` (default `None`), update `REVOKED_NOTICE` / `ERROR_NOTICE` to "… Use /telegram to reconnect." and pass a keyboard with **🔗 Reconnect** (`tgreconnect:0`); update the notifier implementation in `src/scribe/__main__.py` and `tests/integration/test_client_pool.py`
- [X] T034 [US3] Implement `/telegram` in `src/scribe/telegram/control_bot/telegram_command.py` (implemented in its own module instead of `connect_flow.py` to avoid parallel edit conflicts) (moves 001 T071 here): status card per contracts/control-chat.md "`/telegram` status card" with account and status (method/time line added in US4); **🔌 Disconnect** (`tgdisconnect:0`) with a two-step confirmation (`tgdisconnect:1`) → `gateway.log_out(user_id)`, `set_telegram_session_status(revoked)`; `users.state` is **not** changed (data-model.md "users", research Q9; replaces 001's "user → `setting_up`"); waiting turns left untouched (001 FR-030); **🔗 Reconnect** (`tgreconnect:0`) shown when the session is `revoked` or `error`
- [X] T035 [US3] Implement reconnect in `src/scribe/telegram/control_bot/connect_flow.py`: `tgreconnect:0` handler → `begin_connect(..., purpose="reconnect")` (only if the session is not `active`); in `finish_login` for `purpose="reconnect"`: wrong-account check against `users.telegram_user_id` (FR-011), update the session row in place, `client_pool.start(user_id)`, send `RECONNECTED_TEXT.format(account=...)`, clear FSM, do **not** change `users.state`; then count the user's `conversation_turns` in state `waiting` with `due_at <= clock()` (add `count_due_waiting(session, user_id, now)` to `src/scribe/db/repos/turns.py`) and, if > 0, send `BACKLOG_TEXT.format(n=count)` (research Q9 "Backlog after reconnect")
- [X] T036 [US3] Update `start_command` in `src/scribe/telegram/control_bot/setup_flow.py`: a user not in `setting_up` (setup finished) whose session is `revoked`/`error` — including after **Disconnect** — gets "Your Telegram account is disconnected. Use /telegram to reconnect." with the **🔗 Reconnect** button; add `/telegram` to `COMMANDS` / `/help` in `src/scribe/telegram/control_bot/bot.py`

**Checkpoint**: Reconnect works with both methods; settings and profiles survive.

---

## Phase 6: User Story 4 - Know which method connected my account and how to end it (Priority: P3)

**Goal**: `/telegram` shows the method and time of connection and how to end the session; the session appears as "Scribe Agent" in Telegram's Active sessions.

**Independent Test**: Connect by QR; `/telegram` shows "via QR code" and the time; Active sessions lists "Scribe Agent" (quickstart scenarios 12–13).

### Tests for User Story 4 (write first, expect them to fail)

- [X] T037 [P] [US4] Add to `tests/integration/test_reconnect_flow.py`: `test_status_card_shows_method_and_time` (session `login_method='qr'`, `connected_at` fixed → card contains "via QR code" and the time in the user's time zone formatted `20 Oct 2026, 14:03`, plus the "Telegram → Settings → Devices → \"Scribe Agent\" → Terminate session" line; for `phone_code` → "via phone and code")
- [X] T038 [P] [US4] Extend `tests/unit/test_user_client.py`: the per-user `TelegramClient` factory receives `device_model`, `system_version`, `app_version` from settings

### Implementation for User Story 4

- [X] T039 [US4] Add the "Connected: <date>, <time> · via QR code | via phone and code" line and the terminate-session hint (using `settings.tg_device_model`) to the `/telegram` status card in `src/scribe/telegram/control_bot/connect_flow.py` / `render.py`
- [X] T040 [US4] Pass `device_model`, `system_version`, `app_version` to the `TelegramClient` created in `src/scribe/telegram/user_client.py` and thread them from `Settings` through `ClientPool` construction in `src/scribe/__main__.py` (research Q7, FR-016)

**Checkpoint**: All four stories work independently.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T041 Implement restart cleanup (research Q10) in `src/scribe/telegram/control_bot/connect_flow.py` + startup hook in `src/scribe/__main__.py`: at bot start, select `bot_fsm_state` rows whose state is any `ConnectStates.*`, clear them, record a `telegram_login` activity row with outcome `interrupted` for the owning user, and send `RESTARTED_TEXT` with the method choice keyboard; integration test `test_restart_mid_login_is_cleaned_up` in `tests/integration/test_connect_flow.py` (no `telegram_sessions` row exists afterwards)
- [X] T042 [P] Add callback ownership checks for `tglogin:*`, `tgreconnect:*`, `tgdisconnect:*` (the callback's chat must be the user's control chat; reject otherwise) in `src/scribe/telegram/control_bot/connect_flow.py`, with a test in `tests/integration/test_connect_flow.py`
- [X] T043 [P] Mark 001 task T071 as "moved to 002 (T034–T036)" in `specs/001-telegram-reply-agent/tasks.md`, and add a note to `specs/001-telegram-reply-agent/contracts/control-chat.md` that the "Setup FSM states" line and the `/telegram` row are superseded by `specs/002-telegram-qr-login/contracts/control-chat.md`
- [X] T044 [P] Update `README.md` (or `docs/deployment.md` if present): both login methods, the full-account-access disclosure, the operator step "set the app title for SCRIBE_TG_API_ID to Scribe Agent on my.telegram.org", and the new `SCRIBE_TG_*` settings; add them to `.env.example`
- [X] T045 Run `make lint typecheck test-fast test-integration`; fix any failures
- [ ] T046 Execute `specs/002-telegram-qr-login/quickstart.md` on staging with real accounts A, A2, B (including the manual scenarios 4, 13, 16, the disconnect/backlog scenarios 17–18, the privacy grep and the "Timing" check: scan → connected ≤ 10 s, refresh ≤ 2 s); record the results and the observed maximum times, including whether the same-device link works on iOS / Android / Desktop and the resulting `SCRIBE_TG_QR_SAME_DEVICE_LINK` decision, in `specs/002-telegram-qr-login/checklists/requirements.md` notes

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: depends on Setup; blocks all stories. Inside: T009 → T010 → T011; T013 → T014; T015 after T011; T012 and T016 independent; T017 depends on T011 and T013–T016; T018 → T019 → T020.
- **US1 (Phase 3)**: depends on Phase 2.
- **US2 (Phase 4)**: depends on US1's QR loop (T023) for the hand-over; the timeout part (T030) also covers the phone flow.
- **US3 (Phase 5)**: depends on Phase 2 only for phone reconnect; QR reconnect also needs T023. Can run in parallel with US2.
- **US4 (Phase 6)**: depends on T034 (status card) for T039; T040 depends only on Phase 2.
- **Polish (Phase 7)**: after the desired stories.

### User Story Dependencies

- **US1 (P1)**: independent after Phase 2.
- **US2 (P1)**: extends US1's QR loop.
- **US3 (P2)**: independent of US2; uses US1's QR round for the QR option.
- **US4 (P3)**: extends US3's `/telegram` card; device naming is independent.

### Within Each User Story

- Tests first, confirm they fail, then implement.
- `connect_flow.py` is touched by most story tasks, so tasks within a story that edit it run sequentially.

### Parallel Opportunities

- Phase 1: T001 ∥ T002.
- Phase 2 tests: T003 ∥ T004 ∥ T005 ∥ T006 ∥ T007 ∥ T008; then T012 ∥ T016 alongside T009–T011 and T013–T014.
- After US1: US2 (T028–T031) ∥ US3 (T032–T036); T038 + T040 can start any time after Phase 2.
- Polish: T042 ∥ T043 ∥ T044.

---

## Parallel Example: Foundational tests

```bash
Task: "Extend tests/integration/test_models_and_repos.py for migration 0002 and repo changes"
Task: "Extend tests/unit/test_telegram_login.py for QR methods"
Task: "Create tests/unit/test_login_manager.py"
Task: "Add log-out tests in tests/unit/test_user_client.py and tests/integration/test_client_pool.py"
Task: "Create tests/unit/test_qr_image.py"
Task: "Create tests/unit/test_render_connect.py"
```

## Parallel Example: after User Story 1

```bash
Task: "US2 — T028 password tests in tests/integration/test_connect_flow.py, then T029–T031"
Task: "US3 — T032 tests in tests/integration/test_reconnect_flow.py, then T033–T036"
Task: "US4 — T038 + T040 device naming in src/scribe/telegram/user_client.py"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 + Phase 2 (phone login now runs through `connect_flow`, nothing user-visible changes).
2. Phase 3: QR login for accounts without two-step verification.
3. **Stop and validate** with quickstart scenarios 1–4, 7, 11.
4. Ship only together with US2 if many target users have two-step verification (likely for work accounts); otherwise US1 alone is shippable with the phone method as fallback.

### Incremental Delivery

1. Setup + Foundational → no behaviour change, tests green.
2. US1 → QR login in setup.
3. US2 → QR login with two-step verification (recommended minimum for release).
4. US3 → reconnect via `/telegram` (also fixes today's broken reconnect).
5. US4 → method/time in status, recognisable session name.
6. Polish → restart cleanup, ownership checks, docs, staging run.

---

## Notes

- Never log, store, or put in FSM data: phone numbers, login codes, `tg://login` URLs/tokens, 2FA passwords (FR-008, SC-004).
- Every rejected or abandoned authorised session must be logged out (research Q4, Q6).
- Commit after each task or logical group; keep `make test-fast` green at each checkpoint.
