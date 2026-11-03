"""Effective task deadlines from the due field, comments and the description (issue #6).

A task's deadline is often agreed in a comment ("deadline moved to Nov 5", "сдать до пятницы")
or written in the description, not in the tracker's due field. This module turns a
:class:`~scribe.context.tasks.base.Task` (due field, ``due_set_at``, ``description``, last few
``comments``, all filled by the provider behind the ``TaskSource`` interface) into its
*effective* deadline, kept in ``Task.deadline`` with its source, so every consumer of
:meth:`Task.due_on` (conflict backstop, draft context, card labels) uses it.

1. **Deterministic parser** (:func:`find_deadline`, every task, no LLM): explicit dates (ISO,
   ``05.11``, ``5 Nov`` / ``Nov 5`` / ``5 ноября``), weekdays, tomorrow, end of the week / of
   next week / of the month, resolved against the local date of the comment's *own* timestamp
   (the description: the card's creation time), never against now. A date counts as a deadline
   only with a deadline cue: a deadline preposition right before it ("by", "until", "до", "к")
   or a deadline word in the same sentence ("deadline", "due", "ETA", "moved", "перенесли",
   "срок", "сдать"); "call on Friday" is not a deadline. In a sentence with several dates the
   last one directly after a cue wins ("moved from 1 Nov to 5 Nov" -> 5 Nov).
2. **LLM extraction** (:func:`refine_deadlines`, ``extract.deadlines`` on the cheap route): only
   for the tasks the filter route selected for the current turn, only for texts that may hold a
   deadline the parser did not find and that could still change the outcome (newer than the
   newest parsed comment deadline and than the last due change). One call per draft, at most
   :data:`MAX_LLM_TASKS` tasks. An item is kept only when its quote is in the text and its
   confidence is at least :data:`MIN_CONFIDENCE`. Any failure keeps the parsed deadlines.
   Issue #73: the grounded result per task is memoised in process (:class:`DeadlineMemo`) for
   the user's local day, keyed by task id, time zone, due date and the offered texts (id, time
   and a SHA-256 of the text), so a regenerate or the next draft that selects the same task
   with the same comments asks nothing again. Failures are never memoised.
3. **Choice** (:func:`choose_deadline`): the newest comment deadline wins over older comments
   and over the due field when it is newer than the last due change (a due change older than
   every fetched action is "older"); otherwise the due field; otherwise the description.

Comment and description text is third-party content: it is never logged (only counts are) and
never persisted; it reaches an LLM only through the router, whose routes all require a
provider with ``data_use_confirmed`` (``routes.validate_route_table``).
"""

from __future__ import annotations

import calendar
import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from scribe.clock import Clock, utcnow
from scribe.context.commitments import find_dates, format_day
from scribe.context.tasks.base import Deadline, Task
from scribe.llm.provider import DeadlineItem, DeadlineRequest, DeadlineTask, DeadlineText
from scribe.llm.registry import LLMRouter
from scribe.logging import get_logger

log = get_logger("scribe.tasks")

MIN_CONFIDENCE = 0.7
MAX_LLM_TASKS = 10
LLM_TEXT_MAX_CHARS = 600
QUOTE_MAX_CHARS = 160
DESCRIPTION_ID = "desc"

# --- parsing (pure) -----------------------------------------------------------------------------

