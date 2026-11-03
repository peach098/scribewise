# Implementation Plan: Jira Task Source with Comment Commitments

**Branch**: `003-jira-commitments` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/003-jira-commitments/spec.md`

## Summary

Built on the base branch `issue-6-trello-comment-deadlines` (feature 001 US3 + issue #6) merged
with `main` (research J0). Replace the Jira stub with a read-only `JiraTaskSource` that works
against Jira Cloud and self-hosted Jira (Data Center / Server) through one client and a small
per-flavor strategy (base URL, auth, search paging, ADF vs wiki bodies). Open issues assigned to
the user become `Task`s **with comments, description and due-change time**, so the existing
issue #6 logic finds deadlines agreed in Jira comments exactly as for Trello. A comment sync job
(every 5 minutes) reads the user's own new/edited comments on assigned, reported and watched
issues and runs them through the existing `extract.commitments` route and grounding (prompt v3
adds the Jira-comment case), storing promises as commitments linked to the issue and comment,
merged with matching Telegram commitments, kept in step with edits, deletions and issue
completion. A pure reconciliation step makes each task's draft date the user's newest promise and
adds a `📅 Dates differ` warning when sources disagree. Self-hosted support adds an SSRF address
guard, single-certificate pinning and a version check; the client is GET-only.

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: unchanged — httpx, pydantic v2, SQLAlchemy 2 async + Alembic,
aiogram 3, `cryptography`, structlog, LLM adapters behind `LLMRouter`; stdlib `ssl` /
`ipaddress` / `hmac`. **No new dependency, no new LLM route.**

**Storage**: PostgreSQL 16; migration `0004_jira_commitments` (columns on `commitments` and
`task_source_connections`, new `jira_comment_cursor`).

**Testing**: pytest + pytest-asyncio; respx contract tests with synthetic Cloud (v3, ADF) and
Data Center (v2, wiki; 10.3 and 11.3) fixtures; a local TLS server with a
`cryptography`-generated certificate; the existing fake LLM adapter and integration support
(`tests/integration/us3_support.py`); `evals/run.py --suite jira`.

**Target Platform**: Linux server, Docker Compose (unchanged)

**Project Type**: single long-running service (unchanged)

**Performance Goals**: task load for 100 issues in one paged search within the existing 10 s
load timeout; new/edited comment reflected ≤ 15 min (sync every 5 min); first 30-day scan < 10
min for 1000 issues (measured in the staging run, T049); draft path adds only pure work
(reconciliation O(tasks × commitments)).

**Constraints**: Jira access strictly GET (enforced in the client); only the user's own comment
+ at most one request comment leave for the LLM; no Jira text persisted except ≤ 280 chars of
the user's own words (none for restricted comments); https only; private addresses blocked
unless allowlisted by the operator; `Retry-After` honoured; ≤ 20 projects per user.

**Scale/Scope**: ~100 users × ≤ 20 projects (enforced in setup, FR-J03); ≤ 100 open assigned
issues per load; ~12 scan searches/user/hour; 3 user stories, 26 FRs (FR-J00 – FR-J23,
FR-J02a, FR-J06a)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status (pre-research) | Status (post-design) |
|---|---|---|---|
| I. Human approval before sending | Jira data only feeds drafts and commitment records; no new send path; every external action logged. | PASS (FR-J04, FR-J22) | PASS: no code path in `context/tasks/jira/` can reach `sending/send.py`; the Jira client is GET-only (`ReadOnlyViolation` hook, J3), so the agent takes no action on Jira at all; syncs and automatic commitment changes are logged (`jira_sync_finished`, `commitment_auto_updated`; ids and counts only), like the existing commitment events. |
| II. Privacy & data minimization | Only selected projects; only the user's own comments + one request context to the LLM; no thread storage; encrypted credentials and excerpts; view/delete via `/my_data`. | PASS (FR-J20, FR-J21, FR-J23) | PASS: J9 request shape (display names only, no ids/emails; one request comment at most); `jira_comment_cursor` stores HMAC not text; excerpts ≤ 280 chars of the user's own words, encrypted, none for restricted comments, purged 30 days after closure; tasks (incl. comments) only in the existing 60 s cache; logs carry counts only (`test_no_comment_text_in_logs`). |
| III. Grounded replies, no fabrication | Commitments must quote the user's own words; drafts cite issues/comments; mismatches shown, not silently resolved. | PASS (FR-J08, FR-J15, FR-J16) | PASS: existing grounding (`ground_item`: evidence quoted from the user's comment, confidence ≥ 0.7, due only when the deterministic date finder finds it in the comment or the answered request); `reconcile_task_dates` and the `date_mismatch` warning are deterministic, independent of model output; restricted comments never quoted. |
| IV. Faithful personal style | Not affected. | PASS | PASS: style pipeline untouched; Jira text is never used as style samples. |
| V. Evaluation-driven development | Extraction prompt and draft prompt changes measured; deterministic code tested first. | PASS | PASS: new eval suite `jira` (≥ 40 threads × 2 flavors) with thresholds incl. flavor gap; `prompts/commitments.md` v3 requires the `jira` and existing `us3_commitments` suites to pass; contract/unit/integration tests for client, normalizers, guard, pinning, sync, reconciliation. |
| VI. Simplicity & observability | No new service/dependency; Jira behind existing `TaskSource`; structured logs. | PASS | PASS: one client + flavor strategy; reuses the issue #6 deadline pipeline, the `extract.commitments` route, the task service's cache/failure handling, the commitments table and `/commitments`, and the scheduler job runner (adds `Every`); no webhooks, queues, new routes or caches; `jira_sync_finished` log with counts, latency, error codes. |
| Security & data handling | Minimum scopes; encryption; injection resistance; no-training providers. | PASS (with one justified exception) | PASS: Cloud requires scoped `read:jira-work` + `read:jira-user` tokens; credentials in `EncryptedJSON`; all Jira text in delimited data blocks, no tools, output can only become a commitment record (injection evals); no new route (existing routes already require a `data_use_confirmed` provider). Added: SSRF address guard (J4), redirects disabled, single-cert pinning with hostname check (J5). **Exception — self-hosted PAT scope**: Jira Data Center personal access tokens cannot be limited to read-only. Mitigations: GET-only client enforced in code and tested, token encrypted, token message deleted from chat, disconnect deletes it, setup tells the user how to revoke it in Jira. |

**Gate result**: PASS. One justified exception (self-hosted PAT scope), listed in Complexity
Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/003-jira-commitments/
├── plan.md              # This file
├── research.md          # Phase 0 output (J1–J15)
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── internal-interfaces.md               # JiraTaskSource, JiraClient, sync, commitments, router, scheduler additions
│   ├── control-chat.md                      # /tasks Jira setup, /commitments, draft card, notifications
│   └── jira-commitment-extraction.schema.json
├── checklists/requirements.md
└── tasks.md             # Phase 2 output (/speckit-tasks - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
alembic/versions/0004_jira_commitments.py      # commitments/task-connection columns, jira_comment_cursor
prompts/commitments.md                         # version: 3 (Jira comment source, accepted requests)
src/scribe/
├── config.py                                  # + SCRIBE_JIRA_* settings
├── crypto.py                                  # + comment_hmac()
├── db/models.py                               # + enums, columns, JiraCommentCursor
├── db/repos/
│   ├── commitments.py                         # Jira lookups, auto notes, restore, merge target
│   ├── jira_cursor.py                         # new
│   └── tasks_conn.py                          # + claim_due_jira(), set_sync_state()
├── context/
│   ├── commitments.py                         # extract_from_comment, reconcile_task_dates, issue keys
│   └── tasks/
│       ├── base.py                            # Task.due_is_date, identity fields, Jira errors
│       ├── deadlines.py                       # honour due_is_date
│       ├── service.py                         # JiraTaskSource registered, record_result()
│       └── jira/                              # replaces jira.py stub
│           ├── __init__.py, source.py         # JiraTaskSource
│           ├── client.py, flavors.py          # GET-only client, Cloud / self-hosted
│           ├── bodies.py                      # adf_to_text, wiki_to_text
│           ├── netguard.py, tls.py            # SSRF guard, certificate pinning
│           ├── jql.py                         # JQL builders
│           └── sync.py                        # JiraCommentSync (jobs jira_comments, jira_verify)
├── drafting/composer.py                       # issue-linked commitments, reconcile, date_mismatch
├── llm/provider.py                            # CommitmentRequest.source/issue, CommitmentRef fields
├── scheduling/scheduler.py                    # Every schedule
├── retention/purge.py                         # cursor purge
├── __main__.py                                # register Jira jobs
└── telegram/control_bot/
    ├── jira_connect.py                        # new: Jira connect FSM, certificate card
    ├── tasks_command.py                       # provider choice, generalized project picker, status
    ├── commitments_command.py                 # sources, auto notes, Reopen/Restore
    └── render.py                              # date mismatch line, Jira labels

tests/
├── unit/        # test_jira_bodies, test_jira_jql, test_netguard, test_every_schedule,
│                # test_reconcile_task_dates, test_jira_commitment_merge
├── contract/    # test_jira_task_source, test_jira_client_read_only, test_jira_tls_pinning
│   └── fixtures/jira/{cloud,dc_10_3,dc_11_3}/
└── integration/ # test_jira_models, test_jira_connect_flow, test_jira_sync, test_us3_jira_drafts

evals/threads/jira_comments.jsonl, evals/run.py (--suite jira), evals/thresholds.yml
```

