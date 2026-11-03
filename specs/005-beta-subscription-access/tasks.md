---

description: "Task list for Beta & Subscription Access Gate"
---

# Tasks: Beta & Subscription Access Gate

**Input**: Design documents from `/specs/005-beta-subscription-access/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/control-chat.md,
contracts/operator-cli.md, quickstart.md

**Tests**: Included. Constitution Principle V requires automated tests for deterministic
code, written before or together with it.

- **Integration tests** follow `tests/integration/test_control_bot.py`, which uses
  `RecordingSession`, `offline_bot()` and the `text_update`/`callback_update` builders
  from `tests/integration/bot_support.py`. Reconciliation tests follow
  `tests/integration/test_scheduler.py` and `tests/integration/test_client_pool.py`
  (`FakeMessagingGateway`, `FrozenClock` at `FROZEN_NOW` 2026-10-14 10:00 UTC).
- **Fixtures**: the DB fixtures (`session_factory`, `db_session`, `make_user`,
  `committed_session_factory`) exist only in `tests/integration`.
- **Synthetic values only**: Telegram ids such as `900000001`, usernames such as
  `@beta_tester_1`.

**Organization**: Tasks are grouped by user story so each story can be implemented and
tested on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story the task belongs to (US1–US4 from spec.md)

## Path Conventions

Single project: `src/scribe/`, `tests/` and `alembic/versions/` sit at the repository
root.

## Ground Rules

These apply to every task:

- **Gate off by default.** With `SCRIBE_ACCESS_GATE` unset, behaviour must not change.
  The full existing suite must stay green after every phase.
- **Access is keyed by Telegram id.** It equals `users.telegram_control_chat_id`
  (research Q1). Never use `users.telegram_user_id` for access.
- **No content in access records.** Never store message text, names or usernames of
  refused senders (FR-007).
- **Bilingual texts.** Every new user-facing text is an i18n key present in both
  `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`, with the same placeholders.
  `tests/unit/test_i18n_lint.py` enforces this.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: settings and package skeleton

- [X] T001 Add an `# --- access gate (specs/005-beta-subscription-access) ---` section to `Settings` in `src/scribe/config.py` with these fields:
  - `access_gate: bool = False`
  - `operator_telegram_ids: list[int] = []` (parsed from a comma-separated env value, the same way `working_days` is parsed)
  - `access_request_url: str = "https://t.me/scribewise_beta"`
  - `subscription_contact: str = ""`
  - `access_refusal_cooldown_s: int = Field(default=3600, gt=0)`

  Extend `tests/unit/test_config.py` with the defaults and env overrides (`SCRIBE_ACCESS_GATE=true`, `SCRIBE_OPERATOR_TELEGRAM_IDS=900000001,900000002`).
- [X] T002 [P] Create the package `src/scribe/access/__init__.py` (docstring only) and empty modules `policy.py`, `repo.py`, `gate.py` and `reconcile.py`, each with a one-line module docstring pointing to `specs/005-beta-subscription-access/research.md`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the schema, the pure access policy and the repository that every story uses.

**⚠️ CRITICAL**: no user story work can begin until this phase is complete.

- [X] T003 Add the ORM models to `src/scribe/db/models.py` exactly as in `data-model.md`.
  - **`BetaEntry`** (`beta_entries`):
    - `telegram_id` BigInteger, nullable, **unique**;
    - `username` Text, nullable, "stored lowercase without `@`";
    - `note` Text, nullable, "≤ 200 chars";
    - `added_by` Text, not null;
    - `added_at` timestamptz, not null;
    - `linked_at` timestamptz, nullable;
    - check `telegram_id IS NOT NULL OR username IS NOT NULL`;
    - partial unique index on `username` where `telegram_id IS NULL`.
  - **`Subscription`** (`subscriptions`):
    - `telegram_id` BigInteger, not null, indexed;
    - `starts_at` and `ends_at` timestamptz, not null, check `ends_at > starts_at`;
    - `cancelled_at` nullable;
    - `granted_by` Text, not null;
    - `note` Text, nullable, "≤ 200 chars";
    - `reminded_at` nullable;
    - `_Timestamps`.
  - **`RefusedContact`** (`refused_contacts`):
    - `telegram_id` BigInteger PK;
    - `first_seen_at` and `last_seen_at`, not null;
    - `last_answered_at`, nullable;
    - `attempts` Integer, not null, default 0.
  - **`AccessEventKind`** StrEnum, values `beta_added, beta_removed, beta_linked, subscription_granted, subscription_extended, subscription_ended, subscription_reminded, access_lost, access_restored`, stored via `_pg_enum(..., "access_event_kind")`.
  - **`AccessEvent`** (`access_events`):
    - `telegram_id` BigInteger, not null, indexed, **no FK**;
    - `kind`;
    - `actor` Text, not null (`operator` or `system`);
    - `reason` Text, nullable, "≤ 200 chars";
    - `meta_json` JSONB, not null, default `{}`;
    - `at` timestamptz, not null, indexed.
  - **`User.access_suspended_at`**: timestamptz, nullable.
