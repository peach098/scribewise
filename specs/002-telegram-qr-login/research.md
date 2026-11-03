# Research: Telegram QR-Code Login

Decisions for `specs/002-telegram-qr-login/spec.md`. Builds on
`specs/001-telegram-reply-agent/research.md` (R1 Telethon user client, R2 control chat, R7
encryption); only what changes is described here.

## Q1. How QR login works with Telethon

**Decision**: Use Telethon's built-in `TelegramClient.qr_login()` (present in the locked
Telethon 1.45.0) on the same per-attempt `TelegramClient(StringSession())` that the phone
flow already uses in `src/scribe/telegram/login.py`.

How it behaves (read from `telethon/tl/custom/qrlogin.py` and `client/auth.py`):

- `qr_login()` sends `auth.exportLoginToken` and returns a `QRLogin` with `url`
  (`tg://login?token=<base64url>`) and `expires` (server-set, about 30 s).
- `QRLogin.wait(timeout)` must already be running when the code is scanned. It waits for
  `UpdateLoginToken`, then calls `exportLoginToken` again. It follows a DC migration
  (`LoginTokenMigrateTo` → switch DC → `auth.importLoginToken`) and returns the `User` on
  `LoginTokenSuccess`. It raises `asyncio.TimeoutError` when the code expires unscanned.
- If the account has two-step verification, `wait()` raises `SessionPasswordNeededError`.
  After that, `client.sign_in(password=...)` completes the login the same way as the
  phone flow does today. Nothing new is needed for the password step.
- `QRLogin.recreate()` exports a fresh token (new code); the previous token expires on its
  own server-side timer.

**Rationale**: No new Telegram library, the session string is the same `StringSession`
as with phone login, so `client_pool`, `user_client` and encryption (R7) are unchanged
(FR-014).

**Alternatives considered**: calling `auth.exportLoginToken` / `auth.importLoginToken`
directly (re-implements what Telethon already does, including DC migration);
TDLib (heavier native dependency, already rejected in 001 R1).

## Q2. Rendering and showing the QR code

**Decision**: Generate a PNG in memory with **segno** (pure Python, no dependencies,
BSD-3-Clause), send it with `bot.send_photo(BufferedInputFile(...))` and swap it on refresh
with `bot.edit_message_media(...)` on the same message, so the chat always shows exactly
one live code. The caption carries the instructions and the expiry time. When a code is
replaced or the attempt ends, the message is edited to text ("Code expired" / "Connected")
or deleted, so no scannable old image remains in the chat.

**Rationale**: Constitution VI requires each new dependency to be justified: Telegram
cannot render a QR from text, and the token must never leave the service, so a local
generator is needed. segno has no transitive dependencies (no Pillow), is maintained and
type-annotated.

**Alternatives considered**: `qrcode` (needs Pillow or pypng for PNG output); an external
QR-image web service (would send the login token to a third party — rejected outright).

## Q3. Where the QR wait runs

**Decision**: A background `asyncio.Task` per login attempt, owned by the existing
in-memory `LoginManager` (keyed by control chat id). The task loops: show code →
`wait(timeout=until expiry)` → on timeout `recreate()` and swap the image, up to
`tg_qr_max_codes` (5) codes; then it stops and shows a **Show a new code** button. On
success it calls the shared "finish login" step; on `SessionPasswordNeededError` it moves
the FSM to `connect_password` and asks for the password. The bot's FSM context for the
chat is obtained with `dispatcher.fsm.get_context(bot, chat_id, user_id)` because the
task runs outside a handler.

**Rationale**: aiogram handlers must return quickly; the wait can last minutes. Keeping
the task in `LoginManager` means `begin()` / `end()` already give "one attempt per user"
(FR-009) and cleanup on `/cancel`.

**Alternatives considered**: driving the loop from the scheduler table (`scheduled_jobs`)
— persistent, but an MTProto login cannot survive a restart anyway (the unauthorised
client lives in memory), so persistence buys nothing.

## Q4. Making a replaced or cancelled code unusable (FR-005, FR-009)

**Finding**: Telegram has no call to revoke an exported login token. A token is valid
until its `expires` time (~30 s), and if it is scanned the authorisation is granted to the
auth key of the client that exported it.

**Decision**: When an attempt is cancelled or replaced (user switches method, sends
`/cancel`, starts again, or a newer code is shown), the old client is **drained**, not
dropped: it keeps listening until the last shown token's `expires`. If a scan still
arrives in that window, the drain completes the login and immediately calls `log_out()`,
so no stray "Scribe" session is left in the user's Active sessions; the user is told
"That code was no longer active; nothing was connected." Removing the image from the chat
(Q2) stops new scans of the old code. For a refresh inside the same attempt, the client
is the same one, and `recreate()` simply replaces the token it is waiting for.

**Rationale**: This is the only way to honour "the old code stops working" given the
protocol; the window is at most ~30 s and ends in a logged-out session.

## Q5. Same-device approval link (FR-010)

**Finding**: `QRLogin.url` is a `tg://login?token=…` link. Telethon documents that opening
it in a signed-in Telegram app imports the token. Bot API inline keyboards accept `tg://`
URLs. It is **not confirmed** that current Telegram apps (iOS, Android, Desktop) accept
this link when tapped from a chat rather than scanned: Telegram has anti-phishing
protection around login codes and may block or warn.

**Decision**: Implement the link as an extra inline button **Approve on this device**
under the QR image, behind the setting `tg_qr_same_device_link` (default `false`). The
quickstart includes a manual check on iOS, Android and Desktop; the flag is turned on only
if the link works on at least iOS and Android. If it stays off, FR-010's condition
("where the user's Telegram app supports it") is not met and the one-device case is
covered by the phone-and-code fallback.