_MONTHS: tuple[tuple[int, str], ...] = (
    (1, r"jan(?:uary)?|январ[ьяе]"),
    (2, r"feb(?:ruary)?|феврал[ьяе]"),
    (3, r"mar(?:ch)?|март[аеу]?"),
    (4, r"apr(?:il)?|апрел[ьяе]"),
    (5, r"may|ма[йяе]"),
    (6, r"june?|июн[ьяе]"),
    (7, r"july?|июл[ьяе]"),
    (8, r"aug(?:ust)?|август[аеу]?"),
    (9, r"sep(?:t(?:ember)?)?|сентябр[ьяе]"),
    (10, r"oct(?:ober)?|октябр[ьяе]"),
    (11, r"nov(?:ember)?|ноябр[ьяе]"),
    (12, r"dec(?:ember)?|декабр[ьяе]"),
)
_MONTH = "|".join(f"(?:{pattern})" for _, pattern in _MONTHS)
_MONTH_EXACT = [(number, re.compile(f"(?:{p})", re.IGNORECASE)) for number, p in _MONTHS]
_DAY_MONTH_WORD = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th|-?го|-?е)?\s+(?:of\s+)?(?P<month>{_MONTH})\b\.?"
    rf"(?:,?\s+(?P<year>\d{{4}})\b)?",
    re.IGNORECASE,
)
_MONTH_WORD_DAY = re.compile(
    rf"\b(?P<month>{_MONTH})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{{4}})\b)?",
    re.IGNORECASE,
)
_NEXT_WEEK = re.compile(
    r"\b(?:(?:the\s+)?end\s+of\s+next\s+week|next\s+week|"
    r"конц[ауе]\s+следующей\s+недели|следующ(?:ей|ую|ая)\s+недел[еияю])\b",
    re.IGNORECASE,
)
_MONTH_END = re.compile(
    r"\b(?:(?:the\s+)?end\s+of\s+(?:the\s+|this\s+)?month|конц[ауе]\s+(?:этого\s+)?месяца)\b",
    re.IGNORECASE,
)
_CUE_WORDS = (
    r"deadlines?|due(?!\s+to\b)|eta|moved?|moving|postpon\w*|push(?:ed|ing)?|reschedul\w*|shift(?:ed)?|"
    r"deliver\w*|дедлайн\w*|срок\w*|перен[её]с\w*|перенос\w*|сдвин\w*|сдвиг\w*|сдать|сдач\w*|"
    r"сда[её]м"
)
_STRONG_CUE = re.compile(rf"\b(?:{_CUE_WORDS})\b", re.IGNORECASE)
# Directly cued: a deadline preposition right before the date, or a cue word at most two words
# before it ("deadline: 5 Nov", "moving to Nov 5", "перенесли на 5 ноября").
_PREPOSITION_BEFORE = re.compile(
    r"(?:^|[^\w])(?:by|until|till|before|no\s+later\s+than|не\s+позднее|до|к|ко)\s+(?:the\s+)?$",
    re.IGNORECASE,
)
_CUE_BEFORE = re.compile(rf"\b(?:{_CUE_WORDS})\b\W*(?:\w+\W+){{0,2}}$", re.IGNORECASE)
_SENTENCES = re.compile(r"[\n!?;]+|(?<=\.)\s+(?=[A-ZА-ЯЁ])")
_HINT = re.compile(
    rf"{_STRONG_CUE.pattern}|\b(?:{_MONTH})\b|\d{{1,2}}[./]\d{{1,2}}|\bnext\b|следующ|"
    r"\b(?:by|until|до|к)\s+\w",
    re.IGNORECASE,
)
_WORDS = re.compile(r"\w+")


@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    day: date
    kind: str


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_number(word: str) -> int:
    for number, pattern in _MONTH_EXACT:
        if pattern.fullmatch(word.rstrip(".")):
            return number
    raise ValueError(word)  # unreachable: the word matched one of these patterns


def _word_dates(text: str, reference: date) -> list[_Mention]:
    """Dates with a month word ("5 Nov", "Nov 5th", "5 ноября 2026"), next week, month end."""
    found: list[_Mention] = []
    for pattern in (_DAY_MONTH_WORD, _MONTH_WORD_DAY):
        for match in pattern.finditer(text):
            month = _month_number(match.group("month"))
            year_text = match.group("year")
            year = int(year_text) if year_text else reference.year
            day = _safe_date(year, month, int(match.group("day")))
            if day is not None and not year_text and day < reference - timedelta(days=180):
                day = _safe_date(year + 1, month, day.day)
            if day is not None:
                found.append(_Mention(match.start(), match.end(), day, "month_word"))
    for match in _NEXT_WEEK.finditer(text):  # the Friday of the following week
        friday = reference + timedelta(days=7 - reference.weekday() + 4)
        found.append(_Mention(match.start(), match.end(), friday, "next_week"))
    for match in _MONTH_END.finditer(text):
        last = calendar.monthrange(reference.year, reference.month)[1]
        found.append(_Mention(match.start(), match.end(), reference.replace(day=last), "month_end"))
    return found


def _mentions(text: str, reference: date, *, strict: bool) -> list[_Mention]:
    """Every date in ``text``: :func:`commitments.find_dates` plus month words, next week and
    month end; a shorter mention inside a longer one is dropped."""
    found = _word_dates(text, reference) + [
        _Mention(m.start, m.end, m.day, m.kind) for m in find_dates(text, reference, strict=strict)
    ]
    found.sort(key=lambda m: (m.start, -(m.end - m.start)))
    kept: list[_Mention] = []
    for mention in found:
        if kept and mention.start < kept[-1].end:
            continue
        kept.append(mention)
    return kept


