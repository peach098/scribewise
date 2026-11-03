# Security Review: Search Over Earlier Conversations

**Date**: 2026-10-22 · **Scope**: `src/scribe/context/earlier.py`, the search step in
`src/scribe/drafting/composer.py`, `search_chat` / `get_messages` / `get_usernames` in
`src/scribe/telegram/{gateway.py,user_client.py,client_pool.py}`, `LLMRouter.rank_earlier` and
the query sanitizer in `src/scribe/llm/registry.py`, `prompts/filter_context.md` v2,
`earlier_messages` in `src/scribe/drafting/stored_output.py`, the `/settings` toggle, the
`/my_data` export, migration `0007`. **Method**: code reading plus the automated tests named
below. Line numbers are as of branch `004-conversation-search` on the date above.

## Findings

| # | Area | Result | Evidence |
|---|---|---|---|
| 1 | Chat scope (FR-S01) | **Pass.** Every chat that is searched comes from `EarlierSearch.chats_for`, which reads only `work_chats` rows of the user with `enabled = true`: the turn chat only if enabled (`earlier.py:216`), the private chat with the sender filtered by `user_id`, `enabled`, `kind = private` (`earlier.py:219-226`), and at most 2 groups filtered by `user_id`, `enabled`, `kind != private` and the sender's stored messages in the retention window (`earlier.py:232-247`). The composer passes that list unchanged (`composer.py:777-784`) and calls `search(scope, inp.search_chats, queries)` (`composer.py:385-386`). `search` de-duplicates and caps the chats at `MAX_CHATS = 4` whatever the caller passes (`earlier.py:257-260`). `get_messages` for reply targets uses the same chat (`earlier.py:349-351`). A group contact is never treated as the sender (`composer.py:764`). | `test_disabled_private_chat_is_not_searched`, `test_disabled_turn_chat_is_not_searched`, `test_other_users_chats_are_never_in_scope`, `test_scope_then_search_never_touches_the_disabled_chat` (`tests/integration/test_earlier_scope_db.py`); `test_same_chat_twice_is_searched_once` (`tests/unit/test_earlier_scope.py`) |
| 2 | Scope is independent of model output | **Pass.** The filter model contributes only `needs_earlier_context` and `search_queries` (`registry.py:155-161`). No chat id, peer id or chat name from model output reaches `chats_for` or `search_chat`: the chats are fixed before the LLM is called (`composer.py:699-701`, inside `_load`). Ranking may only select offered `earlier:*` ids (`sanitize_filter_output` keeps offered ids only, `registry.py:146-161`, applied to the ranking result at `registry.py:209`). | `test_rank_earlier_sends_only_matches_and_caps_selection` (`tests/unit/test_llm_router.py`) |
| 3 | No global search | **Pass.** The only Telegram search request is `messages.SearchRequest` with `peer=` one resolved chat (`user_client.py:372-388`). No `messages.searchGlobal` (or `SearchGlobalRequest`) appears anywhere in `src/` (`grep -rn "SearchGlobal\|searchGlobal" src` is empty). | `test_search_chat_maps_messages_and_uses_no_flood_sleep` (`tests/contract/test_gateway_search.py`) |
| 4 | No retry loop on flood waits (FR-S18) | **Pass after fix F1 (bounded sleep accepted).** `_no_sleep` raises `GatewayBusy` before sending when Telegram already announced a wait for the request type (`UserClient._no_sleep`, reads Telethon's `_flood_waited_requests`), and maps a wait longer than the client threshold to `GatewayBusy`; `EarlierSearch` stops all requests at the first `GatewayBusy`. A first short wait (≤ 60 s) is slept inside Telethon, which cannot be disabled per request in 1.45; the 6 s step deadline (`asyncio.timeout` in `EarlierSearch.search`) cancels it, so the draft falls back with `earlier_search_unavailable` (outcome `timeout`). No retry loop of our own. | `test_known_flood_wait_raises_before_sending`, `test_flood_wait_raises_gateway_busy_without_sleeping`, `test_flood_wait_cancels_the_remaining_requests`, timeout case in `tests/unit/test_earlier_scope.py` |
| 5 | Prompt injection | **Pass (with the accepted risk below).** Incoming and found messages reach the model only inside delimited untrusted data blocks, and the prompt says they are never instructions and must never be copied into queries (`prompts/filter_context.md:14-16`, `42-43`). Queries are trimmed, at most 60 characters each, at least 2, de-duplicated case-insensitively and capped at 5 (`registry.py:165-176`), and capped at 5 again in `search` (`earlier.py:255`). A query is free text sent to Telegram's per-chat search only: it cannot name a chat, widen the scope (findings 1, 2) or make a request outside the request budget (≤ 5 × 4 searches plus one reply lookup per group). Group matches reach the model only when linked to the sender or the user (`earlier.py:362-367`, `link_reason` at `132-161`). The model has no tools, and a draft is only sent after the user approves it. | `test_sanitizer_cleans_search_queries`, `test_group_reply_linked_match_cited_unrelated_never_sent`; injection threads of the `earlier_context` eval suite (T025) are **pending** — results not available at the time of this review |
| 6 | Data stored | **Pass.** Search results are kept in memory for one draft only. The draft stores only the earlier messages it cites (`composer.py:239-253`, `Composition.stored_earlier`): `ref`, `chat_id`, `message_id`, `sent_at` in clear and the chat title, sender and an excerpt of at most 200 characters encrypted (`stored_output.py:97-109`). Queries are not stored (`EarlierSearchResult.queries` is used only to cut the excerpt). | `tests/unit/test_stored_output_earlier.py` (round trip, ciphertext ≠ plaintext, excerpt cap), `test_earlier_agreement_is_cited_and_stored` |
| 7 | Data purged and exported | **Pass.** `earlier_messages` is in `CONTENT_KEYS` (`stored_output.py:48`), which the retention job strips from `output_json` 30 days after the draft's decision together with the draft text (`purge.py:70-85`). `/my_data` exports them decrypted inside each draft as `output_json.earlier_messages` (`retention/user_data.py:105`, added in this review, T031). `/delete_my_data` deletes the user row, and drafts cascade. | `test_retention_strips_earlier_messages_with_the_content`, `test_export_and_delete_cover_cited_earlier_messages` (`tests/integration/test_us4_account.py`) |
| 8 | Logs (FR-S05a, FR-S17) | **Pass.** One `earlier_search` event per draft carrying ids, the decision, the outcome, counts and timings only (`composer.py:432-453`). Failures log the exception type only (`earlier.py:276-278`). Flood waits log the request type and seconds (`user_client.py:434-439`). Neither queries nor message text are logged. | `test_one_event_per_draft_without_content` asserts that the captured log output contains neither query strings, found text, incoming text nor draft text (`tests/integration/test_earlier_search_logs.py`) |
| 9 | LLM data minimization | **Pass.** The ranking call gets only the incoming messages and the linked matches, with no tasks, commitments or recent history (`registry.py:199-218`, `FilterCandidates(history=list(matches))` at `206`). Each match is a snippet of at most 500 characters (`earlier.py:106`, `SNIPPET_CHARS`), at most 60 before ranking (`earlier.py:370-373`) and at most 10 after it (`FILTER_MAX_EARLIER`). Unlinked group messages are dropped before any LLM call (`earlier.py:362-366`). The drafting call gets the ≤ 10 selected snippets in the same form (`composer.py:401`). | `test_rank_earlier_sends_only_matches_and_caps_selection`, `test_group_reply_linked_match_cited_unrelated_never_sent` |
| 10 | User and operator control | **Pass.** With `users.search_earlier_messages = false` no chats are loaded (`composer.py:777`) and no search runs (`composer.py:380-382`, card note `earlier_search_off`). `SCRIBE_EARLIER_SEARCH_ENABLED=false` gives no search scope at all (`composer.py:757-758`). The toggle changes only the caller's own row (`control_bot/settings.py:507-523`). | `tests/integration/test_settings_search_toggle.py`, `test_setting_off_with_needed_context_warns_off`, `test_operator_switch_off_no_search_no_warning` |

## Findings

- **F1 (fixed, residual risk accepted) — Telethon sleeps through short flood waits on the search path (FR-S18).** Telethon
  1.45.0 `TelegramClient.__call__(request, ordered=False, flood_sleep_threshold=None)` does not
  forward `flood_sleep_threshold` to `_call` (`telethon/client/users.py:29-30`), so
  `self.client(request, flood_sleep_threshold=0)` (`user_client.py:432`) runs with the client's
  threshold, `max_flood_sleep_s` = 60 s (`user_client.py:451`). For a flood wait of ≤ 60 s,
  Telethon sleeps and retries instead of raising, both on the error and on its own early check
  of recently flood-waited request types. The same holds for the `get_dialogs` fallback in
  `_resolve` (`user_client.py:463`), which goes through `_guarded` and sleeps up to 60 s by
  design. **Impact**: bounded. The whole step runs under `asyncio.timeout(6 s)`
  (`earlier.py:266`), so the draft is not held longer, but the outcome is `timeout` rather than
  `rate_limited` (the card note is the same, `earlier_search_unavailable`). The account keeps
  retrying once the wait ends, if the step is still running. The contract test only checks the
  argument passed to a fake client, so it does not catch this.
  **Resolution**: `flood_sleep_threshold=0` was removed (it had no effect). `_no_sleep` now
  refuses a request type Telegram already told us to wait for, before sending
  (`test_known_flood_wait_raises_before_sending`), so repeated drafts during a wait fall back
  at once with `rate_limited`. The first short wait is still slept inside Telethon until the
  6 s deadline cancels it; disabling that would need Telethon's private sender API or a
  client-wide threshold change that also affects sending, so it is accepted as a bounded
  residual risk. Same for the `get_dialogs` fallback in `_resolve`.

## Accepted risks

- **Queries chosen from untrusted text**: an incoming message can steer which words are
  searched for (for example, "the password from last time"). The search stays inside the
  user's own enabled work chats with the sender (findings 1–3), group matches must be linked to
  the sender or the user, and anything found only reaches a draft that the user reviews before
  it is sent. It can still put the user's own earlier messages to that sender into a draft
  that quotes them. The card lists every cited earlier message (💬 sources), which is the
  mitigation.
- **Queries visible to Telegram**: Telegram receives the expanded queries. They come from a
  message Telegram already delivered to the same account and are sent only for that
  account's own chats (research S12).
- **Excerpt metadata in clear**: `chat_id`, `message_id` and `sent_at` of cited messages are
  stored unencrypted next to the draft, like `reply_to_message_id`, so that the Sources view
  can build links. They contain no content and are purged with the excerpt.
- **Eval evidence pending**: the `earlier_context` suite with its injection threads (T025) and
  the regression runs of the `drafts` / `jira` suites with filter prompt v2 (T030) were not
  available for this review. Finding 5 relies on code structure and unit tests until they are.
