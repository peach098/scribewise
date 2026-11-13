# Quickstart: validating the Telegram Work Reply Agent

This guide proves the feature end-to-end against the acceptance scenarios in
[spec.md](spec.md). It references [data-model.md](data-model.md) and
[contracts/](contracts/) rather than repeating them.

## Prerequisites

- Docker + Docker Compose; Python 3.12 and `uv` for local runs.
- A Telegram **bot token** (from @BotFather) for the control chat.
- Telegram **API id/hash** (my.telegram.org) for the Telethon user client.
- An **Anthropic API key**.
- A **Trello API key + token** with read scope for a test account, and a board with a
  card "Q3 report" due next Friday assigned to a member named like your test contact.
- Two Telegram accounts: the *user* (whose work chats the agent handles) and a *colleague*
  who sends test messages. For group-chat scenarios, a test group containing both.

## Configuration

Environment variables (all required unless noted; see `src/scribe/config.py`):

```
SCRIBE_DATABASE_URL=postgresql+asyncpg://scribe:scribe@db:5432/scribe
SCRIBE_MASTER_KEY=<base64 32 bytes>          # openssl rand -base64 32
SCRIBE_BOT_TOKEN=<botfather token>
SCRIBE_TG_API_ID=<int>
SCRIBE_TG_API_HASH=<hash>
ANTHROPIC_API_KEY=<key>                        # required for the default route table
OPENAI_API_KEY=<key>                           # only if a route uses provider openai_compat
OPENAI_BASE_URL=<url>                          # optional, for self-hosted OpenAI-compatible servers
SCRIBE_SCHEDULER_TICK_S=5                      # optional
SCRIBE_LOG_LEVEL=INFO                          # optional
SCRIBE_READ_CONCURRENCY=50                     # optional: parallel mark-read calls
SCRIBE_COMPOSE_CONCURRENCY=10                  # optional: parallel draft compositions
SCRIBE_DRAFT_REMIND_AFTER_S=7200               # optional: reminder after 2 h (FR-032)
SCRIBE_DRAFT_EXPIRE_AFTER_S=86400              # optional: expiry after 24 h (FR-032)
SCRIBE_TEST_FAST_CLOCK=0                       # optional: 1 → reminder 1 min, expiry 2 min
```

LLM routing lives in `config/llm_routes.yml` (see `contracts/internal-interfaces.md`); any
leaf can be overridden per deployment, e.g.
`SCRIBE_LLM__COMPOSE_DRAFT__ECONOMY__MODEL=claude-sonnet-5-5` or
`SCRIBE_LLM__EXTRACT_COMMITMENTS__PROVIDER=openai_compat`. Startup fails fast on an unknown
provider or a model missing from `config/llm_prices.yml`.

## Run

```bash
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run python -m scribe            # starts bot + scheduler; user clients start after login
```
Health: `curl localhost:8080/healthz` → `{"status":"ok","users_active":0}`.

