# Implementation Plan: Release Notes for Users After an Update

**Branch**: `011-release-notes` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/011-release-notes/spec.md` (GitHub issue #20)

## Summary

After a deploy of a new version, every user with access gets one short, silent "what's new"
message from the control bot in their language. A restart or a redeploy of the same version
sends nothing.

Technical approach:

- **Notes as files.** `release-notes/<X.Y.Z>.md` with `## en` / `## ru` sections, shipped in
  the image. A pure parser validates them, and a unit test checks every file in the repo.
- **Announcements in the database.** Migration `0011` adds `release_announcements` (versions
  covered, a snapshot of the text, auto or manual) and `release_deliveries` (one row per
  recipient with its state). The newest `release_version` is the "last handled version", so
  restarts are idempotent. A partial unique index on auto rows makes a second start of the
  same version unable to announce it again.
- **One scheduler job.** `release_notes` (every 15 s, due at start) does the startup check
  once per process, then drains pending deliveries with a single paced sender: 10 msg/s by
  default, at least 1.1 s between parts of one digest, `RetryAfter` honoured, blocked users
  recorded. Each send is bracketed by two short transactions (`sending` → result), so a
  crash affects at most one in-flight message, which is then counted as `unknown` and not
  resent.
- **Recipients from the access gate.** Users with an account and access per
  `access.policy.status` and not suspended (specs/005), or every user when the gate is off.
  Access is checked again right before each send.
- **Operator CLI.** `python -m scribe.admin release-notes preview|check|send [--dry-run]|status`.
  `send` only queues rows. The running service delivers them, so there is exactly one sender
  and one rate limit.

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: aiogram 3 (`Bot.send_message`, `aiogram.exceptions`), SQLAlchemy 2
async + asyncpg, Alembic, pydantic-settings, structlog. **No new dependencies.** Version
parsing is a small local helper instead of `packaging` (research R2).

**Storage**: PostgreSQL 16. Migration `0011_release_notes.py` adds the tables
`release_announcements` and `release_deliveries` and the enums `release_trigger` and
`release_delivery_state` ([data-model.md](data-model.md)). The notes themselves are files in
`release-notes/`.

**Testing**: pytest + pytest-asyncio.

- **Unit**: parser and validation, version compare, digest building and splitting, pacing
  with a fake sleep, error classification, and a test over the real `release-notes/`
  directory.
- **Integration**: the Postgres test container covers the startup check, recipients with the
  gate on and off, delivery states, crash recovery, `/my_data` and deletion, and the CLI
  in-process. It uses `offline_bot()` / `RecordingSession` from
  `tests/integration/bot_support.py`, extended to raise chosen aiogram exceptions.

**Target Platform**: Linux server, Docker Compose, single process (unchanged)

**Project Type**: Single long-running service (asyncio worker + Telegram control bot) +
operator CLI

**Performance Goals**: 1,000 recipients delivered in under 10 minutes (SC-001). At the
default 10 msg/s that is about 100 s.

**Constraints**: Telegram's ~30 msg/s per bot and ~1 msg/s per chat; 4,096 characters per
message. Sending must not stall the bot's polling or the other scheduler steps (FR-018): the
job runs as a background task of the scheduler, and every DB transaction is short.

**Scale/Scope**: about 30 beta users today, designed for low thousands. Small code surface:
one new package (`scribe/release_notes/`, about 4 modules), one migration, CLI commands, two
i18n keys, `/my_data` additions, a Dockerfile line and docs.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment |
|---|---|
| I. Human approval before sending | **Pass.** Nothing is sent on the user's behalf. The control bot sends operator-written notes to the user's own bot chat, like the existing notices. The user's Telegram account is never used. The operator approves the text in review and can preview it, dry-run it and turn automatic sending off. Every announcement and delivery is recorded and attributable (`trigger`, per-recipient state). |
| II. Privacy & data minimization | **Pass.** No message content is involved. Deliveries hold user id, state, counts and times only, and they cascade on `/delete_my_data` and are listed in `/my_data` (R11). Logs hold ids, versions and counts, never the text, chat ids or usernames (FR-023). No secrets are added. |
| III. Grounded replies | **N/A.** No drafts or LLM calls. |
| IV. Faithful personal style | **N/A.** |
| V. Evaluation-driven development | **Pass.** No draft generation changes, so no eval run is needed. The deterministic code (parser, versions, state machine, pacing, CLI) gets tests written with or before the code, as listed in tasks.md. |
| VI. Simplicity & observability | **Pass.** No new dependency or service. It reuses the scheduler, the access policy and the admin CLI. One sequential sender instead of a queue system. Structured logs: `release_notes_announced` (versions, recipients), `release_notes_delivery` (delivery id, user id, result, error class), `release_notes_finished` (counts). Telegram is reached only through aiogram's `Bot`, which tests replace with the recording session. |
| Security & data handling | **Pass.** The notes text comes from the repository, not from incoming messages, so prompt injection does not apply. It is sent as plain text with no parse mode, so nothing in it is interpreted. |
| Workflow | Spec → plan → tasks. Tests and ruff/mypy gate the PR. |

