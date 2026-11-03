# Data Model: Telegram QR-Code Login

Changes to the schema in `specs/001-telegram-reply-agent/data-model.md`. One Alembic
migration: `alembic/versions/0002_telegram_login_method.py`. No new tables.

## telegram_sessions (changed)

| Field | Type | Notes |
|---|---|---|
| user_id | FK unique | unchanged |
| session_string | **enc** | unchanged; same Telethon `StringSession` for both methods |
| status | enum | unchanged: `active`, `revoked`, `error` |
| last_error | text, nullable | unchanged |
| **login_method** | enum `telegram_login_method`: `phone_code`, `qr` | NEW, not null. Backfill existing rows with `phone_code`. Set on every successful login (setup or reconnect). |
| **connected_at** | timestamptz | NEW, not null. Time of the last successful login. Backfill with `created_at`. |
| **account_label** | **enc**, nullable | NEW. The connected Telegram account as shown to the user, e.g. `Anna Petrova (@anna_p)` or `Anna Petrova (+7 ••• ••• •• 34)` (phone only ever masked). Refreshed on every successful login. Null for rows stored before migration 0002; readers then fall back to `users.display_name`. Used by the `/telegram` status card and the wrong-account message. |

Rules:
- The row is written only by the shared finish step, after all account checks pass
  (research Q6). No row exists for an attempt that has not finished.
- On reconnect, the row is updated in place (`session_string`, `status=active`,
  `last_error=null`, `login_method`, `connected_at`); its id is kept.
- "This is not my account" (setup only) deletes the row and clears
  `users.telegram_user_id`.

## users (unchanged columns, new rule)

- `telegram_user_id` stays unique across users (FR-013). On reconnect it must equal the
  `me.id` of the newly authorised account, otherwise the login is rejected and logged out
  (FR-011).
- `state` is changed by **neither** a disconnect nor a reconnect. Disconnect via
  `/telegram` only logs the session out and sets `telegram_sessions.status = revoked`,
  exactly like a session ended from Telegram's Active sessions; reading and drafting
  stop because no client runs for a non-active session (001 FR-030). This replaces 001's
  "user → `setting_up`" for disconnect, so a paused or out-of-office user comes back in
  the same state, and `/start` never re-runs setup for a user who has finished it.
  `setting_up` therefore always means "setup not finished yet".

## activity_records (new kind)

New `activity_kind` enum value: `telegram_login`. One row per finished login attempt.

`meta_json` (metadata only, never secrets):

| Key | Type | Values |
|---|---|---|
| method | string | `qr`, `phone_code` (the method in use when the attempt ended) |
| purpose | string | `setup`, `reconnect` |
| outcome | string | `connected`, `expired` (QR round ran out or password timeout), `cancelled` (user switched method, `/cancel`, new attempt), `wrong_account`, `account_taken`, `not_my_account`, `failed`, `interrupted` (service restart) |
| qr_codes_shown | int | 0 for phone attempts |
| password_needed | bool | |
| duration_s | int | from attempt start to end |
| error_kind | string, nullable | exception class name only, e.g. `LoginFlood` |

Rows with outcome `interrupted` written by the restart cleanup have `duration_s = null`
(the start time was lost) and `method = null` when the state does not tell the method
(`connect_password`).

A method switch ends the current attempt (`cancelled`) and starts a new one, so per-method
success rates (SC-002, SC-003) count each method separately. `not_my_account` is written
as a second row when the user presses the button after a `connected` attempt.

## Login attempt (in memory, not persisted)

Held by `LoginManager`, keyed by control chat id; at most one per chat (FR-009).

| Field | Notes |
|---|---|
| user_id | |
| purpose | `setup` / `reconnect` |
| method | `qr` / `phone_code` |
| client | the unauthorised `LoginClient` |
| started_at | for `duration_s` |
| qr_codes_shown | counter, resets on **Show a new code** |
| qr_message_id | control-chat message showing the current code (edited in place) |
| password_needed | set when Telegram asks for the 2FA password |
| task | background `asyncio.Task` for the QR wait loop / password timeout |

State transitions:

```text
choose ──qr──> qr_waiting ──scan──> (password_needed? password : finishing)
   │              │  └─expired x5──> qr_paused ──"Show a new code"──> qr_waiting
   │              └─"Use phone number instead"──> phone (cancel + drain QR client)
   └─phone──> phone ──code sent──> code ──(password?)──> password ──> finishing
password ──10 min idle──> ended(expired)
finishing ──checks ok──> ended(connected) ──setup──> pick_chats
                                          └─reconnect──> client started, previous user state kept
finishing ──wrong account / taken──> ended(...) + log_out()
any ──/cancel, new attempt, method switch──> ended(cancelled) + drain (research Q4)
any ──service restart──> ended(interrupted) at next boot (research Q10)
```

## Settings (config, not DB)

New fields in `src/scribe/config.py` (`Settings`), environment-overridable:
`tg_qr_max_codes: int = 5`, `tg_login_password_timeout_s: int = 600`,
`tg_qr_same_device_link: bool = False`, `tg_device_model: str = "Scribe Agent"`,
`tg_system_version: str = "server"`, `tg_app_version: str` (defaults to the package version).
