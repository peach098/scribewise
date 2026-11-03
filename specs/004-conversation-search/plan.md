# Implementation Plan: Search Over Earlier Conversations

**Branch**: `004-conversation-search` | **Date**: 2026-10-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/004-conversation-search/spec.md`

## Summary

Let the agent find earlier messages relevant to an incoming message while it drafts, beyond the
fixed recent window (40 messages in the chat, 20 from the contact, 30 days, since connection).
The existing cheap `retrieve.filter_context` call (prompt v2) additionally decides whether
earlier context is needed and proposes up to 5 word-based queries covering paraphrases and
word forms (S1). Only then a new `EarlierSearch` component runs per-chat server-side Telegram
search (`messages.search`, never global) over the sender's enabled work chats (turn chat,
private chat, ≤ 2 groups), with a 6 s deadline and no flood-wait sleeping (S3, S4). Group
matches are kept only when linked to the sender or the user (own, reply, mention, thread; S5).
A second call of the same filter route ranks the matches and selects ≤ 10 (S2); they join the
draft request as citable `earlier:*` history items. Cited ones are stored, encrypted, inside
the draft for the card's Sources view and purged with it (S7). Failures, no match and "turned
off" show as card warnings (S8). A per-user toggle in `/settings` (default on) and an operator
kill switch control the feature (S9). No index, no new table, no new route or dependency; the
bot search command was removed from scope in clarification.

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: unchanged: Telethon (user clients), aiogram 3, pydantic v2,
SQLAlchemy 2 async + Alembic, structlog, LLM adapters behind `LLMRouter`. **No new dependency,
no new LLM route**; `prompts/filter_context.md` → version 2.

**Storage**: PostgreSQL 16; migration `0007_search_earlier` (one column on `users`). New
encrypted system field `earlier_messages` inside `drafts.output_json`. No stored search index
or message copies (FR-S03).

**Testing**: pytest + pytest-asyncio; unit tests for link rules, sanitizer, stored output;
contract tests for `UserClient.search_chat` / `get_messages` against a faked Telethon client;
integration tests with `FakeMessagingGateway.search_index` and the fake LLM adapter; eval suite
`earlier_context` (`evals/run.py --suite earlier_context`).

**Target Platform**: Linux server, Docker Compose (unchanged)

**Project Type**: single long-running service (unchanged)

**Performance Goals**: drafts that need no earlier context: no extra LLM call or Telegram
request; drafts with search: ≤ 5 s added for 95% (SC-003), hard ceiling 10 s for search (≤ 6 s)
plus ranking (remaining time, ≥ 2 s) (FR-S08).

**Constraints**: only enabled work chats are searched (FR-S01); ≤ 20 search requests per draft,
≤ 4 in flight per user; no sleeping on flood waits on the draft path (FR-S18); only linked
matches (≤ 60, ≤ 500 chars each) go to the ranking call and ≤ 10 to drafting (FR-S10); nothing
stored except cited excerpts inside drafts (FR-S02, FR-S03); no content in logs (FR-S17).

**Scale/Scope**: ~100 users; search runs only on drafts whose incoming message refers to the
past (expected ~10–20% of drafts); 2 user stories, 16 active FRs (FR-S01–S10 incl. S04a, S05a,
S05b, FR-S16–S18).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status (pre-research) | Status (post-design) |
|---|---|---|---|
| I. Human approval before sending | Search only feeds draft context; no new send path. | PASS | PASS: `EarlierSearch` only reads (`search_chat`, `get_messages`); no code path reaches `sending/send.py`; drafts still go through approve/edit/reject. |
| II. Privacy & data minimization | Only enabled work chats; only relevant messages to the LLM; nothing stored beyond retention; no content in logs. | PASS (FR-S01, FR-S02, FR-S03, FR-S05b, FR-S10, FR-S17) | PASS: per-chat search only (no `searchGlobal`, S3); deterministic link rules drop unrelated group messages before any LLM call (S5); ≤ 60 snippets to ranking, ≤ 10 to drafting; only cited excerpts stored, encrypted, purged with the draft, covered by `/my_data` and `/delete_my_data` (S7, S12); log event with counts only and a test for it (S10). |
| III. Grounded replies, no fabrication | Earlier facts must be cited; no match → placeholder, not a guess. | PASS (FR-S07, FR-S09) | PASS: `earlier:*` ids go through the existing source validation (regenerate once, then strip + `needs_info`); `earlier_search_no_match` / `earlier_search_unavailable` make missing context visible; eval threshold "0 invented referenced facts" (S11). |
| IV. Faithful personal style | Not affected. | PASS | PASS: found messages are context only, never style samples; style pipeline untouched. |
| V. Evaluation-driven development | Prompt change and retrieval change measured; deterministic code tested first. | PASS | PASS: new `earlier_context` eval suite with decision, citation, no-fabrication and injection thresholds; existing `drafts` and `jira` suites must pass with filter prompt v2; unit/contract/integration tests for link rules, gateway, composer paths (S11). |
| VI. Simplicity & observability | Reuse existing route and gateway; structured logs. | PASS | PASS: no new route, table, index, queue or dependency; one new module (`context/earlier.py`) and two narrow gateway methods; one `earlier_search` log event per draft; ranking cost recorded in `llm_usage_json.rank`. |
| Security & data handling | Injection resistance; minimum scopes; encryption at rest. | PASS | PASS: the search scope is computed in code from the turn (never from model output), so an injected "search all chats" cannot widen it; queries are capped and only sent to Telegram for the user's own enabled chats; excerpts encrypted; no new external service or credential. |

**Gate result**: PASS, no exceptions.

## Project Structure

### Documentation (this feature)

```text
specs/004-conversation-search/
├── plan.md              # This file
├── research.md          # Phase 0 output (S1–S13)
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── internal-interfaces.md   # gateway search, EarlierSearch, router, composer, stored output
│   └── control-chat.md          # card labels and warnings, Sources view, /settings toggle
├── checklists/requirements.md
└── tasks.md             # Phase 2 output (/speckit-tasks - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
alembic/versions/0007_search_earlier.py        # users.search_earlier_messages
prompts/filter_context.md                      # version: 2 (needs_earlier_context, search_queries)
src/scribe/
├── config.py                                  # + earlier_search_enabled, earlier_search_timeout_s
├── db/models.py                               # + User.search_earlier_messages
├── context/
│   ├── earlier.py                             # new: EarlierSearch, SearchScope, link rules
│   └── history.py                             # candidates expose Telegram ids (dedupe, S6)
├── llm/
│   ├── provider.py                            # FilterContextOutput + 2 optional fields
│   └── registry.py                            # sanitizer limits, rank_earlier(), FILTER_MAX_EARLIER
├── drafting/
│   ├── composer.py                            # search step, earlier:* sources, warnings, wiring
│   └── stored_output.py                       # earlier_messages (encrypted content)
├── telegram/
│   ├── gateway.py                             # FoundMessage, search_chat(), get_messages()
│   ├── user_client.py                         # Telethon messages.search, no flood sleep
│   ├── client_pool.py                         # delegation
│   ├── fake_gateway.py                        # search_index, call recording
│   └── control_bot/
│       ├── render.py                          # 💬 labels, new warnings
│       ├── review.py                          # Sources view lines, Composer(earlier_search=…) on regenerate
│       └── settings.py                        # search toggle
├── i18n/en.py, i18n/ru.py                     # card, sources, settings texts
└── __main__.py                                # build EarlierSearch, pass to Composer

