# Research: Release Notes for Users After an Update

Decisions behind [plan.md](plan.md). Each entry gives the decision, the rationale and the
alternatives considered. Code references are to `main` at `2e8a82e`.

## R1. Where the notes live

**Decision**: Markdown-like plain-text files in the repository at
`release-notes/<major>.<minor>.<patch>.md`, one per version, with `## en` and `## ru`
sections ([contracts/release-notes-file.md](contracts/release-notes-file.md)). The
`Dockerfile` copies the directory into the image (`COPY release-notes ./release-notes`), and
`Settings.release_notes_dir` (default `release-notes`, relative to `/app` like
`prompts_dir`) points at it.

**Rationale**: notes are written with the code change that they describe and reviewed in the
same pull request. Deploys are `git pull` + build, so the file is always in the image of the
version it describes. No CLI editor, no extra table, nothing to back up.

**Alternatives**: a DB table edited through the CLI (rejected: no review, text lives only in
one database, the self-hosted copy would not have it); a single `CHANGELOG.md` (rejected:
harder to parse, mixes technical and user-facing text).

## R2. Version source and comparison

**Decision**: the running version is `importlib.metadata.version("scribe")` (already used as
the default of `Settings.tg_app_version`), read through a small `scribe.release_notes.version`
helper so tests can inject it. Versions are parsed as `X.Y.Z` (three non-negative integers)
into tuples and compared as tuples. A running version that is not `X.Y.Z` (for example
`0.2.0.dev0` from a local editable install) disables the automatic check for that start with
a `release_notes_version_unparsed` warning. `Settings.tg_app_version` is not reused, because
an operator may override it for Telegram's "Active sessions" list.

**Rationale**: three-part numeric versions are all this project needs. `packaging` is only a
dev dependency (via pytest), and the constitution asks to justify each new dependency
(Principle VI), so a 10-line parser is preferred.

**Alternatives**: `packaging.version.Version` (would add a runtime dependency); git tags at
runtime (the image has no `.git`).

**Release-process note**: `pyproject.toml`'s version is also recorded in `uv.lock`, and the
`Dockerfile` runs `uv sync --locked`. A bump without `uv lock` fails the build, so the
documented release steps include `uv lock` (US5, [quickstart.md](quickstart.md)).

## R3. Automatic or manual, and the "last handled version"

**Decision**: automatic by default (`SCRIBE_RELEASE_NOTES_AUTO=true`), plus manual send. The
database stores every announcement in `release_announcements` with a `release_version`. The
**last handled version** is the maximum `release_version` over all rows, compared as version
tuples in Python (the table has a row per release, so it stays tiny). At start, if the
running version is newer, the service creates one `auto` announcement whose
`release_version` is the running version and whose `versions` are all versions with notes in
`(last handled, running]`. When there are none, the row is still written with an empty
`versions` list and marked finished at once: that is how "handled without notes" is
recorded (FR-005). A partial unique index on `release_version` for `trigger = 'auto'` makes
the check idempotent.

With no row at all (first deploy of this feature), only the running version is considered
(FR-007), so old notes files are never replayed.

**Rationale**: one table answers "what was announced", "what was handled" and "to whom",
without a separate key-value marker. A manual send also counts: sending 0.3.0 by hand before
deploying 0.3.0 means the deploy sends nothing more.

**Alternatives**: a single-row `release_state` table (one more table for a value already
derivable); storing the marker in `scheduled_jobs` (wrong owner).

## R4. Recipients

**Decision**: when an announcement is created, the recipients are snapshotted as
`release_deliveries` rows (`pending`) for every `users` row that has access now:

- gate on: `access_suspended_at IS NULL` **and** `policy.status(...)` has access (beta,
  subscription or operator). The second check catches a loss that `access_reconcile` has
  not applied yet.
- gate off: every `users` row (`access_reconcile` clears suspensions when the gate is off).

