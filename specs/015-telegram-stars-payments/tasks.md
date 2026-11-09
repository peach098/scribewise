# Tasks: Pay for a Subscription with Telegram Stars

**Input**: Design documents from `/specs/015-telegram-stars-payments/`

**Prerequisites**: plan.md, spec.md, research.md (P1–P14), data-model.md, contracts/
(control-chat.md, operator-cli.md, internal-interfaces.md), quickstart.md. Base: `main` at
`472d940` (alembic head `0021_auto_send.py`).

**Tests**: Included. Constitution Principle V requires tests before or with deterministic code,
and SC-002/SC-003 (credit exactly once, never charge after a failed check) are money-safety
properties. Test file names match quickstart.md. There is no evaluation run, because draft
generation is unchanged.

**Organization**:
- **US1** (subscribe with Stars) is the MVP. It covers the offer, the pre-checkout check, the
  exactly-once credit and the `getStarTransactions` backfill, so no payment is ever lost.
- **US2** adds renewal, cancel/resume, `subscription` updates, `/account`, reminders and the
  end of a subscription.
- **US3** adds `/paysupport`, `/terms` and refunds.
- **US4** adds the operator's `pay` reports and the source shown in existing commands.
- **Data rights** (FR-019/FR-020) is a cross-cutting phase. It must ship before payments are
  turned on in production.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 (user story phases only)

---

## Phase 1: Setup

- [X] T001 Verify the base:
  - branch `015-telegram-stars-payments` is rebased on `origin/main`;
  - `uv run pytest -q` and `make lint typecheck` pass;
  - `uv.lock` pins aiogram `3.31.0` and `aiogram.Router` has `pre_checkout_query` and
    `subscription` observers;
  - this feature's migration is `alembic/versions/0022_stars_payments.py` with
    `revision = "0022"`, `down_revision = "0021"`.

  If a newer head exists on `main`, chain after it instead and rename the file.

---

## Phase 2: Foundational (blocking for all stories)

### Settings

- [X] T002 [P] Tests in `tests/unit/test_config.py`:
  - `payments_enabled` defaults to `False` and `stars_price` to `None`;
  - `SCRIBE_STARS_PRICE=0` and `=10001` fail validation ("`1..10000`", research P1);
  - `SCRIBE_STARS_PRICE=10000` passes;
  - `telegram_test_env` defaults to `False`;
  - the helper `payments_available(settings)` is `True` only when `access_gate`,
    `payments_enabled` and `stars_price` are all set (research P9).
- [X] T003 In `src/scribe/config.py`, in the access gate block:
  - add `payments_enabled: bool = False`;
  - add `stars_price: int | None = Field(default=None, ge=1, le=10000)`;
  - add `telegram_test_env: bool = False`, which makes the bot use Telegram's test server;
  - add the module-level `payments_available(settings) -> bool`, with a comment citing specs/015
    FR-021 and research P9.

  Add the three variables, commented, to `.env.example`. This makes T002 pass.

### Schema

