# Research: Pay for a Subscription with Telegram Stars

**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Date**: 2026-11-07

Sources: Telegram Bot API (Payments section: `createInvoiceLink`, `SuccessfulPayment`,
`RefundedPayment`, `editUserStarSubscription`, `refundStarPayment`, `getStarTransactions`,
`answerPreCheckoutQuery`, `BotSubscriptionUpdated`), "Bot Payments API for Digital Goods and
Services" (core.telegram.org/bots/payments-stars), and aiogram 3.31.0 as pinned in `uv.lock`
(it has `Router.pre_checkout_query`, `Router.subscription`, `Bot.create_invoice_link(...,
subscription_period=...)`, `Bot.edit_user_star_subscription`, `Bot.refund_star_payment`,
`Bot.get_star_transactions`). Existing code: `scribe.access` (specs/005).

## P1. How is a recurring Stars subscription created?

**Decision**: `Bot.create_invoice_link` with `currency="XTR"`, an empty provider token, one
price of `stars_price` Stars and `subscription_period=2592000`. The bot sends the link as an
inline URL button (**Subscribe for N ⭐**) under the offer text. A new link is made for every
offer.

**Rationale**: the Bot API allows `subscription_period` only on `createInvoiceLink`, not on
`sendInvoice`. It "must always be 2592000 (30 days)", and the subscription price "must not
exceed 10000 Telegram Stars". Digital services in bots must be sold in Stars only
(payments-stars page).

**Alternatives considered**: `sendInvoice` with a one-time payment, rejected because the user
chose recurring billing (Clarifications Q2). Mini-app checkout was rejected as needless here.

**Config check**: `stars_price` must be an integer from 1 to 10000. Startup fails on any other
value (pydantic `Field(ge=1, le=10000)`).

## P2. What goes in the invoice payload, and how is an offer checked?

**Decision**: a new table `payment_offers` (id, telegram_id, price, created_at). The payload
is `sub1:<offer uuid>`, 41 bytes, under the 1–128 byte limit. The payload carries no personal
data. The offer row holds the price the person saw.

**Rationale**: the pre-charge check (FR-003) needs the shown price and the offer time, and the
person may hold an older link. Only the bot can create invoices, so the payload cannot be
forged. A row is simpler than signing the payload and can be inspected when debugging. Every
renewal repeats the payload, and `BotSubscriptionUpdated` carries it too. So the offer id is
also how a renewal or a state change finds its Stars subscription.

**Alternatives considered**: an HMAC-signed payload with no table. It would need a key and still
needs a DB lookup for "one active subscription per person", so it was rejected.

## P3. The pre-checkout check (FR-003, SC-003)

**Decision**: an answer on `pre_checkout_query` within the 10 s Telegram allows: one DB
transaction, no other network call, and a 5 s internal timeout that declines on expiry. It
accepts only if all of these hold:

1. `currency == "XTR"` and the payload parses to an offer that exists.
2. **Renewal path**: the offer is already linked to a Stars subscription of this payer
   (`from_user.id`) in state `active` or `canceled` (a lost `subscription` re-enable update must
   not block a renewal). Accept. Telegram does not say whether renewals send
   pre-checkout queries, so the handler accepts them if they come.
3. **First-payment path**:
   - `payments_enabled` is on;
   - `total_amount` equals the offer price and the current `stars_price`;
   - the offer is younger than 1 hour;
   - the payer has no Stars subscription in state `active` or `canceled`.

Otherwise it declines with a localized `error_message`, a short reason from
contracts/control-chat.md. A declined check is logged as `stars_precheckout_declined` with
the reason code and no id.

The payer's id need not equal the offer's `telegram_id`: a forwarded link bills whoever pays,
for themselves. The offer owner is kept only for the `/my_data` view and for offer
retention.

**Rationale**: Telegram says goods must be delivered only after `successful_payment`. The
pre-checkout step is the only point where a charge can still be refused.

## P4. Crediting a payment exactly once (FR-004, FR-005, SC-002)

**Decision**: a `successful_payment` message handler calls `credit_payment(...)`. In one
transaction it does the following:

- **Idempotency**: inserts the `payments` row, keyed by the unique
  `telegram_payment_charge_id` with `ON CONFLICT DO NOTHING`. If the row already exists, it
  returns "already credited" and does nothing else.
- **First payment** (`is_first_recurring`): it marks the offer used and creates a
  `stars_subscriptions` row in state `active`.
- **Renewal**: it finds the Stars subscription by offer id and payer.
- **Access period**: it inserts the access period into `subscriptions` with `source='stars'`
  and `payment_id`. The period runs from the later of `paid_at` and the previous period's end
  until `subscription_expiration_date`, which Telegram reports as authoritative. It also moves
  `stars_subscriptions.period_end`.
- **Audit**: it writes `access_events` (`payment_received` or `payment_renewed`).

After commit, the person gets a confirmation for the first payment only (FR-006).
`access_reconcile` lifts a suspension on its next run, as today. The gate reads the new row on
the next message.