`users.state` is ignored: `setting_up`, `paused` and `out_of_office` users get the notes
(spec Clarifications). Refused senders and applicants have no `users` row, so they are never
candidates. Right before sending, the sender checks again (FR-010) and marks the row
`skipped` if the user no longer has access.

The bulk status code in `access/reconcile.py` (`_betas`, `_subscriptions`) is moved to a
public `access.repo.bulk_status(session, settings, users, now)` so the reconcile job, the
announcement and the CLI share one implementation.

Users created after the snapshot are not added (spec Edge Cases).

## R5. Digest per user

**Decision**: an announcement stores the versions it covers (ascending) and a snapshot of
their notes (`notes_json`: `{version: {"en": text, "ru": text}}`). At send time the sender
computes the user's **personal versions**: the announcement's versions minus the versions
the user already received through another announcement (deliveries in state `sent` or
`unknown`). Empty means `skipped` with `skip_reason = already_received`. The message is the
personal versions, oldest first, each with an i18n heading `release_notes.heading` ("What's
new in Scribe {version}") followed by the text in the user's language (falling back to the
other language when empty, FR-011).

**Splitting**: one Telegram message holds 4,096 characters. Each section is capped at
3,500 characters (validation), so one version always fits with its heading. Versions are
packed greedily into parts of at most 4,096 characters, splitting only at version
boundaries. `release_deliveries.parts_total` (set at claim) and `parts_sent` record progress, so a restart continues with
the next part and never resends a part.

**Rationale**: the snapshot makes a manual announcement created by the CLI (another
container, same image) self-contained, and later edits of a file do not change an
announcement in flight.

## R6. Sending: where it runs

**Decision**: a scheduler job `release_notes` (`Every(15 s)`, `run_at_start=True`),
registered in `Service.start` next to `access_reconcile`. Each run:

1. On the first run of the process only, if `release_notes_auto` is on: the startup check
   (R3).
2. Drain: while the process-local `asyncio.Lock` is free, claim the oldest `pending`
   delivery whose `next_attempt_at <= now` (`FOR UPDATE SKIP LOCKED`), set it `sending`,
   commit, send, and record the result in a second short transaction. Repeat until nothing is
   due. A run that finds the lock taken returns at once, so a long drain never overlaps
   with the next tick's run.

`Service.start` calls `release_notes.recover_interrupted()` before the scheduler starts,
just like `requeue_interrupted_turns()`. It turns rows left in `sending` by a crash into
`unknown` or `pending`. The part in doubt (index `parts_sent`) counts as sent. If it was the
last one (`parts_sent + 1 >= parts_total`), the row becomes `unknown`. Otherwise it goes back
to `pending` with `parts_sent + 1`, so only the remaining parts are sent. Only the service ever sets `sending`; the CLI only creates `pending` rows.

**Rationale**: the scheduler already gives "due at start", a DB-backed schedule, failure
logging and graceful shutdown (`Scheduler.shutdown` cancels tasks). A dedicated
`asyncio.Task` in `Service` would duplicate all of that. The job registration is what makes a
manual `send` from the CLI reach users within ~15 s.

