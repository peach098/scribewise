# Data Model: Telegram Work Reply Agent

All tables carry `id` (UUID), `created_at`, `updated_at`. Every per-user table has
`user_id` with `ON DELETE CASCADE` from `users` (FR-029, FR-030). Columns marked **enc**
use the `EncryptedText` type (research R7). Times are stored in UTC; display uses
`users.timezone`.

## Entities

### users
| Field | Type | Notes |
|---|---|---|
| telegram_control_chat_id | bigint, unique | bot chat used for review and settings |
| telegram_user_id | bigint, unique | the user's own account id (after login) |
| display_name | text | |
| timezone | text (IANA) | default from setup |
| working_days | int[] | 0=Mon … 6=Sun |
| work_start / work_end | time | daily window |
| delay_min_s / delay_max_s | int | global reading-delay range; `min ≤ max` |
| state | enum | `setting_up`, `active`, `paused`, `out_of_office` |
| llm_tier | enum | `quality` (default), `economy` — selects the `compose.draft` model (research R5) |
| language | text | control-bot UI language, `en` (default) or `ru` (check constraint; migration 0003, issue #8); set at `/start` from the Telegram `language_code` (`ru`, `uk`, `be`, `kk` → `ru`), changed in `/settings` |
| ooo_message | text, nullable | suggested reply while OOO; non-NULL exactly while the user is out of office (`""` when there is no suggested text), also while a `/pause` from OOO is in effect, so `/resume` restores `out_of_office` |
| setup_step | text, nullable | FSM position during setup |

### telegram_sessions
| Field | Type | Notes |
|---|---|---|
| user_id | FK unique | one account per user |
| session_string | **enc** | Telethon StringSession |
| status | enum | `active`, `revoked`, `error` |
| last_error | text, nullable | |

### work_chats
| Field | Type | Notes |
|---|---|---|
| user_id | FK | |
| telegram_chat_id | bigint | unique with user_id |
| title | text | |
| kind | enum | `private`, `group`, `channel_comments` |
| enabled | bool | only enabled chats are stored/drafted (FR-001) |
| ~~group_mode~~ | — | removed in 006, migration 0009: every group follows FR-003 |
| override_language | text, nullable | per-chat overrides (FR-024) |
| override_formality | int 1–5, nullable | |
| override_delay_min_s / override_delay_max_s | int, nullable | |

### contacts
| Field | Type | Notes |
|---|---|---|
| user_id | FK | |
| kind | enum | `person`, `group` |
| telegram_peer_id | bigint | person id or group chat id; unique with user_id |
| display_name | text | |
| own_message_count_6m | int | ranking input for profile slots: the user's own messages in that chat over 6 months at import / rebuild; each weekly refresh run (also one that changed nothing) adds the ones written since the previous run or build; reset to 0 when the user removes the contact's profile, after which only messages written after the removal count (refresh and rebuild; the removal is a `profile_undo` record) |
| pinned_profile | bool | never retired when true (at most 20 pinned) |

### messages  (30-day rolling window)
| Field | Type | Notes |
|---|---|---|
| user_id | FK | |
| work_chat_id | FK | |
| telegram_message_id | bigint | unique with work_chat_id |
| sender_contact_id | FK contacts, nullable | null when `is_outgoing` |
| is_outgoing | bool | user's own message |
| text | **enc** | text only; media messages store `has_media=true` and empty text |
| has_media | bool | |
| sent_at | timestamptz | indexed; purge key (FR-020) |
| reply_to_telegram_message_id | bigint, nullable | |
| is_forwarded | bool, default false | a forwarded copy (Telegram `fwd_from`): not the sender's own words; never addressed by itself, and a turn of forwards only is never drafted by itself (issue #47) |
| forwarded_from | **enc**, nullable | display label of the forward's origin (a name or channel title) when known; passed to drafting, never logged (issue #47) |

### conversation_turns
One per group of consecutive incoming messages needing a reply (FR-002). The spec's
"Waiting Message" entity is a `conversation_turn` in state `waiting`.

| Field | Type | Notes |
|---|---|---|
| user_id, work_chat_id, contact_id | FK | |
| first_message_id / last_message_id | FK messages | grouping window 60 s |
| arrived_at | timestamptz | first message time |
| delay_s | int | randomly chosen within range (FR-016) |
| due_at | timestamptz | `next_allowed(arrived_at + delay_s)`; indexed |
| state | enum | see state machine below |
| read_at | timestamptz, nullable | when the agent marked the chat read |
| dropped_reason | enum, nullable | `manual_reply`, `paused`, `chat_disabled`, `media_only` |

### drafts
| Field | Type | Notes |
|---|---|---|
| user_id, conversation_turn_id | FK | one active draft per turn; older ones `superseded` |
| version | int | increments on regenerate |
| text | **enc**, nullable | proposed reply; nulled 30 days after a final state |
| edited_text | **enc**, nullable | user's edited version actually sent; nulled with `text` |
| finalized_at | timestamptz, nullable | set on sent/rejected/expired/dropped/superseded; purge key |
| output_json | jsonb | metadata in the clear: `language`, `needs_info`, `tone_shift_applied`, `context_warnings`, the boolean flags `creates_commitment` / `sensitive`, `system_warnings` detected by the composer (contracts/internal-interfaces.md) and `reply_to_message_id` (the `messages.id` of the newest incoming message the draft was composed for; `send.py` quotes it); plus `enc`: one **enc** string with every content field of `DraftOutput` (`placeholders`, `sources`, `flags`, `conflicts`). The draft text is only in `text`. `enc` is removed with `text` 30 days after a final state (rows written before this layout have plaintext content keys, read the same way and stripped by the same purge). Read through `drafting/stored_output.load_stored_output` |
| style_profile_id | FK, nullable | profile used (global or per-contact) |
| prompt_version | text | observability |
| llm_provider / llm_model | text | which adapter and model composed this draft |
| llm_usage_json | jsonb | `{filter: {tokens…, cost_usd}, compose: {tokens…, cost_usd}}` — no content |
| state | enum | `pending`, `sent`, `rejected`, `expired`, `dropped`, `superseded` |
| prepared_at | timestamptz | |
| remind_at | timestamptz | `prepared_at + 2h` (1 min with `test_fast_clock`); indexed (FR-032) |
| reminded_at | timestamptz, nullable | |
| expires_at | timestamptz | `prepared_at + 24h` (2 min with `test_fast_clock`); indexed |
| approved_by_user_at | timestamptz, nullable | **required non-null before `sent`** (Principle I) |
| sent_telegram_message_id | bigint, nullable | |
| outdated | bool | set when new messages arrive before approval (FR-015), or when a message joined the turn while this draft was being composed; only **Send anyway** (`allow_outdated`) may send it |
| control_message_id | bigint | the bot message showing this draft |

### style_profiles
| Field | Type | Notes |
|---|---|---|
| user_id | FK | |
| scope | enum | `global`, `contact` |
| contact_id | FK, nullable | required when scope=`contact`; ≤ 20 active per user (FR-034); a group chat's contact is the group |
| version | int | new row per build / weekly refresh; `is_current` flag |
| is_current | bool | |
| rules_json | **enc** (`EncryptedJSONBFromPlaintext`, column type `jsonb`, no migration) | learned rules (research R8) plus `weak` (fewer than `min_style_samples` own messages); stored as `{"enc": "<AES-GCM of the JSON>"}`, a pre-encryption plaintext document is read as is and rewritten encrypted by the `reencrypt_profiles` job (`retention/reencrypt.py`, also `python -m scribe.jobs reencrypt_profiles`); never queried in SQL |
| user_rules_json | **enc** (same type) | rules the user set or accepted: `{"rules": [{"text", "source": "user"\|"proposal", "key"?}]}`; copied forward unchanged by every build, refresh and undo (FR-037) |
| summary_text | **enc** (`EncryptedTextFromPlaintext`, column type `text`) | plain-language summary |
| examples | **enc** jsonb | ≤ 20 (contact) / ≤ 50 (global) example messages (FR-035) |
| sample_size | int | own messages sent to the model for this version (at most `profile_max_samples`, after the group-chat and short-message limits and a lighter retry; research R8); `weak` is based on the user's whole history instead |
| state | enum | `current`, `previous` (undo target), `retired` |
| created_at | timestamptz | set from the service clock; the weekly refresh counts own messages written after the newest build or refresh run |

Writes to one user's profiles (build, rebuild, refresh, undo, 🗑 Remove, pin, rule edits and
accepted proposals) are serialised by `pg_advisory_xact_lock(hashtext('profiles:' ||
user_id))`, taken before the rows to change are read, so there is exactly one `current` row per
lineage (no unique constraint, no migration); a user-initiated Rebuild also holds an in-process
per-user lock (a second tap is answered, not started).