- [X] T004 [P] Migration test `test_migration_stars_payments` in
  `tests/integration/test_models_and_repos.py`:
  - after upgrade, existing `subscriptions` rows have `source = 'operator'` and
    `payment_id IS NULL`;
  - `payment_offers.price_stars = 0` and `= 10001` violate the check "`price_stars BETWEEN 1
    AND 10000`";
  - a second `payments` row with the same `charge_id` violates the unique constraint;
  - `payments.amount_stars = 0` violates "`amount_stars > 0`";
  - `period_end <= period_start` violates "`period_end > period_start`";
  - two `stars_subscriptions` rows for one `telegram_id` in states `active` and `canceled`
    violate the partial unique index "`telegram_id WHERE state IN ('active','canceled')`",
    while `active` + `ended` is accepted;
  - deleting a `stars_subscriptions` row sets `payments.stars_subscription_id` to NULL;
  - deleting a `payments` row sets `subscriptions.payment_id` to NULL;
  - the new `access_event_kind` values are insertable;
  - downgrade drops the tables, columns and enums.
- [X] T005 In `src/scribe/db/models.py`, add per data-model.md:
  - **Enums**:
    - `StarsSubState` (`active`, `canceled`, `ended`);
    - `StarsCancelSource` (`user_bot`, `telegram`, `refund`, `deletion`, `operator`);
    - `StarsEndReason` (`canceled`, `failed`, `refunded`, `expired`);
    - `PaymentKind` (`first`, `renewal`);
    - `PaymentSource` (`update`, `backfill`);
    - `SubscriptionSource` (`operator`, `stars`).
  - **`AccessEventKind` values**: `payment_received`, `payment_renewed`, `payment_refunded`,
    `stars_canceled`, `stars_resumed`, `stars_failed`, `stars_ended`.
  - **`PaymentOffer`** (`payment_offers`): `id` uuid PK; `telegram_id` bigint not null indexed;
    `price_stars` int not null with check "`price_stars BETWEEN 1 AND 10000`"; `created_at`
    not null; `used_at` null.
  - **`StarsSubscription`** (`stars_subscriptions`):
    - `id`;
    - `telegram_id` indexed;
    - `offer_id` FK → `payment_offers.id`, not null, unique, `ON DELETE RESTRICT`;
    - `first_charge_id` text, not null, unique;
    - `price_stars`, `started_at`, `period_end`, `state`;
    - `canceled_at`, `canceled_by`, `ended_at`, `ended_reason`, all nullable;
    - timestamps;
    - partial unique index `uq_stars_subscriptions_running` on `telegram_id` "`WHERE state IN
      ('active','canceled')`".
  - **`Payment`** (`payments`):
    - `id`;
    - `telegram_id` indexed;
    - `charge_id` text, not null, unique;
    - `amount_stars` int, not null, with check "`amount_stars > 0`";
    - `kind`, `paid_at` (indexed), `period_start`, `period_end`, with check "`period_end >
      period_start`";
    - `stars_subscription_id` FK, `ON DELETE SET NULL`;
    - `credited_via`;
    - `refunded_at`, `refunded_by`, `owner_deleted_at`, all nullable;
    - `created_at`.
  - **On `Subscription`**: `source` (`NOT NULL DEFAULT 'operator'`), and `payment_id` (FK →
    `payments.id`, nullable, unique, `ON DELETE SET NULL`).

  Comments cite specs/015 FR numbers.
- [X] T006 Write `alembic/versions/0022_stars_payments.py`:
  - create the six pg enums;
  - add the `access_event_kind` values in `op.get_context().autocommit_block()` with
    `ADD VALUE IF NOT EXISTS` (pattern of `0006_beta_applications.py`);
  - create the three tables with their checks and indexes;
  - add the two `subscriptions` columns.

  Downgrade deletes `access_events` rows with the new kinds, then drops the columns, tables and
  enums. This makes T004 pass and depends on T005.

### Telegram seam and repo

- [X] T007 [P] Create `src/scribe/payments/__init__.py` and `src/scribe/payments/telegram.py`
  per contracts/internal-interfaces.md "StarsApi":
  - the `StarsApi` Protocol (`create_subscription_link`, `set_renewal`, `refund`,
    `incoming_transactions`);
  - the frozen dataclass `IncomingStars` (`charge_id`, `telegram_id`, `amount`, `payload`,
    `date`, `subscription_period: int | None`, `is_refund: bool`);
  - `StarsApiError`;
  - `AiogramStarsApi(bot)`:
    - `create_invoice_link(title, description, payload, currency="XTR", prices=[LabeledPrice(
      label, price)], subscription_period=2592000)`, with no provider token;
    - `edit_user_star_subscription`;
    - `refund_star_payment`;
    - `get_star_transactions` paged by `offset`/`limit=100` until older than `since`, mapping
      `TransactionPartnerUser` sources to `IncomingStars` and refunds (`receiver` is a user) to
      `is_refund=True`;
    - every `TelegramAPIError` is wrapped as `StarsApiError`.

  Constant `SUBSCRIPTION_PERIOD_S = 2592000`.
- [X] T008 [P] Create `tests/payments_fakes.py` with `FakeStarsApi`, which records calls,
  returns `https://t.me/$fake-<payload>` links, can be told to raise `StarsApiError` per method,
  and serves a settable list of `IncomingStars`. Every payments test uses it.
- [X] T009 [P] Unit tests `tests/unit/test_stars_offers.py` for `parse_payload`:
  - `sub1:<uuid>` returns the uuid;
  - `sub1:`, `sub1:not-a-uuid`, `sub2:<uuid>`, an empty string and a 129-byte string return
    `None`;
  - `make_payload(uuid)` round-trips and is at most 128 bytes (research P2).
- [X] T010 Create `src/scribe/payments/repo.py` with these queries:
  - `running_for(session, telegram_id)`: the `active` or `canceled` `StarsSubscription`, or
    `None`;
  - `by_offer(session, offer_id)`;
  - `renewing_payment_ids(session, telegram_id)`: the ids of `stars` access periods whose Stars
    subscription is `active`, and `renewing_payment_ids_bulk(session, telegram_ids)` for
    `bulk_status`;
  - `payments_for(session, telegram_id, since=None)`;
  - `payments_in_range(session, start, end)`;
  - `stats(session, start, end)`, returning counts and sums per contracts/operator-cli.md
    `pay stats`.

  Depends on T005.

### Access policy grace

- [X] T011 [P] Tests in `tests/unit/test_access_policy.py`:
  - a `stars` subscription row with `ends_at = T` whose `payment_id` is in
    `renewing_payment_ids` is active at `T + 59 min` and not at `T + 61 min` (`STARS_GRACE =
    1 h`, research P5);
  - the same row not in the set is not active at `T + 1 min`;
  - an `operator` row never gets grace;
  - `AccessStatus.until` stays the real `ends_at`, not `ends_at + grace`.
- [X] T012 In `src/scribe/access/policy.py`:
  - add `STARS_GRACE = timedelta(hours=1)`;
  - give `subscription_active` and `status` an optional `renewing_payment_ids: frozenset[
    uuid.UUID] = frozenset()` parameter;
  - in `src/scribe/access/repo.py`, make `access_status` and `bulk_status` load that set with
    `payments.repo.renewing_payment_ids` (one indexed query) and pass it on.

  This makes T011 pass and depends on T010.

### Polling and gate plumbing

- [X] T013 In `src/scribe/__main__.py`:
  - pass `allowed_updates=self.dispatcher.resolve_used_update_types()` to `start_polling`
    (research P5);
  - when `settings.telegram_test_env` is set, build the `Bot` with
    `session=AiohttpSession(api=TelegramAPIServer(base="https://api.telegram.org/bot{token}/test/{method}",
    file="https://api.telegram.org/file/bot{token}/test/{path}"))`. Do it in `create_bot` in
    `src/scribe/telegram/control_bot/bot.py` so `__main__` and `admin.py` both get it;
  - create one `AiogramStarsApi(bot)` and add it to the dispatcher workflow data as
    `stars_api`.
