# Research: Beta & Subscription Access Gate

**Feature**: `specs/005-beta-subscription-access` | **Date**: 2026-10-21

The Technical Context has no open unknowns: the stack is the existing one. These notes
record the design questions and how they were decided against the current code.

## Q1. Who is "the person" the gate checks?

**Decision**: the sender's Telegram user id (`from_user.id`). In a private chat it is the
same number as `users.telegram_control_chat_id`, so existing users map to it without a
backfill. `users.telegram_user_id` is not used: it is set only after a Telegram login and
cleared on disconnect.

**Rationale**: the gate must work before a `users` row exists (FR-002), and the id never
changes when the username does (FR-009).

**Alternatives considered**: `users.telegram_user_id`, which is empty for new and
disconnected people. Usernames only, which can change and can be taken over.

## Q2. Where does the check run in the control bot?

**Decision**: a new outer middleware `AccessGateMiddleware` on `message`, `callback_query`
and `inline_query`. It is registered right after `UserContextMiddleware`, which already
drops non-private chats and gives it `user` and `lang`. It decides one of:

- **allow**: the gate is off, the sender is the operator, or `access.status()` is not
  `none`. The handler runs as today.
- **refuse**: there is no access and no `users` row. The bot sends the rate-limited "no
  access" reply (Q5) and stops. The handler never runs, so `/start` cannot create a user
  row.
- **restricted**: there is no access but a `users` row exists. Only `/my_data`, the
  `mydata:*` callbacks, `/delete_my_data` and its `DeleteStates.confirm` text step pass.
  Everything else gets the "access ended" reply, rate-limited the same way.

**Rationale**: one place covers every current and future handler (FR-003). Routers do not
need to know about the gate. `inline_query` has no user middleware today, so the gate adds
its own minimal sender check there.

**Alternatives considered**:
- A check inside `setup_flow.start_command`: it misses the other commands and future ones.
- A filter on each router: easy to forget on a new router.
- A gate before `UserContextMiddleware`: it would duplicate the private-chat and language
  logic.

## Q3. How is access computed?

**Decision**: a pure function in a new module `scribe/access/policy.py`:

```
status(beta: BetaEntry | None, subs: list[Subscription], is_operator: bool, now) -> AccessStatus
```

`AccessStatus` holds `kind` (`operator | beta | subscribed | beta+subscribed | none`) and
`until` (the latest end among active subscriptions, or `None`). A subscription is active
when `starts_at <= now < ends_at` and it is not cancelled. A thin repository function loads
the rows for one Telegram id, or for all users in the reconciliation job.

**Rationale**: deterministic and unit-testable without a DB (constitution, Principle V).
The middleware, the reconciliation job, the CLI `access show` command and the send check
all share it.

## Q4. How does a change made by the operator CLI reach the running service?

`admin.py` is a separate process, so it cannot call `ClientPool.stop()`.

**Decision**: two paths.

1. **Bot side, instant.** The middleware reads the DB on every update, so additions and
   removals apply from the next message (FR-010, US2 scenario 2).
2. **Agent side, within about 2 minutes.** A new scheduler job `access_reconcile` runs every minute.
   It compares each user's computed access with the new column `users.access_suspended_at`:
   - **Lost** (access none, column null): set the column. Expire the user's pending drafts.
     Stop the Telethon client after commit. Send the "access ended" notice once. Write an
     access event.
   - **Regained** (access present, column set): clear the column. Start the client, which
     is a no-op if the Telegram session is not active. Send the "access restored" notice.
     Write an access event.
   - **Subscription ending**: if a subscription ends in 3 days or less, `reminded_at` is
     null, and no other grant extends access past it, send the reminder and set
     `reminded_at`.

   This needs a new `Interval(seconds)` schedule kind next to `Daily` and `Weekly`. It is a
   small addition to `next_run()`.

**Rationale**: it reuses the existing DB-polling scheduler, the way every other background
job works, with no IPC and no new service (Principle VI). One minute is well inside the
5-minute target (FR-010, SC-003). The same job handles time-based subscription expiry,
which no CLI action triggers.

**Alternatives considered**:
- Postgres `LISTEN/NOTIFY` from the CLI: instant, but a new mechanism, and it still needs
  polling for time-based expiry.
- A health-port admin endpoint: it opens a network attack surface.

## Q5. How is the "no access" answer rate-limited without storing content?

