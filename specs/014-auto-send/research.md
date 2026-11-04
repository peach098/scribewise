# Research: Auto-Send for Trusted Chats

**Feature**: `specs/014-auto-send` | **Date**: 2026-11-04

Each item: Decision / Rationale / Alternatives considered. Code references are to `main` at
`81f2db3`.

## A1. How an automatic send is approved without faking the user's approval

**Decision**: Keep `drafts.approved_by_user_at` meaning exactly "the user tapped Send". Add
`drafts.auto_approved_at`, set only by the auto-send path, and widen the check constraint
`sent_requires_approval` to
`state <> 'sent' OR approved_by_user_at IS NOT NULL OR auto_approved_at IS NOT NULL`, plus
`ck_drafts_auto_approved_needs_window`: `auto_approved_at IS NULL OR auto_send_due_at IS NOT NULL`
(an auto approval is only possible for a draft that was shown with a cancel window).
`send.py:_check_sendable` accepts either approval.

**Rationale**: Principle I needs every send to be attributable. Writing the user's approval
timestamp for something the user did not tap would make `/history` and the audit trail lie.
A separate column keeps the two cases distinguishable (FR-011) and the database still refuses
a `sent` row with neither approval.

**Alternatives considered**: reuse `approved_by_user_at` with an `auto` flag in activity meta
(loses the distinction in the row itself, easy to misread); drop the constraint (removes the
last structural guard of Principle I).

## A2. Where the countdown lives and who fires it

**Decision**: A timestamp column `drafts.auto_send_due_at` plus `drafts.auto_send_stop`
(enum, NULL while the countdown runs). A new scheduler step `_step_auto_sends` selects pending
drafts with `auto_send_due_at <= now AND auto_send_stop IS NULL AND auto_approved_at IS NULL
AND NOT outdated AND control_message_id IS NOT NULL` (A4a), `FOR UPDATE SKIP LOCKED`, through the existing `_draft_step` helper
(`scheduling/scheduler.py:412`). Its handler re-checks eligibility (A4) under the lock and
either sets `auto_approved_at` and returns an `AfterCommit` that calls `send_approved_draft`,
or sets `auto_send_stop` with the reason and returns an `AfterCommit` that re-renders the card
as an ordinary pending card.

**Rationale**: Same pattern as `remind_at` / `expires_at`: survives restarts, needs no new
framework, and the 5 s tick is precise enough for a 1–15 minute window. Setting
`auto_approved_at` in the claiming transaction removes the row from the step's selection, so a
failed send is never retried automatically (spec edge case).

**Alternatives considered**: an in-memory `asyncio` timer per card (lost on restart, would send
late or never); a named `Every` job (wrong granularity, needs its own locking).

## A3. Race between the countdown and a user tap

**Decision**: Every card action (`approve`, `edit`, `reject`, `regen`, `fill`, the new
`autostop`) sets `auto_send_stop = 'user_action'` inside the transaction that already locks the
draft (`FOR UPDATE`). The auto-send step locks the same row, so exactly one of them wins:

- user first: the step no longer selects the draft;
- step first (`auto_approved_at` set): `autostop`/`edit`/`reject` see the draft is being sent
  and answer `autosend.too_late`; `approve` (Send now) is a no-op that answers the same.

`on_edit` only opens the FSM today; it gains a short locked write that stops the countdown
before asking for the text (FR-008).

A tap that stops a running countdown also re-renders the card as an ordinary one (`stopped`
line, no ⏹) so an abandoned edit or a failed regenerate never leaves a card that promises an
automatic send.

**Stale claims**: the step commits `auto_approved_at` and the send runs after the commit. If the
process stops in between (restart, shutdown cancelling the task), nothing would ever set
`auto_send_stop` and every tap would answer `autosend.too_late`. The same scheduler step
therefore also receives drafts with `auto_approved_at` set and no stop; once the claim is older
than `FIRING_STALE_AFTER` (2 minutes) the handler sets `auto_send_stop = send_failed`, records
`auto_send_stopped` and re-renders the card as an ordinary one. It never retries the send; the
user's Send reuses `send_random_id` (Telegram deduplicates). A send in flight holds the draft
row lock, which the step skips (`SKIP LOCKED`). `AlreadyDelivered` on the automatic path ends
the countdown the same way (`send_failed`, key kept) and the card says it was already delivered.

**Rationale**: Reuses the existing lock order (draft, then turn, `send.py` docstring) and adds
no new lock.

