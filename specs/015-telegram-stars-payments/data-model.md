# Data Model: Pay for a Subscription with Telegram Stars

**Migration**: `alembic/versions/0022_stars_payments.py` | **Research**: [research.md](research.md)

All tables are keyed by the person's Telegram id (`telegram_id`, as in specs/005), not by
`users.id`, because people without an account can pay (Clarifications Q1). None of them hold
message content.

## `payment_offers` (new)

One shown offer and its invoice link (research P2).

| Column | Type | Rules |
|---|---|---|
| `id` | uuid PK | Used in the payload `sub1:<id>` |
| `telegram_id` | bigint, not null, indexed | Who the offer was shown to |
| `price_stars` | integer, not null | `1..10000` (check); the price shown |
| `created_at` | timestamptz, not null | An offer is valid for the first payment for 1 hour (FR-003) |
| `used_at` | timestamptz, null | Set when its first payment is credited |

- Purged 7 days after `created_at` unless a `stars_subscriptions` row references it (FK
  `RESTRICT`; such rows are deleted with the subscription).
- Deleted by `/delete_my_data`.

## `stars_subscriptions` (new)

A person's recurring Stars subscription (research P4, P5).

| Column | Type | Rules |
|---|---|---|
| `id` | uuid PK | |
| `telegram_id` | bigint, not null, indexed | The payer |
| `offer_id` | uuid FK → `payment_offers.id`, not null, unique | Finds the subscription from a renewal payload or a `subscription` update |
| `first_charge_id` | text, not null, unique | Needed by `editUserStarSubscription` |
| `price_stars` | integer, not null | Price it started with (FR-015) |
| `started_at` | timestamptz, not null | |
| `period_end` | timestamptz, not null | End of the latest paid period (Telegram's `subscription_expiration_date`) |
| `state` | enum `stars_sub_state`: `active`, `canceled`, `ended` | |
| `canceled_at` | timestamptz, null | |
| `canceled_by` | enum `stars_cancel_source`, null: `user_bot`, `telegram`, `refund`, `deletion`, `operator` | |
| `ended_at` | timestamptz, null | |
| `ended_reason` | enum `stars_end_reason`, null: `canceled`, `failed`, `refunded`, `expired` | |
| `created_at` / `updated_at` | timestamptz | |

- **At most one running subscription per person**: partial unique index on `telegram_id`
  where `state IN ('active','canceled')` (FR-007).
- Deleted by `/delete_my_data`, after the Telegram side is cancelled (research P10). Ended rows
  are otherwise kept while the account exists (they are shown in `/my_data`) and purged with
  their last payment (3 years).

### State transitions

```text
           first payment
 (none) ───────────────▶ active ◀──────────────┐
                           │   │                 │ re-enable (pay:resume, or
          cancel           │   │ renewal payment │ `subscription` state=active)
  (pay:cancel, Telegram,   │   └─▶ active        │ before period_end
   deletion)               ▼     (period_end moves)
                        canceled ────────────────┘
                           │
   period_end passed       │  (or failed renewal,
   (reconcile)             ▼   refund, expiry > 1 h)
                         ended
```

- `active` → `ended`: refund (`refunded`); a `subscription` update with `failed`, ending at
  `period_end` (`failed`); no renewal by `period_end + 1 h` (`expired`).
- `canceled` → `ended`: `period_end` passed (`canceled`), or refund (`refunded`).
- `ended` is final, with one exception: a renewal payment (same payload, same payer) for a
  subscription that ended as `expired` or `failed` revives it to `active`, because Telegram
  has charged the person (analysis U1). Subscribing again otherwise creates a new row.

## `payments` (new)

One charge in Stars, first or renewal (FR-004).

| Column | Type | Rules |
|---|---|---|
| `id` | uuid PK | |
| `telegram_id` | bigint, not null, indexed | Payer |
| `charge_id` | text, not null, **unique** | `telegram_payment_charge_id`; exactly-once key |
| `amount_stars` | integer, not null | `> 0` |
| `kind` | enum `payment_kind`: `first`, `renewal` | |
| `paid_at` | timestamptz, not null, indexed | Message date, or the transaction date for a backfill |
| `period_start` | timestamptz, not null | |
| `period_end` | timestamptz, not null | `> period_start` |
| `stars_subscription_id` | uuid FK → `stars_subscriptions.id`, null, `ON DELETE SET NULL` | |
| `credited_via` | enum `payment_source`: `update`, `backfill` | |
| `refunded_at` | timestamptz, null | |
| `refunded_by` | text, null | `operator:<name>` or `telegram` |
| `owner_deleted_at` | timestamptz, null | Set by `/delete_my_data`; the row is kept (Clarifications Q3) |
| `created_at` | timestamptz | |

- **Minimal record after deletion** (FR-020): `telegram_id`, `charge_id`, `amount_stars`,
  `kind`, `paid_at`, `period_start`, `period_end`, `refunded_at`, `refunded_by`,
  `owner_deleted_at`. Nothing else exists on the row.
- **Retention**: deleted 3 years (1096 days) after `owner_deleted_at`, by `purge_messages`
  (Clarifications Q3). Rows with `owner_deleted_at IS NULL` are kept while the person's data
  exists.

## `subscriptions` (changed, from specs/005)

| Column | Change |
|---|---|
| `source` | NEW enum `subscription_source`: `operator` (default, existing rows), `stars` |
| `payment_id` | NEW uuid FK → `payments.id`, null, unique, `ON DELETE SET NULL`; set for `stars` |

- A `stars` row is one paid period: `starts_at = period_start`, `ends_at = period_end`,
  `granted_by = 'stars'`. Refund sets `cancelled_at` (research P8).
- **Operator commands**: `sub extend` and `sub end` act on `operator` rows only. `sub list` and
  `access show` print the source.
- **Reminder**: `reminded_at` per row, so each Stars period gets its own reminder (research
  P6).

## `access_events` (changed)

New `access_event_kind` values: `payment_received`, `payment_renewed`, `payment_refunded`,
`stars_canceled`, `stars_resumed`, `stars_failed`, `stars_ended`. `meta` holds `charge_id`,
`amount` and `by`, never text. The existing 365-day purge applies.

## `users` (unchanged)

No column. Access suspension stays `users.access_suspended_at` (specs/005).

## Derived: `AccessStatus` (changed)

`policy.status(beta, subs, stars_running=..., is_operator, now)`: a `stars` subscription row
whose Stars subscription is `active` counts as active until `ends_at + 1 h` (research P5
grace). Everything else is unchanged.

## Deletion (`delete_user_data`, changed)

Before the transaction, cancel an active Stars subscription on Telegram; if that fails, the
deletion is aborted and the person is asked to try again (research P10).

In the transaction, in this order:
1. Set `owner_deleted_at = now` on the person's `payments` (FK nulls itself when the
   subscription goes).
2. Delete `subscriptions` (all sources, as today), `stars_subscriptions` and `payment_offers`
   of the person.
3. Delete everything else, as today.

## Purge (`purge_messages`, changed)

- `payments` with `owner_deleted_at < now - 1096 days`.
- `stars_subscriptions` in state `ended` with no remaining payment.
- `payment_offers` older than 7 days and not referenced.

## Migration `0022_stars_payments`

1. Create enums `stars_sub_state`, `stars_cancel_source`, `stars_end_reason`, `payment_kind`,
   `payment_source`, `subscription_source`.
2. Add new `access_event_kind` values (`ALTER TYPE ... ADD VALUE`, outside a transaction
   block, as migration 0006 does).
3. Create `payment_offers`, `stars_subscriptions` (with the partial unique index), `payments`.
4. Add `subscriptions.source` (`NOT NULL DEFAULT 'operator'`) and `subscriptions.payment_id`.
5. Downgrade drops the new tables and columns. Enum values added to `access_event_kind` stay,
   as in earlier migrations.
