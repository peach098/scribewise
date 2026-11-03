# Implementation Plan: Beta & Subscription Access Gate

**Branch**: `005-beta-subscription-access` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/005-beta-subscription-access/spec.md`

## Summary

The hosted Scribe serves only people on a beta list, people with an operator-granted
subscription, and the operator. Everyone else gets a polite, rate-limited "closed beta"
reply. They get no account, no setup and no LLM calls. People who lose access have their
agent stopped. They keep their data and can still view and delete it. If access returns,
the service resumes without repeating setup.

Technical approach:

- **Bot side.** A new outer aiogram middleware, `AccessGateMiddleware`, runs right after
  the existing `UserContextMiddleware` and decides allow, refuse or restricted on every
  update. Access is computed by one pure function from three new tables keyed by Telegram
  id: `beta_entries`, `subscriptions` and `refused_contacts`. A fourth new table,
  `access_events`, is the audit log.
- **Agent side.** The CLI runs as a separate process, so a new scheduler job,
  `access_reconcile`, runs every minute. It compares each user's access with the new
  `users.access_suspended_at` column. On loss it stops the Telethon client, expires
  pending drafts and sends one notice. On regain it starts the client again. It also sends
  the 3-day subscription reminder.
- **Defence in depth.** The scheduler's claim query, `ClientPool` start, profile refresh
  and `send_approved_draft` also refuse suspended users.
- **Operator.** The operator works through new `beta`, `sub` and `access` subcommands of
  the existing `python -m scribe.admin`.
- **Switch.** The gate is off by default (`SCRIBE_ACCESS_GATE`), so self-hosted
  installations and the existing tests are unchanged.

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: aiogram 3 (outer middleware), Telethon (unchanged),
SQLAlchemy 2 async + asyncpg, Alembic, pydantic-settings. **No new dependencies.**

**Storage**: PostgreSQL 16. Migration `0005_access_gate` adds:
- the tables `beta_entries`, `subscriptions`, `refused_contacts` and `access_events`;
- the enum `access_event_kind`;
- the column `users.access_suspended_at`.

See [data-model.md](data-model.md).

**Testing**:
- **Unit**: pytest + pytest-asyncio. Covers the access policy, the `Every` schedule (shared with specs/003) and
  i18n lint.
- **Integration**: the Postgres test container with real migrations. Covers the gate
  middleware (via `tests/integration/bot_support.py`), reconciliation (with
  `FakeMessagingGateway` and `FrozenClock`), the send refusal and the admin CLI in-process.
- See [quickstart.md](quickstart.md).

**Target Platform**: Linux server, Docker Compose, single process (unchanged)

**Project Type**: Single long-running service (asyncio worker + Telegram control bot) +
operator CLI

**Performance Goals**: the gate adds at most one indexed lookup per update, around 1 ms.
The bot reflects a beta change on the next message. The agent stops or starts within about
2 minutes of a change: a 60 s job interval on a 50 s scheduler tick (target ≤ 5 min, FR-010,
SC-003).

**Constraints**:
- No message content from refused senders is stored (FR-007).
- Refusal answers are sent at most once per hour per person (FR-006).
- `/my_data` and `/delete_my_data` always stay reachable (Principle II).
- A suspended user's draft can never be sent, even if a tap and the reconcile job race.

**Scale/Scope**:
- About 30 beta testers (landing goal) and up to about 100 users.
- Refused contacts: up to thousands of rows, purged after 30 days.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan complies |
|---|---|---|
| I. Human approval before sending | ✅ Pass | The gate only removes the ability to send. `send_approved_draft` gains an access refusal. Pending drafts of a suspended user expire and are never sent. Every access change is logged and attributable (`access_events.actor`). |
| II. Privacy & data minimization | ✅ Pass | Refused senders: only their Telegram id, timestamps and a counter are kept, for 30 days, never content or names. Suspended users keep `/my_data` and `/delete_my_data`. Access records are shown in `/my_data`. Operator notes are capped and never shown to the user. `/delete_my_data` deletes everything, including the beta entry, subscriptions and access events, after warning that access will be lost (research Q10). |
| III. Grounded replies | ➖ N/A | No change to drafting. |
| IV. Faithful personal style | ➖ N/A | No change to style. Profiles are kept while suspended. |
| V. Evaluation-driven development | ✅ Pass | No change to draft generation, so no eval run is needed. All new code is deterministic and gets tests first: the policy, the middleware, reconciliation, the CLI and the migration. |
| VI. Simplicity & observability | ✅ Pass | No new dependency, service or IPC. It reuses the DB-polling scheduler, with the `Every` interval schedule from specs/003; the existing `ClientPool` and `FakeMessagingGateway`; and the admin CLI. Structured logs: `access_refused` (telegram id hash, answered yes/no), `access_lost` and `access_restored` (user id, reason), `access_reconcile` (counts). Health shows `access_gate`. |
| Security: untrusted input | ✅ Pass | The gate runs before any handler. Restricted mode is an allow-list, not a deny-list. Usernames are matched only to unlinked entries and then pinned to the immutable id. |
| Security: secrets | ✅ Pass | No new secrets. Operator ids and links are plain settings. |

**Post-design re-check (after Phase 1)**: ✅ still passes. Data deletion removes access
records too (research Q10, decided after `/speckit-analyze` finding C1), so Principle II
holds without exception. No exceptions to record.

## Project Structure

### Documentation (this feature)

```text
specs/005-beta-subscription-access/
├── spec.md
├── plan.md              # This file
├── research.md          # Phase 0: design decisions Q1–Q11
├── data-model.md        # Phase 1: tables, migration 0005, state transitions
├── quickstart.md        # Phase 1: tests and hosted rollout steps
├── contracts/
│   ├── control-chat.md  # gate decisions, restricted allow-list, texts
│   └── operator-cli.md  # beta / sub / access commands
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
alembic/versions/
└── 0005_access_gate.py                 # NEW: 4 tables, enum, users.access_suspended_at