## Automated checks (run before every push)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest tests/unit tests/contract            # fast, no DB
docker compose up -d db && uv run pytest tests/integration   # Fake LLM + Fake gateway, real Postgres
uv run python evals/run.py --threads evals/threads --thresholds evals/thresholds.yml
uv run python evals/run.py --dry-run --threads evals/threads --thresholds evals/thresholds.yml  # offline harness check
```
Expected: all green; eval report prints groundedness ≥ 0.95, flag recall ≥ 0.90,
style-judge ≥ 0.80 and **0** "unsupported fact" findings (plus tone-shift ≥ 0.85 and 100% of
injection threads resisted), plus total `cost_usd` per route. `--dry-run` serves every route
from the fake adapter with the canned outputs stored in each thread: it proves the runner,
scoring and thresholds without network or cost, and says nothing about model quality.

Model/tier comparison (run before changing the compose default or a route's model):
```bash
uv run python evals/run.py --compare compose.draft=claude-opus-5-5,claude-sonnet-5-5
```
Prints both models' scores side by side with per-draft cost; the cheaper model may become
the default only if it holds every threshold.

Key integration tests to look for (names are the contract):
- `test_no_send_without_approval`: scheduler and every non-approval callback can run for
  a pending draft and `FakeMessagingGateway.send_text` is never called.
- `test_delay_is_random_within_range`: 200 turns with range 120–600 s → all `delay_s`
  within bounds, not all equal.
- `test_next_allowed_outside_hours`: 22:00 arrival → due at next working day 09:00 in
  the user's tz, including a DST-transition date.
- `test_manual_reply_drops_turn`, `test_read_elsewhere_shortens_wait`,
  `test_outdated_on_new_message`, `test_expiry_after_24h`, `test_reminder_after_2h`.
- `test_agent_send_echo_during_send_is_not_a_manual_reply`: Telethon's echo of an approved
  send arrives while `send.py` still holds the draft lock → draft `sent`, turn `answered`,
  nothing dropped.
- `test_profile_cap_and_pinning`: 25 active contacts, 3 pinned → 20 current profiles,
  pinned never retired.
- `test_refresh_keeps_user_rules_and_undo`: a refresh writes new versions in one batch, posts
  "🔄 Profiles refreshed … Changes: …" with ↩️ Undo, keeps the user's rules; undo restores the
  previous summaries, once.
- `test_two_users_are_isolated`: every view and every entity callback of another user's rows is
  refused (SC-010).
- `test_sources_must_reference_request_ids`: a `DraftOutput` citing an unknown task id is
  rejected and regenerated once, then flagged `needs_info`.
- `test_filter_context_selects_only_offered_ids`: ids not offered are dropped; filter
  failure falls back to the deterministic default with `filter_unavailable` warning.
- `test_scheduler_throughput`: 100 users × 20 due turns with Fake providers, every LLM call
  held until the last turn is read (a model of any latency) → every turn drafted, all due
  turns claimed by the first tick after `due_at`, no turn read more than 60 s after `due_at`
  (SC-009). Runs on the wall clock with the production 5 s tick (issue #28).

Key unit tests: `test_route_table_overrides_and_tiers` (env override changes a route's
provider/model; `economy` user gets the economy compose model; unknown provider or
`data_use_confirmed: false` fails startup) and `test_user_text_never_in_system_prompt`.

## Manual end-to-end scenarios

### US1 — draft in my style (P1)
1. As the *user*, open the bot, `/start`, complete login, enable the private chat with the
   *colleague*, connect Trello, accept defaults; wait for "profiles built".
2. In `/settings`, set delay range **0–0 s** for this test.
3. As the *colleague*, send "Привет! Когда будет готов отчёт за Q3?".
4. Expect within 30 s: a draft card (see `contracts/control-chat.md`) in the user's
   style, profile "colleague" or "global", 📎 Sources listing the Trello card and/or prior
   messages.
5. Press ✅ Send → the reply appears in the chat **from the user's account**; card shows
   "✅ Sent".
6. Repeat with ✏️ Edit (reply with changed text → edited text is sent) and ❌ Reject
   (nothing sent). Send a message from a chat that is **not** enabled → no card, and
   `select count(*) from messages where work_chat_id = <that chat>` is 0.

### US2 — delayed reading (P2)
1. Set delay range **180–300 s**, working hours 09:00–19:00 in your tz.
2. Colleague sends a message at T. Verify in the colleague's client that the message stays
   **unread** (single tick) and no card appears before T+180 s; between T+180 and T+300 s
   the message turns read and a card appears.
3. `/waiting` while a message is waiting → item with planned time; press 👀 Read now →
   card appears immediately.
4. While a message is waiting, reply manually from the user's phone → bot posts "dropped",
   no card.
5. While a card is pending, colleague sends another message → card gets "⚠️ New messages
   arrived" banner with Regenerate.
6. Temporarily set working hours to exclude now; send a message → stays unread; set hours
   back → it is read within one tick.
7. Reminder/expiry: with a test setting `SCRIBE_TEST_FAST_CLOCK=1` (reminder 1 min,
   expiry 2 min), leave a card untouched → reminder reply after 1 min, "⌛ Expired" after 2.

### US3 — tasks and commitments (P2)
1. Ensure Trello card "Q3 report" due Friday, assigned to the colleague's name. Connect Trello
   with `/tasks` → 🔌 Connect Trello: send the API key, open the authorize link (read-only) and
   send the token (both messages disappear from the chat), then tick the board and press Done;
   `/tasks` shows "✅ Trello: connected (read-only) · 📋 Boards: 1 selected". (The guided setup
   does not ask for Trello yet: T073.)
2. Colleague asks "when will the report be ready?" → draft mentions Friday; 📎 Sources
   shows `Trello “Q3 report” (due Fri …)`; press 🧾 Sources to see it with its list (status).
3. From the user's account write manually "Отправлю презентацию в четверг" to the
   colleague; within a few seconds `/commitments` lists "send the presentation · <colleague> ·
   due Thu …" (resolve / edit / delete buttons). An approved draft that promises something is
   extracted the same way, once.
4. Colleague asks "can you send the presentation Wednesday?" → draft shows ⚠️ conflict
   with the Thursday commitment and 🤝 flag if it promises a new date.
5. Colleague asks something unknowable ("what did Petya decide at the 3pm meeting?") →
   draft contains a `[[...]]` placeholder and ❓ Needs info; Send asks for confirmation.
6. Revoke the Trello token (trello.com → Account → Applications) and wait a minute (task cache)
   → the next draft shows "⚠️ drafted without task context"; after three such drafts `/tasks`
   shows "⚠️ Trello: error" and the control chat got one "Reconnect it in /tasks" alert.

Automated counterparts: `tests/integration/test_us3_context.py` (`test_task_cited_as_source`,
`test_outgoing_message_creates_commitment`, `test_sent_draft_yields_exactly_one_commitment`,
`test_conflict_flagged`, `test_conflict_backstop_when_the_model_misses_it`,
`test_unknowable_question_yields_placeholder_and_confirmation`, `test_tasks_unavailable_warning`),
`tests/integration/test_us3_commands.py` (/tasks, /commitments, retention, background
extraction), `tests/contract/test_trello_task_source.py` (respx; read-only, 60 s cache, every HTTP
error → `TaskSourceUnavailable`, `test_jira_stub_raises_not_implemented`),
`tests/unit/test_commitments.py`, and the eval suite `evals/threads/us3_commitments.jsonl`
(`conflict_recall` and "no date without a source" checks; `make eval-dry` proves the harness
only).

### US4 — personalization (P3)
1. `/profile` shows a readable summary; ✏️ Edit rules → send "never use emojis" (or open the
   colleague under 👥 Contact profiles → 👁 → ✏️ Edit rules) → next draft to that colleague has no
   emojis. The draft card's "✍️ Draft (…)" names the profile used (the colleague, a group title,
   or "global"). (Contact profiles are built by 🔄 Rebuild and by the weekly refresh; the guided
   setup builds them too once T073 lands after `specs/002-telegram-qr-login`.)
2. Per-chat override: `/chats` → the colleague chat → 🌐 Language → "English" (and 🎩5 for formal)
   → next draft is in English even if the message is in Russian.
3. `/pause` → messages stay unread, no cards; `/resume` → processing continues.
4. `/ooo On vacation until 20 Oct` → next draft is an out-of-office style reply.
5. Edit 5 consecutive drafts the same way (remove sign-off) → a 💡 rule proposal appears;
   accept → `/profile` shows the rule.
6. Trigger the weekly refresh manually (`uv run python -m scribe.jobs refresh_profiles
   --user <id>`) → "🔄 Profiles refreshed" note; press ↩️ Undo → previous summary restored.
   Running it again right away changes nothing and sends no note (the run is still recorded,
   so the same messages are never counted twice). After a 🔄 Rebuild the refresh's ↩️ Undo
   answers "Nothing to undo …" and changes nothing; 🗑 Remove of a contact profile survives a
   🔄 Rebuild.
7. Second test user completes `/start`; verify neither user sees the other's chats,
   drafts or `/history` (SC-010).
8. `/my_data` shows counts by type; 📦 Export returns a JSON file with your data only.
   `/telegram` → Disconnect → no reading/drafting and the session disappears from Active
   Sessions; Reconnect restores it.
9. `/delete_my_data` → type `DELETE` → all rows for the user are gone
   (`select count(*) from messages where user_id = <id>` → 0) and the Telegram session is
   shown as terminated in the user's Active Sessions (log-out via 002's
   `MessagingGateway.log_out`; before that merge only a warning is logged and the session must
   be ended in Telegram by hand).
10. Operator: `uv run python -m scribe.admin list-users`, `uv run python -m scribe.admin set-tier
    <user id or control chat id> economy`. Rows stored before their columns were encrypted are
    rewritten by the `reencrypt_profiles` job (due at the first start, then daily) or now with
    `uv run python -m scribe.jobs reencrypt_profiles` (prints counts; a second run prints zeros).

Automated counterparts: `tests/integration/test_us4_profiles.py` (`test_profile_cap_and_pinning`,
`test_per_contact_profile_selected_over_global`, `test_group_chat_is_one_contact`,
`test_refresh_keeps_user_rules_and_undo`, `test_rule_proposal_after_repeated_edits`,
`test_weak_profile_warning_under_50_samples`), `tests/integration/test_us4_account.py`
(`test_two_users_are_isolated`, `test_every_repo_function_is_scoped_by_user`,
`test_delete_my_data_cascades_and_logs_out`, `test_delete_my_data_log_out_branches`,
`test_my_data_counts_and_export_own_data_only`,
`test_telegram_disconnect_stops_processing_and_reconnect_resumes` (processing stops while the
client is not running and resumes when it is; the `/telegram` command itself is tested in 002),
`test_per_chat_override_language_and_formality`, `test_ooo_state_produces_out_of_office_draft`,
`test_history_shows_no_content`), `tests/integration/test_us4_review_fixes.py` (no-op refreshes,
serialized builds, Remove vs Rebuild, undo after Rebuild and the slot cap, ciphertext at rest and
re-encryption of legacy rows, chat disable vs a turn being read, retention of `previous`
versions, rules input ending on other commands, deleted accounts, synchronous Rebuild and the
resumed weekly job) and `tests/unit/test_us4_style.py` (edit features, rules, the "what
changed" note, language parsing, callback-ownership coverage derived from the registered
callback filters, malformed callbacks refused).

## Observability check
Tail logs: every draft produces one JSON line per LLM route (`route=retrieve.filter_context`,
`route=compose.draft`) with `provider`, `model`, `prompt_version`, `latency_ms`, `cost_usd`,
`cached_input_tokens`, and one `event=draft_prepared` line; **no** `text` field anywhere.
`grep -c '"text"' logs` → 0.
