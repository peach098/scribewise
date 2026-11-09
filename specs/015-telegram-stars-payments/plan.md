# Implementation Plan: Pay for a Subscription with Telegram Stars

**Branch**: `015-telegram-stars-payments` | **Date**: 2026-11-07 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/015-telegram-stars-payments/spec.md`

## Summary

People subscribe to the hosted Scribe inside the control bot with a recurring Telegram Stars
subscription. It costs `SCRIBE_STARS_PRICE` Stars and renews every 30 days until cancelled.

**Buying.**
- The **Subscription** button (specs/005) now shows an offer when payments are available.
- Each offer creates an invoice link with `createInvoiceLink(subscription_period=2592000)` and
  the payload `sub1:<offer id>`.
- A `pre_checkout_query` handler accepts or declines within 10 s using DB checks only: price,
  offer age under 1 h, payments on, no running subscription.
- Each `successful_payment` is credited exactly once, keyed by `telegram_payment_charge_id`. It
  becomes a `payments` row plus an access period in the existing `subscriptions` table
  (`source='stars'`), so the specs/005 gate, reconcile job and `/my_data` work unchanged.

**Renewal and cancellation.**
- Renewals arrive as more `successful_payment` messages. They are credited silently, with a
  1-hour grace while a renewal is expected.
- Cancel and resume go through `editUserStarSubscription` from `/account` (a new command) and
  from the 3-day reminder.
- Changes made inside Telegram arrive as `subscription` updates (`canceled`, `active`,
  `failed`).

**Restarts.** A `stars_reconcile` job reads `getStarTransactions` every 10 minutes and at
start, and credits anything polling lost.

**Support and operator tools.**
- New commands: `/paysupport` (forwarded to operators, not stored) and `/terms`.
- New `scribe.admin pay` commands: list, stats, show, refund, cancel, sync.

**Data deletion.** `/delete_my_data` first cancels renewal on Telegram, then deletes
everything except a minimal payment record, which is kept for 3 years (Clarifications Q3).

No new dependency, no LLM change.

## Technical Context

**Language/Version**: Python 3.12 (unchanged).

**Primary Dependencies**: unchanged.
- aiogram 3.31: `Router.pre_checkout_query`, `Router.subscription`,
  `Bot.create_invoice_link(subscription_period=...)`, `edit_user_star_subscription`,
  `refund_star_payment`, `get_star_transactions`.
- SQLAlchemy 2 async + Alembic, structlog.

**No new dependency.**

**Storage**: PostgreSQL 16. Migration `0022_stars_payments`:
- new tables `payment_offers`, `stars_subscriptions`, `payments`;
- new columns `subscriptions.source` and `subscriptions.payment_id`;
- six new enums and new `access_event_kind` values.

See [data-model.md](data-model.md).

**Testing**: pytest + pytest-asyncio. Telegram is reached only through a `StarsApi` protocol
with a fake in tests, plus `offline_bot` for dispatcher tests, real Postgres and `FrozenClock`.
For manual checks, Telegram's test environment gives free Stars ([quickstart.md](quickstart.md)).

**Target Platform**: Linux server (Docker), unchanged. The hosted instance runs on
`45.151.31.112`.

**Project Type**: single Python service (control bot, user clients, scheduler).

**Performance Goals**:
- The pre-checkout answer comes well under Telegram's 10 s limit: one transaction, no network
  call, 5 s internal timeout.
- The thanks message is sent within 10 s of payment (FR-006).
- The gate adds one indexed query for the set of renewing Stars periods.

**Constraints**:
- Credit exactly once.
- Never deliver goods without `successful_payment`.
- Nobody is charged after cancelling, a refund or data deletion: deletion aborts if the
  Telegram-side cancel fails.
- Subscription price 1–10000 Stars; period fixed at 30 days by Telegram.
- No message content in payment tables or logs.

**Scale/Scope**: tens of paying subscribers at first, a few hundred payment rows a year.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan complies |
|---|---|---|
| I. Human approval before sending | ✅ Pass | Nothing is sent on the user's behalf. Payment messages come from the control bot to the person themselves. Every payment action is attributable and logged: `access_events` with actor, and structured `stars_*` events. |
| II. Privacy & data minimization | ⚠️ Pass with exception | Payment tables hold ids, amounts, dates and Telegram's charge id, never content. `/paysupport` text is forwarded, not stored. Payments are shown in `/my_data`. **Exception**: after `/delete_my_data`, a minimal `payments` record (Telegram id, amount, dates, charge id, refund status) is kept for 3 years for refunds, disputes and tax (Clarifications Q3). The person is warned before deleting, `/terms` states it, and it is purged automatically. See Complexity Tracking. |
| III. Grounded replies | ➖ N/A | No change to drafting. |
| IV. Faithful personal style | ➖ N/A | No change. |
| V. Evaluation-driven development | ✅ Pass | No prompt, model or retrieval change, so no eval run. All code is deterministic and gets tests first: decision tables, period math, lifecycle, reconcile, CLI, deletion and the migration. |
| VI. Simplicity & observability | ✅ Pass | No new dependency or service. The specs/005 access tables, gate, reconcile job and admin CLI are reused, and a paid period is just another `subscriptions` row. Telegram sits behind a narrow `StarsApi` protocol. One new package, `scribe/payments`, and one new job. Structured `stars_*` events and `payments_enabled` in health output. |
| Security: untrusted input | ✅ Pass | The payload is bot-made and parsed strictly (`sub1:<uuid>`). The payer is taken from Telegram's `from_user`, never from the payload. `/paysupport` text is length-capped, sent as plain text and never interpreted. The gate allow-list is explicit. The `pay:*` buttons act only on the presser's own subscription. |
| Security: secrets | ✅ Pass | Stars need no provider token. The only secret stays the bot token. |

**Post-design re-check (after Phase 1)**: ✅ still passes, with the one recorded Principle II
exception. The design adds no other deviation.

## Project Structure

### Documentation (this feature)

```text
specs/015-telegram-stars-payments/
├── spec.md
├── plan.md                         # this file
├── research.md                     # P1–P14
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── control-chat.md             # offer, pre-checkout reasons, /account, reminders, /paysupport, /terms, gate allow-list
│   ├── operator-cli.md             # scribe.admin pay ...
│   └── internal-interfaces.md      # StarsApi, offers, checkout, credit, lifecycle, reconcile
├── checklists/requirements.md
└── tasks.md                        # /speckit-tasks
```

### Source Code (repository root)

```text
alembic/versions/
└── 0022_stars_payments.py              # NEW
src/scribe/
├── payments/                           # NEW package
│   ├── __init__.py
│   ├── telegram.py                     # StarsApi protocol, AiogramStarsApi, StarsApiError
│   ├── offers.py                       # make_offer, parse_payload
│   ├── checkout.py                     # precheckout_decision
│   ├── credit.py                       # credit_payment, apply_refund
│   ├── lifecycle.py                    # cancel/resume, on_subscription_update, end_due
│   ├── reconcile.py                    # stars_reconcile job
│   ├── repo.py                         # queries: running sub, payments for person/range, stats
│   ├── bot.py                          # router: pay:* callbacks, pre_checkout_query,
│   │                                   #   successful_payment, refunded_payment, subscription,
│   │                                   #   /account
│   └── support.py                      # /paysupport (PaySupportStates), /terms
├── access/
│   ├── policy.py                       # grace for renewing Stars periods
│   ├── repo.py                         # renewing set in access_status
│   ├── gate.py                         # payment_allows
│   ├── apply.py                        # Subscription → offer; button on pending/rejected reply
│   └── reconcile.py                    # end_due, Stars reminders, pay.ended_* notices
├── retention/
│   ├── user_data.py                    # cancel-before-delete, keep minimal payments, /my_data
│   └── purge.py                        # payments 1096 d, offers 7 d
├── telegram/control_bot/
│   ├── bot.py                          # include payments routers
│   ├── render.py                       # COMMAND_NAMES + account, paysupport, terms
│   └── account_command.py              # delete warning lines, abort on cancel failure
├── admin.py                            # pay subcommands; sub/access show source
├── config.py                           # payments_enabled, stars_price, telegram_test_env
├── db/models.py                        # tables, enums, Subscription.source/payment_id
├── i18n/en.py, i18n/ru.py              # pay.* keys (contracts/control-chat.md)
└── __main__.py                         # allowed_updates, StarsApi, stars_reconcile, test server
tests/
├── unit/test_stars_checkout.py         # NEW
├── unit/test_stars_periods.py          # NEW
├── unit/test_access_policy.py          # + grace
└── integration/
    ├── test_stars_payment_bot.py       # NEW
    ├── test_stars_lifecycle.py         # NEW
    ├── test_stars_reconcile.py         # NEW
    ├── test_stars_data.py              # NEW
    ├── test_admin_pay.py               # NEW
    └── test_access_gate_bot.py         # + allow-list