**Alternatives considered**: optimistic `UPDATE … WHERE auto_send_stop IS NULL` without lock in
the step (fine for the step, but the send path locks anyway, so the lock costs nothing).

## A4. Eligibility rule (FR-005, FR-012, SC-001)

**Decision**: One pure function `auto_send.eligibility(...) -> Eligibility` in
`src/scribe/sending/auto_send.py`, called twice: when the card is first posted (to arm the
countdown) and when the countdown ends (to fire). It is **fail closed**: it returns blocked
reasons for

| Reason code | Source |
|---|---|
| `hard_stop:<code>` | `injection.hard_stops(system_warnings)` (injection, action claim) |
| `commitment` | `output.creates_commitment` |
| `sensitive` | `output.sensitive` |
| `placeholders` | `output.needs_info` or `PLACEHOLDER` in the text |
| `dates_differ` | any `system_warnings` item starting with `date_mismatch:` |
| `conflict` | `output.conflicts` non-empty |
| `weak_style` | `weak_style_profile` in `system_warnings` |
| `out_of_office` | `out_of_office` in `system_warnings` or `users.state != active` |
| `warning:<code>` | any other system or context warning not in `AUTO_SEND_ALLOWED_WARNINGS` (empty in this release) |
| `no_output` | `draft_output_of` returned `None` |
| `outdated` | `draft.outdated` |
| `possibly_unnecessary` | `draft.possibly_unnecessary` (the reply gate thinks no reply is needed) |
| `chat_off` | `work_chats.auto_send` false or chat disabled/excluded |
| `outside_hours` | `next_allowed(due) != due` (only at fire time) |
| `suspended` | `users.access_suspended_at` set |
| `not_first_version` | `draft.version > 1` or a placeholder-filled version (FR-008: the user asked for it and is present) |

**Rationale**: an allow-list of warnings means a warning added later cannot silently become
auto-sendable; the constitution and `injection.py` docstring name `hard_stops` as the seam.
The same function at both points means the card cannot promise a send the step would refuse,
except for state that changed in between (which then reverts the card, FR-012).

**Alternatives considered**: deny-list of known warnings (unsafe by default); check only at
fire time (card could not show the countdown honestly).

## A4a. The countdown starts only after the card is in the control chat (FR-006)

