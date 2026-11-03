# Implementation Plan: Telegram QR-Code Login

**Branch**: `002-telegram-qr-login` | **Date**: 2026-10-20 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-telegram-qr-login/spec.md`

## Summary

Add login by QR code as a second, recommended way to connect a Telegram account to
Scribe, next to the existing phone → code → password flow, for both first setup and
reconnecting. The user approves the login from a Telegram app where they are already
signed in, so they never send their phone number or a login code to the bot.

Technical approach: reuse Telethon's built-in `qr_login()` on the same per-attempt
`TelegramClient(StringSession())` as today, so the stored session and everything
downstream (`client_pool`, `user_client`, encryption) is unchanged. The QR image is
rendered locally (segno) and shown in the control chat as one photo that is swapped in
place every ~30 s, up to 5 codes. A background task per attempt, owned by the existing
in-memory `LoginManager`, runs the wait loop. The phone/code/password steps move from
`setup_flow.py` into a new shared `connect_flow.py` used by setup and by a new
`/telegram` command (status, reconnect, disconnect; takes over 001's T071). A shared
finish step checks the account (same account on reconnect, not linked to another user),
logs out any session it rejects, records the method and time on `telegram_sessions`, and
writes one metadata-only `activity_records` row per attempt.

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: Telethon 1.45 (`qr_login`, already locked), aiogram 3.31
(photo messages, `edit_message_media`, inline URL buttons), **segno** (new: QR → PNG, pure
Python, no transitive deps; research Q2). Everything else from 001 unchanged.

**Storage**: PostgreSQL 16. Migration `0002`: `telegram_sessions.login_method`,
`telegram_sessions.connected_at`, `activity_kind += telegram_login`. No new tables.

**Testing**: pytest + pytest-asyncio. Unit tests with a fake `LoginClient` and fake clock
for the QR loop, refresh limit, drain and password timeout; integration tests (Postgres
test container + aiogram dispatcher, following `tests/integration/test_us1_setup_flow.py`)
for setup, reconnect, wrong account, account taken, not-my-account and restart cleanup.
Manual checks for real scans and the same-device link (quickstart).

**Target Platform**: Linux server, Docker Compose, single process (unchanged)

**Project Type**: Single long-running service (asyncio worker + Telegram control bot)

**Performance Goals**: connection confirmed ≤ 10 s after the scan is approved (FR-006);
QR image refresh ≤ 2 s after a code expires; median QR connect ≤ 2 min (SC-001)

**Constraints**: no phone number, login code, QR token or 2FA password is ever logged or
stored (FR-008, SC-004); at most one active login attempt per user (FR-009); no
half-connected account after a cancel, rejection or restart; Telegram flood-wait respected

**Scale/Scope**: ~100 users; logins are rare (setup + occasional reconnects), so at most a
handful of concurrent attempts, each one in-memory client + one task

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan complies |
|---|---|---|
| I. Human approval before sending | ✅ Pass | Login only; no change to drafting or sending paths. |
| II. Privacy & data minimization | ✅ Pass | QR token rendered locally, never sent to third parties, logged or stored; image removed from chat when replaced. Phone shown masked, never stored. 2FA password messages deleted, never stored (as in 001). Activity rows hold metadata only and are covered by `/my_data` export and deletion. Session string encrypted as before. Rejected logins are logged out so no unused session lingers. |
| III. Grounded replies | ➖ N/A | No draft changes. |
| IV. Faithful personal style | ➖ N/A | No style changes. Reconnect keeps profiles untouched (SC-006). |
| V. Evaluation-driven development | ✅ Pass | No change to draft generation → no eval run required. All new deterministic code (QR loop, finish checks, migration, cleanup) gets automated tests written with or before the code. |
| VI. Simplicity & observability | ✅ Pass | One new dependency (segno), justified in research Q2. No new table, service or scheduler job; reuses `LoginManager`, `activity_records`, `client_pool`. Telegram access stays behind the `LoginClient` Protocol (fake in tests). Structured logs per step (`login_step`, `login_ended` with method/outcome, no secrets). |
| Security: minimum scopes | ✅ Pass (same as 001) | Telegram user sessions have no scopes; the QR method grants exactly the same access as phone login. The existing full-access disclosure is shown before both methods. |
| Security: untrusted input | ✅ Pass | No message content involved. Callback ownership checks from 001 apply to the new `tglogin` / `tgreconnect` / `tgdisconnect` callbacks. |

**Post-design re-check (after Phase 1)**: ✅ still passes. Design added no tables, no
external calls beyond Telegram, and one dependency. No exceptions to record.

## Project Structure

### Documentation (this feature)

```text
specs/002-telegram-qr-login/
├── plan.md              # This file
├── research.md          # Phase 0: Q1–Q11 decisions
├── data-model.md        # Phase 1: schema delta + in-memory attempt states
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   ├── control-chat.md  # Screens, buttons, callbacks (delta to 001)
│   └── login-client.md  # LoginClient / LoginManager / finish step
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/scribe/
├── config.py                          # + tg_qr_* , tg_login_password_timeout_s, tg_device_* settings
├── db/
│   ├── models.py                      # + LoginMethod enum, TelegramSession.login_method/connected_at,
│   │                                  #   ActivityKind.telegram_login
│   └── repos/users.py                 # upsert_telegram_session(method, connected_at), delete_telegram_session,
│                                      #   find_user_by_telegram_user_id
├── telegram/
│   ├── login.py                       # + QrCode, QrOutcome, start_qr/refresh_qr/wait_qr/log_out,
│   │                                  #   LoginAttempt, LoginManager(begin/end/drain/close_all)
│   ├── qr_image.py                    # NEW: render_qr_png(url) -> bytes (segno)
│   ├── gateway.py                     # + MessagingGateway.log_out(user_id)
│   ├── fake_gateway.py                # + log_out (records calls)
│   ├── user_client.py                 # + log_out; pass device_model/system_version/app_version
│   ├── client_pool.py                 # + log_out; revoked/error notices → /telegram + Reconnect button
│   └── control_bot/
│       ├── connect_flow.py            # NEW: method choice, QR loop, phone/code/password (moved from
│       │                              #   setup_flow), finish_login, not-my-account, /telegram,
│       │                              #   boot cleanup of interrupted attempts
│       ├── setup_flow.py              # /start → connect_flow; keeps pick_chats/importing
│       ├── render.py                  # QR caption, status card, account label + phone masking
│       └── bot.py                     # register connect_flow router; startup cleanup hook
alembic/versions/
└── 0002_telegram_login_method.py      # NEW