- [X] T014 [P] Tests in `tests/integration/test_access_gate_bot.py` with the gate on and a
  sender with no access and no account. Each of these reaches its handler and does not bump
  `refused_contacts.attempts`:
  - a message with `successful_payment`;
  - a message with `refunded_payment`;
  - callback `pay:offer`;
  - `/paysupport`;
  - `/terms`;
  - a text message while in `PaySupportStates.describe`.

  A plain text message from the same sender is still refused. Use stub handlers registered
  only in the test.
- [X] T015 In `src/scribe/access/gate.py`, add `payment_allows(event, state)` and `PAY_PREFIX =
  "pay:"`. It allows messages whose `successful_payment` or `refunded_payment` is set, `pay:`
  callbacks, the commands `paysupport` and `terms`, and any message (`/cancel` included) while
  in `PaySupportStates`. Check it right after `apply_allows` in `AccessGateMiddleware.__call__`
  and pass the update through without `touch_refused`. Define `PaySupportStates`
  (`describe`) in a new `src/scribe/payments/states.py` so the gate does not import the
  routers. Update the module docstring. This makes T014 pass.

**Checkpoint**: settings, schema, Stars seam, grace and gate allow-list are ready. Existing tests
are still green with payments off.

---

## Phase 3: User Story 1 - Subscribe with Stars (Priority: P1) 🎯 MVP

**Goal**: anyone without access presses **Subscription**, pays in Stars and gets access at
once. A bad or outdated offer is declined before any charge. Every payment is credited
exactly once, even across a restart.

**Independent Test**: quickstart "Manual" steps 2–4 and 8;
`uv run pytest tests/unit/test_stars_checkout.py tests/unit/test_stars_periods.py
tests/integration/test_stars_payment_bot.py tests/integration/test_stars_reconcile.py`.

### Tests for User Story 1

- [X] T016 [P] [US1] Unit tests `tests/unit/test_stars_checkout.py` (table-driven, plain rows
  and `FrozenClock`) for `precheckout_decision`, per research P3.

  Accept cases:
  - a fresh offer (`created_at` 59 min ago), the matching `total_amount`, `currency="XTR"`,
    payments available and no running subscription → `Accept`;
  - a payer id different from the offer's `telegram_id` → still `Accept`;
  - renewal path: the offer is linked to an `active` or `canceled` Stars subscription of the
    payer → `Accept`
    even with payments off, an old offer or a changed price.

  Decline cases, one reason each:
  - `currency="USD"` → `error`;
  - unknown payload → `expired`;
  - unknown offer id → `expired`;
  - `created_at` 61 min ago → `expired`;
  - `total_amount` ≠ the offer price → `price`;
  - the offer price ≠ the current `stars_price` → `price`;
  - `payments_enabled=False` → `off`;
  - the payer has a `canceled` (still running) subscription → `running`;
  - the offer is linked to a subscription of another payer → `running`.

  Every `DeclineReason` member is covered (SC-003).
- [X] T017 [P] [US1] Unit tests `tests/unit/test_stars_periods.py` for the pure helper
  `first_period(paid_at, expires_at)` and `next_period(prev_end, paid_at, expires_at)`:
  - first payment → `[paid_at, expires_at)`;
  - a `None` expiry (backfill) → `paid_at + 30 days`;
  - a renewal → `[max(paid_at, prev_end), expires_at)`;
  - a renewal confirmed 20 min after `prev_end` still starts at `paid_at` and ends at Telegram's
    expiry;
  - an `expires_at <= start` raises `ValueError`.
- [X] T018 [P] [US1] Integration tests `tests/integration/test_stars_payment_bot.py`, which drive
  the dispatcher with `offline_bot()`, `FakeStarsApi` as `stars_api`, the gate on and payments
  available (`stars_price=150`):
  1. A sender with no account presses `apply:subinfo` and gets `pay.offer` with one URL button
     whose text contains `150 ⭐`. A `payment_offers` row exists with `price_stars=150`, and the
     payload passed to `create_subscription_link` is `sub1:<offer id>`.
  2. With payments off, the same button gives `access.subscription_placeholder` and creates no
     row.
  3. A `pre_checkout_query` update is answered `ok=True`. An outdated one is answered
     `ok=False` with the `pay.declined_expired` text in the sender's language.
  4. A `successful_payment` message (`is_first_recurring=True`, `subscription_expiration_date`
     = +30 d, unique charge id) creates:
     - `payments` (`kind='first'`, `credited_via='update'`);
     - `stars_subscriptions` (`state='active'`, `first_charge_id`, `period_end` = expiry);
     - a `subscriptions` row (`source='stars'`, `granted_by='stars'`, `payment_id`);
     - `access_events` `payment_received`;
     - offer `used_at`.

     The bot sends `pay.thanks` plus `pay.thanks_start`, and the sender's next `/start` begins
     setup.
  5. The same `successful_payment` delivered again creates nothing and sends nothing.
  6. A suspended user (account, access ended) who pays gets `pay.thanks`, and
     `access_reconcile` then restores them.
  7. A beta tester sees `pay.offer_has_beta` in the offer.
  8. A person with an active Stars subscription pressing **Subscription** gets the manage view,
     not a new offer.
  9. A person with a pending application, and one rejected 3 days ago, see the **Subscription**
     button in the application-status reply when payments are available, and no button when
     they are off (US1 scenario 5).
