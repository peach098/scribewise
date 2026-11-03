# Contract: Operator CLI (`python -m scribe.admin`)

This extends the existing CLI (`list-users`, `set-tier`). It runs inside the app
container with the service's `SCRIBE_*` environment:

```
docker compose run --rm app python -m scribe.admin <command> ...
```

## How arguments work

- `<who>` is a numeric Telegram id (e.g. `123456789`) or `@username`. Usernames are
  matched without regard to case.
- Exit codes:
  - `0`: success.
  - `1`: not found, or nothing to change. The message goes to stderr.
  - `2`: usage error (argparse).
- Every change writes one `access_events` row with `actor=operator` and the `--note` text
  as `reason`.
- The output never shows names, chats or message content.

## Beta list

| Command | Effect | Output |
|---|---|---|
| `beta add <who> [--note TEXT]` | Creates an entry. If one exists for the same id or unlinked username, exits 1 with "already on the beta list" | `added <who>` |
| `beta remove <who>` | Deletes the entry, matched by id or username | `removed <who>` |
| `beta list` | All entries, oldest first | One line per entry: `<telegram_id or -> <@username or -> <added YYYY-MM-DD> <linked yes/no> <has account yes/no> <note>` and a footer `N entries, M active users (goal 30)` |

### Beta applications (US5)

| Command | Effect | Output |
|---|---|---|
| `beta requests [--all]` | Pending applications, oldest first. With `--all`, also decided ones from the last 30 days | `<application id> <status> <created YYYY-MM-DD HH:MM> <telegram_id> <@username or -> <lang>`, then the use case indented on the next lines |
| `beta approve <application id>` | Same as the **Approve** button: adds the beta entry, notifies the applicant and updates the operator messages. Exits 1 if not found or not pending | `approved <telegram_id>` |
| `beta reject <application id>` | Same as **Reject**. Exits 1 if not found or not pending | `rejected <telegram_id>` |

## Subscriptions

| Command | Effect | Output |
|---|---|---|
| `sub grant <who> (--days N \| --until YYYY-MM-DD) [--note TEXT]` | New subscription. It starts now, or at the end of the current one if one is active. `--until` is end of day UTC. `<who>` must be numeric or a username already linked to a Telegram id; an unlinked username gives exit 1 with "ask them to message the bot first" | `granted <who> until <date>` |
| `sub extend <who> (--days N \| --until YYYY-MM-DD) [--note TEXT]` | Moves `ends_at` of the latest open (active or scheduled) subscription, or if none is open, of the latest non-cancelled ended one, so extending after expiry restores access. `--days` counts from `max(ends_at, now)`. Clears `reminded_at`. Exits 1 only if the person has no subscription at all | `extended <who> until <date>` |
| `sub end <who> [--note TEXT]` | Sets `cancelled_at = now` on all active and scheduled subscriptions | `ended <who>` |
| `sub list [--all]` | Active and scheduled subscriptions. With `--all`, also ended and cancelled ones | `<telegram_id> <state> <starts> <ends> <note>` |

## Access

| Command | Output |
|---|---|
| `access show <who>` | `kind=<operator\|beta\|subscribed\|beta_and_subscribed\|none> until=<date or ->`, `account=<yes/no> suspended=<since or no>`, and the last 5 access events (`at kind actor reason`) |
| `access stats [--days N]` (default 7) | `beta entries`, `beta active users`, `subscriptions active`, `refused people (last N days)`, `applications pending`, `applications approved / rejected (last N days)`, `refusal answers sent`, `suspended users` (FR-020, SC-006) |

## When changes take effect

- **The bot**: on the person's next message (FR-010).
- **The agent** (stop or start, notices): within about 2 minutes, through the `access_reconcile`
  job. The CLI prints `the running service applies this within about 2 minutes` after each
  change.