- [X] T004 Create the migration `alembic/versions/0005_access_gate.py` (`revision = "0005"`, `down_revision = "0004"`).
  - Create the four tables, the enum `access_event_kind` and the column `users.access_suspended_at`.
  - Add a partial index on `users (access_suspended_at)` `WHERE access_suspended_at IS NULL`.
  - Name constraints with `op.f(...)` as in `0003_user_language.py`.
  - Write a full `downgrade()`.
  - Check that `alembic upgrade head` followed by `downgrade -1` and `upgrade head` works against the test DB. It depends on T003.
- [X] T005 [P] Write the unit tests `tests/unit/test_access_policy.py` for `status()`.
  - Cases: no rows gives `none`; a beta entry gives `beta`; an active subscription gives `subscribed` with `until=ends_at`; both give `beta_and_subscribed`; the operator id gives `operator`, even with no rows.
  - Subscription edges: `starts_at == now` is active; `ends_at == now` is not; `cancelled_at` set is not; a future `starts_at` is not active; `until` is the max `ends_at` of chained active rows.
- [X] T006 Implement `src/scribe/access/policy.py`:
  - `AccessKind` StrEnum (`operator, beta, subscribed, beta_and_subscribed, none`);
  - frozen dataclass `AccessStatus(kind, until: datetime | None)` with property `has_access`;
  - `subscription_active(sub, now) -> bool`;
  - `status(beta: BetaEntry | None, subs: Sequence[Subscription], *, is_operator: bool, now: datetime) -> AccessStatus`;
  - `is_operator(telegram_id, settings) -> bool`.

  It must be pure: no DB, no clock reads. T005 must pass. Depends on T003.
- [X] T007 Implement `src/scribe/access/repo.py`. All functions take an `AsyncSession` and never commit.
  - `find_beta(session, telegram_id, username, *, now) -> BetaEntry | None`:
    1. Look up by `telegram_id`.
    2. Otherwise, look up an unlinked row with `lower(username)` equal to the sender's username stripped of `@` and lowercased.
    3. On that match, set `telegram_id`, set `linked_at = now` and record a `beta_linked` event with `actor="system"` (FR-009, FR-019).
  - `subscriptions_for(session, telegram_id)`.
  - `access_status(session, settings, telegram_id, username, *, now) -> AccessStatus`, which calls `find_beta`, `subscriptions_for` and `policy.status`.
  - `record_event(session, telegram_id, kind, *, actor, reason=None, meta=None, now)`: truncate `reason` to 200 chars; `meta` holds ids and ISO dates only.
  - `touch_refused(session, telegram_id, *, now, cooldown_s) -> bool`:
    - upsert with `INSERT ... ON CONFLICT (telegram_id) DO UPDATE` setting `attempts = attempts + 1, last_seen_at = now`;
    - return True and set `last_answered_at = now` only when `last_answered_at` is null or older than `cooldown_s`.

  Depends on T003 and T006.
- [X] T008 [P] Add the integration tests `tests/integration/test_access_repo.py` for `find_beta` and `touch_refused`.
  - Username linking is case-insensitive and happens once: the event is written once, and a later username change still matches by id.
  - With the cooldown, the first call is True; a second call within 3600 s is False with `attempts == 2`; a call after 3601 s is True.
  - Depends on T004 and T007.

**Checkpoint**: the schema migrates, the policy is unit-tested, and the repo is
integration-tested. Story work can start.

---

## Phase 3: User Story 1 — Strangers are turned away (Priority: P1) 🎯 MVP

**Goal**: with the gate on, a sender with no access and no account gets one polite
"closed beta" reply per cooldown. Nothing else happens.

**Independent Test**: with `access_gate=True` and no beta rows, send `/start`, `/help`,
`/settings`, free text and a callback from id `900000001`. Expected:
- exactly one `sendMessage` containing `access_request_url`;
- no `users` row;
- the callback is answered with an empty `answerCallbackQuery`;
- `refused_contacts.attempts == 5`.

### Tests for User Story 1

- [X] T009 [P] [US1] Write `tests/integration/test_access_gate_bot.py::test_refuse_*` per `contracts/control-chat.md` → "Refuse".
  - With the gate on, no user and no beta entry:
    - `/start` sends the `access.refused` text, which contains the request URL, and creates no `users` row and no FSM state;
    - a second message within the cooldown sends nothing;
    - after `FrozenClock` advances 3601 s, a message gets the text again;
    - a callback gets an empty `answerCallbackQuery`;
    - a group-chat message sends nothing and creates no `refused_contacts` row.
  - The subscription line appears only when `subscription_contact` is set.
  - The reply is in Russian for `language_code="ru"`.
  - With the gate off, `/start` creates a user as today.

### Implementation for User Story 1

- [X] T010 [P] [US1] Add the i18n keys to `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`, with English texts from `contracts/control-chat.md`:
  - `access.refused` with `{request_url}`;
  - `access.subscription_line` with `{subscription_contact}`.

  Russian: "Scribe сейчас в закрытой бете. Чтобы попасть в неё, оставьте заявку: {request_url}." and "Или оформите подписку: {subscription_contact}."