- [X] T019 [P] [US1] Integration tests `tests/integration/test_stars_reconcile.py` for the
  `stars_reconcile` job with `FakeStarsApi.incoming`:
  - a first payment missing locally is credited with `credited_via='backfill'`, its period
    ends at `date + subscription_period`, and the person gets `pay.thanks`;
  - a renewal missing locally is credited without a message;
  - a transaction already credited is skipped;
  - a payload that is not ours, or a transaction from a non-user partner, is ignored;
  - a second run changes nothing;
  - with payments off and no running Stars subscription, the job does nothing and makes no API
    call;
  - a `StarsApiError` is logged and the job finishes.

### Implementation for User Story 1

- [X] T020 [US1] Create `src/scribe/payments/offers.py`:
  - `make_payload`, `parse_payload`;
  - `make_offer(session, api, settings, telegram_id, *, lang, now) -> Offer | Running |
    Unavailable` (contracts/internal-interfaces.md). The link title and description are the
    i18n `pay.invoice_title` / `pay.invoice_description` in `lang`, with the price label
    `pay.invoice_label`;
  - `offer_view(...)`, which renders `pay.offer` plus the optional `pay.offer_has_beta` /
    `pay.offer_has_sub` line and the URL button `pay.btn_subscribe` (contracts/control-chat.md
    "Offer").

  This makes T009 pass and depends on T007 and T010.
- [X] T021 [US1] Create `src/scribe/payments/checkout.py` with `DeclineReason` (`off`, `price`,
  `expired`, `running`, `error`), `Accept`, `Decline` and `precheckout_decision(...)` per
  research P3, plus `OFFER_TTL = timedelta(hours=1)`. This makes T016 pass.
- [X] T022 [US1] Create `src/scribe/payments/credit.py`:
  - `first_period` / `next_period`, which make T017 pass;
  - `credit_payment(...)` per research P4 and contracts/internal-interfaces.md:
    - insert `payments` with `ON CONFLICT (charge_id) DO NOTHING` → `AlreadyCredited`;
    - first payment: create `StarsSubscription` and set the offer's `used_at`;
    - renewal: return `Unmatched` for now (T029 adds the renewal path);
    - insert the `Subscription` access period;
    - write `payment_received` / `payment_renewed` with meta `{charge_id, amount}`;
    - an unknown payload is `Unmatched`, still recorded with a 30-day period and
      `stars_subscription_id=NULL`, and logged as `stars_payment_unmatched`.

  The caller commits.
- [X] T023 [US1] Create `src/scribe/payments/bot.py` with `create_router()`, which registers:
  - `pre_checkout_query`: call `precheckout_decision` under `asyncio.wait_for(..., 5)` and
    answer `answer_pre_checkout_query(ok, error_message=t(f"pay.declined_{reason}", lang))`,
    where `lang` comes from the stored user or `from_user.language_code`. Log
    `stars_precheckout`.
  - `F.successful_payment`: call `credit_payment(via=update)`, commit, then send `pay.thanks`
    (+ `pay.thanks_start` when the payer has no `users` row) for a first payment only. Log
    `stars_payment_credited`.
  - Callback `pay:offer`: same as `apply:subinfo`.

  Include it in `src/scribe/telegram/control_bot/bot.py` right after `apply.create_router()`.
  Depends on T020–T022.
- [X] T024 [US1] In `src/scribe/access/apply.py`:
  - `subscription_callback` calls `offers.make_offer` when `payments_available(settings)`. It
    renders the offer, the manage view (`pay.account` for a running subscription) or, when
    unavailable, today's placeholder;
  - `no_access_reply` / `block_text` add the **Subscription** button (`apply:subinfo`) to the
    pending and rejected texts only when payments are available. This needs a `settings`
    argument: update the call in `gate.py`.

  This makes T018 scenarios 1–2 and 7–9 pass.
- [X] T025 [US1] Create `src/scribe/payments/reconcile.py` with `STARS_RECONCILE_JOB =
  "stars_reconcile"`, `Every(timedelta(minutes=10))`, `run_at_start=True`, and
  `register_stars_reconcile(scheduler, session_factory, settings, api, bot)`. It reads
  `incoming_transactions(since=now-7d)`, credits through `credit_payment(via=backfill)` and
  sends `pay.thanks` for backfilled first payments. Refund entries are left to T038. Log
  `stars_reconcile` counts. Register it in `src/scribe/__main__.py` next to
  `register_access_reconcile`. This makes T019 pass, except its renewal case, which T029 completes.
- [X] T026 [US1] Add the US1 i18n keys from contracts/control-chat.md "Offer", "Pre-checkout
  decline reasons" and "After payment" (first-payment rows) to `src/scribe/i18n/en.py` and
  `src/scribe/i18n/ru.py`:
  - `pay.offer`, `pay.offer_has_beta`, `pay.offer_has_sub`, `pay.btn_subscribe`;
  - `pay.invoice_title`, `pay.invoice_description`, `pay.invoice_label`;
  - `pay.declined_off|price|expired|running|error`;
  - `pay.thanks`, `pay.thanks_start`.

  Placeholders `{price}` and `{date}` are the same in both languages, and pre-checkout
  `error_message` texts are at most 255 characters.
  `uv run pytest tests/unit/test_i18n.py tests/unit/test_i18n_lint.py` passes.

**Checkpoint**: US1 is complete. A person can pay and is served, nothing is credited twice and
nothing is lost. Quickstart manual steps 3, 4 and 8 pass on the test server.

---

## Phase 4: User Story 2 - Automatic renewal and cancellation (Priority: P1)

