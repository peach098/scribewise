# Data Model: Search Over Earlier Conversations

**Feature**: [spec.md](spec.md) | **Research**: [research.md](research.md)

No new tables and no stored index (FR-S03). One column, one migration, new fields inside the
existing encrypted draft output, and transient in-memory types.

## Persistent changes

### `users` (column added, migration `0007_search_earlier`)

| Column | Type | Rule |
|---|---|---|
| `search_earlier_messages` | `BOOLEAN NOT NULL DEFAULT true` | FR-S16. Toggled from `/settings`; read by the composer at draft time. |

Downgrade drops the column.

### `drafts.output_json` (system field added, no migration)

`stored_output.pack_output` / `load_stored_output` gain `earlier_messages`, a list of the
earlier messages **cited** by the draft (S7):

| Field | Stored | Rule |
|---|---|---|
| `ref` | clear | `earlier:<n>`, as cited in `output.sources` |
| `chat_id` | clear | Telegram chat id of the work chat (for the link) |
| `message_id` | clear | Telegram message id (for the link) |
| `sent_at` | clear | ISO timestamp (UTC) |
| `chat_title` | **encrypted** | work chat title |
| `sender` | **encrypted** | display name, or `me` for the user |
| `excerpt` | **encrypted** | ≤ 200 chars around the match |

`StoredOutput.earlier_messages` is `[]` for drafts stored before this feature. Lifetime = the
draft's (existing purge 30 days after the final state; `/delete_my_data` removes it).

`drafts.llm_usage_json` gains an optional `rank` entry (usage record of the second
`retrieve.filter_context` call; metadata and cost only).

## Transient types (never persisted)

### `SearchPlan` (from the filter call, S1)

| Field | Type | Rule |
|---|---|---|
| `needs_earlier_context` | bool | model decision (FR-S05) |
| `search_queries` | list[str] | ≤ 5 items, each 2–60 chars, trimmed, de-duplicated; empty when not needed |

Added to `FilterContextOutput` as optional fields with defaults (`False`, `[]`) so older
adapters / fakes stay valid; `sanitize_filter_output` enforces the limits and clears both on
the ranking call.

### `FoundMessage` (gateway result, S3)

| Field | Type | Note |
|---|---|---|
| `chat_id` | int | work chat |
| `message_id` | int | |
| `sender_peer_id` | int | |
| `sender_display_name` | str | |
| `is_outgoing` | bool | written by the user |
| `text` | str | message text, else media caption, else the document file name |
| `sent_at` | datetime | |
| `reply_to_id` | int \| None | |
| `thread_top_id` | int \| None | reply thread top |
| `mentioned_peer_ids` | frozenset[int] | from mention entities |
| `mentioned_usernames` | frozenset[str] | lower-case `@username`s in the text |

### `EarlierMatch` (after linking and de-duplication, S5–S6)

`FoundMessage` + `chat_title`, `ref` (`earlier:<n>`), `link_reason`
(`private` / `own` / `sender` / `reply` / `replied_by` / `mention` / `thread`). Becomes a
`HistoryItem` (`id=ref`, `is_user`, `sender`, `text` ≤ 500 chars, `sent_at`) for the ranking and
drafting calls.

### `EarlierSearchResult`

| Field | Type | Note |
|---|---|---|
| `outcome` | `ok` / `no_match` / `failed` / `rate_limited` / `timeout` / `not_started` | → card warning (S8) and log (S10) |
| `matches` | list[EarlierMatch] | linked, de-duplicated, newest first, ≤ 60 |
| `stats` | counts | chats, queries, requests, raw matches, dropped_unlinked, duration_ms |

## State and flow per draft

```text
filter call ──► needs_earlier_context?
   │ no ─────────────────────────────► draft (as today), log decision=not_needed
   │ yes, setting off ───────────────► draft + warning earlier_search_off, log decision=off
   │ yes, setting on
   ▼
EarlierSearch (≤ 6 s) ── failed/timeout/rate-limited ──► draft + earlier_search_unavailable
   │ 0 matches ───────────────────────────────────────► draft + earlier_search_no_match
   ▼
ranking call (filter route, ≤ 10 ids) ── none selected ─► draft + earlier_search_no_match
   ▼
draft request: history = recent selection + ranked earlier matches
   ▼
persist: cited earlier matches → output_json.earlier_messages (encrypted content)
```

## Validation rules

- Only enabled work chats are searched (FR-S01): scope is built from `work_chats.enabled`
  at search time; the gateway is never called with any other chat id.
- Group matches pass the link rules (FR-S05b) before they become `HistoryItem`s.
- `earlier:*` ids in model output are valid only if offered in the request (existing
  `invalid_source_ids` / regenerate-then-strip path).
- No query string, match text or excerpt in logs (FR-S17).
