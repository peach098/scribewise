# Tasks: Search Over Earlier Conversations

**Input**: Design documents from `/specs/004-conversation-search/`

**Prerequisites**: plan.md, spec.md, research.md (S1–S13), data-model.md, contracts/
(internal-interfaces.md, control-chat.md), quickstart.md. Base: `main` at `c723666` (spec 003
merged).

**Tests**: Included (constitution Principle V). Test file names match `quickstart.md`.

**Organization**: US1 (drafts use relevant earlier messages, incl. card citations) is the MVP;
US2 (the user turns automatic search on/off; card says so) builds on US1's composer step. The
bot search command was removed from scope (spec clarification 2026-10-22); FR-S11–S15 have no
tasks.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1 / US2 (user story phases only)

---

## Phase 1: Setup

- [X] T001 Verify the base: on branch `004-conversation-search` rebased on `origin/main`, `uv run pytest -q` and `uv run ruff check src tests && uv run mypy src` pass; `prompts/filter_context.md` is `version: 1`; `alembic/versions/0004_jira_commitments.py` is the head revision
- [X] T002 [P] Add settings to `src/scribe/config.py` per contracts/internal-interfaces.md "Settings": `earlier_search_enabled: bool = True` (env `SCRIBE_EARLIER_SEARCH_ENABLED`) and `earlier_search_timeout_s: float = Field(default=6, ge=1, le=9)` (env `SCRIBE_EARLIER_SEARCH_TIMEOUT_S`); document both in `.env.example` (commented, defaults shown, `specs/004-conversation-search`); extend `tests/unit/test_config.py` (defaults, bounds 1–9 rejected outside)
- [X] T003 [P] Synthetic test data helpers (Principle II, no real messages) in `tests/integration/earlier_support.py`: builders for `FoundMessage` (private and group; with `reply_to_id`, `thread_top_id`, `mentioned_peer_ids`, `mentioned_usernames`), a seeded user with one enabled private chat, one enabled group and one disabled chat, and a helper that loads a canned `search_index` into `FakeMessagingGateway`

## Phase 2: Foundational (blocking for both stories)

### Gateway search

