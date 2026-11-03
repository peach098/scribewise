# Research: Jira Task Source with Comment Commitments

All Technical Context unknowns are resolved below. Research IDs use a `J` prefix to avoid
confusion with `specs/001-telegram-reply-agent/research.md`.

## J0. What the base branch already provides (revision 2026-10-21)

This feature is built on `issue-6-trello-comment-deadlines` (feature 001 US3 + issue #6) merged
with `main`. Reused as is:

| Existing piece | Where | Used for |
|---|---|---|
| `TaskSource` protocol, `Task` with `comments`, `description`, `description_at`, `due_set_at`, `deadline` | `context/tasks/base.py` | Jira issues map onto it (J7) |
| Effective deadline from due field / comments / description, LLM refinement `extract.deadlines` for the selected tasks | `context/tasks/deadlines.py`, composer | deadlines agreed in Jira comments (FR-J06a) |
| `TaskSources` service: 60 s cache, timeout, failure counter → `error` after 3, one alert per streak, `tasks_unavailable` / `tasks_partial` warnings | `context/tasks/service.py`, `review.py` | Jira loads and comment checks (FR-J19) |
| Commitment extraction `extract.commitments` + grounding (`ground_item`, `select_storable`, dedupe on normalized what/due), `/commitments` with resolve/edit/delete scoped to the caller | `context/commitments.py`, `commitments_command.py` | promises in the user's Jira comments (J9) |
| `/tasks` Trello connect flow, board picker, token message deletion | `tasks_command.py` | extended with a provider choice and the Jira flow (J15) |
| Scheduler jobs (`Daily`, `Weekly`), retention purge | `scheduling/scheduler.py`, `retention/purge.py` | comment sync (J8), cursor purge (J13) |

Consequences: no new LLM route, prompt or schema for comments (the earlier
`extract.jira_commitments` design is dropped); no separate outage notifier; no
`commitment_sources` table (J10).

## J1. One client for two Jira deployments

**Decision**: A single `JiraTaskSource` (implements the 001 `TaskSource` protocol) built on
httpx, with a small `JiraFlavor` strategy object chosen by `connection.config_json.deployment`
(`cloud` | `self_hosted`). The flavor supplies: base URL, auth header, search endpoint and
paging style, comment body format, user identity field and mention syntax. Everything above
the flavor (JQL, sync, extraction, commitments, rendering) is shared.

| Concern | Cloud | Self-hosted (Data Center / Server) |
|---|---|---|
| Base URL | `https://api.atlassian.com/ex/jira/{cloudId}` (required by scoped tokens); browse links still use `https://<site>.atlassian.net/browse/<KEY>` | user-entered `https://jira.example.com[/context]` |
| Auth | Basic `email:scoped_api_token` | `Authorization: Bearer <personal access token>` |
| Discover identity | `GET /rest/api/3/myself` → `accountId`, `displayName`, `timeZone` | `GET /rest/api/2/myself` → `key`, `name`, `displayName`, `timeZone` |
| Version check | n/a (always current) | `GET /rest/api/2/serverInfo` → `versionNumbers`, `deploymentType` |
| Issue search | `GET /rest/api/3/search/jql` (token paging: `nextPageToken`; the old `/search` returns 410) | `GET /rest/api/2/search` (`startAt` / `maxResults`) |
| Comments | `GET /rest/api/3/issue/{key}/comment?orderBy=created` (`startAt` paging); body is ADF JSON | `GET /rest/api/2/issue/{key}/comment?orderBy=created`; body is wiki markup string |
| Mentions | ADF `mention` node `attrs.id` = accountId | wiki `[~username]` / `[~key]` |
| Restricted comment | `visibility {type: group|role, value}` | same |

Cloud uses v3 everywhere because the v2 enhanced-search path is reported as deprecated in
some contexts; DC has no v3. Two body normalizers (`adf_to_text`, `wiki_to_text`) convert
bodies to the same internal `CommentText {plain, quoted_spans[], mentions[]}`. Both are
small, pure, unit-tested functions — no new dependency.

**Rationale**: FR-J00 asks for identical behavior; isolating the differences in one table
keeps the rest identical and lets contract tests run the same scenarios against both
flavors (SC-008).

**Alternatives considered**: two separate `TaskSource` classes (duplicates sync and paging
logic); a third-party Jira library such as `jira`/`atlassian-python-api` (sync-only or heavy,
hides which HTTP methods are used, which the read-only guard in J3 must control; Principle VI).

## J2. Authentication and minimum access

**Decision**:
- **Cloud**: the user creates an Atlassian *scoped* API token with only `read:jira-work` and
  `read:jira-user` and enters site address, account email and token. Setup resolves the
  `cloudId` from `https://<site>.atlassian.net/_edge/tenant_info` (unauthenticated), then
  calls `/myself` through the gateway URL. Classic unscoped tokens are not accepted: if the
  gateway rejects the token but the site URL accepts it, setup says "this looks like a classic
  token, please create a scoped read-only token" (FR-J04).
- **Self-hosted**: the user creates a Personal Access Token (available since Jira 8.14) and
  enters the base URL and token. PATs carry the user's full permissions; Jira offers no
  read-only PAT. This is recorded as a constitution exception in the plan, mitigated by the
  GET-only guard (J3), encryption at rest and disconnect/revoke instructions.
- Credentials are stored in `task_source_connections.credentials` (existing `EncryptedJSON`
  column) and deleted on disconnect (existing `delete_connection`).

**Rationale**: narrowest grant each deployment allows (constitution Security section).

**Alternatives considered**: OAuth 2.0 (3LO) for Cloud (needs a registered Atlassian app,
public callback URL and per-site consent; disproportionate for a Telegram-only UI, possible
later); OAuth 1.0a application links on DC (requires Jira admin, contradicts the spec
assumption that a typical user sets this up alone); username + password on DC (rejected:
broader than PAT, often disabled with SSO).

## J3. Read-only enforcement

**Decision**: The Jira httpx client is created with an event hook that raises
`ReadOnlyViolation` for any method other than `GET` (and `HEAD`) before the request leaves the
process; redirects are disabled (`follow_redirects=False`) so a redirect cannot move
credentials to another host. All endpoints in J1 are GETs (Cloud enhanced search supports
GET; DC search is GET). A contract test sends every client method through respx and asserts
no non-GET route was ever registered or hit; a unit test asserts the hook blocks `POST`.

**Rationale**: SC-007 ("zero writes") becomes structurally guaranteed, which matters most for
DC where the token itself cannot be restricted.

**Alternatives considered**: relying on code review only (not verifiable); POST search on DC
(harder to guard; GET is enough for our JQL length).

## J4. Self-hosted network safety (SSRF) and reachability

The service is multi-tenant; a user-supplied base URL would otherwise let anyone make the
server call internal addresses (Postgres, the health port, cloud metadata).

**Decision**:
- Only `https://` URLs; no credentials, query or fragment in the URL; port allowed.
- Before validate and at the start of each sync, the host is resolved and **every** resolved
  address is checked: loopback, link-local (incl. `169.254.169.254`), private (RFC 1918, ULA),
  CGNAT, multicast and unspecified addresses are refused unless the host (or a CIDR) is listed
  in operator setting `SCRIBE_JIRA_ALLOWED_PRIVATE_HOSTS` (for operators who run Scribe inside
  the company network). The client then connects to the checked IP with the original
  hostname for SNI/Host, so DNS rebinding between check and connect is not possible.
- Error classes for FR-J02: `JiraAddressInvalid`, `JiraUnreachable` (DNS failure, refused,
  timeout, blocked address — message: "Scribe could not reach this server; it must be
  reachable from the internet or allowed by the operator"), `JiraCertificateUntrusted`,
  `JiraVersionUnsupported`, `JiraAuthFailed`.

**Rationale**: makes FR-J02's distinctions concrete and closes the SSRF hole created by
supporting self-hosted Jira.

**Alternatives considered**: no restriction (SSRF); a static allowlist of Jira hosts
(unworkable for many companies); an outbound proxy (extra infrastructure; operators may still
add one via standard `HTTPS_PROXY`).

## J5. Company-issued certificates (FR-J02a)

**Decision**: Connect with normal system-CA verification first. If it fails with a
certificate verification error, fetch the server's leaf certificate (TLS handshake without
verification, no HTTP request is sent, credentials never transmitted), and show the user its
subject, issuer, validity dates and SHA-256 fingerprint with buttons **Trust this
certificate** / **Cancel**. On trust, store the leaf PEM and fingerprint in
`config_json.pinned_cert` and build an `ssl.SSLContext` with `cadata=<pinned PEM>`,
`verify_flags |= VERIFY_X509_PARTIAL_CHAIN` and `check_hostname=True`. If the server later
presents another certificate, verification fails → connection `status=error`,
`error_code=cert_changed`, and the control chat offers the new fingerprint for confirmation.

**Rationale**: trusting exactly one certificate is narrower than trusting a whole company CA
and matches the spec wording ("refuse if the certificate changes until the user confirms").
Hostname checking stays on.

**Alternatives considered**: `verify=False` (rejected: MITM); asking the user to upload the
company root CA (harder for users in Telegram; may be added later as an alternative).

## J6. Supported self-hosted versions

**Decision**: Minimum version is a setting, `SCRIBE_JIRA_DC_MIN_VERSION`, default `10.3`
(the oldest Atlassian LTS still in support at plan time: 10.3 LTS EOS 2026-12-05; 11.3 LTS EOS
2027-12-03; Data Center EOL 2029-03-28). Hard floor `8.14` (PATs). Setup reads
`serverInfo.versionNumbers`; below the setting → `JiraVersionUnsupported` with the supported
range in the message. Contract fixtures cover 10.3 and 11.3 response shapes.

**Rationale**: the spec ties support to Atlassian's support window; a setting avoids a code
change when 10.3 leaves support.

## J7. Jira issues as tasks (FR-J06, FR-J06a)

**Decision**: `list_open_tasks` runs one paged search per load:
`project in (<keys>) AND assignee = currentUser() AND statusCategory != Done ORDER BY duedate ASC`
with `fields=summary,status,duedate,assignee,reporter,description,comment,created` and
`expand=changelog`, capped at 100 issues. Mapping to the existing `Task`:

| `Task` field | Jira |
|---|---|
| `id` | issue key (`PAY-142`); the composer's ref is `task:PAY-142` |
| `title`, `status`, `assignees`, `url` | `summary`, `status.name`, `[assignee.displayName]`, `<site>/browse/<KEY>` |
| `due_at` | `duedate` (a calendar date) as noon UTC, plus `due_is_date=True` so `Task.due_on()` returns the date itself in every time zone |
| `description`, `description_at` | normalized body (J1 normalizers), ≤ 2000 chars; issue `created` |
| `comments` | last 5 comments (oldest first), normalized text without quoted spans, ≤ 1000 chars, `author` = display name, `at` = `updated`; restricted comments are left out (never shown to drafts) |
| `due_set_at` | newest changelog history item whose field is `duedate` (None when not in the returned history) |

Nothing else changes: the task service resolves the effective deadline and the composer,
card labels and conflict backstop use it. `Task` gains one optional field, `due_is_date: bool =
False`, honoured by `due_on()` and by `deadlines.choose_deadline` for the due-field candidate.
Results are cached 60 s per user like Trello.

## J8. Comment sync for the user's own promises (FR-J07, FR-J13)

**Decision**: Add `Every(interval)` to the scheduler and register the job `jira_comments`
every 5 minutes. A run claims Jira connections with `status != disconnected AND (next_sync_at IS
NULL OR next_sync_at <= now)` using `FOR UPDATE SKIP LOCKED`, sets `next_sync_at = now + 5 min`
before releasing the lock, and syncs them with concurrency 5. Search:
`project in (<keys>) AND (assignee = currentUser() OR reporter = currentUser() OR watcher = currentUser()) AND updated >= "<since>" ORDER BY updated ASC`
with `fields=summary,status,duedate,reporter,comment`. `<since>` is `sync_watermark − 5 min`
rendered `yyyy/MM/dd HH:mm` in the Jira user's time zone (`myself.timeZone`), or `-30d` on the
first run (capped at 1000 issues, newest first; the cap is reported once in the control chat).
Commenting adds the user as a watcher under Jira's default settings, so `watcher` covers
issues the user commented on (spec FR-J07). 429/503 set `next_sync_at` from `Retry-After`
(capped at 15 min). Failures go through the task service's failure counter (J12).

A daily job `jira_verify` (03:40 UTC) re-reads every issue that still has an open Jira-sourced
commitment for done status, comment deletion and lost access (J11).

## J9. Promises in the user's comments → commitments (FR-J08 – FR-J10, FR-J21)

**Decision**: reuse `LLMRouter.extract_commitments` and the existing grounding.

1. For each issue in the sync result, take the user's comments (author id == identity) with
   `updated` within 30 days whose `(comment_id, body HMAC)` is not in `jira_comment_cursor`.
2. Context: at most one earlier comment by someone else — the latest one that mentions the user
   or is mentioned by the user's comment, else the latest earlier comment by someone else.
3. `CommitmentRequest` gains `source: "telegram" | "jira_comment"` (default telegram) and
   `issue: {key, title} | null`. For Jira: `outgoing_text` = the user's comment without quoted
   spans, `contact_display_name` = counterparty (first mentioned person, else the request
   author, else the issue reporter), `sent_at` = comment `created` in the user's Scribe time
   zone, `recent_incoming` = `[request]` or `[]`. Display names only — no account ids or emails.
4. `prompts/commitments.md` → `version: 3`: the message may be a comment on a work task; when
   `source` is `jira_comment` and the user accepts a request in `recent_incoming` ("sure",
   "ок"), `due` is the date that request names. Telegram behavior is unchanged.
5. Grounding: the existing `ground_item` (evidence quoted from `outgoing_text`, confidence ≥
   0.7, `due` only when the deterministic date finder finds it) — for Jira the due date may also
   be found in the single `recent_incoming` request. Items failing grounding are dropped.
6. Store (J10), then upsert the cursor `(comment_id, comment_updated_at, HMAC, outcome)`.

**Rationale**: one extraction path for both sources keeps behavior and evals aligned
(Principle V) and adds no route (Principle VI).

## J10. Storing Jira-sourced commitments and merging (FR-J11, FR-J14, FR-J17)

**Decision**: extend the existing `commitments` row instead of a sources table:

- `contact_id` becomes nullable; `to_whom` (encrypted) holds the counterparty name; the Jira
  counterparty is matched to a Telegram `Contact` by case/diacritics-insensitive display name,
  else `contact_id` is null.
- `source_kind` (`telegram_message` | `jira_comment`) says where the commitment was first
  found; `jira_issue_key`, `jira_comment_id`, `jira_comment_url` link a Jira comment (set for
  Jira-sourced rows and when a Telegram commitment is merged with a Jira comment);
  `source_message_text` (existing, encrypted) keeps the user's own words (evidence ≤ 280 chars);
  for a restricted comment it stays null and `restricted = true`.
- **Merge**: a new Jira item whose counterparty and due match an open commitment (same contact,
  or same normalized `to_whom` when no contact) and whose normalized `what` equals it or
  overlaps ≥ 0.6 by words is not inserted; the Jira link columns are set on the existing row
  (shown as one commitment with both sources).
- **Edits**: a changed comment is re-extracted; the row linked to that comment id gets the new
  `what` / `due` unless `user_modified`; if nothing is found any more → J11 `source_changed`.
- A comment id that belongs to a row the user deleted (`closed_by='user'`) is never stored
  again.
- Telegram extraction also sets `jira_issue_key` when the outgoing message names a key of the
  user's selected projects (`[A-Z][A-Z0-9_]+-\d+`), so Telegram-only promises are compared
  with the issue's due date (FR-J16).

## J11. Automatic state changes

| Event | Change | `auto_note` | Undo |
|---|---|---|---|
| Issue reaches status category Done (sync / verify) | `resolved`, `closed_by='system'` | `issue_done` | **Reopen** |
| Source comment gone (verify) | merged row: Jira link cleared, stays open; Jira-only row: `resolved`, `closed_by='system'` | `source_removed` | **Restore** |
| Edited comment no longer promises anything | as above | `source_changed` | **Restore** |
| Issue 403/404 or project deselected | stays `open` | `source_unreachable` | — |

`user_modified` rows get notes only. Reopen/Restore sets `state=open`, `user_modified=true`,
clears `closed_at`/`auto_note`. Events are logged (`commitment_auto_updated`, ids and note
only), consistent with the existing commitment events (no new `activity_kind` values).

## J12. Drafting: mismatches and outages (FR-J15, FR-J16, FR-J18, FR-J19)

**Decision**:
- `open_for` additionally returns open commitments whose `jira_issue_key` is among the loaded
  tasks' keys or named in the incoming text (selected projects only). `CommitmentRef` gains
  `issue_key` and `source_note` ("your Jira comment on PAY-142, 21 Oct"); restricted rows are
  passed without quotes.
- `reconcile_task_dates(task_items, tasks, commitments, tz)` (pure, in `context/commitments.py`)
  runs after deadline refinement: for each task it collects the due field, the comment /
  description deadline and the linked open commitments, each with a time (due change, comment
  time, promise time). The **newest** becomes `TaskItem.due` with its `deadline_source` /
  `deadline_note` (a promise reads "from your own promise, Jira comment on Tue 21 Oct: …"), so
  the draft follows the user's latest promise without a prompt change. When two or more
  distinct dates exist, the composer adds `date_mismatch:<task id>` to `system_warnings` and
  the card shows `📅 Dates differ: …` listing each date with its source. This also applies to
  Trello cards whose comment deadline differs from the due field.
- Conflicts: unchanged mechanism; Jira tasks and commitments are candidates like Trello ones,
  and labels name the issue key.
- Outage: the existing service behavior (`tasks_unavailable`, failure streak, alert) applies to
  Jira; the comment sync reports its failures through the same counter (public
  `TaskSources.record_result`). Stored Jira-sourced commitments are in the database, so drafts
  keep using them during an outage.

## J13. Privacy and retention

- Stored from Jira: the connection (encrypted credentials, selected project keys, identity,
  pinned certificate), sync time fields, commitments (encrypted `what`, `to_whom`, ≤ 280-char
  excerpt of the user's own words; none for restricted comments) and `jira_comment_cursor` rows
  (ids, timestamps, HMAC — no text).
- Never stored: issues, other people's comments, descriptions. Task data lives in the 60 s
  cache only, like Trello.
- Retention: commitments as today (deleted 30 days after closing); cursors 30 days after the
  comment's `updated`, and with the connection.
- `/my_data` counts Jira-sourced commitments and cursor rows; export includes them and the
  connection's `config_json` minus credentials and the pinned certificate.
- Logs: counts, ids, error codes only (`jira_sync_finished`, `jira_verify_finished`).

## J14. Evaluation (Principle V; SC-002 – SC-006, SC-008)

- `evals/threads/jira_comments.jsonl`: ≥ 40 synthetic issue threads, each rendered as ADF
  (Cloud) and wiki markup (DC) from one neutral source, run through the real normalizer → the
  `extract.commitments` route (v3) → grounding. Labels: expected commitments, negatives
  (vague, unaccepted request, quoted text, injection), relative dates across time zones,
  accepted requests, Telegram duplicates. `evals/run.py --suite jira` reports recall,
  precision, date accuracy, merge rate per flavor and the flavor gap; thresholds:
  `jira.recall ≥ 0.85`, `jira.precision ≥ 0.90`, `jira.date_accuracy ≥ 0.95`,
  `jira.merge_rate ≥ 0.90`, `jira.flavor_gap ≤ 0.05`.
- Draft-level mismatch is deterministic (J12) and covered by integration tests.
- The existing `us3_commitments` suite must not regress with prompt v3.

## J15. Setup UX in the control chat

`/tasks` when nothing is connected shows **🔌 Connect Trello** and **🔌 Connect Jira**. Jira
flow (`jira_connect.py`, its own FSM group): deployment (Cloud / Self-hosted) → site address →
Cloud: email → scoped token; self-hosted: personal access token → (if the certificate is
company-issued) certificate card → project picker (the existing paged picker, generalized from
boards; at most 20 projects). Token messages are deleted after reading, as for Trello.
Connecting Jira while Trello is connected (or the reverse) asks for confirmation and replaces
the connection. Status shows provider, deployment, host, version (self-hosted), projects and
the existing error state.
