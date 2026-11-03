"""Search over earlier conversations while drafting (specs/004-conversation-search).

The recent window (``history.load_candidates``) covers the last messages of the last 30 days.
When the filter route says a reply needs older context (research S1), :class:`EarlierSearch`
runs a server-side Telegram search over the sender's enabled work chats only (FR-S01, S4):

- chats: the turn's chat, the private chat with the sender if it is an enabled work chat, and up
  to 2 other enabled group work chats where the sender wrote within the retention window, most
  recent first;
- ≤ 5 queries × ≤ 4 chats requests, at most 4 in flight, 20 results each (the queries are the
  filter's, led by up to 2 word stems they share: :func:`search_terms`); the whole step is
  bounded by ``settings.earlier_search_timeout_s`` and stops at the first flood wait
  (``GatewayBusy``, FR-S18);
- group matches survive only when linked to the sender or the user (:func:`link_reason`, S5);
- matches already in the recent window, empty ones and duplicates are dropped (S6); at most
  60, newest first, numbered ``earlier:1``…

Nothing found here is stored (FR-S03); the composer keeps only the cited ones inside the draft.
``search`` never raises: failures become an outcome for the card and the log.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import ChatKind, Message, WorkChat
from scribe.llm.provider import HistoryItem
from scribe.logging import get_logger
from scribe.telegram.gateway import (
    FoundMessage,
    GatewayBusy,
    GatewayNotStarted,
    MessagingGateway,
    SessionRevoked,
)

log = get_logger("scribe.earlier")

MAX_QUERIES = 5
MAX_EXTRA_GROUPS = 2
MAX_CHATS = 4  # turn chat + private chat + MAX_EXTRA_GROUPS
PER_REQUEST_LIMIT = 20
MAX_MATCHES = 60
SNIPPET_CHARS = 500
IN_FLIGHT = 4
EARLIER_PREFIX = "earlier:"

Outcome = Literal["ok", "no_match", "failed", "rate_limited", "timeout", "not_started"]
LinkReason = Literal["private", "own", "sender", "reply", "replied_by", "mention", "thread"]


@dataclass(frozen=True)
class SearchChat:
    telegram_chat_id: int
    title: str
    kind: ChatKind


@dataclass(frozen=True)
class SearchScope:
    user_id: uuid.UUID
    turn_id: uuid.UUID | None
    turn_chat_id: int  # Telegram id
    sender_peer_id: int | None
    user_peer_id: int | None
    incoming_thread_top_id: int | None = None
    known: frozenset[tuple[int, int]] = frozenset()  # (chat id, message id) already offered
    sender_username: str | None = None  # filled by ``search`` via ``get_usernames``
    user_username: str | None = None


@dataclass(frozen=True)
class EarlierMatch:
    ref: str
    found: FoundMessage
    chat_title: str
    chat_kind: ChatKind
    link_reason: LinkReason

    def sender_name(self) -> str:
        if self.found.is_outgoing:
            return "me"
        if self.found.sender_display_name:
            return self.found.sender_display_name
        return self.chat_title if self.chat_kind == ChatKind.private else ""

    def history_item(self, timezone: str = "UTC") -> HistoryItem:
        sender = self.sender_name()
        if self.chat_kind != ChatKind.private and not self.found.is_outgoing:
            sender = f"{sender} ({self.chat_title})" if sender else self.chat_title
        return HistoryItem(
            id=self.ref,
            is_user=self.found.is_outgoing,
            sender=sender,
            text=self.found.text[:SNIPPET_CHARS],
            sent_at=self.found.sent_at.astimezone(ZoneInfo(timezone)),
            forwarded=self.found.is_forwarded,  # #47: not the sender's own words
            forwarded_from=self.found.forwarded_from,
        )


@dataclass(frozen=True)
class EarlierRef:
    """A ranked match kept by Telegram ids, to fetch again on 🔁 Regenerate (issue #73)."""

    ref: str  # earlier:<n>
    chat_id: int
    message_id: int
    link: LinkReason

    @classmethod
    def of(cls, match: EarlierMatch) -> EarlierRef:
        return cls(match.ref, match.found.chat_id, match.found.message_id, match.link_reason)


@dataclass
class SearchStats:
    chats: int = 0
    queries: int = 0
    requests: int = 0
    matches: int = 0  # raw, before linking and de-duplication
    dropped_unlinked: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class EarlierSearchResult:
    outcome: Outcome
    matches: list[EarlierMatch] = field(default_factory=list)
    stats: SearchStats = field(default_factory=SearchStats)
    queries: tuple[str, ...] = ()


# --- pure helpers ---------------------------------------------------------------------------------


def link_reason(
    found: FoundMessage,
    scope: SearchScope,
    kind: ChatKind,
    reply_targets: Mapping[int, FoundMessage],
    replied_by: Mapping[int, FoundMessage],
) -> LinkReason | None:
    """Why a match may join the draft context, or None to drop it (research S5, FR-S05b)."""
    if kind == ChatKind.private:
        return "private"
    parties = {p for p in (scope.sender_peer_id, scope.user_peer_id) if p is not None}
    if found.is_outgoing or (
        scope.user_peer_id is not None and found.sender_peer_id == scope.user_peer_id
    ):
        return "own"
    if scope.sender_peer_id is not None and found.sender_peer_id == scope.sender_peer_id:
        return "sender"
    target = reply_targets.get(found.reply_to_id) if found.reply_to_id is not None else None
    if target is not None and (target.is_outgoing or target.sender_peer_id in parties):
        return "reply"
    replier = replied_by.get(found.message_id)
    if replier is not None and (replier.is_outgoing or replier.sender_peer_id in parties):
        return "replied_by"
    names = {n.lower().lstrip("@") for n in (scope.sender_username, scope.user_username) if n}
    if found.mentioned_peer_ids & parties or found.mentioned_usernames & names:
        return "mention"
    top = scope.incoming_thread_top_id
    if top is not None and top in (found.thread_top_id, found.reply_to_id, found.message_id):
        return "thread"
    return None


def _by_party(found: FoundMessage, scope: SearchScope) -> bool:
    """Written by the user or by the sender."""
    return found.is_outgoing or (
        found.sender_peer_id is not None
        and found.sender_peer_id in (scope.sender_peer_id, scope.user_peer_id)
    )


def excerpt(text: str, queries: Sequence[str], limit: int = 200) -> str:
    """At most ``limit`` chars of ``text`` around the first query hit (or its start)."""
    if len(text) <= limit:
        return text
    lowered = text.casefold()
    hits = [i for i in (lowered.find(q.casefold()) for q in queries) if i >= 0]
    start = max(0, min(hits) - limit // 4) if hits else 0
    start = min(start, len(text) - limit)
    return text[start : start + limit]


def is_earlier_ref(ref: str) -> bool:
    return ref.startswith(EARLIER_PREFIX)


# --- query planning (issue #108) ------------------------------------------------------------------

MAX_SHARED_STEMS = 2
MIN_STEM = 4
_WORD = re.compile(r"[^\W_]+")  # "brand_guide" is two words, as in a file name
# Russian inflection endings, longest first; English plural and verb endings.
_RU_ENDINGS_TEXT = """
    ами ями ого его ому ему ыми ими иям иях ах ях ов ев ей ом ем ой ий ый ая яя ое ее ые ие ую
    юю ам ям а я ы и у ю о е ь й
"""
_RU_ENDINGS = tuple(sorted(_RU_ENDINGS_TEXT.split(), key=len, reverse=True))
_EN_ENDINGS = ("ing", "ed", "es", "s")
_STOPWORDS_TEXT = """
    the and for with what which where when who how did does our your this that these those are
    was were been from about any into some have has had
    что как где когда какой какая какое какие каким наш наша наше наши ваш ваша это этот тот эта
    для при про или над под без так уже ещё еще
"""
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


def stem(word: str) -> str:
    """A crude prefix of ``word`` that its other word forms share («поставки» → «поставк»,
    "approved" → "approv"); at least ``MIN_STEM`` letters, else ``word`` itself."""
    word = word.casefold()
    is_cyrillic = any("а" <= ch <= "я" or ch == "ё" for ch in word)
    endings = _RU_ENDINGS if is_cyrillic else _EN_ENDINGS
    for ending in endings:
        if ending == "s" and word.endswith("ss"):
            continue
        if word.endswith(ending) and len(word) - len(ending) >= MIN_STEM:
            return word[: -len(ending)]
    return word


def search_terms(queries: Sequence[str]) -> tuple[str, ...]:
    """The queries sent to Telegram: up to ``MAX_SHARED_STEMS`` word stems that several of the
    filter's queries share, then the filter's queries; de-duplicated, at most ``MAX_QUERIES``.

    Telegram's search is word-based (research S3), so a phrase ("workshop venue", «дата
    поставки») finds nothing when the earlier message words it differently («Поставку
    согласовали…», "Workshop booked in…", "brand_guide_v3_approved.pdf"). The words the filter
    repeats across its queries are the topic; their stem is a prefix of every word form. The
    stems push out only the filter's last, least relevant queries: the request budget stays."""
    cleaned = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    seen_in: dict[str, set[int]] = {}
    for index, query in enumerate(cleaned):
        for word in _WORD.findall(query):
            if len(word) < 3 or word.casefold() in _STOPWORDS or word.isdigit():
                continue
            seen_in.setdefault(stem(word), set()).add(index)
    shared = [s for s, where in seen_in.items() if len(where) >= 2]  # first appearance order
    shared.sort(key=lambda s: len(seen_in[s]), reverse=True)  # stable: ties keep that order
    terms: dict[str, str] = {}
    for term in [*shared[:MAX_SHARED_STEMS], *cleaned]:
        terms.setdefault(term.casefold(), term)
    return tuple(terms.values())[:MAX_QUERIES]


# --- service --------------------------------------------------------------------------------------


class EarlierSearch:
    def __init__(
        self,
        gateway: MessagingGateway,
        settings: Settings,
        *,
        clock: Clock = utcnow,
    ) -> None:
        self._gateway = gateway
        self._settings = settings
        self._clock = clock

    @property
    def timeout_s(self) -> float:
        return float(self._settings.earlier_search_timeout_s)

    async def chats_for(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        turn_chat: WorkChat,
        sender_contact_id: uuid.UUID | None,
        sender_peer_id: int | None,
    ) -> list[SearchChat]:
        """The enabled work chats searched for a draft (research S4); never any other chat."""
        chats: list[SearchChat] = []
        if turn_chat.enabled:
            chats.append(SearchChat(turn_chat.telegram_chat_id, turn_chat.title, turn_chat.kind))
        if sender_peer_id is not None and sender_peer_id != turn_chat.telegram_chat_id:
            private = await session.scalar(
                select(WorkChat).where(
                    WorkChat.user_id == user_id,
                    WorkChat.enabled.is_(True),
                    WorkChat.kind == ChatKind.private,
                    WorkChat.telegram_chat_id == sender_peer_id,
                )
            )
            if private is not None:
                chats.append(SearchChat(private.telegram_chat_id, private.title, private.kind))
        if sender_contact_id is not None:
            since = self._clock() - timedelta(days=self._settings.message_retention_days)
            last_seen = func.max(Message.sent_at)
            rows = await session.execute(
                select(WorkChat)
                .join(Message, Message.work_chat_id == WorkChat.id)
                .where(
                    WorkChat.user_id == user_id,
                    WorkChat.enabled.is_(True),
                    WorkChat.kind != ChatKind.private,
                    WorkChat.id != turn_chat.id,
                    Message.user_id == user_id,
                    Message.sender_contact_id == sender_contact_id,
                    Message.sent_at >= since,
                )
                .group_by(WorkChat.id)
                .order_by(last_seen.desc())
                .limit(MAX_EXTRA_GROUPS)
            )
            for group in rows.scalars():
                chats.append(SearchChat(group.telegram_chat_id, group.title, group.kind))
        return chats

    async def search(
        self, scope: SearchScope, chats: Sequence[SearchChat], queries: Sequence[str]
    ) -> EarlierSearchResult:
        queries = search_terms(queries)
        # Never more than MAX_CHATS distinct chats, whatever the caller passes (S4 budget).
        unique: dict[int, SearchChat] = {}
        for chat in chats:
            unique.setdefault(chat.telegram_chat_id, chat)
        chats = list(unique.values())[:MAX_CHATS]
        stats = SearchStats(chats=len(chats), queries=len(queries))
        started = time.monotonic()
        outcome: Outcome
        matches: list[EarlierMatch] = []
        try:
            async with asyncio.timeout(self.timeout_s):
                matches = await self._run(scope, chats, queries, stats)
            outcome = "ok" if matches else "no_match"
        except TimeoutError:
            outcome = "timeout"
        except GatewayBusy:
            outcome = "rate_limited"
        except (GatewayNotStarted, SessionRevoked):
            outcome = "not_started"
        except Exception as exc:  # never break drafting (FR-S08)
            log.warning(
                "earlier_search_failed", user_id=str(scope.user_id), error=type(exc).__name__
            )
            outcome = "failed"
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        if outcome not in ("ok", "no_match"):
            matches = []
        return EarlierSearchResult(outcome=outcome, matches=matches, stats=stats, queries=queries)

    async def refetch(
        self, scope: SearchScope, chats: Sequence[SearchChat], refs: Sequence[EarlierRef]
    ) -> list[EarlierMatch] | None:
        """The matches ``refs`` name, fetched again by id (issue #73: a regenerate reuses the
        ranked selection instead of searching and ranking again). Only chats still among
        ``chats`` are read; deleted messages are left out. None on any failure (the caller
        then searches afresh)."""
        by_chat = {chat.telegram_chat_id: chat for chat in chats}
        wanted: dict[int, dict[int, EarlierRef]] = {}
        for ref in refs:
            if ref.chat_id in by_chat:
                wanted.setdefault(ref.chat_id, {})[ref.message_id] = ref
        matches: list[EarlierMatch] = []
        try:
            async with asyncio.timeout(self.timeout_s):
                for chat_id, by_id in wanted.items():
                    chat = by_chat[chat_id]
                    for found in await self._gateway.get_messages(
                        scope.user_id, chat_id, sorted(by_id)
                    ):
                        kept = by_id.get(found.message_id)
                        if kept is None or not found.text.strip():
                            continue
                        matches.append(
                            EarlierMatch(kept.ref, found, chat.title, chat.kind, kept.link)
                        )
        except Exception as exc:  # never break drafting: search afresh instead
            log.warning(
                "earlier_refetch_failed", user_id=str(scope.user_id), error=type(exc).__name__
            )
            return None
        return sorted(matches, key=lambda m: (m.found.sent_at, m.found.message_id))

    async def _run(
        self,
        scope: SearchScope,
        chats: Sequence[SearchChat],
        queries: Sequence[str],
        stats: SearchStats,
    ) -> list[EarlierMatch]:
        if not chats or not queries:
            return []
        peers = [p for p in (scope.sender_peer_id, scope.user_peer_id) if p is not None]
        names = await self._gateway.get_usernames(scope.user_id, peers) if peers else {}
        scope = replace(
            scope,
            sender_username=names.get(scope.sender_peer_id) if scope.sender_peer_id else None,
            user_username=names.get(scope.user_peer_id) if scope.user_peer_id else None,
        )
        semaphore = asyncio.Semaphore(IN_FLIGHT)

        async def one(chat: SearchChat, query: str) -> list[FoundMessage]:
            async with semaphore:
                stats.requests += 1
                return await self._gateway.search_chat(
                    scope.user_id, chat.telegram_chat_id, query, limit=PER_REQUEST_LIMIT
                )

        pairs = [(chat, query) for chat in chats for query in queries]
        tasks = [asyncio.create_task(one(chat, query)) for chat, query in pairs]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        # Retrieve every failure (no "exception never retrieved" noise), raise the first one.
        errors = [exc for task in done if (exc := task.exception()) is not None]
        if errors:
            raise errors[0]
        by_chat: dict[int, dict[int, FoundMessage]] = {c.telegram_chat_id: {} for c in chats}
        for (chat, _), task in zip(pairs, tasks, strict=True):
            for found in task.result():
                stats.matches += 1
                by_chat[chat.telegram_chat_id].setdefault(found.message_id, found)

        kept: list[EarlierMatch] = []
        for chat in chats:
            found_here = by_chat[chat.telegram_chat_id]
            candidates = {
                mid: f
                for mid, f in found_here.items()
                if f.text.strip() and (chat.telegram_chat_id, mid) not in scope.known
            }
            if not candidates:
                continue
            targets: dict[int, FoundMessage] = dict(found_here)
            if chat.kind != ChatKind.private:
                missing = sorted(
                    {
                        f.reply_to_id
                        for f in candidates.values()
                        if f.reply_to_id is not None and f.reply_to_id not in targets
                    }
                )
                if missing:
                    stats.requests += 1
                    for target in await self._gateway.get_messages(
                        scope.user_id, chat.telegram_chat_id, missing
                    ):
                        targets[target.message_id] = target
            # One hop "replied to by": when several matches reply to the same message, the one
            # written by the sender or the user wins, so a third-party reply cannot hide it.
            replied_by: dict[int, FoundMessage] = {}
            for f in found_here.values():
                if f.reply_to_id is None:
                    continue
                current = replied_by.get(f.reply_to_id)
                if current is None or not _by_party(current, scope):
                    replied_by[f.reply_to_id] = f
            for found in candidates.values():
                reason = link_reason(found, scope, chat.kind, targets, replied_by)
                if reason is None:
                    stats.dropped_unlinked += 1
                    continue
                kept.append(EarlierMatch("", found, chat.title, chat.kind, reason))

        kept.sort(key=lambda m: (m.found.sent_at, m.found.message_id), reverse=True)
        return [
            replace(match, ref=f"{EARLIER_PREFIX}{n}")
            for n, match in enumerate(kept[:MAX_MATCHES], start=1)
        ]


__all__ = [
    "EARLIER_PREFIX",
    "EarlierRef",
    "MAX_CHATS",
    "MAX_MATCHES",
    "EarlierMatch",
    "EarlierSearch",
    "EarlierSearchResult",
    "SearchChat",
    "SearchScope",
    "SearchStats",
    "excerpt",
    "is_earlier_ref",
    "link_reason",
]
