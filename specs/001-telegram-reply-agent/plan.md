# Implementation Plan: Telegram Work Reply Agent

**Branch**: `001-telegram-reply-agent` | **Date**: 2026-10-16 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-telegram-reply-agent/spec.md`

## Summary

A shared, multi-user service that watches each user's enabled Telegram work chats through
their own Telegram account, holds every incoming message unread for a randomly chosen delay
within a configured range and within working hours, then drafts a reply in the user's
learned style (global + per-contact profiles), grounded in the chat history, extracted
commitments and the user's Trello tasks. Drafts are reviewed in a private Telegram control
chat (a bot with inline buttons); nothing is ever sent without explicit approval.

Technical approach: one Python 3.12 asyncio service containing (a) a Telethon MTProto client
per user for reading and sending as the user, (b) an aiogram bot for the control chat,
(c) a PostgreSQL-backed scheduler that polls `due_at` timestamps (no message broker),
(d) an `LLMProvider` abstraction with a configurable route table (provider + model +
effort per logical route: context filtering and extraction on a small model, composition
and profile building on a strong one; Anthropic and OpenAI-compatible adapters, nothing
hard-coded) using structured outputs for drafts, style profiles and commitment extraction,
and (e) a `TaskSource` interface with a Trello implementation (Jira later). Message bodies, session strings and tokens are
encrypted at application level; messages are purged after 30 days.

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**: Telethon (Telegram MTProto user client), aiogram 3 (control bot),
`anthropic` SDK and `openai` SDK (LLM provider adapters, selected per route by config),
SQLAlchemy 2 async + asyncpg + Alembic (storage), httpx (Trello REST), pydantic v2 +
pydantic-settings (schemas, config), PyYAML (route table), `cryptography` (AES-GCM at-rest
encryption), structlog (JSON logs)

**Storage**: PostgreSQL 16 (single database; per-user rows keyed by `user_id`; encrypted
columns for message text, session strings, task-source tokens)

**Testing**: pytest + pytest-asyncio (unit, integration against a Postgres test container),
respx for Trello HTTP mocking, a `FakeLLMProvider` and `FakeMessagingGateway` for
deterministic tests; `evals/` runner for draft quality (groundedness, style, safety) on a
synthetic thread set

**Target Platform**: Linux server, Docker Compose (service + Postgres); single process for
the first release

**Project Type**: Single long-running service (asyncio worker + Telegram bot); no public
HTTP API. A minimal `/healthz` endpoint is exposed for container health only.

**Performance Goals**: draft ready ≤ 30 s after a message's delay expires (p95, SC-003);
scheduler tick every 5 s so no message is read more than 1 min late (SC-009); 100
concurrently active users on one instance

**Constraints**: no reply ever sent without an explicit approval callback (Principle I);
message content never logged in plaintext; Telegram flood-wait limits respected per
account; LLM calls use structured outputs so draft metadata (sources, flags, placeholders)
is machine-checkable; 30-day message retention enforced by a daily job

**Scale/Scope**: ~100 active users, ~50 enabled chats per user, ~200 incoming work messages
per user per day peak; ≤ 20 per-contact profiles per user; 4 user stories, 37 FRs

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status (pre-research) | Status (post-design) |
|---|---|---|---|
| I. Human approval before sending | The only code path that sends a reply is the control-bot approval callback; no scheduler job can send. Auto-send does not exist in this release. | PASS (spec FR-013/FR-014) | PASS: `sending.send_approved_draft()` is invoked solely from the `approve` callback handler; `Draft` state machine has no transition to `sent` except via `approved_by_user_at` being set. |
| II. Privacy & data minimization | Only enabled chats are stored; content encrypted at rest; 30-day purge; no content in logs or fixtures; tokens from env/DB-encrypted. | PASS (FR-001, FR-020, FR-026, FR-035) | PASS: `EncryptedText` column type; `retention.purge_messages()` daily; structlog processor redacts `text`/`body` keys; eval fixtures are synthetic. |
| III. Grounded replies, no fabrication | Drafts carry machine-readable sources, placeholders and commitment flags; the prompt forbids unsupported facts; evals check groundedness. | PASS (FR-005 to FR-008) | PASS: `DraftOutput` structured schema (contracts/draft-output.schema.json) requires `sources[]`, `placeholders[]`, `flags`; drafts with `needs_info=true` render placeholders visibly. |
| IV. Faithful personal style | Profiles learned from the user's own messages, inspectable, editable; changes never silent. | PASS (FR-023, FR-033, FR-034, FR-037) | PASS: `StyleProfile` has `summary_text` + `rules_json` + `version`; weekly refresh writes a new version and posts a diff note; undo restores previous version. |
| V. Evaluation-driven development | Versioned eval set; eval run required for prompt/model changes; deterministic code unit-tested. | PASS | PASS: `evals/threads/*.jsonl` + `evals/run.py` with thresholds; CI job `make eval` on changes under `src/scribe/drafting/`, `src/scribe/style/`, `prompts/`. |
| VI. Simplicity & observability | Minimal services; providers behind narrow interfaces; structured logs with redaction. | PASS | PASS: one process + Postgres, no broker/cache; `LLMProvider` (adapters registered by name, provider/model/effort chosen per route in `config/llm_routes.yml`, nothing hard-coded), `TaskSource`, `MessagingGateway` protocols; structlog JSON with `route`, `provider`, `model`, `prompt_version`, `latency_ms`, `cost_usd`, `decision`. |
| Security & data handling | Minimum scopes; encryption at rest; injection resistance; provider no-training. | PASS (with one justified exception) | PASS: Trello read-only token; AES-GCM columns; incoming text placed only inside delimited data blocks in the user turn, no LLM route defines tools, injection cases covered by evals and a unit test; every provider in `llm_routes.yml` must carry `data_use_confirmed: true` (operator attests no training on user data) or startup fails. **Exception — Telegram scope**: Telegram has no scoped grant for user accounts, so the MTProto session necessarily has full account access. Mitigations: disclosed at setup before login, session string encrypted, only enabled chats are read or stored, user can disconnect (`/telegram`) or revoke from Telegram's Active Sessions at any time. |

**Gate result**: PASS. No violations; Complexity Tracking left empty.

## Project Structure

### Documentation (this feature)

```text
specs/001-telegram-reply-agent/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── control-chat.md              # Control-bot commands, buttons, message layouts
│   ├── internal-interfaces.md       # LLMProvider / TaskSource / MessagingGateway protocols
│   └── draft-output.schema.json     # Structured-output schema for drafts
└── tasks.md             # Phase 2 output (/speckit-tasks - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
pyproject.toml
docker-compose.yml
Dockerfile
alembic.ini
alembic/                     # migrations
config/
├── llm_routes.yml           # route → provider, model, effort, max_tokens; per-tier compose model
└── llm_prices.yml           # per-model $/M tokens for cost_usd logging
prompts/                     # versioned prompt files (filter_context.md, draft.md, style_profile.md, commitments.md, style_judge.md)
src/scribe/
├── __main__.py              # `python -m scribe` entrypoint: starts bot, user clients, scheduler
├── config.py                # pydantic-settings: env vars, feature limits (delay defaults, caps), route-table loading + env overrides
├── logging.py               # structlog setup + redaction processor
├── crypto.py                # AES-GCM helpers, EncryptedText SQLAlchemy type
├── db/
│   ├── models.py            # SQLAlchemy models (see data-model.md)
│   ├── session.py           # engine / session factory
│   └── repos/               # one repo module per aggregate (users, chats, messages, drafts, profiles, commitments)
├── telegram/
│   ├── gateway.py           # MessagingGateway protocol
│   ├── user_client.py       # Telethon: per-user client, update handlers, mark-read, send, history import
│   ├── client_pool.py       # starts/stops one Telethon client per active user
│   └── control_bot/
│       ├── bot.py           # aiogram dispatcher
│       ├── setup_flow.py    # /start, link code, Telegram login (phone/code/2FA), work-chat selection
│       ├── review.py        # draft cards: approve / edit / reject / regenerate / read-now
│       ├── settings.py      # delays, hours, overrides, pause, OOO, profiles, commitments, data deletion
│       └── render.py        # message/keyboard builders
├── intake/
│   ├── router.py            # is this message in an enabled chat? mention/reply rules; grouping window
│   └── waiting.py           # create WaitingMessage with random delay within range + working hours
├── scheduling/
│   ├── scheduler.py         # 5 s tick: due waiting messages, reminders, expiries; daily/weekly jobs
│   └── working_hours.py     # next-allowed-time computation (tz-aware)
├── context/
│   ├── history.py           # recent chat messages + prior messages with contact (30-day window)
│   ├── commitments.py       # extraction from outbound messages; conflict detection
│   └── tasks/
│       ├── base.py          # TaskSource protocol + Task model
│       ├── trello.py        # Trello implementation (read-only)
│       └── jira.py          # stub raising NotImplemented (release version)
├── style/
│   ├── profiles.py          # build / refresh / select profile (global, per-contact, cap 20, pinning)
│   ├── proposals.py         # edit-pattern detection → StyleRuleProposal
│   └── importer.py          # 6-month own-message import at setup, discard raw after build
├── drafting/
│   ├── composer.py          # filter context (small model) → compose (strong model) → DraftOutput → Draft row
│   └── flags.py             # sensitive-topic / commitment / conflict flagging helpers
├── sending/
│   └── send.py              # the ONLY place a reply is sent; requires approved draft
├── llm/
│   ├── provider.py          # LLMProvider protocol + request/response pydantic models
│   ├── routes.py            # route table model, tier resolution, env overrides
│   ├── registry.py          # provider name → adapter factory; `get(route, user_tier)`
│   ├── pricing.py           # cost_usd from token usage + llm_prices.yml
│   ├── adapters/
│   │   ├── anthropic.py     # Claude: structured outputs, caching, effort, fallbacks, batches
│   │   ├── openai_compat.py # OpenAI and OpenAI-compatible servers (vLLM, Ollama…): response_format JSON schema
│   │   └── fake.py          # deterministic adapter for tests
│   └── schema_fallback.py   # JSON-schema prompting + validate + 1 retry for adapters without native structured outputs
├── retention/
│   └── purge.py             # 30-day message purge; account closure cascade
└── observability/
    └── health.py            # /healthz

tests/
├── unit/                    # pure logic: working hours, delay picking, router, flags, proposals
├── integration/             # Postgres-backed flows with Fake providers: intake → waiting → draft → approve → send
└── contract/                # schema validation of DraftOutput; TaskSource contract tests (Trello via respx)

evals/
├── threads/                 # synthetic JSONL threads with expected criteria
├── run.py                   # runs composer against threads, scores groundedness/style/safety; `--compare` runs two compose models side by side
└── thresholds.yml
```

**Structure Decision**: Single Python package `src/scribe` organised by domain stage
(intake → scheduling → context/style → drafting → sending), with the Telegram and LLM
integrations isolated behind protocols in `telegram/gateway.py`, `llm/provider.py` and
`context/tasks/base.py`. No separate frontend or API project exists because the only user
interface is the Telegram control chat.

## Complexity Tracking

No constitution violations to justify.

## Phase Summary

- **Phase 0 (research.md)**: resolved all technical unknowns: Telegram access method and
  login flow, "unread" semantics and read-detection, control-chat linking, scheduler
  design, LLM routing (configurable provider/model per route, two-tier cost model) and
  injection defences, Trello mapping, encryption, profile representation, commitment
  extraction, retention, multi-tenancy limits, eval design.
- **Phase 1 (data-model.md, contracts/, quickstart.md)**: entities and state machines,
  control-chat UI contract, internal protocols, draft structured-output schema, and an
  end-to-end validation guide mapped to the four user stories.
- **Phase 2**: `/speckit-tasks` generates tasks.md from these artifacts.
