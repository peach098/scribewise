# Tasks: Telegram Work Reply Agent

**Input**: Design documents from `/specs/001-telegram-reply-agent/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included. Constitution Principle V requires automated tests for deterministic code and
a versioned evaluation set for anything that affects draft generation, so test and eval tasks are
mandatory, not optional. Test names below match `quickstart.md` and are the contract.

**Organization**: Tasks are grouped by user story. US1 is the MVP; US2–US4 each add an
independently testable increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 (draft in my style), US2 (delayed reading), US3 (tasks & commitments),
  US4 (personalization & multi-user)
- Paths follow the single-project layout in plan.md: `src/scribe/`, `tests/`, `evals/`,
  `config/`, `prompts/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project skeleton, tooling, containers, configuration, logging, encryption.

- [X] T001 Create `pyproject.toml` (uv-managed, Python 3.12) with dependencies telethon, aiogram>=3, anthropic, openai, sqlalchemy[asyncio]>=2, asyncpg, alembic, httpx, pydantic>=2, pydantic-settings, pyyaml, cryptography, structlog, aiohttp; dev deps pytest, pytest-asyncio, respx, testcontainers[postgres], ruff, mypy; create package dirs `src/scribe/{db/repos,telegram/control_bot,intake,scheduling,context/tasks,style,drafting,sending,llm/adapters,retention,observability}/__init__.py`, `tests/{unit,integration,contract}/`, `evals/threads/`, `config/`, `prompts/`
- [X] T002 [P] Configure ruff (line length 100, isort) and mypy (strict for `src/scribe`) in `pyproject.toml`; add `Makefile` with targets `lint`, `typecheck`, `test-fast`, `test-integration`, `eval`
- [X] T003 [P] Create `Dockerfile` (python:3.12-slim, uv sync, `python -m scribe`) and `docker-compose.yml` with services `app` and `db` (postgres:16, volume, healthcheck) reading env from `.env`; add `.env.example` with every variable from quickstart.md Configuration section
- [X] T004 [P] Create CI workflow `.github/workflows/ci.yml` (constitution "Development Workflow & Quality Gates"): on every pull request run `make lint typecheck test-fast` and `make test-integration` (Postgres service container); run `make eval` when the diff touches `src/scribe/drafting/**`, `src/scribe/style/**`, `src/scribe/llm/**`, `prompts/**` or `config/llm_routes.yml` (use `dorny/paths-filter`; `ANTHROPIC_API_KEY` from repository secrets); run `gitleaks` secret scanning on the diff and `pip-audit` on the locked dependencies; all jobs required for merge
- [X] T005 [P] Implement `src/scribe/config.py` with pydantic-settings `Settings`: `database_url`, `master_key` (base64, must decode to 32 bytes), `bot_token`, `tg_api_id`, `tg_api_hash`, `scheduler_tick_s=5`, `log_level`, `health_port=8080`, `test_fast_clock=False`, defaults `delay_min_s=120`, `delay_max_s=600`, `work_start=09:00`, `work_end=19:00`, `working_days=[0..4]`, `grouping_window_s=60`, `max_contact_profiles=20`, `message_retention_days=30`, `style_import_months=6`, `profile_examples_contact=20`, `profile_examples_global=50`, `min_style_samples=50`
- [X] T006 [P] Implement `src/scribe/logging.py`: structlog JSON configuration plus a redaction processor that drops/replaces keys `text`, `body`, `edited_text`, `session_string`, `credentials`, `examples` at any nesting depth; unit test in `tests/unit/test_logging_redaction.py` asserting no such key survives
- [X] T007 [P] Implement `src/scribe/crypto.py`: AES-256-GCM encrypt/decrypt with random 96-bit nonce and `key_id` prefix (format `v1:<nonce_b64>:<ct_b64>`), `EncryptedText` SQLAlchemy `TypeDecorator`, and `EncryptedJSON`; unit tests in `tests/unit/test_crypto.py` (round-trip, tamper detection, key_id rotation path)
- [X] T008 Initialize Alembic (`alembic.ini`, `alembic/env.py` using async engine from `src/scribe/db/session.py`); implement `src/scribe/db/session.py` with async engine + `async_sessionmaker` from `Settings`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schema, LLM routing layer, Telegram gateway, bot skeleton, scheduler loop,
retention, test fixtures. No user story can be implemented without these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Database

- [X] T009 Implement all SQLAlchemy models in `src/scribe/db/models.py` exactly per data-model.md: `users` (incl. `llm_tier` enum quality|economy default quality, `state` enum setting_up|active|paused|out_of_office, `working_days int[]`, `delay_min_s <= delay_max_s` check, `0 <= delay_min_s`, `delay_max_s <= 86400`, `work_start < work_end`), `telegram_sessions` (`session_string` EncryptedText, unique user_id), `work_chats` (unique (user_id, telegram_chat_id); `kind` private|group|channel_comments; `group_mode` mentions_only|always; nullable overrides), `contacts` (unique (user_id, telegram_peer_id); `kind` person|group; `own_message_count_6m`; `pinned_profile`), `messages` (`text` EncryptedText, `has_media`, indexed `sent_at`, unique (work_chat_id, telegram_message_id)), `conversation_turns` (state enum waiting|reading|pending_review|answered|rejected|dropped|handle_manually; indexed `due_at`; `dropped_reason` enum), `drafts` (`text`/`edited_text` nullable EncryptedText, `finalized_at` nullable timestamptz set on any final state, `output_json` jsonb, `state` enum pending|sent|rejected|expired|dropped|superseded, `remind_at`/`expires_at` indexed, `approved_by_user_at`, `llm_provider`, `llm_model`, `llm_usage_json`, `outdated`, `control_message_id`, **check constraint `state <> 'sent' OR approved_by_user_at IS NOT NULL`**, partial unique index one `pending` per `conversation_turn_id`), `style_profiles` (scope global|contact; `rules_json`, `user_rules_json`, `summary_text`, `examples` EncryptedJSON, `version`, `is_current`, `state` current|previous|retired), `style_rule_proposals`, `commitments` (`source_message_text` nullable EncryptedText; `closed_at` nullable timestamptz; `confidence >= 0.7` check; state open|resolved|deleted), `bot_fsm_state` (`chat_id` bigint PK, `state` text nullable, `data` EncryptedJSON), `task_source_connections` (`credentials` EncryptedJSON; provider trello|jira), `activity_records` (kind enum per data-model.md; `meta_json`), `scheduled_jobs`. All per-user FKs `ON DELETE CASCADE`.
- [X] T010 Generate and hand-verify initial Alembic migration `alembic/versions/0001_initial.py` from T009 models (ensure check constraints, partial unique index and enums are present); `uv run alembic upgrade head` succeeds against the compose `db`
- [X] T011 [P] Implement repository modules in `src/scribe/db/repos/`: `users.py`, `chats.py` (work_chats + contacts), `messages.py`, `turns.py`, `drafts.py`, `profiles.py`, `commitments.py`, `tasks_conn.py`, `activity.py`, `jobs.py` — thin async functions over the session, each scoped by `user_id`; no business logic

