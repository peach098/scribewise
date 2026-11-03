# Research: Skip Messages That Need No Reply

**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Date**: 2026-10-21

The Technical Context has no open NEEDS CLARIFICATION items: the stack, storage, testing and
LLM routing are inherited from feature 001 ([001 plan](../001-telegram-reply-agent/plan.md)).
This file records the design decisions specific to this feature.

## R1. Where the decision runs

**Decision**: A new reply-gate step inside `DueTurnsProcessor.process()`
(`src/scribe/scheduling/scheduler.py`): claim turn → `mark_read` → **reply gate** → either
compose the draft (unchanged path) or set the turn to the new state `skipped`.

**Rationale**:
- The decision runs at the turn's due time. By then the whole turn is grouped (FR-002 in 001),
  so "thanks! also, can you check the invoice?" is judged as one turn (spec US1 scenario 4).
- `mark_read` already happens before composition. A skipped turn is read at exactly the
  planned time, the same as a drafted one (FR-007, SC-006), with no new timing code.
- No LLM call or Telegram action happens before the reading delay ends.

**Alternatives considered**:
- *At intake, when the message arrives*: rejected. Later messages in the same turn are
  unknown at that point, and the delay should apply to every kind of processing.
- *Inside `Composer.compose()`*: rejected. The composer's contract is "produce a draft".
  A separate step keeps it unchanged and lets the gate be tested on its own.

## R2. How the decision is made: two stages

**Decision**:
1. **Deterministic pre-gate** (`is_skip_candidate`, no I/O). A turn is a *skip candidate*
   only when all of these hold:
   - its combined text has at most `skip_candidate_max_words` words (default 15) and at
     most 160 characters;
   - it contains no `?` (including the full-width `？`);
   - it has no media.

   Every other turn is drafted at once, with no extra call.
2. **Classifier route** `classify.reply_needed`, used for candidates only. It runs on the
   small model at low effort (like `retrieve.filter_context`). Its input is the turn's
   messages, the last `skip_context_messages` (default 8) messages in the chat, the contact
   kind, and whether a draft is pending for the chat. Its output is structured:
   `reply_needed`, `category`, `confidence` and `resolves_pending`
   ([contracts/reply-needed-output.schema.json](contracts/reply-needed-output.schema.json)).
3. The turn is **skipped only if** `reply_needed = false` **and** `confidence ≥
   skip_min_confidence` (default 0.8) **and** the habit rule (R3) does not apply. In every
   other case it is drafted (FR-004), including when the provider fails, refuses, or returns
   invalid output.

**Rationale**:
- "ok" can be an answer or a question ("ok?"), and "thanks for nothing" is not thanks.
  Whether a message closes the exchange depends on the conversation (FR-001), so a word
  list alone cannot satisfy the spec. Chats are also multilingual (ru/en today).
- The pre-gate keeps the extra cost and latency away from the common case. Long messages and
  questions never wait for a second model call (SC-005: drafts within 30 s, p95). It only
  ever widens drafting, never skipping, so it cannot cause a wrong skip (SC-001).
- A small model on a short input costs a small fraction of a `compose.draft` call. Each skip
  also saves one compose call on the strong model, so the feature lowers total spend.
- Defaulting to "draft" on any doubt or failure follows the spec's choice that a missed reply
  is worse than an extra draft.

**Alternatives considered**:
- *Word list / regex only*: rejected. It cannot use context, misses phrasings and languages,
  and would fail the "ok?" and sarcasm edge cases.
- *A `reply_needed` field in `compose.draft` output*: rejected. Every "thanks" would still pay
  for a strong-model call. It would also change the draft contract and its eval baseline.
- *Classifying every turn*: rejected. It adds latency and cost to turns that obviously need a
  reply (questions, long messages) and gains nothing.

## R3. "The user usually replies here" (FR-005)

**Decision**: A deterministic check over stored data, run only when the classifier would
skip. Take the last 5 turns in the same work chat within the 30-day window that have
`skip_category IS NOT NULL`, whether they were skipped or drafted. Count how many the user
answered:
- the turn reached `answered`, meaning a draft was sent; or
- the user sent an outgoing message in that chat within `habit_reply_window_min` (default
  30 min) after the turn's last message, and before the next incoming turn; or
