# Data Model: Auto-Send for Trusted Chats

**Feature**: `specs/014-auto-send` | **Migration**: `0021_auto_send` (revises `0020`; see
research A10)

No message content is stored by this feature. All new rows and columns are deleted with the
user (existing `ON DELETE CASCADE`).

## `work_chats` (new columns)

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `auto_send` | `BOOLEAN` | no | `false` | FR-001/FR-002: auto-send on for this chat |
| `auto_send_changed_at` | `TIMESTAMPTZ` | yes | `NULL` | last on/off change |
| `auto_send_source` | `auto_send_source` enum (`manual`, `offer`) | yes | `NULL` | how it was last turned on |
| `auto_send_offer` | `auto_send_offer_state` enum (`none`, `offered`, `opted_out`) | no | `'none'` | FR-016/FR-017 |
| `auto_send_offer_not_before` | `TIMESTAMPTZ` | yes | `NULL` | no offer before this time (Not now = +30 d) |
| `auto_send_warned_at` | `TIMESTAMPTZ` | yes | `NULL` | last "consider turning off" suggestion (FR-020, 7 d) |

Checks:
- `ck_work_chats_auto_send_needs_source`: `NOT auto_send OR auto_send_source IS NOT NULL`.

Invariant kept by code (not a constraint, because `enabled` changes elsewhere): `auto_send`
implies `enabled AND NOT excluded`. Every path that disables or excludes a chat also clears
`auto_send` (FR-021).

## `users` (new columns)

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `auto_send_window_s` | `INTEGER` | no | `120` | FR-007 cancel window |
| `auto_send_last_offer_at` | `TIMESTAMPTZ` | yes | `NULL` | FR-018 one offer per 24 h |

Checks: `ck_users_auto_send_window_range`: `auto_send_window_s BETWEEN 60 AND 900`.

## `drafts` (new columns, changed constraint)

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `auto_send_due_at` | `TIMESTAMPTZ` | yes | `NULL` | set when the card is posted with a countdown; end of the cancel window |
| `auto_send_stop` | `auto_send_stop` enum | yes | `NULL` | why the countdown stopped; `NULL` while running or never armed |
| `auto_approved_at` | `TIMESTAMPTZ` | yes | `NULL` | set by the auto-send step when it fires (research A1) |

`auto_send_stop` values: `user_action`, `disabled`, `outdated`, `ineligible`, `outside_hours`,
`paused`, `send_failed`.

Constraints:
- `sent_requires_approval` (replaced): `state <> 'sent' OR approved_by_user_at IS NOT NULL OR
  auto_approved_at IS NOT NULL`.
- `ck_drafts_auto_approved_needs_window`: `auto_approved_at IS NULL OR auto_send_due_at IS NOT
  NULL`.
- No exclusivity check between the two approvals: after `send_failed` the user's Send sets
  `approved_by_user_at` on a row that already has `auto_approved_at`, and the retry reuses the
  same `send_random_id` (Telegram deduplicates). `/history` then shows both, in order.
- Partial index `ix_drafts_auto_send_due` on `auto_send_due_at` `WHERE state = 'pending' AND
  auto_send_stop IS NULL AND auto_approved_at IS NULL`.

Retention: the three columns are metadata and survive the 30-day content purge.

### Countdown state machine (one pending draft)

```text
  card posted, eligible          ─► ARMED   (due_at = now + window, stop = NULL)
  card posted, not eligible      ─► MANUAL  (due_at = NULL) card lists reasons
  ARMED + user taps any action   ─► STOPPED (stop = user_action)  → ordinary pending
  ARMED + new incoming message   ─► STOPPED (stop = outdated)     → existing outdated card
  ARMED + chat/all turned off    ─► STOPPED (stop = disabled)     → card re-rendered
  ARMED + manual reply / chat removed ─► draft dropped (existing paths)
  ARMED + due, re-check fails    ─► STOPPED (stop = ineligible | outside_hours | paused)
  ARMED + due, re-check passes   ─► FIRING  (auto_approved_at = now) ─► send
  FIRING + send ok               ─► draft sent  (card: "sent automatically")
  FIRING + send fails            ─► STOPPED (stop = send_failed), approval kept for the
                                    idempotent retry by Send; card shows the failure
```

## `draft_decisions` (new table)

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | `UUID` PK | no | |
| `user_id` | `UUID` FK `users` CASCADE | no | |
| `work_chat_id` | `UUID` FK `work_chats` CASCADE | no | chat the draft replied in |
| `draft_id` | `UUID` FK `drafts` SET NULL | yes | |
| `decision` | `draft_decision` enum | no | see below |
| `during_auto_send` | `BOOLEAN` | no | the draft had a countdown armed when decided |
| `at` | `TIMESTAMPTZ` | no | |

`draft_decision` values: `sent_unchanged`, `sent_edited`, `rejected`, `regenerated`,
`auto_sent`, `auto_cancelled`.

Index: `ix_draft_decisions_chat_at` on (`work_chat_id`, `at`).

Writers (same transaction as the action): `_approve_and_send` result (unchanged/edited),
`on_reject`, `on_regen`, auto-send step (`auto_sent`, written after a successful send in
`send.py`), `autostop` (`auto_cancelled`).

Readers:
- **Offer rate** (FR-014/FR-015): rows in the last 30 days with `decision NOT IN ('auto_sent',
  'auto_cancelled')`; rate = `sent_unchanged / count`; needs `count >= 20` and rate `> 0.60`.
- **Intervention rate** (FR-020): last 10 rows with `during_auto_send` grouped by `draft_id`
  (one outcome per draft: `auto_sent` = no intervention, anything else = intervention);
  suggest when interventions `> 4`.

Retention: `purge_messages` deletes rows with `at < now - 31 days`.

## `activity_kind` (new enum values)

| Value | Meta (ids/counts only) |
|---|---|
| `auto_send_on` | `work_chat_id`, `source` (`manual`/`offer`) |
| `auto_send_off` | `work_chat_id` or `all: true`, `reason` (`user`, `chat_disabled`, `excluded`, `work_account_off`) |
| `auto_send_offered` | `work_chat_id`, `unchanged`, `total` |
| `auto_send_offer_answered` | `work_chat_id`, `answer` (`on`/`later`/`never`) |
| `auto_approved` | draft id; `window_s` |
| `auto_send_stopped` | draft id; `reason` (an `auto_send_stop` value) |

The existing `sent` record gains `meta.auto: true` for automatic sends. History labels
`history.kind.<value>` are added for each new kind in `en.py` and `ru.py`.

## Validation rules from the spec

- Window 60–900 s (FR-007), enforced by the check and by the settings handler.
- Threshold constants live in `sending/auto_send.py`: `OFFER_MIN_DECISIONS = 20`,
  `OFFER_RATE = 0.60` (strict `>`), `OFFER_WINDOW = 30 d`, `OFFER_SNOOZE = 30 d`,
  `OFFER_GAP = 24 h`, `WARN_SAMPLE = 10`, `WARN_MAX_INTERVENTIONS = 4`, `WARN_GAP = 7 d`.
