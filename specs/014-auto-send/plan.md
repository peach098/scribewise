# Implementation Plan: Auto-Send for Trusted Chats

**Branch**: `014-auto-send` | **Date**: 2026-11-04 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/014-auto-send/spec.md`

## Summary

An opt-in, per-chat auto-send mode. When a first-version draft in a chat with auto-send on
passes a fail-closed eligibility check (no hard stop, promise, sensitive flag, placeholder,
date mismatch, conflict, weak style, out-of-office or any other warning), the card is posted
with "will be sent automatically at HH:MM", and only once the card is in the bot chat is
`drafts.auto_send_due_at` set to the end of the user's cancel window (2 min default; 1, 2, 5,
10 or 15) counted from that moment. A draft whose card failed to post is never armed; a later
successful post (card retry, issue #15) arms it then. A new scheduler step picks due drafts with
`FOR UPDATE SKIP LOCKED`, re-checks eligibility and working hours, sets a new
`drafts.auto_approved_at` (never the user's `approved_by_user_at`) and sends through the
existing `send_approved_draft`. Any tap on the card, a new incoming message, pause, or turning
the mode off stops the countdown. A new content-free table `draft_decisions` records each
decision per chat; after each decision an advisor offers auto-send when a chat has ≥ 20
decisions in 30 days and > 60% sent unchanged, and suggests turning it off after > 4
interventions in the last 10 automatic drafts. No new dependency, no LLM change.

## Technical Context

**Language/Version**: Python 3.12 (unchanged)

**Primary Dependencies**: unchanged: aiogram 3 (control bot), Telethon (user clients, through
`MessagingGateway`), SQLAlchemy 2 async + Alembic, structlog. **No new dependency.**

**Storage**: PostgreSQL 16; migration `0021_auto_send` (research A10): columns on
`work_chats`, `users`, `drafts`; replaced `sent_requires_approval` check; new table
`draft_decisions`; new enums `auto_send_source`, `auto_send_offer_state`, `auto_send_stop`,
`draft_decision`; new `activity_kind` values. See [data-model.md](data-model.md).

**Testing**: pytest + pytest-asyncio. Unit: eligibility table (every `BlockReason`), rate
math, callback lengths, i18n parity. Integration (real Postgres, `FakeMessagingGateway`,
`FrozenClock`, `offline_bot`): arm → fire → sent; each stop path; tap-vs-step race;
constraint refusal; offer/snooze/opt-out; warn; work-chat removal; export.

**Target Platform**: Linux server (Docker), unchanged.

**Project Type**: single Python service (control bot + user clients + scheduler).

**Performance Goals**: an automatic send happens within one scheduler tick (5 s) after the
window ends; one extra indexed query per tick; rate query per decision on an index of
(`work_chat_id`, `at`).

**Constraints**: never send a draft with any block reason (SC-001); never send without an
explicit chat opt-in (SC-002); no automatic retry of a failed send; lock order draft → turn
(`send.py`); no live countdown edits (Telegram edit limits).

**Scale/Scope**: ≈ 30 beta users, tens of chats each; decisions ≈ hundreds per user per
month.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Human approval before sending | Opt-in per chat with an explicit confirmation (FR-003), off by default (`work_chats.auto_send` default false), scoped to one chat (no global on switch, FR-002), reversible in ≤ 2 taps including everywhere at once (FR-004). Every draft still passes the approve / edit / reject card, now with a cancel window that starts only once the card is posted (A4a; DB check `ck_drafts_auto_send_needs_card`). Hard stops are refused by a fail-closed check at arm and at fire time. Automatic sends are attributable: separate `auto_approved_at`, `auto_approved` activity, `sent.meta.auto`, and the DB check still refuses a sent row with no approval of either kind. | Pass |
| II. Privacy & data minimization | `draft_decisions` holds ids, a decision code and a timestamp; no text. New columns are flags and times. All in `/my_data`, cascade-deleted, decisions purged after 31 days. Logs carry ids and reason codes. | Pass |
| III. Grounded replies | Draft generation unchanged. Drafts that commit the user (promise, dates differ) or need details (placeholders) are never auto-sent, so the existing "extra attention" flags keep a human in the loop. | Pass |
| IV. Faithful style | Unchanged; weak-style drafts are never auto-sent. | Pass |
| V. Evaluation-driven | No prompt, model or retrieval change, so no evaluation run (research A11). Eligibility, step, races and advisor are deterministic code with tests written first. | Pass |
| VI. Simplicity & observability | Reuses the scheduler's `_draft_step` pattern, the existing send path and presenter. One new module, one new table, one repo. Structured events `auto_send_*`. | Pass |
| Security: untrusted input | Incoming text cannot arm, extend or fire a countdown: arming depends only on the user's chat setting and on deterministic flags; injection warnings block (FR-024). New callbacks go through `OwnershipMiddleware`. | Pass |

Note: spec 001's plan states "no scheduler job can send". This feature changes that on
purpose, under Principle I's opt-in clause; the `send.py` docstring and README ("Nothing is
sent without your approval") are updated in the same change.

Post-design re-check (after research, data model and contracts): still passes. No
exceptions, so Complexity Tracking stays empty.

## Project Structure

### Documentation (this feature)

```text
specs/014-auto-send/
├── spec.md
├── plan.md                         # this file
├── research.md                     # A1–A11
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── internal-interfaces.md      # eligibility, arm, step, send, stop, advisor
│   └── control-chat.md             # card, offer, confirmation, /chats, /settings, /history
├── checklists/requirements.md
└── tasks.md                        # /speckit-tasks
```

### Source Code (repository root)

```text
alembic/versions/
└── 0021_auto_send.py                   # NEW
src/scribe/
├── db/
│   ├── models.py                       # WorkChat/User/Draft columns, DraftDecision, enums, ActivityKind
│   └── repos/decisions.py              # NEW: record, chat_rate, recent_auto_outcomes, purge
├── sending/
│   ├── auto_send.py                    # NEW: eligibility, arm, AutoSender, stop_*, set_chat_auto_send, AutoSendAdvisor
│   └── send.py                         # auto approval, decision row, sent.meta.auto
├── scheduling/scheduler.py             # on_auto_sends/_step_auto_sends; DueTurnsProcessor arms before post_card
├── intake/router.py                    # _flag_pending_draft stops the countdown
├── chats/work_account.py               # clear auto_send on deactivate/exclude/switch off
├── retention/
│   ├── purge.py                        # purge draft_decisions
│   └── user_data.py                    # DraftDecision in PER_USER_TABLES and summary
├── telegram/control_bot/
│   ├── render.py                       # DraftCardView auto fields, armed keyboard
│   ├── review.py                       # stop on every action, on_autostop, too_late, decision rows, advisor calls
│   ├── settings.py                     # /settings line, window, off-all; /chats auto line and callbacks
│   └── auto_send_flow.py               # NEW: offer/confirm/warn callbacks (autooffer, autoconfirm, autowarn)
├── i18n/en.py, i18n/ru.py              # keys from contracts/control-chat.md
└── __main__.py                         # wire AutoSender, AutoSendAdvisor
tests/
├── unit/test_auto_send_eligibility.py          # NEW
├── unit/test_auto_send_rate.py                 # NEW
└── integration/
    ├── test_auto_send_flow.py                  # NEW: arm/fire/stop/race/failure/constraint
    ├── test_auto_send_offer.py                 # NEW: offer/snooze/opt-out/warn/daily cap
    └── test_auto_send_settings.py              # NEW: /chats, /settings, off-all, work chat removal, export
