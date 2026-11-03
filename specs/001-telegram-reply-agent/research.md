# Phase 0 Research: Telegram Work Reply Agent

All NEEDS CLARIFICATION items from the Technical Context are resolved below. Each entry
records the decision, why, and what else was considered.

## R1. How the agent acts as the user in Telegram

**Decision**: Telethon (MTProto) user client, one per user, authenticated with the user's
own phone number; session stored as an encrypted session string.

**Rationale**: The spec requires replies to appear as written by the user, messages to stay
*unread* until the agent consumes them, and detection of the user reading a chat on another
device. Only a user-account (MTProto) client can do all three; the Bot API cannot read a
user's chats or send as the user. Telethon is actively maintained, asyncio-native, supports
multiple concurrent clients in one process, and does not mark messages as read unless asked.

**Alternatives considered**: Pyrogram (maintenance stalled since 2024; forks fragment);
Bot API (cannot act as the user); TDLib via bindings (heavier native dependency, no benefit
for this scope).

**Login flow**: phone → code → optional 2FA password, collected in the control chat
(R2) during setup; the resulting `StringSession` is encrypted (R8) and stored. The service
never stores the password or code. Users are told they are granting full account access and
can revoke the session from Telegram's "Active sessions" at any time.

## R2. Control chat

**Decision**: A single Telegram Bot (aiogram 3) serves every user's private control chat.
Linking: the user presses `/start` in the bot, which creates a `User` row and starts the
setup flow; the Telethon login happens inside that conversation. Drafts are shown as
messages with inline keyboards (approve / edit / reject / regenerate); editing uses a
"reply with the corrected text" step.

**Rationale**: Inline buttons and command menus require a bot; a user-account client cannot
show buttons. One bot for all users is how Telegram multi-tenant bots work; per-user state
is keyed by the bot chat id ↔ `user_id`.

**FSM storage**: aiogram conversation state (setup steps, "reply with your edited text",
rule editing) is persisted in Postgres through a small custom `BaseStorage` implementation
backed by a `bot_fsm_state` table (key = bot chat id, state, data jsonb), so a restart in
the middle of setup or an edit does not lose the user's place. No Redis is introduced.

**Alternatives considered**: Using the user's own "Saved Messages" via Telethon (no
buttons, fragile parsing of free-text commands); a separate bot per user (operationally
absurd at 100 users); aiogram `MemoryStorage` (state lost on every restart); Redis storage
(extra service just for this; Principle VI).

## R3. "Unread" semantics and read detection

**Decision**: Incoming messages are received via Telethon updates without acknowledging
them. The scheduler commits the `waiting → reading` transition and releases its row lock
*before* calling Telegram or the LLM, so slow composition never holds database locks. When a waiting message becomes due, the agent calls
`client.send_read_acknowledge(entity, max_id=<message id>)`, which marks the chat read up to
that message (Telegram shows "read" to the sender) and then drafts. If Telethon delivers
`UpdateReadHistoryInbox` / `UpdateReadChannelInbox` for a chat with a waiting message
(the user read it themselves on another device), the wait is cut short and drafting starts
immediately (spec edge case). If an outgoing message from the user appears in a chat with a
waiting message or pending draft (`UpdateNewMessage` with `out=True`), the turn is dropped
(FR-015).

**Rationale**: Matches the spec's delay semantics exactly and uses only standard update
types. Receiving updates does not mark anything read, so the illusion of "not yet seen" is
preserved until the agent decides to consume.

**Alternatives considered**: Polling `get_dialogs()` for unread counts (slow, rate-limited);
ignoring read-on-other-device (spec explicitly asks for it).

## R4. Scheduling

**Decision**: A PostgreSQL-backed scheduler: `WaitingMessage.due_at`, `Draft.remind_at`,
`Draft.expires_at` and `ScheduledJob.next_run_at` (daily purge, weekly profile refresh) are
polled every 5 s with `SELECT ... FOR UPDATE SKIP LOCKED`. No message broker.

**Rationale**: Scale is ~100 users; a 5 s tick easily meets "never more than 1 minute late"
(SC-009). Keeping schedule state in the same database makes "cancel on manual reply",
"read now" and restarts trivial and transactional. `SKIP LOCKED` lets a second instance be
added later without redesign.

**Alternatives considered**: APScheduler (in-memory jobs lost on restart unless configured
with a job store, which is just the DB again); Celery/Redis (a broker and a worker fleet for
a problem the DB already solves; violates Principle VI).

