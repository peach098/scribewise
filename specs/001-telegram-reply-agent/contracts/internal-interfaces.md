# Contract: Internal Interfaces

These protocols isolate external systems (Constitution Principle VI) and define the shape
of data crossing module boundaries. Signatures are Python `typing.Protocol`s; bodies are
not part of this contract.

## LLM routing  (`src/scribe/llm/`)

Two layers: a **route table** decides *which provider and model* serve a logical route;
**provider adapters** implement the calls. Callers never name a provider or model; they
call `LLMRouter.<route>(request, user_tier)`.

### Route table  (`config/llm_routes.yml`, env-overridable)

```yaml
routes:
  retrieve.filter_context: {provider: anthropic, model: claude-haiku-5-5, effort: low,    max_tokens: 2000}
  extract.commitments:     {provider: anthropic, model: claude-haiku-5-5, effort: low,    max_tokens: 2000}
  extract.deadlines:       {provider: anthropic, model: claude-haiku-5-5, effort: low,    max_tokens: 1000}
  compose.draft:
    tiers:
      quality:             {provider: anthropic, model: claude-opus-5-5,   effort: medium, max_tokens: 4000}
      economy:             {provider: anthropic, model: claude-sonnet-5-5, effort: medium, max_tokens: 4000}
  profile.build:           {provider: anthropic, model: claude-opus-5-5,   effort: high,   max_tokens: 16000, prefer_batch: true}
  eval.style_judge:        {provider: anthropic, model: claude-haiku-5-5, effort: low,    max_tokens: 2000}
providers:
  anthropic:     {api_key_env: ANTHROPIC_API_KEY, data_use_confirmed: true}
  openai_compat: {api_key_env: OPENAI_API_KEY, base_url_env: OPENAI_BASE_URL, data_use_confirmed: false}
```

`data_use_confirmed` is the operator's attestation that the provider does not train on
submitted data under the account in use (constitution Security section). A route that
resolves to a provider with `data_use_confirmed: false` fails startup.

Override any leaf with `SCRIBE_LLM__<ROUTE>__<FIELD>` (e.g.
`SCRIBE_LLM__COMPOSE_DRAFT__QUALITY__MODEL=claude-opus-5-5`,
`SCRIBE_LLM__EXTRACT_COMMITMENTS__PROVIDER=openai_compat`). Unknown provider names or a
model missing from `config/llm_prices.yml` fail startup, not the first request.

### LLMRouter  (`src/scribe/llm/registry.py`)

```python
class LLMRouter:
    async def filter_context(
        self, req: FilterContextRequest, *, user_tier: Tier
    ) -> FilterContextOutput: ...
    async def draft_reply(self, req: DraftRequest, *, user_tier: Tier) -> DraftOutput: ...
    async def build_style_profile(
        self, req: StyleProfileRequest, *, batch: bool = False
    ) -> StyleProfileOutput: ...
    async def extract_commitments(self, req: CommitmentRequest) -> list[CommitmentItem]: ...
    # Task deadlines in comments / descriptions the deterministic parser left (issue #6);
    # only for the tasks the filter selected, one call per draft.
    async def extract_deadlines(self, req: DeadlineRequest) -> list[DeadlineItem]: ...
    async def style_judge(self, req: StyleJudgeRequest) -> StyleJudgeOutput: ...
```

### ProviderAdapter  (`src/scribe/llm/provider.py`)

```python
class ProviderAdapter(Protocol):
    name: str
    capabilities: Capabilities  # structured_outputs, prompt_caching, batch, reasoning_effort

    async def complete_structured(
        self,
        *,
        model: str,
        system: str,
        user_blocks: list[Block],
        schema: type[BaseModel],
        effort: Effort | None,
        max_tokens: int,
        cache_prefix: bool,
    ) -> StructuredResult[BaseModel]: ...
    async def complete_structured_batch(
        self, requests: list[...]
    ) -> list[StructuredResult]: ...  # only if capabilities.batch
```

- `StructuredResult` carries the validated pydantic object plus `Usage {input_tokens,
  output_tokens, cached_input_tokens, latency_ms}`; the router adds `cost_usd` from
  `config/llm_prices.yml` and logs `route, provider, model, prompt_version, usage,
  cost_usd` — never content.
