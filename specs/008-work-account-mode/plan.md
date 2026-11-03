# Implementation Plan: Work Account Mode — Every Private Chat Is a Work Chat

**Branch**: `008-work-account-mode` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/008-work-account-mode/spec.md` (issue #15)

## Summary

A per-user **work account mode** makes every eligible private chat (a one-to-one chat with a
real person; never bots, Saved Messages, service or deleted accounts, never groups) a work
chat, minus chats the user excludes. It is off by default, confirmed with a privacy
explanation, and switchable in the setup picker and `/settings`; `/chats` shows it, pages the
list and offers an exclusion picker.

Technical approach:

- **Materialised flag** (research R1). `work_chats.enabled` stays the single effective flag
  that intake, outgoing handling, the composer, earlier-conversation search, style import and
  refresh, and `/skipped` already read, so none of those readers change. Two new columns say
  why a chat is a work chat: `origin` (`picked` | `auto`) and `excluded`. For `auto` chats
  `enabled = users.work_account AND NOT excluded`, kept by one new module,
  `scribe/chats/work_account.py`.
- **Lazy pick-up** (R2, R3). The gateway reports a `peer_kind` for private chats. When intake
  or the outgoing handler sees a message in an eligible private chat with no `work_chats` row
  while the mode is on, it creates an `auto` row (under the existing per-chat lock and a
  `FOR SHARE` read of the user row) and continues the unchanged path. No row is created for
  dialogs that never write.
- **Switch** (R4, R8). Turning off commits the flag first, then deactivates `auto` chats one by
  one with the existing disable lock order (chat → pending drafts → turns; new dropped reason
  `work_account_off`). Turning on re-enables non-excluded `auto` chats in one update. An
  idempotent `reconcile_user` finishes a sweep interrupted by a crash.
- **Exclusion** (R5). "Exclude" on an `auto` chat sets `excluded`; on a `picked` chat it is the
  existing disable. Chats can be excluded before they ever wrote (an `auto` row with only id,
  title, kind).
- **Style** (R6). With the mode on, the import reads the enabled work chats plus the 50 most
  recent eligible private dialogs (configurable), never excluded chats or unpicked groups, and
  materialises imported chats with own messages as `auto` so they rank for contact profiles
  (#29). #30's sampling caps apply unchanged.
- **UI** (R7). The setup picker moves to a shared `chat_picker.py` (also needed by spec 007),
  gains the mode switch and exclusion toggles; `/settings` gains the state line, button and two
  confirmations; `/chats` gains a header, marks, paging, the exclusion picker and
  Exclude/Include labels; `/history` and `/my_data` gain entries (R9).

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: aiogram 3, Telethon, SQLAlchemy 2 async + asyncpg, Alembic,
pydantic-settings. **No new dependencies.**

**Storage**: PostgreSQL 16. One migration (next free number; `0009` on today's main, spec 006
may take it): enum `work_chat_origin`; `work_chats.origin`, `work_chats.excluded` + check
constraint; `users.work_account`, `users.work_account_changed_at`; new `activity_kind` and
`dropped_reason` values (added in autocommit first, as in 0008). See
[data-model.md](data-model.md).

**Testing**: pytest + pytest-asyncio. Unit: eligibility, Telethon `peer_kind` mapping, picker
keyboards, `decide()` unchanged. Integration: Postgres test container with real migrations,
`FakeMessagingGateway`, `FrozenClock`, `tests/integration/bot_support.py` for bot flows. See
[quickstart.md](quickstart.md).

**Target Platform**: Linux server, Docker Compose, single process (unchanged)

**Project Type**: single long-running service (asyncio worker + Telegram control bot)

**Performance Goals**: intake adds at most one indexed user-row read for messages from private
chats without a row (only to decide pick-up); no change for known chats. Turning the mode off
on an account with 200 picked-up chats completes in a few seconds (one short transaction per
chat). Setup import with the mode on reads at most 50 extra chats (same per-chat cost as
today).

**Constraints**:
- Nothing is stored from ineligible, excluded, paused or unpicked chats (spec FR-005, SC-003).
- A draft is never stored in a chat that was excluded or paused meanwhile (existing composer
  `FOR SHARE` check on the chat row).
- No message text or chat titles in logs (Principle II).
- Callback data ≤ 64 bytes; inline keyboards ≤ 100 buttons (hence `/chats` paging).

**Scale/Scope**: ~100 users; a work account may have hundreds of private dialogs; picked-up
chats grow with real correspondents only.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan complies |
|---|---|---|
| I. Human approval before sending | ✅ Pass | Nothing about sending changes. Picked-up chats produce drafts that need approval like any other; turning the mode off or excluding a chat drops pending drafts, never sends them. Mode changes, pick-ups and exclusions are recorded in `/history` and logs (attributable). |
| II. Privacy & data minimization | ✅ Pass (widening justified) | The mode widens what is read, by explicit user choice only: off by default, a confirmation spells out what will be read, groups/bots/service chats are never included, exclusions are available before a chat writes, chats are recorded lazily (no rows for silent dialogs), exclusions store no messages or contacts, style import is capped (50) and own-messages-only. Everything is visible (`/settings`, `/chats`, `/history`, `/my_data`) and deleted with `/delete_my_data`. Logs carry ids and counts only. |
| III. Grounded replies | ➖ N/A | No drafting change. Picking up on outgoing messages gives drafts *more* grounding (the user's opening message). |
| IV. Faithful personal style | ✅ Pass | Style is still learned only from the user's own messages; the mode widens the import to the user's real work chats, which improves fidelity. Profiles remain inspectable and editable. |
| V. Evaluation-driven development | ✅ Pass | No prompt, model or retrieval change, so the draft eval set is not a gate (research R11). All new code is deterministic and tested first (eligibility, invariant, pick-up, switch sweep, pickers, i18n). |
| VI. Simplicity & observability | ✅ Pass | No new dependency, service or job: one module (`scribe/chats/work_account.py`), two columns per table, readers untouched by materialising `enabled`. Structured logs `work_account_switched`, `work_chat_picked_up`, `work_chat_excluded/included`, `work_account_reconciled`. |
| Security: untrusted input | ✅ Pass | Eligibility comes from Telegram entity flags, not message content. A stranger who writes gets at most a draft for approval; message content still cannot change instructions (unchanged injection checks). Callbacks are ownership-checked; `wxpick` ids are accepted only if present in the picker's dialog list in FSM data. |
| Security: secrets / scopes | ✅ Pass | No new secrets; no new Telegram permissions (the user client already receives all updates). |

**Post-design re-check (after Phase 1)**: ✅ still passes. The denormalised `enabled` flag is a
deliberate simplicity trade-off (research R1) guarded by one module, a DB check constraint and
`reconcile_user`; recorded below.

## Project Structure

### Documentation (this feature)

```text
specs/008-work-account-mode/
├── spec.md
├── plan.md                    # This file
├── research.md                # Phase 0: decisions R1–R11
├── data-model.md              # Phase 1: columns, enums, invariant, state transitions
├── quickstart.md              # Phase 1: tests and manual validation
├── contracts/
│   ├── control-chat.md        # setup picker, /settings, /chats, exclusion picker, /history, /my_data
│   └── internal-interfaces.md # gateway fields, work_account module, intake/outgoing/style hooks
├── checklists/requirements.md
└── tasks.md                   # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
alembic/versions/
└── 00NN_work_account_mode.py          # NEW: enum, columns, check, enum values