**Delay selection**: `random.SystemRandom().uniform(min, max)` seconds within the applicable
range (per-chat override, else global). `due_at = next_allowed(arrival + delay)` where
`next_allowed` moves the time into working hours (R4b).

**R4b. Working hours**: stored as per-user time zone (IANA), working days (set of weekday
ints) and a daily start/end `time`. `next_allowed(t)` returns `t` if inside a working
window, else the start of the next window. Pure function, fully unit-tested, including
DST boundaries via `zoneinfo`.

## R5. LLM routing: configurable providers and models per route

**Decision**: Nothing about the LLM is hard-coded. A **route table** (`config/llm_routes.yml`,
every value overridable by environment variables) maps each logical route to a
`provider`, `model`, `effort`/reasoning setting and `max_tokens`. Providers are adapters
registered by name behind the `LLMProvider` protocol; the first release ships the
Anthropic adapter and an OpenAI-compatible adapter (covers OpenAI and self-hosted servers
such as vLLM/Ollama that speak the same API) plus a `fake` adapter for tests. Adding a
provider is a new adapter module and a registry entry, nothing else.

Routes and defaults:

| Route | Purpose | Default provider/model | Effort |
|---|---|---|---|
| `retrieve.filter_context` | Select which history messages, tasks and commitments are relevant to this turn. Returns **ids only**, never summaries. | anthropic / `claude-haiku-5-5` | low |
| `extract.commitments` | Structured extraction from the user's outgoing messages | anthropic / `claude-haiku-5-5` | low |
| `compose.draft` | The reply itself (includes tone-shift judgement; no separate tone route) | anthropic / `claude-opus-5-5` (tier `quality`); `claude-sonnet-5-5` (tier `economy`) | medium |
| `profile.build` | Setup import and weekly refresh of style profiles; weekly refresh runs through the provider's batch API when available (50% cost) | anthropic / `claude-opus-5-5` | high |
| `eval.style_judge` | LLM-as-judge in the eval runner | anthropic / `claude-haiku-5-5` | low |

Per-user **tier** (`quality` default, `economy` optional) selects the `compose.draft` model;
all other routes are shared. The compose default is decided by data, not taste: the eval
runner reports style-judge and groundedness for both candidate models side by side
(`evals/run.py --compare`), and the default is switched to the cheaper model only if it
holds every threshold (Principle V).

**Provider adapter contract**: each adapter must (a) return outputs validated against the
pydantic schemas in `contracts/internal-interfaces.md`; where the provider has native
structured outputs (Anthropic `output_config.format`, OpenAI `response_format` JSON schema)
it uses them, otherwise it prompts with the JSON schema, validates, and retries once;
(b) report `input_tokens`, `output_tokens`, `cached_input_tokens`, `cost_usd` (from a
per-model price table in config) for every call; (c) declare capabilities
(`structured_outputs`, `prompt_caching`, `batch`, `reasoning_effort`) so the composer and
the weekly job can degrade gracefully (e.g. no batch API → run refresh synchronously).