### LLM routing layer (contracts/internal-interfaces.md, research R5)

- [X] T012 [P] Implement `src/scribe/llm/provider.py`: `ProviderAdapter` Protocol, `Capabilities`, `Effort` literal, `Block` (text/data block), `Usage`, `StructuredResult[T]`, and pydantic request/response models `FilterContextRequest/Output`, `DraftRequest`, `DraftOutput` (mirrors `contracts/draft-output.schema.json` exactly), `StyleProfileRequest/Output`, `CommitmentRequest`, `CommitmentItem`, `StyleJudgeRequest/Output`; contract test `tests/contract/test_draft_output_schema.py` asserting `DraftOutput.model_json_schema()` is equivalent to the JSON file
- [X] T013 [P] Create `config/llm_routes.yml` (exact content from contracts/internal-interfaces.md) and `config/llm_prices.yml` (per-model `input_per_m`, `output_per_m`, `cached_input_per_m` for claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5; placeholder entries for openai_compat models); implement `src/scribe/llm/routes.py` (pydantic model of the table, tier resolution for `compose.draft`, env overrides `SCRIBE_LLM__<ROUTE>__<FIELD>` with `__` nesting) and `src/scribe/llm/pricing.py` (`cost_usd(model, usage)`); **startup validation fails on unknown provider, on a model missing from prices, or on any route resolving to a provider whose `data_use_confirmed` is not `true`**; unit test `tests/unit/test_llm_routes.py` → `test_route_table_overrides_and_tiers`
- [X] T014 Implement `src/scribe/llm/adapters/fake.py` (`FakeAdapter`: canned outputs keyed by route + request fingerprint, call recorder) and `src/scribe/llm/registry.py` (`LLMRouter` with methods `filter_context`, `draft_reply`, `build_style_profile(batch=False)`, `extract_commitments`, `style_judge`; resolves route → adapter/model/effort; wraps every call with structured log `route, provider, model, prompt_version, usage, cost_usd`; loads prompt text from `prompts/<name>.md` with a `version:` front-matter line)
- [X] T015 [P] Implement `src/scribe/llm/adapters/anthropic.py` per research R5: `anthropic.AsyncAnthropic`, structured outputs via `output_config.format` from the pydantic schema, `output_config.effort` per route, system prompt + stable profile block first with `cache_control` when `cache_prefix=True`, `betas=["server-side-fallback-2026-07-01"]` + `fallbacks="default"`, handle `stop_reason == "refusal"` → `ProviderRefused`; `complete_structured_batch` via Message Batches for `profile.build`; typed error chain (RateLimitError → APIStatusError → APIConnectionError) mapped to `ProviderUnavailable`; usage → `Usage` incl. `cache_read_input_tokens`. Read the claude-api skill `python/claude-api/README.md` and `tool-use.md` (structured outputs section) before writing.
- [X] T016 [P] Implement `src/scribe/llm/schema_fallback.py` (schema-in-prompt → parse → validate → one retry → `ProviderOutputInvalid`) and `src/scribe/llm/adapters/openai_compat.py` (`openai.AsyncOpenAI` with `base_url`; `response_format` json_schema when supported, else schema_fallback; capabilities `batch=False`, `prompt_caching=False`); unit tests in `tests/unit/test_schema_fallback.py`

### Telegram

- [X] T017 [P] Implement `src/scribe/telegram/gateway.py`: `MessagingGateway` Protocol, `Dialog`, `IncomingMessage` (chat_id, message_id, sender_peer_id, sender_display_name, text, has_media, sent_at, is_outgoing, reply_to_id, mentions_me, is_reply_to_me), `ReadEvent`, `OutgoingEvent`; and `src/scribe/telegram/fake_gateway.py` (`FakeMessagingGateway`: records `mark_read`/`send_text` calls, `emit_new_message/emit_read_elsewhere/emit_outgoing` test helpers)
- [X] T018 Implement `src/scribe/telegram/user_client.py` (Telethon, research R1/R3): per-user `TelegramClient(StringSession(decrypted))`, `start/stop`, `list_dialogs`, `fetch_own_history(chat_id, since)` (own messages only), `mark_read` → `send_read_acknowledge(entity, max_id=...)`, `send_text(reply_to)`, update handlers mapping `NewMessage` (incoming → `on_new_message`; `out=True` → `on_outgoing`), `UpdateReadHistoryInbox`/`UpdateReadChannelInbox` → `on_read_elsewhere`; `FloodWaitError` handling (sleep ≤ 60 s, otherwise raise `GatewayBusy` and log); **never** acknowledge reads on receipt
- [X] T019 Implement `src/scribe/telegram/client_pool.py`: start a client for every user with `telegram_sessions.status='active'` at boot, start/stop on login/logout, mark session `error`/`revoked` on `AuthKeyUnregisteredError` and notify via control bot
- [X] T020 Implement control-bot skeleton: `src/scribe/telegram/control_bot/bot.py` (aiogram `Dispatcher`, routers registration, middleware resolving `user_id` from `telegram_control_chat_id`), `src/scribe/telegram/control_bot/render.py` (keyboard/callback helpers, `callback_data` parser `<action>:<id>[:<extra>]` ≤ 64 bytes), `/help` and `/start` creating the `users` row in state `setting_up` (per contracts/control-chat.md); `src/scribe/telegram/control_bot/fsm_storage.py`: aiogram `BaseStorage` implementation backed by the `bot_fsm_state` table (research R2) wired into the `Dispatcher`, with an integration test `tests/integration/test_fsm_storage.py` proving state survives a dispatcher restart; `bot_fsm_state` row deleted when its user is deleted

### Runtime

