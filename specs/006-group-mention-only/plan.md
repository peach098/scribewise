# Implementation Plan: Group Chats — Draft Only When Addressed

**Branch**: `006-group-mention-only` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/006-group-mention-only/spec.md` (GitHub issue #25)

## Summary

In group work chats a conversation turn (and so a draft) is opened only for a message that
addresses the user: it tags the user (@username or name link) or it is a direct reply to one of
the user's messages. The per-chat `always` ("every message") group mode is removed, together
with its column, enum, buttons and texts; chats that had it fall back to the single rule.

Technical approach:
- **Intake rule** (`src/scribe/intake/router.py`): `ChatRule` loses `group_mode`; the pure
  `is_addressed` becomes `address_reason(message, rule) -> IntakeReason | None`
  (`private`, `reply`, `tagged`; `None` = not addressed). `decide` carries the reason into
  `IntakeDecision`, and the existing `intake` log line gains `reason` (FR-011). Follow-ups in
  the grouping window keep joining (`reason="follow_up"`, FR-008).
- **Signals** (`src/scribe/telegram/user_client.py`): `mentions_me` keeps coming from the
  server-set MTProto `Message.mentioned` flag, which already covers @username and name-link
  tags of the user ([research R1–R2](research.md)). Two small corrections: a forwarded message
  (`fwd_from` set) never counts as a tag (spec edge case), and `is_reply_to_me` ignores the
  implicit forum-topic header and service messages, so "posting in a topic the user created"
  is not a reply to the user (R3).
- **Schema**: Alembic `0009_drop_group_mode` drops `work_chats.group_mode` and the
  `group_mode` Postgres enum. No data update is needed before the drop, since the column goes
  away; FR-005 holds because the rule no longer reads it ([data-model.md](data-model.md)).
- **Control chat** (`src/scribe/telegram/control_bot/settings.py`): the group-mode button row
  goes; the chat settings card shows a fixed line "👥 Drafts in this group: only when you are
  tagged or replied to"; a stale `chatgroup:*` callback changes nothing and answers "This
  option is no longer available" ([contracts/control-chat.md](contracts/control-chat.md)).
  The setup chat picker gets one sentence stating the group rule (FR-006).
- **Docs**: 001 spec (FR-003, the group edge case, FR-024 / Work Chat entity), 001
  data-model and 001 control-chat contract updated (FR-010).

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: Unchanged: Telethon 1.x (installed 1.45), aiogram 3, SQLAlchemy 2
async + asyncpg + Alembic, structlog. **No new dependencies.**

**Storage**: PostgreSQL 16. Migration `0009` drops one column and one enum type; no new
tables, no data rewrite.

**Testing**: pytest + pytest-asyncio. Unit: `tests/unit/test_router.py`,
`tests/unit/test_user_client.py`, `tests/unit/test_i18n_us34.py`. Integration (Postgres test
container): `tests/integration/test_us1_pipeline.py`, `test_us4_account.py`,
`test_us2_review_fixes.py`, `test_earlier_search_drafts.py`, a migration up/down test next to
the existing ones in `tests/integration/test_jira_models.py`, and `alembic check`
(`test_models_and_repos.py`).

**Target Platform**: Linux server, Docker Compose, single process (unchanged)

**Project Type**: Single long-running service; UI is the Telegram control chat.

**Performance Goals**: No change. Intake stays a pure decision plus one `get_reply_message`
call for group replies (already made today).

**Constraints**:
- No message text in logs (the new `reason` field is an enum value).
- Callback data ≤ 64 bytes (only removals; the stale `chatgroup:<uuid>:<mode>` keeps parsing).
- Pending drafts and open turns are not re-evaluated at deploy (spec Assumptions).

**Scale/Scope**: ~6 source files, ~8 test files, 1 migration, 2 i18n catalogs, 3 docs in
`specs/001-telegram-reply-agent/`.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Human approval before sending | PASS | Intake only decides whether a draft is prepared; nothing new can send. Fewer drafts, same review path. |
| II. Privacy & data minimization | PASS | Unaddressed group messages are stored as context exactly as today (FR-003, already limited to enabled work chats). The new log field `reason` carries no content. Fixtures stay synthetic. Dropping `group_mode` removes a stored setting; nothing new is stored. |
| III. Grounded replies | PASS | Draft generation is unchanged; context still includes stored unaddressed messages (SC-004). |
| IV. Faithful personal style | PASS | No change to style profiles. |
| V. Evaluation-driven development | PASS | Deterministic intake code: unit tests written first (`decide`, `address_reason`, mapping helpers), integration tests for the pipeline and settings, a migration round-trip test. No prompt/model/retrieval change, so no eval run is required; `make eval-dry` stays green. |
| VI. Simplicity & observability | PASS | Removes a mode, a column, an enum and two buttons. No new abstraction: one small `IntakeReason` enum replaces a boolean so the decision is loggable (FR-011). Telegram stays behind `MessagingGateway`; `IncomingMessage` fields are unchanged. |

Re-check after Phase 1: still PASS. No Complexity Tracking entries.

## Project Structure

### Documentation (this feature)

```text
specs/006-group-mention-only/
├── spec.md
├── plan.md              # This file
├── research.md          # Phase 0: Telegram mention/reply signals, migration choice
├── data-model.md        # Phase 1: removed column/enum, IncomingMessage semantics, migration 0009
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   ├── intake-rule.md   # address_reason / decide contract and log line
│   └── control-chat.md  # chat settings card, stale callback, setup text, i18n keys
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/scribe/
├── intake/router.py                    # IntakeReason, address_reason(), decide(), intake log reason
├── telegram/user_client.py             # mentions_me excludes forwards; is_reply_to_me ignores topic headers
├── telegram/gateway.py                 # IncomingMessage docstring for mentions_me / is_reply_to_me (fields unchanged)
├── db/models.py                        # remove GroupMode and WorkChat.group_mode
├── telegram/control_bot/settings.py    # settings text/keyboard, stale chatgroup callback
├── telegram/control_bot/ownership.py   # keep "chatgroup" so stale callbacks are still ownership-checked
└── i18n/en.py, i18n/ru.py              # remove group_mode keys/buttons, new chat_group text, alert, setup sentence