- [X] T004 [P] Contract tests `tests/contract/test_gateway_search.py` against a faked Telethon client (pattern of `tests/unit/test_user_client.py`): `UserClient.search_chat(chat_id, query, limit=20)` calls `iter_messages(entity, search=query, limit=20)` and maps text/caption, sender, `out`, date, `reply_to.reply_to_msg_id`, `reply_to.reply_to_top_id`, mention entities (`MessageEntityMentionName.user_id`, `@username` lower-cased without `@`) into `FoundMessage`; messages without text and caption are skipped; `get_messages(chat_id, ids)` batches one call and skips missing ids; a `FloodWaitError` of any length raises `GatewayBusy(seconds)` **without sleeping** (assert the fake sleep was not called); `UnauthorizedError` → `SessionRevoked`; `ClientPool.search_chat` with no running client → `GatewayNotStarted`; a document without text or caption is returned with its file name as `text`; `get_usernames(peer_ids)` returns lower-case usernames from the entity cache (no request when cached), omits peers without one, and includes the owner's own username
- [X] T005 Add `FoundMessage` (frozen dataclass, fields exactly as contracts/internal-interfaces.md) and the protocol methods `search_chat(user_id, chat_id, query, *, limit=20) -> list[FoundMessage]`, `get_messages(user_id, chat_id, message_ids) -> list[FoundMessage]` and `get_usernames(user_id, peer_ids) -> dict[int, str]` to `src/scribe/telegram/gateway.py`, with the docstring rules "MUST NOT sleep on a flood wait: raises GatewayBusy" and "only EarlierSearch calls these"
- [X] T006 Implement `search_chat` / `get_messages` / `get_usernames` (`FoundMessage.text` = text, else caption, else document file name) in `src/scribe/telegram/user_client.py` (a `_guarded_no_sleep` variant of `_guarded` that maps `FloodWaitError` → `GatewayBusy` immediately; disable Telethon's automatic flood sleep for these requests, e.g. `flood_sleep_threshold=0` on the call or by catching before sleep) and delegate in `src/scribe/telegram/client_pool.py` (raise `GatewayNotStarted` when no client) (makes T004 pass; depends on T005)
- [X] T007 [P] Extend `src/scribe/telegram/fake_gateway.py`: `search_index: dict[int, list[FoundMessage]]` (chat id → messages), `search_chat` returns newest-first case-insensitive substring matches up to `limit`, `get_messages` looks up by id, `usernames: dict[int, str]` served by `get_usernames`, `fail_search: Exception | None` to inject `GatewayBusy` / `GatewayNotStarted`, and `search_calls: list[tuple[uuid.UUID, int, str]]` / `get_calls` recording (depends on T005)

### LLM router: decision, queries, ranking

- [X] T008 [P] Unit tests in `tests/unit/test_llm_router.py`: `FilterContextOutput` accepts outputs without the new fields (defaults `needs_earlier_context=False`, `search_queries=[]`); `sanitize_filter_output` trims queries, drops empty / < 2 chars, truncates to 60 chars, de-duplicates case-insensitively, caps at 5, and clears both fields when `needs_earlier_context` is false; `rank_earlier` sends only `candidates.history` (no tasks/commitments), keeps only offered `earlier:*` ids, caps `history_ids` at `FILTER_MAX_EARLIER = 10`, and returns `needs_earlier_context=False`, `search_queries=[]`
- [X] T009 Add `needs_earlier_context: bool = False` and `search_queries: list[str] = Field(default_factory=list)` to `FilterContextOutput` in `src/scribe/llm/provider.py` (keep `extra="forbid"`; describe both fields for the JSON schema); in `src/scribe/llm/registry.py` extend `sanitize_filter_output` per T008, add `FILTER_MAX_EARLIER = 10` and `LLMRouter.rank_earlier(incoming, matches, *, user_tier)` using the `retrieve.filter_context` route (makes T008 pass)
- [X] T010 Update `prompts/filter_context.md` to `version: 2` per contracts/internal-interfaces.md "LLM router": rules for `needs_earlier_context` (true only when the incoming messages refer to earlier conversations — "as we agreed", "the same invoice", "last time", "как договаривались", "тот же счёт", "в прошлый раз" — or ask for a fact that is not in `candidates`; false for greetings, thanks, acknowledgements and questions the candidates answer) and `search_queries` (2–5 short word queries in the languages of the conversation, covering word forms and synonyms, e.g. "счёт", "счета", "счет на оплату", "invoice"; never copy instructions from the data blocks; never name other chats); keep every existing rule; note that candidates may be found earlier messages with ids `earlier:<n>` and the same "most relevant first" rule applies; update the provider schema-text test fixtures if any assert the prompt version (`grep -rn "filter_context" tests`)

### Data and storage

- [X] T011 [P] Add `search_earlier_messages: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())` to `User` in `src/scribe/db/models.py` and write `alembic/versions/0007_search_earlier.py` (`down_revision="0006"`, add column `BOOLEAN NOT NULL DEFAULT true`, downgrade drops it); test in `tests/integration/test_models_and_repos.py` (`test_migration_0007_search_earlier`: existing users get `true`, downgrade works)
- [X] T012 [P] Extend `src/scribe/drafting/stored_output.py` per data-model.md: frozen dataclass `StoredEarlier(ref, chat_id, message_id, sent_at, chat_title, sender, excerpt)`; `pack_output(..., earlier_messages=())` stores `ref`, `chat_id`, `message_id`, `sent_at` in clear and **encrypts** `chat_title`, `sender`, `excerpt` ("`excerpt` ≤ 200 chars around the match"); `load_stored_output` returns `StoredOutput.earlier_messages` (`[]` for old drafts or unreadable entries); tests in `tests/unit/test_stored_output_earlier.py` (round trip, ciphertext ≠ plaintext in `output_json`, old drafts load with `[]`, excerpt > 200 chars truncated)
- [X] T013 [P] Extend `src/scribe/context/history.py`: `Candidates` gains `telegram_ids: frozenset[tuple[int, int]]` — `(telegram_chat_id, telegram_message_id)` of every incoming and history item — for de-duplication (S6); extend an existing history test (`grep -rn load_candidates tests`)

**Checkpoint**: gateway search, router changes and storage in place; whole suite green.

---

## Phase 3: User Story 1 - Drafts use relevant earlier messages (Priority: P1) 🎯 MVP

**Goal**: when an incoming message refers to the past, the draft finds the earlier message in the
sender's enabled work chats (any depth of Telegram history), cites it, and the card lists it;
failures and "no match" are visible; nothing else is searched or stored.

**Independent Test**: quickstart §1 integration tests and §3 steps 1–4; §4 group scoping.

### Tests for User Story 1

- [X] T014 [P] [US1] `tests/unit/test_earlier_link_rules.py` for `link_reason` (S5): private-chat match → `private`; group match by the user → `own`, by the sender → `sender`; reply to a sender/user message (target from `reply_targets`) → `reply`; a match replied to by a sender/user match → `replied_by`; mention by peer id or by `@username` (case-insensitive) of sender or user → `mention`; same `thread_top_id` as `incoming_thread_top_id` → `thread`; unrelated third-party group message → `None`
- [X] T015 [P] [US1] `tests/unit/test_earlier_scope.py` for `EarlierSearch.chats_for` / `search` with `FakeMessagingGateway` (S4, S6): scope = turn chat + the sender's private chat if enabled + ≤ 2 enabled groups where the sender has stored messages within retention, most recent first; a disabled chat is never passed to `search_chat` (FR-S01); ≤ 5 queries × ≤ 4 chats requests; matches already in `known` dropped; duplicates across queries merged; empty text dropped; capped at 60 newest first; refs `earlier:1..N`; `GatewayBusy` → outcome `rate_limited`, `GatewayNotStarted` / `SessionRevoked` → `not_started`, other errors → `failed`, deadline exceeded → `timeout` (patch the timeout to 0.05 s and make the fake slow); `search` never raises; an edited message in the fake index is matched by its new text and a removed one is never returned; `@username` mentions are linked only when `get_usernames` knows the username (unknown → only id-based mentions count)
- [X] T016 [P] [US1] Integration tests `tests/integration/test_earlier_search_drafts.py` (fake LLM adapter scripted for the filter, rank and draft calls; `earlier_support.py`): (a) agreement outside the recent window → draft cites `earlier:*`, card source label `💬 <chat> · <date>`, Sources view shows excerpt, `output_json.earlier_messages` holds only cited entries with encrypted content; (b) "ok, thanks" with `needs_earlier_context=false` → zero `search_calls`, zero rank calls, no warning; (c) search raises `GatewayBusy` → draft from recent window + `earlier_search_unavailable`; (d) no matches → `earlier_search_no_match`, draft keeps placeholder / `needs_info`; (e) rank call raises `ProviderError` → newest 10 linked matches used + `filter_unavailable`; (f) group: reply-linked third-party message cited, unrelated message never in any LLM request; (g) a new chat with no stored history still runs the filter call and the search; (h) 🔁 Regenerate searches again; (i) model cites an `earlier:*` id that was not offered → regenerate-then-strip path as for `msg:*`; (j) ranking slower than the remaining budget (search took 6 s, ranking blocked) → finishes within 10 s using the newest 10 linked matches + `earlier_search_unavailable`; (k) the first filter call raises `ProviderError` with search enabled → no `search_calls`, warnings `filter_unavailable` and `earlier_search_unavailable`
- [X] T017 [P] [US1] Logging test in `tests/unit/test_logging_redaction.py` (or new `tests/integration/test_earlier_search_logs.py`): exactly one `earlier_search` event per draft with `user_id`, `turn_id`, `decision`, `outcome`, `chats`, `queries`, `requests`, `matches`, `dropped_unlinked`, `used`, `duration_ms`, `rank_ms`, `total_ms`; the captured log output contains none of the query strings or message texts (FR-S05a, FR-S17)
- [X] T018 [P] [US1] Card tests in `tests/unit/test_draft_card.py`: `💬` labels render chat title and date in the user's time zone; warnings `earlier_search_unavailable` / `earlier_search_no_match` render the EN and RU texts from contracts/control-chat.md; a draft without `earlier_messages` but citing `earlier:*` falls back to `💬 earlier message` / `💬 сообщение из прошлой переписки`

### Implementation for User Story 1

- [X] T019 [US1] Create `src/scribe/context/earlier.py` per contracts/internal-interfaces.md "EarlierSearch": constants `MAX_QUERIES = 5`, `MAX_EXTRA_GROUPS = 2`, `PER_REQUEST_LIMIT = 20`, `MAX_MATCHES = 60`, `SNIPPET_CHARS = 500`; dataclasses `SearchScope` (usernames optional, filled via `get_usernames`), `EarlierMatch` (with `link_reason` in `private`/`own`/`sender`/`reply`/`replied_by`/`mention`/`thread`), `EarlierSearchResult` (`outcome` in `ok`/`no_match`/`failed`/`rate_limited`/`timeout`/`not_started`, `matches`, `stats`); pure `link_reason(...)`; `EarlierMatch.history_item(timezone)` (text truncated to `SNIPPET_CHARS`, `is_user` from `is_outgoing`, sender `me` for the user); `MAX_CHATS = 4`; `EarlierSearch(gateway, settings, *, clock)` with `chats_for(session, user_id, turn_chat, sender_contact_id, sender_peer_id) -> list[SearchChat]` (only `work_chats.enabled = true`; the `SearchScope` itself is built by the composer, T021), `search(scope, chats, queries)` (≤ 4 in flight via `asyncio.Semaphore`, whole call in `asyncio.timeout(settings.earlier_search_timeout_s)`, one batched `get_messages` per group chat for reply targets, link rules, de-dup vs `scope.known`, cap, refs) (makes T014, T015 pass; depends on T006, T007, T013)
- [X] T020 [US1] Integrate into `src/scribe/drafting/composer.py` per contracts/internal-interfaces.md "Composer": `ComposeInput.search_scope`, `search_chats`, `search_enabled`; `Composition.rank_result`, `earlier`, `cited_earlier`; `compose_output(router, inp, *, earlier_search=None)`: run the filter call even with no candidates when `search_scope` is set and `search_enabled`; on `needs_earlier_context` run `EarlierSearch.search` with `filter_result.output.search_queries`, then `router.rank_earlier`, append ranked matches to `DraftRequest.history` (after the selected recent history, oldest first); ranking timeout = `10 s − search time` (≥ 2 s), on timeout newest 10 linked matches + `earlier_search_unavailable`; first filter call failing with search enabled → no search, `filter_unavailable` + `earlier_search_unavailable`, log `decision="skipped"`; warnings `earlier_search_unavailable` / `earlier_search_no_match` / `filter_unavailable` per research S8; `invalid_source_ids` accepts offered `earlier:*` ids as `message` sources; `cited_earlier` = matches whose ref is in `output.sources`; `usage_json` adds `"rank"`; one `earlier_search` log event (S10) (makes T016 a–g, i–k pass; depends on T009, T019)
- [X] T021 [US1] In `Composer` (`src/scribe/drafting/composer.py`): constructor arg `earlier_search: EarlierSearch | None = None`; `_load` builds `SearchScope` (turn chat, sender peer id, user peer id = `users.telegram_user_id`, usernames left `None` for `search` to fill, incoming thread top id, `candidates.telegram_ids`) and `search_chats` inside the session, and `search_scope=None` when `settings.earlier_search_enabled` is false or no `earlier_search` was given; `_persist` passes `cited_earlier` to `pack_output(..., earlier_messages=...)` with `excerpt` cut to 200 chars around the first query hit (depends on T012, T020)
- [X] T022 [US1] Wiring: build one `EarlierSearch(self.pool, settings, clock=self._clock)` in `src/scribe/__main__.py` and pass it to the service's `Composer`; in `src/scribe/telegram/control_bot/review.py` the 🔁 Regenerate handler builds `Composer(..., earlier_search=EarlierSearch(gateway, settings, clock=clock))` (makes T016 h pass; depends on T021)
- [X] T023 [P] [US1] Card and Sources view: in `src/scribe/telegram/control_bot/review.py` `_ref_label` / `source_labels` resolve `earlier:*` ids from `StoredOutput.earlier_messages` → `💬 <chat title> · <d MMM yyyy>` (detailed: `💬 <chat> · <sender> · <d MMM yyyy HH:mm> — <used_for>`, the excerpt in «», and `https://t.me/c/<internal id>/<message id>` for supergroups, where internal id = `-chat_id - 10**12`); fallback label when the entry is missing; warnings added to the known set in `src/scribe/telegram/control_bot/render.py` (makes T018 pass; depends on T012)
- [X] T024 [P] [US1] Texts in `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py` exactly as contracts/control-chat.md: `card.warning.earlier_search_unavailable`, `card.warning.earlier_search_no_match`, `card.source_earlier`, `review.source_earlier`, `review.source_earlier_missing`; `tests/unit/test_i18n_lint.py` passes (both languages, same placeholders)
- [X] T025 [US1] Eval suite (Principle V, S11): `evals/threads/earlier_context.jsonl` with ≥ 40 synthetic threads (≥ 15 Russian, ≥ 15 English; ≥ 10 that need no earlier context; ≥ 5 with no match; ≥ 5 group threads with an unrelated decoy; ≥ 3 injection threads asking to search all chats or reveal other chats); each thread has recent history, incoming messages, a canned `search_index`, and expected `needs_earlier_context`, expected cited message, and forbidden facts; extend `evals/run.py` with `--suite earlier_context` (runs `compose_output` with `EarlierSearch` over a `FakeMessagingGateway`) and metrics `decision_recall`, `decision_precision`, `earlier_citation_rate`, `invented_reference_rate`, `scope_violations`; thresholds in `evals/thresholds.yml`: recall ≥ 0.9, precision ≥ 0.8, citation ≥ 0.8, invented = 0, scope violations = 0; unit test in `tests/unit/test_eval_runner.py` for the new suite loader and metrics (depends on T020)

**Checkpoint**: US1 complete — quickstart §1 (US1 parts), §2 and §3 steps 1–4 pass.

---

## Phase 4: User Story 2 - The user controls what is searched (Priority: P2)

**Goal**: `/settings` toggle for automatic search (default on); when off, no search runs and the
card says so when earlier context would have been needed; every earlier message used is listed
on the card (done in US1).

**Independent Test**: turn the setting off, regenerate a draft that needs an earlier message:
no `search_calls`, card shows `earlier_search_off` (quickstart §3 step 5).

### Tests for User Story 2

- [X] T026 [P] [US2] Integration test `tests/integration/test_settings_search_toggle.py` (pattern of `tests/integration/test_us2_settings.py`): `/settings` shows `🔎 Search earlier conversations: on`; the toggle button flips `users.search_earlier_messages` and re-renders; callback `format_callback("settings", "search_earlier")` flips the value and logs `settings_changed` with `fields=["search_earlier_messages"]`; RU texts per contracts/control-chat.md
- [X] T027 [P] [US2] Add to `tests/integration/test_earlier_search_drafts.py`: with `search_earlier_messages=false` and `needs_earlier_context=true` → zero `search_calls`, zero rank calls, warning `earlier_search_off`, log `decision="off"`; with the setting off and `needs_earlier_context=false` → no warning; operator switch `earlier_search_enabled=false` → no search and no warning

### Implementation for User Story 2

- [X] T028 [US2] Composer: `_load` sets `ComposeInput.search_enabled = user.search_earlier_messages`; `compose_output` adds `earlier_search_off` when the filter returns `needs_earlier_context=true` and `search_enabled` is false, and logs `decision="off"` (makes T027 pass) in `src/scribe/drafting/composer.py`
- [X] T029 [US2] Settings toggle in `src/scribe/telegram/control_bot/settings.py`: row `settings.search_earlier` (on/off) and button `btn.search_earlier_off` / `btn.search_earlier_on` with callback `format_callback("settings", "search_earlier")` (each press flips the value), stored in `users.search_earlier_messages`, `settings_changed` log; texts in `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py` (`settings.search_earlier`, `btn.search_earlier_on`, `btn.search_earlier_off`, `card.warning.earlier_search_off`) exactly as contracts/control-chat.md (makes T026 pass)

**Checkpoint**: US1 + US2 complete; quickstart §3 step 5 passes.

---

## Phase 5: Polish & Cross-Cutting

- [X] T030 [P] Run `uv run python evals/run.py --suite drafts` and `--suite jira` with filter prompt v2; record results in the PR; if an agreed metric regresses, fix the prompt or document the justification (Principle V)
- [X] T031 [P] `/my_data` export and `/delete_my_data`: verify (test in `tests/integration/test_us4_account.py`) that cited `earlier_messages` appear decrypted inside the exported draft and are gone after deletion
- [X] T032 [P] Docs: README section "Search over earlier conversations" (what is searched, the toggle, the operator switch, Telegram rate limits); `.env.example` already updated in T002; add `FoundMessage` / `search_chat` / `get_messages` to `specs/001-telegram-reply-agent/contracts/internal-interfaces.md` "MessagingGateway" with a pointer to spec 004
- [X] T033 Security pass `specs/004-conversation-search/security-review.md` (pattern of spec 003): FR-S01 scope enforced in code (no chat id from model output), no `searchGlobal`, injection eval results, data stored and purged, log redaction test
- [ ] T034 Full checks: `uv run pytest -q`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy src`; then quickstart §3–§4 manually on a test deployment (after a backup), watching `earlier_search` outcomes (share of `rate_limited`) and SC-003: the 95th percentile of `total_ms` over drafts with `decision="searched"` must be ≤ 5000 ms

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 → US1 → US2 → Polish.
- Phase 2: T005 → T006, T007 (T004 test written first); T008 → T009 → T010; T011, T012, T013
  independent.
- US1: tests T014–T018 first (parallel) → T019 → T020 → T021 → T022; T023, T024 after T012
  (parallel with T019–T022); T025 after T020.
- US2: tests T026, T027 → T028 (needs T020/T021) and T029 (independent of the composer).
- Polish after US2.

## Parallel Opportunities

- Setup: T002, T003.
- Foundational: T004, T007, T008, T011, T012, T013 (different files).
- US1: tests T014–T018; T023 and T024 alongside T019–T022.
- US2: T026, T027; T029 alongside T028.
- Polish: T030, T031, T032.

### Parallel example: User Story 1

```text
Task: "T014 link rule unit tests in tests/unit/test_earlier_link_rules.py"
Task: "T015 scope/search unit tests in tests/unit/test_earlier_scope.py"
Task: "T016 draft integration tests in tests/integration/test_earlier_search_drafts.py"
Task: "T018 card tests in tests/unit/test_draft_card.py"
# then
Task: "T019 EarlierSearch in src/scribe/context/earlier.py"
Task: "T023 Sources view in src/scribe/telegram/control_bot/review.py"
Task: "T024 texts in src/scribe/i18n/en.py and ru.py"
```

## Implementation Strategy

1. **MVP = Phases 1–3**: drafts find, rank and cite earlier messages with visible fallbacks;
   ship behind `SCRIBE_EARLIER_SEARCH_ENABLED` once the `earlier_context` eval suite meets its
   thresholds and the `drafts` / `jira` suites do not regress.
2. **US2**: user toggle and the "turned off" card note.
3. **Polish**: docs, security review, full test/eval run, check of rate limits and latency on a test server.

## Evaluation results (T030, 2026-10-22)

Real-provider runs on a self-hosted model behind the `openai_compat` provider (one model on
every route):

- `earlier_context` (40 threads): decision recall 1.000, precision 1.000, earlier citation
  0.842, invented references 0, scope violations 0 — all thresholds hold.
- `jira` (47 cases × 2 flavors): all metrics 1.000, all thresholds hold.
- `drafts` (57 threads), three runs per branch, filter prompt v1 (`main`) vs v2 (this branch):

  | metric | main avg | 004 avg | threshold |
  |---|---|---|---|
  | groundedness | 0.860 | 0.842 | ≥ 0.95 |
  | unsupported_facts | 1.3 | 2.0 | 0 |
  | flag_recall | 0.912 | 0.912 | ≥ 0.9 |
  | injection_resisted | 0.967 | 0.933 | 1.0 |

  Both branches miss the `drafts` thresholds on this model (the thresholds were set for the
  Claude routes); the differences between branches are within run-to-run variance (the same
  injection thread fails on `main` too). Justification for shipping (Principle V): no
  systematic regression from prompt v2; the `drafts` gap on that model predates this
  feature and needs its own follow-up. A run on the Claude routes is still pending (no
  Anthropic key in this environment).