**Alternatives**: sending from the CLI process (rejected: two processes would share the
bot's rate limit without coordination, and a long-running CLI is fragile); a sending loop
spawned directly in `Service.start` (rejected: duplicates the scheduler's lifecycle).

## R7. Rate limits

**Decision**: one sequential sender per process, paced by a minimal interval of
`1 / release_notes_rate_per_s` between sends (default 10 messages/s, allowed range 1-25).
Parts of the same digest are spaced at least 1.1 s apart (Telegram allows about one message
per second per chat). The pacing uses an injectable `sleep` so tests do not wait.

The bot has no global send queue today: draft cards, notices and reminders call
`bot.send_message` directly. 10/s for announcements leaves about two thirds of Telegram's
~30/s for that traffic, and 1,000 users take under 2 minutes (SC-001 allows 10).

**Retry after**: `aiogram.exceptions.TelegramRetryAfter` pauses the whole sender for
`retry_after` seconds (plus a 1 s margin) and retries the same part. It does not increase
`attempts` (FR-014).

**Rationale**: a sequential sender is the simplest way to meet both limits. Concurrency is
unnecessary at this scale.

**Alternatives**: an aiogram-wide rate-limiting session middleware for all bot traffic
(useful, but a larger change to every sender; left as a possible follow-up); token-bucket
concurrency (unneeded).

## R8. Errors and the crash window

**Decision** ([data-model.md](data-model.md) state machine):

| Telegram outcome | Result |
|---|---|
| success | `parts_sent += 1`; after the last part `sent`, `delivered_at = now` |
| `TelegramRetryAfter` | sleep and retry the same part (no attempt counted) |
| `TelegramForbiddenError` (blocked, user deactivated) | `blocked` |
| `TelegramBadRequest` whose message says the chat was not found, or `TelegramNotFound` | `blocked` |
| other `TelegramBadRequest` | `failed` (retrying the same text cannot help) |
| `TelegramNetworkError`, `TelegramServerError`, other `TelegramAPIError` | `attempts += 1`; back to `pending` with `next_attempt_at = now + backoff` (30 s, 2 min, 10 min, 30 min); after `release_notes_max_attempts` (default 5) `failed` |

A crash between Telegram accepting a part and the commit of `parts_sent` leaves the row in
`sending`. On the next start (R6), that part is treated as delivered: the row becomes
`unknown` (if it was the last part) or moves on to the next part. This chooses "no
duplicates" over "nobody missed", because a repeated "what's new" annoys more than one missed
announcement. With one row in `sending` at a time, at most one user is affected per crash.
`unknown` counts as received for de-duplication (R5) and is shown separately in `status`
(FR-016, FR-022).

Logs (`release_notes_delivery`) carry the delivery id, the user id, the result and the error
class only, never the text, the chat id or the username (FR-023).

## R9. Silent delivery and plain text

**Decision**: `send_message(..., disable_notification=True)` with no `parse_mode` (the bot
is created without a default parse mode, `telegram/control_bot/bot.py:create_bot`). The text
goes out exactly as written, and nothing in it is interpreted as formatting or links.

**Rationale**: announcements are informational and can go out at night after a deploy.
Plain text means no escaping bugs and no surprises from `*` or `<` in notes.

## R10. Operator CLI

**Decision**: a new `release-notes` command group in `scribe/admin.py`
([contracts/operator-cli.md](contracts/operator-cli.md)): `preview <version>`, `check`,
`send <version> [--dry-run]` and `status [--all]`. `send` creates a `manual` announcement for
exactly that version and its `pending` deliveries in one transaction, leaving out users who
already received that version. It prints the counts and `the running service sends this
within about a minute`. `--dry-run` runs the same computation and rolls back. If nobody is
left to send to, it exits with code 1 and "nothing to send". The CLI never calls Telegram,
so it needs no bot.

## R11. Data subject rights

**Decision**: `release_deliveries.user_id` has an `ON DELETE CASCADE` foreign key to
`users`, so `/delete_my_data` (which deletes the user row) removes the deliveries.
`export_user_data` gains a `release_notes` key (announcement versions, state, delivered at),
and `data_summary` a `release_notes` count rendered as one `/my_data` line. Announcements
hold only operator-written text and counts, so they are not personal data and are kept.
Nothing about refused senders is stored.

## R12. Migration numbering

**Decision**: migration `0011_release_notes.py` (`revision = "0011"`, `down_revision = "0008"`)
(the current head). Parallel feature branches also start from 0008 (0009, 0010, 0012, 0013). Whoever merges second
renumbers (or adds an Alembic merge revision), and `alembic heads` must show exactly one head
before merge.