**Alternatives considered**: always showing the link (risk of a dead or warning-raising
button in the main flow).

## Q6. Wrong account and account already in use (FR-011, FR-012, FR-013)

**Decision**: All checks happen in the shared finish step, after Telegram returns the
`User`, before anything is stored:

- **Reconnect**: if `me.id != users.telegram_user_id`, call `log_out()` on the new client,
  store nothing, and tell the user to scan with the original account (FR-011).
- **Any purpose**: if another user already has this `telegram_user_id` (today detected
  by the unique-index `IntegrityError`), also `log_out()` the new client before replying
  (FR-013). The existing phone flow currently leaves that session alive; this change
  fixes it for both methods.
- **First setup**: after a successful login the bot posts "Connected as <name> (@username
  or +7 ••• ••• •• 34)" with a **This is not my account** button, and moves straight on to
  choosing work chats (no extra tap for the normal case). The button stays valid until
  setup finishes; pressing it logs the session out via `client_pool.stop` + `log_out()`,
  deletes the `telegram_sessions` row, clears `users.telegram_user_id`, and returns to the
  method choice (FR-012).

The phone number shown is masked (country code + last 2 digits) and is never stored.

## Q7. Session name in "Active sessions" (FR-016)

**Finding**: "Active sessions" shows the app name registered for the `api_id` plus the
`device_model` / `system_version` / `app_version` sent in `initConnection`. Telethon
defaults to the host's platform values (e.g. "Linux x86_64"), which users do not
recognise.

**Decision**: Pass `device_model`, `system_version` and `app_version` from new settings
(`tg_device_model="Scribe Agent"`, `tg_system_version="server"`, `tg_app_version` = package
version) to every `TelegramClient` the service creates (login clients **and** the per-user
clients in `user_client.py`), so the name is set at authorisation time and stays the same
on reconnects. The registered app title of the `api_id` should also be "Scribe Agent"
(operator step, in the quickstart).

## Q8. Recording the method and the attempts (FR-015, FR-018)

**Decision**:
- `telegram_sessions` gets `login_method` (`phone_code` | `qr`) and `connected_at`
  (timestamptz). Existing rows are backfilled `phone_code` / `created_at`.
- Each finished attempt writes one `activity_records` row with a new kind
  `telegram_login` and metadata only: `method`, `purpose` (`setup` | `reconnect`),
  `outcome` (`connected` | `expired` | `cancelled` | `wrong_account` | `account_taken` |
  `failed` | `interrupted`), `qr_codes_shown`, `password_needed`, `duration_s`,
  `error_kind`. No phone, code, token or password (FR-008, SC-004).

**Rationale**: `activity_records` already holds metadata-only history per user, is shown
by `/history` and covered by data export/deletion; a separate table would add a model,
repo and retention rule for the same purpose (Constitution VI). SC-001–SC-003 are
computed with SQL over these rows.

## Q9. Reconnect entry point and its relation to 001 task T071

**Finding**: In 001, the revoked-session notice says "Send /start", but `/start` answers
"already set up" for a user who is not in `setting_up`, and `/telegram` (T071) is not
implemented yet. So reconnecting does not work today.

**Decision**: This feature implements `/telegram` (status + **Reconnect** + **Disconnect**)
and takes over 001's T071; T071 is marked as moved to 002. Reconnect does **not** put the
user back into `setting_up`: settings, chats and profiles are untouched, the connect
sub-flow runs with `purpose=reconnect`, and on success the client is started and the
user's previous state (`active` / `paused` / `out_of_office`) is kept (Story 3, SC-006).
The revoked and error notices in `client_pool.py` point to `/telegram` and carry a
**Reconnect** button; `/start` for an already set-up user with a revoked session also
points to `/telegram`.

**Disconnect** (`/telegram` → Disconnect) logs the session out
(`MessagingGateway.log_out`, new) and sets the session to `revoked`, but does **not**
change `users.state`. 001's contract moved the user to `setting_up`, which would make a
later `/start` re-run setup (chat picker, profile import) and lose a paused or
out-of-office state. With this rule a disconnect is handled exactly like a session ended
from Telegram, and reconnect has a single path.

**Backlog after reconnect**: messages that arrived while disconnected are already stored
as waiting turns (the client stopped before reading them, so nothing new is fetched). After
a reconnect, the bot counts the user's `waiting` turns whose `due_at` has passed and, if
any, sends "📥 N messages arrived while you were disconnected — I'm processing them now.";
the scheduler then handles them as usual. 001 never implemented this notice, so it is
built here.

## Q10. Restart during a login (edge case)

**Decision**: The in-memory login is lost on restart, but the FSM state survives in
`bot_fsm_state`. At bot start-up, the service finds chats whose FSM state is one of the
connect states with a login in progress (`connect_qr`, `connect_phone`, `connect_code`,
`connect_password`; `connect_choose` is skipped because no attempt runs there and its
buttons keep working), resets them to `connect_choose`, records `outcome=interrupted`
(skipped if a graceful shutdown already recorded it), and sends "The
service restarted while you were connecting Telegram. Tap to start again." with the
method choice buttons. No `telegram_sessions` row is written before the finish step, so
no half-connected account can exist.

## Q11. Timeouts and limits

| Setting | Default | Source |
|---|---|---|
| `tg_qr_max_codes` | 5 codes per round (≈ 2.5 min) | FR-005 |
| `tg_login_password_timeout_s` | 600 s | FR-007 |
| `tg_qr_same_device_link` | `false` until verified | FR-010, Q5 |
| `tg_device_model` / `tg_system_version` / `tg_app_version` | "Scribe Agent" / "server" / package version | FR-016 |

`FloodWaitError` on `exportLoginToken` maps to the existing `LoginFlood`, which already
produces "Telegram asks to wait N s" and now also offers the other method.
