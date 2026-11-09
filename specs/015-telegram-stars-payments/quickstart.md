# Quickstart: Validate Telegram Stars Payments

## Automated

```
uv run pytest tests/unit/test_stars_checkout.py tests/unit/test_stars_periods.py \
  tests/unit/test_access_policy.py \
  tests/integration/test_stars_payment_bot.py tests/integration/test_stars_lifecycle.py \
  tests/integration/test_stars_reconcile.py tests/integration/test_admin_pay.py \
  tests/integration/test_stars_data.py tests/integration/test_access_gate_bot.py
uv run pytest            # full suite: payments default to off, so existing tests stay green
make lint typecheck
```

| Test | Proves |
|---|---|
| `test_stars_checkout` | Pre-checkout decision table: each decline reason, the renewal path, payer differs from offer owner, timeout → decline (FR-003, SC-003) |
| `test_stars_periods` | Period math: first, renewal from Telegram's expiry, backfill without expiry, late renewal, duplicate (FR-005, SC-004) |
| `test_access_policy` | Grace: a renewing Stars period stays active for 1 h after `ends_at`; a cancelled one does not |
| `test_stars_payment_bot` | US1 through the dispatcher with an offline bot: the **Subscription** button → offer (and the placeholder when off); `successful_payment` from a person with no account passes the gate and grants access; thanks message; duplicate update credited once; pending/rejected applicant sees **Subscription**; `/paysupport` forwards plain text and stores nothing; `/terms`; `/account` views (FR-001–007, FR-012, US1 #5) |
| `test_stars_lifecycle` | US2: renewal is silent and extends access; reminder variants with buttons; `pay:cancel` → `canceled`, access to `period_end`, then `ended` and `pay.ended_canceled`; `pay:resume`; `subscription` updates `canceled`/`active`/`failed`; expiry fallback after 1 h; API error changes nothing (FR-008–011) |
| `test_stars_reconcile` | A charge missing locally is credited from fake `getStarTransactions` and the person is told; a refund seen there is applied; a second run does nothing (SC-002) |
| `test_admin_pay` | `pay list/stats/show/refund/cancel/sync` output, exit codes, events; refund cancels renewal, ends access, notifies; a Telegram refund error changes nothing (FR-013–016, SC-007, SC-008) |
| `test_stars_data` | `/my_data` lists payments; `/delete_my_data` warns, cancels on Telegram first (aborts on error), keeps only the minimal `payments` columns with `owner_deleted_at`; purge after 1096 days; offers after 7 days (FR-019, FR-020) |
| `test_access_gate_bot` | Allow-list: payment service messages, `pay:` callbacks, `/paysupport`, `/terms` pass without using the refusal cooldown |

Migration check: `alembic upgrade head`, then `downgrade -1`, then `upgrade head` against the
test database (as in earlier migrations).

## Manual: Telegram test environment (free test Stars)

1. In the **test** Telegram app (test account), create a bot with the test @BotFather. Give
   the test account Stars through the test environment's top-up.
2. Run locally with `SCRIBE_BOT_TOKEN=<test token>`, `SCRIBE_TELEGRAM_TEST_ENV=true`,
   `SCRIBE_ACCESS_GATE=true`, `SCRIBE_PAYMENTS_ENABLED=true`, `SCRIBE_STARS_PRICE=1` and your
   test id in `SCRIBE_OPERATOR_TELEGRAM_IDS`.
3. From a second test account, send `/start`, then press **Subscription** → **Subscribe for 1
   ⭐** and pay. Expected: the thanks message within 10 s, `/start` begins setup,
   `scribe.admin pay list` shows one `first` payment, and `access show` reports `subscribed`
   from `stars`.
4. Press the same old offer link more than 1 hour later. Expected: Telegram shows "This offer
   has expired…" and nothing is charged.
5. `/account` → **Cancel subscription** → **Yes**. Expected: "cancelled, active until …".
   Telegram's subscription list shows it as cancelled. **Turn renewal back on** restores it.
6. Cancel from Telegram's own Settings → Stars → Subscriptions. Expected: `/account` shows
   cancelled within seconds (`subscription` update).
7. `/paysupport` → describe the issue. Expected: the operator account receives it as plain
   text with the charge id. `scribe.admin pay refund <charge id>`: the Stars return, the person
   gets `pay.refunded`, and the agent stops within 2 minutes.
8. Stop the service, pay from a third test account while it is down, then start it again.
   Expected: the payment is credited by `stars_reconcile` at start, and the person is told.
9. Renewal: it cannot be sped up on the test server, which also renews every 30 days. Check
   it on the hosted instance after the first 30 days (step 4 below), or rely on the automated
   tests.

## Hosted rollout

1. Back up the database:
   `sudo docker compose exec -T db pg_dump -U scribe scribe > backups/pre-0022.sql`, then
   `git pull`, `sudo docker compose build app` and
   `sudo docker compose run --rm app alembic upgrade head`.
2. Deploy with `SCRIBE_PAYMENTS_ENABLED=false` first and check that nothing changes (the full
   suite covers this too).
3. Set `SCRIBE_STARS_PRICE` to the chosen price and `SCRIBE_PAYMENTS_ENABLED=true`, then
   restart. Pay once from the operator's own second account and refund it with `pay refund`.
4. After 30 days, check `pay stats` for the first renewals, and that no person with a
   `renewal` payment ever got an "access ended" notice (SC-004).
