# Data Model: Beta & Subscription Access Gate

Migration `0005_access_gate` (down_revision `0004`, after the Jira migration from specs/003). It adds four tables, one column and
two enums. Existing rows need no backfill. A person's Telegram id equals their
`users.telegram_control_chat_id` (research Q1).

## `beta_entries`

One row per invited person.

| Column | Type | Rules |
|---|---|---|
| `id` | uuid PK | |
| `telegram_id` | bigint, null, **unique** | Set on add-by-id, or on the first message of a person added by username (FR-009) |
| `username` | text, null | Stored lowercase without `@`. Unique among rows where `telegram_id IS NULL` (partial unique index) |
| `note` | text, null | ≤ 200 chars. Operator's note, never message content |
| `added_by` | text, not null | `operator` (CLI) |
| `added_at` | timestamptz, not null | |
| `linked_at` | timestamptz, null | When the username entry was tied to a Telegram id |

- Check constraint: `telegram_id IS NOT NULL OR username IS NOT NULL`.
- **Remove** means deleting the row, which is recorded in `access_events`.
- **Matching** (`access.repo.find_beta`):
  1. Look up by `telegram_id`.
  2. Otherwise, look up an unlinked row whose `username` matches the sender's username,
     ignoring case. On a match, set `telegram_id` and `linked_at` in the same transaction.

## `subscriptions`

| Column | Type | Rules |
|---|---|---|
| `id` | uuid PK | |
| `telegram_id` | bigint, not null, indexed | Who it belongs to |
| `starts_at` | timestamptz, not null | |
| `ends_at` | timestamptz, not null | `> starts_at` (check) |
| `cancelled_at` | timestamptz, null | Set by `sub end` |
| `granted_by` | text, not null | `operator` |
| `note` | text, null | ≤ 200 chars, e.g. a payment reference |
| `reminded_at` | timestamptz, null | The 3-day reminder was sent (FR-016) |
| `created_at` / `updated_at` | timestamptz | |

- **Active** when `cancelled_at IS NULL AND starts_at <= now < ends_at`.
- **State** (derived): `active`, `ended` (`now >= ends_at`), `cancelled`, or `scheduled`
  (`now < starts_at`).
- **Extend**: update `ends_at` on the latest active or scheduled row and clear
  `reminded_at`. No new row.
- **Grant while one is active**: the new row starts at the current `ends_at`, so periods
  chain.

## `refused_contacts`

| Column | Type | Rules |
|---|---|---|
| `telegram_id` | bigint PK | |
| `first_seen_at` | timestamptz, not null | |
| `last_seen_at` | timestamptz, not null | |
| `last_answered_at` | timestamptz, null | Cooldown anchor (FR-006) |
| `attempts` | int, not null, default 0 | |

- No content, username or name is stored (FR-007).
- Purged when `last_seen_at < now - 30 days`, in the daily purge job.
- Deleted when the same person deletes their data.

## `access_events`

| Column | Type | Rules |
|---|---|---|
| `id` | uuid PK | |
| `telegram_id` | bigint, not null, indexed | No FK. The person may never have had an account. Events of a username-only beta entry that is not linked yet use `0` (`repo.UNLINKED_TELEGRAM_ID`) with `meta.beta_entry_id`, and are re-keyed to the real id when the entry links |
| `kind` | enum `access_event_kind` | See below |
| `actor` | text, not null | `operator` or `system` |
| `reason` | text, null | ≤ 200 chars. Operator note only |
| `meta_json` | jsonb, not null, default `{}` | Ids and dates only, e.g. `{"subscription_id":…, "ends_at":…}` |
| `at` | timestamptz, not null, indexed | |

- `access_event_kind` values: `beta_added`, `beta_removed`, `beta_linked`,
  `subscription_granted`, `subscription_extended`, `subscription_ended`,
  `subscription_reminded`, `access_lost`, `access_restored`.
- Purged after 365 days. Included in the `/my_data` export.

## `users` (changed)

| Column | Type | Rules |
|---|---|---|
| `access_suspended_at` | timestamptz, null, **new** | Set or cleared only by the `access_reconcile` job. Independent of `state` (research Q6) |