alembic/versions/0009_drop_group_mode.py

tests/
├── unit/test_router.py, unit/test_user_client.py, unit/test_i18n_us34.py
└── integration/test_us1_pipeline.py, test_us4_account.py, test_us2_review_fixes.py,
    test_earlier_search_drafts.py, test_jira_models.py (migration 0009 round trip)

specs/001-telegram-reply-agent/spec.md, data-model.md, contracts/control-chat.md   # FR-010
```

**Structure Decision**: Existing single-project layout; no new modules.

## Risks

- **Migration number collision**: other open branches (007–011) may also add `0009`. Whoever
  merges second re-chains `down_revision` (as 0008 was re-chained); the migration has no data
  dependency on its neighbours.
- **Server `mentioned` semantics** are documented only as "mentions the current user"
  (core.telegram.org/api/mentions). We rely on it for tags only and keep the explicit reply
  check, so whether the server also flags replies does not matter. If the server flags
  forum-topic posts or forwards, R3's guards cover forwards; topic posts are covered for
  `is_reply_to_me` but not for a server-side `mentioned` flag. Quickstart scenario 6 checks
  this on a live account.
- **Deploy order**: old code fails on the dropped column; stop the app, run
  `alembic upgrade head`, start the new image (research R4).
- **Behaviour change for `always` chats is silent** (spec Assumption): users notice through
  the settings text only.
- **Tests that used `GroupMode.always` as a shortcut** (`test_us2_review_fixes.py`,
  `test_earlier_search_drafts.py`) must send addressed messages instead; their assertions
  about group behaviour must not weaken.

## Complexity Tracking

None.