**Decision**: a table `refused_contacts` (`telegram_id` PK, `first_seen_at`,
`last_seen_at`, `last_answered_at`, `attempts`). Each refused update does an upsert that
bumps `attempts` and `last_seen_at`. The bot answers only if `last_answered_at` is null or
older than `access_refusal_cooldown_s` (default 3600). Restricted users with a `users` row
use the same table for their "access ended" answers. The daily purge job deletes rows whose
`last_seen_at` is older than 30 days (FR-007).

**Rationale**: it holds the minimum (FR-007) and gives the demand count (FR-020: rows with
`last_seen_at` inside the period). It survives restarts, unlike an in-memory map.

**Alternatives considered**: an in-memory TTL map, which is lost on restart and gives no
demand numbers. Reusing `activity_records`, which has a FK to `users` and cascades.

## Q6. How is a paused user kept from being processed?

The existing user-controlled `paused` state only stops step-1 drafting. The client keeps
running and `/resume` would undo it.

**Decision**: a separate nullable column `users.access_suspended_at`, independent of
`users.state`, so `/pause` and `/resume` keep their meaning and cannot override the gate.
It is checked at each place that acts for a user:

| Place | Change |
|---|---|
| `ClientPool.start_all` / `users_repo.list_user_ids_with_session_status` | skip suspended users at boot |
| `ClientPool.start` | refuse to start a suspended user |
| `Scheduler._claim_due_turns` (`drafting_users` subquery) | add `access_suspended_at IS NULL` |
| `send_approved_draft` | after `_lock_draft`, raise `AccessSuspended(SendRefused)` if suspended (defence in depth for the race between a tap and the job) |
| `DraftPresenter.post_card` | a suspended user's new draft is expired, not shown |
| `jobs.refresh_profiles` query | skip suspended users |
| Review/settings handlers | already unreachable through the middleware (restricted mode) |

Intake stops by itself because the client is stopped. Turns already queued remain and are
cleaned up by the normal retention.

**Rationale**: the agent's work goes through these few choke points (verified in the code),
so the gate holds even if the bot middleware were bypassed.

**Alternatives considered**: a new `UserState.suspended`, which would collide with the
`paused`/`out_of_office` restore logic in `/resume` and with every `state.in_(...)` query.

## Q7. Pending drafts when access ends (US3 scenario 4, FR-012)

**Decision**: the reconciliation job marks the user's `pending` drafts as `expired`, as the
existing deadline expiry does, and sets their turns to `handle_manually`. It sends no
per-draft cards: the single "access ended" notice says that pending replies were not sent.
A draft being composed at that moment is stored by the composer as usual. Then
`DraftPresenter.post_card` sees the suspension, expires the draft and does not show it.
The send-time check (Q6) makes sure no draft of a suspended user can ever be sent.

## Q8. Operator identity

**Decision**: a new setting `SCRIBE_OPERATOR_TELEGRAM_IDS` (a list of ints, default
empty). These ids always have access (FR-022). With the gate on and the list empty, the
service logs a startup warning `access_gate_no_operator` but still starts: the operator can
add themselves with `beta add`.

## Q9. The gate switch and links

**Decision**:
- `SCRIBE_ACCESS_GATE`: bool, default `false`, so self-hosted installations and existing
  deployments keep today's behaviour (FR-021). The hosted `.env` sets `true`.
- ~~`SCRIBE_ACCESS_REQUEST_URL`~~: removed on 2026-10-22 (Q14). Access is requested inside
  the bot (Q12).
- `SCRIBE_SUBSCRIPTION_CONTACT`: free text or a link for arranging a subscription. If
  empty, the subscription line is left out of the message.
- `SCRIBE_ACCESS_REFUSAL_COOLDOWN_S`: default `3600`.

The health endpoint gains `access_gate: true|false` so the operator can see the mode at a
glance.

## Q10. Audit of access changes (FR-019)

**Decision**: a new table `access_events` keyed by `telegram_id`, with no FK to `users`, so
it can record events for people who never had an account. Each row has `kind`, `actor`
(`operator` or `system`), an optional `reason` (operator note, length-capped, never message
content) and `at`.

User actions such as `/delete_my_data` stay in `activity_records` as today. Access events
are included in the `/my_data` export. Access events are purged after 365 days by the daily
purge job.

**Deletion (decided 2026-10-21, option a)**: `/delete_my_data` deletes **everything** keyed
to the person's Telegram id: `beta_entries`, `subscriptions`, `access_events` and
`refused_contacts`, in the same transaction as the user row (constitution Principle II,
FR-011). Before the user confirms, the confirmation text warns that their beta access or
active subscription (with its end date) will be lost, and that they will need to ask the
operator again to come back. The only trace left is the structured log line
`access_records_deleted`, which holds counts only and no Telegram id. The operator is
expected to keep payment records in their own accounting, outside Scribe.

