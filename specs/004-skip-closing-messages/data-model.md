# Data Model: Skip Messages That Need No Reply

**Feature**: [spec.md](spec.md) | **Research**: [research.md](research.md) R5

Changes to the 001 schema ([001 data-model](../001-telegram-reply-agent/data-model.md)), all
in migration `alembic/versions/0008_skip_closing_messages.py`. No new tables.

## New enums

| Enum | Values |
|---|---|
| `reply_decision` | `draft`, `skip` |
| `skip_category` | `thanks`, `farewell`, `acknowledgment`, `forwarded` (issue #47: the turn held only forwarded messages; set without a model call, source `pregate`, whatever the skip setting; not a closing, so the habit rule R3 ignores it) |
| `decision_source` | `pregate` (not a candidate), `model` (classifier decided), `fallback` (classifier failed or low confidence → draft), `habit` (FR-005 → draft), `disabled` (skipping off → draft), `user` ("draft anyway") |

## Extended enums

| Enum | Added values |
|---|---|
| `turn_state` | `skipped` |
| `activity_kind` | `skipped`, `draft_anyway` |

## Changed tables

### conversation_turns

| Column | Type | Notes |
|---|---|---|
| reply_decision | reply_decision NULL | set by the reply gate; null until the gate runs |
| skip_category | skip_category NULL | what the classifier recognized; set whenever it returned a closing category, including turns then drafted because of `habit` or low confidence (input to R3); null for `pregate`, `disabled` and classifier failures. Never cleared by a later decision: a "draft anyway" decision keeps it (`COALESCE` on update). Also set on a turn a manual reply dropped before the gate decided it (`ReplyGate.classify_dropped`; `reply_decision` stays null) |
| decision_source | decision_source NULL | see enum |
| decision_confidence | numeric(3,2) NULL | classifier confidence, 0–1; null for `pregate`/`disabled`/`user` |
| decided_at | timestamptz NULL | when the gate ran |
| draft_anyway_at | timestamptz NULL | set by the `draftany` callback |

Index: `ix_turns_user_skipped (user_id, decided_at DESC) WHERE state = 'skipped'` for
`/skipped`. It must be created after `skipped` has been added to `turn_state` and committed:
the migration adds the enum values in an autocommit block (research R5).

Check: `reply_decision = 'skip' ⇒ skip_category IS NOT NULL`.

### users

| Column | Type | Notes |
|---|---|---|
| skip_closing | bool NOT NULL DEFAULT true | global switch (FR-014) |

### work_chats

| Column | Type | Notes |
|---|---|---|
| skip_closing | bool NULL | per-chat override; null = use `users.skip_closing` (FR-014) |

### drafts

| Column | Type | Notes |
|---|---|---|
| possibly_unnecessary | bool NOT NULL DEFAULT false | set when a later closing message resolves the request (FR-011); the card shows the 💤 banner. A closing message (gate `skip`) also clears the `outdated` flag intake set for it, unless the draft was outdated before or a newer incoming message exists |

### activity_records (meta only, never text)

| Kind | `meta_json` |
|---|---|
| `skipped` | `{category, source: "model", confidence}` |
| `draft_anyway` | `{skipped_at, category}` |

## State machine: conversation_turns.state (additions to 001)

```
reading ──(reply gate: skip)──▶ skipped           (message already marked read)
reading ──(reply gate: draft)──▶ … 001 path (compose → pending_review)
skipped ──("draft anyway", while available)──▶ waiting  (due_at = now, draft_anyway_at set)
waiting ──(due, draft_anyway_at set)──▶ reading ──▶ compose, gate bypassed
```

`skipped` is a resting state: it is never expired, reminded, or dropped. 001's manual-reply
drop (T047) touches only `waiting`, `reading` and `pending_review`. A skipped turn's messages follow the
30-day purge. Once they are gone, the turn no longer appears in `/skipped`, because there is
no preview and "draft anyway" would fail with `TurnNotDraftable`.

## Derived values (not stored)

- **Effective skip setting** for a turn: `work_chats.skip_closing` if not null, else
  `users.skip_closing`.
- **Draft anyway available** for a skipped turn: both of these must hold:
  - its messages still exist;
  - no outgoing message in the chat has `sent_at` after the turn's last message, and no later
    turn in the chat is `answered`.
- **Habit replied count** (R3): over the last 5 turns in the chat with
  `skip_category IS NOT NULL` within 30 days, the count answered by draft, by manual reply,
  or by an outgoing message within 30 min.

## Validation rules

- The reply gate writes its columns only while the turn is `reading`, with a guarded update
  in the same style as `DueTurnsProcessor._set_turn`.
- `draftany` updates only rows with `state = 'skipped'` that belong to the caller's `user_id`.
- `decision_confidence` lies between 0 and 1.