src/scribe/
├── access/                             # NEW package
│   ├── __init__.py
│   ├── policy.py                       # AccessKind, AccessStatus, status() — pure
│   ├── repo.py                         # beta/sub/refused/events queries, find_beta + linking
│   ├── gate.py                         # AccessGateMiddleware: allow / refuse / restricted
│   └── reconcile.py                    # access_reconcile job: lost / regained / reminders
├── admin.py                            # + beta, sub, access subcommands
├── config.py                           # + access_gate, operator_telegram_ids,
│                                       #   subscription_contact,
│                                       #   access_refusal_cooldown_s
├── db/models.py                        # + BetaEntry, Subscription, RefusedContact,
│                                       #   AccessEvent, AccessEventKind, User.access_suspended_at
├── db/repos/users.py                   # list_user_ids_with_session_status skips suspended
├── i18n/en.py, i18n/ru.py              # + access.* keys
├── scheduling/scheduler.py             # previous_run supports Every; claim query skips suspended
├── telegram/client_pool.py             # start() refuses suspended users
├── telegram/control_bot/bot.py         # register AccessGateMiddleware after UserContext
├── sending/send.py                     # SendRefused when the user is suspended
├── jobs.py                             # refresh_profiles skips suspended
├── retention/purge.py                  # purge refused_contacts (30 d), access_events (365 d)
├── retention/user_data.py              # delete all access rows of the person;
│                                       #   export/summary gain an Access section
├── telegram/control_bot/account_command.py # delete confirmation warns about lost access
├── telegram/control_bot/review.py      # DraftPresenter.post_card skips suspended users;
│                                       #   AccessSuspended reply
├── observability/health.py             # + access_gate flag
└── __main__.py                         # register access_reconcile (Every 60 s, run_at_start)

tests/
├── unit/test_access_policy.py
├── unit/test_scheduler_interval.py
├── integration/test_access_gate_bot.py
├── integration/test_access_reconcile.py
├── integration/test_send_access.py
└── integration/test_admin_access.py
```

**Structure Decision**: single project as before. The new logic lives in one package,
`scribe/access/`, behind three seams: the middleware, the reconcile job and the policy
function. Existing modules each get only a one-line suspension check at their existing
choke point (research Q6).

## Delivery Order

The spec says a stranger is already using the hosted bot, so this goes in increments:

1. **MVP (US1 + US2)**: the migration, policy, repo, middleware (allow, refuse and
   restricted), beta CLI commands, settings and texts. This is enough to close the door
   and admit beta testers. An existing user without access is already restricted in the
   bot, but their agent keeps running until step 2.
2. **US3**: `access_suspended_at`, the reconcile job and the choke-point checks.
3. **US4**: the subscription CLI, reminders, `access stats` and the `/my_data` access
   section.

## Addendum 2026-10-22: in-bot beta applications (US5)

The wrong `t.me/scribewise_beta` link and the `access_request_url` setting are replaced by
an in-bot application flow (research Q12–Q14):

- **Applicants**: a new router, `src/scribe/access/apply.py`, with `ApplyStates`.
- **Storage**: a new table `beta_applications` (migration `0006`).
- **Operators**: review in their bot chat (`betaapp:*` buttons) or with `beta requests` /
  `beta approve` / `beta reject`.

Constitution check:

- **II. Privacy & data minimization.** The use case is stored only after explicit consent
  (**Send application**). It is deleted 30 days after the decision, and abandoned unsent text
  in the FSM is purged after 24 h. Applications are part of `/my_data` and
  `/delete_my_data`.
- **Security: untrusted input.** The use case is sent to operators as plain text with no
  parse mode, and only operators can decide.
- **VI. Simplicity & observability.** No new dependencies.

Tasks: T036–T046.

## Complexity Tracking

No constitution violations to justify.