### style_rule_proposals
| Field | Type | Notes |
|---|---|---|
| user_id, style_profile_id | FK | |
| rule_json | **enc** (`EncryptedJSONBFromPlaintext`, column type `jsonb`) | proposed rule `{"key": <edit feature>, "text": <rule>}`; legacy plaintext rows are rewritten by `reencrypt_profiles` |
| evidence_draft_ids | uuid[] | drafts whose edits showed the pattern (≥ 5 of the last 6 edited drafts of the profile's versions) |
| state | enum | `proposed`, `accepted`, `dismissed` (FR-033) |

### commitments
| Field | Type | Notes |
|---|---|---|
| user_id, contact_id | FK | |
| what | **enc** (`EncryptedTextFromPlaintext`, column type `text`, no migration) | never filtered or sorted in SQL (dedupe compares normalised values in Python); a value written before encryption is read as is and encrypted on its next write |
| due | date or timestamptz, nullable | |
| source_message_text | **enc**, nullable | kept beyond message purge while open; row deleted 30 days after `closed_at` |
| closed_at | timestamptz, nullable | set when resolved or deleted |
| source_sent_at | timestamptz | |
| confidence | numeric | ≥ 0.7 stored |
| state | enum | `open`, `resolved`, `deleted` (FR-031) |

`contact_id` is the chat's contact (the person in a private chat, the group as a whole in a group
chat). Extraction stores an item only when its `evidence` quotes the outgoing text and no open
commitment of the same contact has the same normalised `what` (case and spacing ignored) and
`due` (a `due` the outgoing text does not give is dropped first). Concurrent stores for one
(user, contact) are serialised by `pg_advisory_xact_lock(hashtext(user_id || ':' ||
contact_id))` taken before the dedupe read. Resolve and delete keep the row with
`closed_at` set; the daily retention job deletes it, with its source quote, 30 days later.