tests/
├── unit/
│   ├── test_telegram_login.py         # + QR methods on TelethonLoginClient with a fake TelegramClient
│   ├── test_login_manager.py          # NEW: one attempt per chat, drain, password timeout, close_all
│   ├── test_qr_image.py               # NEW
│   └── test_render_connect.py         # NEW: caption, status card, masked phone
└── integration/
    ├── test_connect_flow.py           # NEW: QR setup, 2FA, refresh limit, method switch, not-my-account,
    │                                  #   account taken, restart cleanup, activity rows
    ├── test_reconnect_flow.py         # NEW: /telegram reconnect (QR + phone), wrong account, state kept
    └── test_us1_setup_flow.py         # updated: phone flow now behind the method choice
```

**Structure Decision**: Single project, existing layout. New code lives next to the
current login code (`telegram/login.py`, `telegram/control_bot/`). The connect steps get
their own module so setup and `/telegram` share one implementation instead of
duplicating the phone/code/password handlers.

## Cross-feature impact

- **001 T071** (`/telegram` status / disconnect / reconnect) is implemented by this
  feature; mark it "moved to 002" in `specs/001-telegram-reply-agent/tasks.md` when 002's
  tasks are generated.
- **001 T073** (setup polish) and **T056** (task-source steps) insert their steps after
  `pick_chats`; this plan only changes what comes before it, so there is no conflict.
- **001 contracts/control-chat.md**: the "Setup FSM states" line and the `/telegram` row
  are superseded by `contracts/control-chat.md` here.
- **001 Disconnect semantics change**: 001 moved a disconnected user to `setting_up`;
  here Disconnect only revokes the session and keeps `users.state` (research Q9), so
  reconnect has one path and never re-runs setup.
- **001 T069** (`/delete_my_data`) should use the new `MessagingGateway.log_out` instead
  of calling Telethon directly.
- Existing users connected by phone keep working; migration backfills
  `login_method=phone_code`.

## Risks

| Risk | Mitigation |
|---|---|
| Same-device `tg://login` link blocked or warned about by Telegram apps | Behind a flag, off by default; phone flow is the one-device fallback (research Q5). |
| A cancelled QR code is scanned within its last seconds | Drain + immediate `log_out()` (research Q4); tested with the fake client, checked manually (quickstart #4). |
| Telegram changes the "Link Desktop Device" path or naming | Instruction text lives in one place in `render.py`. |
| Telegram rate-limits `exportLoginToken` | Mapped to `LoginFlood`; user offered the other method. |

## Complexity Tracking

No Constitution Check violations; nothing to justify.
