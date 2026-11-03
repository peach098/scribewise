"""Style rule proposals from the user's edits (FR-033; contracts/control-chat.md "Style rule
proposal").

After every edited-and-approved draft the draft text and the user's edited text are compared as
features (emojis removed / added, greeting or sign-off removed, much shorter / longer, more
formal / casual, language switched). When the same feature shows up in at least 5 of the last 6
edited drafts of one profile (the global profile, or one contact's, across its versions), a
``style_rule_proposals`` row is created and the control chat shows "💡 I noticed …" with
✅ Add rule (``rule_accept:<id>``) / 🚫 Dismiss (``rule_dismiss:<id>``). The profile changes only
when the user accepts: the rule is added to the current version's ``user_rules_json``. Never
silently (Principle IV).

The same rule is not proposed again while a proposal for it is open or accepted, nor from the
same edits after a dismissal; a rule the profile already has is not proposed (for a contact's
profile, a rule of the global profile counts too: it applies to that contact already, issue
#42), and accepting one the global profile got meanwhile adds nothing. Logs carry ids and
feature names only, never text.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.db.models import (
    ActivityKind,
    Draft,
    DraftState,
    ProfileScope,
    ProfileState,
    ProposalState,
    StyleProfile,
    StyleRuleProposal,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.i18n import Msg
from scribe.logging import get_logger
from scribe.style.profiles import add_rule, effective_user_rules
from scribe.style.rule_catalog import FEATURES, rule_label

log = get_logger("scribe.style")

WINDOW = 6  # the last N edited drafts of one profile
THRESHOLD = 5  # ... of which at least this many show the same change


_EMOJI = re.compile(
    "[\U0001f000-\U0001faff☀-➿⬀-⯿️]|[:;]-?[)(DP]",
)
_WORD = re.compile(r"[\w']+", re.UNICODE)
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN = re.compile(r"[a-z]", re.IGNORECASE)
GREETINGS = (
    "привет",
    "здравствуй",
    "добрый день",
    "доброе утро",
    "добрый вечер",
    "hi",
    "hello",
    "hey",
    "dear",
    "good morning",
    "good afternoon",
)
FORMAL_MARKERS = frozenset(
    {
        "вы", "вас", "вам", "ваш", "ваша", "ваши", "пожалуйста", "уважаемый", "уважаемая",
        "благодарю", "please", "kindly", "regards", "sincerely",
    }
)  # fmt: skip
CASUAL_MARKERS = frozenset(
    {
        "ты", "тебя", "тебе", "твой", "ок", "окей", "спс", "давай",
        "ok", "okay", "thx", "yeah", "yep", "cool",
    }
)  # fmt: skip


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def _greets(text: str) -> bool:
    head = " ".join(text.split()).casefold()
    return any(head.startswith(g) for g in GREETINGS)


def _language(text: str) -> str | None:
    cyrillic, latin = len(_CYRILLIC.findall(text)), len(_LATIN.findall(text))
    if cyrillic + latin < 10:
        return None
    if cyrillic >= 3 * latin:
        return "ru"
    if latin >= 3 * cyrillic:
        return "en"
    return None


def _formality(words: list[str]) -> int:
    return sum(w in FORMAL_MARKERS for w in words) - sum(w in CASUAL_MARKERS for w in words)


def edit_features(draft_text: str, edited_text: str) -> frozenset[str]:
    """What kind of change the user made to a draft (deterministic, no LLM)."""
    found: set[str] = set()
    had_emoji, has_emoji = bool(_EMOJI.search(draft_text)), bool(_EMOJI.search(edited_text))
    if had_emoji and not has_emoji:
        found.add("emoji_removed")
    elif has_emoji and not had_emoji:
        found.add("emoji_added")
    if _greets(draft_text) and not _greets(edited_text):
        found.add("greeting_removed")
    draft_lines, edited_lines = _lines(draft_text), _lines(edited_text)
    if len(draft_lines) >= 2:
        sign_off = draft_lines[-1].casefold()
        if len(sign_off.split()) <= 4 and sign_off not in {
            line.casefold() for line in edited_lines
        }:
            found.add("sign_off_removed")
    draft_words, edited_words = _words(draft_text), _words(edited_text)
    if len(draft_words) >= 5:
        if len(edited_words) <= 0.6 * len(draft_words):
            found.add("shorter")
        elif len(edited_words) >= 1.5 * len(draft_words):
            found.add("longer")
    delta = _formality(edited_words) - _formality(draft_words)
    if delta >= 1:
        found.add("more_formal")
    elif delta <= -1:
        found.add("more_casual")
    before, after = _language(draft_text), _language(edited_text)
    if before is not None and after is not None and before != after:
        found.add(f"language_{after}")
    return frozenset(found & set(FEATURES))


@dataclass(frozen=True)
class ProposalNotice:
    proposal_id: uuid.UUID
    text: Msg  # rendered in the user's language by the sender


async def _lineage_ids(
    session: AsyncSession, user_id: uuid.UUID, profile: StyleProfile
) -> list[uuid.UUID]:
    lineage = await profiles_repo.list_lineage(
        session, user_id, scope=profile.scope, contact_id=profile.contact_id
    )
    return [p.id for p in lineage]


async def _current_of(
    session: AsyncSession, user_id: uuid.UUID, profile: StyleProfile
) -> StyleProfile | None:
    current = await profiles_repo.get_current_profile(
        session, user_id, scope=profile.scope, contact_id=profile.contact_id
    )
    return current if current is not None and current.state == ProfileState.current else None


async def profile_owner_label(
    session: AsyncSession, user_id: uuid.UUID, profile: StyleProfile
) -> str | Msg | None:
    """The contact's name for a contact profile, None for the global profile."""
    if profile.scope == ProfileScope.global_ or profile.contact_id is None:
        return None
    contact = await chats_repo.get_contact(session, user_id, profile.contact_id)
    return (contact.display_name if contact is not None else "") or Msg("proposal.this_contact")