- [X] T011 [US1] Implement `AccessGateMiddleware` in `src/scribe/access/gate.py`.
  - It is an aiogram `BaseMiddleware` built with `(session_factory, settings, clock)`.
  - **Pass through** when `settings.access_gate` is False or `data` has no `user`-context, i.e. `UserContextMiddleware` dropped a non-private chat.
  - Take the sender from `event.from_user`.
  - **Allow** if the sender is an operator or `repo.access_status(...).has_access`. Commit any username-linking change and call the handler.
  - **Refuse** if there is no access and `data["user"] is None`:
    1. Call `touch_refused` and commit.
    2. If it returned True, send `access.refused`, plus `access.subscription_line` when `settings.subscription_contact` is set, in `data["lang"]`. `Message` events use `message.answer`. `CallbackQuery` events use `callback.answer()` with no text, then `bot.send_message` to the chat if answering.
    3. For `InlineQuery`, call `answer([], cache_time=0)`.
    4. Return None without calling the handler.
  - Log `access_refused` with only `answered` (bool) and `has_account` (bool). Do not log the Telegram id or username: `src/scribe/logging.py` has `redact` but no id-hashing helper, and the demand numbers come from `refused_contacts`.
  - The restricted branch (user row exists) is added in T015b. Until then, a sender with a user row and no access is refused the same way, and the handler is not called.

  Depends on T007 and T010.
- [X] T012 [US1] Register the gate in `src/scribe/telegram/control_bot/bot.py` `create_dispatcher()`. Pass `settings` and the clock to it; take the clock from `workflow_data` if present, otherwise `SystemClock`, matching how handlers get `clock`.
  - Add `dp.message.outer_middleware(AccessGateMiddleware(...))` and the same for `callback_query` **right after** the `UserContextMiddleware` registrations (bot.py:103-104).
  - Add it for `inline_query` too. `inline_query` has no `UserContextMiddleware`, so the gate must work there without `data["user"]`: look the user up itself via `users_repo.get_by_control_chat(session, from_user.id)`.
  - T009 must pass. Depends on T011.

**Checkpoint**: with the gate on, strangers cannot create accounts. This is a deployable
MVP once US2 lets testers in.

---

## Phase 4: User Story 2 — Beta testers get in (Priority: P1)

**Goal**: the operator adds people to the beta list from the CLI. Listed people get the
normal service from their next message.

**Independent Test**:
1. `python -m scribe.admin beta add @beta_tester_1`.
2. Id `900000001` with username `beta_tester_1` sends `/start`: normal setup starts.
3. `beta list` shows the entry as linked.
4. Another id still gets the refusal.

**Pulled forward from US3 into the MVP**: the bot's restricted mode (spec US3 scenario 2,
FR-011), where an existing user without access keeps only `/my_data` and
`/delete_my_data`. Without it, turning the gate on would leave pre-gate accounts either
fully usable or unable to delete their data. These items are marked `[US3]` (T015a,
T015b) but sit in this phase. The agent-side part of US3 stays in Phase 5.

### Tests for User Story 2

- [X] T013 [P] [US2] Extend `tests/integration/test_access_gate_bot.py`:
  - after a refusal, adding a beta entry by id makes the next `/start` start setup, with a user row created (US2 scenario 2);
  - an entry by `@Beta_Tester_1` matches the sender username `beta_tester_1` and links;
  - an operator id in `operator_telegram_ids` passes with no entry (FR-022);
  - `/start` from a beta sender with `language_code="ru"` creates the user with `language="ru"` and gets the Russian welcome (`start_command` already uses `lang`; this guards against the gate changing it).
- [X] T014 [P] [US2] Write `tests/integration/test_admin_access.py::test_beta_*` per `contracts/operator-cli.md` → "Beta list":
  - `beta add 900000001 --note x` and `beta add @Some_User` print `added …`, exit 0 and write `beta_added` events with `actor="operator"` and `reason="x"`;
  - a duplicate add exits 1;
  - `beta remove @some_user` deletes the row and writes `beta_removed`; removing someone who is not listed exits 1;
  - `beta list` prints one line per entry and the footer `N entries, M active users (goal 30)`.

  Call `scribe.admin.run(argv, session_factory)` in-process and capture stdout and stderr with `capsys`.

### Implementation for User Story 2

- [X] T015a [P] [US3] Write `tests/integration/test_access_gate_bot.py::test_restricted_*` per `contracts/control-chat.md` → "Restricted". An existing user row with no access:
  - `/my_data` reaches its handler, and so do `mydata:*` callbacks;
  - `/delete_my_data` → `DELETE` completes the deletion;
  - `/settings` gets `access.ended_reply` once per cooldown;
  - a `review:*` callback is answered silently and the review handler is not called.
