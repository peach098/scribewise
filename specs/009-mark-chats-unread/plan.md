# Implementation Plan: Mark Chats Unread After Agent Activity

**Branch**: `009-mark-chats-unread` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/009-mark-chats-unread/spec.md` (GitHub issue #16)

## Summary

After the agent reads new messages in a work chat (scheduler read phase, or the read before
"Regenerate") and after an approved reply is sent, mark the chat unread in the user's Telegram
with `messages.markDialogUnread(unread=True)`, so the chat list again shows where something
happened. A per-chat **floor** (`work_chats.unread_mark_floor`, the newest message id the chat
was marked for or the user is known to have seen) makes the mark happen at most once per new
message or reply. It also skips messages the user has already seen: read on another device,
cleared the mark (new gateway event from `UpdateDialogUnreadMark(unread=False)`) or replied by
hand. The decision is taken **before** the agent's own read, so an echo of that read cannot
cancel the mark (research U4). A new `UnreadMarker` component owns all of it. It never raises,
never retries and never delays reading or sending by more than 5 s. A per-user toggle in
`/settings` (`users.mark_unread`, default on) controls it. No new dependency, no LLM change.

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: unchanged: Telethon 1.45 (user clients; `MarkDialogUnreadRequest`,
`UpdateDialogUnreadMark` verified present), aiogram 3, SQLAlchemy 2 async + Alembic, structlog.
**No new dependency.**

**Storage**: PostgreSQL 16; migration `0009_mark_unread`: `users.mark_unread` (bool, default
true) and `work_chats.unread_mark_floor` (bigint, nullable). No message content stored.

**Testing**: pytest + pytest-asyncio. Unit tests for the update mapping; contract tests for
`UserClient.mark_unread` and the raw handler against a faked Telethon client (as
`tests/contract/test_gateway_search.py` does); integration tests with `FakeMessagingGateway`
(new `mark_unread_calls`, `emit_unread_mark_cleared`) through `DueTurnsProcessor`,
`send_approved_draft`, `ChatEventHandler` and the `/settings` router.

**Target Platform**: Linux server (Docker), unchanged.

**Project Type**: single Python service (control bot + user clients + scheduler).

**Performance Goals**: the mark is visible within 10 s of the read or send (SC-001). One extra
MTProto request per agent read or send in a chat, nothing per incoming message.

**Constraints**: the mark must not delay reading or sending beyond a 5 s bound, and must not
retry (FR-004, SC-004). It must not run inside a draft or turn row lock (`send.py` lock order).
It must never read, draft or send (FR-008, Principle I).

**Scale/Scope**: ≈ 30 beta users; per-chat state is one integer.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Human approval before sending | The feature sends no message. The mark is set only after reads that already happen and after sends the user approved; `UnreadMarker` has no access to `send_text`. The mark is logged per chat (attributable). | Pass |
| II. Privacy & data minimization | Stores one boolean per user and one message id per work chat; no text. Logs carry ids and reasons only. Setting and floor are in `/my_data` and deleted with the user. | Pass |
| III. Grounded replies | Draft content unchanged. | N/A |
| IV. Faithful style | Unchanged. | N/A |
| V. Evaluation-driven | No change to prompts, models or retrieval, so no eval run. Deterministic code gets unit, contract and integration tests written first (tasks are test-first). | Pass |
| VI. Simplicity & observability | One small component, two columns, one gateway method and one gateway event. It sits behind the existing `MessagingGateway` protocol and its fake. Structured events `unread_mark_set/skipped/failed/seen`. | Pass |
| Security: untrusted input | Incoming message content plays no part in marking. Only update types and ids from the user's own session are used. | Pass |

Post-design re-check (after research, data model and contracts): still passes. No exceptions,
so Complexity Tracking stays empty.

## Project Structure

### Documentation (this feature)

```text
specs/009-mark-chats-unread/
├── spec.md
├── plan.md                         # this file
├── research.md                     # U1–U10
├── data-model.md                   # users.mark_unread, work_chats.unread_mark_floor
├── quickstart.md
├── contracts/
│   ├── internal-interfaces.md      # gateway additions, UnreadMarker, call sites
│   └── control-chat.md             # /settings line and toggle
├── checklists/requirements.md
└── tasks.md                        # /speckit-tasks
```

### Source Code (repository root)

```text
alembic/versions/
└── 0009_mark_unread.py                 # NEW: users.mark_unread, work_chats.unread_mark_floor
src/scribe/
├── db/models.py                        # User.mark_unread, WorkChat.unread_mark_floor
├── telegram/
│   ├── gateway.py                      # UnreadMarkClearedEvent, mark_unread, on_unread_mark_cleared
│   ├── user_client.py                  # mark_unread (no-sleep), UpdateDialogUnreadMark mapping
│   ├── client_pool.py                  # delegate + fan-out
│   ├── fake_gateway.py                 # mark_unread_calls, failures, emit_unread_mark_cleared
│   ├── unread_mark.py                  # NEW: UnreadMarker (prepare/commit/after_send/seen)
│   └── control_bot/
│       ├── settings.py                 # settings:mark_unread toggle, text + keyboard
│       └── review.py                   # _mark_turn_read prepare/commit; pass marker to send
├── scheduling/scheduler.py             # DueTurnsProcessor._read prepare/commit
├── sending/send.py                     # after_send after commit
├── intake/events.py                    # seen() from read elsewhere and manual replies
├── i18n/en.py, i18n/ru.py              # settings.mark_unread*, btn.mark_unread_*
└── __main__.py                         # wiring
tests/
├── unit/test_unread_mark_updates.py            # NEW: update -> event mapping
├── contract/test_gateway_mark_unread.py        # NEW: UserClient.mark_unread, raw handler
└── integration/
    ├── test_unread_mark.py                     # NEW: read/send/seen/loop/failure flows
    └── test_settings_mark_unread_toggle.py     # NEW: /settings toggle, export
