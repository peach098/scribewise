# Contract: Operator CLI (payments)

Extends `specs/005-beta-subscription-access/contracts/operator-cli.md`. The same conventions
apply: `<who>` is an id or `@username`, exit codes 0/1/2, and no names or content in the output.

```
docker compose run --rm app python -m scribe.admin pay <command> ...
```

| Command | Effect | Output |
|---|---|---|
| `pay list [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--who <who>]` | Payments in the range (default: the last 30 days), newest first | One line per payment: `<paid YYYY-MM-DD HH:MM> <telegram_id> <amount>⭐ <first/renewal> <period start>–<period end> <charge_id> [refunded YYYY-MM-DD by X] [deleted owner]` |
| `pay stats [--from] [--to]` | Totals for the range (FR-016, SC-007) | `payments N (first F, renewal R), received S⭐, refunded K payments / T⭐, net S-T⭐`, then `stars subscriptions: active A, cancelled but running C, ended E (in range)` |
| `pay show <who>` | The person's Stars subscription(s) and payments | State, price, `period_end`, `canceled_by` / `ended_reason`, then payments as in `pay list` |
| `pay refund <charge_id> [--note TEXT]` | Research P8: cancels renewal, refunds through Telegram, cancels the access period, ends the Stars subscription, notifies the person. Exits 1 if not found or already refunded. On a Telegram refund error, exits 1 and changes nothing | `refunded <charge_id> <amount>⭐ to <telegram_id>` |
| `pay cancel <who> [--note TEXT]` | Stops renewal of a running Stars subscription without a refund (`canceled_by=operator`, an `operator` value of `stars_cancel_source`). Exits 1 if nothing is running | `cancelled renewal for <telegram_id>, active until YYYY-MM-DD` |
| `pay sync [--days N]` | Runs `stars_reconcile` once now (default 7 days) | `checked N transactions, credited M, refunds applied R` |

Changes to the existing commands:

- `sub list` and `access show` print each subscription's source (`operator` or `stars`).
- `sub extend` and `sub end` act on `operator` rows only. If the person also has a running
  Stars subscription, they print a hint: "stars subscription running: use `pay cancel`/`pay
  refund`".
- `access stats` adds `paying subscribers: N`.

The CLI creates a `Bot` for `pay refund`, `pay cancel` and `pay sync` only (as it does today
for the notifying `beta` commands). Every change writes an `access_events` row with
`actor=operator` and the note as `reason`.
