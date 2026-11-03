# Implementation Plan: Skip Messages That Need No Reply

**Branch**: `004-skip-closing-messages` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/004-skip-closing-messages/spec.md` (first drafted as 003, renumbered on the port to `main`)

## Summary

When a work-chat turn only closes the exchange ("thanks", "bye", "ok", "👍"), the agent reads
it at its normal planned time and prepares no draft. The user can list skipped messages for
the last 7 days in the control chat, ask for a draft anyway, and turn skipping off globally
or per chat.

Technical approach: a **reply gate** step in the existing `DueTurnsProcessor`, placed between
`mark_read` and `Composer.compose`. It has two stages:
1. A pure pre-gate drafts every turn that is long, contains a question, or has media, with no
   extra cost.
2. Only short candidates go to a new small-model LLM route, `classify.reply_needed`, which
   returns structured output.

The turn is skipped only on a confident "no reply needed", and only when the user does not
habitually answer such messages in that chat. Every failure or doubt leads to a draft. Skips
become a new turn state `skipped`, with decision columns on `conversation_turns`. "Draft
anyway" puts the turn back on the normal scheduler path. A separate eval suite with two
asymmetric thresholds guards the decision (recall of needed replies ≥ 98 %, skip rate on
closings ≥ 85 %).

## Technical Context

**Language/Version**: Python 3.12 (unchanged from 001)

**Primary Dependencies**: Unchanged: Telethon, aiogram 3, `anthropic`/`openai` SDKs behind
`LLMRouter`, SQLAlchemy 2 async + asyncpg + Alembic, pydantic v2, structlog. **No new
dependencies.**

**Storage**: PostgreSQL 16. Migration `0008` adds columns and enum values only, with no new
tables ([data-model.md](data-model.md)).

**Testing**: pytest + pytest-asyncio (unit, plus integration against the Postgres test
container); `FakeLLMProvider` for the new route; `evals/run.py` gate suite with dry-run
recordings.

**Target Platform**: Linux server, Docker Compose, single process (unchanged)

**Project Type**: Single long-running service (unchanged); the new UI lives in the control
chat.

**Performance Goals**:
- No visible delay for turns that need a reply: they pass the pre-gate without an LLM call,
  and only short candidates pay one small-model call (~1 s). Feature 001's SC-003 still holds:
  a draft within 30 s, p95 (spec SC-005).
- A skipped message is marked read at its planned time (SC-006).
- "Draft anyway" produces a card within one scheduler tick plus normal composition (≈ 35 s).

**Constraints**:
- Nothing new can send (Principle I).
- No message text in logs or activity records.
- The classifier sees at most the turn plus 8 history messages.
- On any gate failure the turn is drafted; it is never dropped.
- Callback data ≤ 64 bytes.

**Scale/Scope**:
- Same load as 001: ~100 users, ~200 incoming turns per user per day.
- The pre-gate is expected to pass roughly a third of turns to the classifier.
- 3 user stories, 16 FRs. Roughly 1 new module, 1 prompt, 1 control-bot router, 1 migration
  and 1 eval suite.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status (pre-research) | Status (post-design) |
|---|---|---|---|
| I. Human approval before sending | The feature may only *remove* drafts; it must add no send path. "Draft anyway" must go through normal review. | PASS (spec FR-008, FR-013; Assumptions exclude auto-replies) | PASS: the gate returns only `draft`/`skip` and has no gateway access except the existing `mark_read`. `draftany` moves the turn to `waiting`, after which the unchanged compose → card → approve path applies. `sending.send_approved_draft()` and the `sent_requires_approval` constraint are untouched. |
| II. Privacy & data minimization | Classifier input at most what drafting already sees; no text in skip records or logs; skipped content follows the 30-day retention. | PASS (FR-009, FR-010) | PASS: `ReplyNeededRequest` = turn + ≤ 8 recent messages (less than the 40 + 20 that drafting receives). Activity `skipped` meta is `{category, source, confidence}`. `reply_gate_decided` carries no text. The `/skipped` preview is decrypted only while rendering for the owner. There is no new storage of content. |
| III. Grounded replies, no fabrication | No draft content is produced by the gate. | PASS (n/a) | PASS: the gate output is a classification, never shown as text to anyone. Composition is unchanged. |
| IV. Faithful personal style | Respect the user's habit of answering closings in a chat. | PASS (FR-005) | PASS: a deterministic habit rule (research R3) drafts instead of skipping when the user answered ≥ 3 of the last 5 closings in the chat, and the card explains why. Style profiles are unchanged. |
| V. Evaluation-driven development | The decision is LLM-driven, so it needs a versioned eval set with thresholds; deterministic parts need tests. | PASS (FR-016) | PASS: `evals/threads/skip_*.jsonl` (≥ 60 synthetic ru/en threads). The thresholds `gate_reply_recall_min: 0.98`, `gate_skip_rate_min: 0.85` and `gate_injection_forced_skip_max: 0` are added to `evals/thresholds.yml`. `make eval` is required for changes to `prompts/reply_needed.md`, `drafting/reply_gate.py` or the route. There are unit tests for the pre-gate, the `decide()` ordering and the habit count. |
| VI. Simplicity & observability | No new service or dependency; the new route sits behind `LLMRouter`; structured decision logs. | PASS | PASS: there is one module, and its columns live on an existing table rather than a new one. The route is configured in `config/llm_routes.yml` like the others, and the provider is swappable and fakeable. One `reply_gate_decided` log line carries route, provider, model, prompt_version, latency_ms, cost_usd and the decision. |
| Security & data handling | Injection resistance; provider data-use attestation. | PASS | PASS: the prompt places all content in untrusted data blocks, the route has no tools, and instructions in messages cannot force a skip (FR-006; eval threshold `gate_injection_forced_skip_max: 0`). The new route is subject to the existing `data_use_confirmed` startup check. |

**Gate result**: PASS. No violations; Complexity Tracking left empty.

## Dependencies on Feature 001

This feature was first built on a base with only 001 US1 (T001–T041) and 002, then ported
(renumbered 003 → 004, because `003-jira-commitments` owns 003) onto `main`, which now has
001 US2–US4 and the i18n layer (issue #8). The 001 tasks it waited for are all on `main`:

| 001 task | Used for | How it is used now |
|---|---|---|
| T047 (`intake/events.py` `on_outgoing`; outdated handling in `intake/router.py`) | FR-005 manual replies in the habit count; FR-011 pending-draft rule; FR-013 "you already replied" | Manual replies are stored as outgoing `messages` rows and drop open turns (`dropped_reason='manual_reply'`); the habit count and "Draft anyway" availability read both. A closing turn dropped before the gate saw it is classified afterwards (`ReplyGate.classify_dropped`) so it counts for the habit rule. `IntakeRouter._flag_pending_draft` keeps flagging the pending draft `outdated` in its transaction; after the commit, `ReplyGate.decide_for_pending` may clear that again for a closing message and set `possibly_unnecessary` |
| T067 (`/chats` per-chat settings) | Per-chat "Skip thanks/bye" row | A row on each chat's `/chats` screen (`chatskip:<chat_id>:inherit\|off\|on`, same column as `skipchat:`), next to the per-chat view in `/skipped` |
| T068 (`/history`) | Showing `skipped` / `draft_anyway` | `💤 Skipped — <reason>` and `✍️ Drafted on request`, with the contact, no content |
| T043 (scheduler throughput test) | T037 throughput variant | `tests/integration/test_scheduler_throughput.py::test_scheduler_throughput_with_reply_gate` |

Every new control-chat text is in the `scribe.i18n` catalogs (`en.py`, `ru.py`).

**Migration chain**: `0008_skip_closing_messages` (revision `0008`) follows
`0007_search_earlier` (revision `0007`, `004-conversation-search`), which follows
`0006_beta_applications`, `0005_access_gate` (`005-beta-subscription-access`) and
`0004_jira_commitments` (`003-jira-commitments`). It was written as `0005` and renumbered when
those migrations reached `main` first.

## Project Structure

### Documentation (this feature)

```text
specs/004-skip-closing-messages/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R8
├── data-model.md        # Phase 1: columns, enums, state machine additions
├── quickstart.md        # Phase 1: validation guide
├── contracts/
│   ├── reply-gate.md                    # internal interface, route, settings, log line
│   ├── reply-needed-output.schema.json  # classifier structured output
│   └── control-chat.md                  # /skipped, callbacks, card additions
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
src/scribe/
├── drafting/
│   ├── reply_gate.py          # NEW: is_skip_candidate, decide(), ReplyGate, habit count
│   └── composer.py            # + "drafted_by_habit" system warning passthrough
├── llm/
│   ├── provider.py            # + ReplyNeededRequest/Output, route name, output schema
│   └── registry.py            # + classify_reply_needed(), PROMPT_FOR_ROUTE entry
├── scheduling/scheduler.py    # DueTurnsProcessor: gate between mark_read and compose
├── intake/router.py           # pending_review incoming → gate (decide_for_pending, FR-011)
├── intake/events.py           # manual reply drops an ungated closing → classify_dropped
├── db/
│   ├── models.py              # enums + columns per data-model.md
│   └── repos/turns.py         # list_skipped, draft_anyway (guarded), habit query
├── telegram/control_bot/
│   ├── skipped.py             # NEW: /skipped, draftany, skipcfg, skipchat, skippage
│   ├── render.py              # COMMAND_NAMES entry, skipped list, card banners
│   ├── review.py              # 💤 banner, regenerate keeps gate warnings
│   ├── settings.py            # includes skipped router; /chats row (chatskip); /history
│   └── ownership.py           # draftany / skipchat / chatskip ownership checks
├── i18n/en.py, i18n/ru.py     # every new control-chat text, English and Russian
└── config.py                  # skip_* settings
prompts/reply_needed.md        # NEW, version 1
config/llm_routes.yml          # + classify.reply_needed (small model, low effort)
alembic/versions/0008_skip_closing_messages.py   # NEW
evals/
├── threads/skip_*.jsonl       # NEW gate suite (≥ 60 synthetic threads)
├── run.py                     # gate suite loader, metrics, report
└── thresholds.yml             # + gate thresholds

tests/
├── unit/test_reply_gate.py              # pre-gate, decide() ordering, fallbacks
├── unit/test_skipped_render.py          # list layout, callback data ≤ 64 bytes
├── contract/test_reply_needed_schema.py # pydantic model ↔ JSON schema
├── integration/test_skip_flow.py        # skip after delay, no card, activity record,
│                                        # draft anyway, toggles, habit, ownership
├── integration/test_skip_pending_and_habit.py  # FR-011 pending draft, habit from real
│                                        # manual replies, /chats row, /history, Russian UI
└── integration/test_scheduler_throughput.py    # + gate-enabled variant (T037)
```

**Structure Decision**: Single project, extending the existing `src/scribe` layout. The gate
lives in `drafting/` next to the composer it guards. The control-chat surface is a new router
module following the `commitments_command.py` pattern: included in the settings router (same
"no other flow active" filter and `OwnershipMiddleware`), all texts from `scribe.i18n`.

## Complexity Tracking

No constitution violations to justify.