- [X] T015b [US3] Add the restricted branch to `AccessGateMiddleware` in `src/scribe/access/gate.py`, used when there is no access and `data["user"]` is not None.
  - **Let through**:
    - `Message` with command `/my_data`, `/delete_my_data` or `/cancel`;
    - any `Message` while the FSM state (from `data["state"]`, the `FSMContext`) is `DeleteStates.confirm`, imported from `src/scribe/telegram/control_bot/account_command.py`;
    - `CallbackQuery` whose `data` starts with `mydata:`.
  - **Anything else**:
    - call `touch_refused` with the same cooldown;
    - if it returned True, reply `access.ended_reply` (with the optional subscription line);
    - answer callbacks silently;
    - do not call the handler.

  Make the allow-list a module-level constant so it is easy to review. Add the i18n key `access.ended_reply` (en and ru) with the text from `contracts/control-chat.md`, placeholder `{request_url}`. T015a must pass.
- [X] T016 [US2] Add the `beta` subcommands to `src/scribe/admin.py` per `contracts/operator-cli.md`.
  - Parse `<who>`: digits give a Telegram id; a leading `@` or other text gives a username, lowercased with `@` stripped.
  - `beta add <who> [--note TEXT]` (note "≤ 200 chars", rejected by argparse type if longer).
  - `beta remove <who>`.
  - `beta list`: compute "has account" from `users.telegram_control_chat_id`, and "active users" as entries linked to a `users` row with `state != setting_up`.
  - Each change writes an `access_events` row via `repo.record_event(actor="operator")` and prints `the running service applies this within about 2 minutes`.
  - Return exit codes 0, 1 and 2 as specified.
  - Update the module docstring to list the new commands.

  T014 must pass.
- [X] T017 [US3] Make data deletion complete (research Q10, option a; constitution Principle II; FR-011).
  - In `src/scribe/retention/user_data.py` `delete_user_data`, in the same transaction as the user row, delete every `beta_entries`, `subscriptions`, `access_events` and `refused_contacts` row whose `telegram_id` equals the user's `telegram_control_chat_id`. Log `access_records_deleted` with counts only and no id.
  - In `src/scribe/telegram/control_bot/account_command.py`, the `/delete_my_data` confirmation adds `access.delete_warning` (en and ru, text in `contracts/control-chat.md`) when the person has a beta entry or an active or scheduled subscription.
  - Extend `tests/integration/test_us4_account.py`:
    - after deletion, no access rows remain for that id;
    - the warning is shown only when there is something to lose;
    - the next `/start` from that id with the gate on is refused.

  It sits in the MVP because restricted users can already delete their data there. Depends on T003 and T010.

**Checkpoint**: MVP complete.
- The hosted instance can turn the gate on.
- The existing users are added with `beta add`.
- Strangers are refused.
- Users who are not listed are restricted in the bot. Their agent keeps running until US3.

---

## Phase 5: User Story 3 — Losing access pauses the service (Priority: P2)

**Goal**: when access ends, the agent stops within about 2 minutes:
- no reading, no drafts, no sends;
- pending drafts expire;
- one notice is sent;
- data is kept.

When access returns, the service resumes without repeating setup.

**Independent Test**: an active user with a running fake client and one pending draft.
`beta remove`, then run `access_reconcile` once. Expected:
- `access_suspended_at` is set;
- `FakeMessagingGateway` recorded `stop(user_id)`;
- the draft is `expired` and its turn is `handle_manually`;
- one `access.ended_notice` was sent;
- an `access_lost` event was written.

Then `beta add` and run again. Expected:
- the column is cleared;
- `start(user_id)` was called;
- `access.restored_notice` was sent;
- an `access_restored` event was written.

A third run changes nothing.

### Tests for User Story 3

- [X] T018 [P] [US3] Add a unit test for the interval schedule. Done as `test_previous_run_subtracts_the_interval` in `tests/unit/test_every_schedule.py`: after merging main, the job uses the `Every(timedelta)` schedule from specs/003 instead of a separate `Interval`.
- [X] T019 [P] [US3] Write `tests/integration/test_access_reconcile.py` per the Independent Test above. Also cover:
  - the gate off: the job is a no-op;
  - an operator user is never suspended;
  - a user with no access and no active Telegram session gets suspended and notified, and `stop` is a harmless no-op;
  - on regain with a revoked `telegram_sessions` row, the notice includes `access.reconnect_hint`;
  - a user who had `/pause`d themselves (`state=paused`) is still `paused` after regain;
  - the notice text in `users.language`.
- [X] T020 [P] [US3] Write `tests/integration/test_send_access.py`: with `users.access_suspended_at` set, `send_approved_draft` raises `AccessSuspended` (a `SendRefused` subclass), the gateway's `send_text` is not called, and the draft is not marked sent (FR-004, research Q6). Also cover `DraftPresenter.post_card` for a suspended user: no `sendMessage` is recorded, and the draft ends `expired` (spec edge case "Access ends mid-draft").
- [X] T021 [P] [US3] Extend `tests/integration/test_scheduler.py` and `tests/integration/test_client_pool.py`:
  - `_claim_due_turns` does not claim turns of a suspended `active` user;
  - `ClientPool.start_all` and `ClientPool.start(user_id)` do not start a suspended user that has an active session.

### Implementation for User Story 3