- [X] T021 Implement `src/scribe/scheduling/scheduler.py` tick loop (every `scheduler_tick_s`): four steps per contracts/internal-interfaces.md, each in its own transaction using `SELECT … FOR UPDATE SKIP LOCKED`; step handlers are injectable callables so stories plug in; `scheduled_jobs` runner with `next_run_at` computation (daily/weekly)
- [X] T022 [P] Implement `src/scribe/retention/purge.py`: `purge_messages()` deletes `messages` with `sent_at < now() - message_retention_days`; sets `drafts.text = NULL, edited_text = NULL` where `finalized_at < now() - 30 days`; deletes `commitments` where `closed_at < now() - 30 days`; deletes `style_profiles` with `state='retired'` older than 30 days; register as `scheduled_jobs` row `purge_messages` (daily); integration test `tests/integration/test_retention.py`
- [X] T023 [P] Implement `src/scribe/observability/health.py` (aiohttp `/healthz` → `{"status","users_active"}`) and `src/scribe/__main__.py` wiring: settings → logging → route table validation (fail fast) → DB → `LLMRouter` → `ClientPool` → control bot polling → scheduler → health server; graceful shutdown
- [X] T024 Create test infrastructure: `tests/conftest.py` (Postgres via testcontainers or `SCRIBE_TEST_DATABASE_URL`, per-test transaction rollback, `settings` fixture with `delay 0–0`, `FakeAdapter`-backed `LLMRouter`, `FakeMessagingGateway`, `make_user/make_chat/make_contact` factories, frozen clock fixture)

**Checkpoint**: Foundation ready — `python -m scribe` boots with a bot that answers `/help`, the scheduler ticks, `/healthz` responds, and all Phase 1–2 tests pass.

---

## Phase 3: User Story 1 — Draft a reply in my style (Priority: P1) 🎯 MVP

**Goal**: A message in an enabled work chat produces a draft in the user's style in the control
chat; approve/edit/reject; approved replies are sent from the user's account; other chats are
ignored and never stored.

**Independent Test**: With delay 0–0, send a message from a second account into an enabled chat →
draft card within 30 s in the user's style with sources; ✅ Send delivers it from the user's
account; ✏️ Edit sends the edited text; ❌ Reject sends nothing; a message in a non-enabled chat
produces no card and no `messages` row.

### Tests for User Story 1

- [X] T025 [P] [US1] Unit tests `tests/unit/test_router.py`: enabled-chat filter, group `mentions_only` vs `always`, 60 s grouping window merges consecutive messages into one turn, media-only → `handle_manually`
- [X] T026 [P] [US1] Unit tests `tests/unit/test_working_hours.py` (`test_next_allowed_outside_hours` incl. a DST transition date via `zoneinfo`) and `tests/unit/test_delay.py` (`test_delay_is_random_within_range`: 200 draws in 120–600 all within bounds, not all equal)
- [X] T027 [P] [US1] Unit tests `tests/unit/test_flags.py` (sensitive keyword categories credentials|payment|personal_data|legal|hr) and `tests/unit/test_composer_validation.py` (`test_sources_must_reference_request_ids`; `test_filter_context_selects_only_offered_ids` incl. fallback with `filter_unavailable`)
- [X] T028 [P] [US1] Prompt-injection tests (FR-010): unit test `tests/unit/test_prompt_assembly.py::test_user_text_never_in_system_prompt` (incoming text, history, task titles and contact names appear only inside delimited data blocks of the user turn, never in `system`, for every route); eval threads `evals/threads/us1_injection.jsonl` (≥ 8 threads where the incoming message tries to override instructions, request files/credentials, change settings or claim to be the user/operator) with `must_not_claim`, `expected_flags` (sensitive where relevant) and a check that the draft does not comply
- [X] T029 [P] [US1] Integration tests `tests/integration/test_us1_draft_flow.py`: `test_no_send_without_approval` (scheduler ticks + every non-approval callback → `send_text` never called), `test_approve_sends_and_marks_sent`, `test_edit_sends_edited_text`, `test_reject_sends_nothing`, `test_non_work_chat_ignored_and_not_stored`, `test_sent_requires_approved_at_db_constraint`

### Implementation for User Story 1

