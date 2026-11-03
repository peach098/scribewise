# Quickstart: Validate the Access Gate

## Automated

```
uv run pytest tests/unit/test_access_policy.py tests/unit/test_scheduler_interval.py \
  tests/integration/test_access_gate_bot.py tests/integration/test_access_reconcile.py \
  tests/integration/test_admin_access.py tests/integration/test_send_access.py \
  tests/integration/test_beta_apply_bot.py
uv run pytest            # full suite: the gate defaults to off, so the existing tests must stay green
```

| Test | Proves |
|---|---|
| `test_access_policy` | Status rules: beta, subscription windows (start, end and cancel edges), both, operator, chained grants (FR-001, FR-014–015) |
| `test_scheduler_interval` | `Interval` schedule `next_run` |
| `test_access_gate_bot` | Refuse: no user row, one answer per cooldown, callbacks answered silently, groups ignored. Restricted: only the data commands pass. Username entry links on first message. Beta added mid-conversation means the next message starts setup (US1, US2, FR-002–007, FR-009, FR-011) |
| `test_access_reconcile` | Lost: column set, client stopped (`FakeMessagingGateway`), pending drafts expired, one notice, event written. Regained: client started, notice, reconnect hint. 3-day reminder sent once. Idempotent across runs (US3, FR-012–013, FR-016) |
| `test_send_access` | `send_approved_draft` refuses for a suspended user (FR-004) |
| `test_beta_apply_bot` | The in-bot beta application: two buttons and no link in the refusal, use case validation and review, consent, the operator message (plain text) and its buttons, approve and reject (30-day block), pending notice, first decision wins, only operators decide, restricted users, Russian (US5, FR-005, FR-023–028) |
| `test_admin_access` | Every CLI command in [contracts/operator-cli.md](contracts/operator-cli.md): output, exit codes, events (FR-008, FR-018–020) |

The i18n lint (`tests/unit/test_i18n_lint.py`) covers the new `access.*` keys.

## Manual, on the hosted instance

1. **Back up the database**, then apply the migrations (0005 and, since US5, 0006):
   - `docker compose exec -T db pg_dump -U scribe scribe > backups/pre-0006.sql`
   - `git pull && docker compose build app`
   - `docker compose run --rm app alembic upgrade head`
   - `docker compose up -d app`
2. **Add the operator** to `.env`: `SCRIBE_OPERATOR_TELEGRAM_IDS=<your id>`.
3. **Add the existing beta users** before turning the gate on, so nobody is cut off:
   - `docker compose run --rm app python -m scribe.admin list-users`;
   - for each user to keep: `... scribe.admin beta add <control chat id> --note "pre-gate"`.
4. **Turn the gate on**: set `SCRIBE_ACCESS_GATE=true` in `.env` and run
   `docker compose up -d app`. `curl -s localhost:8083/healthz` should show
   `"access_gate": true`.
5. **Stranger applies**: from an account that is not on the list, send `/start`, then
   `hello` 3 times.
   - Expected: one "closed beta" reply with **Join the beta** and **Subscription**, and no link.
   - **Subscription** shows the "coming soon" placeholder.
   - **Join the beta**, type a use case, check the review (with the personal-data note) and
     press **Send application**: the bot confirms, and the operator chat gets the application
     with **Approve** / **Reject**.
   - `scribe.admin beta requests` lists it as pending; `access stats` shows 1 refused person
     and 1 pending application; `list-users` shows no new account.
   - Writing again (after the cooldown) gets "your application is being reviewed".
6. **Approve**: press **Approve** in the operator chat (or `scribe.admin beta approve <id>`).
   - Expected: the operator message shows "✅ Approved by …" with no buttons; the applicant
     gets "You're in the Scribe beta!"; their `/start` begins setup.
   - `beta list` shows the entry (note `application`) as linked.
   - From a second account, apply and **Reject**: it gets the polite rejection, and writing
     again says it can apply again in 30 days.
   - Inviting directly still works: `scribe.admin beta add @thatusername`.
7. **Remove**: `scribe.admin beta remove @thatusername`.
   - Expected: within about 2 minutes, the "access ended" notice.
   - Then `/settings` gets the "access ended" reply, while `/my_data` still works.
8. **Subscription**: `scribe.admin sub grant <id> --days 2`.
   - Expected: within about 2 minutes, the "access restored" notice and, on the next run, the
     ending reminder.
   - `sub end <id>` ends access again.

## Existing users when the gate goes on

Any account not added in step 3 is suspended by the first `access_reconcile` run, about 2
minutes after step 4. It gets one "access ended" notice and keeps its data. This includes the
stranger account created on 2026-10-21.