- [X] T022 [US3] Interval schedule in `src/scribe/scheduling/scheduler.py`. Merged into the `Every(interval: timedelta)` schedule from specs/003; this feature adds `previous_run()` support for `Every`, which the startup requeue in `ensure_jobs` needs.
  - T018 must pass.
- [X] T023 [US3] Add suspension checks at the choke points. Each is a single added condition (research Q6):
  - (a) `src/scribe/scheduling/scheduler.py` `_claim_due_turns`: the `drafting_users` subquery adds `User.access_suspended_at.is_(None)`.
  - (b) `src/scribe/db/repos/users.py` `list_user_ids_with_session_status`: join `users` and filter `access_suspended_at IS NULL`. If the function has other callers, add a keyword `exclude_suspended: bool = False` and pass True from `ClientPool.start_all`.
  - (c) `src/scribe/telegram/client_pool.py` `start(user_id)`: return without starting if the user row has `access_suspended_at` set, logging `client_start_skipped_suspended`.
  - (d) `src/scribe/sending/send.py`:
    - add `class AccessSuspended(SendRefused)` with the docstring "The user's access has ended (specs/005)". `SendRefused` has no reason field; subclasses such as `DraftOutdated` are the existing pattern;
    - in `send_approved_draft`, right after `_lock_draft`, load the user and raise `AccessSuspended` if `access_suspended_at` is set;
    - in `src/scribe/telegram/control_bot/review.py`, catch `AccessSuspended` before the generic `SendRefused` handler and reply `access.ended_reply`.
  - (e) `src/scribe/jobs.py` refresh query: add `User.access_suspended_at.is_(None)`.
  - (f) `src/scribe/telegram/control_bot/review.py` `DraftPresenter.post_card` (around line 399): after loading `user`, if `user.access_suspended_at` is set, mark the draft `expired` (`finalized_at = now`, turn `handle_manually`) and return without sending. This covers the spec edge case "a draft being prepared when access ends is discarded and never shown", including the compose that finishes between two reconcile runs (`scheduling/scheduler.py:687`).

  T020 and T021 must pass.
- [X] T024 [US3] Add the i18n keys (en and ru) per `contracts/control-chat.md`:
  - `access.ended_notice` with `{reason_text}` and `{request_url}`;
  - `access.reason_beta_removed`;
  - `access.reason_subscription_ended`;
  - `access.restored_notice`;
  - `access.reconnect_hint`.
- [X] T025 [US3] Implement `src/scribe/access/reconcile.py`.
  - `register_access_reconcile(scheduler, session_factory, pool, bot, settings, clock)` registers the job `access_reconcile` with `Every(timedelta(seconds=60))` and `run_at_start=True`.
  - The job function, in one transaction:
    1. If `not settings.access_gate`, return.
    2. Load every `users` row with its control chat id, the beta entry (by `telegram_id = telegram_control_chat_id`), the subscriptions and the operator flag.
    3. Compute `policy.status`.
  - **Lost** (no access, column null):
    - set `access_suspended_at = now`;
    - set all `pending` drafts of the user to `DraftState.expired` with `finalized_at = now`, and their turns to `handle_manually`, as `scheduling/deadlines.py` `expire` does;
    - `record_event(access_lost, actor="system", meta={"reason": "beta_removed" | "subscription_ended"})`. Pick the reason by what ended most recently: compare the latest `beta_removed` event `at` with the latest subscription end, which is `cancelled_at` or else `ends_at` if that is in the past. The later of the two wins. With neither present (a pre-gate user who was never listed), use `beta_removed`.
  - **Regained** (access, column set):
    - clear the column;
    - `record_event(access_restored)`.
  - After commit:
    - for lost users, call `pool.stop(user_id)` and send `access.ended_notice`;
    - for regained users, call `pool.start(user_id)` and send `access.restored_notice`, plus `access.reconnect_hint` when the user's `telegram_sessions.status != active`.
  - Send through `bot.send_message(control_chat_id, Msg(...).render(user.language))`. Catch and log Telegram errors per user without aborting the loop.
  - Log `access_reconcile` with `lost`, `regained` and `checked` counts.
  - It must be idempotent: a second run with no changes does nothing. T019 must pass.
- [X] T026 [US3] Wire it in `src/scribe/__main__.py`: call `register_access_reconcile(...)` next to `register_purge_job` (around line 182) and before `ensure_jobs()`. If `settings.access_gate` is true and `settings.operator_telegram_ids` is empty, log the warning `access_gate_no_operator` at startup.
- [X] T030a [US3] Gate turned off undoes its suspensions (not in the original plan). With `settings.access_gate` false, `reconcile_access` used to return at once, so a user suspended while the gate was on stayed suspended for good after the operator turned it off. Now, with the gate off, the job only clears `access_suspended_at` for every suspended user, through the regained path: `record_event(access_restored, actor="system", meta={"reason": "gate_off"})`, then after commit `pool.start(user_id)` and `access.restored_notice` (plus `access.reconnect_hint` when the session is not active). No reminders and no new suspensions. Test: `tests/integration/test_access_reconcile.py::test_gate_off_restores_users_suspended_while_it_was_on`.