**Goal**: renewals extend access silently. Subscribers cancel or resume from `/account` or from
the reminder. Changes made inside Telegram are reflected. An ended subscription ends access
with a clear reason.

**Independent Test**: quickstart "Manual" steps 5–6;
`uv run pytest tests/integration/test_stars_lifecycle.py`.

### Tests for User Story 2

- [X] T027 [P] [US2] Integration tests `tests/integration/test_stars_lifecycle.py` (offline bot,
  `FakeStarsApi`, `FrozenClock`).

  **Renewal**: a `successful_payment` with `is_recurring=True`, `is_first_recurring=False` and
  the same payload extends `period_end` and adds a second `stars` access period and a
  `payment_renewed` event, with no message to the person (FR-006). The person's access never
  lapses across the boundary (SC-004).

  **Cancel and resume in the bot**:
  - `/account` for an active subscriber shows `pay.account` with "renews on" and the
    **Cancel subscription** button.
  - `pay:cancel` asks `pay.cancel_confirm`. `pay:cancel:yes` calls
    `set_renewal(canceled=True, first_charge_id=...)`, sets `state='canceled'` and
    `canceled_by='user_bot'`, writes `stars_canceled` and answers `pay.canceled`.
  - `pay:resume` before `period_end` calls `set_renewal(canceled=False)` and sets `state='active'`
    with `stars_resumed`.
  - `pay:resume` after `period_end` answers `pay.stale`.
  - A `StarsApiError` on cancel answers `pay.try_again` and leaves the row unchanged.

  **`subscription` updates** (`BotSubscriptionUpdated`):
  - `canceled` → `canceled`, with `canceled_by='telegram'`;
  - `active` → `active`;
  - `failed` → `ended` at `period_end`, with `ended_reason='failed'`;
  - an unknown payload or a payer mismatch is ignored.

  **Ending** (`end_due` via `access_reconcile`):
  - `canceled` past `period_end` → `ended` (`canceled`), access lost, and the notice is
    `pay.ended_canceled` with **Subscription**, not `access.ended_notice`;
  - `active` with no renewal by `period_end + 61 min` → `ended` (`expired`);
  - at `+59 min` it is still active and gets no notice;
  - `failed` → `pay.ended_failed`;
  - `expired` → `pay.ended_expired`;
  - access lost through a refund sends no ended notice (the person already got
    `pay.refunded`).

  **Reminders 3 days ahead**:
  - active → `pay.renew_reminder` with `pay:cancel`;
  - canceled → `pay.end_reminder` with `pay:resume`;
  - an operator subscription with payments available → `access.sub_reminder` whose renew text
    is `pay.renew_with_stars`, with the **Subscription** button;
  - each Stars period is reminded once.

  **Late renewal**: a subscription set to `ended (expired)` by `end_due` receives a renewal
  `successful_payment` (or a backfilled one) with the same payload → it is `active` again with
  the new `period_end`, the person regains access, and gets no second "ended" notice.

  **Restarting after the end**: a person whose subscription ended pays again → a new
  `stars_subscriptions` row and service resumes (US2 scenario 7).

### Implementation for User Story 2

- [X] T028 [US2] Create `src/scribe/payments/lifecycle.py` per contracts/internal-interfaces.md:
  - `cancel_renewal`, `resume_renewal`: Telegram first, then the row and the `access_events`
    row;
  - `on_subscription_update`;
  - `end_due(session, *, now) -> list[EndedStars]`: `canceled` with `period_end <= now`, and
    `active` with `period_end + STARS_GRACE <= now`, set to `ended` with `ended_at` and
    `ended_reason`, writing `stars_ended`.

  Depends on T010 and T022.
- [X] T029 [US2] Add the renewal path to `credit_payment` in `src/scribe/payments/credit.py`:
  for `is_first=False`, find the Stars subscription with `repo.by_offer` + payer, take the
  period from `next_period(prev_end=stars.period_end, ...)`, move `period_end`, insert the
  `stars` access period and write `payment_renewed`. If that Stars subscription is `ended` with `ended_reason` `expired`
  or `failed`, set it back to `active` (clear `ended_at`/`ended_reason`) and credit the renewal:
  Telegram has charged the person (data-model.md "State transitions"). Also make `stars_reconcile` (T025) send no
  message for backfilled renewals. A renewal never sends a message. This makes T027 "Renewal"
  and the T019 renewal case pass.
- [X] T030 [US2] In `src/scribe/payments/bot.py`, register:
  - `subscription` updates → `on_subscription_update`;
  - callbacks `pay:cancel`, `pay:cancel:yes`, `pay:cancel:no` and `pay:resume`. Each acts only
    on the presser's own running subscription, and anything else answers `pay.stale`;
  - `/account` and the `pay:*` callbacks work by `telegram_id` for a payer who has no `users`
    row (paid without `/start`, Clarifications Q1);
  - the `/account` command, which renders `pay.account` with the existing
    `account_command.access_lines` and the Stars line and buttons per contracts/control-chat.md
    "/account".
- [X] T031 [US2] In `src/scribe/access/gate.py`, add `account` to `RESTRICTED_COMMANDS`, so a
  user whose access ended still sees `/account`. In `src/scribe/telegram/control_bot/render.py`,
  add `account` to `COMMAND_NAMES` after `settings`. Add `cmd.account` to both i18n files.
