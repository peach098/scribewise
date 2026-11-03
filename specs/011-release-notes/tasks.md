---

description: "Task list for 011 release notes (GitHub issue #20)"
---

# Tasks: Release Notes for Users After an Update

**Input**: Design documents from `/specs/011-release-notes/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Included. The constitution (Principle V) requires automated tests for
deterministic code, written before or together with it. Within each story, write the tests
first and see them fail.

**Organization**: Tasks are grouped by user story. US1 and US2 are both P1 and together make
the MVP: do not deploy US1 without US2, or suspended users would be notified.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on unfinished tasks)
- **[Story]**: the user story from spec.md (US1-US5)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: the package skeleton, settings and image changes

- [X] T001 Create the package `src/scribe/release_notes/` with `__init__.py` (module docstring pointing to specs/011-release-notes) and empty modules `version.py`, `notes.py`, `repo.py`, `announce.py`, `sender.py`
- [X] T002 [P] Add settings to `src/scribe/config.py` under a `# --- release notes (specs/011-release-notes)` block: `release_notes_dir: Path = Path("release-notes")`, `release_notes_auto: bool = True`, `release_notes_rate_per_s: float = Field(default=10, ge=1, le=25)`, `release_notes_max_attempts: int = Field(default=5, ge=1)`; extend `tests/unit/test_config.py` with defaults and bounds (rate 0 and 26 rejected)
- [X] T003 [P] Create the directory `release-notes/` with a `README.md` that points to `contracts/release-notes-file.md` and `docs/release-notes.md` (the parser ignores non-`X.Y.Z.md` names: `README.md` is skipped, not an error), and add `COPY release-notes ./release-notes` to `Dockerfile` next to `COPY prompts ./prompts`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: schema, pure parsing and the shared access status that every story uses

**⚠️ CRITICAL**: no user story work can begin until this phase is complete