**Decision**: Arming is split in two and lives in `DraftPresenter.post_card`, whoever calls it
(`DueTurnsProcessor` now, the card retry of issue #15 later):

1. `plan_arm` (before posting, no write): arm-time eligibility and `planned = now + window`.
   The card is posted with `card.auto_send_at = planned`, or with `card.auto_send_manual` and
   the reasons.
2. `commit_arm` (in the transaction that stores `control_message_id`): locks the draft,
   re-checks eligibility and sets `auto_send_due_at = max(planned, stored_at + window)`.

If posting fails, nothing is armed: the draft stays an ordinary pending draft (as today after
`draft_card_failed`). When a later post of the same draft succeeds, the same two steps run at
that moment, so the countdown starts then, with the full window counted from the moment the
card appeared. If `commit_arm` fails or finds the draft ineligible after a successful post,
the card is re-rendered with `show_manual` (best effort) and `auto_send_arm_failed` /
`auto_send_blocked phase=arm` is logged; a card that still shows a countdown then waits for
an ordinary Send (safe direction).

Guards: the step selects only drafts with `control_message_id IS NOT NULL`, and the DB check
`ck_drafts_auto_send_needs_card` (`auto_send_due_at IS NULL OR control_message_id IS NOT NULL`)
makes an armed draft without a card impossible.

**Rationale**: Principle I and FR-006: a draft the user has not seen must never leave on its
own; the user always gets the full window from the moment the card appears, and the send is
never earlier than the time printed on the card.

**Alternatives considered**: arm before posting (a failed post leaves an armed, invisible
draft); post without the countdown line and add it by editing after arming (a failed edit
leaves an armed draft whose card does not say so).

## A5. Statistics source (FR-013, FR-014)

**Decision**: New table `draft_decisions`, one row per user decision on a draft, written in
the same transaction as the decision: `approve` (unchanged or edited), `reject`, `regen`, the
auto-send step (`auto_sent`), and `autostop`. Columns: `user_id`, `work_chat_id`, `draft_id`
(SET NULL), `decision`, `during_auto_send` (bool), `at`. Rate query:

```sql
count(*) FILTER (WHERE decision = 'sent_unchanged') / count(*)
WHERE work_chat_id = :c AND at > now() - 30 days AND decision <> 'auto_sent'
```

`auto_cancelled` rows are not decisions for the rate (the follow-up approve/reject on the same
draft is). Rows older than 31 days are deleted by the existing `purge_messages` job.

**Rationale**: `activity_records` has no chat column (join through drafts and turns, which the
purge and `SET NULL` may break) and its `meta_json` is not indexed. A narrow table makes the
rate one indexed query and holds no content (Principle II).

**Alternatives considered**: derive from `activity_records` (fragile joins); counters on
`work_chats` (cannot express a rolling 30-day window).

## A6. What counts as "sent unchanged"

**Decision**: `sent_unchanged` iff the send has `edited_text IS NULL`, exactly as today's
`ActivityKind.sent.meta.edited` (`send.py:247`). A filled placeholder counts as edited (it
stores `edited_text`, `placeholders.py`). Regenerate is one `regenerated` decision for the old
version; the new version gets its own decision. Expired and dropped drafts write no row.

**Rationale**: matches the spec Assumptions (rejections and regenerations count against) and
reuses the existing definition so the two never disagree.

## A7. When the offer and the "turn off" suggestion are evaluated (FR-015–FR-020)

**Decision**: `AutoSendAdvisor.after_decision(user_id, work_chat_id)` is called after the
decision's transaction commits (same place `_propose_rules` / `marker.after_send` run). It
never raises. It evaluates, under `SELECT … FOR UPDATE` of the work chat:

- offer: auto-send off, `auto_send_offer` not `opted_out`, `auto_send_offer_not_before` NULL or
  past, ≥ 20 counted decisions, rate > 0.60, and `users.auto_send_last_offer_at` older than
  24 h (FR-018). It sets `auto_send_offer_not_before = now + 30 d` and
  `users.auto_send_last_offer_at = now` before posting, so a crash cannot repeat it.
- suggestion to turn off: auto-send on, at least 10 drafts with `during_auto_send` in the
  chat, and among the last 10 the interventions (a draft with an `auto_cancelled`,
  `sent_edited`, `rejected` or `regenerated` row) > 4, `auto_send_warned_at` older than 7
  days. A Send now without changes (`sent_unchanged`) and `auto_sent` are not interventions.

A chat that qualifies only because of decisions made before a later dip still needs the next
decision to trigger; no periodic scan.

**Rationale**: the spec ties the offer to "the next decision"; a hook is simpler and testable
without the scheduler.

**Alternatives considered**: a daily job scanning all chats (delayed, more queries, harder to
test).

## A8. Card rendering of the countdown

**Decision**: The card shows a static line "⏱ Will be sent automatically at 14:32" (time in the
user's zone, `format_when`) and is not edited every second. Keyboard: ✅ Send now
(`approve:<id>`), ✏️ Edit, ⏹ Don't send automatically (`autostop:<id>`), 🔁, ❌, 🧾. A draft that is
not eligible in an auto-send chat shows "Needs your approval: <reasons>" (FR-012).

**Rationale**: Telegram limits message edits; a live counter adds load and nothing a clock
time does not give.

## A9. Pause, out-of-office, work chat removal, data deletion

**Decision**:
- `/pause` and `/ooo`: the fire-time check (A4 `out_of_office`) stops running countdowns; no
  scan needed because the step reads `users.state` under the lock.
- Turning auto-send off for a chat, or for all chats: one `UPDATE drafts SET auto_send_stop =
  'disabled' WHERE pending AND auto_send_stop IS NULL AND auto_approved_at IS NULL` for the
  chat(s), then `show_outdated`-style re-render of each card without the countdown.
- `work_account.deactivate_chat` / `exclude_chat` / `switch(off)`: set `work_chats.auto_send =
  false` in the same transaction (FR-021); pending drafts are already dropped there.
- `/delete_my_data`: cascades (`_user_fk`). `/my_data`: add `DraftDecision` to
  `PER_USER_TABLES`; new `users` and `work_chats` columns are exported already.

## A10. Migration number and agent context

**Decision**: `0021_auto_send` revising `0020`. Branch `013-noncommittal-replies` adds
`0020_behaviour_rules`; if 014 merges first, renumber to `0020` at merge. There is no
`update-agent-context.sh` in `.specify/scripts/bash/`; skipped, as in 009.

## A11. Evaluation impact (Principle V)

**Decision**: No change to prompts, models or retrieval, so no evaluation run is required.
SC-001 is covered by a table-driven unit test over every reason in A4 and an integration test
per hard-stop type through the real composer output (`draft_output` helper in
`tests/integration/us1_support.py`).