- [X] T032 [US2] In `src/scribe/access/reconcile.py`:
  - call `lifecycle.end_due` at the start of each run, before access is computed;
  - when the last access source of a lost user was a Stars subscription, send
    `pay.ended_canceled`, `pay.ended_failed` or `pay.ended_expired` (with the **Subscription**
    button `pay:offer`) instead of `access.ended_notice`, and send nothing when the reason is
    `refunded` (`pay.refunded` was already sent);
  - split the `ending` branch per research P6. Stars `active` → `pay.renew_reminder` +
    `pay:cancel`. Stars `canceled` → `pay.end_reminder` + `pay:resume`. Operator rows keep
    `access.sub_reminder`, with `renew = Msg("pay.renew_with_stars")` and a `pay:offer` button
    when `payments_available(settings)`. A renewing Stars period is reminded even though renewal
    will extend access.

  Update the module docstring. Depends on T028.
- [X] T033 [US2] Add the US2 i18n keys to `src/scribe/i18n/en.py` and `ru.py`:
  - `pay.account` and its line variants;
  - `pay.btn_cancel`, `pay.btn_resume`, `pay.btn_subscription`;
  - `pay.cancel_confirm`, `pay.btn_cancel_yes`, `pay.btn_cancel_no`;
  - `pay.canceled`, `pay.resumed`, `pay.try_again`, `pay.stale`;
  - `pay.renew_reminder`, `pay.end_reminder`, `pay.renew_with_stars`;
  - `pay.ended_canceled`, `pay.ended_failed`, `pay.ended_expired`;
  - `cmd.account`.

  The i18n tests pass, and so does T027.

**Checkpoint**: US1 + US2 are the full paid product. Quickstart manual steps 5–6 pass.

---

## Phase 5: User Story 3 - Payment help and refunds (Priority: P2)

**Goal**: `/paysupport` reaches the operator. `/terms` states the rules. The operator refunds
a payment in one command, and the person loses exactly what that payment bought.

**Independent Test**: quickstart "Manual" step 7;
`uv run pytest tests/integration/test_stars_payment_bot.py -k "support or terms"
tests/integration/test_admin_pay.py -k refund`.

### Tests for User Story 3

- [X] T034 [P] [US3] Tests in `tests/integration/test_stars_payment_bot.py`.

  **`/paysupport`**:
  - From anyone, it answers `pay.support_intro` and sets `PaySupportStates.describe`.
  - A 1–1000-character text is sent to every operator id as `pay.support_operator`, with
    `parse_mode=None`, containing `id <telegram_id>`, the charge ids from the last 90 days (or
    "none") and the text verbatim (a `<b>` stays literal). The person gets `pay.support_sent`.
  - No table gains a row containing the text.
  - A 1001-character text or a photo gets the request again. `/cancel` leaves.
  - A second request within 10 minutes gets `pay.support_wait`.
  - With no operator configured, the person gets `pay.support_contact`.

  **`/terms`**: from anyone (gate on), it shows `pay.terms` with the price, or the "prices not
  set" variant when payments are not available.
- [X] T035 [P] [US3] Tests in `tests/integration/test_admin_pay.py` for `pay refund <charge_id>
  [--note]`:
  - In order, it calls `set_renewal(canceled=True)` and then `refund(...)`.
  - It sets `payments.refunded_at` and `refunded_by='operator:<note or cli>'`, and sets
    `cancelled_at` on that payment's access period.
  - It sets the Stars subscription to `ended` with `ended_reason='refunded'` and
    `canceled_by='refund'`, and writes `payment_refunded`.
  - The person gets `pay.refunded`, the output is `refunded <charge_id> <amount>⭐ to <id>`, and
    the exit code is 0.
  - `access_reconcile` then suspends the user if nothing else gives access.
  - A refund of an older (renewal) period of a still-running subscription also ends the
    subscription.
  - Exit code 1 with nothing changed for: an unknown charge id, an already refunded payment,
    and a `StarsApiError` from `refund`.
  - A `StarsApiError` from `set_renewal` is printed as a warning and the refund continues.
- [X] T036 [P] [US3] Tests in `tests/integration/test_stars_payment_bot.py` and
  `tests/integration/test_stars_reconcile.py`: a `refunded_payment` service message, and a
  refund transaction seen by `stars_reconcile`, both call `apply_refund(by="telegram")`. Each is
  applied once, and a second delivery changes nothing.

### Implementation for User Story 3

- [X] T037 [US3] Create `src/scribe/payments/support.py` with `create_router()`:
  - `/paysupport` → intro and `PaySupportStates.describe`; the 10-minute cooldown is stored as
    `last_support_at` in the FSM data;
  - text input validation (1–1000 characters) → forward to each id in
    `settings.operator_telegram_ids` via `bot.send_message(..., parse_mode=None)`. Charge ids
    come from `repo.payments_for(since=now-90d)`. Then clear the state;
  - `/cancel` in the state;
  - `/terms` → `pay.terms`, rendered with `stars_price` when payments are available.

  Nothing is stored except the FSM state. Include the router in
  `src/scribe/telegram/control_bot/bot.py` next to the payments router. This makes T034 pass.
- [X] T038 [US3] Add `apply_refund(session, *, charge_id, by, now)` to
  `src/scribe/payments/credit.py` per research P8. It is idempotent and returns `Refunded |
  AlreadyRefunded | NotFound`. In `src/scribe/payments/bot.py`, register `F.refunded_payment`
  → `apply_refund(by="telegram")` + commit + `pay.refunded`. In
  `src/scribe/payments/reconcile.py`, apply `IncomingStars.is_refund` entries the same way.
  This makes T036 pass.