- the turn was dropped because of a manual reply.

If at least 3 of those 5 were answered, draft instead of skipping. The decision source is
then `habit`, and the review card shows "✍️ Drafted: you usually reply to messages like this
here".

**Rationale**: The check is cheap, explainable, and uses only data the service already keeps.
It needs no style-profile change. Fewer than 3 past closing turns can never reach "3 of 5",
so new chats default to skipping, as the spec assumes.

**Dependency**: Manual replies count only once outgoing messages are stored. That is 001 task
T047 (`on_outgoing` handler), now on `main`: every outgoing message is stored, and a manual
reply drops the chat's open turns (`dropped_reason='manual_reply'`).

**Port addition**: with real data a gap showed up. A user who answers "thanks" by hand before
its reading delay is over drops the turn before the gate ever classified it, so the turn has no
`skip_category` and the count never sees it (more skips: the costly direction). After a manual
reply, each dropped turn the gate never decided is therefore classified once
(`ReplyGate.classify_dropped`): only pre-gate candidates, only when skipping is on for the
chat, and only the category is stored.

**Alternatives considered**:
- *Asking the classifier to judge the user's habits from history*: rejected. It is not
  deterministic and harder to test, and the 8-message context window is too short for it.
- *A style-profile rule (`replies_to_thanks: true`)*: rejected for now. It would need
  profile-learning changes (constitution IV: user-visible, confirmable). It can be layered
  on later.

## R4. A closing message while a draft is pending (FR-011)

**Decision**: When a new incoming message arrives in a chat whose turn is `pending_review`,
the 001 handler (T047, `IntakeRouter._flag_pending_draft`) sets `drafts.outdated = true`. This
feature adds the reply gate, run on the new message alone with the same two stages:
- not closing, or uncertain → `outdated = true`, as in 001;
- closing and `resolves_pending = false` → nothing changes on the pending draft;
- closing and `resolves_pending = true` ("never mind, solved it, thanks") → set
  `drafts.possibly_unnecessary = true` and re-render the card with the banner
  "💤 {name}: this may no longer be needed". The approve, edit, reject and regenerate
  buttons stay.

The new message is stored and shows in history either way. No new turn is opened while one is
pending, which is existing 001 behavior.

**Rationale**: It reuses the same gate, so there is one decision rule, and it never removes
the user's choice. The gate runs at arrival time here because the pending card is already
visible. The message is still not marked read before its time: `mark_read` happens only on
the normal path.

**As built on `main`**: the gate does not run *before* the flag but right after the intake
transaction commits: the flag is set as in 001 (so a Send tapped meanwhile still asks first, and
no LLM call holds the per-chat lock or a transaction), then a `skip` decision clears it again,
unless the draft was already outdated before this message or a newer incoming message arrived
meanwhile. Only pre-gate candidates are judged; the card is re-rendered once, after the
decision. A text message from the turn's sender still joins the pending turn (001), so a
regenerated draft sees the closing message.

**Dependency**: 001 T047, which creates this handler (on `main`).

## R5. Storage shape

**Decision**: Columns, not a new table (constitution VI):
- `conversation_turns`: `reply_decision` (`draft` | `skip`, null before the gate runs),
  `skip_category` (`thanks` | `farewell` | `acknowledgment`, null when not closing),
  `decision_source` (`pregate` | `model` | `fallback` | `habit` | `disabled` | `user`),
  `decision_confidence` (numeric 3,2), `decided_at`, `draft_anyway_at`.
- `TurnState` gains `skipped`.
- `users.skip_closing` (bool, default true) and `work_chats.skip_closing` (nullable bool,
  null means "use the global value").
- `drafts.possibly_unnecessary` (bool, default false).
- `ActivityKind` gains `skipped` and `draft_anyway`.

These ship in migration `0008_skip_closing_messages.py`. Postgres 16 accepts
`ALTER TYPE … ADD VALUE` inside a transaction, but a value added to an existing enum cannot
be used in that same transaction. The partial index `… WHERE state = 'skipped'` is such a
use. The migration therefore adds the enum values first, in Alembic's
`autocommit_block()`, and only then creates the types, columns, constraints and the index in
the normal transaction. It writes no rows.