tests/
├── unit/        # test_earlier_link_rules, test_earlier_scope, test_llm_router (sanitizer),
│                # test_stored_output_earlier, test_draft_card (labels/warnings)
├── contract/    # test_gateway_search (UserClient search/get_messages, flood → GatewayBusy)
└── integration/ # test_earlier_search_drafts (cited, no match, unavailable, off, not needed,
                 # group scoping, regenerate), test_settings_search_toggle, migration test

evals/threads/earlier_context.jsonl, evals/run.py (--suite earlier_context), evals/thresholds.yml
```

**Structure Decision**: Single package `src/scribe`. Search logic lives in one new module,
`context/earlier.py`, next to `history.py` (the recent window it complements); Telegram access
goes through two new methods on the existing `MessagingGateway` protocol so tests use the fake
gateway; the LLM side reuses the `retrieve.filter_context` route for both the decision/queries
and the ranking.

## Complexity Tracking

No constitution violations to justify.

## Phase Summary

- **Phase 0 (research.md)**: S1 decision + query expansion in the filter route; S2 ranking via
  the same route; S3 per-chat `messages.search`; S4 chat scope and request budget; S5 group link
  rules; S6 ids and de-duplication; S7 what the draft stores; S8 fallbacks and card notes; S9
  user setting and kill switch; S10 logging; S11 evals; S12 privacy; S13 wiring.
- **Phase 1**: data-model.md, contracts (internal interfaces, control chat), quickstart.md.
- **Phase 2**: tasks.md (`/speckit-tasks`).
- **Rollout**: ship with `SCRIBE_EARLIER_SEARCH_ENABLED=true`; watch `earlier_search` outcomes
  (`rate_limited` share) on a test server before enabling for all users.