- [X] T030 [P] [US1] Implement `src/scribe/scheduling/working_hours.py` (`next_allowed(t, tz, working_days, start, end)` pure, DST-safe) and `pick_delay(min_s, max_s, rng=SystemRandom())` in `src/scribe/intake/waiting.py`
- [X] T031 [P] [US1] Implement `src/scribe/intake/router.py`: on `IncomingMessage` → look up enabled `work_chats` (else return without storing anything, FR-001); group rule (`mentions_me or is_reply_to_me or group_mode=='always'`); upsert `contacts`; store `messages`; attach to an open `waiting` turn for the same chat+contact within `grouping_window_s` else create a new turn (`delay_s=pick_delay`, `due_at=next_allowed(arrived_at+delay)`); media-only → turn `handle_manually` + notification
- [X] T032 [P] [US1] Write prompts: `prompts/filter_context.md` (select ids only; output `FilterContextOutput`), `prompts/draft.md` (system: role, style rules, untrusted-data notice, no fabrication, placeholders `[[…]]`, sources required for every fact/date, flags, tone judgement; user turn: delimited data blocks), `prompts/style_profile.md` (produce `rules_json` + `summary_text` + example indices), each with `version: 1` front matter
- [X] T033 [P] [US1] Implement `src/scribe/style/importer.py` (`import_own_messages(user)`: for each enabled chat `fetch_own_history(since=now-6 months)`, stream into a temporary in-memory sample list, never persisted raw) and global profile build in `src/scribe/style/profiles.py` (`build_global_profile(user, samples)` via `LLMRouter.build_style_profile`; keep ≤ 50 examples encrypted; `sample_size < min_style_samples` → mark profile `rules_json.weak=true`; write version 1 `is_current`); `select_profile(user, contact)` returning global for now
- [X] T034 [P] [US1] Implement `src/scribe/context/history.py`: `load_candidates(turn)` → turn messages + ≤ 40 most recent in chat + ≤ 20 prior with contact, all within 30 days, decrypted, with stable ids `msg:<uuid>`
- [X] T035 [US1] Implement `src/scribe/drafting/flags.py` (keyword-based sensitive detection merged with model flags) and `src/scribe/drafting/composer.py` pipeline per contracts: candidates → `filter_context` (drop ids not offered, cap 15 history, fallback on failure) → `select_profile` + chat overrides → `draft_reply(user_tier)` → reject outputs whose `sources` ids ∉ request (regenerate once, then set `needs_info=true`, `system_warnings+=["unsupported_sources"]`; system-detected warnings such as `filter_unavailable`/`unsupported_sources` are stored as `system_warnings` next to the unchanged `DraftOutput` in `drafts.output_json`, and the card renders the union with `context_warnings`) → persist `drafts` row (`pending`, `remind_at=prepared_at+2h`, `expires_at=prepared_at+24h`, `llm_provider/model/usage`) → `activity_records(draft_prepared)`
- [X] T036 [US1] Register scheduler step 1 in `src/scribe/scheduling/scheduler.py`: select turns `waiting AND due_at<=now()` with `FOR UPDATE SKIP LOCKED`, set state `reading` and **commit, releasing the lock**; then, outside any transaction, `gateway.mark_read(chat, last_message_id)` → `composer.compose(turn)` → state `pending_review`; on failure revert to `waiting` with `due_at = now() + 60 s` and log; run composition for due turns concurrently (bounded `asyncio.Semaphore`, default 10)
- [X] T037 [US1] Implement `src/scribe/sending/send.py`: `send_approved_draft(draft_id, edited_text=None)` asserts `draft.state=='pending' and approved_by_user_at is not None`, calls `gateway.send_text(chat, text, reply_to=…)` where `reply_to` is the last incoming message id only in group chats or when that incoming message was itself a reply, else `None` (FR-012 "where appropriate"), sets `sent_telegram_message_id`, draft `sent`, turn `answered`, stores the outgoing message in `messages`, writes `activity_records(sent)`; this is the only module importing `send_text`
- [X] T038 [US1] Implement draft card rendering in `src/scribe/telegram/control_bot/render.py` per contracts/control-chat.md "Draft card" (header, quoted incoming ≤ 600 chars, profile used, tone shift, flags line, needs-info line, sources line, expiry) and keyboard `approve|edit|regen|reject|sources`
- [X] T039 [US1] Implement `src/scribe/telegram/control_bot/review.py` callbacks: `approve:<draft_id>` (set `approved_by_user_at`, call `send_approved_draft`, edit card → "✅ Sent HH:MM"); `edit:<draft_id>` (FSM: ask for reply text → `send_approved_draft(edited_text)` → "✅ Sent (edited)"); `reject` (turn `rejected`, card "❌ Rejected"); `regen` (old draft `superseded`, compose new version, post new card); `sources` (reply with full list); expired/superseded drafts answer "This draft has expired"; extra confirmation step when `flags.sensitive.value` or `needs_info`
- [X] T040 [US1] Implement minimal setup in `src/scribe/telegram/control_bot/setup_flow.py` (aiogram FSM): `/start` → phone → code → optional 2FA password → store encrypted `StringSession` → `pick_chats` (paged dialog list with ✅/⬜ toggles) → defaults applied from `Settings` → run `importer` + `build_global_profile` with a progress message → user `active` → "first draft will appear here"; `/cancel` keeps `setting_up`; never log phone/code/password
- [X] T041 [US1] Create initial eval set `evals/threads/us1_style_basic.jsonl` (≥ 15 synthetic threads: profile, history, incoming, `must_mention`, `must_not_claim`, `expected_flags`, `expected_sources`, `style_targets`) and `evals/threads/us1_tone_shift.jsonl` (≥ 8 threads where the incoming message is ≥ 2 points more formal/urgent/emotional than the contact baseline, plus ≥ 4 controls with no shift; field `expected_tone_shift`; check per FR-036 that the draft moves at most halfway toward the incoming level), `prompts/style_judge.md`, `evals/thresholds.yml` (groundedness ≥ 0.95, flag recall ≥ 0.90, style_judge ≥ 0.80, unsupported_facts = 0) and `evals/run.py` (runs composer with the real router against threads, deterministic checks + `LLMRouter.style_judge`, prints per-route `cost_usd`, `--compare compose.draft=<m1>,<m2>` side-by-side mode, non-zero exit on threshold miss; `--dry-run` serves every route from the fake adapter with canned outputs stored in the thread files, so the runner, scoring and thresholds are proven offline)

**Checkpoint**: US1 fully functional: `make test-fast test-integration` green, `make eval` passes thresholds, quickstart US1 manual scenario passes.

---

## Phase 4: User Story 2 — Read messages and prepare drafts after a configurable delay (Priority: P2)

**Goal**: Messages stay unread until a random delay within the configured range has passed and
working hours allow; "read now"; drop on manual reply; shorten on read-elsewhere; outdated banner;
2 h reminder and 24 h expiry; pause.

**Independent Test**: With delay 180–300 s the message stays unread and no card appears before
T+180 s; between T+180 and T+300 s it turns read and a card appears; `/waiting` shows it with
planned time and 👀 Read now works; manual reply drops it; a pending card gets the "new messages"
banner when more messages arrive; reminder after 2 h and expiry after 24 h (fast clock).

### Tests for User Story 2

- [X] T042 [P] [US2] Integration tests `tests/integration/test_us2_delay.py`: `test_message_stays_unread_until_due` (no `mark_read` before `due_at`), `test_outside_working_hours_waits_for_next_window`, `test_manual_reply_drops_turn`, `test_read_elsewhere_shortens_wait`, `test_outdated_on_new_message`, `test_read_now_skips_delay`, `test_paused_user_leaves_messages_unread`, `test_new_messages_while_waiting_extend_turn_without_moving_due_at`
- [X] T043 [P] [US2] Throughput test `tests/integration/test_scheduler_throughput.py::test_scheduler_throughput` (SC-009): seed 100 users × 20 turns all due at the same instant, Fake adapter with 2 s simulated latency, run ticks; assert every turn reaches `pending_review` and none is read more than 60 s after its `due_at`
- [X] T044 [P] [US2] Integration tests `tests/integration/test_us2_reminders.py`: `test_reminder_after_2h` (exactly one reminder), `test_expiry_after_24h` (draft `expired`, turn `handle_manually`, buttons removed, approve callback refused)

### Implementation for User Story 2