- [X] T039 [US3] In `src/scribe/admin.py`:
  - add the `pay` subparser with `refund <charge_id> [--note]` per contracts/operator-cli.md;
  - extend the `bot` creation in `main` to `pay refund|cancel|sync`;
  - implement `pay_refund`: `AiogramStarsApi(bot)`, then `set_renewal(canceled=True)` (warn on
    error), then `refund` (exit 1 on error, nothing changed), then `apply_refund(by=
    "operator:<note or cli>")` + commit, then notify `pay.refunded`.

  This makes T035 pass.
- [X] T040 [US3] In `src/scribe/telegram/control_bot/render.py`, add `paysupport` and `terms` to
  `COMMAND_NAMES` (after `my_data`/`delete_my_data`). Add to both i18n files:
  - `cmd.paysupport`, `cmd.terms`;
  - `pay.support_intro`, `pay.support_operator`, `pay.support_sent`, `pay.support_wait`,
    `pay.support_contact`, `pay.support_again`;
  - `pay.terms` and `pay.terms_no_price`;
  - `pay.refunded`.

  `pay.terms` must contain the 3-year minimal-record sentence and "renews automatically until
  cancelled" (contracts/control-chat.md "/terms"). The i18n tests pass.

**Checkpoint**: Telegram's payment requirements (`/paysupport`, `/terms`, refunds) are met.

---

## Phase 6: User Story 4 - The operator sets the price and sees the money (Priority: P2)

**Goal**: the operator sees every payment, totals and subscription counts, and existing
access commands tell Stars subscriptions from operator grants.

**Independent Test**: quickstart US4 rows; `uv run pytest tests/integration/test_admin_pay.py
tests/integration/test_admin_access.py`.

### Tests for User Story 4

- [X] T041 [P] [US4] Tests in `tests/integration/test_admin_pay.py`:
  - `pay list` (default last 30 days, `--from/--to`, `--who`) prints one line per payment in
    the contract format, newest first, including `[refunded …]` and `[deleted owner]`;
  - `pay stats` totals match two payments and one refund (count, first/renewal split, received,
    refunded, net) plus active / cancelled-running / ended counts (SC-007);
  - `pay show <who>` prints the subscription and its payments;
  - `pay cancel <who>` calls `set_renewal(canceled=True)` and sets `canceled_by='operator'`,
    and exits 1 when nothing is running;
  - `pay sync --days 3` runs the reconcile once and prints the counts;
  - none of the output contains a username or text beyond the contract.
- [X] T042 [P] [US4] Tests in `tests/integration/test_admin_access.py`:
  - `sub list` and `access show` print `stars` / `operator` per row;
  - `sub extend` and `sub end` leave `stars` rows untouched and print the hint when a Stars
    subscription is running;
  - `access stats` prints `paying subscribers: N`.
- [X] T043 [P] [US4] Test in `tests/integration/test_stars_payment_bot.py`: after
  `stars_price` changes from 150 to 200 (a new `Settings`), a new offer shows 200. A
  `pre_checkout_query` for an older 150 offer is declined `price`. A renewal of a subscription
  that started at 150 is credited at 150 (FR-015).

### Implementation for User Story 4

- [X] T044 [US4] In `src/scribe/admin.py`, add `pay list`, `pay stats`, `pay show`, `pay cancel`
  (via `lifecycle.cancel_renewal(by=operator)`) and `pay sync` (runs the T025 job body once with
  `since=now-days`). Use `payments.repo` queries. The output follows contracts/operator-cli.md
  exactly. This makes T041 pass.
- [X] T045 [US4] In `src/scribe/admin.py`:
  - `sub_list` and `access show` print `Subscription.source`;
  - `sub_extend` and `sub_end` filter `source == operator` and print the hint;
  - `access stats` counts distinct `telegram_id` with a running Stars subscription.

  This makes T042 pass, and T043 passes with no extra code if T021 compares against the
  current `stars_price`.

**Checkpoint**: all four stories work.

---

## Phase 7: Data rights (cross-cutting, FR-019, FR-020; required before production)

- [X] T046 [P] Integration tests `tests/integration/test_stars_data.py`.

  **`/my_data`** lists the payments (`{date} {amount} {first|renewal} [refunded {date}]`) and
  the Stars subscription state.

  **`/delete_my_data`**:
  - With a running Stars subscription, the warning contains `pay.delete_warning_running`.
    With any payment, it contains `pay.delete_warning_record`.
  - On confirmation, `set_renewal(canceled=True)` is called **before** any row is deleted.
  - If that call raises `StarsApiError`, the bot answers `pay.delete_cancel_failed` and every
    row is still present.
  - On success:
    - `payment_offers`, `stars_subscriptions` and `subscriptions` of the person are gone;
    - `payments` rows remain with `owner_deleted_at` set, `stars_subscription_id IS NULL`, and
      the same `charge_id`, `amount_stars`, `paid_at` and refund columns;
    - `pay list` shows `[deleted owner]`;
    - `pay refund` still works on such a row.

  **Purge**:
  - a payment with `owner_deleted_at` 1097 days ago is deleted, one with `owner_deleted_at`
    1095 days ago is kept, and one with `owner_deleted_at IS NULL` and `paid_at` 5 years ago is
    kept;
  - an offer 8 days old with no subscription is deleted, and one 6 days old is kept;
  - an `ended` Stars subscription with no payments left is deleted.
