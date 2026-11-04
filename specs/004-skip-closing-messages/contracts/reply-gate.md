# Contract: Reply Gate (internal interface)

Extends [001 internal interfaces](../../001-telegram-reply-agent/contracts/internal-interfaces.md).
Design rationale: [research.md](../research.md) R1–R4.

## Module `src/scribe/drafting/reply_gate.py`

```python
SkipCategory = Literal["thanks", "farewell", "acknowledgment"]
DecisionSource = Literal["pregate", "model", "fallback", "habit", "disabled", "user"]


@dataclass(frozen=True)
class GateDecision:
    decision: Literal["draft", "skip"]
    source: DecisionSource
    category: SkipCategory | None  # set whenever the classifier recognised a closing
    confidence: float | None
    resolves_pending: bool = False
    routed: RoutedResult[ReplyNeededOutput] | None = None  # usage/cost for logging


def is_skip_candidate(texts: Sequence[str], *, has_media: bool, max_words: int) -> bool:
    """Pure pre-gate: ≤ max_words words, ≤ 160 chars, no '?' / '？', no media."""


async def decide(
    router: LLMRouter,
    req: ReplyNeededRequest,
    *,
    enabled: bool,  # effective skip setting for the chat
    draft_anyway: bool,  # turn.draft_anyway_at is set
    habit_replied: Callable[[], Awaitable[int]],  # async, lazily evaluated R3 count
    min_confidence: float,
    max_words: int,
    has_media: bool = False,  # any turn message has media (pre-gate input)
    habit_min_replies: int = 3,  # settings.skip_habit_min_replies
) -> GateDecision:
    """Database-free core used by ReplyGate and evals/run.py. Order:
    draft_anyway → ("draft","user"); not enabled → ("draft","disabled");
    not candidate → ("draft","pregate"); classifier error → ("draft","fallback");
    reply_needed or confidence < min → ("draft","fallback"|"model");
    habit_replied() >= habit_min_replies → ("draft","habit"); else ("skip","model").
    reply_needed=false with category "none" → ("draft","fallback")."""


async def no_habit() -> int:
    """habit_replied for callers where the habit rule does not apply (evals, pending check)."""
    return 0


class ReplyGate:
    async def decide_for_turn(self, user_id: UUID, turn_id: UUID) -> GateDecision: ...
    async def decide_for_pending(self, user_id: UUID, message_id: UUID) -> GateDecision: ...
    async def classify_dropped(self, user_id: UUID, turn_id: UUID) -> None:
        """Habit-rule input: a closing turn a manual reply dropped before the gate saw it gets
        the classifier's category (pre-gate candidates, skipping on). Stores nothing else."""

    async def record_skip(self, user_id: UUID, turn_id: UUID, decision: GateDecision) -> bool:
        """Guarded update (state='reading' → 'skipped', decision columns) + activity record
        `skipped`, one transaction. Returns False if the turn left 'reading' meanwhile."""
```

Invariants:
- `decide` never returns `skip` when any of these hold: the classifier raised
  `ProviderError`, `reply_needed` is true, `confidence < min_confidence`, `enabled` is
  false, or `draft_anyway` is true (FR-004, FR-013, FR-015).
- `decide` never sends, never calls `mark_read`, and never writes to the database.
- Nothing in incoming text is interpreted outside the classifier's data blocks (FR-006).

## Scheduler integration (`DueTurnsProcessor._compose`)

```
claim → mark_read → set read_at            (read phase; never waits for an LLM)
      → compose phase, compose_concurrency at a time:
      → gate = ReplyGate.decide_for_turn(turn)
      → gate.decision == "skip": record_skip(); done (no card, no notification — FR-008)
      → else: store decision columns on the turn; composer.compose(turn) → post_card
```

- When `source == "habit"`, the card shows "✍️ Drafted: you usually reply to messages like
  this here" (FR-005). This is a system warning `drafted_by_habit` in
  `drafts.output_json.system_warnings`.
- A gate exception that is not a `ProviderError` is logged and treated as `fallback`, so the
  turn is drafted. The gate never moves a turn to `handle_manually` or back to `waiting`.

## Pending-draft integration (`IntakeRouter`, 001 T047's incoming-during-`pending_review` path)