```

**Structure Decision**: single project, existing layout. `UnreadMarker` lives in
`telegram/` next to `client_pool.py`: it is a Telegram-side effect with a little state, and
the scheduler, `send.py`, `review.py` and `intake/events.py` all depend on it the same way they
depend on the gateway.

## Key Design Decisions

1. **Mark after read and after send, never before** (research U2). Reading and sending may
   clear the mark on Telegram's side; ordering makes that irrelevant.
2. **Decide before the agent's read, commit after** (research U4): `prepare` checks
   `floor < n` without writing, the read happens, then `commit` moves the floor if it is still
   `<= n` and marks. An echo of the agent's own read moves the floor to `n` at most, which still
   passes. A genuine user read beyond `n`, or before the agent's turn, blocks the mark.
3. **Seen signals** (research U3): the existing `ReadEvent`, a new `UnreadMarkClearedEvent`
   (`UpdateDialogUnreadMark`, `unread=False`), and manual outgoing messages. They move the floor
   whatever the setting is, so turning the setting on later cannot mark seen messages.
4. **Best effort, bounded**: no flood sleep, a 5 s timeout, no retry, no user notice. The floor
   is committed before the call, so a failed mark is never retried by a later re-read.
5. **Outside locks**: `send.py` calls `after_send` after its transaction commits; the scheduler
   calls `prepare`/`commit` around `mark_read`, which already runs outside any transaction.
6. **No per-chat grouping in the scheduler**: two turns of one chat read in one batch may mark
   twice in a row. That is idempotent in Telegram and rare, so not worth the change.

## Risks

- **Server behavior not documented** (does `readHistory`/`sendMessage` clear `unread_mark`; does
  the issuing session receive its own `UpdateReadHistoryInbox`): the design is correct either
  way (U2, U4). Quickstart Q1–Q4 confirm it on a real account.
- **"Opened the chat" without any update**: if the user opens a marked chat in a client that
  does not clear the mark, Telegram sends nothing; the mark simply stays (no re-mark happens
  either). Acceptable.
- **Migration number clash** with spec 008 (issue #15) or a later 006 migration: renumber at
  merge (U9).
- **Interplay with 006 / 008**: they change which messages are read; this feature follows reads,
  so no code conflict is expected beyond `intake/` touch points (`events.py` only here).
- **Noise from closing messages** (004 skips): marked by design (spec Assumptions); the toggle is
  the escape hatch.

## Complexity Tracking

No constitution violations.

## Phase Summary

- Phase 0 (research.md): U1–U10 resolved; no open NEEDS CLARIFICATION.
- Phase 1: data-model.md, contracts/internal-interfaces.md, contracts/control-chat.md,
  quickstart.md.
- Agent context: `.specify/scripts/bash/update-agent-context.sh` does not exist in this repo and
  the plan template does not call it; skipped (research U10).
- Next: `/speckit-tasks`.