- [X] T047 In `src/scribe/retention/user_data.py`:
  - add Stars rows to the access part of `DataSummary` and `/my_data` (`access_lines` in
    `src/scribe/telegram/control_bot/account_command.py`);
  - in the deletion path, before deleting the person's `subscriptions`, set
    `payments.owner_deleted_at = now` and delete `stars_subscriptions` and `payment_offers` by
    `telegram_id`. Do not add `Payment` to `PER_USER_TABLES`.

  Depends on T005.
- [X] T048 In `src/scribe/telegram/control_bot/account_command.py`:
  - `access_warning` adds `pay.delete_warning_running` / `pay.delete_warning_record`;
  - `delete_confirm_input` calls `lifecycle.cancel_renewal(by=deletion)` with `stars_api` before
    `delete_user_data`. On `StarsApiError` it replies `pay.delete_cancel_failed`, keeps the
    confirmation state cleared and deletes nothing.

  Add the three i18n keys to both languages. This makes the T046 deletion part pass.
- [X] T049 In `src/scribe/retention/purge.py`, add `PAYMENT_RETENTION = timedelta(days=1096)`,
  `OFFER_RETENTION = timedelta(days=7)` and the three deletes from data-model.md "Purge". Update
  the module docstring. This makes the T046 purge part pass.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T050 [P] In `src/scribe/observability/health.py` and its wiring in
  `src/scribe/__main__.py`, add `payments_enabled` to the `/healthz` JSON. Update its test in
  `tests/unit/` or `tests/integration/` (wherever `access_gate` in health is tested).
- [X] T051 [P] Docs:
  - `docs/configuration.md` — `SCRIBE_PAYMENTS_ENABLED`, `SCRIBE_STARS_PRICE`,
    `SCRIBE_TELEGRAM_TEST_ENV`;
  - `docs/access-gate.md` — a Stars section: how it plugs into access, refunds and the `pay`
    CLI;
  - `docs/commands.md` — `/account`, `/paysupport`, `/terms`;
  - `docs/privacy.md` — the 3-year minimal payment record (the Principle II exception);
  - `README.md` and `README.ru.md` — one line each, in English and Russian. Self-hosted stays
    free with payments off by default.
- [X] T052 [P] Add release notes for the next version in `release-notes/` (same format as
  `release-notes/0.5.0.md`): subscription in Stars, `/account`, `/paysupport`, `/terms`.
  `uv run python -m scribe.admin release-notes check` passes.
- [X] T053 Structured logs review: grep the new modules for `log.` calls. Check that they only
  emit the events in research P14, that no log carries a username, support text or invoice
  link, and that amounts appear only in `stars_payment_credited` and `stars_refund`.
- [ ] T054 Run `uv run pytest -q` and `make lint typecheck`. Check that with
  `payments_enabled=False` all pre-existing access tests pass unchanged (FR-021). Then run the
  quickstart "Manual: Telegram test environment" steps 1–8 and record the results in the PR
  description. That includes whether Telegram sent a `pre_checkout_query` for a renewal, if one
  was observed (research P3).

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)** → **Foundational (Phase 2)** → user stories.
- **US1 (Phase 3)** depends on Phase 2.
- **US2 (Phase 4)** depends on US1: renewals and the manage view need a first payment and
  `credit_payment`.
- **US3 (Phase 5)** depends on US1 (payments to refund). Its `/paysupport` and `/terms` parts
  (T034, T037, T040) only need Phase 2 and can start in parallel with US1.
- **US4 (Phase 6)** depends on US1. `pay cancel` (part of T044) depends on T028 (US2).
- **Data rights (Phase 7)** depends on US1 and on T028 (cancel before delete). It must be done
  before payments are enabled in production.
- **Polish (Phase 8)** comes last.

### Within phases

- Tests are written first and fail. The implementation task named "makes T0xx pass" follows.
- `models` (T005) → migration (T006) → repo (T010) → services → bot routers → i18n.
- T023 (router) depends on T020–T022. T032 depends on T028.

### Parallel opportunities

- Phase 2: T002, T004, T007, T008, T009, T011 and T014 touch different files.
- US1: T016, T017, T018 and T019 (tests) together. Then T020 and T021 in parallel (different
  modules). T022 and T025 follow.
- US3 `/paysupport` / `/terms` (T034, T037, T040) can run alongside US1 or US2.
- US4 tests T041, T042 and T043 together.
- Polish: T050, T051 and T052 together.

## Parallel Example: User Story 1

```text
Task: "T016 Unit tests for precheckout_decision in tests/unit/test_stars_checkout.py"
Task: "T017 Unit tests for period math in tests/unit/test_stars_periods.py"
Task: "T018 Bot integration tests in tests/integration/test_stars_payment_bot.py"
Task: "T019 Backfill tests in tests/integration/test_stars_reconcile.py"
# then
Task: "T020 offers.py"   Task: "T021 checkout.py"
```

## Implementation Strategy

### MVP first (US1)

1. Phases 1–2, then US1. Validate on the Telegram **test** server (quickstart steps 3, 4 and
   8).
2. Do **not** enable payments in production with US1 alone: there would be no way to cancel
   from the bot, no `/paysupport` or `/terms`, and data deletion would not cancel renewal.

### Production-ready increment

US1 + US2 + US3 + Phase 7 are the minimum for production. They meet Telegram's rules and the
constitution: cancel, support, terms, refunds and deletion that stops charges. US4 can follow
in the same release or the next one, since `pay refund` (US3) and `access show` already give
the operator the essentials.

### Rollout

Follow quickstart.md "Hosted rollout": deploy with payments off, then set the price and enable
payments, then make one real payment and refund it from the operator's second account.