**Alternatives considered**: keeping the grant and payment records after deletion as an
operator record. This was rejected because it is an exception to Principle II, which says
the user must be able to delete everything stored about them.

## Q11. CLI shape

**Decision**: extend `scribe.admin` with argparse sub-subcommands. The full list is in
[contracts/operator-cli.md](contracts/operator-cli.md).

- `beta add|remove|list`
- `sub grant|extend|end|list`
- `access show <who>`
- `access stats [--days N]`

`<who>` is a numeric Telegram id or `@username`. `beta add` by username is the only form
allowed to be unlinked.

The commands reuse `run(argv, session_factory)`, so tests call them in-process with the
integration DB fixtures (`admin.py` has no tests today; this feature adds them).

## Q12. Applying for the beta inside the bot (US5, added 2026-10-22)

**Decision**: a new control-bot router `src/scribe/access/apply.py` with FSM states
`ApplyStates.use_case` and `ApplyStates.review`.

- The "no access" reply (refuse and restricted) carries an inline keyboard:
  - `apply:start` (**Join the beta**);
  - `apply:subinfo` (**Subscription**).
- **`apply:start`** asks for the use case and sets `ApplyStates.use_case`.
- **A text message in `ApplyStates.use_case`**: the text is validated (10–1000 chars, text
  only) and kept **only in the FSM data**. The bot shows a preview with the consent note and
  the buttons `apply:send`, `apply:edit` and `apply:cancel`, then sets `ApplyStates.review`.
- **`apply:send`**:
  - inserts the `beta_applications` row, recording `consent_at = now`;
  - records the `application_submitted` event;
  - clears the FSM;
  - after commit, confirms to the person and sends the application to every operator.
- **`apply:subinfo`** sends the placeholder text and stores nothing.

The gate (Q2) lets through, for senders without access with or without a `users` row:
- callbacks starting with `apply:`;
- any message while in `ApplyStates`;

These do not count against the refusal cooldown. Operators always pass the gate, so the
decision callbacks need no special case.

**Rationale**: it reuses the existing FSM storage and inline-keyboard patterns. No user row
is created for an applicant (FR-002). The typed text is never stored as an application
before the person presses **Send application**, i.e. before consent.

**Abandoned flows**: an `ApplyStates` FSM row holds the unsent text. The daily purge
deletes `bot_fsm_state` rows in `ApplyStates:*` that have not changed for 24 h (FR-007).

## Q13. Operator review

**Decision**: each new application is sent to every id in `operator_telegram_ids` as plain
text with no parse mode, so the use case cannot inject markup. The message holds:
- the @username, or "no username";
- the Telegram id;
- the language;
- the use case;
- buttons `betaapp:approve:<uuid>` and `betaapp:reject:<uuid>`.

The (chat_id, message_id) pairs are stored in `beta_applications.operator_messages`.

A decision runs in one transaction:
1. `SELECT … FOR UPDATE` the application. If it is no longer pending, only refresh this
   operator's message.
2. Set `status`, `decided_at` and `decided_by` (the operator's Telegram id).
3. On approve, create a `beta_entries` row for the Telegram id unless one exists
   (`added_by="operator"`, note `"application"`), and record `beta_added`.
4. Record `application_approved` or `application_rejected`.

After commit:
- edit every operator copy to show the decision, who made it and when, and remove the
  buttons;
- send the applicant `access.apply_approved` or `access.apply_rejected` in the
  application's language.

The decision callbacks are registered only for operators: a filter checks
`is_operator(from_user.id)`, and anyone else gets a silent answer.

**CLI** (FR-027):
- `beta requests [--all]` lists applications;
- `beta approve <application id>` and `beta reject <application id>` run the same decision
  function, with the operator recorded as `operator` (no id) and the same notifications.
  The CLI has no bot polling, but it can send messages with a `Bot(token)` built from
  settings, as `_main` already builds settings.

## Q14. The link setting

**Decision**: remove `access_request_url` and every `{request_url}` placeholder.
- The refusal and the restricted reply get the buttons instead.
- `access.ended_notice` and `access.sub_reminder` say "send /start" instead.
  - `{renew}` for the reminder becomes `subscription_contact` if set, otherwise "send
    /start".
- `subscription_contact` stays optional. If set, it is appended to the subscription
  placeholder text.