def proposal_message(feature: str, count: int, total: int, owner: str | Msg | None) -> Msg:
    params = {
        "noticed": Msg(f"proposal.noticed.{feature}"),
        "rule": Msg(f"proposal.rule.{feature}"),
        "count": count,
        "total": total,
    }
    if owner:
        return Msg("proposal.text_contact", owner=owner, **params)
    return Msg("proposal.text_global", **params)


def proposal_text(
    feature: str, count: int, total: int, owner: str | Msg | None, lang: str = "en"
) -> str:
    return proposal_message(feature, count, total, owner).render(lang)


async def propose_after_edit(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    *,
    lang: str,
) -> list[ProposalNotice]:
    """Check the edit pattern of the profile ``draft_id`` used; create proposals for features
    seen in at least ``THRESHOLD`` of the last ``WINDOW`` edited drafts. A rule that already
    applies (own or global, the same rule in the user's ``lang``) is not proposed."""
    async with session_factory() as session:
        draft = await session.scalar(
            select(Draft).where(Draft.user_id == user_id, Draft.id == draft_id)
        )
        if draft is None or draft.style_profile_id is None:
            return []
        profile = await profiles_repo.get_profile(session, user_id, draft.style_profile_id)
        if profile is None:
            return []
        lineage = await _lineage_ids(session, user_id, profile)
        edited = list(
            (
                await session.execute(
                    select(Draft)
                    .where(
                        Draft.user_id == user_id,
                        Draft.style_profile_id.in_(lineage),
                        Draft.state == DraftState.sent,
                        Draft.edited_text.is_not(None),
                        Draft.text.is_not(None),
                    )
                    .order_by(Draft.finalized_at.desc(), Draft.created_at.desc())
                    .limit(WINDOW)
                )
            )
            .scalars()
            .all()
        )
        if len(edited) < THRESHOLD:
            return []
        per_draft = {d.id: edit_features(d.text or "", d.edited_text or "") for d in edited}
        counts = Counter(feature for features in per_draft.values() for feature in features)
        target = await _current_of(session, user_id, profile)
        if target is None:
            return []
        applied = await effective_user_rules(session, user_id, target, lang=lang)
        proposals = await profiles_repo.list_proposals(session, user_id, profile_ids=lineage)
        owner = await profile_owner_label(session, user_id, target)
        notices: list[ProposalNotice] = []
        for feature, count in sorted(counts.items()):
            if count < THRESHOLD:
                continue
            spec = FEATURES[feature]
            if applied.covers(spec.rule, feature):
                continue
            evidence = [d_id for d_id, features in per_draft.items() if feature in features]
            if _already_proposed(proposals, feature, evidence):
                continue
            proposal = await profiles_repo.create_proposal(
                session,
                user_id,
                style_profile_id=target.id,
                rule_json={"key": feature, "text": spec.rule},
                evidence_draft_ids=evidence,
            )
            notices.append(
                ProposalNotice(proposal.id, proposal_message(feature, count, len(edited), owner))
            )
            log.info(
                "rule_proposed",
                user_id=str(user_id),
                proposal_id=str(proposal.id),
                feature=feature,
                evidence=len(evidence),
            )
        await session.commit()
    return notices


