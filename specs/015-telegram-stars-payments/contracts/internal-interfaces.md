# Contract: Internal Interfaces (`scribe.payments`)

Signatures and guarantees that tests rely on. The Telegram calls sit behind the `StarsApi`
protocol, so tests use a fake (constitution VI: narrow interfaces).

## `scribe.payments.telegram.StarsApi` (Protocol)

```python
class StarsApi(Protocol):
    async def create_subscription_link(
        self, *, payload: str, price: int, title: str, description: str
    ) -> str: ...
    async def set_renewal(
        self, *, telegram_id: int, first_charge_id: str, canceled: bool
    ) -> None: ...  # editUserStarSubscription
    async def refund(self, *, telegram_id: int, charge_id: str) -> None: ...
    async def incoming_transactions(self, *, since: datetime) -> list[IncomingStars]: ...
```

`AiogramStarsApi(bot)` is the real one. `IncomingStars` holds `charge_id`, `telegram_id`,
`amount`, `payload`, `date`, `subscription_period`, `is_refund`. Telegram errors are raised
as `StarsApiError`.

## `scribe.payments.offers`

- `async def make_offer(session, api, settings, telegram_id, *, lang, now) -> Offer | Running |
  Unavailable`: inserts a `payment_offers` row and creates the link. It returns `Running` when
  the person has an `active` or `canceled` Stars subscription, and `Unavailable` when payments
  are not available.
- `def parse_payload(payload: str) -> uuid.UUID | None`: accepts `sub1:<uuid>` only.

## `scribe.payments.checkout`

- `async def precheckout_decision(session, settings, *, payer_id, payload, currency,
  total_amount, now) -> Accept | Decline(reason)`: research P3. It is pure apart from reads.
  The handler wraps it in a 5 s timeout, and a timeout gives `Decline("error")`.

## `scribe.payments.credit`

- `async def credit_payment(session, *, payer_id, charge_id, amount, payload, paid_at,
  expires_at: datetime | None, is_first: bool, via: PaymentSource, now) -> Credited |
  AlreadyCredited | Unmatched`: research P4.
  - It is idempotent on `charge_id`.
  - It returns `Unmatched` (logged, and the operator is alerted once) for an unknown payload.
    Telegram has charged the person, so such a payment is never dropped silently. The row is
    still recorded with `stars_subscription_id = NULL` and a 30-day access period, so the
    payer is not left without what they paid for.
  - `expires_at = None` (backfill) means `paid_at + 30 days`.
  - The caller commits, then sends the notice.
- `async def apply_refund(session, *, charge_id, by: str, now) -> Refunded | AlreadyRefunded |
  NotFound`: the DB part of research P8. It is idempotent.

## `scribe.payments.lifecycle`

- `async def cancel_renewal(session, api, *, telegram_id, by: CancelSource, now) -> Canceled |
  NothingRunning`: calls the API first, then updates the row.
- `async def resume_renewal(session, api, *, telegram_id, now) -> Resumed | NothingToResume`.
- `async def on_subscription_update(session, *, payer_id, payload, state: str, now) -> None`:
  handles `canceled`, `active` and `failed`, and ignores unknown states with a log line.
- `async def end_due(session, *, now) -> list[EndedStars]`: `canceled` subscriptions past
  `period_end`, and `active` ones past `period_end + 1 h`. It is called from
  `access_reconcile`.

## `scribe.payments.reconcile`

- Job `stars_reconcile` (`Every(10 min)`, also at start, only when payments are available or
  any Stars subscription is running). It calls `incoming_transactions(since=now-7d)`, then
  `credit_payment(via=backfill)` and `apply_refund(by="telegram")`. It logs counts.

## Changes to existing modules

| Module | Change |
|---|---|
| `access/policy.py` | `status(..., renewing_payment_ids: frozenset[uuid.UUID] = frozenset())`: a `stars` row whose `payment_id` is in the set counts as active until `ends_at + STARS_GRACE` (1 h) |
| `access/repo.py` | `access_status` loads the renewing set (one extra indexed query) |
| `access/gate.py` | `payment_allows(event, state)` allow-list (contracts/control-chat.md) |
| `access/apply.py` | `subscription_callback` → offer or manage view; the pending/rejected reply gets **Subscription** when payments are available |
| `access/reconcile.py` | calls `lifecycle.end_due`; Stars reminder variants (research P6); `pay.ended_*` notices |
| `retention/user_data.py` | cancel-before-delete hook; keep `payments` with `owner_deleted_at`; `/my_data` payments |
| `retention/purge.py` | payments 1096 days, offers 7 days, orphan ended Stars subscriptions |
| `telegram/control_bot/bot.py` | include the `payments` routers; register the gate allow-list |
| `telegram/control_bot/render.py` | `COMMAND_NAMES` + `account`, `paysupport`, `terms` |
| `__main__.py` | `start_polling(..., allowed_updates=dispatcher.resolve_used_update_types())`; wire `AiogramStarsApi`, register `stars_reconcile`; optional test-server base URL |
| `config.py` | `payments_enabled`, `stars_price` (1–10000), `telegram_test_env` |
| `admin.py` | `pay` subcommands (contracts/operator-cli.md) |
| `db/models.py` | new tables, enums and columns (data-model.md) |
