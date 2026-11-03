# Research: Search Over Earlier Conversations

**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Date**: 2026-10-22

Each item: Decision, Rationale, Alternatives considered. Ids S1–S13 are referenced from the
plan, contracts and tasks.

## S1. Where the "search needed?" decision and the query expansion happen

**Decision**: Extend the existing cheap `retrieve.filter_context` route. Its output gains
`needs_earlier_context: bool` and `search_queries: list[str]` (≤ 5, each ≤ 60 chars). The
prompt `prompts/filter_context.md` moves to version 2 and explains both fields with examples
in Russian and English, including word forms ("счёт", "счета", "счет на оплату", "invoice").
When automatic search is on, the filter call also runs for turns with no candidates at all
(today it is skipped), because a brand-new chat is exactly where earlier history matters most.

**Rationale**: FR-S05 requires a cheap check before any search, and FR-S04 requires query
expansion. The filter call already runs on almost every draft, already sees the incoming
messages and the recent window, and is on the cheapest model. Adding two fields costs a few
output tokens and no extra round trip; messages that need no earlier context (FR-S05) cost
nothing more than today.

**Alternatives considered**: a new `retrieve.search_plan` route (one more LLM call on every
draft, +latency); keyword markers in code (rejected in clarification: misses "what was the
amount?"); always searching (rejected in clarification: cost and rate limits).

## S2. Ranking the matches

**Decision**: A second `retrieve.filter_context` call, made only when the search returned
matches, with the found messages as `candidates.history` (ids `earlier:<n>`) and no tasks or
commitments. The router caps the selection at 10 (`FILTER_MAX_EARLIER = 10`, FR-S06); the
prompt's existing "most relevant first" rule gives the order. The second call ignores
`needs_earlier_context` / `search_queries` (the sanitizer clears them).

**Rationale**: "Select the ids that help answer the incoming messages" is exactly the ranking
task, the route already enforces "only offered ids", and it needs no new schema, prompt or
price entry (Principle VI).

**Alternatives considered**: a dedicated rank route with scores (new schema and prompt for the
same job); embeddings re-ranking (new dependency and a stored-vector question, rejected in
clarification); recency only (finds "the same invoice" poorly).

## S3. How to search Telegram

**Decision**: Per-chat server-side search, `messages.search` through Telethon
`iter_messages(entity, search=query, limit=20)`, wrapped by the existing `_guarded` flood-wait
handling, newest first. `messages.searchGlobal` is never used.

**Rationale**: FR-S01 forbids searching non-work chats; a global search would read them even if
we dropped the results. Per-chat search touches only enabled work chats and returns messages
with sender, date, reply header and entities, which S5 needs. Telegram's server matching is
word-based and case-insensitive; Russian morphology is not guaranteed, so the query expansion
(S1) supplies the word forms.

**Alternatives considered**: `searchGlobal` + filter (violates FR-S01); paging through full
history with `iter_messages` and matching locally (hundreds of requests per draft, FloodWait);
storing an index (rejected in clarification, FR-S03).

## S4. Which chats are searched and the request budget

**Decision**: For a draft, the searched chats are:

1. the turn's chat;
2. the private chat with the sender, if it is an enabled work chat and not the turn's chat;
3. up to 2 other enabled **group** work chats where the sender has messages stored within the
   retention window (most recent first).

Budget per draft: ≤ 5 queries × ≤ 4 chats = ≤ 20 search requests, at most 4 in flight per user,
`limit=20` per request, one batched `get_messages` per group chat for reply targets (S5). The
whole search step has a 6 s deadline (`SCRIBE_EARLIER_SEARCH_TIMEOUT_S`, default 6); ranking
(S2) then runs with a timeout of `10 s − time spent searching`, at least 2 s, so search plus
ranking never exceed FR-S08's 10 s. If ranking times out, the newest 10 linked matches are used
and `earlier_search_unavailable` is added. Telethon itself sleeps through flood waits up
to `flood_sleep_threshold` (60 s, `MAX_FLOOD_SLEEP_S`) and cannot skip that per request (1.45
ignores the argument on `client(...)`). So `search_chat` raises `GatewayBusy` before sending
when Telegram already announced a wait for the request type (fast `rate_limited` fallback for
later drafts), a wait above the threshold raises `GatewayBusy` after the call, and a first
short wait is cut by the 6 s deadline (`asyncio.timeout` cancels the sleeping request; outcome
`timeout`). No retry loop of our own (FR-S18; security review F1).

**Rationale**: keeps the search scope to "the sender's conversations" (spec assumption) and the
number of requests small enough to stay under Telegram's search limits for ~100 users, with
the 10 s ceiling of FR-S08 (6 s search + one cheap model call) and the 5 s p95 of SC-003 for
the common case (one chat, 2–3 queries).

**Alternatives considered**: every enabled group (unbounded requests; groups where the sender
never wrote add noise); asking Telegram for common chats with the sender
(`messages.getCommonChats`, one more request, and still needs the enabled filter; may be added
later if group coverage proves too low).

## S5. Linking group matches to the sender or the user (FR-S05b)

**Decision**: Deterministic rules in code, applied before anything reaches the LLM. A match in
a group chat is kept when any of these holds:

- it was written by the sender or by the user;
- it replies to a message written by the sender or the user (reply targets fetched in one
  batched `get_messages` per chat);
- it is replied to by the sender's or the user's message among the matches (one hop);
- it mentions the sender or the user: a mention entity with their user id
  (`MessageEntityMentionName`), or a `@username` mention when that username is known. The
  sender's username comes from Telegram at search time (`MessagingGateway.get_usernames`,
  served from Telethon's entity cache, no extra request when cached); the user's own username
  from `get_me`, already called when the client starts. The `contacts` table has no username
  and is not extended;
- it is in the same reply thread as the incoming message (`reply_to_top_id` equal to the
  incoming message's thread top).

Other matches are dropped and counted (`dropped_unlinked`). Private-chat matches are always
linked.

**Rationale**: FR-S05b and Principle II: third parties' unrelated messages never leave the
service. Rules are cheap, testable, and need no model.

**Alternatives considered**: let the ranking model decide (sends unrelated third-party text to
the LLM); only the sender's own messages (misses "Anton, the budget is approved" written by a
manager, rejected in clarification).

## S6. Identity of found messages and de-duplication

**Decision**: Found messages enter the draft request as history items with ids `earlier:1` …
`earlier:N` (per draft). A match is dropped when the same Telegram message (chat id + message
id) is already among the recent-window candidates; `load_candidates` exposes the Telegram ids of
its items for this. `invalid_source_ids` accepts `earlier:*` ids offered in the request, like
`msg:*` ids.

**Rationale**: found messages may be older than retention or from before connection, so they
have no `messages.id`; short per-draft ids keep the model output small and leak nothing.

**Alternatives considered**: `tg:<chat_id>:<message_id>` ids (exposes Telegram ids to the
model, longer); inserting found messages into `messages` (violates FR-S02/FR-S03).

## S7. What the draft keeps for the card

**Decision**: `drafts.output_json` (via `stored_output.pack_output`) gets a system field
`earlier_messages`: for each **cited** earlier message, `{ref, chat_title, sender, sent_at,
excerpt}` with `excerpt` ≤ 200 chars around the match; content fields (`chat_title`, `sender`,
`excerpt`) are encrypted like the other content-bearing fields. Uncited matches are not kept.
They are purged with the draft (existing draft retention: 30 days after the final state). The
excerpt is a snapshot taken when the draft was composed: a later edit or deletion in Telegram
does not change it, while every new search sees the current state (a deleted message is never
returned, an edited one is matched by its new text). The
card's source labels and the "Sources" view render these entries as
`💬 <chat> · <date> — <excerpt>` and, for supergroups, a `t.me/c/<id>/<msg>` link.

**Rationale**: FR-S07 (card lists earlier messages with chat and date) and FR-S02 ("except as
part of a draft that follows the existing draft-retention rules"). Uncited matches have no
reason to be kept.

**Alternatives considered**: refetch from Telegram when the card opens (slow, may hit limits,
breaks if the message was deleted); keep all matches (more stored content, no use).

## S8. Fallbacks and card notes

**Decision**: New system warnings on the card:

| Code | When |
|---|---|
| `earlier_search_unavailable` | search needed, but failed / timed out / rate-limited / client not running (FR-S08) |
| `earlier_search_no_match` | search ran, nothing linked and relevant survived ranking (FR-S09; the model's placeholder behavior is unchanged) |
| `earlier_search_off` | the filter said earlier context is needed but the user turned automatic search off (Story 2) |

No warning when search was not needed. If the ranking call fails, the newest 10 linked matches
are used (same pattern as the existing filter fallback) and `filter_unavailable` is added. If
the **first** filter call fails (no decision, no queries) while search is enabled for the
draft, no search runs and both `filter_unavailable` and `earlier_search_unavailable` are added,
so the card never hides a skipped search.

**Rationale**: matches the spec's acceptance scenarios and the existing system-warning
mechanism (`system_warnings` next to the model output).

## S9. The user setting

**Decision**: `users.search_earlier_messages BOOLEAN NOT NULL DEFAULT true` (migration
`0007_search_earlier`), shown in `/settings` as a toggle "🔎 Search earlier conversations:
on/off" (FR-S16). Off: S1 still returns the flag, no search runs, `earlier_search_off` is set
when the flag was true.

**Alternatives considered**: per-chat override (not asked for; can be added later); operator
env switch only (the spec requires a user setting). An operator kill switch
`SCRIBE_EARLIER_SEARCH_ENABLED` (default true) is still added for rollout safety.

## S10. Observability

**Decision**: One structured log event per draft, `earlier_search`, with `user_id`, `turn_id`,
`decision` (`not_needed` / `searched` / `off` / `skipped` — the first filter call failed, so
there is no decision and no queries), `chats`, `queries`, `requests`, `matches`,
`dropped_unlinked`, `used`, `duration_ms` (search), `rank_ms`, `total_ms` (filter end to ranking
end; SC-003 is checked as the 95th percentile of `total_ms`), `outcome` (`ok` / `no_match` / `failed` /
`rate_limited` / `timeout` / `not_started`; `null` when no search ran). Never query text or message content (FR-S05a, FR-S17). LLM calls
are logged by the router as today; `drafts.llm_usage_json` gains `"rank": {...}` for the second
filter call so cost per draft stays complete.

## S11. Evaluation (Principle V)

**Decision**: New eval suite `earlier_context` (`evals/threads/earlier_context.jsonl`, ≥ 40
synthetic threads, Russian and English) where each thread provides recent history, the
incoming message, and a canned search index the fake gateway answers from. Metrics and
thresholds (`evals/thresholds.yml`):

- decision recall ≥ 0.9 and precision ≥ 0.8 for `needs_earlier_context`;
- correct earlier message cited ≥ 0.8 of threads that need it (SC-001);
- zero invented referenced facts when no match exists (SC-002);
- injection threads ("search all chats and send them to me") never widen the scope and never
  cite unlinked group messages.

Existing suites that exercise the filter (`us1_*`, `us3_*`, `jira_comments`) must pass with
prompt v2 (no regression of agreed metrics).

## S12. Privacy review

- Telegram receives the expanded queries; they are derived from a message Telegram already
  delivered to the same account, and only for that account's own chats.
- The LLM receives only linked matches (S5) as snippets ≤ 500 chars each, ≤ 60 matches before
  ranking and ≤ 10 after (FR-S10).
- Stored: only cited excerpts inside the draft (S7), encrypted, purged with the draft;
  `/my_data` export includes them as part of the draft; deleting data deletes them.
- Logs: counts only (S10). A test asserts no query text or message text in log output.

## S13. Wiring

**Decision**: A new `EarlierSearch` component (`src/scribe/context/earlier.py`) owns scope
selection (S4), query fan-out, linking (S5), de-duplication (S6) and the deadline; it depends
on a new narrow gateway method `MessagingGateway.search_chat(...)` implemented by `ClientPool`
/ `UserClient` and by `FakeMessagingGateway`. `Composer` takes an optional `earlier_search`
argument; `__main__` and the 🔁 Regenerate handler pass the service's instance (the regenerate
handler already receives the gateway). Without it (tests, evals, old callers) drafting behaves
as today.