**Post-design re-check**: still passes. The design adds two tables keyed to `users` with
cascade and no content, and the CLI never contacts Telegram.

## Project Structure

### Documentation (this feature)

```text
specs/011-release-notes/
├── plan.md                         # This file
├── research.md                     # Phase 0: decisions R1-R12
├── data-model.md                   # Phase 1: tables, states, settings, i18n keys
├── quickstart.md                   # Phase 1: validation guide and release steps
├── contracts/
│   ├── release-notes-file.md       # File format, validation, message layout
│   └── operator-cli.md             # release-notes CLI commands and settings
├── checklists/requirements.md      # Spec quality checklist
└── tasks.md                        # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
release-notes/
└── 0.2.0.md                        # NEW: first announcement (beta applications, this feature)

alembic/versions/
└── 0011_release_notes.py           # NEW: two tables, two enums, indexes

src/scribe/
├── release_notes/                  # NEW package
│   ├── __init__.py
│   ├── version.py                  # Version = tuple[int,int,int]; parse(), running_version()
│   ├── notes.py                    # load/validate files; render blocks; pack digest parts (pure)
│   ├── repo.py                     # announcements/deliveries queries (caller's session, no commit)
│   ├── announce.py                 # last handled version, startup check, recipients, manual queue
│   └── sender.py                   # ReleaseNotesSender: claim → send → record; pacing; recovery;
│                                   #   register_release_notes_job(scheduler, ...)
├── access/
│   ├── repo.py                     # + bulk_status(session, settings, users, now) (moved from reconcile)
│   └── reconcile.py                # uses access.repo.bulk_status (no behaviour change)
├── db/models.py                    # + ReleaseTrigger, ReleaseDeliveryState, ReleaseAnnouncement,
│                                   #   ReleaseDelivery
├── config.py                       # + release_notes_dir/_auto/_rate_per_s/_max_attempts
├── __main__.py                     # Service.start: recover_interrupted() before the scheduler runs;
│                                   #   register_release_notes_job(...) next to access_reconcile
├── admin.py                        # + release-notes preview|check|send|status
├── i18n/en.py, i18n/ru.py          # + release_notes.heading, my_data.release_notes
├── retention/user_data.py          # + export "release_notes", summary count
└── telegram/control_bot/account_command.py   # + /my_data line

Dockerfile                          # + COPY release-notes ./release-notes
docs/release-notes.md               # NEW: writing notes, release steps, settings, CLI
docs/configuration.md               # + the four settings
README.md / README.ru.md            # + link to docs/release-notes.md in "Documentation"

tests/
├── unit/
│   ├── test_release_notes_version.py
│   ├── test_release_notes_parse.py
│   ├── test_release_notes_digest.py
│   ├── test_release_notes_pacing.py
│   └── test_release_notes_files.py # validates the real release-notes/ directory
└── integration/
    ├── release_notes_support.py    # failing-bot helpers, notes dir fixture, version injection
    ├── test_release_notes_announce.py
    ├── test_release_notes_recipients.py
    ├── test_release_notes_delivery.py
    ├── test_release_notes_admin.py
    └── test_release_notes_user_data.py
```

**Structure Decision**: a new `scribe/release_notes/` package beside `scribe/access/`. It
follows the same split: pure logic (`notes.py`, `version.py`), DB helpers that never commit
(`repo.py`), orchestration (`announce.py`, `sender.py`) and wiring in `__main__.py` and
`admin.py`.

## Design Notes

### Startup sequence (`Service.start`)

The existing order is kept. Two calls are added:

1. After `self.scheduler = Scheduler(...)` and before `scheduler.run`:
   `await release_notes.sender.recover_interrupted(self.session_factory, clock)` turns rows
   left in `sending` into `unknown`, or `pending` for the next part (research R6, R8). It is
   placed next to `requeue_interrupted_turns()`.
2. `register_release_notes_job(self.scheduler, self.session_factory, self.bot, settings,
   self._clock)` next to `register_access_reconcile`. The job object owns the
   `asyncio.Lock`, the "startup check done" flag, the pacing clock and an injectable `sleep`.

The startup check runs inside the job's first run and not in `Service.start`, so a slow or
failing check never delays polling. A failure is logged by the scheduler and retried after
`job_retry_s`.

### Startup check (`announce.ensure_auto_announcement`)

```text
running = running_version()            # None if not X.Y.Z → log and stop
last = last_handled_version(session)   # None if no rows
if last is not None and running <= last: return
notes = load_notes(settings.release_notes_dir)   # errors: log release_notes_invalid, stop
lower = last if last is not None else just-below(running)  # first deploy: running only
covered = [v for v in sorted(notes) if lower < v <= running and notes[v].has_text]
INSERT release_announcements(auto, release_version=running, versions=covered, notes_json)
  ON CONFLICT on the partial unique index DO NOTHING → another start won; stop
if covered: INSERT deliveries for recipients(session, settings, now); recipients=count
if not covered or count == 0: finished_at = now
commit; log release_notes_announced(release, versions, recipients)
```

An invalid notes file stops the automatic announcement for that start, and nothing is
recorded, so it is retried on the next start after a fix. The unit test over
`release-notes/` keeps invalid files out of `main`.

### Sender loop (`ReleaseNotesSender.drain`)

```text
async with lock (non-blocking; return if held):
  loop:
    tx1: SELECT pending WHERE next_attempt_at <= now ORDER BY created_at
         LIMIT 1 FOR UPDATE SKIP LOCKED; none → break
         load user (None → skipped no_account), access check (→ skipped no_access),
         personal versions (empty → skipped already_received), parts (empty → skipped no_text)
         else state = sending, parts_total = len(parts); commit
    for part in parts[parts_sent:]:
        await pace(); send(chat_id, part, disable_notification=True)
          RetryAfter → sleep(retry_after + 1); retry same part
          Forbidden / chat not found → tx: blocked; break
          BadRequest → tx: failed; break
          network/server → tx: attempts += 1; pending + backoff, or failed at max; break
        tx: parts_sent += 1 (sent + delivered_at on the last part)
    tx: finish announcement if no pending/sending rows remain
```

`pace()` waits until `1 / rate_per_s` seconds have passed since the previous send of the
process. Between parts of one user it waits at least 1.1 s. Both use the injected `sleep` and
`clock`.

### Shared access status

`access/reconcile.py` today has private `_betas` and `_subscriptions` loops around
`policy.status`. They move to `access/repo.py` as
`bulk_status(session, settings, users, now) -> dict[uuid.UUID, AccessStatus]`, and reconcile
calls it unchanged. The existing reconcile tests guard the refactor.

## Risks

- **Version not bumped**: without a bump nothing is ever announced. The release steps are
  documented, and `release-notes status` shows `running=` and `last_handled=`.
- **`uv.lock` drift**: bumping `pyproject.toml` without `uv lock` breaks `docker compose
  build` (`uv sync --locked`). This is in the release steps.
- **First deploy announces 0.2.0 to every beta user**: intended. The operator should review
  `release-notes/0.2.0.md`, or set `SCRIBE_RELEASE_NOTES_AUTO=false` for that deploy.
- **Migration number clash**: parallel branches use 0009, 0010, 0012 and 0013 from 0008. Renumber at merge
  so that `alembic heads` shows exactly one head (research R12).
- **Shared rate limit**: other bot traffic is not paced. At 10/s announcements leave enough
  headroom. A bot-wide limiter is a possible follow-up, not part of this feature.
- **Crash window**: at most one message per crash has an unknown result. It is counted, not
  resent, by design (FR-016).
- **Local editable installs** may report a stale version. The automatic check uses whatever
  `importlib.metadata` reports, and a non-`X.Y.Z` value disables it with a warning.

## Complexity Tracking

No constitution violations to justify.