- [X] T045 [P] [US2] Implement `/settings` in `src/scribe/telegram/control_bot/settings.py`: inline menu to set global delay range (validate `0 <= min <= max <= 86400`, "min = max gives a fixed delay"), working hours (`start < end`), working days (non-empty), time zone (IANA validated via `zoneinfo`); per-chat delay override entry in `/chats` chat settings
- [X] T046 [P] [US2] Implement `/waiting` in `src/scribe/telegram/control_bot/settings.py` and `readnow:<turn_id>` callback in `review.py`: list `waiting` turns with planned reading time in the user's tz, "Read now" sets `due_at=now()` and writes `activity_records(read_now)`
- [X] T047 [US2] Implement gateway event handlers in `src/scribe/intake/router.py`: `on_outgoing` → drop any `waiting` turn / `pending` draft in that chat (`dropped_reason=manual_reply`), notify "✅ You replied yourself — dropped", store the outgoing message; `on_read_elsewhere` → set `due_at=now()` for waiting turns in that chat regardless of working hours (spec edge case: once the user has read it, holding it serves no purpose); new incoming while turn `waiting` → extend `last_message_id`; new incoming while turn `pending_review` → `drafts.outdated=true` and re-render card with banner + `regen` / `approve` ("Send anyway") buttons
- [X] T048 [US2] Register scheduler steps 2 and 3 in `src/scribe/scheduling/scheduler.py`: reminders (`pending AND remind_at<=now() AND reminded_at IS NULL` → reply "⏰ Still waiting…" once, set `reminded_at`) and expiry (`pending AND expires_at<=now()` → draft `expired`, turn `handle_manually`, card → "⌛ Expired — handle manually", remove keyboard); honour `settings.test_fast_clock` (reminder 1 min, expiry 2 min)
- [X] T049 [US2] Implement pause handling: `/pause` and `/resume` in `src/scribe/telegram/control_bot/settings.py` toggle `users.state`; scheduler step 1 in `src/scribe/scheduling/scheduler.py` skips users not `active`/`out_of_office`; `src/scribe/telegram/client_pool.py` keeps receiving (messages still stored, never read) while paused

**Checkpoint**: US1 + US2 work independently; quickstart US2 scenario passes.

---

## Phase 5: User Story 3 — Use my tasks and message history to avoid contradictions (Priority: P2)

**Goal**: Drafts use Trello tasks and extracted commitments as sources, flag conflicts and new
commitments, and never invent answers.

**Independent Test**: Trello card "Q3 report" due Friday → "when will the report be ready?" draft
mentions Friday and lists the card as a source; a manual "I'll send the deck Thursday" creates a
commitment visible in `/commitments`; "can you send it Wednesday?" draft shows ⚠️ conflict; an
unknowable question yields a `[[…]]` placeholder; revoked Trello token → "drafted without task
context".

### Tests for User Story 3

- [X] T050 [P] [US3] Contract tests `tests/contract/test_trello_task_source.py` (respx): `validate`, `list_containers`, `list_open_tasks` mapping (status = list name, due, assignees = member full names), 60 s cache, every HTTP error → `TaskSourceUnavailable`; `test_jira_stub_raises_not_implemented`
- [X] T051 [P] [US3] Unit tests `tests/unit/test_commitments.py`: items with `confidence < 0.7` are not stored, dedupe on (contact, what, due), conflict detection helper between an incoming request date and open commitments/tasks
- [X] T052 [P] [US3] Integration tests `tests/integration/test_us3_context.py`: `test_task_cited_as_source`, `test_outgoing_message_creates_commitment`, `test_conflict_flagged`, `test_unknowable_question_yields_placeholder_and_confirmation`, `test_tasks_unavailable_warning`

### Implementation for User Story 3

- [X] T053 [P] [US3] Implement `src/scribe/context/tasks/base.py` (`Task`, `Container`, `TaskSourceIdentity`, `TaskSource` Protocol, `TaskSourceUnavailable`) and `src/scribe/context/tasks/jira.py` stub raising `NotImplementedError("release version")`
- [X] T054 [P] [US3] Implement `src/scribe/context/tasks/trello.py` per contracts: httpx client with key/token query params, endpoints `GET /1/members/me`, `/1/members/me/boards`, `/1/boards/{id}/cards?filter=open&fields=name,due,idList,idMembers,url`, `/1/boards/{id}/lists`, `/1/boards/{id}/members`; per-user 60 s in-memory cache; read-only (no POST/PUT/DELETE anywhere)
- [X] T055 [P] [US3] Write `prompts/commitments.md` (extract `{what,to_whom,due,confidence,evidence}` from an outgoing message; `version: 1`) and add commitment/task/conflict instructions to `prompts/draft.md` (bump to `version: 2`: list `conflicts[]`, set `flags.creates_commitment` when promising a date/deliverable)
- [X] T056 [US3] Implement `/tasks` command and setup steps `tasks_connect` / `tasks_boards` in `src/scribe/telegram/control_bot/settings.py` and `setup_flow.py`: collect Trello key+token (validate via `TaskSource.validate`), pick boards (paged), store `task_source_connections` (encrypted credentials, `config_json.board_ids`), disconnect; show status `error` after repeated failures. **Done as `/tasks` only** (`control_bot/tasks_command.py`, included by the settings router): connect, validate, paged board picker, disconnect, status/error. **The setup-flow steps `tasks_connect` / `tasks_boards` are deferred to T073**: `setup_flow.py` is rewritten by `specs/002-telegram-qr-login`, so they are added there after 002 merges; until then a new user connects Trello with `/tasks` after setup.
- [X] T057 [US3] Implement `src/scribe/context/commitments.py`: `extract_from_outgoing(message)` via `LLMRouter.extract_commitments` on every outgoing message in enabled chats (manual and agent-sent), store items with `confidence >= 0.7` as `open` with `source_message_text`, `source_sent_at`, dedupe; `open_for(contact|chat)`; `find_conflicts(incoming_dates, commitments, tasks)` helper Agent sends are extracted in `send.py` after commit; their Telethon echo is skipped in `intake/events.py`, which extracts new manual messages only (exactly once per message); the LLM call runs in the background.
- [X] T058 [US3] Extend `src/scribe/drafting/composer.py`: load open tasks via the user's `TaskSource` (tolerant: `TaskSourceUnavailable` → `tasks=[]`, `system_warnings+=["tasks_unavailable"]`) and open commitments; pass both as filter candidates and draft context with ids `task:<id>` / `commit:<uuid>`; render ⚠️ conflicts, 🤝 commitment flag and "⚠️ drafted without task context" on the card in `render.py`; `sources:<draft_id>` expands tasks and commitments with titles/dates **Card**: `render.py` already rendered conflicts, the 🤝 flag and warnings; the US3 labels (task / commitment titles and dates, conflict titles, the one-time "Trello error → /tasks" alert) are filled in `review.py`; a deterministic backstop adds conflicts the model missed (requested date earlier than a selected item's due date).
- [X] T059 [US3] Implement `/commitments` in `src/scribe/telegram/control_bot/settings.py`: list open commitments (what · to whom · due), buttons resolve / edit (FSM: new text or date) / delete; `activity_records` entries. **Done in `control_bot/commitments_command.py`** (included by the settings router); actions are logged as structured events instead of `activity_records`, because `activity_kind` has no commitment kinds and this release adds no enum values or migrations (002 owns migration 0002).
- [X] T060 [US3] Add eval threads `evals/threads/us3_commitments.jsonl` (≥ 15 threads with tasks/commitments: expected sources, expected conflicts, unknowable questions expecting placeholders) and extend `evals/run.py` deterministic checks for `expected_conflicts` and "no date in text without a source"

