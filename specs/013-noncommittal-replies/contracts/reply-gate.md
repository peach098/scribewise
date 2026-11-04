# Contract: Reply Gate changes (internal interface)

Extends [004 reply-gate](../../004-skip-closing-messages/contracts/reply-gate.md). Only the
differences are listed. Rationale: [research.md](../research.md) R4–R5.

## `src/scribe/drafting/reply_gate.py`

```python
SkipCategory = Literal[
    "thanks", "farewell", "acknowledgment", "informational", "user_rule", "forwarded", "budget"
]
DecisionSource = Literal["pregate", "model", "fallback", "habit", "disabled", "user", "rule"]


def is_skip_candidate(texts: Sequence[str], *, has_rules: bool, max_chars: int) -> bool:
    """Pure pre-gate: non-empty, ≤ max_chars chars; a '?' / '？' disqualifies only when
    has_rules is False. Media no longer disqualifies."""


async def decide(
    router: LLMRouter,
    req: ReplyNeededRequest,  # now carries user_rules
    *,
    enabled: bool,
    draft_anyway: bool,
    habit_replied: Callable[[], Awaitable[int]],
    min_confidence: float,
    max_chars: int,  # replaces max_words
    has_media: bool = False,  # kept for logging; no longer used by the pre-gate
    habit_min_replies: int = DEFAULT_HABIT_MIN_REPLIES,
    user_id: uuid.UUID | None = None,
) -> GateDecision:
    """Order: draft_anyway -> ("draft","user"); not enabled -> ("draft","disabled");
    not a candidate -> ("draft","pregate"); classifier error -> ("draft","fallback");
    decided_by=user_rule and reply_needed -> ("draft","rule");
    decided_by=user_rule, not reply_needed, confidence >= min -> ("skip","rule","user_rule");
    reply_needed -> ("draft","model");
    category none or confidence < min -> ("draft","fallback");
    habit_replied() >= habit_min_replies -> ("draft","habit");
    else ("skip","model", category)  # category may now be "informational"."""
```

`ReplyGate._decide` / `_decide_pending` / `_decide_dropped` load the effective user rules with
`effective_user_rules(session, user_id, select_profile(...), lang=user.language)` (the same call
the composer uses, `composer.py:922`) and pass them in `ReplyNeededRequest.user_rules`.

Messages in `ReplyNeededRequest.incoming_messages` carry `has_media: bool` (new field on the
gate's `MessageItem` use; default False).

`has_rules` is true when the effective rule set has at least one rule of any kind (research R4
trade-off).

Invariants (in addition to 004):
- `decide` never returns `skip` for a rule decision with `confidence < min_confidence`.
- A `rule` skip never consults `habit_replied`.
- Forwarded-only, budget, skip-switch-off and draft-anyway paths are decided before any user
  rule is read and are unchanged.
- The `user_rules` block is the only block rendered `trust="user"`.

## Logging

`reply_gate_decided` adds `decided_by` and `has_rules` (bool); still no message or rule text.