- [X] T004 [P] Write `tests/unit/test_release_notes_version.py`: `parse("0.10.0") > parse("0.9.0")`; `parse` rejects `v0.2.0`, `0.2`, `0.2.0.dev0`, `0.2.0-rc1` and leading spaces; `format` round-trips; `running_version()` returns `None` (and does not raise) for a non-`X.Y.Z` installed version
- [X] T005 [P] Implement `src/scribe/release_notes/version.py`: `Version = tuple[int, int, int]`, `parse(text) -> Version` (regex `^\d+\.\d+\.\d+$`, `ValueError` otherwise), `format(v) -> str`, `running_version(read=importlib.metadata.version) -> Version | None` (logs `release_notes_version_unparsed` with the raw value on failure) (research R2)
- [X] T006 [P] Write `tests/unit/test_release_notes_parse.py` against contracts/release-notes-file.md: both sections; a missing `ru` section → warning `missing ru section`; both empty → `has_text` false; HTML comment before the first section ignored (multi-line too); plain text before the first section → `NotesError`; `## de` → error; duplicate `## en` → error; a section of 3,501 characters → error (3,500 is fine); leading and trailing blank lines stripped, inner lines kept verbatim (`*`, `<`, `_` untouched); `load_notes(dir)` skips `README.md`, errors on `0.2.md`, and returns `{}` for a missing directory
- [X] T007 Implement parsing in `src/scribe/release_notes/notes.py`: `MAX_SECTION_CHARS = 3500`, `@dataclass(frozen=True) ReleaseNotes(version, texts: dict[Lang, str], warnings: list[str])` with `has_text`, `text_for(lang)` (the user's language, else the other one, else `None`; FR-011), `parse_notes(version, text) -> ReleaseNotes`, `load_notes(dir) -> dict[Version, ReleaseNotes]` (raises `NotesError` naming the file); depends on T005
- [X] T008 Add the models to `src/scribe/db/models.py` (new section `# --- release notes (specs/011-release-notes)`): enums `ReleaseTrigger(auto, manual)` and `ReleaseDeliveryState(pending, sending, sent, blocked, failed, skipped, unknown)`; `ReleaseAnnouncement` (`id` UUID PK, `release_version` text not null, `versions` ARRAY(Text) not null, `notes_json` JSONB not null, `trigger` not null, `created_at` timestamptz not null, `finished_at` timestamptz null, `recipients` int not null default 0; partial unique index `uq_release_announcements_auto_version` on `release_version` WHERE `trigger = 'auto'`); `ReleaseDelivery` (`_Timestamps`; `id` UUID PK; `announcement_id` FK ON DELETE CASCADE not null; `user_id` FK `users.id` ON DELETE CASCADE not null; `state` not null default `pending`; `parts_total` smallint null; `parts_sent` smallint not null default 0; `attempts` smallint not null default 0; `next_attempt_at` timestamptz not null; `delivered_at`, `skip_reason`, `error` nullable; unique `(announcement_id, user_id)`; partial index `ix_release_deliveries_due` on `next_attempt_at` WHERE `state = 'pending'`; index `ix_release_deliveries_user` on `user_id`) per data-model.md
- [X] T009 Write the migration `alembic/versions/0011_release_notes.py` (`revision = "0011"`; numbered 0011 because parallel branches use 0009/0010/0012/0013 from 0008; originally planned as 0009, `down_revision = "0008"`) creating both enums, both tables and all indexes, with a downgrade that drops them; check `uv run alembic heads` shows one head (research R12); depends on T008
- [X] T010 [P] Move the bulk access computation from `src/scribe/access/reconcile.py` (`_betas`, `_subscriptions` and the per-user `policy.status` call) to `src/scribe/access/repo.py` as `bulk_status(session, settings, users, *, now) -> dict[uuid.UUID, policy.AccessStatus]`, and make `reconcile_access` and `_remind` use it with no behaviour change; `tests/integration/test_access_reconcile.py` must still pass unchanged
- [X] T011 [P] Add the i18n keys `release_notes.heading` (`What's new in Scribe {version}` / `Что нового в Scribe {version}`) and `my_data.release_notes` (`Release notes received: {n}` / `Получено обновлений: {n}`) to `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`; `tests/unit/test_i18n_lint.py` must pass
- [X] T012 [P] Extend `tests/integration/bot_support.py` so that `RecordingSession` can be given a queue of outcomes per call (an aiogram exception instance such as `TelegramRetryAfter`, `TelegramForbiddenError`, `TelegramBadRequest` or `TelegramNetworkError`, or success), and records `disable_notification` and `parse_mode` of each `SendMessage`; then create `tests/integration/release_notes_support.py` with fixtures for a temporary notes directory (`write_notes(version, en=..., ru=...)`), an injectable running version, a fake `sleep` that records the delays and advances `FrozenClock`, and `make_access_user(kind=beta|subscribed|operator|suspended|none, lang=...)`
- [X] T013 Implement `src/scribe/release_notes/repo.py` (caller's session, never commits): `last_handled_version(session) -> Version | None` (max of parsed `release_version`), `create_announcement(...)` using `INSERT ... ON CONFLICT DO NOTHING` on the partial unique index for `auto` and returning `None` on conflict, `add_deliveries(session, announcement, user_ids, now)`, `received_versions(session, user_id, *, exclude_announcement) -> set[Version]` (states `sent`/`unknown`), `claim_next_due(session, now)` (`FOR UPDATE SKIP LOCKED`, oldest first, limit 1), `finish_if_done(session, announcement_id, now)`, `counts_by_state(session, announcement_id)`; depends on T008

**Checkpoint**: schema migrated, notes parse, the access status is shared, and the test helpers exist

---

## Phase 3: User Story 1 - Users learn what is new after an update (Priority: P1) 🎯 MVP (with US2)

**Goal**: on startup after a version bump, every user with an account gets one digest of the
new notes in their language. Restarts and same-version redeploys send nothing.

**Independent Test**: two users (en, ru), a notes file newer than the last handled version:
start → one message each; restart → nothing (spec US1).

### Tests for User Story 1 ⚠️

- [X] T014 [P] [US1] Write `tests/unit/test_release_notes_digest.py` for `build_parts(notes_by_version, versions, lang) -> list[str]`: one version gives heading `release_notes.heading` + blank line + text; two versions oldest first, separated by one blank line; the language fallback; a version with no text is left out; parts never exceed 4,096 characters and split only between versions (three 3,500-character versions → three parts)
- [X] T015 [P] [US1] Write `tests/integration/test_release_notes_announce.py` covering spec US1 scenarios 1-6 and the edge cases: first deploy (empty table) announces only the running version even when older files exist; a newer running version → one `auto` row with `versions` = the versions in `(last, running]` that have text, deliveries for every user, `recipients` set; the same version again → no new row (also when two `ensure_auto_announcement` calls race: one row); no notes → a row with `versions = []` and `finished_at` set, no deliveries; rollback (running < last) → nothing; `release_notes_auto = False` → nothing written; an invalid notes file → nothing written and `release_notes_invalid` logged; a running version of `None` → nothing written
- [X] T016 [P] [US1] Write the happy-path part of `tests/integration/test_release_notes_delivery.py`: after an announcement, `ReleaseNotesSender.drain()` sends one `SendMessage` per user to `telegram_control_chat_id` with the user's language, `disable_notification=True` and no `parse_mode`; rows end `sent` with `delivered_at` and `parts_sent = 1`; the announcement gets `finished_at`; a second `drain()` sends nothing; logs contain no notes text (use `log_capture`)

### Implementation for User Story 1

- [X] T017 [US1] Implement `build_parts` (and a `render_block(version, text, lang)` helper using `Msg("release_notes.heading", version=...)`) in `src/scribe/release_notes/notes.py` per contracts/release-notes-file.md "Message to a user"; `TELEGRAM_MAX_CHARS = 4096`
- [X] T018 [US1] Implement `src/scribe/release_notes/announce.py`: `ensure_auto_announcement(session_factory, settings, *, now, running) -> ReleaseAnnouncement | None` per plan.md "Startup check" (pseudo-code), with a `recipients(session, settings, now) -> list[uuid.UUID]` that for now returns every `users` row (US2 makes it access-aware); `notes_json` is the snapshot `{ "X.Y.Z": {"en": ..., "ru": ...} }` of the covered versions; log `release_notes_announced` with `release`, `versions` and `recipients` only (FR-023)
- [X] T019 [US1] Implement the basic `ReleaseNotesSender` in `src/scribe/release_notes/sender.py`: constructor `(session_factory, bot, settings, *, clock, sleep=asyncio.sleep, running=running_version)`; `run_once()` performs the startup check on the first call only, then `drain()`; `drain()` holds a non-blocking `asyncio.Lock` (returns if held) and loops tx1 (claim → load user → personal versions = announcement versions − `received_versions` → `build_parts` from `notes_json` → `sending` with `parts_total = len(parts)`, or `skipped` with `skip_reason` `no_account`/`already_received`/`no_text`) → send each remaining part with `disable_notification=True` → tx2 (`parts_sent += 1`, `sent` + `delivered_at` after the last part) → `finish_if_done`; log `release_notes_delivery` (delivery id, user id, result) and `release_notes_finished` (counts)
- [X] T020 [US1] Add `register_release_notes_job(scheduler, session_factory, bot, settings, clock)` in `src/scribe/release_notes/sender.py` (job name `release_notes`, `Every(timedelta(seconds=15))`, `run_at_start=True`) and call it from `Service.start` in `src/scribe/__main__.py` right after `register_access_reconcile(...)`; extend `tests/integration/test_service.py` to assert the job is registered
- [X] T021 [US1] Write the first notes file `release-notes/0.2.0.md` (en + ru, user-facing, per the example in contracts/release-notes-file.md: in-bot beta applications from specs/005 US5, and "after each update the bot tells you what's new") and bump `version` in `pyproject.toml` to `0.2.0` with `uv lock`; mark the PR description "operator: review the 0.2.0 text before deploy"

**Checkpoint**: US1 works end to end (with every account as a recipient)

---

## Phase 4: User Story 2 - Only people with access are notified (Priority: P1) 🎯 MVP

**Goal**: recipients are the users with access (specs/005), and access is checked again
right before each send.

**Independent Test**: gate on, with one each of beta, subscriber, operator, suspended,
refused sender and applicant: only beta, subscriber and operator get the message (spec US2).

### Tests for User Story 2 ⚠️

- [X] T022 [P] [US2] Write `tests/integration/test_release_notes_recipients.py`: gate on → deliveries only for beta, subscriber and operator accounts, not for a suspended user (`access_suspended_at` set) or an account without access whose suspension `access_reconcile` has not applied yet; a refused contact (`refused_contacts` row only) and a pending `beta_applications` row with no account get no delivery; gate off → every account, including a stale `access_suspended_at`; `setting_up`, `paused` and `out_of_office` users are included; a user created after the announcement gets no delivery
- [X] T023 [P] [US2] Add to `tests/integration/test_release_notes_delivery.py`: a user who loses access (beta entry removed) between the announcement and the send ends `skipped` with `skip_reason = "no_access"` and gets no message; a user deleted in between has no delivery row left (cascade) and nothing is sent

### Implementation for User Story 2

- [X] T024 [US2] Make `recipients()` in `src/scribe/release_notes/announce.py` access-aware per research R4: gate on → users with `access_suspended_at IS NULL` whose `access_repo.bulk_status(...)` has access; gate off → every user; `users.state` ignored
- [X] T025 [US2] Add the pre-send access check to tx1 in `src/scribe/release_notes/sender.py`: compute the user's status with `access_repo.bulk_status(session, settings, [user], now=now)` (gate on) and check `access_suspended_at`; no access → `skipped` / `no_access` (FR-010)

**Checkpoint**: MVP complete. Deployable once Phase 8 privacy tasks T040-T041 are done.

---

## Phase 5: User Story 3 - Sending is safe at any scale (Priority: P2)

**Goal**: Telegram limits are respected, `RetryAfter` is honoured, blocked users are
recorded, and a restart resumes with no duplicates.

**Independent Test**: many users and a bot that answers some sends with "retry after" and
one with "blocked"; stop halfway and restart; each due user gets exactly one message
(spec US3).

### Tests for User Story 3 ⚠️

- [X] T026 [P] [US3] Write `tests/unit/test_release_notes_pacing.py` for the `Pacer` (injected `clock` and `sleep`): at `rate_per_s = 10`, 50 sends make the fake sleeps total at least 4.9 s; two parts to the same chat are at least 1.1 s apart; `backoff(attempts)` gives 30 s, 2 min, 10 min, 30 min, 30 min; the `classify(exc)` table of research R8 (`TelegramRetryAfter` → retry_after, `TelegramForbiddenError` → blocked, `TelegramBadRequest` "chat not found" and `TelegramNotFound` → blocked, other `TelegramBadRequest` → failed, `TelegramNetworkError`/`TelegramServerError`/other `TelegramAPIError` → retryable)
- [X] T027 [P] [US3] Add to `tests/integration/test_release_notes_delivery.py`: `TelegramRetryAfter(retry_after=3)` once → a fake sleep of at least 4 s, the same part resent, `attempts` stays 0, row `sent`; `TelegramForbiddenError` → `blocked` with `error = "TelegramForbiddenError"`, and the other users are still delivered; `TelegramNetworkError` → `pending`, `attempts = 1`, `next_attempt_at = now + 30 s`, then `failed` after `release_notes_max_attempts`; other `TelegramBadRequest` → `failed` at once; the log has no text, chat id or username
- [X] T028 [P] [US3] Add crash-recovery tests to `tests/integration/test_release_notes_delivery.py`: a row left in `sending` with `parts_sent = 0` of a one-part digest → `recover_interrupted()` makes it `unknown`, `drain()` does not resend, and a later announcement covering that version skips it (`already_received`); a two-part digest with `parts_sent = 0` left in `sending` → part 1 counts as sent, back to `pending` with `parts_sent = 1`, and `drain()` sends only part 2; the same digest with `parts_sent = 1` left in `sending` → `unknown`, nothing resent; with 20 recipients and a send that raises `CancelledError` at the 8th, a restart (new sender + `recover_interrupted`) leaves 7 `sent`, 1 `unknown` (the 8th, not resent) and delivers the remaining 12, and no chat gets two messages
- [X] T029 [P] [US3] Add to `tests/integration/test_release_notes_delivery.py`: while `drain()` holds the lock, a second `run_once()` returns at once without sending (no overlap); a long drain does not block the scheduler (`test_scheduler.py`-style: another job still runs during the drain)

### Implementation for User Story 3

- [X] T030 [US3] Implement `Pacer` (`min_interval = 1 / rate_per_s`, `PER_CHAT_GAP_S = 1.1`), `backoff(attempts)` and `classify(exc)` in `src/scribe/release_notes/sender.py` per research R7-R8
- [X] T031 [US3] Wire pacing and error handling into `drain()` in `src/scribe/release_notes/sender.py`: `await pacer.wait(chat_id)` before each part; `RetryAfter` → `sleep(retry_after + 1)` and retry the same part with no attempt counted; blocked/failed/retryable outcomes recorded in their own short transaction (`attempts`, `next_attempt_at`, `error` = class name, `failed` at `release_notes_max_attempts`); `finish_if_done` after every final state
- [X] T032 [US3] Implement `recover_interrupted(session_factory, *, now) -> int` in `src/scribe/release_notes/sender.py` (rows in `sending`: the part in doubt counts as sent; `unknown` when `parts_sent + 1 >= parts_total`, otherwise `pending` with `parts_sent + 1`, per data-model.md state machine; log `release_notes_recovered` with counts) and call it in `Service.start` (`src/scribe/__main__.py`) next to `await self.scheduler.requeue_interrupted_turns()`, before `scheduler.run`

**Checkpoint**: delivery is rate-limited, resilient and resumable

---

## Phase 6: User Story 4 - The operator controls the announcement (Priority: P2)

**Goal**: `release-notes preview|check|send [--dry-run]|status` in the operator CLI, and
automatic sending can be turned off.

**Independent Test**: preview shows both languages with warnings; a dry run shows the counts
and sends nothing; a real send reaches users; with auto off a new version sends nothing
(spec US4).

### Tests for User Story 4 ⚠️

- [X] T033 [P] [US4] Write `tests/integration/test_release_notes_admin.py` (in-process `scribe.admin.run(...)` with `capsys`, as in `tests/integration/test_admin_access.py`) for contracts/operator-cli.md: `preview 0.2.0` prints `--- en ---` and `--- ru ---` blocks with the exact heading and text, a `warning:` line and the fallback text when `ru` is missing, exit 1 for a missing or invalid file, and the `note:` line for a version newer than the running one; `check` → exit 1 when any file is invalid; `send 0.2.0 --dry-run` prints `dry run 0.2.0: N recipients (en a, ru b), k already have it` and writes no rows; `send 0.2.0` creates one `manual` announcement with `pending` rows for recipients without 0.2.0 (users who got 0.2.0 in an auto digest are excluded) and prints the "running service" line; `send` with nobody left → exit 1 `nothing to send`; `send` of a version with no text → exit 1; `status` prints the `running= last_handled= auto=` line and per-state counts, and `status --all` lists every announcement newest first; `release-notes send v0.2.0` → exit 2
- [X] T034 [P] [US4] Add to `tests/integration/test_release_notes_announce.py`: with `release_notes_auto = False`, a newer version writes nothing at startup, and a later manual `send` is delivered by `drain()`; a manual send of the running version before the start means the start sends nothing more (`last_handled` covers it)

### Implementation for User Story 4

- [X] T035 [US4] Implement `queue_manual(session, settings, version, notes, *, now) -> QueueResult` (recipients from `recipients()`, minus users whose `received_versions` contain `version`, plus per-language counts) and `status_lines(session, *, show_all, running, settings)` in `src/scribe/release_notes/announce.py`
- [X] T036 [US4] Add the `release-notes` command group to `src/scribe/admin.py` (`_parser`, `run`, module docstring) per contracts/operator-cli.md: `preview <version>`, `check`, `send <version> [--dry-run]` (dry run = same transaction, rolled back), `status [--all]`; `<version>` parsed with `version.parse` as an argparse type (exit 2 on bad input); no bot is created for these commands

**Checkpoint**: the operator can preview, send by hand, dry-run and check progress

---

## Phase 7: User Story 5 - A release has a version and notes (Priority: P3)

**Goal**: release steps are documented, and the automated checks reject bad notes files.

**Independent Test**: follow the documented steps for a test version; an invalid file fails
the tests (spec US5).

- [X] T037 [P] [US5] Write `tests/unit/test_release_notes_files.py`: `load_notes(Path("release-notes"))` over the real repository directory raises no `NotesError`, every `X.Y.Z.md` has both `en` and `ru` text (warnings are failures for files in the repo), and the newest notes version is not newer than `pyproject.toml`'s `[project].version` (read with `tomllib`)
- [X] T038 [P] [US5] Write `docs/release-notes.md`: what release notes are for, how to write them (audience, plain text, length limit, both languages), the release steps from quickstart.md "Release steps" (bump, `uv lock`, notes file, `preview`, tag `v<version>`, deploy, `status`), the four settings and the CLI commands, and what happens on restart, rollback and crash (`unknown`)
- [X] T039 [P] [US5] Add the four `SCRIBE_RELEASE_NOTES_*` settings to `docs/configuration.md` and `.env.example` (if present), and link `docs/release-notes.md` from the "Documentation" sections of `README.md` and `README.ru.md`

**Checkpoint**: releases are repeatable, and bad notes files cannot reach `main`

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: data-subject rights (required before deploy) and final checks

- [X] T040 Write `tests/integration/test_release_notes_user_data.py`: after a delivery, `export_user_data` has `release_notes` = `[{"versions": ["0.2.0"], "state": "sent", "delivered_at": ...}]`; `data_summary(...).release_notes == 1` and `/my_data` shows `my_data.release_notes`; `delete_user_data` removes the user's deliveries and keeps the announcement (FR-024)
- [X] T041 Implement the export key and summary count in `src/scribe/retention/user_data.py` (module docstring updated; query scoped to `user_id`, versions taken from the joined announcement) and the `/my_data` line in `src/scribe/telegram/control_bot/account_command.py`; depends on T040
- [X] T042 [P] Review every new log call in `src/scribe/release_notes/` against FR-023 (ids, versions, counts and error class names only) and add a case to `tests/unit/test_logging_redaction.py` if that test enumerates event names
- [ ] T043 (automated part done: ruff, mypy and the full test suite pass; the manual walkthrough needs a live bot) Run `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`, and walk through quickstart.md "Manual check on a local instance" steps 1-6

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: after Setup. Blocks all stories. T007 depends on T005, T009 on
  T008, T013 on T008.
- **US1 (Phase 3)**: after Foundational.
- **US2 (Phase 4)**: after US1 (it changes `recipients()` and the tx1 of `drain()` written in
  US1).
- **US3 (Phase 5)**: after US1. Independent of US2, so it can run in parallel with US2 if
  the two people coordinate on `sender.py`.
- **US4 (Phase 6)**: after US2 (`queue_manual` reuses the access-aware `recipients()`). It
  can run in parallel with US3: `admin.py` and `announce.py` vs `sender.py`.
- **US5 (Phase 7)**: T037 after T007 (and T021 for a real file); docs any time after the
  contracts are stable.
- **Polish (Phase 8)**: T040-T041 after T008 and US1. They are required before the first
  deploy.

### Within Each User Story

- Tests first, failing; then implementation; then the checkpoint.
- `notes.py` before `announce.py` before `sender.py` before the wiring in `__main__.py` and
  `admin.py`.

### Parallel Opportunities

- Phase 1: T002 and T003.
- Phase 2: T004, T006, T010, T011 and T012 together; then T005 → T007, and T008 → T009 / T013.
- US1: T014, T015 and T016 together.
- US2: T022 and T023 together.
- US3: T026-T029 together.
- US4: T033 and T034 together; US4 alongside US3.
- US5: T037, T038 and T039 together.

---

## Parallel Example: User Story 3

```text
Task: "T026 [US3] tests/unit/test_release_notes_pacing.py (Pacer, backoff, classify)"
Task: "T027 [US3] RetryAfter / Forbidden / network cases in tests/integration/test_release_notes_delivery.py"
Task: "T028 [US3] crash-recovery cases in tests/integration/test_release_notes_delivery.py"
Task: "T029 [US3] no-overlap and non-blocking cases in tests/integration/test_release_notes_delivery.py"
```

(T027-T029 touch the same file: run them in parallel only when each adds its own test
functions.)

---

## Implementation Strategy

### MVP First (US1 + US2 + T040-T041)

1. Phase 1 and Phase 2.
2. US1: announce on startup, basic delivery.
3. US2: access-aware recipients and the pre-send check.
4. T040-T041: `/my_data` and deletion.
5. **Stop and validate** with quickstart.md steps 1-5. This is enough for ~30 beta users,
   since one sequential send cannot hit Telegram's limits at that scale.

### Incremental Delivery

1. MVP → deploy 0.2.0 (the first announcement).
2. US3 → safe at scale (pacing, retry after, crash recovery).
3. US4 → operator CLI (preview before releases, manual send, status).
4. US5 → documented release process and the repository check.

Recommended single PR order: Phases 1-4, then 8 (T040-T041), 5, 6, 7 and 8 (T042-T043). Do
not deploy before US3 if the user count has grown well beyond the beta.

---

## Notes

- [P] tasks touch different files and depend on no unfinished task.
- Never log the notes text, chat ids or usernames (FR-023, constitution Principle II).
- The CLI never calls Telegram. Only the running service sends (research R6, R10).
- Commit after each task or logical group.