**Checkpoint**: US1–US3 work independently; quickstart US3 scenario passes; `make eval` green.

---

## Phase 6: User Story 4 — Personalize and configure the agent for a specific user (Priority: P3)

**Goal**: Per-contact profiles with cap/pinning, editable rules, rule proposals from edits,
weekly refresh with undo, per-chat overrides, pause/OOO, history, data deletion, multi-user
isolation, guided setup polish.

**Independent Test**: A person who did not build the agent completes `/start` from the
instructions alone and gets a first draft within 30 minutes; `/profile` shows and edits rules;
manager vs teammate get different profiles; 25 contacts → 20 profiles with pinned ones kept;
refresh posts "what changed" and undo restores; two users never see each other's data;
`/delete_my_data` removes everything.

### Tests for User Story 4

- [X] T061 [P] [US4] Integration tests `tests/integration/test_us4_profiles.py`: `test_profile_cap_and_pinning` (25 active contacts, 3 pinned → 20 current, pinned never retired, least-active unpinned retired on newcomer), `test_per_contact_profile_selected_over_global`, `test_group_chat_is_one_contact`, `test_refresh_keeps_user_rules_and_undo`, `test_rule_proposal_after_repeated_edits` (5 of 6 edits same pattern → proposal; accept updates `user_rules_json`), `test_weak_profile_warning_under_50_samples` Also `tests/unit/test_us4_style.py` (edit features, user rules, refresh note, language parsing, ownership coverage).
- [X] T062 [P] [US4] Integration tests `tests/integration/test_us4_account.py`: `test_two_users_are_isolated` (every repo query and bot view scoped to the caller), `test_delete_my_data_cascades_and_logs_out`, `test_my_data_counts_and_export_own_data_only`, `test_telegram_disconnect_stops_processing_and_reconnect_resumes`, `test_per_chat_override_language_and_formality`, `test_ooo_state_produces_out_of_office_draft`, `test_history_shows_no_content` Plus `test_every_repo_function_is_scoped_by_user` and `test_delete_my_data_log_out_branches`. `test_telegram_disconnect_stops_processing_and_reconnect_resumes` covers processing while the client is not running / running again; the `/telegram` command is 002's (T071).

### Implementation for User Story 4

- [X] T063 [P] [US4] Extend `src/scribe/style/profiles.py`: `rank_contacts(user)` (own-message counts per person/group over 6 months at import, since-last-refresh + existing at refresh), `assign_profile_slots(user)` (pinned first, then by rank up to `max_contact_profiles=20`; retire the rest → `state=retired`), `build_contact_profile(contact, samples)` (≤ 20 examples), `select_profile(user, contact)` → current contact profile else global; group chats count as one contact **Also** `build_profiles_from_import` (global + top-contact profiles from one import; used by `/profile` 🔄 Rebuild); a contact needs ≥ 10 own messages for a profile; the importer keeps samples per chat in memory only. **Setup-flow wiring deferred to T073**: `setup_flow.py` (rewritten by 002) still calls `build_global_profile`; after the 002 merge it calls `build_profiles_from_import` instead, until then contact profiles come from 🔄 Rebuild and the weekly refresh. Profile rules, user rules and summaries are now encrypted at rest (same column types, no migration).
- [X] T064 [P] [US4] Implement `src/scribe/style/proposals.py`: after each `edited_and_approved`, diff draft vs edited text into features (emoji removed/added, sign-off removed, length ratio, formality markers, language switch); when the same feature appears in ≥ 5 of the last 6 edits for a profile → create `style_rule_proposals` (`proposed`) and post the proposal message; `rule_accept` merges into `user_rules_json`, `rule_dismiss` closes Hooked into `review.py` after an edited send; rule texts and keys live in the encrypted `rule_json`; logs carry ids and feature names only.
- [X] T065 [US4] Implement weekly refresh job `refresh_profiles` in `src/scribe/style/profiles.py` + registration in `scheduled_jobs`: samples = user's own messages of last 30 days (incl. agent-sent); for each current profile create a new version (`previous` ← old), copy `user_rules_json` unchanged, use `build_style_profile(batch=True)` when the adapter has `batch` capability else synchronous; post "🔄 Profiles refreshed… Changes: …" note with `undo_refresh:<batch_id>`; `undo` swaps previous↔current for all profiles in the batch; `activity_records(profile_refreshed|profile_undo)`; a `python -m scribe.jobs refresh_profiles --user <id>` CLI entry in `src/scribe/jobs.py` Job registered from `src/scribe/jobs.py` (`register_profile_jobs`, wired in `__main__`; Sunday 02:00 UTC); contact ranking and slot assignment run inside it (no separate `rank_contacts` job); the global and contact builds go through one `build_style_profiles` call (one batch when the adapter can batch); the batch id is the `profile_refreshed` activity record.
- [X] T066 [US4] Implement `/profile` in `src/scribe/telegram/control_bot/settings.py` per contracts: global summary (version, sample size), "Rules you set" list, buttons edit rules (FSM free-text rule add/remove), contact profiles list (N/20) with 📌 `pin:<id>` / `unpin:<id>` / 🗑 `removeprofile:<id>`, ↩️ undo last refresh, 🔄 rebuild (re-run importer + builds) In `control_bot/profile_command.py` (included by the settings router); callbacks `profrules:<id>`, `profcontacts:0`, `profview:<id>`, `pin`/`unpin`/`removeprofile:<id>`, `undo_refresh:<batch_id>`, `profrebuild:0|1`, `rule_accept`/`rule_dismiss:<id>` (contracts/control-chat.md).
- [X] T067 [US4] Implement per-chat overrides UI in `/chats` chat settings in `src/scribe/telegram/control_bot/settings.py`: language (BCP-47 validated), formality 1–5, group drafting mode `mentions_only|always`, delay override; `src/scribe/drafting/composer.py` applies `override_language`/`override_formality` on top of the selected profile (FR-024 precedence) The composer already applied the overrides (US1); also group mode and enable / disable (a disabled chat drops its waiting turns, `chat_disabled`).
- [ ] T068 [US4] Implement `/ooo [text]` (state `out_of_office`, `ooo_message`; `src/scribe/drafting/composer.py` passes `user_state.ooo` so drafts become out-of-office replies; `/ooo` with no text clears) and `/history` (last 20 `activity_records` rendered without any message content) in `src/scribe/telegram/control_bot/settings.py`
- [ ] T069 [US4] Implement `/delete_my_data` in `src/scribe/telegram/control_bot/settings.py`: two-step confirmation ("type DELETE") → Telethon `log_out()` → stop client → `DELETE FROM users WHERE id=…` (cascade) → final confirmation message; `activity_records(data_deleted)` written before deletion to logs only
- [ ] T070 [US4] Implement `/my_data` in `src/scribe/telegram/control_bot/settings.py` (FR-026): counts per type (messages, drafts, style profiles, commitments, activity records) with oldest item and purge date; 📦 Export button builds a JSON file of all the caller's rows (decrypted, grouped by table, no other user's data) and sends it as a document in the control chat; `activity_records` kind `data_exported` (add to the enum in `src/scribe/db/models.py` and a migration)
- [ ] T071 [US4] **Moved to `specs/002-telegram-qr-login` (T034–T036); disconnect now keeps `users.state` (002 research Q9).** Implement `/telegram` in `src/scribe/telegram/control_bot/settings.py` and `setup_flow.py` (FR-030): show session status; Disconnect → Telethon `log_out()`, `client_pool.stop`, `telegram_sessions.status='revoked'`, user `setting_up`, waiting turns left untouched but not processed; Reconnect → phone → code → 2FA steps reused from setup, then `client_pool.start` and user back to `active`
- [ ] T072 [US4] Multi-user hardening: aiogram middleware in `bot.py` rejects callbacks whose entity (`draft_id`, `turn_id`, `profile_id`, `proposal_id`) does not belong to the calling `user_id`; all repo functions require `user_id`; add `src/scribe/admin.py` CLI `set-tier <user> quality|economy` and `list-users`
- [ ] T073 [US4] Setup-flow polish in `src/scribe/telegram/control_bot/setup_flow.py` per contracts "Setup FSM states": time zone step (auto from Telegram if available, else ask), hours and delay steps with validation, import progress message with counts, tasks steps from T056, retry on bad code/password, clear instructions text; `/help` lists all commands