src/scribe/
├── chats/                             # NEW package
│   ├── __init__.py
│   └── work_account.py                # is_eligible, pick_up, switch, preview_off, exclude_chat,
│                                      #   include_chat, deactivate_chat, import_targets,
│                                      #   materialise_imported, reconcile_user, counts
├── config.py                          # + work_account_import_max_chats (50)
├── db/models.py                       # + WorkChatOrigin, WorkChat.origin/excluded,
│                                      #   User.work_account/_changed_at, new enum values
├── db/repos/chats.py                  # list_work_chats(page/offset, include_auto), counts
├── telegram/gateway.py                # + PeerKind; IncomingMessage.peer_kind;
│                                      #   OutgoingEvent.chat_kind/peer_kind/chat_title
├── telegram/user_client.py            # fill peer_kind (in + out); dialog_kind drops deleted/service
├── telegram/fake_gateway.py           # accept new fields
├── intake/router.py                   # _handle: pick_up when no row (decide() unchanged)
├── intake/events.py                   # outgoing: pick_up when no row
├── style/importer.py                  # optional explicit targets
├── telegram/control_bot/chat_picker.py   # NEW (shared with spec 007): picker keyboard + state
├── telegram/control_bot/setup_flow.py    # pickwa switch, exclusions, Done rules, import targets
├── telegram/control_bot/settings.py      # /settings line + wacct confirmations; /chats header,
│                                         #   marks, paging, exclusion picker (ChatsStates),
│                                         #   Exclude/Include on chat screen; disable_chat →
│                                         #   work_account.deactivate_chat; /history labels
├── telegram/control_bot/profile_command.py  # Rebuild uses import targets + materialise
├── telegram/control_bot/review.py        # status text for work_account_off drops (if needed)
├── retention/user_data.py                # summary/export fields
├── i18n/en.py, i18n/ru.py                # new keys (contracts/control-chat.md)
└── __main__.py                           # startup reconcile for recently switched users

