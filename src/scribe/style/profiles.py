"""Style profiles (FR-023, FR-034, FR-037; research R8).

- One global profile per user plus up to ``max_contact_profiles`` (20) per-contact profiles. A
  contact is a person in a private chat or a group chat as a whole (a group uses one slot).
- :func:`rank_contacts` orders contacts by ``contacts.own_message_count_6m`` (the user's own
  messages: the last 6 months at import; at each weekly refresh the messages written since the
  last refresh are added). :func:`assign_profile_slots` keeps pinned contacts first, then the
  most active ones with enough history, and retires the profiles of every other contact (rows
  kept 30 days for undo, then deleted by the retention job).
- :func:`select_profile` picks the contact's current profile, else the global one (a contact
  with no profile or too little history uses the global profile).
- The global profile's user rules apply to every draft (issue #42). :func:`effective_user_rules`
  resolves the rules a profile's drafts follow: for a contact profile, the current global rules
  (read when the draft is made, never copied into the contact's rows, so a changed or removed
  global rule reaches the next draft without a rebuild) plus the contact's own, which win on
  conflict. Profile builds get only their own lineage's rules: the learned contact style must
  not bake a global rule in (precedence is enforced at draft time).
- Every build writes a new version: the old current row becomes ``previous`` (the undo target)
  and an older ``previous`` is retired. ``user_rules_json`` (rules the user set or accepted) is
  copied forward unchanged, never rebuilt by the model.
- :func:`refresh_profiles` is the automatic refresh (job ``refresh_profiles``, registered in
  ``scribe/jobs.py``): a profile gets a new version when at least
  ``profile_refresh_min_new_messages`` own messages (including replies sent through the agent)
  were written for it since it was built. With the Telegram gateway it is built, like a
  Rebuild, from the own messages of the last ``style_import_months`` read from Telegram;
  without one (the jobs CLI) from the 30 days kept in the database. One batch when the adapter
  can batch, a "what changed" note, and :func:`undo_refresh` restores the previous versions of
  every profile in that refresh.
  Every run writes a ``profile_refreshed`` record, also when nothing changed
  (``meta_json.noop``): it is the mark after which own messages are counted next time.
- Writes to one user's profiles are serialized: every write transaction takes
  ``profiles_repo.lock_profiles`` (a transaction-level advisory lock) before reading the rows it
  changes, so concurrent builds (a double-tapped Rebuild, Rebuild during the weekly job, the jobs
  CLI next to the service) leave exactly one current version per profile.
  :func:`profile_build_lock` is the in-process lock a user-initiated Rebuild holds.
- 🗑 Remove is recorded (``profile_undo`` record with ``meta_json.removed_contact_id``): rebuilds
  and refreshes then rank that contact by the messages written after the removal only.
- A Rebuild supersedes the latest refresh (``profile_undo`` record with ``superseded``): its
  Undo is no longer offered, and an undo whose new versions are no longer current is refused.

Sample selection and retry (issue #30):

- Each build sends at most ``profile_max_samples`` (400) own messages. A contact's profile (a
  group's too) gets its newest messages; the global profile gets :func:`select_global_samples`:
  each group chat offers at most ``profile_group_samples_per_chat`` (40) newest messages, group
  chats together take at most ``profile_group_samples_share`` (30%) when private chats can fill
  the rest (else group messages fill the space), and very short messages (under 3 words, or no
  letter or digit) take at most ``SHORT_SAMPLES_SHARE`` (15%). ``sample_size`` is the number
  sent; ``weak`` still means fewer than ``min_style_samples`` own messages in the history.
- A truncated answer (``ProviderOutputInvalid.truncated``) is retried once, synchronously, with
  half the samples and the route's effort one step lower (setup, Rebuild and the weekly
  refresh; a batch item too). If the retry fails, that profile stays as it is; the global and
  the contact profiles fail independently.

Samples are only in memory while a profile is built (FR-035); a profile keeps at most 20 (contact)
/ 50 (global) example messages. Rules, summaries and examples are encrypted at rest. Nothing here
logs message content.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Contact,
    ContactKind,
    Message,
    ProfileScope,
    ProfileState,
    StyleProfile,
    WorkChat,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.i18n import LocalizedError, Msg, t
from scribe.llm.provider import (
    ProfileContext,
    ProviderError,
    ProviderOutputInvalid,
    StyleProfileOutput,
    StyleProfileRequest,
    StyleSample,
)
from scribe.llm.registry import LLMRouter, RoutedResult
from scribe.logging import get_logger
from scribe.style.importer import ImportResult, import_own_messages
from scribe.style.rule_catalog import rule_label
from scribe.telegram.gateway import MessagingGateway

log = get_logger("scribe.style")

# Very short own messages (fewer than SHORT_SAMPLE_WORDS words, or no letter or digit at all:
# "+", "👍") still count towards contact ranking and the weak mark, but take at most this share
# of the global sample sent to the model (issue #30).
SHORT_SAMPLE_WORDS = 3
SHORT_SAMPLES_SHARE = 0.15
# A contact needs this many of the user's own messages for a profile of its own (else global).
MIN_CONTACT_SAMPLES = 10
# The database window a refresh counts new own messages in (and builds from without a gateway).
# How many new messages a profile needs is ``Settings.profile_refresh_min_new_messages``.
REFRESH_WINDOW = timedelta(days=30)
MAX_USER_RULES = 30
MAX_RULE_CHARS = 200
USER_RULES_KEY = "rules"
REMOVED_KEY = "removed_contact_id"  # ``profile_undo`` meta of a 🗑 Remove

_BUILD_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}


def profile_build_lock(user_id: uuid.UUID) -> asyncio.Lock:
    """The in-process lock of one user's user-initiated profile build (🔄 Rebuild): a second tap
    while it is held is answered instead of starting a second build. The database lock
    (``profiles_repo.lock_profiles``) is what keeps the rows consistent across processes."""
    lock = _BUILD_LOCKS.get(user_id)
    if lock is None:
        lock = _BUILD_LOCKS[user_id] = asyncio.Lock()
    return lock


NEUTRAL_RULES: dict[str, Any] = {
    "languages": [],
    "formality": 3,
    "typical_length_words": 25,
    "greetings": [],
    "sign_offs": [],
    "emoji_usage": "none",
    "punctuation_habits": [],
    "typical_phrases": [],
    "never_does": [],
}
NEUTRAL_SUMMARY = (
    "There are not enough of your own messages yet to learn your style, so drafts use a "
    "neutral, polite and concise default until more of your messages are available."
)


def is_weak(profile: StyleProfile | None) -> bool:
    return profile is None or bool(profile.rules_json.get("weak", False))


def profile_context(profile: StyleProfile | None) -> ProfileContext:
    """What the drafting call sees of a profile (the stable, cached prefix). The user's own
    rules travel in their own block: :meth:`AppliedRules.user_rules_json` of
    :func:`effective_user_rules` (specs/013)."""
    if profile is None:
        return ProfileContext(rules_json={**NEUTRAL_RULES, "weak": True})
    return ProfileContext(rules_json=profile.rules_json, examples=list(profile.examples))


# --- user rules (FR-023: readable, editable) ----------------------------------------------------


def user_rules(user_rules_json: dict[str, Any]) -> list[dict[str, Any]]:
    """The rules the user set or accepted: ``{"text", "source": "user"|"proposal", "key"?}``."""
    items = user_rules_json.get(USER_RULES_KEY)
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("text"), str)]


def rule_texts(profile: StyleProfile | None) -> list[str]:
    return [] if profile is None else [r["text"] for r in user_rules(profile.user_rules_json)]


RULE_SCOPE_GLOBAL = "global"
RULE_SCOPE_CONTACT = "contact"


def _norm(text: str) -> str:
    return "text:" + " ".join(text.split()).casefold()


def _rule_ids(rule: dict[str, Any], *, lang: str) -> set[str]:
    """What makes two rules the same for a user writing in ``lang``: the text (case and spacing
    ignored), the proposal ``key``, or the label ``/profile`` shows for it in ``lang``
    (:func:`~scribe.style.rule_catalog.rule_label`: an accepted proposal's rule is stored in
    English and shown localized, e.g. "без эмодзи")."""
    key = rule.get("key")
    ids = {_norm(rule["text"]), _norm(rule_label(rule["text"], key, lang))}
    if key:
        ids.add(f"key:{key}")
    return ids


def same_rule(a: dict[str, Any], b: dict[str, Any], *, lang: str) -> bool:
    """The one definition of "the same rule" (adding, merging, proposals): :func:`_rule_ids`.
    ``lang`` (the user's language) is required: the label a rule is shown with counts too."""
    return bool(_rule_ids(a, lang=lang) & _rule_ids(b, lang=lang))


@dataclass(frozen=True)
class AppliedRules:
    """The user rules the drafts of one profile follow (issue #42). ``own``: the profile's own
    rules, as numbered in ``/profile`` (``-N`` removes one). ``inherited``: for a contact
    profile, the global profile's rules the contact does not have itself."""

    own: list[dict[str, Any]]
    lang: str  # the user's language: rule identity (:func:`same_rule`) depends on it
    inherited: list[dict[str, Any]] = field(default_factory=list)
    contact: bool = False

    def user_rules_json(self) -> dict[str, Any]:
        """For the drafting call. A contact profile's rules are marked ``scope`` ``global`` /
        ``contact``, global first; precedence (prompts/draft.md): chat overrides > contact rules
        > global rules > learned ``rules_json``. The global profile's go without a scope."""
        if not self.contact:
            return {USER_RULES_KEY: list(self.own)}
        return {
            USER_RULES_KEY: [
                *({**r, "scope": RULE_SCOPE_GLOBAL} for r in self.inherited),
                *({**r, "scope": RULE_SCOPE_CONTACT} for r in self.own),
            ]
        }

    def inherits(self, text: str, key: str | None = None) -> bool:
        """True when this rule (:func:`same_rule`) applies through the global profile."""
        rule = {"text": text, "key": key}
        return any(same_rule(r, rule, lang=self.lang) for r in self.inherited)

    def covers(self, text: str, key: str | None = None) -> bool:
        """True when this rule (:func:`same_rule`) already applies (own or inherited)."""
        rule = {"text": text, "key": key}
        return any(same_rule(r, rule, lang=self.lang) for r in (*self.own, *self.inherited))


def resolve_user_rules(
    profile: StyleProfile | None, global_rules_json: dict[str, Any], *, lang: str
) -> AppliedRules:
    """:class:`AppliedRules` of ``profile`` given the global profile's ``user_rules_json``
    (used for a contact profile only). A global rule the contact has too (same text or key) is
    listed once, as the contact's, so the contact's copy wins."""
    if profile is None:
        return AppliedRules(own=[], lang=lang)
    own = user_rules(profile.user_rules_json)
    if profile.scope != ProfileScope.contact:
        return AppliedRules(own=own, lang=lang)
    seen: set[str] = set()
    for rule in own:
        seen |= _rule_ids(rule, lang=lang)
    inherited: list[dict[str, Any]] = []
    for rule in user_rules(global_rules_json):
        ids = _rule_ids(rule, lang=lang)
        if not ids & seen:
            seen |= ids
            inherited.append(rule)
    return AppliedRules(own=own, lang=lang, inherited=inherited, contact=True)


async def inherited_rules_json(
    session: AsyncSession, user_id: uuid.UUID, profile: StyleProfile | None
) -> dict[str, Any]:
    """The global ``user_rules_json`` a contact profile inherits, read now; ``{}`` otherwise."""
    if profile is None or profile.scope != ProfileScope.contact:
        return {}
    return await profiles_repo.get_global_user_rules(session, user_id)


async def effective_user_rules(
    session: AsyncSession, user_id: uuid.UUID, profile: StyleProfile | None, *, lang: str
) -> AppliedRules:
    """The user rules ``profile``'s drafts follow, read now (:func:`resolve_user_rules`). The
    one resolver for drafting, ``/profile`` and rule proposals, so none of them forgets the
    global rules of a contact profile."""
    global_rules = await inherited_rules_json(session, user_id, profile)
    return resolve_user_rules(profile, global_rules, lang=lang)


class InvalidRule(LocalizedError):
    """A rule the user typed that cannot be stored (shown to the user)."""


def with_rule_added(
    user_rules_json: dict[str, Any],
    text: str,
    *,
    lang: str,
    source: str = "user",
    key: str | None = None,
) -> dict[str, Any]:
    """Add a rule unless the profile has it already (:func:`same_rule` in the user's ``lang``)."""
    rule = " ".join(text.split())
    if not rule:
        raise InvalidRule("err.rule_empty")
    if len(rule) > MAX_RULE_CHARS:
        raise InvalidRule("err.rule_too_long", n=MAX_RULE_CHARS)
    rules = user_rules(user_rules_json)
    if any(same_rule(r, {"text": rule, "key": key}, lang=lang) for r in rules):
        return dict(user_rules_json)
    if len(rules) >= MAX_USER_RULES:
        raise InvalidRule("err.rule_limit", n=MAX_USER_RULES)
    item: dict[str, Any] = {"text": rule, "source": source}
    if key is not None:
        item["key"] = key
    return {**user_rules_json, USER_RULES_KEY: [*rules, item]}


@dataclass(frozen=True)
class RuleAddition:
    user_rules_json: dict[str, Any]  # the profile's rules afterwards (unchanged if not added)
    already_global: bool = False  # the rule applies through the global profile: not added


def add_rule(
    applied: AppliedRules,
    user_rules_json: dict[str, Any],
    text: str,
    *,
    source: str = "user",
    key: str | None = None,
) -> RuleAddition:
    """Add a rule to a profile whose rules resolve to ``applied`` (typed in ✏️ Edit rules or an
    accepted proposal). A rule a contact profile inherits from the global profile (same rule in
    ``applied.lang``, also by the label shown) is not copied: it applies already (issue #42) and a
    copy would outlive its removal from the global profile."""
    if applied.inherits(text, key):
        return RuleAddition(user_rules_json, already_global=True)
    added = with_rule_added(user_rules_json, text, lang=applied.lang, source=source, key=key)
    return RuleAddition(added)


def with_rule_removed(user_rules_json: dict[str, Any], number: int) -> dict[str, Any]:
    """Remove rule ``number`` (1-based, as listed to the user)."""
    rules = user_rules(user_rules_json)
    if not 1 <= number <= len(rules):
        raise InvalidRule("err.rule_missing", number=number)
    return {**user_rules_json, USER_RULES_KEY: rules[: number - 1] + rules[number:]}


# --- selection ----------------------------------------------------------------------------------


async def select_profile(
    session: AsyncSession, user_id: uuid.UUID, contact_id: uuid.UUID | None
) -> StyleProfile | None:
    """The profile a draft for ``contact_id`` uses (FR-004, FR-034): the contact's current
    profile (a group chat's contact is the group), else the current global profile."""
    if contact_id is not None:
        contact_profile = await profiles_repo.get_current_profile(
            session, user_id, scope=ProfileScope.contact, contact_id=contact_id
        )
        if contact_profile is not None and contact_profile.state == ProfileState.current:
            return contact_profile
    return await profiles_repo.get_current_profile(session, user_id, scope=ProfileScope.global_)


# --- slots (FR-034) -----------------------------------------------------------------------------


async def rank_contacts(session: AsyncSession, user_id: uuid.UUID) -> list[Contact]:
    """Contacts by the user's own message volume, most active first (stable on ties)."""
    contacts = await chats_repo.list_contacts(session, user_id)
    return sorted(contacts, key=lambda c: (-c.own_message_count_6m, c.display_name, str(c.id)))


@dataclass
class SlotAssignment:
    kept: list[Contact]
    retired_profile_ids: list[uuid.UUID]  # rows that were ``current`` and are retired now


async def assign_profile_slots(
    session: AsyncSession, user_id: uuid.UUID, *, max_profiles: int
) -> SlotAssignment:
    """Pinned contacts first, then by rank (with at least ``MIN_CONTACT_SAMPLES`` own messages)
    up to ``max_profiles``; the profiles of every other contact are retired. Pinned contacts are
    never retired."""
    ranked = await rank_contacts(session, user_id)
    pinned = [c for c in ranked if c.pinned_profile]
    others = [
        c for c in ranked if not c.pinned_profile and c.own_message_count_6m >= MIN_CONTACT_SAMPLES
    ]
    kept = (pinned + others)[: max(max_profiles, len(pinned))]
    kept_ids = {c.id for c in kept}
    retired: list[uuid.UUID] = []
    for profile in await profiles_repo.list_profiles(session, user_id):
        if profile.scope != ProfileScope.contact or profile.contact_id in kept_ids:
            continue
        if profile.state == ProfileState.retired:
            continue
        if profile.state == ProfileState.current:
            retired.append(profile.id)
        profile.state = ProfileState.retired
        profile.is_current = False
    await session.flush()
    if retired:
        log.info("contact_profiles_retired", user_id=str(user_id), count=len(retired))
    return SlotAssignment(kept=kept, retired_profile_ids=retired)


# --- building -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Built:
    rules: dict[str, Any]
    summary: str
    examples: list[str]
    sample_size: int


def _built_from(
    output: StyleProfileOutput, used: list[StyleSample], *, weak: bool, max_examples: int
) -> _Built:
    indices = [i for i in dict.fromkeys(output.chosen_example_indices) if 0 <= i < len(used)]
    return _Built(
        rules={**output.rules_json.model_dump(mode="json"), "weak": weak},
        summary=output.summary_text,
        examples=[used[i].text for i in indices][:max_examples],
        sample_size=len(used),
    )


def _by_newest(samples: Iterable[StyleSample]) -> list[StyleSample]:
    return sorted(samples, key=lambda s: s.sent_at, reverse=True)


def is_short_sample(text: str) -> bool:
    """Fewer than ``SHORT_SAMPLE_WORDS`` words, or no letter or digit (emoji-only, "+")."""
    return len(text.split()) < SHORT_SAMPLE_WORDS or not any(ch.isalnum() for ch in text)


def select_contact_samples(samples: Sequence[StyleSample], *, limit: int) -> list[StyleSample]:
    """A contact's own profile (a group's too) learns from its newest ``limit`` messages."""
    return _by_newest(samples)[:limit]


def select_global_samples(
    samples: Sequence[StyleSample],
    *,
    limit: int,
    group_per_chat: int,
    group_share: float,
    short_share: float = SHORT_SAMPLES_SHARE,
) -> list[StyleSample]:
    """The global profile's sample (issue #30), newest first, at most ``limit`` messages.

    - Each group chat offers at most its ``group_per_chat`` newest messages, and group chats
      together take at most ``group_share`` of ``limit``; private chats (newest first) fill the
      rest. When private chats run out, group messages fill the remaining space (first the
      capped ones, then older messages of the groups), so a user who mostly writes in groups
      still gets a full sample.
    - Very short messages (:func:`is_short_sample`) take at most ``short_share`` of ``limit``
      (at least one); private chats are served first, further short messages are left out.
    """
    private = _by_newest(s for s in samples if not s.group)
    per_chat: dict[int | None, list[StyleSample]] = defaultdict(list)
    for sample in _by_newest(s for s in samples if s.group):
        per_chat[sample.chat_id].append(sample)
    capped = _by_newest(s for chat in per_chat.values() for s in chat[:group_per_chat])
    overflow = _by_newest(s for chat in per_chat.values() for s in chat[group_per_chat:])

    short_left = max(1, int(limit * short_share))
    picked: list[StyleSample] = []

    def take(pool: Iterator[StyleSample], until: int) -> None:
        nonlocal short_left
        while len(picked) < until:
            sample = next(pool, None)
            if sample is None:
                return
            if is_short_sample(sample.text):
                if short_left <= 0:
                    continue
                short_left -= 1
            picked.append(sample)

    private_pool, capped_pool, overflow_pool = iter(private), iter(capped), iter(overflow)
    take(private_pool, limit - min(int(limit * group_share), len(capped)))
    take(capped_pool, limit)  # the groups' share, or more when private chats ran out
    take(private_pool, limit)  # short group messages left out: private chats fill their place
    take(overflow_pool, limit)
    return _by_newest(picked)


def _select(job: _BuildJob, settings: Settings, limit: int) -> list[StyleSample]:
    if job.contact_id is None:
        return select_global_samples(
            job.samples,
            limit=limit,
            group_per_chat=settings.profile_group_samples_per_chat,
            group_share=settings.profile_group_samples_share,
        )
    return select_contact_samples(job.samples, limit=limit)


def _request(job: _BuildJob, used: list[StyleSample]) -> StyleProfileRequest:
    return StyleProfileRequest(
        samples=used,
        scope="global" if job.contact_id is None else "contact",
        existing_user_rules_json=job.user_rules_json,
    )


BuildOutcome = RoutedResult[StyleProfileOutput] | ProviderError


async def _retry_if_truncated(
    llm_router: LLMRouter,
    user_id: uuid.UUID,
    job: _BuildJob,
    used: list[StyleSample],
    outcome: BuildOutcome,
    *,
    settings: Settings,
) -> tuple[BuildOutcome, list[StyleSample]]:
    """A truncated answer (``ProviderOutputInvalid.truncated``) is retried once, synchronously,
    with a lighter request: half the samples (selected the same way) and the route's effort one
    step lower. Any other outcome, and the retry's, is returned as it is."""
    if not (isinstance(outcome, ProviderOutputInvalid) and outcome.truncated):
        return outcome, used
    lighter = _select(job, settings, max(len(used) // 2, 1))
    log.warning(
        "profile_build_retry",
        user_id=str(user_id),
        scope=job.scope.value,
        samples=len(used),
        retry_samples=len(lighter),
    )
    try:
        result = await llm_router.build_style_profile(
            _request(job, lighter), lighter=True, user_id=user_id
        )
        return result, lighter
    except ProviderError as exc:
        return exc, lighter


async def _store_version(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    scope: ProfileScope,
    contact_id: uuid.UUID | None,
    built: _Built,
    at: datetime,
) -> tuple[StyleProfile, uuid.UUID | None]:
    """New current version; the old current becomes ``previous``, an older ``previous`` is
    retired. User rules are copied forward. Returns (new row, id of the replaced current).
    Takes the user's profile lock first, so the lineage read here is the one it changes."""
    await profiles_repo.lock_profiles(session, user_id)
    lineage = await profiles_repo.list_lineage(session, user_id, scope=scope, contact_id=contact_id)
    current = next((p for p in lineage if p.state == ProfileState.current), None)
    user_rules_json = dict(current.user_rules_json) if current is not None else {}
    if current is None:  # a retired lineage (e.g. a contact back in the top 20) keeps its rules
        latest = lineage[0] if lineage else None
        user_rules_json = dict(latest.user_rules_json) if latest is not None else {}
    version = (lineage[0].version + 1) if lineage else 1
    for old in lineage:
        if old.state == ProfileState.previous:
            old.state = ProfileState.retired
            old.is_current = False
        elif old.state == ProfileState.current:
            old.state = ProfileState.previous
            old.is_current = False
    await session.flush()
    profile = await profiles_repo.create_profile(
        session,
        user_id,
        scope=scope,
        contact_id=contact_id,
        rules_json=built.rules,
        summary_text=built.summary,
        examples=built.examples,
        sample_size=built.sample_size,
        version=version,
        user_rules_json=user_rules_json,
    )
    profile.created_at = at  # the service clock: the refresh counts messages written after it
    await session.flush()
    return profile, current.id if current is not None else None


async def build_global_profile(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    user_id: uuid.UUID,
    samples: list[StyleSample],
    *,
    settings: Settings,
    clock: Clock = utcnow,
    supersede_refresh: bool = False,
) -> StyleProfile:
    """Build and store a new current global profile from ``samples`` (any order).

    With no samples there is nothing to learn: a neutral default is stored without an LLM call.
    The model gets :func:`select_global_samples` of them (``sample_size`` is what was sent);
    a truncated answer is retried once with a lighter request. Fewer than
    ``min_style_samples`` imported messages (all of ``samples``, not the capped sample) marks
    the profile ``weak`` (spec edge case). The previous current profile becomes ``previous``;
    user-edited rules are carried over. A build that still fails raises ``ProviderError`` and
    changes nothing.
    ``supersede_refresh`` (a Rebuild): the latest refresh can no longer be undone, recorded in
    the same transaction as the new version.
    """
    async with session_factory() as session:
        existing = await profiles_repo.get_current_profile(
            session, user_id, scope=ProfileScope.global_
        )
        existing_rules = dict(existing.user_rules_json) if existing is not None else {}

    weak = len(samples) < settings.min_style_samples
    job = _BuildJob(None, samples, existing_rules, weak=weak)
    used = _select(job, settings, settings.profile_max_samples)
    if used:
        outcome: BuildOutcome
        try:
            outcome = await llm_router.build_style_profile(_request(job, used), user_id=user_id)
        except ProviderError as exc:
            outcome = exc
        outcome, used = await _retry_if_truncated(
            llm_router, user_id, job, used, outcome, settings=settings
        )
        if isinstance(outcome, ProviderError):
            log.warning("profile_build_failed", user_id=str(user_id), scope=job.scope.value)
            raise outcome
        built = _built_from(
            outcome.output, used, weak=weak, max_examples=settings.profile_examples_global
        )
    else:
        built = _Built({**NEUTRAL_RULES, "weak": True}, NEUTRAL_SUMMARY, [], 0)

    async with session_factory() as session:
        profile, _ = await _store_version(
            session, user_id, scope=ProfileScope.global_, contact_id=None, built=built, at=clock()
        )
        if supersede_refresh:
            await supersede_latest_refresh(session, user_id, at=clock())
        await session.commit()
    log.info(
        "style_profile_built",
        user_id=str(user_id),
        scope="global",
        version=profile.version,
        sample_size=profile.sample_size,
        weak=weak,
    )
    return profile


@dataclass
class _BuildJob:
    """One profile to (re)build: the global one (``contact_id`` None) or a contact's."""

    contact_id: uuid.UUID | None
    samples: list[StyleSample]
    user_rules_json: dict[str, Any]
    weak: bool = False

    @property
    def scope(self) -> ProfileScope:
        return ProfileScope.global_ if self.contact_id is None else ProfileScope.contact


async def _build_versions(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    user_id: uuid.UUID,
    jobs: list[_BuildJob],
    *,
    settings: Settings,
    clock: Clock = utcnow,
    interactive: bool = False,
    supersede_refresh: bool = False,
) -> list[tuple[StyleProfile, uuid.UUID | None]]:
    """One ``profile.build`` call per profile, all in one batch when the route prefers batching
    and the adapter has the ``batch`` capability, else synchronous calls
    (``LLMRouter.build_style_profiles``); ``interactive`` builds (the user is waiting) are always
    synchronous. A truncated answer is retried once, synchronously (also after a batch), with a
    lighter request (:func:`_retry_if_truncated`). A build that still fails leaves that profile
    as it is. ``supersede_refresh``: when anything was stored, the latest refresh can no longer
    be undone (recorded in the same transaction)."""
    if not jobs:
        return []
    used = [_select(job, settings, settings.profile_max_samples) for job in jobs]
    requests = [_request(job, samples) for job, samples in zip(jobs, used, strict=True)]
    outcomes = await llm_router.build_style_profiles(
        requests, interactive=interactive, user_id=user_id
    )
    retried = [
        await _retry_if_truncated(llm_router, user_id, job, samples, outcome, settings=settings)
        for job, samples, outcome in zip(jobs, used, outcomes, strict=True)
    ]
    stored: list[tuple[StyleProfile, uuid.UUID | None]] = []
    async with session_factory() as session:
        await profiles_repo.lock_profiles(session, user_id)
        for job, (outcome, samples) in zip(jobs, retried, strict=True):
            if isinstance(outcome, ProviderError):
                log.warning("profile_build_failed", user_id=str(user_id), scope=job.scope.value)
                continue
            limit = (
                settings.profile_examples_global
                if job.contact_id is None
                else settings.profile_examples_contact
            )
            built = _built_from(outcome.output, samples, weak=job.weak, max_examples=limit)
            stored.append(
                await _store_version(
                    session,
                    user_id,
                    scope=job.scope,
                    contact_id=job.contact_id,
                    built=built,
                    at=clock(),
                )
            )
        if supersede_refresh and stored:
            await supersede_latest_refresh(session, user_id, at=clock())
        await session.commit()
    log.info("style_profiles_built", user_id=str(user_id), count=len(stored), requested=len(jobs))
    return stored


async def build_contact_profile(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    user_id: uuid.UUID,
    contact: Contact,
    samples: list[StyleSample],
    *,
    settings: Settings,
    clock: Clock = utcnow,
) -> StyleProfile | None:
    """A new current profile for one contact (at most ``profile_examples_contact`` examples).
    None when the contact has too little history (the global profile applies)."""
    if len(samples) < MIN_CONTACT_SAMPLES:
        return None
    async with session_factory() as session:
        lineage = await profiles_repo.list_lineage(
            session, user_id, scope=ProfileScope.contact, contact_id=contact.id
        )
        rules = dict(lineage[0].user_rules_json) if lineage else {}
    stored = await _build_versions(
        session_factory,
        llm_router,
        user_id,
        [_BuildJob(contact.id, samples, rules)],
        settings=settings,
        clock=clock,
    )
    return stored[0][0] if stored else None


async def _contact_for_chat(session: AsyncSession, user_id: uuid.UUID, chat: WorkChat) -> Contact:
    """The chat's contact: the person in a private chat, the group as a whole otherwise (the
    same rows intake creates)."""
    existing = await chats_repo.get_contact_by_peer(session, user_id, chat.telegram_chat_id)
    if existing is not None:
        return existing
    kind = ContactKind.person if chat.kind == ChatKind.private else ContactKind.group
    return await chats_repo.upsert_contact(
        session,
        user_id,
        telegram_peer_id=chat.telegram_chat_id,
        kind=kind,
        display_name=chat.title,
    )


async def _lineage_rules(
    session: AsyncSession, user_id: uuid.UUID, contact_id: uuid.UUID
) -> dict[str, Any]:
    lineage = await profiles_repo.list_lineage(
        session, user_id, scope=ProfileScope.contact, contact_id=contact_id
    )
    return dict(lineage[0].user_rules_json) if lineage else {}


@dataclass
class BuildSummary:
    global_profile: StyleProfile | None  # None: its build failed, the old one stays current
    contact_profiles: int
    retired: int
    contact_failures: int = 0  # contact profiles whose build failed (each stays as it was)


async def build_profiles_from_import(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    user_id: uuid.UUID,
    imported: ImportResult,
    *,
    settings: Settings,
    clock: Clock = utcnow,
    interactive: bool = True,
) -> BuildSummary:
    """Setup / 🔄 Rebuild: the global profile from every imported sample, then the per-contact
    profiles of the top contacts. ``own_message_count_6m`` is set from the import (6 months);
    for a contact whose profile the user removed (🗑) only the messages written after the
    removal count, so it gets a profile again only if those alone rank it into the top 20.
    The latest refresh can no longer be undone afterwards. The user is waiting (``interactive``):
    synchronous LLM calls, never the batch API.

    The global and the contact profiles fail independently: when the global build fails (after
    its retry) the old global profile stays and the contact profiles are still built
    (``BuildSummary.global_profile`` is None); a failed contact build keeps that contact's
    profile (``contact_failures``)."""
    global_profile: StyleProfile | None
    try:
        global_profile = await build_global_profile(
            session_factory,
            llm_router,
            user_id,
            imported.samples,
            settings=settings,
            clock=clock,
            supersede_refresh=True,
        )
    except ProviderError as exc:
        log.warning("global_profile_rebuild_failed", user_id=str(user_id), error=type(exc).__name__)
        global_profile = None
    jobs: list[_BuildJob] = []
    async with session_factory() as session:
        await profiles_repo.lock_profiles(session, user_id)
        removed = await removed_contacts(session, user_id)
        samples_by_contact: dict[uuid.UUID, list[StyleSample]] = {}
        for chat in await chats_repo.list_work_chats(session, user_id, enabled_only=True):
            if chat.telegram_chat_id not in imported.per_chat_counts:
                continue
            contact = await _contact_for_chat(session, user_id, chat)
            chat_samples = imported.by_chat.get(chat.telegram_chat_id, [])
            removed_at = removed.get(contact.id)
            if removed_at is not None and not contact.pinned_profile:
                contact.own_message_count_6m = sum(s.sent_at > removed_at for s in chat_samples)
            else:
                contact.own_message_count_6m = imported.per_chat_counts[chat.telegram_chat_id]
            samples_by_contact[contact.id] = chat_samples
        slots = await assign_profile_slots(
            session, user_id, max_profiles=settings.max_contact_profiles
        )
        for contact in slots.kept:
            samples = samples_by_contact.get(contact.id, [])
            if len(samples) >= MIN_CONTACT_SAMPLES:
                rules = await _lineage_rules(session, user_id, contact.id)
                jobs.append(_BuildJob(contact.id, samples, rules))
        await session.commit()
    stored = await _build_versions(
        session_factory,
        llm_router,
        user_id,
        jobs,
        settings=settings,
        clock=clock,
        interactive=interactive,
        supersede_refresh=True,  # a no-op when the global build already recorded it
    )
    return BuildSummary(
        global_profile,
        len(stored),
        len(slots.retired_profile_ids),
        contact_failures=len(jobs) - len(stored),
    )


# --- automatic refresh (FR-037) -----------------------------------------------------------------


@dataclass
class RefreshResult:
    batch_id: uuid.UUID | None  # the ``profile_refreshed`` activity record; None: nothing changed
    global_refreshed: bool = False
    contacts_refreshed: int = 0
    retired: int = 0
    changes: list[Msg] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.batch_id is not None


def _label(contact: Contact | None) -> Msg:
    if contact is None:
        return Msg("refresh.where.overall")
    if contact.kind == ContactKind.group:
        if contact.display_name:
            return Msg("refresh.where.group", name=contact.display_name)
        return Msg("refresh.where.a_group")
    if contact.display_name:
        return Msg("refresh.where.contact", name=contact.display_name)
    return Msg("refresh.where.a_contact")


_EMOJI_LEVEL = {"none": 0, "rare": 1, "frequent": 2}


def describe_changes(
    contact: Contact | None, old: dict[str, Any] | None, new: dict[str, Any]
) -> list[Msg]:
    """Short "what changed" phrases between two learned rule sets (no message content),
    rendered later in the user's language."""
    label = _label(contact)
    if old is None:
        return [Msg("refresh.change.new_profile", where=label)]
    changes: list[Msg] = []
    old_formality, new_formality = old.get("formality"), new.get("formality")
    if isinstance(old_formality, int) and isinstance(new_formality, int):
        if new_formality > old_formality:
            changes.append(Msg("refresh.change.more_formal", where=label))
        elif new_formality < old_formality:
            changes.append(Msg("refresh.change.more_casual", where=label))
    old_len, new_len = old.get("typical_length_words"), new.get("typical_length_words")
    if isinstance(old_len, int) and isinstance(new_len, int) and old_len > 0:
        if new_len <= old_len * 0.8:
            changes.append(Msg("refresh.change.shorter", where=label))
        elif new_len >= old_len * 1.25:
            changes.append(Msg("refresh.change.longer", where=label))
    old_emoji = _EMOJI_LEVEL.get(str(old.get("emoji_usage")))
    new_emoji = _EMOJI_LEVEL.get(str(new.get("emoji_usage")))
    if old_emoji is not None and new_emoji is not None and old_emoji != new_emoji:
        more = new_emoji > old_emoji
        changes.append(
            Msg(
                "refresh.change.more_emojis" if more else "refresh.change.fewer_emojis", where=label
            )
        )
    old_langs, new_langs = old.get("languages") or [], new.get("languages") or []
    if old_langs and new_langs and old_langs[0] != new_langs[0]:
        changes.append(Msg("refresh.change.language", language=new_langs[0], where=label))
    return changes


async def _own_messages_since(
    session: AsyncSession, user_id: uuid.UUID, since: datetime
) -> list[tuple[WorkChat, Message]]:
    """The user's own text messages in enabled work chats since ``since`` (manual and sent
    through the agent; both are stored as outgoing). Forwards are not the user's writing (#47)."""
    result = await session.execute(
        select(WorkChat, Message)
        .join(Message, Message.work_chat_id == WorkChat.id)
        .where(
            Message.user_id == user_id,
            WorkChat.user_id == user_id,
            WorkChat.enabled.is_(True),
            Message.is_outgoing.is_(True),
            Message.is_forwarded.is_(False),
            Message.has_media.is_(False),
            Message.sent_at >= since,
        )
        .order_by(Message.sent_at)
    )
    return [(chat, message) for chat, message in result.all() if message.text.strip()]


async def _last_refresh_at(session: AsyncSession, user_id: uuid.UUID) -> datetime | None:
    """The latest refresh run (no-op runs included): own messages are counted after it."""
    result = await session.execute(
        select(ActivityRecord.at)
        .where(
            ActivityRecord.user_id == user_id,
            ActivityRecord.kind == ActivityKind.profile_refreshed,
        )
        .order_by(ActivityRecord.at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# --- removal and supersession records (existing activity kinds, metadata only) ---------------


async def removed_contacts(session: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
    """Contacts whose profile the user removed (🗑), with the time of the latest removal."""
    result = await session.execute(
        select(ActivityRecord.meta_json, ActivityRecord.at).where(
            ActivityRecord.user_id == user_id,
            ActivityRecord.kind == ActivityKind.profile_undo,
            ActivityRecord.meta_json.has_key(REMOVED_KEY),
        )
    )
    removed: dict[uuid.UUID, datetime] = {}
    for meta, at in result.all():
        try:
            contact_id = uuid.UUID(str(meta[REMOVED_KEY]))
        except ValueError:
            continue
        if contact_id not in removed or at > removed[contact_id]:
            removed[contact_id] = at
    return removed


async def remove_contact_profile(
    session: AsyncSession, user_id: uuid.UUID, contact_id: uuid.UUID, *, at: datetime
) -> None:
    """🗑 Remove: retire every version of the contact's profile, unpin it, restart its message
    count and record the removal (so a rebuild does not bring the profile back from messages
    written before it). The caller commits."""
    await profiles_repo.lock_profiles(session, user_id)
    for row in await profiles_repo.list_lineage(
        session, user_id, scope=ProfileScope.contact, contact_id=contact_id
    ):
        if row.state != ProfileState.retired:
            row.state, row.is_current = ProfileState.retired, False
    contact = await chats_repo.get_contact(session, user_id, contact_id)
    if contact is not None:
        contact.pinned_profile = False
        contact.own_message_count_6m = 0
    await activity_repo.record(
        session,
        user_id,
        ActivityKind.profile_undo,
        at=at,
        meta={REMOVED_KEY: str(contact_id), "removed": True},
    )
    await session.flush()


async def supersede_latest_refresh(
    session: AsyncSession, user_id: uuid.UUID, *, at: datetime
) -> None:
    """A Rebuild replaced the profiles: the latest refresh can no longer be undone."""
    batch_id = await last_undoable_refresh(session, user_id)
    if batch_id is None:
        return
    await activity_repo.record(
        session,
        user_id,
        ActivityKind.profile_undo,
        at=at,
        meta={"batch_id": str(batch_id), "superseded": True},
    )


async def refresh_profiles(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    user_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Clock = utcnow,
    gateway: MessagingGateway | None = None,
) -> RefreshResult:
    """Automatic refresh of the global and per-contact profiles (FR-037).

    New own messages are counted in the database (the last 30 days). Contact ranking adds the
    messages written since the last refresh run (or rebuild; for a removed contact, since the
    removal); the slots are re-assigned (a newcomer that now ranks higher gets a profile, the
    least active unpinned contact is retired). Each profile with at least
    ``settings.profile_refresh_min_new_messages`` own messages written since it was built (for a
    contact without a profile: in the window) gets a new version; user rules are copied
    unchanged. With a ``gateway`` the new versions are built from the own messages of the last
    ``style_import_months`` read from Telegram, as a Rebuild does (only when some profile is due,
    after the run is recorded); without one, or for a profile the import found nothing for, from
    the database window.

    One ``profile_refreshed`` activity record per run, written with the counts in the same
    transaction (so a run is never counted twice), lists the replaced, new and retired rows (ids
    only) and is the batch the "what changed" note's ↩️ Undo refers to. ``meta_json.noop`` marks
    a run that changed nothing (not undoable); ``meta_json.pending`` is true until the builds are
    stored (the weekly job resumes users whose run did not complete).
    """
    now = clock()
    window_start = now - REFRESH_WINDOW
    threshold = settings.profile_refresh_min_new_messages
    async with session_factory() as session:
        await profiles_repo.lock_profiles(session, user_id)
        last = await _last_refresh_at(session, user_id)
        global_current = await profiles_repo.get_current_profile(
            session, user_id, scope=ProfileScope.global_
        )
        # Messages written since the last refresh run or (re)build were not counted yet.
        marks = [m for m in (last, global_current.created_at if global_current else None) if m]
        since_last = max(marks) if marks else window_start
        removed = await removed_contacts(session, user_id)
        rows = await _own_messages_since(session, user_id, window_start)
        by_contact: dict[uuid.UUID, list[StyleSample]] = defaultdict(list)
        contacts: dict[uuid.UUID, Contact] = {}
        new_counts: dict[uuid.UUID, int] = defaultdict(int)
        all_samples: list[StyleSample] = []
        built_at: dict[uuid.UUID, datetime] = {
            p.contact_id: p.created_at
            for p in await profiles_repo.list_current_contact_profiles(session, user_id)
            if p.contact_id is not None
        }
        # Own messages written since each profile was built: what decides whether it is due.
        due_counts: dict[uuid.UUID, int] = defaultdict(int)
        global_due = 0
        for chat, message in rows:
            contact = await _contact_for_chat(session, user_id, chat)
            contacts[contact.id] = contact
            sample = StyleSample(
                text=message.text,
                sent_at=message.sent_at,
                contact_display_name=chat.title,
                chat_id=chat.telegram_chat_id,
                group=chat.kind != ChatKind.private,
            )
            by_contact[contact.id].append(sample)
            all_samples.append(sample)
            removed_at = removed.get(contact.id)
            floor = max(since_last, removed_at) if removed_at is not None else since_last
            if message.sent_at > floor:
                new_counts[contact.id] += 1
            built = built_at.get(contact.id)
            marks_due = [m for m in (built, removed_at) if m is not None]
            if not marks_due or message.sent_at > max(marks_due):
                due_counts[contact.id] += 1
            if global_current is None or message.sent_at > global_current.created_at:
                global_due += 1
        for contact_id, count in new_counts.items():
            contacts[contact_id].own_message_count_6m += count
        await session.flush()
        old_rules: dict[uuid.UUID | None, dict[str, Any]] = {}
        if global_current is not None:
            old_rules[None] = dict(global_current.rules_json)
        for current in await profiles_repo.list_current_contact_profiles(session, user_id):
            old_rules[current.contact_id] = dict(current.rules_json)
        slots = await assign_profile_slots(
            session, user_id, max_profiles=settings.max_contact_profiles
        )
        jobs = [
            _BuildJob(c.id, by_contact[c.id], await _lineage_rules(session, user_id, c.id))
            for c in slots.kept
            if due_counts.get(c.id, 0) >= threshold
        ]
        if global_current is not None and global_due >= threshold:
            jobs.insert(0, _BuildJob(None, all_samples, dict(global_current.user_rules_json)))
        # Telegram chat id -> contact, for the samples of the import (enabled work chats).
        chat_contacts: dict[int, uuid.UUID] = {}
        if jobs and gateway is not None:
            for chat in await chats_repo.list_work_chats(session, user_id, enabled_only=True):
                chat_contacts[chat.telegram_chat_id] = (
                    await _contact_for_chat(session, user_id, chat)
                ).id
        kept_contacts = {c.id: c for c in slots.kept}
        retired_ids = [str(i) for i in slots.retired_profile_ids]
        meta: dict[str, Any] = {
            "global": False,
            "contacts": 0,
            "replaced": [],
            "retired": retired_ids,
            "samples": len(all_samples),
            "noop": not jobs and not retired_ids,
            "pending": bool(jobs),
        }
        record = await activity_repo.record(
            session, user_id, ActivityKind.profile_refreshed, at=now, meta=meta
        )
        batch_id = record.id
        global_weak_before = is_weak(global_current)
        await session.commit()

    if jobs and gateway is not None:
        # A failure here leaves the run ``pending``: the next job run resumes it.
        imported = await import_own_messages(
            gateway, session_factory, user_id, settings=settings, clock=clock
        )
        by_contact_imported: dict[uuid.UUID, list[StyleSample]] = defaultdict(list)
        for chat_id, chat_samples in imported.by_chat.items():
            owner = chat_contacts.get(chat_id)
            if owner is not None:
                by_contact_imported[owner].extend(chat_samples)
        for job in jobs:
            fresh = (
                imported.samples if job.contact_id is None else by_contact_imported[job.contact_id]
            )
            if fresh:
                job.samples = fresh
    for job in jobs:
        if job.contact_id is None:
            # A refresh never makes a profile weak: the old one was learned from more history.
            job.weak = global_weak_before and len(job.samples) < settings.min_style_samples

    replaced: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    changes: list[Msg] = []
    global_refreshed = False
    if jobs:
        for profile, old_id in await _build_versions(
            session_factory, llm_router, user_id, jobs, settings=settings, clock=clock
        ):
            replaced.append((profile.id, old_id))
            target: Contact | None = None
            if profile.contact_id is None:
                global_refreshed = True
            else:
                target = kept_contacts.get(profile.contact_id)
            previous_rules = old_rules.get(profile.contact_id) if old_id is not None else None
            changes += describe_changes(target, previous_rules, profile.rules_json)
        contacts_refreshed = len(replaced) - int(global_refreshed)
        meta = {
            **meta,
            "global": global_refreshed,
            "contacts": contacts_refreshed,
            "replaced": [[str(new), str(old) if old else None] for new, old in replaced],
            "noop": not replaced and not retired_ids,
            "pending": False,
        }
        async with session_factory() as session:
            await session.execute(
                update(ActivityRecord)
                .where(ActivityRecord.user_id == user_id, ActivityRecord.id == batch_id)
                .values(meta_json=meta)
            )
            await session.commit()
    if meta["noop"]:
        log.info("profiles_refresh_nothing_new", user_id=str(user_id), samples=len(all_samples))
        return RefreshResult(batch_id=None)
    contacts_refreshed = len(replaced) - int(global_refreshed)
    log.info(
        "profiles_refreshed",
        user_id=str(user_id),
        batch_id=str(batch_id),
        global_refreshed=global_refreshed,
        contacts=contacts_refreshed,
        retired=len(slots.retired_profile_ids),
    )
    return RefreshResult(
        batch_id=batch_id,
        global_refreshed=global_refreshed,
        contacts_refreshed=contacts_refreshed,
        retired=len(slots.retired_profile_ids),
        changes=changes,
    )


def refresh_note(result: RefreshResult, lang: str = "en") -> str:
    """The control-chat note (contracts/control-chat.md "Weekly refresh note")."""
    scope: list[str] = []
    if result.global_refreshed:
        scope.append(t("refresh.scope_global", lang))
    if result.contacts_refreshed:
        scope.append(t("refresh.scope_contacts", lang, n=result.contacts_refreshed))
    head = (
        t("refresh.head", lang, scope=" + ".join(scope)) if scope else t("refresh.head_plain", lang)
    )
    changes = (
        "; ".join(change.render(lang) for change in result.changes)
        if result.changes
        else t("refresh.no_changes", lang)
    )
    text = t("refresh.changes", lang, head=head, changes=changes)
    if result.retired:
        text += t("refresh.retired", lang, n=result.retired)
    return text + t("refresh.rules_unchanged", lang)


class UndoRefused(Exception):
    """The refresh cannot be undone (already undone, superseded, or unknown)."""


@dataclass
class UndoResult:
    restored: int
    retired: int


async def last_undoable_refresh(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """The latest ``profile_refreshed`` record that changed something, if it was not undone yet
    and no Rebuild superseded it (no-op runs have nothing to undo; a run still building, or
    interrupted while building, is not offered)."""
    latest = (
        await session.execute(
            select(ActivityRecord)
            .where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.kind == ActivityKind.profile_refreshed,
                ~ActivityRecord.meta_json.contains({"noop": True}),
                ~ActivityRecord.meta_json.contains({"pending": True}),
            )
            .order_by(ActivityRecord.at.desc(), ActivityRecord.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return None
    undone = await session.execute(
        select(ActivityRecord.id)
        .where(
            ActivityRecord.user_id == user_id,
            ActivityRecord.kind == ActivityKind.profile_undo,
            ActivityRecord.meta_json["batch_id"].astext == str(latest.id),
        )
        .limit(1)
    )
    return None if undone.first() is not None else latest.id


async def undo_refresh(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    batch_id: uuid.UUID,
    *,
    clock: Clock = utcnow,
    max_profiles: int = 20,
) -> UndoResult:
    """↩️ Undo (FR-037): every profile of refresh ``batch_id`` goes back to its previous
    version, profiles new in that refresh are retired, and contacts retired by it get their
    profile back while a slot is free (at most ``max_profiles`` current contact profiles). Rules
    the user set meanwhile stay (copied to the restored version). Only the latest refresh can be
    undone, once, and only while every version it created is still current (a Rebuild, a later
    refresh or 🗑 Remove replaced it otherwise): :class:`UndoRefused` changes nothing."""
    async with session_factory() as session:
        await profiles_repo.lock_profiles(session, user_id)
        if await last_undoable_refresh(session, user_id) != batch_id:
            raise UndoRefused("not the latest refresh, or already undone")
        record = (
            await session.execute(
                select(ActivityRecord).where(
                    ActivityRecord.user_id == user_id, ActivityRecord.id == batch_id
                )
            )
        ).scalar_one()
        meta = record.meta_json
        pairs: list[tuple[StyleProfile, StyleProfile | None]] = []
        for new_id, old_id in meta.get("replaced", []):
            new = await profiles_repo.get_profile(session, user_id, uuid.UUID(new_id))
            if new is None or new.state != ProfileState.current:
                raise UndoRefused("the refreshed profiles were replaced since")
            old = (
                await profiles_repo.get_profile(session, user_id, uuid.UUID(old_id))
                if old_id
                else None
            )
            pairs.append((new, old))
        restored = retired = 0
        for new, old in pairs:
            new.state, new.is_current = ProfileState.retired, False
            retired += 1
            if old is not None and old.state == ProfileState.previous:
                old.state, old.is_current = ProfileState.current, True
                old.user_rules_json = dict(new.user_rules_json)
                restored += 1
        await session.flush()
        slots_used = len(await profiles_repo.list_current_contact_profiles(session, user_id))
        for profile_id in meta.get("retired", []):
            old = await profiles_repo.get_profile(session, user_id, uuid.UUID(profile_id))
            if old is None or old.state != ProfileState.retired or old.contact_id is None:
                continue
            if await profiles_repo.get_current_profile(
                session, user_id, scope=ProfileScope.contact, contact_id=old.contact_id
            ):
                continue
            if slots_used >= max_profiles:
                continue  # the slot was taken meanwhile: the contact keeps the global profile
            old.state, old.is_current = ProfileState.current, True
            slots_used += 1
            restored += 1
        await activity_repo.record(
            session,
            user_id,
            ActivityKind.profile_undo,
            at=clock(),
            meta={"batch_id": str(batch_id), "restored": restored, "retired": retired},
        )
        await session.commit()
    log.info("profiles_refresh_undone", user_id=str(user_id), batch_id=str(batch_id))
    return UndoResult(restored=restored, retired=retired)


__all__ = [
    "MIN_CONTACT_SAMPLES",
    "REMOVED_KEY",
    "RULE_SCOPE_CONTACT",
    "RULE_SCOPE_GLOBAL",
    "AppliedRules",
    "SHORT_SAMPLES_SHARE",
    "BuildSummary",
    "InvalidRule",
    "RefreshResult",
    "RuleAddition",
    "SlotAssignment",
    "UndoRefused",
    "add_rule",
    "assign_profile_slots",
    "build_contact_profile",
    "build_global_profile",
    "build_profiles_from_import",
    "describe_changes",
    "effective_user_rules",
    "inherited_rules_json",
    "is_short_sample",
    "is_weak",
    "last_undoable_refresh",
    "profile_build_lock",
    "profile_context",
    "rank_contacts",
    "refresh_note",
    "refresh_profiles",
    "resolve_user_rules",
    "remove_contact_profile",
    "removed_contacts",
    "rule_texts",
    "same_rule",
    "select_contact_samples",
    "select_global_samples",
    "select_profile",
    "supersede_latest_refresh",
    "undo_refresh",
    "user_rules",
    "with_rule_added",
    "with_rule_removed",
]