Indexed partially on `access_suspended_at IS NULL` for the scheduler's claim query.

## Derived: `AccessStatus` (not stored)

`scribe/access/policy.py`:

```
AccessKind = operator | beta | subscribed | beta_and_subscribed | none
AccessStatus(kind: AccessKind, until: datetime | None)
```

Rules:

- `operator` if the sender's id is in `Settings.operator_telegram_ids`.
- Otherwise `beta` is set by a matching `beta_entries` row, and `subscribed` by any active
  subscription, where `until` is the latest `ends_at` among active rows.
- `none` if neither applies.
- With `Settings.access_gate = false`, every caller treats the result as allowed. The
  status is still computed for `access show`.

## State transitions of a user's access

```
            access present                      access none
 ┌──────────────────────────┐  reconcile: lost   ┌──────────────────────────────┐
 │ serving                  │ ─────────────────▶ │ suspended                    │
 │ access_suspended_at=NULL │                    │ access_suspended_at=<time>   │
 │ (state as before)        │ ◀───────────────── │ client stopped, drafts       │
 └──────────────────────────┘ reconcile: regained│ expired, bot in restricted   │
                                                 │ mode                         │
                                                 └──────────────────────────────┘
```

- **On lost**:
  - set `access_suspended_at = now`;
  - pending drafts become `expired` and their turns `handle_manually`;
  - write an `access_lost` event;
  - after commit, stop the pool and send the "access ended" notice once.
- **On regained**:
  - clear the column;
  - write an `access_restored` event;
  - after commit, call `pool.start(user_id)` and send the "access restored" notice. If the
    Telegram session is not active, the notice adds the `/telegram` reconnect hint (US3
    scenario 3).
- `users.state` (`setting_up`, `active`, `paused`, `out_of_office`) is untouched by both
  transitions. A user who had paused themselves stays paused after access returns.

## Deletion (`delete_user_data`)

- **Deleted**: everything. That is the user's own data, as today, plus every
  `beta_entries`, `subscriptions`, `access_events` and `refused_contacts` row whose
  `telegram_id` equals the user's `telegram_control_chat_id`. Username-only beta entries
  are already linked by then, because the person has messaged the bot. All of it goes in
  one transaction (research Q10, Principle II).
- **Before** deletion, the confirmation warns that beta access and any active subscription
  will be lost.
- **After** deletion, the person is a stranger to the gate. Coming back needs a new `beta
  add` or `sub grant`.

## `beta_applications` (US5, migration `0006_beta_applications`)

| Column | Type | Rules |
|---|---|---|
| `id` | uuid PK | |
| `telegram_id` | bigint, not null, indexed | The applicant. No FK, since applicants have no `users` row |
| `username` | text, null | Lowercase without `@`, as sent by Telegram at submit time |
| `use_case` | text, null | 10–1000 chars at submit. Set to NULL 30 days after the decision (FR-029) |
| `language` | text, not null | `en` or `ru` |
| `consent_at` | timestamptz, not null | When **Send application** was pressed (FR-023) |
| `status` | enum `beta_application_status`, not null | `pending`, `approved` or `rejected` |
| `decided_at` | timestamptz, null | |
| `decided_by` | bigint, null | Operator Telegram id. NULL when decided from the CLI |
| `operator_messages` | jsonb, not null, default `[]` | `[[chat_id, message_id], …]` of the copies sent to operators |
| `created_at` / `updated_at` | timestamptz | |

Constraints and indexes:
- partial unique index on `telegram_id` where `status = 'pending'` (FR-024);
- check `char_length(use_case) <= 1000`.

Rules:
- **Applying again**: allowed only if there is no pending application and no `rejected` one
  with `decided_at > now - 30 days`.
- **Retention**: the daily purge deletes decided applications whose `decided_at` is more
  than 30 days old (FR-029). Pending ones are kept until decided.
- **Deletion**: `delete_user_data` deletes all applications with the user's
  `telegram_control_chat_id`. `/my_data` lists them with status and dates; the use case is
  included in the JSON export only.

Migration `0006` also extends `access_event_kind` with `application_submitted`,
`application_approved` and `application_rejected`, using `ALTER TYPE … ADD VALUE IF NOT
EXISTS`.