**Restart safety**: aiogram polling confirms an update by the next `getUpdates` offset, so a
crash while an update is being handled can lose it. A job, `stars_reconcile`, runs every
10 minutes and once at start. It reads `getStarTransactions` for the last 7 days and credits any
incoming `TransactionPartnerUser` payment whose charge id is missing, through the same
`credit_payment`. The transaction carries the payer, the amount, the payload and
`subscription_period`. For a renewal found this way, the end of the period is
`date + subscription_period`. The person is told then (edge case "Payment arrives while the
service is restarting").

**Alternatives considered**:
- Webhooks: these are a deployment change, still not exactly-once, and were rejected.
- Trusting polling alone: this fails SC-002, so it was rejected.

## P5. Renewal, cancellation and failure (US2, FR-008–FR-011)

**Decision**:
- **Renewal**: arrives as a `successful_payment` with `is_recurring=true` and handled as in
  P4. No message is sent to the person.
- **Cancel or re-enable from the bot** (`/account` or the reminder button): call
  `edit_user_star_subscription(user_id, telegram_payment_charge_id=<first charge>,
  is_canceled=True|False)`. On success, set the state to `canceled` or `active`. On a Telegram
  error, change nothing and tell the person to try again.
- **Changes made in Telegram**: the `subscription` update (`BotSubscriptionUpdated`) reports
  `canceled`, `active` (re-enabled) or `failed`. The handler finds the subscription by payload
  and payer and sets the state:
  - `canceled` → `canceled`, with `canceled_by=telegram`;
  - `active` → `active`;
  - `failed` → state `ended` at the end of the current period, with `ended_reason=failed`.
    The access period stays in place until its end. The person is told when access ends
    (FR-010).
- **Fallback (FR-011)**: an `active` subscription whose `period_end` passed more than 1 hour
  ago with no renewal is set to `ended` with `ended_reason=expired` by `access_reconcile`.
- **Grace (edge case "Late renewal confirmation")**: while a Stars subscription is `active`,
  its latest access period counts as active until `period_end + 1 h`. This is added in
  `policy.status`. A `canceled` or ended subscription gets no grace. The "access ended" notice
  for a renewing subscriber therefore waits out the hour.
- **Updates must be requested**: Telegram skips some update types unless they are requested.
  The bot passes `allowed_updates=dispatcher.resolve_used_update_types()` to `start_polling`,
  so `pre_checkout_query` and `subscription` are delivered.

## P6. Reminders 3 days ahead (FR-008)

**Decision**: keep the `ending` branch of `access_reconcile` (specs/005 FR-016) and extend the
condition. A period whose Stars subscription is `active` (renewing) gets
`access.stars_renew_reminder` with the date, the amount and a **Cancel subscription** button
(`pay:cancel`). A `canceled` one gets `access.stars_end_reminder` with **Turn renewal back
on** (`pay:resume`). An operator-granted subscription keeps `access.sub_reminder`, now ending
with "subscribe with Stars: press Subscription in /account" when payments are on. A renewing
subscriber is reminded every period: `reminded_at` sits on the access period row, and each
period is a new row.

## P7. Gate integration (FR-001, FR-012, US1 scenario 5)

The `AccessGateMiddleware` sits on `message`, `callback_query` and `inline_query`, so
`pre_checkout_query` and `subscription` updates never pass through it. They need no gate: they
only touch payment rows.

These pass the gate for anyone, without counting against the refusal cooldown:
- service messages with `successful_payment` or `refunded_payment`, so a person with no account
  is credited;
- `pay:` callbacks;
- `/paysupport` and `/terms`, and messages in `PaySupportStates`.

`no_access_reply` already has the **Subscription** button. Its `apply:subinfo` handler
switches to the offer when payments are on, and to the placeholder otherwise. The
pending/rejected application text gains that button (today it has none: specs/005 FR-005),
but only when payments are on.

**Rationale**: an allow-list in the gate is the same pattern as `apply_allows` in specs/005.

## P8. Refunds (US3, FR-013, FR-014)

**Decision**: the operator CLI command `python -m scribe.admin pay refund <charge id>` does the
following:

1. If the payment belongs to a Stars subscription that is `active` or `canceled`, call
   `edit_user_star_subscription(is_canceled=True)`. A Telegram error here is reported and the
   refund still continues.
2. Call `refund_star_payment(user_id, charge_id)`. On error, stop and change nothing.
3. In one transaction:
   - set `payments.refunded_at` and `refunded_by`;
   - cancel the access period that payment bought (`subscriptions.cancelled_at`), so access
     ends now unless another source remains;
   - set the Stars subscription to `ended` with `ended_reason=refunded`;
   - write `payment_refunded`.
4. After commit, send `pay.refunded` to the person. `access_reconcile` stops the agent within
   2 minutes, as for any loss (FR-014).

The `refunded_payment` service message, and a refund made outside the CLI, are handled by the
same idempotent `apply_refund` with `refunded_by='telegram'`.

**Alternatives considered**: a refund button in the operator's bot chat. It was rejected for
v1 because refunds are rare and the CLI already holds operator actions (specs/005 Q11).

## P9. Settings (FR-015, FR-021)

**Decision**: two settings in `Settings`:
- `payments_enabled: bool = False` (env `SCRIBE_PAYMENTS_ENABLED`);
- `stars_price: int | None` (env `SCRIBE_STARS_PRICE`, 1–10000).

Offers are made only when `payments_enabled` and `stars_price` are set and `access_gate` is on.
With the gate off, everyone already has access, so there is nothing to sell. A change applies
on restart. The spec allows a price "set once at deployment" for v1 (US4), and a restart does
not touch running subscriptions, which keep their Telegram-side price (FR-015). The plan
description is a fixed i18n text.

**Alternatives considered**: a DB-stored price editable by a CLI command. That is more code for
a value that changes rarely, so it was rejected for now.

## P10. Data deletion and retention (FR-019, FR-020; constitution exception)

**Decision**:
- **When the person confirms `/delete_my_data`, before rows are deleted**: if they have an
  `active` Stars subscription, call `edit_user_star_subscription(is_canceled=True)`. If that
  fails, abort the deletion and ask them to retry, so they are never charged after deleting.
- **The delete transaction**:
  - deletes `payment_offers` and `stars_subscriptions` of the person;
  - deletes the access periods (as today);
  - keeps `payments` rows, setting `owner_deleted_at`, nulling `stars_subscription_id`, and
    leaving `telegram_id`, `amount`, `paid_at`, `charge_id`, `refunded_at`. The period columns
    stay too: they are dates of a payment, not content.
- **Purge**: `purge_messages` deletes `payments` whose `owner_deleted_at` is older than 3 years
  (1096 days), and `payment_offers` older than 7 days.
- **Warning**: `access.delete_warning` gains a line from contracts/control-chat.md.

**Rationale**: this follows Clarifications Q3. The exception to Principle II is recorded in
plan.md (Complexity Tracking).

## P11. `/paysupport` and `/terms` (FR-012)

**Decision**:
- **`/paysupport`** works for everyone in the private chat. The bot explains payment help and
  enters `PaySupportStates.describe`. The next text, 1–1000 characters, is forwarded as plain
  text to every operator with `id`, `@username` and the charge ids of the payer's payments in
  the last 90 days. The text is **not stored**: it is only forwarded.
  - If no operator is configured, the person gets `subscription_contact` instead.
  - Operator messages use `parse_mode=None`.
  - `/cancel` leaves the state.
  - A cooldown of 1 request per 10 minutes per person uses the FSM data timestamp.
- **`/terms`** is a static i18n text with the price, the period, renewal and cancellation, the
  refund rule, the 3-year record and the contact.
- Telegram asks that users agree to the terms before buying, so the offer text says "By
  subscribing you accept the /terms".

**Alternatives considered**: storing support requests in a table. It adds retention work for
no need, since operators reply directly, so it was rejected (Principle II, VI).

## P12. Who may subscribe (FR-007)

Anyone in a private chat (Clarifications Q1): refused senders with no account, restricted
users, beta applicants and beta testers. A person with a Stars subscription in state `active`
or `canceled` gets the "manage" view instead of an offer. A beta tester, or someone with an
operator subscription, sees a line stating their current access before **Subscribe**.

## P13. Testing approach

- **Unit**:
  - payload parsing;
  - the pre-checkout decision table (every reason);
  - period math (first, renewal, late, out of order);
  - `policy.status` grace;
  - i18n parity for the new keys.
- **Integration** (real Postgres, `offline_bot`, `FrozenClock`, as in
  `tests/integration/test_access_*`):
  - offer → pre-checkout → `successful_payment` → access;
  - a duplicate update, and the `stars_reconcile` backfill from a fake `getStarTransactions`;
  - renewal with no message;
  - cancel/resume via the button with a fake `edit_user_star_subscription`;
  - `subscription` updates `canceled`, `active` and `failed`;
  - the expiry fallback;
  - CLI refund;
  - deletion cancelling the subscription and keeping the minimal record;
  - the purge after 3 years;
  - the gate allow-list.
- **Manual**: Telegram's test environment, where Stars are free. Run a bot token from
  @BotFather on the test server, with `SCRIBE_TELEGRAM_TEST_ENV=true`, which sets aiogram's
  `TelegramAPIServer` to the `/test/` endpoints. See quickstart.md.

## P14. Observability

Structured events, without ids beyond the internal user id and without amounts in plain
logs except `stars_payment_credited`, which needs amount and kind:

- `stars_offer_created`;
- `stars_precheckout` (`accepted`/`declined`, reason);
- `stars_payment_credited` (`first`/`renewal`/`backfill`, amount);
- `stars_payment_duplicate`;
- `stars_subscription_state` (from, to, by);
- `stars_refund` (ok/error);
- `stars_reconcile` (counts).

Health output gains `payments_enabled`. Message content is never logged (Principle II).