- Adapters with `structured_outputs=True` use the provider's native mechanism (Anthropic
  `output_config.format`; OpenAI-compatible `response_format` json_schema). Others go through
  `schema_fallback.py`: schema in the prompt → parse → validate → one retry → raise
  `ProviderOutputInvalid`.
- No adapter call ever defines tools; prompts place all user-derived text in delimited data
  blocks (research R5).
- Shipped adapters: `anthropic`, `openai_compat`, `fake`. `fake` returns canned outputs keyed
  by request fingerprint and records calls for assertions.

### Request / response models

- `FilterContextRequest`: `incoming_messages[] {id, sender, text, sent_at}` (`sent_at` in the
  user's time zone, ISO 8601 with offset; optional for old fixtures), `candidates
  {history[] {id, is_user, sender, text, sent_at}, tasks[] {id, title, status, due, assignees},
  commitments[] {id, what, due}}` (≤ 60 history, open tasks capped at 100 — the contact's own
  first, then by due date — and all open commitments to the contact). Ids are namespaced:
  `msg:<uuid>`, `task:<provider id>`, `commit:<uuid>`.
  `FilterContextOutput`: `history_ids[]`, `task_ids[]`, `commitment_ids[]` — the router
  drops any id not offered and caps history at 15; the filter may **select only**, never
  rewrite. If the route fails, the composer falls back to the deterministic default (last
  15 history messages + all tasks + all commitments) and adds `"filter_unavailable"` to the
  draft's `system_warnings` (see "System warnings" below), not to `DraftOutput`.
- `DraftRequest`: `incoming_messages[]`, `history[]` (filtered), `contact {display_name,
  kind}`, `profile {rules_json, user_rules_json, examples[]}`, `chat_overrides {language?,
  formality?}`, `tasks[]` (each `due` the user's local date), `commitments[]`, `user_state
  {ooo?: text}`, and the date anchor `today` (the user's local date of the newest incoming
  message) and `timezone` (the user's IANA zone); `incoming_messages[].sent_at` and
  `history[].sent_at` are local too. `prompts/draft.md` (version 3) resolves relative dates
  ("в среду", "завтра", "до конца недели") against `today` / a message's `sent_at`. These are
  request-side fields only: `DraftOutput` and `draft-output.schema.json` are unchanged. Tone
  judgement is part of the draft call; the output's `tone_shift_applied` records it.
- `DraftOutput`: see `draft-output.schema.json`. The composer rejects an output whose
  `sources` reference ids not present in the request (prevents invented sources): it
  regenerates once, and if the second output is still invalid it drops the unknown ids, sets
  `needs_info=true` and adds `"unsupported_sources"` to `system_warnings`.
- `StyleProfileRequest`: `samples[] {text, sent_at, contact_display_name?}`,
  `scope: "global"|"contact"`, `existing_user_rules_json`. `StyleProfileOutput`:
  `rules_json`, `summary_text`, `chosen_example_indices[]`. Several profiles (a rebuild's
  contacts, a weekly refresh's global + contacts) go through `LLMRouter.build_style_profiles` in
  one call: one Message Batch when the `profile.build` route has `prefer_batch` and the adapter
  the `batch` capability, else sequential synchronous calls; a failed item leaves that profile
  unchanged. `build_style_profiles(reqs, interactive=True)` (setup import and 🔄 Rebuild: the
  user is waiting) always uses synchronous calls; only the weekly refresh batches.
- `DraftRequest.profile` is the selected profile (`style/profiles.select_profile`: the turn
  contact's current profile — in a group chat the group's — else the current global one) and
  `chat_overrides` the chat's `override_language` / `override_formality`;
  `user_state.ooo` is `users.ooo_message` while the user is out of office, else null.
- `CommitmentRequest`: `outgoing_text`, `contact_display_name`, `sent_at` (in the user's time
  zone: the date anchor, so weekdays resolve to the user's dates), `recent_incoming[]` (≤ 5
  incoming messages of the last 2 days in that chat, each with a local `sent_at`).
  `CommitmentItem`: `what, to_whom, due (ISO date or null), confidence (0–1), evidence
  (quote)`; `prompts/commitments.md` is version 2.
- `StyleJudgeRequest`: `draft_text`, `profile_summary`, `reference_examples[]`.
  `StyleJudgeOutput`: `score 0–1`, `reasons[]`.

## TaskSource  (`src/scribe/context/tasks/base.py`)

```python
class Task(BaseModel):
    id: str
    title: str
    status: str
    due_at: datetime | None  # aware; shown / compared as due_on(users.timezone)
    assignees: list[str]
    url: str
    source: Literal["trello", "jira"]

    def due_on(self, timezone: str) -> date | None: ...
    @property
    def done(self) -> bool: ...  # status (list name) like Done / Готово / Closed


class TaskSource(Protocol):
    @property
    def provider(self) -> Literal["trello", "jira"]: ...

    async def validate(self, credentials: dict) -> TaskSourceIdentity: ...
    async def list_containers(self, credentials: dict) -> list[Container]: ...  # boards / projects
    async def list_open_tasks(self, conn: TaskSourceConnection) -> list[Task]: ...
```

- Read-only: no method mutates the remote system (FR-021).
- `TrelloTaskSource`: `GET /1/members/me` (validate), `GET /1/members/me/boards`
  (containers), `GET /1/boards/{id}/cards?filter=open&fields=name,due,dueComplete,idList,idMembers,url`
  + `GET /1/boards/{id}/lists` + `GET /1/boards/{id}/members`. Cards with `dueComplete=true` are
  skipped. A board that fails is skipped and counted in the returned `TaskList`'s
  `skipped_containers` (partial load); only all boards failing raises. Results cached 60 s per
  user; no entry is kept longer than a day.
- `JiraTaskSource`: stub in this release; raises `NotImplementedError("release version")`.
- Credentials are `{"key", "token"}` (`task_source_connections.credentials`, encrypted);
  `list_open_tasks` reads `config_json.board_ids`. `TaskSourceUnavailable` messages never carry
  the key, the token or a request URL; httpx/httpcore loggers are capped at WARNING because
  their INFO lines contain request URLs.
- Failure contract: any provider error → `TaskSourceUnavailable`; the composer proceeds
  with `tasks=[]` and adds `"tasks_unavailable"` to the draft's `system_warnings`, which the
  card renders as "⚠️ drafted without task context". The draft-time service
  (`context/tasks/service.py`, `TaskSources.load_open_tasks`) reads the connection, closes the
  DB session, then calls the provider with a 10 s overall timeout (5 s per request); it never
  raises: the Jira stub's `NotImplementedError`, timeouts and any unexpected exception (logged
  by type only) count as unavailable too, and after a failure the user's source is not asked
  again for 30 s (those drafts are unavailable without counting another failure). A partial
  load adds `"tasks_partial"` to `system_warnings` ("⚠️ some task boards could not be read").
  No connection (or `disconnected`) → no tasks and no warning.
  `config_json.consecutive_failures` counts failures in a row; from the 3rd the connection
  `status` becomes `error` (shown by `/tasks`, and the control chat gets one alert per streak,
  remembered as `config_json.error_alerted`); a success resets both. Every writer of
  `config_json` (failure counter, alert flag, board toggle, reconnect) locks the row
  (`tasks_conn.get_connection_for_update`).

## MessagingGateway  (`src/scribe/telegram/gateway.py`)

```python
class MessagingGateway(Protocol):
    async def start(self, user_id: UUID) -> None: ...
    async def stop(self, user_id: UUID) -> None: ...
    async def list_dialogs(self, user_id: UUID) -> list[Dialog]: ...
    async def fetch_own_history(
        self, user_id: UUID, chat_id: int, since: datetime
    ) -> AsyncIterator[IncomingMessage]: ...
    async def mark_read(self, user_id: UUID, chat_id: int, up_to_message_id: int) -> None: ...
    async def send_text(
        self, user_id: UUID, chat_id: int, text: str, reply_to: int | None
    ) -> int: ...
    # specs/004-conversation-search: search over earlier conversations
    async def search_chat(
        self, user_id: UUID, chat_id: int, query: str, *, limit: int = 20
    ) -> list[FoundMessage]: ...
    async def get_messages(
        self, user_id: UUID, chat_id: int, message_ids: Sequence[int]
    ) -> list[FoundMessage]: ...
    async def get_usernames(self, user_id: UUID, peer_ids: Sequence[int]) -> dict[int, str]: ...
    def on_new_message(self, handler: Callable[[IncomingMessage], Awaitable[None]]) -> None: ...
    def on_read_elsewhere(self, handler: Callable[[ReadEvent], Awaitable[None]]) -> None: ...
    def on_outgoing(self, handler: Callable[[OutgoingEvent], Awaitable[None]]) -> None: ...


@dataclass(frozen=True)
class FoundMessage:  # specs/004
    chat_id: int
    message_id: int
    sender_peer_id: int
    sender_display_name: str
    is_outgoing: bool
    text: str  # text, else media caption, else the document file name
    sent_at: datetime
    reply_to_id: int | None = None
    thread_top_id: int | None = None
    mentioned_peer_ids: frozenset[int] = frozenset()
    mentioned_usernames: frozenset[str] = frozenset()  # lower-case, without "@"
```

- Receiving updates MUST NOT mark anything read; only `mark_read` does.
- `send_text` is called exclusively by `sending/send.py`, which asserts
  `draft.state == "pending" and draft.approved_by_user_at is not None` first, refuses an
  `outdated` draft unless called with `allow_outdated=True` ("Send anyway", or the user's own
  edited text), and locks the draft and then its turn (`pending_review` required) before the
  call.
- `FakeMessagingGateway` records calls and lets tests inject events.
- `search_chat` (server-side `messages.search` in one chat, newest first), `get_messages` (one
  batched lookup by id, missing ids skipped) and `get_usernames` (lower-case usernames, peers
  without one omitted) serve the search over earlier conversations
  ([specs/004-conversation-search/contracts/internal-interfaces.md](../../004-conversation-search/contracts/internal-interfaces.md)).
  Only `EarlierSearch` calls them, and only with chat ids of the user's enabled work chats. They
  MUST NOT sleep on a flood wait (raise `GatewayBusy`), raise `GatewayNotStarted` when the
  user's client is not running and `SessionRevoked` when the session is gone.

## Scheduler contracts  (`src/scribe/scheduling/scheduler.py`)

Each tick (5 s) runs, in order, each in its own transaction with `FOR UPDATE SKIP LOCKED`:
0. `conversation_turns` stuck in `reading` (`updated_at` older than `stuck_reading_s`, default
   600 s) that no handler in this process is still working on → `waiting` by the requeue rule
   below (a failed statement must not strand a batch until restart).
1. `conversation_turns` where `state='waiting' AND due_at <= now()` and the user is `active`
   or `out_of_office` (paused users' messages stay unread) → `reading`, in batches (500) until
   nothing is due; **commit and release the lock** per batch, then (outside any transaction)
   mark read (`read_concurrency`, default 50) and return; composition runs afterwards in the
   background (`compose_concurrency`, default 10), so a slow LLM never delays reading
   (SC-009). A chat is never marked read past the first message of a turn still `waiting` in
   it. On failure revert to `waiting` with `due_at = now() + 60 s` (per turn when a batch
   statement fails; if even that fails, step 0 picks the turn up). At startup, turns left in
   `reading` are put back to `waiting`. Requeue rule (startup and step 0): due now when the
   turn was already marked read (`read_at`) or has a `read_now` record, else
   `next_allowed(now)` in the user's time zone and working hours (SC-004).
2. `drafts` where `state='pending' AND remind_at <= now() AND reminded_at IS NULL` → set
   `reminded_at`; after commit reply "⏰ Still waiting…" to the card (skipped if already
   expired).
3. `drafts` where `state='pending' AND expires_at <= now()` → `expired`; turn → `handle_manually`;
   `activity_records(expired)`; after commit the card → "⌛ Expired — handle manually".
   Steps 2–3 live in `scheduling/deadlines.py`; their handlers may return an after-commit
   callable for control-chat messages. Neither can send to a contact.
4. `scheduled_jobs` where `next_run_at <= now()` → run job, set next. At startup
   `Scheduler.ensure_jobs()` creates missing rows (a job registered with `run_at_start=True`,
   e.g. `reencrypt_profiles`, is due at once) and makes a job due again whose latest scheduled
   run was claimed but never recorded as finished (interrupted by a restart).

Pure helpers with unit-test contracts:
- `pick_delay(min_s, max_s, rng) -> int` : uniform, inclusive bounds.
- `next_allowed(t, tz, working_days, start, end) -> datetime` : returns `t` if inside a
  working window, else the next window start; DST-safe.

## Draft composition pipeline  (`src/scribe/drafting/composer.py`)

```
turn → load candidates (turn + ≤40 recent in chat + ≤20 prior with contact, 30-day window)
     → tasks (TaskSource, tolerant) + open commitments for contact/chat
     → LLMRouter.filter_context  (small model; ids only; deterministic fallback on failure)
     → select profile (per-contact current → global current) + chat overrides
     → LLMRouter.draft_reply      (compose model for the user's tier)
     → validate sources ⊆ provided ids; compute flags (sensitive keywords ∪ model flags)
     → conflicts = model ∪ backstop: exactly one date the incoming messages ask for (ISO,
       dd.mm.yyyy, dd.mm after a date preposition or before a month word, weekday names —
       3-letter English abbreviations only after on/by/until/before/до/к/в/во —,
       tomorrow, end of the day / week; en/ru, resolved against the message's local date; a
       bare "today" and dates before that day are ignored) that is earlier than the due date of
       a commitment or unfinished task the filter selected (skipped when the filter fell back
       or several dates are asked for); same-day double-booking is left to the model
     → persist Draft (pending, remind_at, expires_at; provider/model/cost recorded)
     → render card → activity record
```

### System warnings

`DraftOutput` is the model-facing contract and mirrors `draft-output.schema.json` exactly; its
`context_warnings` holds only what the model reports. Warnings the system detects itself are
kept separately as `system_warnings: list[str]`, stored next to the model output in
`drafts.output_json`. That column holds metadata in the clear only — `language`, `needs_info`,
`tone_shift_applied`, `context_warnings`, `creates_commitment` / `sensitive` (booleans),
`system_warnings`, `reply_to_message_id` — and every content-bearing `DraftOutput` field
(`placeholders`, `sources`, `flags`, `conflicts`) in one AES-GCM string `enc`; the draft text is
only in `drafts.text`. `drafting/stored_output.py` packs it (`pack_output`) and is the one reader
(`load_stored_output`, also for rows with the older plaintext layout); the retention job removes
`enc` (and old plaintext keys) with `drafts.text` 30 days after `finalized_at`.

| Code | Set when |
|---|---|
| `filter_unavailable` | `retrieve.filter_context` failed; deterministic fallback used |
| `unsupported_sources` | the draft cited ids not in the request twice; unknown ids dropped |
| `weak_style_profile` | the selected profile is weak or missing (fewer than 50 samples) |
| `tasks_unavailable` | the task source failed (US3) |
| `tasks_partial` | some selected boards could not be read; the others were used (US3) |
| `out_of_office` | the user is out of office: `user_state.ooo` carried `users.ooo_message` and the draft is an out-of-office reply (US4) |

The draft card renders the union of `context_warnings` and `system_warnings`.

### Commitment extraction  (`src/scribe/context/commitments.py`)

Every outgoing message in an enabled work chat is extracted exactly once:

- agent sends: `sending/send.py`, after the send is committed (the reply is already sent and
  recorded; nothing here can fail or undo it);
- manual messages: `intake/events.py`, when the outgoing update is new (not a redelivery) and
  not an agent send. The Telethon echo of an agent send is recognised by
  `drafts.sent_telegram_message_id` and skipped.

`CommitmentExtractor.submit` awaits the short DB reads (user time zone, chat contact, recent
incoming), then runs `LLMRouter.extract_commitments` and the insert as a background task (≤ 4
at once); failures are logged without content and dropped. Stored: items with `confidence ≥
0.7` whose `evidence` quotes the outgoing text (words compared case- and
punctuation-insensitively; anything else, e.g. a promise lifted from `recent_incoming`, is
dropped), with `due` kept only when the deterministic date extractor finds it in the outgoing
text relative to its local date ("в четверг", "завтра", "до конца недели"), not equal to an
open commitment of the same contact on (normalised `what`, `due`), as `open` rows with
`source_message_text` and `source_sent_at`; the dedupe read runs under
`pg_advisory_xact_lock(hashtext(user_id || ':' || contact_id))`. The contact is the chat's contact
(the person in a private chat, the group as a whole in a group chat). At draft time the composer
loads open commitments for the turn's contact and, in a group chat, also for the person who wrote.
The extractor instance is created by the first `Composer` (`ensure_default_extractor`) and
installed as the process default that `send.py` and `ChatEventHandler` use unless one is passed
explicitly; later composers with the same session factory (each 🔁 Regenerate) reuse it.
`Service.stop()` drains it (`drain_default_extractor`, up to 10 s, then the rest is cancelled)
before disposing of the engine. Known gap: an extraction cut off by that timeout or by a crash
mid-call is lost (the message is not extracted again). Explicit wiring in `__main__` follows
the 002 merge.