README.md, README.ru.md, docs/          # payments settings, /terms, /paysupport
.env.example                            # SCRIBE_PAYMENTS_ENABLED, SCRIBE_STARS_PRICE
```

**Structure Decision**: single project, existing layout. Payment logic gets its own
`scribe/payments` package. It is a new external integration with its own lifecycle, and
keeping it out of `access/` stops those modules (already ~1300 lines) from growing. `access/`
only learns that a `subscriptions` row may come from Stars and that renewing periods get
grace.

## Key Design Decisions

1. **Paid periods are ordinary access periods** (data-model `subscriptions.source`). The gate,
   suspension, reminders and `/my_data` from specs/005 keep working, and the new tables hold
   only the billing side.
2. **Exactly once, two inputs** (research P4): updates are credited live, and
   `getStarTransactions` fills gaps. Both go through `credit_payment`, keyed by the unique
   charge id.
3. **Telegram's expiry date is authoritative** for the period end. Our clock decides only the
   grace and the fallback expiry.
4. **The offer row is the anchor** (research P2): it holds the price check, the 1-hour validity
   and the link from renewals and `subscription` updates back to the Stars subscription.
5. **Telegram first, then the DB**, for cancel, resume, refund and the deletion cancel. If the
   Telegram call fails, nothing changes locally, and deletion is aborted, so nobody is charged
   for a subscription we think is cancelled.
6. **Request the update types** explicitly with `allowed_updates`. Without it, `subscription`
   updates may never arrive.
7. **Price in settings** for v1 (research P9): a restart applies it, and running subscriptions
   keep their Telegram-side price.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Principle II: "the user MUST be able to delete everything". A minimal `payments` record survives `/delete_my_data` for 3 years | The operator must be able to refund, answer disputes (Telegram makes the seller responsible for them) and account for income. The user chose this in Clarifications Q3 | Deleting payments with the account would leave the operator unable to check or refund a charge after deletion, and without accounting records. The kept record is the minimum (no content, no names), the person is warned, `/terms` states it, and an automatic purge enforces the limit |