README.md, README.ru.md, docs/                  # describe the opt-in mode
```

**Structure Decision**: single project, existing layout. Auto-send logic sits in `sending/`
next to `send.py`, because it decides when the one send path runs; bot-facing flows go in a
new `control_bot/auto_send_flow.py` to keep `settings.py` (already ~1900 lines) from growing
further, with only the `/settings` and `/chats` lines added there.

## Key Design Decisions

0. **No countdown without a card** (A4a): arming happens in `post_card` after the card is
   stored; scheduler condition and DB check `ck_drafts_auto_send_needs_card` back it up.
1. **Separate automatic approval** (A1): `auto_approved_at` beside `approved_by_user_at`; the
   DB constraint still refuses any unapproved sent row.
2. **Countdown as a row timestamp** (A2) polled by the existing scheduler; restart-safe, no
   automatic retry of a failed send.
3. **One lock decides the race** (A3): every card action stops the countdown in its existing
   `FOR UPDATE` transaction; once fired, actions answer "already being sent".
4. **Fail-closed eligibility, checked twice** (A4): at arm (so the card is honest) and at fire
   (state may have changed). Only first-version drafts arm.
5. **Own decisions table** (A5–A6) for a cheap, content-free 30-day per-chat rate; "unchanged"
   uses the existing `edited_text IS NULL` definition.
6. **Advisor on the decision hook** (A7): offers and warnings are evaluated right after a
   decision commits, with leases written before posting.

## Risks

- **Migration clash** with `013-noncommittal-replies` (`0020_behaviour_rules`): this plan takes
  `0021`; renumber if 014 merges first.
- **Context warnings are common**: fail-closed blocking on any warning may make few drafts
  eligible. Measure via `auto_send_blocked` reasons in beta; extend
  `AUTO_SEND_ALLOWED_WARNINGS` deliberately, one code at a time, with a test.
- **Wrong send at scale of one**: a correct-looking draft that is still wrong goes out. The
  cancel window, the turn-off suggestion and the opt-in per chat are the mitigations the spec
  accepts.
- **Scheduler step ordering**: the auto-send step runs after due turns in the same tick, so a
  very short window cannot fire before its card is posted (arming happens before `post_card`
  but `due_at ≥ now + 60 s`).
- **Card retry is a separate change** (issue #15): until it lands, a draft whose card failed
  stays without a card as today; it is never auto-sent.
- **Group chats** reply with a quote (`reply_to_for`); unchanged, but worth one manual check
  (quickstart Q1 in a group).

## Complexity Tracking

No constitution violations.

## Phase Summary

- Phase 0 ([research.md](research.md)): A1–A11 and A4a resolved; no open NEEDS CLARIFICATION.
- Phase 1: [data-model.md](data-model.md), [contracts/internal-interfaces.md](contracts/internal-interfaces.md),
  [contracts/control-chat.md](contracts/control-chat.md), [quickstart.md](quickstart.md).
- Agent context: `.specify/scripts/bash/update-agent-context.sh` does not exist in this repo;
  skipped (research A10).
- Next: `/speckit-tasks`.
