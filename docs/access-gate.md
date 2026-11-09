# Access gate (hosted)

On a hosted instance the bot can be limited to invited beta users and people with a paid
subscription. Everyone else gets a short "closed beta" reply (at most once per cooldown) with
two buttons: **Join the beta**, where they describe how they plan to use Scribe and send an
application (agreeing to the processing of their personal data), and **Subscription**, a
"coming soon" placeholder. No account is created and nothing they type is stored before they
send the application. Each application reaches the operators in the bot with **Approve** /
**Reject**; approving adds the person to the beta list. A personal install turns the gate on
with only its owner in `SCRIBE_OPERATOR_TELEGRAM_IDS`.

Settings (`.env`):

- `SCRIBE_ACCESS_GATE` (false): turn the gate on. Off means every sender is served, so
  `python -m scribe` then refuses to start (exit code 2) unless `SCRIBE_OPEN_BOT=true`.
- `SCRIBE_OPEN_BOT` (false): serve everyone with the gate off, on purpose.
- `SCRIBE_OPERATOR_TELEGRAM_IDS` (empty): comma-separated Telegram ids that always have
  access, e.g. `900000001,900000002`.
- `SCRIBE_SUBSCRIPTION_CONTACT` (empty): where to arrange a subscription (text or link), shown
  with the **Subscription** placeholder and in renewal reminders. Empty leaves it out.
- `SCRIBE_ACCESS_REFUSAL_COOLDOWN_S` (3600): how often one refused person is answered.

`/healthz` reports the mode as `"access_gate": true|false` and `"payments_enabled": true|false`.

## Paying with Telegram Stars

With `SCRIBE_PAYMENTS_ENABLED=true`, `SCRIBE_STARS_PRICE` set and the gate on, **Subscription**
shows an offer: a recurring Telegram Stars subscription for the price, 30 days, renewing until
the person cancels. Stars need no payment provider. Digital services in bots are sold in Stars
only, so this is the way a hosted instance charges.

- **How it plugs into access.** Each paid 30 days is an ordinary row of `subscriptions` with
  `source=stars`, so the gate, the reconcile job and `/my_data` work as before. Renewals are
  credited silently, and a renewing period stays active for 1 hour past its end so a renewal
  confirmed a little late never cuts access. `sub extend` and `sub end` act on operator rows
  only.
- **Exactly once.** Every charge is credited once, keyed by Telegram's charge id. The
  `stars_reconcile` job (every 10 minutes and at start) reads `getStarTransactions` for the
  last 7 days and credits anything the bot missed, for example during a restart.
- **People.** `/account` shows the access and the subscription, with **Cancel subscription**
  and **Turn renewal back on**. The same buttons come with the reminder 3 days before a
  renewal. Changes made inside Telegram (cancelling from Settings, a failed charge) are
  followed. `/paysupport` forwards a message to the operators as plain text, with the charge
  ids of the last 90 days; nothing is stored. `/terms` states the rules.
- **Operator.** `pay list [--from] [--to] [--who]`, `pay stats`, `pay show <who>`,
  `pay refund <charge id> [--note]` (stops the renewal, refunds through Telegram, ends that
  payment's access and tells the person), `pay cancel <who>` (stops a renewal, no refund) and
  `pay sync [--days N]` (one `stars_reconcile` run). Refunds, `pay cancel` and `pay sync` need
  `SCRIBE_BOT_TOKEN`. `sub list` and `access show` name each period's source once Stars are in
  use, and `access stats` adds `paying subscribers`.
- **Data.** `/delete_my_data` first cancels the renewal on Telegram (and stops if Telegram
  refuses, so nobody is charged after deleting), then deletes everything except a minimal
  payment record: Telegram id, amount, dates, charge id and refund status. It is kept for
  3 years after the deletion for refunds, disputes and accounting, then purged; the person is
  warned before they delete, and `/terms` says so.
- **Trying it.** Run the bot against Telegram's test server (`SCRIBE_TELEGRAM_TEST_ENV=true`),
  where Stars are free. The manual checks are in
  [`specs/015-telegram-stars-payments/quickstart.md`](../specs/015-telegram-stars-payments/quickstart.md).

Self-hosted installs stay free: payments are off by default and nothing changes without them.

Admin commands (`docker compose run --rm app python -m scribe.admin <command>`); `<who>` is a
numeric Telegram id or `@username`:

```bash
beta add <who> [--note TEXT]        # invite; a @username links on their first message
beta remove <who>
beta list                           # entries, linked or not, and active users
beta requests [--all]               # applications from the bot; --all adds those decided in 30 days
beta approve <application id>       # same as the Approve button: adds to the beta list, tells them
beta reject <application id>        # same as the Reject button
sub grant <who> (--days N | --until YYYY-MM-DD) [--note TEXT]   # chains after an active one
sub extend <who> (--days N | --until YYYY-MM-DD) [--note TEXT]
sub end <who> [--note TEXT]
sub list [--all]                    # active and scheduled; --all adds ended and cancelled
access show <who>                   # status, account, suspension, last access events
access stats [--days N]             # beta, subscriptions, refused people, applications, suspended
```

Exit code 0 is success, 1 is "not found or nothing to change", 2 is a usage error. Every
change is recorded as an access event. The bot applies a change on the person's next message;
the agent stops or starts within about 2 minutes (the `access_reconcile` job), and the person
gets one "access ended" or "access restored" notice. A user without access keeps their data
and can still use `/my_data` and `/delete_my_data`.

Rollout order, so nobody is cut off:

1. Back up the database, then `docker compose run --rm app alembic upgrade head` and
   `docker compose up -d app`.
2. Set `SCRIBE_OPERATOR_TELEGRAM_IDS` to your own id.
3. **Add the existing users before turning the gate on**: `python -m scribe.admin list-users`,
   then `python -m scribe.admin beta add <control chat id> --note "pre-gate"` for each one to
   keep. Anyone left out is suspended by the first `access_reconcile` run (about 2 minutes
   after the next step); they get one notice and keep their data.
4. Set `SCRIBE_ACCESS_GATE=true`, run `docker compose up -d app` and check that
   `curl -s localhost:<health port>/healthz` shows `"access_gate": true`.

Retention: refused contacts are deleted 30 days after they were last seen, access events after
365 days, decided beta applications 30 days after the decision (pending ones wait for one), and
an application typed but never sent after 24 hours. `/delete_my_data` also removes the
person's beta entry, subscriptions, applications and access events. The full walkthrough is in [`specs/005-beta-subscription-access/quickstart.md`](../specs/005-beta-subscription-access/quickstart.md).
