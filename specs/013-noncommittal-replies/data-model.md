# Data Model: Non-Committal Drafts, Informational Skips, Behaviour Rules

No new tables or columns. Extends [004 data-model](../004-skip-closing-messages/data-model.md)
and the user-rule shape in `src/scribe/style/profiles.py`. Rationale: [research.md](./research.md)
R5–R6.

## Enum changes (migration `0020_behaviour_rules`)

| Postgres enum | Python enum (`db/models.py`) | New value | Meaning |
|---|---|---|---|
| `skip_category` | `SkipCategory` | `informational` | the turn only informs and calls for no reaction (FR-007) |
| `skip_category` | `SkipCategory` | `user_rule` | a user rule decided the skip (FR-008a) |
| `decision_source` | `DecisionSource` | `rule` | a user rule decided the draft/skip (either direction) |

Values are added with `ALTER TYPE … ADD VALUE IF NOT EXISTS` outside a transaction block, as in
`0014`/`0019`; downgrade leaves them (Postgres cannot drop enum values).

## `conversation_turns` (unchanged columns, new combinations)

| reply_decision | decision_source | skip_category | When |
|---|---|---|---|
| skip | model | informational | classifier: informational, confident, habit below threshold |
| skip | rule | user_rule | classifier: `decided_by=user_rule`, `reply_needed=false`, confident |
| draft | rule | classifier's category or null | classifier: `decided_by=user_rule`, `reply_needed=true` |

Invariants:
- `skip_has_category` CHECK still holds: every skip carries a category.
- A `rule` skip is never overridden by the habit rule; a `model` informational skip is.
- The habit-rule count (`turns_repo.habit_replied_count`) keeps counting turns by stored
  category, so `informational` turns the user answered also push later informational turns to
  draft. Turns decided by a rule (`decision_source = rule`, skip or draft) are excluded from
  the habit count, so a rule never shapes the learned habit.

## Activity record

`ActivityKind.skipped` meta `{category, source, confidence}` gains `decided_by`
(`default`|`user_rule`). No message content (Principle II).

## User rules (shape unchanged)

`{"rules": [{"text": str, "source": "user"|"proposal", "key"?: str, "scope"?: "global"|"contact"}]}`
as produced by `AppliedRules.user_rules_json()`. New: the same effective rule set is sent to the
reply gate. A rule is a *behaviour rule* only by its meaning; nothing is stored to mark it.

## LLM request/response models (`llm/provider.py`)

- `MessageItem` (as used by the gate): new `has_media: bool = False`.
- `ReplyNeededRequest`: new field `user_rules: dict` (the effective `user_rules_json` for the
  turn's contact; `{}` when none), rendered as block `user_rules` with `trust="user"`.
- `DraftRequest`: `profile.user_rules_json` moves to a top-level `user_rules` field, rendered as
  its own block `user_rules` (`trust="user"`, `stable=True`, placed right after `profile` so the
  cache prefix order is kept).
- `Block`: new field `trust: Literal["untrusted","user"] = "untrusted"`.
- `ReplyNeededOutput`: `category` adds `informational`; new required `decided_by:
  Literal["default","user_rule"]`. See [contracts/reply-needed-output.schema.json](./contracts/reply-needed-output.schema.json).
- `DraftOutput`: unchanged.

## Settings (`config.py`)

| Setting | Change |
|---|---|
| `skip_candidate_max_words` | removed |
| `skip_candidate_max_chars` | new, default 2000 |
| `skip_min_confidence` | unchanged (0.8), also applies to rule skips |
