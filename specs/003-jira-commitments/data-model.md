# Data Model: Jira Task Source with Comment Commitments

Extends `specs/001-telegram-reply-agent/data-model.md` as implemented on the base branch
(feature 001 US3 + issue #6 + main). Conventions are unchanged: every per-user table has
`user_id` FK with `ON DELETE CASCADE`; **enc** = encrypted at rest. One migration:
`alembic/versions/0004_jira_commitments.py` (`down_revision = "0003"`, after main's `0003_user_language`).

## Changed tables

### task_source_connections  (FR-J01 – FR-J05, J8)

| Field | Type | Change / Notes |
|---|---|---|
| provider | enum `trello`, `jira` | unchanged |
| credentials | **enc** jsonb | Jira Cloud `{email, api_token}`; self-hosted `{pat}` |
| config_json | jsonb | Jira: `{deployment: "cloud"\|"self_hosted", site_url, api_base_url, cloud_id?, project_keys[] (1–20), identity {id, name, display_name, time_zone}, server_version?, pinned_cert? {pem, sha256, subject, not_after}, first_scan_done?: bool}`; existing keys `consecutive_failures`, `error_alerted` are shared with Trello |
| status | enum | unchanged (`active`, `error`, `disconnected`) |
| next_sync_at | timestamptz, nullable | **new**: when the comment sync may run next (J8) |
| sync_watermark | timestamptz, nullable | **new**: newest issue `updated` processed (J8) |

Validation: `site_url` is `https://`, no userinfo/query/fragment; `project_keys` match
`^[A-Z][A-Z0-9_]+$`, 1–20 entries before the connection is saved.

### commitments  (FR-J08 – FR-J17)

| Field | Type | Change / Notes |
|---|---|---|
| contact_id | FK contacts, **now nullable** | null when a Jira counterparty matches no Telegram contact |
| to_whom | **enc** text, nullable | **new**: counterparty display name; always set for Jira-sourced rows |
| source_kind | enum `commitment_source_kind` (`telegram_message`, `jira_comment`), default `telegram_message` | **new** |
| jira_issue_key | text, nullable | **new**: issue the promise is about (Jira-sourced, merged, or a key named in a Telegram message); index `(user_id, jira_issue_key)` |
| jira_comment_id | text, nullable | **new**; partial unique index `(user_id, jira_comment_id) WHERE jira_comment_id IS NOT NULL AND state = 'open'` |
| jira_comment_url | text, nullable | **new**: `<site>/browse/<KEY>?focusedCommentId=<id>` |
| restricted | bool, default false | **new**: the source comment had `visibility`; no excerpt kept, never quoted |
| auto_note | enum `commitment_auto_note` (`issue_done`, `source_removed`, `source_changed`, `source_unreachable`), nullable | **new** |
| closed_by | enum `commitment_closed_by` (`user`, `system`), nullable | **new**; existing closed rows backfilled `user` |
| user_modified | bool, default false | **new**: set by edit/restore in `/commitments` |
| source_message_text | **enc** text, nullable | unchanged column; for Jira rows the user's own words (≤ 280 chars), null when restricted |
| source_sent_at | timestamptz | unchanged; for Jira rows the comment's `updated` (the promise time used by J12) |
| what, due, confidence, state, closed_at | | unchanged (`confidence ≥ 0.7`) |

State transitions (additions):

```
open ──(user resolve/delete in /commitments)────────────► resolved/deleted  closed_by=user
open ──(issue done | Jira-only source removed/changed)──► resolved  closed_by=system, auto_note
resolved[closed_by=system] ──(Reopen/Restore)───────────► open  user_modified=true, auto_note=null
open ──(issue unreachable)──► open  auto_note=source_unreachable
open[merged] ──(Jira source removed)──► open  jira_* cleared, auto_note=source_removed
```

A comment id on a row with `closed_by='user'` is never stored again (FR-J14).

## New table

### jira_comment_cursor  (J9, no content)

| Field | Type | Notes |
|---|---|---|
| id | uuid | |
| user_id | FK users, cascade | |
| issue_key | text | |
| comment_id | text | unique `(user_id, comment_id)` |
| comment_updated_at | timestamptz | |
| body_hmac | bytea | HMAC-SHA256 of the normalized body with a key derived from `SCRIBE_MASTER_KEY` (HKDF, `info=b"jira-comment-hmac"`) |
| outcome | enum `jira_cursor_outcome` (`no_commitment`, `commitment`, `merged`, `skipped`) | |
| created_at, updated_at | timestamptz | |

Deleted 30 days after `comment_updated_at` (daily purge) and when the Jira connection is
disconnected.

## Not persisted

Jira issues, descriptions and other people's comments (60 s in-memory task cache only, as for
Trello); account ids or emails of other Jira users.

## Migration notes (0004)

1. Create enums `commitment_source_kind`, `commitment_auto_note`, `commitment_closed_by`,
   `jira_cursor_outcome`.
2. `commitments`: drop NOT NULL on `contact_id`; add the new columns; backfill
   `closed_by='user'` where `closed_at IS NOT NULL`; create the two indexes.
3. `task_source_connections`: add `next_sync_at`, `sync_watermark`.
4. Create `jira_comment_cursor`.
5. Downgrade: delete Jira-sourced commitments without a contact, restore NOT NULL, drop the new
   objects.
