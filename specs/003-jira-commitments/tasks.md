# Tasks: Jira Task Source with Comment Commitments

**Input**: Design documents from `/specs/003-jira-commitments/`

**Prerequisites**: plan.md, spec.md, research.md (J0 lists what the base branch provides),
data-model.md, contracts/, quickstart.md. Base branch: `003-jira-commitments` =
`origin/issue-6-trello-comment-deadlines` (feature 001 US3 + issue #6) merged with `origin/main`.
Ids of feature 001 tasks are written `001-Txxx`; unprefixed ids are this feature's.

**Tests**: Included (constitution Principle V). Test names match `quickstart.md`.

**Organization**: US1 (connect Jira, issues as tasks incl. comment deadlines) is the MVP; US2
(promises in the user's Jira comments) needs US1's client; US3 (date mismatches) needs US2's
issue-linked commitments.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- Every Jira test runs for `cloud` and `self_hosted` unless it names one (FR-J00, SC-008).

---

## Phase 1: Setup

- [X] T001 Verify the base: `src/scribe/context/tasks/{base,service,trello,deadlines}.py`, `src/scribe/context/commitments.py`, `src/scribe/telegram/control_bot/{tasks_command,commitments_command}.py` exist and `uv run pytest tests/unit tests/contract -q` passes on `003-jira-commitments`
- [X] T002 Create package `src/scribe/context/tasks/jira/` (`__init__.py`, `source.py`, `client.py`, `flavors.py`, `bodies.py`, `netguard.py`, `tls.py`, `jql.py`, `sync.py`) replacing the stub module `src/scribe/context/tasks/jira.py`; `__init__.py` re-exports `JiraTaskSource`; remove the stub test(s) asserting `NotImplementedError` (`grep -rn "release version\|NotImplementedError" tests`), and the `NotImplementedError` special case in `src/scribe/context/tasks/service.py` docs
- [X] T003 [P] Add settings to `src/scribe/config.py`: `jira_dc_min_version: str = "10.3"` (must parse and be ≥ 8.14), `jira_allowed_private_hosts: list[str] = []` (comma-separated env `SCRIBE_JIRA_ALLOWED_PRIVATE_HOSTS`; hostnames or CIDRs), `jira_sync_interval_s: int = 300` (ge 60), `jira_sync_concurrency: int = 5` (ge 1), `jira_lookback_days: int = 30` (ge 1), `jira_first_scan_max_issues: int = 1000` (ge 1); document them in `.env.example`; extend `tests/unit/test_config.py`
- [X] T004 [P] Synthetic fixtures (Principle II) in `tests/contract/fixtures/jira/`: `render.py` builds Cloud (v3, ADF bodies) and DC (v2, wiki bodies; `serverInfo` 10.3.4 and 11.3.1) JSON responses from one neutral description: `tenant_info`, `myself` (with `timeZone`), `serverInfo`, project list, tasks search page(s) with `comment`, `description`, `changelog` (a `duedate` change), scan search, per-issue comments, an issue in Done; comments include a mention, a quote, a restricted comment and an edited comment

## Phase 2: Foundational

- [X] T005 [P] Unit tests `tests/unit/test_every_schedule.py`: `Every(timedelta(minutes=5)).next_after(t) == t + 5 min`; non-positive interval → `ValueError`; `next_run(Every(...), t)`
- [X] T006 [P] Integration tests `tests/integration/test_jira_models.py` (`test_migration_0003_*`): upgrade from `0002` keeps existing commitments (backfills `closed_by='user'` for closed rows), `commitments.contact_id` accepts NULL, partial unique `(user_id, jira_comment_id)` among open rows, `jira_comment_cursor` unique `(user_id, comment_id)`, user delete cascades, downgrade works; existing model/repo/retention tests still pass
- [X] T007 Add to `src/scribe/db/models.py` per data-model.md: enums `CommitmentSourceKind` (`telegram_message`, `jira_comment`), `CommitmentAutoNote` (`issue_done`, `source_removed`, `source_changed`, `source_unreachable`), `CommitmentClosedBy` (`user`, `system`), `JiraCursorOutcome` (`no_commitment`, `commitment`, `merged`, `skipped`); `Commitment`: `contact_id` nullable, `to_whom` (encrypted like `what`, nullable), `source_kind` (default `telegram_message`), `jira_issue_key`, `jira_comment_id`, `jira_comment_url` (text, nullable), `restricted` (bool, default false), `auto_note`, `closed_by` (nullable), `user_modified` (bool, default false); `TaskSourceConnection`: `next_sync_at`, `sync_watermark` (timestamptz, nullable); new model `JiraCommentCursor(user_id, issue_key, comment_id, comment_updated_at, body_hmac bytea, outcome)` with unique `(user_id, comment_id)`
- [X] T008 Write `alembic/versions/0004_jira_commitments.py` (`down_revision="0003"`) per data-model.md "Migration notes" (depends on T007)
- [X] T009 [P] `comment_hmac(text) -> bytes` in `src/scribe/crypto.py` (HMAC-SHA256, key = HKDF-SHA256 of the master key with `info=b"jira-comment-hmac"`); test in `tests/unit/test_crypto.py`
- [X] T010 [P] `src/scribe/db/repos/jira_cursor.py` (all take `user_id`): `get_many(comment_ids)`, `upsert(...)`, `delete_for_user()`, `delete_older_than(cutoff)` (depends on T007)
- [X] T011 [P] Extend `src/scribe/db/repos/commitments.py`: `add_commitment(..., contact_id=None, to_whom=None, source_kind, jira_issue_key, jira_comment_id, jira_comment_url, restricted)`; `get_by_comment(user_id, comment_id, *, any_state=False)`; `list_open_for_issues(user_id, keys)`; `list_open_jira(user_id)`; `close(..., closed_by=user, auto_note=None)` (existing callers keep `closed_by=user`); `reopen(user_id, id)`; `list_recently_closed_by_system(user_id, since)`; make existing readers tolerate `contact_id IS NULL` (`/commitments` shows `to_whom`) (depends on T007)
- [X] T012 [P] Extend `src/scribe/db/repos/tasks_conn.py`: `claim_due_jira(session, now, limit)` (`provider='jira' AND status != 'disconnected' AND (next_sync_at IS NULL OR next_sync_at <= now)`, `FOR UPDATE SKIP LOCKED`, sets `next_sync_at = now + interval` before returning ids) and `set_sync_state(session, user_id, *, watermark=None, next_sync_at=None)`; `delete_connection` also deletes `jira_comment_cursor` rows of the user (depends on T007)
- [X] T013 [P] Add `Every(interval: timedelta)` to `src/scribe/scheduling/scheduler.py` (`Schedule = Daily | Weekly | Every`) (makes T005 pass)
- [X] T014 [P] Extend `src/scribe/context/tasks/base.py`: `Task.due_is_date: bool = False` (honoured by `due_on()`); `TaskSourceIdentity.time_zone`, `server_version` (optional); Jira exceptions per contracts/internal-interfaces.md (subclasses of `TaskSourceUnavailable` with `error_code`, plus `ReadOnlyViolation(RuntimeError)`, `CertInfo`); update `src/scribe/context/tasks/deadlines.py` so the due-field candidate uses `due_at.date()` when `due_is_date`; tests in `tests/unit/test_task_deadlines.py`
- [X] T015 Extend `src/scribe/retention/purge.py`: delete `jira_comment_cursor` rows 30 days after `comment_updated_at`; extend `tests/integration/test_retention.py` (depends on T008, T010)

**Checkpoint**: migration applies, base test suite green.

---

## Phase 3: User Story 1 - Connect Jira and use its issues as task context (P1) 🎯 MVP

**Goal**: `/tasks` connects Jira Cloud or self-hosted Jira, picks ≤ 20 projects; drafts use open
assigned issues with their effective deadlines (due field or a newer comment deadline); GET-only;
clear errors; disconnect removes credentials.

**Independent Test**: quickstart steps 1–3, 2a, 10, 11 on both deployment types.

### Tests for User Story 1

- [X] T016 [P] [US1] `tests/unit/test_netguard.py` (`test_address_guard_blocks_private_and_metadata`): blocks `127.0.0.1`, `::1`, `10.0.0.5`, `172.16.0.1`, `192.168.1.1`, `169.254.169.254`, `100.64.0.1`, `fc00::1`, `fe80::1`, `0.0.0.0`, multicast; blocks a host if any resolved address is blocked; allows public; allowlist by host and CIDR; `validate_site_url` rejects `http://`, userinfo, query, fragment, empty host; accepts `https://jira.example.com:8443/jira`
- [X] T017 [P] [US1] `tests/unit/test_jira_jql.py`: `jql_datetime` in a zone; `tasks_jql`, `scan_jql` exact strings (research J7/J8); keys validated `^[A-Z][A-Z0-9_]+$` and quoted; `parse_version`
- [X] T018 [P] [US1] `tests/unit/test_jira_bodies.py`: ADF and wiki renderings of the same neutral thread give the same `plain`; quotes (`{quote}`, `bq.`, ADF `blockquote`) go to `quoted`; ADF `mention` / wiki `[~user]`, `[~accountid:…]` → `@Name` + `mentions`; lists, code, panels, tables, links degrade to text; unknown nodes skipped
- [X] T019 [P] [US1] `tests/contract/test_jira_client_read_only.py` (`test_jira_client_rejects_non_get`): POST/PUT/PATCH/DELETE raise `ReadOnlyViolation` before I/O; redirects are not followed; every `JiraClient` method only hits GET routes (respx), both flavors
- [X] T020 [P] [US1] `tests/contract/test_jira_task_source.py` parametrized `[cloud|self_hosted]` (`test_flavors_same_scenarios`, `test_validate_error_mapping`): Cloud `validate` reads `cloudId` from `tenant_info` and calls the gateway `/rest/api/3/myself` with Basic auth; DC reads `serverInfo` then `/rest/api/2/myself` with Bearer; DC 9.12 → `JiraVersionUnsupported`; 401 → `JiraAuthFailed`; gateway 401 + site 200 → `hint="classic_token"`; connect error → `JiraUnreachable`; 429 → `JiraRateLimited(retry_after_s)`; `list_containers` returns projects; `list_open_tasks` maps to identical `Task`s for both flavors (`id="PAY-142"`, `due_is_date=True`, last 5 comments oldest first without quotes, restricted comment excluded, `description`, `due_set_at` from changelog), follows Cloud `nextPageToken` / DC `startAt`, caps at 100, caches 60 s; no token or URL in exception messages
- [X] T021 [P] [US1] `tests/contract/test_jira_tls_pinning.py` (`test_pinned_cert_accepts_same_rejects_changed`): local TLS server (self-signed via `cryptography`, `localhost` allowlisted): unpinned → `JiraCertificateUntrusted` with matching sha256; `fetch_leaf_certificate` sends no HTTP bytes; pinned works; swapped cert fails; hostname mismatch fails
- [X] T022 [P] [US1] `tests/integration/test_jira_connect_flow.py` (bot support from `tests/integration/bot_support.py` / `us3_support.py`, respx): Cloud and self-hosted flows incl. certificate confirmation; token messages deleted; credentials encrypted at rest; `config_json` has no secrets; 0 projects refused, 21st refused; Trello→Jira replacement asks first; each error message per contracts/control-chat.md; disconnect deletes connection and cursor rows
- [X] T023 [P] [US1] `tests/integration/test_us3_jira_drafts.py::test_jira_task_cited_as_source` and `::test_jira_comment_deadline_wins` (a comment "deadline moved to Nov 5" newer than the due change → draft context `due` 5 Nov with the comment note; card label shows it)

### Implementation for User Story 1

- [X] T024 [P] [US1] `src/scribe/context/tasks/jira/netguard.py` (research J4): `validate_site_url`, `check_address(host, port, allowlist)` (raises `JiraUnreachable(blocked=True)`), `GuardedTransport(httpx.AsyncHTTPTransport)` that resolves and checks the host per request and connects to the checked address (SNI/Host keep the hostname)
- [X] T025 [P] [US1] `src/scribe/context/tasks/jira/tls.py` (research J5): `fetch_leaf_certificate(host, port) -> CertInfo`, `cert_info(pem)`, `pinned_ssl_context(pem)` (`cadata`, `VERIFY_X509_PARTIAL_CHAIN`, hostname check on); map verify errors to `JiraCertificateUntrusted`
- [X] T026 [P] [US1] `src/scribe/context/tasks/jira/jql.py` and `src/scribe/context/tasks/jira/bodies.py` (makes T017, T018 pass)
- [X] T027 [US1] `src/scribe/context/tasks/jira/flavors.py` (`CloudFlavor`, `SelfHostedFlavor`, `flavor_for`) and `src/scribe/context/tasks/jira/client.py` (`build_http_client` with GET-only hook, no redirects, timeouts, guarded transport, pinned context; `JiraClient` methods per contract; HTTP → typed errors) (depends on T024–T026)
- [X] T028 [US1] `src/scribe/context/tasks/jira/source.py` `JiraTaskSource` (`validate`, `list_containers`, `list_open_tasks` per research J7, `cached_tasks`, `invalidate`); register it in `src/scribe/context/tasks/service.py` defaults; add public `record_result()`; `src/scribe/telegram/control_bot/render.py` task label prefixes the issue key (`PAY-142 Fix refund rounding`) (depends on T027)
- [X] T029 [US1] `src/scribe/telegram/control_bot/jira_connect.py` + `tasks_command.py`: provider buttons when not connected (`tasks:connect` Trello, `jira:connect` Jira), replacement confirmation `jira:replace`, `JiraStates` FSM (`jdep:cloud|self` → site → email → token | pat → cert card `jcert:<sha256[:16]>` → picker), token messages deleted, validation errors per contract, store `provider=jira`, `credentials`, `config_json` (deployment, site_url, api_base_url, cloud_id, identity, server_version, pinned_cert), `next_sync_at=now`; generalize the picker in `tasks_command.py` to Jira projects (`config_json.project_keys`, cap 20); status text per provider; register the router in `settings.py`; idle-only like the Trello buttons (depends on T028)

**Checkpoint**: US1 works for both flavors; `make lint typecheck test-fast` green.

---

## Phase 4: User Story 2 - Find my promises in Jira comments (P1)

**Goal**: the comment sync turns the user's own promises (and accepted requests) in Jira comments
into commitments with links, merged with Telegram ones, kept in step with edits, deletions and
issue completion, listed in `/commitments` with Reopen/Restore.

**Independent Test**: quickstart steps 4, 5, 7, 8.

### Tests for User Story 2

- [X] T030 [P] [US2] `tests/unit/test_jira_commitment_merge.py`: merge when counterparty and due match and `what` is equal or word overlap ≥ 0.6, not otherwise; counterparty name → contact match is case/diacritics-insensitive; no match → `contact_id=None`, `to_whom` set; `issue_keys_in` only returns selected project keys
- [X] T031 [P] [US2] `tests/integration/test_jira_sync.py` (Postgres + respx + fake LLM, per flavor): `test_own_promise_creates_commitment_with_link`, `test_unaccepted_request_creates_nothing`, `test_accepted_request_uses_request_date`, `test_comment_edit_updates_not_duplicates`, `test_comment_delete_resolves_restorable` (via verify), `test_user_modified_not_overwritten`, `test_user_deleted_not_recreated`, `test_issue_done_auto_resolves`, `test_unreachable_keeps_open`, `test_unchanged_comment_not_resent_to_llm`, `test_other_peoples_comments_never_sent` (fake LLM calls hold only the user's comment and ≤ 1 request), `test_restricted_comment_has_no_excerpt`, `test_to_whom_falls_back_to_reporter`, `test_first_scan_30_days_and_cap`, `test_watermark_overlap_and_timezone`, `test_sync_rate_limit_backoff`, `test_sync_failure_counts_toward_task_source_error`, `test_sync_skips_locked_connection`, `test_comment_picked_up_within_two_runs`, `test_no_comment_text_in_logs`, `test_telegram_and_jira_merge`
- [X] T032 [P] [US2] `tests/integration/test_jira_commitments_view.py`: `test_commitments_view_sources_and_restore` (source lines with links, `(restricted)`, "Recently closed by Scribe", unreachable note, `cmtreopen:<id>` reopens with `user_modified=true`), `test_cmtreopen_rejects_other_users_commitment`; `test_telegram_message_with_issue_key_sets_issue_key`

### Implementation for User Story 2

- [X] T033 [P] [US2] `src/scribe/llm/provider.py`: `IssueInfo{key, title}`; `CommitmentRequest.source: Literal["telegram","jira_comment"] = "telegram"`, `issue: IssueInfo | None = None`; `CommitmentRef.issue_key`, `due_at`, `source_note` (optional); `prompts/commitments.md` → `version: 3` (Jira comment case; accepted request takes the request's date; Telegram rules unchanged)
- [X] T034 [US2] `src/scribe/context/comment_commitments.py` (implemented in its own module; `commitments.py` only gained the Telegram issue-key link and `to_ref` fields): `CommentCommitmentInput`, `extract_from_comment` (request per research J9, existing `ground_item` with the request text as an extra date source for Jira, `select_storable`, merge per J10, update-in-place for edited comments unless `user_modified`, `source_changed` when nothing is left, never re-store a comment of a user-deleted row; excerpt ≤ 280 chars, none when restricted), `resolve_for_issue`, `mark_comment_gone`, `mark_issue_unreachable`, `restore`, `issue_keys_in`; Telegram extraction sets `jira_issue_key` for keys of the user's selected projects; log `commitment_auto_updated` events (ids + note only) (depends on T011, T033)
- [X] T035 [US2] `src/scribe/context/tasks/jira/sync.py` `JiraCommentSync` (`run_due`, `sync_user`, `verify_all`, `verify_user`) per contracts "Comment sync" and research J8/J9/J11; first-scan message via notifier; failures through `TaskSources.record_result` (depends on T028, T034)
- [X] T036 [US2] Register jobs `jira_comments` (`Every(jira_sync_interval_s)`) and `jira_verify` (`Daily(03:40)`) in `src/scribe/__main__.py`; notifier sends to the user's control chat via the bot; extend `tests/integration/test_service.py` job assertions (depends on T013, T035)
- [X] T037 [US2] `src/scribe/telegram/control_bot/commitments_command.py`: show `to_whom` when there is no contact, source line (`📎 Jira PAY-142 comment 21 Oct ↗`, `Telegram`, `(restricted)`), "Recently closed by Scribe" (30 days) with `cmtreopen:<id>` (scoped to the caller), unreachable note; edit/delete set `user_modified` / `closed_by=user` (depends on T034)
- [X] T038 [US2] `jira_data_summary(session, user_id)` in `src/scribe/context/commitments.py` (counts of Jira-sourced commitments and cursor rows) for the future `/my_data` (001-T070), with a unit/integration test
- [X] T039 [US2] Eval suite `evals/threads/jira_comments.jsonl` (≥ 40 synthetic threads rendered per flavor by `tests/contract/fixtures/jira/render.py`) and `--suite jira` in `evals/run.py` (recall, precision, date accuracy, merge rate per flavor, flavor gap; canned outputs for dry runs); thresholds in `evals/thresholds.yml`; extend `tests/unit/test_eval_runner.py`

**Checkpoint**: US1 + US2 work; `make eval-dry` green.

---

## Phase 5: User Story 3 - Catch mismatches between dates and promises (P2)

**Goal**: each task's draft date is the newest of due field / comment deadline / user's promise,
and the card shows `📅 Dates differ` whenever they disagree; outages keep stored promises.

**Independent Test**: quickstart steps 6 and 9.

### Tests for User Story 3

- [X] T040 [P] [US3] `tests/unit/test_reconcile_task_dates.py`: newest source wins (due change vs comment deadline vs promise time); one warning per task with ≥ 2 distinct dates; none when equal or single; promise note text; Trello tasks (no key) only compare due field vs comment deadline
- [X] T041 [P] [US3] `tests/integration/test_us3_jira_drafts.py`: `test_date_mismatch_on_card`, `test_conflict_names_issue`, `test_telegram_promise_mismatch`, `test_jira_outage_uses_stored_commitments`

### Implementation for User Story 3

- [X] T042 [US3] `reconcile_task_dates` in `src/scribe/context/task_dates.py` (research J12) (makes T040 pass)
- [X] T043 [US3] `src/scribe/drafting/composer.py`: candidate commitments include those linked to loaded task keys / keys named in the incoming text; call `reconcile_task_dates` after deadline refinement; add `date_mismatch:<task ref>` warnings (allowed prefix) and store `date_notes` in `output_json`; `src/scribe/telegram/control_bot/render.py` renders `📅 Dates differ: …`; extend `tests/unit/test_draft_card.py` (depends on T034, T042)

**Checkpoint**: all stories pass; `make test-fast` and Jira integration tests green.

---

## Phase 6: Polish

- [X] T044 [P] `README.md`: Jira Cloud (scoped token `read:jira-work` + `read:jira-user`) and self-hosted (PAT, reachability, certificate confirmation, allowlist) setup; known limitation (autowatch); `SCRIBE_JIRA_*` settings; raise `SCRIBE_JIRA_DC_MIN_VERSION` to `11.3` after 2026-12-05
- [X] T045 [P] Redaction: `src/scribe/logging.py` drops `api_token`, `pat`, `pem`, `email`; extend `tests/unit/test_logging_redaction.py`
- [X] T046 Security review `specs/003-jira-commitments/security-review.md`: GET-only coverage, redirects off, SSRF guard incl. rebinding, pinning hostname check, tokens deleted and encrypted, no other people's text persisted, injection handling, PAT-scope exception mitigations
- [X] T047 Run `make lint typecheck test-fast test-integration`; fix defects
- [ ] T048 Run `uv run python -m evals.run --suite jira` and the existing suites with prompt v3 against the configured model; record results in `specs/003-jira-commitments/eval-report.md` (needs `ANTHROPIC_API_KEY`)
- [ ] T049 Execute the manual scenarios in `quickstart.md` against Jira Cloud and a self-hosted Jira (DC 10.3/11.3, self-signed certificate); record SC-001, SC-005 and first-scan timings in `checklists/requirements.md` notes

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 (T007 → T008 → T015; T009–T014 parallel after T007) → US1 → US2 → US3 → Polish.
- US1: T024–T026 parallel → T027 → T028 → T029.
- US2: T033 → T034 → T035 → T036; T037, T038 after T034; T039 after T033.
- US3: T042 → T043 (needs T034).
- Tests of each story are written first (they may be parallel with each other).

## Parallel Opportunities

- Setup: T003, T004. Foundational: T005, T006, T009–T014.
- US1 tests T016–T023; implementation T024–T026.
- US2 tests T030–T032; T037, T038 after T034.
- US3 tests T040, T041.

## Implementation Strategy

1. MVP = Phases 1–3: Jira as task source on both deployment types, with comment deadlines via
   issue #6 logic.
2. US2 commitments from comments; ship after the `jira` eval suite meets thresholds.
3. US3 reconciliation and mismatch warnings.
4. Polish: docs, security review, full test/eval/manual runs.

Rebase onto `main` once `001-us3-tasks-commitments` and `issue-6-trello-comment-deadlines` merge.
