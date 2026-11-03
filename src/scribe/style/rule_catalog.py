"""The rules a style proposal can add (FR-033) and how a rule reads to the user.

Shared by ``style/proposals.py`` (which proposes them) and ``style/profiles.py`` (whose rule
identity also matches a proposal rule by the label the user sees, issue #42).
"""

from __future__ import annotations

from dataclasses import dataclass

from scribe.i18n import has_text, t


@dataclass(frozen=True)
class RuleSpec:
    rule: str  # the rule added to user_rules_json (as the model reads it; shown via the catalog)
    noticed: str  # "I noticed you <noticed> in 5 of your last 6 edits" (English; see the catalog)


FEATURES: dict[str, RuleSpec] = {
    "emoji_removed": RuleSpec("no emojis", "removed emojis"),
    "emoji_added": RuleSpec("use emojis", "added emojis"),
    "greeting_removed": RuleSpec("no greeting", "removed the greeting"),
    "sign_off_removed": RuleSpec("no sign-off", "removed the sign-off"),
    "shorter": RuleSpec("keep replies short", "made the reply much shorter"),
    "longer": RuleSpec("write longer replies", "made the reply much longer"),
    "more_formal": RuleSpec("more formal tone", "made the reply more formal"),
    "more_casual": RuleSpec("more casual tone", "made the reply more casual"),
    "language_en": RuleSpec("reply in English", "switched the reply to English"),
    "language_ru": RuleSpec("reply in Russian", "switched the reply to Russian"),
}


def rule_label(text: str, key: str | None, lang: str | None) -> str:
    """A rule as the user reads it: an accepted proposal's rule (stored in English, as the model
    reads it) in the user's language; a rule the user typed as typed."""
    if key in FEATURES and FEATURES[key].rule == text and has_text(f"proposal.rule.{key}"):
        return t(f"proposal.rule.{key}", lang)
    return text


__all__ = ["FEATURES", "RuleSpec", "rule_label"]