def _quote(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= QUOTE_MAX_CHARS else flat[: QUOTE_MAX_CHARS - 1].rstrip() + "…"


def find_deadline(text: str, reference: date) -> tuple[date, str] | None:
    """The deadline ``text`` sets (its date and the sentence that gives it), or None.

    ``reference`` is the local date the text was written: relative dates resolve against it.
    A date counts only with a deadline cue (see the module doc); the last such date wins."""
    best: tuple[date, str] | None = None
    for sentence in _SENTENCES.split(text):
        if not sentence.strip():
            continue
        strong = bool(_STRONG_CUE.search(sentence))
        direct: list[_Mention] = []
        cued: list[_Mention] = []
        for mention in _mentions(sentence, reference, strict=not strong):
            before = sentence[: mention.start]
            is_direct = bool(_PREPOSITION_BEFORE.search(before) or _CUE_BEFORE.search(before))
            if mention.kind == "today" and not strong:
                continue  # "today we agreed…" is not a deadline
            if is_direct:
                direct.append(mention)
            elif strong:
                cued.append(mention)
        chosen = direct or cued
        if chosen:
            best = (chosen[-1].day, _quote(sentence))
    return best


def _local_date(moment: datetime, timezone: str) -> date:
    return moment.astimezone(ZoneInfo(timezone)).date()


# --- choice (pure) ------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeadlineCandidate:
    """A deadline found in one comment or the description."""

    on: date
    source: str  # "comment" | "description"
    quote: str
    text_id: str  # "c<index into task.comments>" or "desc"
    author: str = ""
    noted_at: datetime | None = None

    def to_deadline(self) -> Deadline:
        return Deadline(
            on=self.on,
            source="comment" if self.source == "comment" else "description",
            quote=self.quote,
            author=self.author,
            noted_at=self.noted_at,
        )


def comment_id(index: int) -> str:
    return f"c{index}"


def parsed_candidates(task: Task, timezone: str) -> list[DeadlineCandidate]:
    """The deadlines :func:`find_deadline` finds in the task's comments and description."""
    found: list[DeadlineCandidate] = []
    for index, comment in enumerate(task.comments):
        hit = find_deadline(comment.text, _local_date(comment.at, timezone))
        if hit is not None:
            found.append(
                DeadlineCandidate(
                    on=hit[0],
                    source="comment",
                    quote=hit[1],
                    text_id=comment_id(index),
                    author=comment.author,
                    noted_at=comment.at,
                )
            )
    if task.description:
        anchor = task.description_at
        reference = _local_date(anchor, timezone) if anchor is not None else None
        hit = find_deadline(task.description, reference) if reference is not None else None
        if hit is not None:
            found.append(
                DeadlineCandidate(
                    on=hit[0],
                    source="description",
                    quote=hit[1],
                    text_id=DESCRIPTION_ID,
                    noted_at=anchor,
                )
            )
    return found


def choose_deadline(
    task: Task, timezone: str, candidates: Sequence[DeadlineCandidate]
) -> Deadline | None:
    """The effective deadline: the newest comment deadline when it differs from the due field
    and is newer than the last due change; else the due field; else the description's."""
    due = (
        Deadline(on=task.due_local_date(task.due_at, timezone), source="due", at=task.due_at)
        if task.due_at is not None
        else None
    )
    comments = [c for c in candidates if c.source == "comment" and c.noted_at is not None]
    if comments:
        newest = max(comments, key=lambda c: c.noted_at or datetime.min)
        assert newest.noted_at is not None
        newer_than_due = task.due_set_at is None or newest.noted_at > task.due_set_at
        if due is None or (newer_than_due and newest.on != due.on):
            return newest.to_deadline()
    if due is not None:
        return due
    descriptions = [c for c in candidates if c.source == "description"]
    return descriptions[-1].to_deadline() if descriptions else None


def resolve_deadline(
    task: Task, timezone: str, extracted: Sequence[DeadlineCandidate] = ()
) -> Task:
    """``task`` with ``deadline`` set from the due field, parsed comments / description and
    ``extracted`` (LLM) candidates. Idempotent: always recomputed from the raw fields."""
    deadline = choose_deadline(task, timezone, [*parsed_candidates(task, timezone), *extracted])
    return task.model_copy(update={"deadline": deadline})


def resolve_all(tasks: Sequence[Task], timezone: str) -> list[Task]:
    return [resolve_deadline(task, timezone) for task in tasks]


def deadline_note(deadline: Deadline | None, timezone: str) -> str | None:
    """'from a comment by Ivan Petrov, Sun 18 Oct: “moving to Nov 5”' (None for the due field)."""
    if deadline is None or deadline.source == "due":
        return None
    if deadline.source == "description":
        return f"from the card description: “{deadline.quote}”"
    by = f" by {deadline.author}" if deadline.author else ""
    when = (
        f", {format_day(_local_date(deadline.noted_at, timezone))}"
        if deadline.noted_at is not None
        else ""
    )
    return f"from a comment{by}{when}: “{deadline.quote}”"


# --- LLM extraction -----------------------------------------------------------------------------


def pending_texts(task: Task, timezone: str) -> list[DeadlineText]:
    """Texts worth asking the LLM about: no parsed deadline, a hint of one, and able to change
    the outcome (a comment newer than the newest parsed comment deadline and the last due
    change; the description only when nothing else gives a deadline)."""
    tz = ZoneInfo(timezone)
    parsed = parsed_candidates(task, timezone)
    parsed_ids = {c.text_id for c in parsed}
    newest = max(
        (c.noted_at for c in parsed if c.source == "comment" and c.noted_at is not None),
        default=None,
    )
    texts: list[DeadlineText] = []
    for index, comment in enumerate(task.comments):
        if comment_id(index) in parsed_ids or not _HINT.search(comment.text):
            continue
        if newest is not None and comment.at <= newest:
            continue
        due_changed_later = task.due_set_at is not None and comment.at <= task.due_set_at
        if task.due_at is not None and due_changed_later:
            continue
        texts.append(
            DeadlineText(
                id=comment_id(index),
                kind="comment",
                author=comment.author,
                written_at=comment.at.astimezone(tz),
                text=comment.text[:LLM_TEXT_MAX_CHARS],
            )
        )
    nothing_else = task.due_at is None and newest is None and DESCRIPTION_ID not in parsed_ids
    if (
        nothing_else
        and not texts
        and task.description
        and task.description_at is not None
        and _HINT.search(task.description)
    ):
        texts.append(
            DeadlineText(
                id=DESCRIPTION_ID,
                kind="description",
                written_at=task.description_at.astimezone(tz),
                text=task.description[:LLM_TEXT_MAX_CHARS],
            )
        )
    return texts


def _normalize_words(text: str) -> str:
    return " ".join(_WORDS.findall(text.casefold().replace("ё", "е")))


def ground(item: DeadlineItem, texts: Sequence[DeadlineText]) -> DeadlineCandidate | None:
    """Keep an LLM item only when it names an offered text of its kind, is confident enough,
    quotes that text and gives a plausible date (within a year before / three after it)."""
    text = next((t for t in texts if t.id == item.text_id), None)
    if text is None or text.kind != item.source or item.confidence < MIN_CONFIDENCE:
        return None
    quote = _normalize_words(item.quote)
    if not quote or f" {quote} " not in f" {_normalize_words(text.text)} ":
        return None
    written = text.written_at
    if written is not None and not (
        written.date() - timedelta(days=365)
        <= item.deadline_at
        <= written.date() + timedelta(days=3 * 365)
    ):
        return None
    return DeadlineCandidate(
        on=item.deadline_at,
        source=text.kind,
        quote=_quote(item.quote),
        text_id=text.id,
        author=text.author,
        noted_at=written,  # the same instant as the comment's (or description's) own time
    )


MemoKey = tuple[str, str, date | None, tuple[tuple[str, str, str], ...]]


def memo_key(task: Task, texts: Sequence[DeadlineText], timezone: str) -> MemoKey:
    """(task id, time zone, due date, offered texts). Comment ids are positions (``c<n>``), so
    each text also counts with its time and a digest of its content (never the text itself)."""
    due = task.due_local_date(task.due_at, timezone) if task.due_at is not None else None
    return (
        task.id,
        timezone,
        due,
        tuple(
            (
                text.id,
                text.written_at.isoformat() if text.written_at is not None else "",
                hashlib.sha256(text.text.encode()).hexdigest(),
            )
            for text in texts
        ),
    )


class DeadlineMemo:
    """Grounded ``extract.deadlines`` results per task for the user's local day (issue #73).

    In process only: one instance per database is enforced by the advisory lock, the inputs
    (tracker comments) are not stored in Postgres either, and losing the memo on a restart only
    costs one extra cheap call. Entries from an earlier day never answer; the oldest entries
    are dropped beyond ``max_entries``."""

    def __init__(self, *, clock: Clock = utcnow, max_entries: int = 2000) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[MemoKey, tuple[date, tuple[DeadlineCandidate, ...]]] = {}
        self.hits = 0
        self.misses = 0

    def today(self, timezone: str) -> date:
        return self._clock().astimezone(ZoneInfo(timezone)).date()

    def get(self, key: MemoKey) -> tuple[DeadlineCandidate, ...] | None:
        entry = self._entries.get(key)
        if entry is None or entry[0] != self.today(key[1]):
            self.misses += 1
            return None
        self.hits += 1
        return entry[1]

    def put(self, key: MemoKey, found: Sequence[DeadlineCandidate]) -> None:
        self._entries.pop(key, None)
        if len(self._entries) >= self._max_entries:
            self._entries = {k: v for k, v in self._entries.items() if v[0] == self.today(k[1])}
            while len(self._entries) >= self._max_entries:
                del self._entries[next(iter(self._entries))]
        self._entries[key] = (self.today(key[1]), tuple(found))

    def clear(self) -> None:
        self._entries.clear()
        self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)