def _already_proposed(
    proposals: list[StyleRuleProposal], feature: str, evidence: list[uuid.UUID]
) -> bool:
    for proposal in proposals:
        if proposal.rule_json.get("key") != feature:
            continue
        if proposal.state in (ProposalState.proposed, ProposalState.accepted):
            return True
        if set(proposal.evidence_draft_ids) & set(evidence):  # dismissed for these edits
            return True
    return False


class AcceptedRule(NamedTuple):
    rule: str
    owner: str | Msg | None  # None: the global profile
    already_global: bool = False  # a contact's proposal the global profile has: nothing added


class ProposalClosed(Exception):
    """The proposal is not open any more (accepted, dismissed, or its profile is gone)."""


async def accept_proposal(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    lang: str,
    clock: Clock = utcnow,
) -> AcceptedRule:
    """Add the rule to the current version of the proposal's profile (read under the user's
    profile lock, so a concurrent rebuild or refresh is waited for). A contact's proposal whose
    rule the global profile got meanwhile (:func:`same_rule`) adds nothing: the rule applies to
    the contact already (issue #42) and a copy would outlive its removal (:func:`add_rule`).
    Such a proposal is closed as ``dismissed`` with its evidence cleared and
    ``rule_json.closed = "already_global"``: ``accepted`` would block proposing the rule for
    that contact for good, kept evidence would block it until the window holds new edits (e.g.
    after the global rule is removed). No ``rule_accepted`` record: nothing was added.
    ``InvalidRule`` (e.g. the profile has the maximum number of rules) is raised before anything
    is stored: the proposal stays open."""
    async with session_factory() as session:
        await profiles_repo.lock_profiles(session, user_id)
        proposal = await profiles_repo.get_proposal(session, user_id, proposal_id)
        if proposal is None or proposal.state != ProposalState.proposed:
            raise ProposalClosed
        profile = await profiles_repo.get_profile(session, user_id, proposal.style_profile_id)
        target = await _current_of(session, user_id, profile) if profile is not None else None
        if target is None:
            raise ProposalClosed
        rule = str(proposal.rule_json.get("text") or "")
        key = proposal.rule_json.get("key")
        owner = await profile_owner_label(session, user_id, target)
        addition = add_rule(
            await effective_user_rules(session, user_id, target, lang=lang),
            target.user_rules_json,
            rule,
            source="proposal",
            key=str(key) if key is not None else None,
        )
        if addition.already_global:
            # No migration-free state means "resolved, nothing to add": closed as dismissed
            # with no evidence (so a later proposal of the rule from the same edits is not
            # blocked, see _already_proposed) and the reason kept in the row.
            proposal.state = ProposalState.dismissed
            proposal.evidence_draft_ids = []
            proposal.rule_json = {**proposal.rule_json, "closed": "already_global"}
            await session.commit()
            log.info("rule_already_global", user_id=str(user_id), proposal_id=str(proposal_id))
            return AcceptedRule(rule, owner, already_global=True)
        target.user_rules_json = addition.user_rules_json
        proposal.state = ProposalState.accepted
        await activity_repo.record(
            session,
            user_id,
            ActivityKind.rule_accepted,
            at=clock(),
            meta={"proposal_id": str(proposal.id), "profile_id": str(target.id), "key": key},
        )
        await session.commit()
    log.info("rule_accepted", user_id=str(user_id), proposal_id=str(proposal_id))
    return AcceptedRule(rule, owner)


async def dismiss_proposal(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, proposal_id: uuid.UUID
) -> None:
    async with session_factory() as session:
        proposal = await profiles_repo.get_proposal(session, user_id, proposal_id)
        if proposal is None or proposal.state != ProposalState.proposed:
            raise ProposalClosed
        proposal.state = ProposalState.dismissed
        await session.commit()
    log.info("rule_dismissed", user_id=str(user_id), proposal_id=str(proposal_id))


__all__ = [
    "FEATURES",
    "AcceptedRule",
    "THRESHOLD",
    "WINDOW",
    "ProposalClosed",
    "ProposalNotice",
    "accept_proposal",
    "dismiss_proposal",
    "edit_features",
    "proposal_message",
    "proposal_text",
    "propose_after_edit",
    "rule_label",
]