### task_source_connections
| Field | Type | Notes |
|---|---|---|
| user_id | FK unique | |
| provider | enum | `trello`, `jira` |
| credentials | **enc** jsonb | Trello key+token; Jira later |
| config_json | jsonb | `board_ids` (selected Trello boards; Jira project keys later), `member_id` (the Trello account the credentials belong to; reconnecting another account resets `board_ids`), `consecutive_failures` (task-source failures in a row; `status=error` from 3), `error_alerted` (the control-chat alert of the current failure streak was sent); no Trello content is stored. Every read-modify-write locks the row (`SELECT … FOR UPDATE`, `tasks_conn.get_connection_for_update`) |
| status | enum | `active`, `error`, `disconnected` |

### activity_records  (FR-027)
| Field | Type | Notes |
|---|---|---|
| user_id | FK | |
| kind | enum | `draft_prepared`, `approved`, `edited_and_approved`, `rejected`, `regenerated`, `sent`, `expired`, `dropped`, `read_now`, `profile_refreshed`, `profile_undo`, `rule_accepted`, `data_exported`, `data_deleted` |
| draft_id / conversation_turn_id | FK, nullable | |
| at | timestamptz | |
| meta_json | jsonb | no message content. `profile_refreshed` (one per refresh run, written with the count update): `replaced` ([new id, previous id or null]), `retired` (ids), counts, `noop` (nothing changed: not undoable, but the mark after which own messages are counted next), `pending` (true until the builds are stored; the weekly job re-runs users without a completed record since the scheduled time); its id is the refresh's `batch_id` for ↩️ Undo. `profile_undo`: `batch_id` and counts (an undo), or `batch_id` + `superseded: true` (a Rebuild ended that refresh's undo), or `removed_contact_id` + `removed: true` (🗑 Remove of a contact profile; kept with the activity log, so a later rebuild still knows). `rule_accepted`: proposal and profile ids, feature key. `data_exported`: row counts per table. `data_deleted` is written to the logs only (the table is deleted with the user) |