**Checkpoint**: All four stories independently functional; quickstart US4 scenario passes.

**Phase 6 status for T068–T073** (kept here, not on the task lines above, so that this branch
merges into `specs/002-telegram-qr-login` without conflicts: 002 rewrites the T071 line, and
git conflicts on changes to adjacent lines; tick the lines above when 002 has merged):

- [X] T068 done: `/ooo [text]` and `/history` in `settings.py`; the composer passes
  `user_state.ooo` and the system warning `out_of_office` (card: "⚠️ out-of-office reply").
  `ooo_message` stays non-NULL exactly while out of office, also through /pause → /resume.
- [X] T069 done in `control_bot/account_command.py` (included by the settings router): two-step
  "type DELETE" (10 min; anything else cancels) → Telegram log-out → client stopped → user row
  deleted (cascade; FSM row by trigger) → task-source cache entry dropped → final message;
  `data_deleted` to the logs only. **After 002 merges**: call 002's `MessagingGateway.log_out`
  directly in `account_command.log_out_telegram` (now `getattr(gateway, "log_out", None)`, with a
  warning when absent; TODO(002 merge) in the code).
- [X] T070 done in `control_bot/account_command.py` + `retention/user_data.py`; the
  `data_exported` enum value already existed in migration 0001, so no enum change or migration.
- [ ] T071 owned by `specs/002-telegram-qr-login` (T034–T036); not implemented here.
- [X] T072 done except the global registration: `control_bot/ownership.py`
  (`OwnershipMiddleware`, every entity callback: draft, turn, profile, proposal, refresh batch,
  commitment, work chat) is attached to the settings router (and the routers it includes) and the
  review router; repo functions take `user_id` (cross-user ones listed and tested in
  `test_every_repo_function_is_scoped_by_user`); `src/scribe/admin.py` `set-tier` / `list-users`.
  **After 002 merges**: register the middleware once on the dispatcher in `bot.py` (global
  registration in bot.py after 002 merges) and drop the router-level registrations.
- [ ] T073 **blocked until 002 merges** (`setup_flow.py` is rewritten there). Also then: the
  Trello steps deferred from T056, and the import step calls `build_profiles_from_import` (T063)
  instead of `build_global_profile`.