_default_memo = DeadlineMemo()


def default_memo() -> DeadlineMemo:
    """The process-wide memo the composer uses."""
    return _default_memo


async def refine_deadlines(
    router: LLMRouter,
    tasks: Sequence[Task],
    timezone: str,
    *,
    memo: DeadlineMemo | None = None,
    user_id: uuid.UUID | None = None,
) -> list[Task]:
    """``tasks`` (the ones selected for the current turn) with LLM-found deadlines applied.

    One ``extract.deadlines`` call for at most :data:`MAX_LLM_TASKS` tasks that have pending
    texts and no answer in ``memo`` today; no call when none has. Never raises: on any failure
    the parsed deadlines stay."""
    asks: list[tuple[Task, list[DeadlineText]]] = []
    for task in tasks:
        texts = pending_texts(task, timezone)
        if texts:
            asks.append((task, texts))
    asks = asks[:MAX_LLM_TASKS]
    extracted: dict[str, list[DeadlineCandidate]] = {}
    if memo is not None:
        missing: list[tuple[Task, list[DeadlineText]]] = []
        for task, texts in asks:
            known = memo.get(memo_key(task, texts, timezone))
            if known is None:
                missing.append((task, texts))
            elif known:
                extracted[task.id] = list(known)
        if len(missing) < len(asks):
            log.info("deadlines_memo_hit", tasks=len(asks) - len(missing), asked=len(missing))
        asks = missing
    if not asks:
        return _apply(tasks, timezone, extracted)
    request = DeadlineRequest(
        timezone=timezone,
        tasks=[
            DeadlineTask(
                id=task.id,
                title=task.title,
                due=task.due_local_date(task.due_at, timezone) if task.due_at is not None else None,
                texts=texts,
            )
            for task, texts in asks
        ],
    )
    try:
        result = await router.extract_deadlines(request, user_id=user_id)
    except Exception as exc:  # optional enrichment: drafting goes on with parsed deadlines
        log.warning("deadline_extraction_failed", error=type(exc).__name__)
        return _apply(tasks, timezone, extracted)
    offered = {task.id: (task, texts) for task, texts in asks}
    fresh: dict[str, list[DeadlineCandidate]] = {}
    for item in result.output:
        if item.task_id not in offered:
            continue
        task, texts = offered[item.task_id]
        candidate = ground(item, texts)
        if candidate is not None:
            fresh.setdefault(task.id, []).append(candidate)
    if memo is not None:  # an empty answer is an answer too: not asked again today
        for task, texts in asks:
            memo.put(memo_key(task, texts, timezone), fresh.get(task.id, []))
    log.info(
        "deadlines_extracted",
        tasks=len(asks),
        texts=sum(len(texts) for _, texts in asks),
        items=len(result.output),
        kept=sum(len(found) for found in fresh.values()),
    )
    return _apply(tasks, timezone, {**extracted, **fresh})


def _apply(
    tasks: Sequence[Task], timezone: str, extracted: dict[str, list[DeadlineCandidate]]
) -> list[Task]:
    return [
        resolve_deadline(task, timezone, extracted[task.id]) if task.id in extracted else task
        for task in tasks
    ]


__all__ = [
    "DESCRIPTION_ID",
    "MAX_LLM_TASKS",
    "MIN_CONFIDENCE",
    "DeadlineCandidate",
    "DeadlineMemo",
    "choose_deadline",
    "comment_id",
    "default_memo",
    "deadline_note",
    "find_deadline",
    "ground",
    "memo_key",
    "parsed_candidates",
    "pending_texts",
    "refine_deadlines",
    "resolve_all",
    "resolve_deadline",
]