**Anthropic adapter specifics**: adaptive thinking (model default) with per-route
`output_config.effort`; structured outputs via `output_config.format`; server-side refusal
fallbacks on by default (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`);
prompt caching with the system prompt and the stable style profile placed first under
`cache_control` and volatile content (incoming message, filtered history, tasks) after;
Message Batches for `profile.build` weekly refresh.

**Cost model** (list prices, Oct 2026: Opus 5.5 $4/$20 per M in/out, Sonnet 5.5 $2/$10,
Haiku 5.5 $0.10/$0.50): the `compose.draft` route is ~90% of spend (~$0.02–0.03 per draft
on Opus, about half on Sonnet); all auxiliary routes together cost well under $1 per user
per month. The Haiku pre-filter pays for itself by cutting the volatile Opus input from
~4K to ~1–2K tokens per draft. Real per-route cost is logged and reviewed after the first
week of use.

**Rationale**: Constitution Principle VI requires swappable providers; the spec's core
value (style imitation) justifies a strong composing model while retrieval and extraction
are simple enough for a small one. Keeping all choices in one route table makes model and
provider changes an operator decision with no code change, and the tier gives the shared
service a pricing lever.

**Prompt-injection defence** (FR-010, constitution Security section): incoming message text,
history and task titles are never placed in the system prompt; they are embedded in the
user turn inside clearly delimited data blocks, the system prompt states that those blocks
are untrusted content, no route defines tools, the filter route may only return ids that
were offered to it, and the compose output is a draft object that a human reviews. Nothing
the model emits can trigger an action.

**Alternatives considered**: one model for every route (simplest, but 10× the cost on
routes that do not need it); a separate tone-detection call (removed: the draft schema
already carries `tone_shift_applied`, so the extra call bought nothing); Batch API for
commitment extraction (rejected: batches may take up to 24 h, and a commitment can be needed
by the next draft minutes later); free-text output parsed with regex (brittle, cannot
enforce sources); giving the model tools to fetch tasks itself (more moving parts and an
injection surface; the composer fetches context deterministically instead); a
LangChain-style framework for provider abstraction (heavier dependency than a 4-method
protocol warrants; Principle VI).

## R6. Task source: Trello now, Jira later

**Decision**: `TaskSource` protocol with `list_open_tasks(user) -> list[Task]` and
`search(user, query) -> list[Task]`. `Task` = `{id, title, status, due, assignees,
url, source}`. Trello implementation uses the REST API with a per-user key/token (read
scope only): boards the user selects at setup → lists → cards; `status` = list name,
`due` = card due, `assignees` = member full names, related person matched by name against
contact display names. Tasks are fetched live at draft time with a 60 s in-memory cache
per user; nothing from Trello is persisted except the connection and board selection.

**Rationale**: Read-only (FR-021), minimal data retention (Principle II), and the protocol
makes Jira (Atlassian REST v3, JQL `assignee = currentUser() AND statusCategory !=
Done`) a drop-in later (FR-019).

**Alternatives considered**: syncing tasks into the DB (more state, staleness); Trello
webhooks (write permission not needed and not wanted).

## R7. Encryption at rest and secrets

**Decision**: Application-level AES-256-GCM (`cryptography`) with a master key from the
`SCRIBE_MASTER_KEY` env var (base64, 32 bytes); per-value random 96-bit nonce; a custom
SQLAlchemy `EncryptedText` type. Encrypted columns: message text, draft text, Telethon
session string, Trello token, style-profile example messages. Key rotation is supported by
a `key_id` prefix on each ciphertext. Disk-level encryption of the Postgres volume is
additionally recommended in deployment docs.

**Rationale**: Constitution requires encryption at rest and credentials outside source
code; a DB dump without the env key is useless. Column-level keeps the DB queryable on
metadata (ids, timestamps, states) which is all the scheduler needs.

**Alternatives considered**: relying on disk encryption only (does not protect dumps or
DB-admin access); pgcrypto (key must be passed into SQL, visible in logs).

## R8. Style profile representation

**Decision**: Each `StyleProfile` version stores `rules_json` (structured: language(s),
formality 1–5, typical length in words, greeting/sign-off patterns, emoji usage, punctuation
habits, typical phrases, things the user never does) plus `summary_text` (plain-language
summary shown to the user) plus up to N encrypted example messages (20 per contact, 50
global). Profiles are generated by a structured-output LLM call over the sample messages.
User-edited rules live in a separate `user_rules_json` that is merged on top and never
touched by refreshes (FR-037).

**Per-contact selection**: at import and at each weekly refresh, rank contacts (private
chats and group chats as single contacts) by count of the user's own messages in the last
6 months (import) / since the last refresh plus existing volume (refresh). Keep pinned
contacts, fill remaining slots up to 20 by rank; retire the rest (profile rows kept 30 days
for undo, then deleted).

**Sample per build** (issue #30): at most `profile_max_samples` (400) own messages per
`profile.build` call. A contact's profile (a group's too) gets its newest messages. The global
profile limits group chats so one busy group cannot crowd out the user's style with individual
people: each group chat offers at most `profile_group_samples_per_chat` (40) newest messages,
group chats together at most `profile_group_samples_share` (30%) while private chats can fill
the rest (otherwise group messages fill the space, so a group-only user still gets a full
sample), and very short messages (under 3 words, or emoji / "+" only) at most 15%. They still
count for contact ranking and the `weak` mark (fewer than `min_style_samples` own messages in
the history). A truncated answer is retried once, synchronously, with half the samples and the
route's effort one step lower; if that fails too, that profile stays as it was (the global and
the contact profiles fail independently).

**Rationale**: Structured rules make profiles inspectable and editable (Principle IV),
summaries make them readable, examples give the drafting prompt concrete voice. Separating
user rules guarantees "edited rules survive a refresh".

**Alternatives considered**: fine-tuning (not available for Claude, and opaque to the
user); embedding-based retrieval of similar past replies only (good voice, no explainable
rules; kept as a possible later enhancement for example selection).

## R9. Commitment extraction and conflict detection

**Decision**: Every outgoing message from the user in an enabled chat (manual or sent via
the agent) is passed through a low-effort structured-output extraction that returns zero or
more `{what, to_whom, due, confidence}` items; items with confidence ≥ 0.7 are stored as
open `Commitment` rows linked to the source message id and contact, but only when the item's
`evidence` quotes the outgoing text itself (a promise lifted from the incoming context is
dropped) and with a `due` only when the deterministic date extractor finds that date in the
outgoing text (relative to its local date). At draft time the
composer loads open commitments for the contact (and, for group chats, for the chat) plus
open tasks, passes them as context, and the draft schema requires the model to list
`conflicts[]` when the incoming request clashes with a due date. Users list/resolve/delete
commitments in the control chat (FR-031).

**Rationale**: Extraction at write time keeps commitments beyond the 30-day message window
(clarification) and avoids re-reading history at draft time.

**Alternatives considered**: extracting at draft time from history (loses commitments older
than 30 days); regex date parsing (misses most natural-language promises).

## R10. Retention and deletion

**Decision**: Daily job deletes `Message` rows older than 30 days; nulls `drafts.text` and
`drafts.edited_text` and removes the content keys of `drafts.output_json` (its encrypted
`enc` blob) 30 days after a draft reaches a final state; nulls
`commitments.source_message_text` 30 days after a commitment is resolved or deleted (and
then removes the row). Account closure deletes the user row with `ON DELETE
CASCADE` across all tables and logs out the Telethon session. Style profiles, commitments
and activity records are derived data retained until closure or explicit deletion; replaced
profile versions (`retired`, and `previous` ones that the latest undoable refresh would not
restore) are deleted 30 days after they were replaced.

**Rationale**: As clarified in the spec; satisfies constitution retention requirement.

## R11. Multi-tenancy limits

**Decision**: One process runs all Telethon clients (asyncio) and the bot. 100 concurrent
MTProto sessions is well within Telethon's comfort zone on a small VM. Telegram flood waits
are handled per client by Telethon's built-in `FloodWaitError` sleep (capped; longer waits
are logged and the affected operation rescheduled). Horizontal sharding (user_id hash →
instance) is possible later thanks to `SKIP LOCKED` scheduling and stateless drafting, but
is out of scope.

**Alternatives considered**: one container per user (simple isolation but 100 containers to
run and update; rejected for operational cost).

## R12. Evaluation set and tests

**Decision**: `evals/threads/*.jsonl`: synthetic threads, each with a style profile, recent
history, tasks, commitments, the incoming message, and expected criteria
(`must_mention`, `must_not_claim`, `expected_flags`, `expected_sources`,
`style_targets`). `evals/run.py` runs the composer with the real provider, scores
deterministic checks (flags, sources present, no unsupported dates) plus an LLM-judge for
style match, and compares to `thresholds.yml` (e.g. groundedness ≥ 0.95, flag recall ≥
0.9). Unit tests cover pure logic; integration tests use `FakeLLMProvider` and
`FakeMessagingGateway` against a Postgres container to prove the full approve→send path and
the "no send without approval" invariant.

**Rationale**: Principle V. The eval set is the regression guard for prompt/model changes;
fixtures are synthetic by construction (Principle II).

## R13. Setup and personalization UX in a bot

**Decision**: Setup is a linear aiogram FSM: link → Telegram login → pick work chats (paged
list of dialogs with toggles) → connect Trello (key/token + board pick) → defaults
(time zone auto-detected from Telegram if available, else asked; delay 2–10 min; hours
09:00–19:00 Mon–Fri) → import & build profiles (progress message) → "first draft will
appear here". Settings are commands (`/settings`, `/chats`, `/profile`, `/commitments`,
`/pause`, `/ooo`, `/waiting`, `/delete_my_data`) each opening an inline menu. Full contract
in `contracts/control-chat.md`.

**Rationale**: Meets FR-022 "without editing code" and the 30-minute setup target
(SC-007) within the Telegram-only decision.