**Checkpoint**: removing or expiring access stops the agent within about 2 minutes. Restoring it
resumes the agent. `/my_data` and `/delete_my_data` keep working.

---

## Phase 6: User Story 4 — Subscribers get in (Priority: P2)

**Goal**: the operator grants, extends and ends subscriptions. Active subscribers get the
service, and one reminder is sent 3 days before the end.

**Independent Test**:
1. `sub grant 900000001 --days 2`: the sender is allowed by the gate.
2. Run `access_reconcile`: one `access.sub_reminder` is sent and `reminded_at` is set.
3. A second run sends no reminder.
4. Advance the clock past `ends_at` and run again: the user is suspended with reason
   `subscription_ended`.

### Tests for User Story 4

- [X] T027 [P] [US4] Write `tests/integration/test_admin_access.py::test_sub_*` and `::test_access_*` per `contracts/operator-cli.md`:
  - `sub grant --days 30` and `--until 2026-12-31` (end of day UTC);
  - granting while one is active chains the new one from the current `ends_at`;
  - `sub grant @unlinked` exits 1 with "ask them to message the bot first";
  - `sub extend` moves `ends_at` and clears `reminded_at`; with no subscription it exits 1;
  - `sub end` sets `cancelled_at`;
  - `sub list` and `sub list --all`;
  - `access show` prints `kind=… until=…`, the account and suspended lines and the last 5 events;
  - `access stats --days 7` counts:
    - beta entries;
    - active beta users;
    - active subscriptions;
    - refused people whose `last_seen_at` is in the window;
    - the sum of refusal answers in the window, using rows with `last_answered_at` in the window;
    - suspended users.
  - Each change writes the matching event.
- [X] T028 [P] [US4] Extend `tests/integration/test_access_reconcile.py` with the subscription cases:
  - the reminder is sent once when `ends_at - now <= 3 days` and no other grant extends access beyond `ends_at`. Beta access counts as extending, so beta testers get no reminder;
  - after the reminder, `subscription_reminded` is recorded;
  - expiry by clock suspends the user;
  - `sub extend` after expiry restores access on the next run.

### Implementation for User Story 4

- [X] T029 [US4] Add the `sub` and `access` subcommands to `src/scribe/admin.py` per `contracts/operator-cli.md`.
  - `--days N` (int > 0) and `--until YYYY-MM-DD` are mutually exclusive and required for grant and extend.
  - `--note` is "≤ 200 chars".
  - `granted_by="operator"`.
  - The events are `subscription_granted`, `subscription_extended` and `subscription_ended` with `meta={"subscription_id", "ends_at"}`.
  - `access show` uses `repo.access_status`.
  - T027 must pass.
