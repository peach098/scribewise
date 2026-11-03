# Implementation Plan: Add Work Chats After Setup

**Branch**: `007-add-work-chats` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/007-add-work-chats/spec.md` (issue #32)

## Summary

`/chats` gets a **➕ Add work chats** button (callback `chatadd:0`). It opens the setup chat
picker, moved out of `setup_flow.py` into a shared module `control_bot/chat_picker.py` and
parameterised by *purpose* (`setup` or `add`). In `add` mode the picker lists only the dialogs
that have no `work_chats` row yet, and **Done** upserts the selected chats as enabled work
chats. It writes one `activity_records(kind = chats_added)` row and shows "Added N chats" with
the refreshed `/chats` list. Then, in the same handler (aiogram runs handlers as tasks, so the
bot stays responsive), it runs the 🔄 Rebuild pipeline (`import_own_messages` →
`build_profiles_from_import`, interactive). It waits for the per-user `profile_build_lock`
instead of refusing, and skips the LLM build when the new chats hold none of the user's own
messages in the import window.

The intake already looks the work chat up in the database for every incoming message
(`intake/router.py`), so a chat is monitored from the commit of its row. Nothing in the
Telethon client or the scheduler changes.

There is one schema change: migration `0012_chats_added` adds the `activity_kind` value
`chats_added`. The public contract change is the `/chats` section of the 001 control-chat
contract.

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: aiogram 3 (FSM, routers, outer middleware), Telethon behind
`MessagingGateway` (unchanged), SQLAlchemy 2 async + asyncpg, Alembic. **No new dependencies.**

**Storage**: PostgreSQL 16. Migration `0012_chats_added`:
`ALTER TYPE activity_kind ADD VALUE 'chats_added'`. The value is added in an autocommit block,
the same pattern as 0008. `work_chats` is unchanged: the `(user_id, telegram_chat_id)` unique
constraint already exists, and `chats_repo.upsert_work_chat` uses it. See
[data-model.md](data-model.md).

**Testing**:
- **Unit**: pytest. Covers the picker keyboard and the exclusion filter, the history label and
  the i18n parity lint.
- **Integration**: the Postgres test container. Covers the bot flow with `offline_bot()`,
  `text_update` / `callback_update` from `tests/integration/bot_support.py`, and
  `FakeMessagingGateway` with a new `fail_list_dialogs` field. The scenarios are adding, the
  already-added case, a double Done, cancel and other commands, a stale picker, Telegram
  disconnected or unavailable, the profile update (success, nothing to learn, failure, waiting
  on a running rebuild), and intake of a message in the new chat.
- The setup flow tests (`test_us1_setup_flow.py`, `test_connect_flow.py`) must stay green
  without edits to their assertions. They prove that the picker refactor did not change setup.

**Target Platform**: Linux server, Docker Compose, single process (unchanged)

**Project Type**: single long-running service (asyncio worker + Telegram control bot)

**Performance Goals**:
- One `list_dialogs` call per ➕ tap, the same as setup.
- The profile update costs one rebuild: one import plus one synchronous global build and up to
  20 contact builds. It happens only when Done added chats that contain own messages.

**Constraints**:
- No message content in logs or activity records (Principle I/VI). The activity row stores
  work chat ids only, and titles are read at render time.
- No duplicate `work_chats` rows.
- The picker never changes data after it is closed.
- Setup behaviour is unchanged: it still requires at least one chat, then goes to the time
  zone step.

**Scale/Scope**: up to about 100 users with hundreds of dialogs each. The picker pages 8
dialogs at a time, as in setup.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan complies |
|---|---|---|
| I. Human-in-the-Loop | Pass | Adding a chat only lets drafts appear. Every send still needs approval. Nothing from before the add is drafted, because intake only sees new events. |
| II. Privacy & Data Minimization | Pass | Only chats the user picks become work chats. The style import reads only the user's own messages, in memory, as in setup and Rebuild (001 FR-035). The activity row holds ids, not titles or content. `/my_data` already counts work chats, and `/delete_my_data` removes the rows and the activity. |
| III. Grounded Replies | Pass | Draft generation is unchanged. |
| IV. Faithful Personal Style | Pass | The new chats feed the profiles through the existing Rebuild path. User rules and removed-contact handling are unchanged. |
| V. Evaluation-Driven Development | Pass | No prompt, model or retrieval change, so no evaluation run is needed. The profile build is the existing one. Deterministic code is covered by unit and integration tests written with the code. |
| VI. Simplicity & Observability | Pass | One shared picker module instead of a copy. The handler runs the rebuild itself, with no new task queue. Structured logs `chats_added` (counts only) and the existing `style_import_finished` / `profile_rebuild_*` lines. No new dependency. |
| Security (prompt injection, scopes) | Pass | Chat titles from Telegram are shown as plain button text and truncated. They are never interpreted. No new Telegram scopes. |

Post-design re-check: still passes. The design adds one enum value, one module and one
middleware, and needs no exceptions.

## Project Structure

### Documentation (this feature)

```text
specs/007-add-work-chats/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R10
├── data-model.md        # Phase 1: activity kind, FSM data, no table changes
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   └── control-chat.md  # Phase 1: /chats ➕ button, picker in add mode, texts
├── checklists/
│   └── requirements.md  # spec quality checklist
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/scribe/
├── telegram/control_bot/
│   ├── chat_picker.py        # NEW: shared picker (keyboard, show, toggle, page, done dispatch,
│   │                         #      stale-picker answer, abandon middleware)
│   ├── setup_flow.py         # picker code moved out; setup calls chat_picker with purpose=setup
│   ├── add_chats.py          # NEW: ➕ entry (chatadd:0), AddChatsStates, on-done for purpose=add,
│   │                         #      chats_added record, profile update after adding
│   ├── settings.py           # chats_command: ➕ button (also on empty list); _history_label for
│   │                         #   chats_added; include add_chats router; idle filter gains
│   │                         #   AddChatsStates
│   ├── profile_command.py    # rebuild body extracted to rebuild_profiles(...) used by
│   │                         #   rebuild_callback and add_chats
│   └── bot.py                # (only if needed) abandon middleware placement, see research R5
├── db/models.py              # ActivityKind.chats_added
├── i18n/en.py, i18n/ru.py    # new keys (contracts/control-chat.md)
└── telegram/fake_gateway.py  # fail_list_dialogs for tests
alembic/versions/0012_chats_added.py   # NEW
specs/001-telegram-reply-agent/spec.md, contracts/control-chat.md   # FR-024 / /chats updated
tests/
├── unit/test_chat_picker.py            # NEW
├── unit/test_i18n_lint.py              # existing, must pass with new keys
└── integration/test_add_chats_flow.py  # NEW
```

**Structure Decision**: single project, the existing layout. The add flow gets its own module
next to the other `/chats`-related code. `settings.py` is already 1500 lines, and a separate
module keeps the FSM states and handlers of the flow together. That matches how `skipped.py`,
`profile_command.py` and `tasks_command.py` are included by the settings router.

## Complexity Tracking

No constitution violations; nothing to justify.