```
new message stored; pending draft flagged outdated (001 behaviour, in the intake transaction)
commit, chat lock released
not a pre-gate candidate (or no gate)        → card re-rendered with the outdated banner
gate = ReplyGate.decide_for_pending(message)
  decision == "draft" / any failure          → stays outdated
  decision == "skip"                         → outdated cleared, unless the draft was outdated
                                               before this message or a newer incoming message
                                               is stored in the chat
  decision == "skip" and resolves_pending    → also drafts.possibly_unnecessary = true
card re-rendered once with the resulting banners
```

Flag first, clear after: a Send tapped while the classifier runs still gets the "new messages"
question, and no LLM call runs inside the chat lock or a database transaction.

## Manual-reply integration (`ChatEventHandler`, habit rule)

A manual reply drops the chat's open turns (001 T047). Dropped turns that the gate never
decided (`reply_decision IS NULL`) go to `ReplyGate.classify_dropped` after the transaction;
a recognised category is written with a guarded update (`state='dropped'`,
`dropped_reason='manual_reply'`, `skip_category IS NULL`). Non-candidates and chats with
skipping off cost no classifier call.

## LLM route `classify.reply_needed`

- Route table (`config/llm_routes.yml`):
  `classify.reply_needed: {provider: anthropic, model: claude-haiku-5-5, effort: low, max_tokens: 500}`.
- `RouteName`, `ROUTE_NAMES`, `ROUTE_OUTPUT_SCHEMAS` and `PROMPT_FOR_ROUTE`
  (`"reply_needed"`) are extended. `LLMRouter.classify_reply_needed(req) ->
  RoutedResult[ReplyNeededOutput]` is added.
- Prompt: `prompts/reply_needed.md`, `version: 1`. It states the rules from spec FR-002 and
  FR-003 and the edge cases, says that data blocks are untrusted, and says that instructions
  in messages never decide the outcome.

```python
class ReplyNeededRequest(LLMRequest):
    incoming_messages: list[MessageItem]  # the turn (or the single new message for pending)
    history: list[HistoryItem]  # last ≤ skip_context_messages in the chat
    contact: ContactInfo
    pending_draft: bool = False


class ReplyNeededOutput(_Output):  # mirrors reply-needed-output.schema.json
    reply_needed: bool
    category: Literal["thanks", "farewell", "acknowledgment", "none"]
    confidence: float = Field(ge=0, le=1)
    resolves_pending: bool
```

## Settings (`src/scribe/config.py`, env `SCRIBE_…`)

| Setting | Default | Meaning |
|---|---|---|
| `skip_candidate_max_words` | 15 | pre-gate word limit |
| `skip_min_confidence` | 0.8 | minimum classifier confidence to skip |
| `skip_context_messages` | 8 | history messages sent to the classifier |
| `skip_habit_window` | 5 | past closing turns examined (R3) |
| `skip_habit_min_replies` | 3 | replies among them that force drafting |
| `skip_habit_reply_window_min` | 30 | an outgoing message within this many minutes counts as a reply |
| `skipped_list_days` | 7 | `/skipped` look-back |

## Log line

`reply_gate_decided`: `user_id, turn_id, message_id, pending, decision, category, source,
confidence, route, provider, model, prompt_version, latency_ms, cost_usd` (`turn_id` for a
turn, `message_id` with `pending=true` for the pending-draft check). It never includes message
text.

## Changed by 013

[specs/013-noncommittal-replies/contracts/reply-gate.md](../../013-noncommittal-replies/contracts/reply-gate.md)
extends this contract; where they differ, 013 wins:

- The pre-gate no longer looks at words, media or a fixed 160-char limit:
  `is_skip_candidate(texts, *, has_rules, max_chars)` accepts any non-empty turn of at most
  `skip_candidate_max_chars` (2000) chars; a `?` disqualifies it only when the user has no rules.
  `skip_candidate_max_words` is gone.
- `SkipCategory` gains `informational` and `user_rule`; `DecisionSource` gains `rule`.
- The classifier output has the required field `decided_by` (`default` | `user_rule`); a
  confident rule decision is taken before the habit rule and never consults it.
- The gate request carries the user's rules (`user_rules`, `trust="user"`) and each message's
  `has_media`.