tests/
├── unit/test_work_account_eligibility.py     # NEW
├── unit/test_chat_picker.py                  # NEW
├── unit/test_user_client.py                  # extended
├── integration/test_work_account_intake.py   # NEW
├── integration/test_work_account_switch.py   # NEW
├── integration/test_work_account_bot.py      # NEW
└── integration/test_work_account_import.py   # NEW
```

**Structure Decision**: single project as before. The mode's rules live in one new module,
`scribe/chats/work_account.py`, with three seams into existing code: gateway (`peer_kind`),
intake/outgoing (`pick_up` when no row), and the control bot (picker, settings, chats). The
existing disable protocol moves into that module so exclusion, mode-off and plain disable share
one implementation.

## Delivery Order

1. **MVP (US1 + US2, P1)**: migration and models; gateway `peer_kind`; `is_eligible`,
   `pick_up`, `deactivate_chat`, `exclude_chat`/`include_chat`; intake and outgoing pick-up;
   setup picker switch and exclusions. Satisfies the issue's first three acceptance criteria
   from setup.
2. **US3 (P1)**: `switch`, `preview_off`, `reconcile_user`; `/settings` line, button and
   confirmations; `/chats` header. Satisfies "checkbox in /settings" and "turning it off goes
   back to the hand-picked list".
3. **US4 (P2)**: `/chats` marks, paging, mode-off summary, exclusion picker, chat-screen
   labels, `/history` entries, `/my_data`.
4. **US5 (P3)**: import targets and materialisation in setup and Rebuild.

## Coordination with in-flight work

- **Spec 006 (origin/006-group-mention-only)**: removes `GroupMode.always` and the group-mode
  buttons in `chat_settings_keyboard`. This feature touches the same screen only for private
  chats' enable row; rebase whichever lands second. Groups are never picked up here, so 006's
  intake rule is unaffected.
- **Spec 007 (issue #32)**: both need the picker outside setup. `chat_picker.py` is the shared
  module; with the mode on, 007's ➕ picker lists groups only (spec FR-011). 007 records
  `chat_added` with `origin: "picked"`; this feature uses the same activity kind with
  `origin: "auto"` — the migration that lands first adds the value, the other skips it
  (`ADD VALUE IF NOT EXISTS`).
- **Issues #29 / #30**: this feature changes only the import *target list*. If #29 has landed,
  materialised `auto` chats feed `build_profiles_from_import` ranking at setup; if not, they are
  ranked on the next 🔄 Rebuild. #30's group caps are untouched.
- **Migration numbers**: take the next free revision at implementation time.

## Risks

- **Denormalised `enabled`** for `auto` chats can drift if a writer bypasses the module →
  check constraint, all writes through `work_account.py`, invariant assertions in tests,
  `reconcile_user` after every switch and at startup.
- **Many picked-up chats on turn-off**: per-chat transactions bound lock time; cards for many
  dropped drafts are updated sequentially (rate-limited by the bot API) — acceptable at the
  expected scale.
- **Spam on a public work account**: strangers get drafts the user must reject; mitigated by
  one-tap exclusion. A future "only contacts in my address book" option is out of scope.
- **Telethon entity flags** (`deleted`, `support`) missing on cached minimal entities →
  conservative default (`peer_kind=None` is not eligible); unit tests pin the mapping.
- **Style import cost**: capped at 50 extra chats; the cap is a setting.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Denormalised effective flag (`work_chats.enabled` maintained for `auto` chats from `users.work_account` + `excluded`) | Keeps the ten existing readers of `enabled` and the composer lock protocol unchanged, so picked-up chats behave exactly like picked ones | A computed predicate at every reader changes ten queries and the lock protocol, with a higher risk of a missed site reading or ignoring the wrong chats |