### bot_fsm_state
| Field | Type | Notes |
|---|---|---|
| chat_id | bigint, PK | control-bot chat |
| state | text, nullable | aiogram FSM state name |
| data | **enc** jsonb | FSM data (may contain draft text during edit) |

Not keyed by `user_id` (exists before the user row during `/start`), but deleted together
with the user on account closure.

### scheduled_jobs
| Field | Type | Notes |
|---|---|---|
| name | text unique | `purge_messages` (daily), `refresh_profiles` (weekly, Sunday 02:00 UTC; contact ranking and slot assignment run inside it, so there is no separate `rank_contacts` job), `reencrypt_profiles` (due at once on the first start that registers it, then daily 04:00 UTC; idempotent re-encryption of legacy plaintext `style_profiles`, `style_rule_proposals.rule_json`, `commitments.what` and `drafts.output_json` content keys) |
| next_run_at | timestamptz | |
| last_run_at / last_status | | at startup a job whose latest scheduled run was claimed (`next_run_at` moved on) but has no `last_run_at` at or after it — a restart interrupted it — is due again at once |

## State machines

### conversation_turns.state
```
waiting ──(due_at reached | user read elsewhere | "read now")──▶ reading
waiting ──(user replies manually | chat disabled)──▶ dropped
reading ──(mark read + draft prepared)──▶ pending_review
reading ──(media-only / cannot draft)──▶ handle_manually
reading ──(user replies manually while the draft is being composed)──▶ dropped
reading ──(chat disabled meanwhile: the composer stores no draft)──▶ dropped
reading ──(restart interrupted it | stuck > 10 min, not being worked on)──▶ waiting
          (due now if already marked read or "read now"; else next_allowed(now))
(new message older than a manual reply already stored) ──▶ dropped (created dropped)
pending_review ──(draft sent)──▶ answered
pending_review ──(draft rejected)──▶ rejected
pending_review ──(user replies manually | chat disabled; the pending draft is dropped)──▶ dropped
pending_review ──(draft expires 24h)──▶ handle_manually
```
004 adds `skipped` (`reading ──(reply gate: skip)──▶ skipped`, and back to `waiting` on
"Draft anyway"): see [004 data-model](../004-skip-closing-messages/data-model.md).
New incoming messages while `waiting` (same sender, within the grouping window) extend the
turn (`last_message_id`) without changing `due_at`. New incoming messages while
`pending_review` set `drafts.outdated=true` and offer regeneration (FR-015); a text message
from the turn's sender also joins that turn (`last_message_id`) so the regenerated draft
covers it. A text message from the sender of a turn in `reading` (being read and drafted)
joins that turn too, instead of opening a parallel one. The composer snapshots the turn's
`last_message_id` when it loads the messages; if it moved by the time the draft is stored (or
the draft being regenerated was flagged outdated meanwhile), the new draft is stored with
`outdated=true`.

A manual reply (outgoing message that is not an agent send) drops only the open turns whose
first message has a lower Telegram message id; a turn created later for a message older than
an already stored manual reply is created `dropped` (`manual_reply`). Incoming and outgoing
messages of one chat are handled one at a time (shared per-chat lock).