- [X] Phase 6 review fixes (`tests/integration/test_us4_review_fixes.py`, no migration, no new
  enum value): every refresh run is recorded (`meta_json.noop`/`pending`), so own messages are
  never counted twice; profile writes take `pg_advisory_xact_lock(hashtext('profiles:'||
  user_id))` (builds, refresh, undo, Remove, pin, rule edits, accepted proposals) and Rebuild an
  in-process per-user lock (second tap answered, confirm button removed, synchronous LLM calls);
  🗑 Remove is a `profile_undo` record (`removed_contact_id`) that rebuild and refresh respect; a
  Rebuild supersedes the latest refresh and undo is refused unless every row it created is still
  current, and never exceeds 20 contact profiles; `reencrypt_profiles` job + CLI rewrites legacy
  plaintext profile, proposal, commitment and draft-output rows; disabling a chat locks the chat,
  drops pending drafts (card updated) and waiting turns with a conditional update, and the
  composer ends a `reading` turn of a disabled chat; `previous` versions older than 30 days are
  purged unless the latest undoable refresh needs them; the ownership middleware fails closed on
  malformed entity callbacks and its test derives the actions from the registered filters;
  other commands end ✏️ Edit rules; a failed deletion after log-out is explained; an interrupted
  weekly job is requeued at startup and resumes with the users not refreshed yet.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T074 [P] Write `README.md` (what it is, Telegram account-access disclosure, ToS caution on user-account automation, quickstart link) and `docs/deployment.md` (env vars, master key handling/rotation, disk encryption recommendation, backups, log retention, and a per-provider data-use checklist: before setting `data_use_confirmed: true` for any provider in `config/llm_routes.yml`, the operator verifies that provider's terms exclude training on submitted data for the account used)
- [ ] T075 [P] Observability pass: ensure every LLM route and `draft_prepared`/`sent` emit one structured line with `route, provider, model, prompt_version, latency_ms, cost_usd, cached_input_tokens`; add `tests/unit/test_log_fields.py`; verify `grep -c '"text"'` on captured logs is 0
- [ ] T076 [P] Resilience: `GatewayBusy`/flood-wait backoff in `src/scribe/scheduling/scheduler.py` step 1 and `src/scribe/sending/send.py` (reschedule, never duplicate-send: check `sent_telegram_message_id` before sending), Trello/LLM timeouts with bounded retries in `src/scribe/context/tasks/trello.py` and `src/scribe/llm/registry.py`, startup recovery in `src/scribe/__main__.py` of `reading` turns left by a crash (revert to `waiting`)
- [ ] T077 Run `evals/run.py --compare compose.draft=claude-opus-5-5,claude-sonnet-5-5` on the full thread set; record scores and per-draft cost in `specs/001-telegram-reply-agent/eval-report.md`; set the `compose.draft` tier defaults in `config/llm_routes.yml` according to the thresholds rule in research R5
- [ ] T078 Security review: confirm no plaintext secrets or content in logs, fixtures are synthetic, Trello token read-only, callback ownership checks (T072), prompt data blocks delimited and system prompt states they are untrusted; record findings in `specs/001-telegram-reply-agent/security-review.md`
- [ ] T079 Execute every scenario in `specs/001-telegram-reply-agent/quickstart.md` against a staging deployment with two Telegram accounts; fix defects; tick the checklist in `specs/001-telegram-reply-agent/checklists/requirements.md` notes with results

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: no dependencies; T002–T007 parallel after T001.
- **Phase 2 (Foundational)**: depends on Phase 1. Blocks all stories. T009 → T010 → T011; T012 → T013 → T014 → T015/T016; T017 → T018 → T019; T020 after T011; T021–T023 after T011; T024 after T014 and T017.
- **Phase 3 (US1)**: depends on Phase 2. MVP.
- **Phase 4 (US2)**: depends on Phase 2; builds on US1's router/scheduler/review modules (T031, T036, T039) but is testable on its own with the fake gateway.
- **Phase 5 (US3)**: depends on Phase 2; extends US1's composer (T035) and card (T038).
- **Phase 6 (US4)**: depends on Phase 2; extends US1's profiles (T033) and setup (T040).
- **Phase 7 (Polish)**: after the stories you intend to ship.

### User Story Dependencies

- **US1 (P1)**: only Phase 2. Delivers value alone.
- **US2 (P2)**: shares files with US1 (`router.py`, `scheduler.py`, `review.py`); implement after US1 or coordinate on those files.
- **US3 (P2)**: independent of US2; touches `composer.py`, `render.py`, `draft.md` from US1.
- **US4 (P3)**: independent of US2/US3; touches `profiles.py`, `setup_flow.py`, `settings.py`.

### Within Each Story

Tests first (they must fail), then pure modules, then pipeline/handlers, then UI, then evals.

### Parallel Opportunities

- Phase 1: T002, T003, T004, T005, T006, T007 together.
- Phase 2: T011 ‖ T012 ‖ T017; then T013 ‖ T015 ‖ T016 ‖ T018 ‖ T022 ‖ T023.
- US1: T025–T029 together; then T030 ‖ T031 ‖ T032 ‖ T033 ‖ T034.
- US2: T042 ‖ T043 ‖ T044; T045 ‖ T046.
- US3: T050 ‖ T051 ‖ T052; T053 ‖ T054 ‖ T055.
- US4: T061 ‖ T062; T063 ‖ T064.
- US3 and US4 can be developed in parallel by two people after US1.

---

## Parallel Example: User Story 1

```bash
# Tests first, in parallel:
Task: "Unit tests tests/unit/test_router.py"
Task: "Unit tests tests/unit/test_working_hours.py and tests/unit/test_delay.py"
Task: "Unit tests tests/unit/test_flags.py and tests/unit/test_composer_validation.py"
Task: "Integration tests tests/integration/test_us1_draft_flow.py"

# Then independent modules together:
Task: "src/scribe/scheduling/working_hours.py + pick_delay"
Task: "src/scribe/intake/router.py"
Task: "prompts/filter_context.md, prompts/draft.md, prompts/style_profile.md"
Task: "src/scribe/style/importer.py + global profile build"
Task: "src/scribe/context/history.py"

# Then sequentially: composer (T035) → scheduler step 1 (T036) → send (T037) → card (T038) → review (T039) → setup (T040) → evals (T041)
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1 → Phase 2 (boot, bot answers `/help`, scheduler ticks).
2. Phase 3 (US1) with delay range 0–0 by default in tests.
3. **Stop and validate**: `make test-fast test-integration eval`, then quickstart US1 with two
   Telegram accounts. This alone is a usable product for one user.

### Incremental Delivery

1. US1 → MVP demo.
2. US2 → natural timing; the product stops looking like a bot.
3. US3 → grounded answers with tasks and commitments.
4. US4 → multi-user readiness, personalization, deletion. Run T077 (model comparison) before
   onboarding external users to settle the compose-model default.

### Parallel Team Strategy

- Everyone: Phases 1–2.
- Dev A: US1 → US2 (same files). Dev B: US3 after US1's composer lands. Dev C: US4 after US1's
  profiles/setup land.

---

## Notes

- Constitution Principle I is enforced in three places on purpose: the DB check constraint
  (T009), `send.py`'s assertion (T037), and `test_no_send_without_approval` (T029).
- Nothing about models or providers is hard-coded: all LLM choices live in
  `config/llm_routes.yml` (T013); the compose default is decided by T077's measured comparison.
- Never log or fixture real message content; the redaction processor (T006) is tested, and
  eval threads (T041, T060) are synthetic.
- Commit after each task or logical group; stop at each checkpoint to validate the story.