**Structure Decision**: Single package `src/scribe`. Jira code lives in `context/tasks/jira/`
behind the existing `TaskSource` protocol, with all Cloud-vs-self-hosted differences confined to
`flavors.py` and `bodies.py`. Commitment logic stays in `context/commitments.py` so Telegram and
Jira share extraction, grounding, merge and resolve rules. The Jira connect flow is its own
module next to `tasks_command.py` to keep the Trello flow untouched.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Self-hosted Jira token cannot be read-only (Security: minimum scopes) | Spec requires self-hosted Jira support (clarification 2026-10-21); Data Center PATs inherit full user permissions | OAuth 1.0a application links need a Jira admin (contradicts spec assumption); passwords are broader. Mitigated by a GET-only client enforced and tested in code (J3). |

## Phase Summary

- **Phase 0 (research.md)**: J0 base-branch reuse; J1–J6 flavors, auth, GET-only, SSRF guard,
  certificate pinning, versions; J7 issues as tasks with comments; J8 comment sync; J9
  extraction via `extract.commitments`; J10 storage and merge; J11 automatic changes; J12
  reconciliation and outages; J13 privacy; J14 evals; J15 setup UX.
- **Phase 1**: data-model.md, contracts (internal interfaces, control chat), quickstart.md.
- **Base**: branch `003-jira-commitments` = `origin/issue-6-trello-comment-deadlines` + `origin/main`.
  It must be rebased once those merge.
- **Phase 2**: tasks.md.