"User read elsewhere" and "read now" set `due_at = now()` (regardless of working hours) and
write an `activity_records` row of kind `read_now` for the turn (`meta_json.source =
"read_elsewhere"` for the former); the scheduler then claims the turn on its next tick. A
paused user's turns stay `waiting` (received and stored, never read): the scheduler only
claims turns of users in `active` or `out_of_office`, so `dropped_reason = paused` is not
used. Changing working hours, days or time zone, and /resume, re-plan waiting turns: `due_at =
next_allowed(max(arrived_at + delay_s, now))`, except turns with a `read_now` record (or
`due_at < arrived_at + delay_s`), which keep their `due_at`.

Marking a chat read never goes past the first message of a turn that is still `waiting` in
that chat (`min(target, first_waiting_id - 1)`), in the scheduler and on regenerate.

Lock order for concurrent changes is draft row, then turn row (send, manual-reply drop,
outdated flag, regenerate, reject, expiry), so they wait for each other instead of
deadlocking. `send.py` locks the draft and then its turn *before* calling Telegram; the
manual-reply drop locks the chat's pending drafts, then the turns, and retries the
transaction if a draft appeared in between. Several turns are locked in id order. Disabling a
chat locks the `work_chats` row first (`FOR UPDATE`), then its pending drafts, then their turns,
and drops waiting turns with one conditional `UPDATE … WHERE state = 'waiting'` (a turn the
scheduler claimed meanwhile is never written over); the composer reads the chat row `FOR SHARE`
before it locks the draft and turn, and ends a `reading` turn of a disabled chat as `dropped`
instead of storing a draft.

### drafts.state
```
pending ──(approve: approved_by_user_at set)──▶ sent
pending ──(reject)──▶ rejected
pending ──(regenerate)──▶ superseded  (new draft row, version+1, state=pending)
pending ──(expires_at reached)──▶ expired
pending ──(turn dropped)──▶ dropped
```
Invariant enforced in code and by a DB check constraint:
`state = 'sent' ⇒ approved_by_user_at IS NOT NULL`.

### style_profiles lifecycle
```
(import, rebuild or refresh) ─▶ new row state=current, is_current=true
previous current row ─▶ state=previous (kept for undo); an older previous ─▶ retired
undo of the latest refresh ─▶ swap: previous→current, current→retired; profiles new in that
                              refresh → retired; contacts it retired → current again while
                              fewer than 20 contact profiles are current. Refused (nothing
                              changes) unless every row the refresh created is still current
rebuild ─▶ the latest refresh is superseded (no longer undoable)
contact retired by ranking or removed by the user ─▶ state=retired; deleted after 30 days
previous replaced more than 30 days ago ─▶ deleted, unless the latest undoable refresh would
                              restore it
```
Slots (FR-034): pinned contacts first, then the most active contacts with at least 10 own
messages, up to `max_contact_profiles` (20); every other contact's profile is retired. A draft
uses the contact's current profile, else the global one.

## Validation rules
- `users.delay_min_s ≤ delay_max_s`; same for chat overrides; `0 ≤ min`; `max ≤ 24h`.
- `work_start < work_end`; `working_days` non-empty.
- At most 20 `style_profiles` rows with `scope=contact AND is_current` per user.
- `drafts`: only one row with `state=pending` per `conversation_turn_id`.
- `messages` are inserted only for `work_chats.enabled = true` (checked in intake, not DB).
- `commitments.confidence ≥ 0.7` on insert.

## Relationships (summary)
users 1─1 telegram_sessions, task_source_connections; 1─n work_chats, contacts, messages,
conversation_turns, drafts, style_profiles, commitments, activity_records.
work_chats 1─n messages, conversation_turns. contacts 1─n messages (as sender),
conversation_turns, commitments, style_profiles (scope=contact).
conversation_turns 1─n drafts (one pending). style_profiles 1─n style_rule_proposals.
