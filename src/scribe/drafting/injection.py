"""Deterministic prompt-injection checks (FR-010, issue #23).

Two keyword passes, independent of the drafting model:

- :func:`detect_injection` looks at the **incoming** messages for instruction-like text aimed at
  the agent ("ignore previous instructions", "SYSTEM:", "this is the operator", a literal
  ``</data>``, "одобри и отправь", "без подтверждения", ...);
- :func:`detect_action_claims` looks at the **draft text** for statements of actions a chat reply
  cannot perform ("sent all pending drafts", "changed the settings", "without review",
  "отправил автоматически", "одобрено", ...): the model complied with an injection, or invented
  an action.

The composer turns a match into the *system* warnings :data:`SUSPECTED_INJECTION` and
:data:`ACTION_CLAIM` (``system_warnings`` in ``drafts.output_json``). They live next to the
model output, never in it: the model can neither raise nor clear them, and a regenerated draft
is checked again from scratch.

Both warnings are **hard stops** (:data:`HARD_STOP_WARNINGS`, :func:`hard_stops`): the review
card shows them and Send asks for an explicit confirmation. Scribe has no automatic sending
(constitution I: every message is approved); should an opt-in auto-send mode ever be added, it
must refuse any draft for which :func:`hard_stops` is non-empty: that function is its seam.

Patterns are word-start anchored like ``flags.py``, English and Russian. False positives cost a
confirmation tap, false negatives may cost an unwanted send, so the lists lean towards
matching; the one deliberate narrowing is "operator", which is a normal word ("the operator of
the site said...") and only counts in a self-introduction or command context ("this is the
Scribe operator", "operator override", "я оператор"). The claim check does not try to tell a
refusal ("I can't send it without review") from compliance: that is safe-side by design.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

SUSPECTED_INJECTION = "suspected_injection"
ACTION_CLAIM = "action_claim"
HARD_STOP_WARNINGS: tuple[str, ...] = (SUSPECTED_INJECTION, ACTION_CLAIM)

# Instruction-like text in an incoming message.
_INJECTION_PATTERNS: tuple[str, ...] = (
    # "ignore all previous instructions", "disregard your rules", "forget the above prompt"
    r"(?:ignore|disregard|forget)\s+(?:(?:all|any|the|your|my|of|previous|prior|above|earlier"
    r"|these|those)\s+){1,4}(?:instructions?|rules|prompts?|guidelines|directions)",
    r"(?:new|updated|additional)\s+instructions?",
    r"you\s+are\s+now",
    r"from\s+now\s+on\s+you",
    r"system\s*(?:prompt\s*)?:",
    r"\[\s*system\s*\]",
    # "operator" only as a self-introduction or a command, not as an ordinary noun.
    r"(?:this\s+is|i\s+am|i'm|i’m|we\s+are|message\s+from)\s+(?:the\s+|your\s+|an?\s+)?"
    r"operators?",
    r"operators?\s+(?:here|mode|override|command|instructions?|message)",
    r"(?:scribe|bot|agent|assistant)\s+operators?",
    r"approve\s+and\s+send",
    r"send\s+(?:\w+\s+){0,3}(?:automatically|without\s+review)",
    r"auto[- ]?send",
    r"новы[ехй]\s+инструкци\w*",
    r"нов\w*\s+указани\w*",
    r"игнорируй\w*",
    r"проигнорируй\w*",
    r"забудь\w*\s+(?:все\s+|про\s+|о\s+)?(?:предыдущ\w+\s+|прошл\w+\s+)?(?:инструкци|правил|указани)\w*",
    r"систем[аы]\s*:",
    r"системн\w+\s+(?:сообщени|промпт|инструкци)\w*\s*:?",
    r"(?:это|я|мы)\s+(?:ваш\w*\s+|тво\w+\s+)?оператор\w*",
    r"оператор\w*\s+(?:scribe|бота|сервиса|системы)",
    r"одобри\s+и\s+отправь",
    r"без\s+подтверждени\w+",
    r"без\s+проверки",
    r"отправь\w*\s+(?:\w+\s+){0,3}автоматически",
    r"автоматически\s+отправь\w*",
    # A data-block delimiter written into a message (escaped in the prompt, but still a tell).
    r"</?\s*data",
)

# Statements in a draft of actions a chat reply cannot perform.
_CLAIM_PATTERNS: tuple[str, ...] = (
    # "sent all (pending) drafts / messages / replies": narrower than "sent all", which is an
    # ordinary "I sent all the files yesterday".
    r"sent\s+(?:out\s+)?all\s+(?:(?:the|your|my|of\s+the|pending|queued|waiting|remaining)\s+)*"
    r"(?:drafts?|messages|replies|responses)",
    r"(?:changed|updated|reset|set)\s+(?:the|my|your)\s+(?:reading\s+|reply\s+)?"
    r"(?:settings|delay)",
    r"(?:reading\s+|reply\s+)?delay\s+(?:is|has\s+been)\s+(?:now|set|changed)",
    r"without\s+(?:a\s+|any\s+)?review",
    r"(?:sent|replied|approved)\s+automatically",
    r"automatically\s+(?:sent|approved)",
    r"изменил[аи]?\s+(?:\w+\s+)?настройк\w*",
    r"поменял[аи]?\s+(?:\w+\s+)?настройк\w*",
    r"отправил[аи]?\s+(?:\w+\s+){0,3}автоматически",
    r"автоматически\s+отправил[аи]?",
    r"отправлен[оаы]?\s+автоматически",
    r"одобрено",
    r"без\s+подтверждени\w+",
    r"без\s+проверки",
)


def _compile(patterns: Iterable[str]) -> re.Pattern[str]:
    return re.compile(r"(?<![\w])(?:" + "|".join(patterns) + r")(?![\w])", re.IGNORECASE)


_INJECTION = _compile(_INJECTION_PATTERNS)
_CLAIM = _compile(_CLAIM_PATTERNS)


def injection_markers(*texts: str) -> list[str]:
    """The suspected-injection phrases found in ``texts`` (incoming messages), in order."""
    return [m.group(0) for text in texts for m in _INJECTION.finditer(text)]


def detect_injection(*texts: str) -> bool:
    """True when any incoming text looks like instructions aimed at the agent."""
    return any(_INJECTION.search(text) for text in texts)


def action_claims(text: str) -> list[str]:
    """The action-claim phrases found in a draft text, in order."""
    return [m.group(0) for m in _CLAIM.finditer(text)]


def detect_action_claims(text: str) -> bool:
    """True when the draft states an action a chat reply cannot perform."""
    return _CLAIM.search(text) is not None


def injection_warnings(draft_text: str, incoming: Iterable[str]) -> list[str]:
    """The hard-stop system warnings for a draft, in :data:`HARD_STOP_WARNINGS` order."""
    found = []
    if detect_injection(*incoming):
        found.append(SUSPECTED_INJECTION)
    if detect_action_claims(draft_text):
        found.append(ACTION_CLAIM)
    return found


def hard_stops(system_warnings: Iterable[str]) -> list[str]:
    """The warnings that forbid sending without the user's explicit confirmation. An automatic
    send must refuse a draft for which this is non-empty."""
    present = set(system_warnings)
    return [code for code in HARD_STOP_WARNINGS if code in present]


__all__ = [
    "ACTION_CLAIM",
    "HARD_STOP_WARNINGS",
    "SUSPECTED_INJECTION",
    "action_claims",
    "detect_action_claims",
    "detect_injection",
    "hard_stops",
    "injection_markers",
    "injection_warnings",
]