- [X] T030 [US4] Add the 3-day reminder to `src/scribe/access/reconcile.py`.
  - In the same run, for each person with an active subscription where `reminded_at IS NULL` and `ends_at - now <= timedelta(days=3)`, and no beta entry or later chained subscription:
    - set `reminded_at = now`;
    - `record_event(subscription_reminded)`;
    - after commit, send `access.sub_reminder` with `{date}` (the user's timezone, date only) and `{renew}` (`subscription_contact` or `access_request_url`) to users that have a `users` row.
  - Add the i18n key `access.sub_reminder` (en and ru).
  - T028 must pass.

**Checkpoint**: all four stories work independently.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T031 [P] Retention, in `src/scribe/retention/purge.py`: delete `refused_contacts` where `last_seen_at < now - 30 days`, and `access_events` where `at < now - 365 days`, in the daily purge (FR-007, research Q10). Add tests to `tests/integration/test_retention.py`.
- [X] T032 [P] Update `/my_data`, in `src/scribe/retention/user_data.py`:
  - `data_summary` gains an **Access** section with the status kind, beta since, subscriptions (start, end, state) and the access event count, with new i18n keys `access.mydata_*` in en and ru;
  - `export_user_data` gains `access.beta`, `access.subscriptions` and `access.events` (ids and dates only; no operator `reason`);
  - Deletion itself is done in T017.

  Extend `tests/integration/test_us4_account.py`.
- [X] T033 [P] Health, in `src/scribe/observability/health.py` and `src/scribe/__main__.py` `health_status`: add `"access_gate": settings.access_gate` to the JSON, and extend `tests/unit/test_health.py`.
- [X] T034 [P] Docs:
  - In `README.md`, add an "Access gate (hosted)" section: the settings `SCRIBE_ACCESS_GATE`, `SCRIBE_OPERATOR_TELEGRAM_IDS`, `SCRIBE_ACCESS_REQUEST_URL`, `SCRIBE_SUBSCRIPTION_CONTACT` and `SCRIBE_ACCESS_REFUSAL_COOLDOWN_S`; the admin commands; and the rollout order from `quickstart.md` (add existing users **before** turning the gate on).
  - Add the new variables to `.env.example` if it exists.
- [X] T035 Run the full suite with `uv run pytest`, plus `ruff` and `mypy` as configured in `pyproject.toml`. Fix any regressions, then walk through the automated part of `quickstart.md`.

---

## Phase 8: User Story 5 — Applying for the beta inside the bot (Priority: P1, added 2026-10-22)

**Goal**: people without access apply for the beta from the "no access" message. Operators
approve or reject in the bot or the CLI. **Subscription** shows a placeholder. The wrong
`t.me/scribewise_beta` link is gone.

**Independent Test**: see spec US5. Stranger → **Join the beta** → use case → **Send
application** → the operator gets a message → **Approve** → the stranger gets "you're in" →
`/start` begins setup.

### Tests for User Story 5

- [X] T036 [P] [US5] Write `tests/integration/test_beta_apply_bot.py` per `contracts/control-chat.md` → "Beta application flow". Use the `gated` settings with `operator_telegram_ids=[OPERATOR]` and `RecordingSession`.
  - The refusal has the two buttons (`apply:start`, `apply:subinfo`) and no URL.
  - `apply:subinfo` sends the placeholder and creates no rows.
  - `apply:start` → text "short" gets `access.apply_too_short`; a sticker or photo gets `access.apply_text_only`; 1001 chars gets too long; a valid text shows the review with the consent note and three buttons. No `beta_applications` row exists yet.
  - `apply:edit` returns to the ask step; `apply:cancel` and `/cancel` clear the FSM and store nothing.
  - `apply:send`:
    - creates a pending row with `consent_at`, the lowercase username and the language;
    - records an `application_submitted` event;
    - confirms to the applicant;
    - sends one message to `OPERATOR` with the use case as plain text (no parse mode) and the two `betaapp:*` buttons;
    - stores `operator_messages`.
  - While pending, any message gets `access.apply_pending` with no buttons, and `apply:start` is refused.
  - An operator's `betaapp:approve:<id>`:
    - sets `approved`, `decided_by` and `decided_at`;
    - creates a `beta_entries` row and a `beta_added` event (actor `operator`);
    - sends `access.apply_approved` to the applicant;
    - edits the operator message to show the decision with no buttons.

    The applicant's next `/start` begins setup.
  - `betaapp:reject:<id>` sends `access.apply_rejected` with a date. For 30 days the applicant gets `access.apply_rejected_recently`, and `apply:start` is refused. After 30 days and 1 s (`FrozenClock`), they can apply again.
  - A non-operator pressing `betaapp:*` gets a silent answer and nothing changes.
  - A second decision on a decided application changes nothing and only refreshes the message.
  - A restricted user (a `users` row with no access) can also apply. After approval, the `access_reconcile` run restores them.
  - Everything works in Russian too (`language_code="ru"`).
- [X] T037 [P] [US5] Extend `tests/integration/test_admin_access.py` with:
  - `beta requests`, `beta requests --all`;
  - `beta approve <id>` and `beta reject <id>`: exit codes, events and the applicant notification via a fake bot passed into `run()`;
  - the new `access stats` lines.
- [X] T038 [P] [US5] Extend `tests/integration/test_retention.py`:
  - decided applications older than 30 days are deleted, pending ones are kept;
  - `bot_fsm_state` rows in `ApplyStates:*` older than 24 h are deleted.

  Extend `tests/integration/test_us4_account.py`: `/delete_my_data` deletes the person's applications; `/my_data` lists them; the export includes the use case.

### Implementation for User Story 5

- [X] T039 [US5] Write the migration `alembic/versions/0006_beta_applications.py` (down_revision `"0005"`) and the model `BetaApplication` in `src/scribe/db/models.py`, exactly as in `data-model.md` → "beta_applications".
  - Enum `beta_application_status`.
  - Partial unique index on `telegram_id` WHERE `status = 'pending'`.
  - Check `char_length(use_case) <= 1000`.
  - `operator_messages` JSONB, default `'[]'`.
  - `AccessEventKind` += `application_submitted`, `application_approved`, `application_rejected`, added with `ALTER TYPE access_event_kind ADD VALUE IF NOT EXISTS`. Postgres cannot drop enum values, so `downgrade()` leaves them and only drops the table and the status enum.
  - The `alembic check` test must pass.
- [X] T040 [US5] Add the repository functions in `src/scribe/access/applications.py`:
  - `can_apply(session, telegram_id, now) -> ApplyBlock | None` (`pending` / `rejected_until(date)`);
  - `submit(session, *, telegram_id, username, use_case, language, now) -> BetaApplication` (records `application_submitted`);
  - `decide(session, application_id, *, approve: bool, operator_id: int | None, now) -> Decision`, which locks the row with `FOR UPDATE`, is idempotent when already decided, and on approve ensures the beta entry and records `beta_added`;
  - `list_applications(session, *, include_decided, now)`.

  Also extend `access stats` counts and add the application rows to `delete_user_data`, `data_summary` and the export in `src/scribe/retention/user_data.py`.
- [X] T041 [US5] Remove the `access_request_url` setting from `src/scribe/config.py`, `tests/unit/test_config.py`, `.env.example` and `README.md`. Rewrite the i18n texts per `contracts/control-chat.md`:
  - `access.refused`, `access.ended_reply`, `access.ended_notice` and `access.sub_reminder` lose `{request_url}`;
  - add `access.apply_*`, `access.subscription_placeholder` and the operator texts, in en and ru.

  Update `src/scribe/access/gate.py`, `src/scribe/access/reconcile.py` and `src/scribe/telegram/control_bot/review.py` (`AccessSuspended` reply) accordingly. `tests/unit/test_i18n_lint.py` must pass.
- [X] T042 [US5] Update the gate in `src/scribe/access/gate.py`:
  - the refuse and restricted replies attach the two buttons, unless `can_apply` says pending or recently rejected; in that case send `access.apply_pending` or `access.apply_rejected_recently` instead;
  - let through, for senders without access, callbacks starting with `apply:` and any message while the FSM state is in `ApplyStates`. These do not count against the cooldown;
  - keep `_drop_unfinished_flow` from dropping `ApplyStates`.
- [X] T043 [US5] Implement the router `src/scribe/access/apply.py`:
  - `ApplyStates`;
  - the handlers per the contract, working without a `users` row: use `from_user` and `data["lang"]`;
  - the operator notifications as plain text, storing `operator_messages`;
  - the `betaapp:*` decision handlers behind an operator filter;
  - a shared `notify_decision(bot, application, decision, now)` that edits the operator copies and notifies the applicant. The CLI uses it too.

  Register the router in `src/scribe/telegram/control_bot/bot.py` **before** the setup and settings routers, so `ApplyStates` text is not taken by other handlers. T036 must pass.
- [X] T044 [US5] CLI in `src/scribe/admin.py`:
  - `beta requests [--all]`, `beta approve <id>`, `beta reject <id>`;
  - `run()` accepts an optional `bot`, and `_main` builds `Bot(settings.bot_token)` for the commands that notify, closing its session afterwards;
  - extend `access stats`.

  T037 must pass.
- [X] T045 [US5] In `src/scribe/retention/purge.py`, delete decided applications with `decided_at < now - 30 days`, and `bot_fsm_state` rows whose `state LIKE 'ApplyStates:%'` and `updated_at < now - 24 h`. T038 must pass.
- [X] T046 Run the full suite, `ruff` and `mypy`. Update `quickstart.md` manual steps 5–6 to the in-bot application flow and add `beta requests` to the README.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup** (T001–T002) has no dependencies.
- **Foundational** (T003–T008) depends on Setup, and it blocks all stories.
  - T003 must finish before T004 and T006.
  - T006 must finish before T007.
  - T007 must finish before T008.
- **US1** (T009–T012) depends on Foundational.
- **US2 phase** (T013–T017, including the US3 bot-side items T015a, T015b and T017) depends
  on US1, because it extends the gate middleware (T011 must finish before T015b).
- **US3** (T018–T026) depends on Foundational. It is independent of US1 and US2 on the
  agent side (reconcile, choke points), and can run in parallel with them.
- **US4** (T027–T030) depends on US3 for T030 (the reconcile module) and on US2 for T029
  (the admin CLI structure from T016).
- **Polish** (T031–T035) runs after the stories it touches. T032 after US4 and T017; the
  others any time after Foundational.

### Within Each Story

Tests come first and must fail before the implementation. Then i18n keys, then the core
logic, then wiring.

### Parallel Opportunities

- **Phase 1**: T001 and T002.
- **Phase 2**: T005 (policy tests) alongside T003 and T004.
- **US1**: T009 (tests) and T010 (i18n), then T011 and T012.
- **US2 phase**: T013, T014 and T015a, all tests. T013 and T015a share a file, so write
  them one after the other or in separate test classes.
- **US3**: T018, T019, T020 and T021, all tests in different files. US3 can be developed
  by a second person while US1 and US2 are in progress.
- **US4**: T027 and T028.
- **Polish**: T031, T032, T033 and T034, all in different files.

## Parallel Example: User Story 3

```text
Task: "T018 Every previous_run unit test in tests/unit/test_every_schedule.py"
Task: "T019 Reconcile integration test in tests/integration/test_access_reconcile.py"
Task: "T020 Send refusal test in tests/integration/test_send_access.py"
Task: "T021 Claim/pool suspension tests in tests/integration/test_scheduler.py, test_client_pool.py"
```

## Implementation Strategy

### MVP first (US1 + US2): close the door

1. Phases 1–2, then US1 and US2 (T001–T017).
2. Deploy to the hosted instance:
   - back up the DB;
   - run `alembic upgrade head`;
   - `beta add` the existing users;
   - set `SCRIBE_OPERATOR_TELEGRAM_IDS` and `SCRIBE_ACCESS_GATE=true`.
3. Validate with quickstart steps 5–6.

### Incremental delivery

4. US3 (T018–T026): agents of removed users stop. Validate with quickstart step 7.
5. US4 (T027–T030): subscriptions. Validate with quickstart step 8.
6. Polish (T031–T035).

## Notes

- Never put real Telegram ids or usernames in tests or fixtures.
- Commit after each task or logical group.
- Stop at each checkpoint and run the story's Independent Test.