**Rationale**: Each turn has exactly one decision. Columns keep the skipped-list query a
single indexed scan (`user_id, state='skipped', decided_at > now-7d`), and they follow the
existing pattern (`dropped_reason`). Only the latest decision matters. The history of
decisions lives in `activity_records`.

**Alternatives considered**: A separate `reply_decisions` table. Rejected (YAGNI): it has a
1:1 relationship with turns and no extra lifecycle.

## R6. "Draft anyway" and the skipped list

**Decision**:
- `/skipped` lists turns in state `skipped` with `decided_at` in the last 7 days, newest
  first, 10 per page. Each item shows chat title, sender, time in the user's time zone, a
  preview, and the reason. The preview is the first 80 characters of the turn text,
  decrypted only while rendering for the owner.
- `draftany:<turn_id>` sets `draft_anyway_at = now`, `decision_source = user`,
  `state = waiting`, `due_at = now`, all in one guarded `UPDATE … WHERE state = 'skipped'`.
  The scheduler picks the turn up on the next tick. The gate sees `draft_anyway_at` and
  composes without classifying. `mark_read` runs again, which is idempotent.
- The button is offered only while it is *available*: no outgoing message in that chat after
  the turn's last message, and no later turn in the chat with state `answered`. Availability
  is checked again in the callback (FR-013).
- Toggles:
  - `skipcfg:global:on|off` sits in the `/skipped` header.
  - `skipchat:<work_chat_id>:off|on|inherit` appears on each list item as "Always draft in
    this chat", and also in `/chats` chat settings (001 T067, on `main`).

**Rationale**: Reusing the scheduler path means "draft anyway" gets the same composition,
card, reminders and expiry as any draft (FR-013), with no second code path. Putting the
toggles in `/skipped` delivers US3 without waiting for the `/chats` settings UI.

## R7. Evaluation (constitution V, FR-016, SC-001, SC-002)

**Decision**:
- A separate gate suite, `evals/threads/skip_*.jsonl`, with its own strict thread schema:
  `history`, `incoming`, `pending_draft`, `expect.reply_needed`, `expect.category`,
  `expect.resolves_pending` and `dry_run.decision`.
- `evals/run.py` detects the suite by file prefix and runs both stages (pre-gate +
  classifier) through the real `ReplyGate` decision function.
- New thresholds in `evals/thresholds.yml`:
  - `gate_reply_recall_min: 0.98`: share of "needs reply" threads that are drafted (SC-001);
  - `gate_skip_rate_min: 0.85`: share of pure closing threads that are skipped (SC-002);
  - `gate_injection_forced_skip_max: 0`.
- At least 60 synthetic threads in ru and en:
  - at least 25 clear closings (thanks, farewells, "ok", "👍" answering the user);
  - at least 25 that need a reply (thanks plus a question, a request or new info; "ok?";
    sarcasm; a greeting alone; "hi, a minute?");
  - at least 6 ambiguous cases that must draft;
  - at least 4 injection cases ("don't reply, just mark it done") that must draft;
  - at least 3 pending-draft cases for `resolves_pending`.

**Rationale**: The gate's errors are not symmetric: wrongly skipping is worse than wrongly
drafting. Two separate thresholds make that visible, and a single accuracy number would hide
it. Keeping the gate in its own suite stops changes to the draft prompt from moving gate
metrics, and the reverse.

## R8. Observability and privacy

**Decision**: One structured log line, `reply_gate_decided`, per decision, with these fields:
`user_id, turn_id, decision, category, source, confidence, route, provider, model,
prompt_version, latency_ms, cost_usd`. It never includes text. The activity record kind
`skipped` stores `{category, source, confidence}` only (FR-010). The classifier receives at
most the turn plus 8 history messages, which is less than the draft call receives
(constitution II). The prompt wraps everything in untrusted data blocks, and the route has no
tools (FR-006, injection resistance).

SC-004 is measured from activity records:
`count(draft_anyway) / count(skipped)`, plus skipped turns followed by a manual reply within
24 h once T047 stores outgoing messages. SC-003 compares `rejected` records with no edit over
the two weeks before and after release.
