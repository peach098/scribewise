"""Commitments: promises extracted from the user's own outgoing messages (FR-031, research R9).

Extraction runs at most once per outgoing message in an enabled work chat:

- a **manual** message (typed by the user on any device) when ``intake/events.py`` handles the
  outgoing update and it is new and not an agent send;
- an **agent send** in ``sending/send.py`` right after the send is committed. Telethon's echo of
  that send is recognised as an agent send by ``intake/events.py`` and is *not* extracted again.

Issue #72: the ``extract.commitments`` call is skipped for a message with no promise cue
(:mod:`scribe.context.commitment_gate`), and for an approved draft sent unchanged whose
``creates_commitment`` flag is false. Every decision is logged without content.

The database work is awaited by the caller after its own commit (short reads, plus a contact
upsert for a chat nobody wrote in yet); the ``extract.commitments`` LLM call and the insert run
as background tasks (bounded by :data:`EXTRACT_CONCURRENCY`), so sending and intake never wait
for the LLM, and any failure is logged (no content) and dropped.

Only items with ``confidence >= 0.7`` whose ``evidence`` quotes the outgoing text are stored
(Principle III: a promise lifted from ``recent_incoming`` is not the user's), with ``due`` only
when the deterministic date extractor finds that date in the outgoing text; as ``open`` rows
with ``what`` and the source quote (both encrypted) and the send time. An item that equals an
open commitment to the same contact on (normalised ``what``, ``due``) is a duplicate and skipped;
the dedupe read runs under a per-(user, contact) advisory lock. The contact is the chat's contact:
the person in a private chat, the group as a whole in a group chat (as for drafting).

:func:`open_for` loads open commitments for drafting (ids ``commit:<uuid>``);
:func:`requested_dates` + :func:`find_conflicts` are the deterministic conflict backstop used by
the composer when the model does not report a conflict itself (conservative: one unambiguous
requested date, no bare "today", nothing in the past, ordinary words such as "sat" or "1.10"
are not dates).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.context.commitment_gate import GateDecision, log_decision, needs_extraction
from scribe.context.history import forward_fields
from scribe.context.tasks.base import is_done_status
from scribe.context.tasks.jira.jql import ISSUE_KEY
from scribe.db.models import ChatKind, Commitment, Contact, ContactKind, TaskProvider, WorkChat
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.db.repos import users as users_repo
from scribe.llm.provider import (
    CommitmentItem,
    CommitmentRef,
    CommitmentRequest,
    Conflict,
    MessageItem,
    TaskItem,
)
from scribe.llm.registry import LLMRouter
from scribe.logging import get_logger

log = get_logger("scribe.commitments")

MIN_CONFIDENCE = 0.7
COMMIT_PREFIX = "commit:"
RECENT_INCOMING = 5
RECENT_WINDOW = timedelta(days=2)
EXTRACT_CONCURRENCY = 4

DedupeKey = tuple[uuid.UUID, str, date | None]


# --- ids ----------------------------------------------------------------------------------------


def commitment_ref(commitment_id: uuid.UUID) -> str:
    return f"{COMMIT_PREFIX}{commitment_id}"


def parse_commitment_ref(ref: str) -> uuid.UUID | None:
    if not ref.startswith(COMMIT_PREFIX):
        return None
    try:
        return uuid.UUID(ref[len(COMMIT_PREFIX) :])
    except ValueError:
        return None


def to_ref(commitment: Commitment) -> CommitmentRef:
    return CommitmentRef(
        id=commitment_ref(commitment.id),
        what=commitment.what,
        due=commitment.due,
        issue_key=commitment.jira_issue_key,
        due_at=commitment.source_sent_at,
        source_note=_source_note(commitment),
    )


def _source_note(commitment: Commitment) -> str | None:
    """Where the promise was made, for the draft context (no quotes of restricted comments)."""
    if commitment.jira_comment_id and commitment.jira_issue_key:
        return f"your Jira comment on {commitment.jira_issue_key}"
    if commitment.jira_issue_key:
        return "your Telegram message"
    return None


# --- store rule (pure) --------------------------------------------------------------------------

_EDGE_PUNCTUATION = re.compile(r"^[\W_]+|[\W_]+$")


def normalize_what(what: str) -> str:
    return _EDGE_PUNCTUATION.sub("", " ".join(what.split()).casefold())


def dedupe_key(contact_id: uuid.UUID, what: str, due: date | None) -> DedupeKey:
    return (contact_id, normalize_what(what), due)


_WORDS = re.compile(r"\w+")


def _normalize_words(text: str) -> str:
    """Lower-cased words separated by single spaces (punctuation and spacing ignored)."""
    return " ".join(_WORDS.findall(text.casefold().replace("ё", "е")))


def ground_item(item: CommitmentItem, source_text: str, local_date: date) -> CommitmentItem | None:
    """Principle III: keep what the outgoing message itself supports.

    The item's ``evidence`` must be a quote of ``source_text`` (words compared case- and
    punctuation-insensitively), else the item is dropped: a promise taken from
    ``recent_incoming`` or invented is not the user's. A ``due`` the deterministic extractor
    cannot find in ``source_text`` (resolved against the message's local date, so "в четверг",
    "завтра", "до конца недели" count) is dropped and the item kept without a date."""
    evidence = _normalize_words(item.evidence)
    if not evidence or f" {evidence} " not in f" {_normalize_words(source_text)} ":
        return None
    if item.due is not None and item.due not in extract_dates(source_text, local_date):
        return item.model_copy(update={"due": None})
    return item


def select_storable(
    items: Iterable[CommitmentItem],
    *,
    contact_id: uuid.UUID,
    existing: set[DedupeKey],
    source_text: str | None = None,
    local_date: date | None = None,
) -> list[CommitmentItem]:
    """Items to store: confident enough, with a ``what``, grounded in ``source_text`` (when
    given, see :func:`ground_item`) and not a duplicate of an open commitment (``existing``) or
    of an earlier item of the same extraction."""
    seen = set(existing)
    keep: list[CommitmentItem] = []
    for item in items:
        if item.confidence < MIN_CONFIDENCE or not normalize_what(item.what):
            continue
        if source_text is not None and local_date is not None:
            grounded = ground_item(item, source_text, local_date)
            if grounded is None:
                continue
            item = grounded
        key = dedupe_key(contact_id, item.what, item.due)
        if key in seen:
            continue
        seen.add(key)
        keep.append(item.model_copy(update={"what": " ".join(item.what.split())}))
    return keep


async def store_items(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    contact_id: uuid.UUID,
    items: Sequence[CommitmentItem],
    source_text: str,
    sent_at: datetime,
) -> list[Commitment]:
    """Store the grounded, confident, new items. ``sent_at`` is in the user's time zone (its
    date resolves "Thursday"). Concurrent stores for the same (user, contact) are serialised by
    a transaction-scoped advisory lock taken before the dedupe read."""
    await session.execute(
        sql_text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"{user_id}:{contact_id}"}
    )
    open_now = await commitments_repo.list_open_for_contacts(session, user_id, [contact_id])
    existing = {dedupe_key(contact_id, c.what, c.due) for c in open_now}  # all to contact_id
    storable = select_storable(
        items,
        contact_id=contact_id,
        existing=existing,
        source_text=source_text,
        local_date=sent_at.date(),
    )
    stored = []
    for item in storable:
        stored.append(
            await commitments_repo.add_commitment(
                session,
                user_id,
                contact_id=contact_id,
                what=item.what,
                due=item.due,
                source_sent_at=sent_at,
                confidence=round(item.confidence, 2),
                source_message_text=source_text,
            )
        )
    return stored


async def open_for(
    session: AsyncSession, user_id: uuid.UUID, contact_ids: Sequence[uuid.UUID]
) -> list[Commitment]:
    """Open commitments to the given contacts (the turn's contact, plus the person who wrote in
    a group chat)."""
    unique = list(dict.fromkeys(contact_ids))
    return await commitments_repo.list_open_for_contacts(session, user_id, unique)


# --- dates and conflicts (pure) -----------------------------------------------------------------

# Full weekday names are dates on their own; the bare 3-letter English abbreviations ("sat",
# "sun", "wed") are ordinary words too and count only after a date preposition ("on Sat").
_WEEKDAYS: tuple[tuple[int, str], ...] = (
    (0, r"monday|понедельник\w*"),
    (1, r"tuesday|tues|вторник\w*"),
    (2, r"wednesday|сред[аеуы]"),
    (3, r"thursday|thurs|thur|четверг\w*"),
    (4, r"friday|пятниц[аеуы]"),
    (5, r"saturday|суббот[аеуы]"),
    (6, r"sunday|воскресень[еяю]"),
)
_WEEKDAY_ABBREVIATIONS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RELATIVE: tuple[tuple[str, str], ...] = (
    ("after_tomorrow", r"day after tomorrow|послезавтра"),
    ("tomorrow", r"tomorrow|завтра"),
    ("week_end", r"(?:the\s+)?end\s+of\s+(?:the\s+|this\s+)?week|конц[ауе]\s+(?:этой\s+)?недели"),
    ("day_end", r"(?:the\s+)?end\s+of\s+(?:the\s+)?day|конц[ауе]\s+дня"),
    ("today", r"today|сегодня"),
)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(r"(?<![\d.\-/])(\d{1,2})[./](\d{2})(?:[./](\d{4}|\d{2}))?(?![\d\-]|[./]\d)")
_TOKEN = re.compile(
    "|".join(
        [f"(?P<wd{n}>\\b(?:{p})\\b)" for n, p in _WEEKDAYS]
        + [f"(?P<ab{n}>\\b{a}\\b)" for n, a in enumerate(_WEEKDAY_ABBREVIATIONS)]
        + [f"(?P<rel_{name}>\\b(?:{p})\\b)" for name, p in _RELATIVE]
    ),
    re.IGNORECASE,
)
# "on Sat", "by 20.10", "до 21.10", "к пятнице", "в среду", "во вторник".
_DATE_PREPOSITION_BEFORE = re.compile(
    r"(?:^|[^\w])(?:on|by|until|till|before|до|к|ко|в|во)\s+$", re.IGNORECASE
)
_MONTH_WORD_AFTER = re.compile(
    r"^\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
    re.IGNORECASE,
)
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class DateMention:
    start: int
    end: int
    day: date
    kind: str  # "iso", "day_month", "weekday", "today", "tomorrow", "after_tomorrow", ...


def format_day(day: date) -> str:
    """``Thu 15 Oct`` (locale independent)."""
    return f"{_DAY_NAMES[day.weekday()]} {day.day} {_MONTH_NAMES[day.month - 1]}"


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _after_preposition(text: str, start: int) -> bool:
    return bool(_DATE_PREPOSITION_BEFORE.search(text[:start]))


def _relative_day(kind: str, reference: date) -> date:
    if kind == "after_tomorrow":
        return reference + timedelta(days=2)
    if kind == "tomorrow":
        return reference + timedelta(days=1)
    if kind == "week_end":  # that Friday (the coming one on a weekend)
        return reference + timedelta(days=(4 - reference.weekday()) % 7)
    return reference  # today, end of day


def find_dates(text: str, reference: date, *, strict: bool = True) -> list[DateMention]:
    """Every date mention in ``text`` resolved against ``reference`` (the message's local date),
    in order of appearance: ISO dates; ``dd.mm.yyyy``; ``dd.mm`` / ``dd/mm`` only after a date
    preposition or before a month word ("version 1.10" is not a date); weekday names (the next
    such day, today included), the 3-letter English abbreviations only after a date preposition;
    today / tomorrow / the day after / end of the day / end of the week; English and Russian.
    ``strict=False`` (input that is known to be a date, e.g. a commitment edit) also accepts a
    bare ``dd.mm`` and the abbreviations."""
    found: list[DateMention] = []
    for match in _ISO.finditer(text):
        day = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if day is not None:
            found.append(DateMention(match.start(), match.end(), day, "iso"))
    for match in _DAY_MONTH.finditer(text):
        year_text = match.group(3)
        if (
            strict
            and not year_text
            and not (
                _after_preposition(text, match.start())
                or _MONTH_WORD_AFTER.match(text[match.end() :])
            )
        ):
            continue
        year = reference.year
        if year_text:
            year = int(year_text) + (2000 if len(year_text) == 2 else 0)
        day = _safe_date(year, int(match.group(2)), int(match.group(1)))
        if day is not None and not year_text and day < reference - timedelta(days=180):
            day = _safe_date(year + 1, day.month, day.day)
        if day is not None:
            found.append(DateMention(match.start(), match.end(), day, "day_month"))
    for match in _TOKEN.finditer(text):
        group = match.lastgroup or ""
        if group.startswith("rel_"):
            kind = group[len("rel_") :]
            found.append(
                DateMention(match.start(), match.end(), _relative_day(kind, reference), kind)
            )
            continue
        if group.startswith("ab") and strict and not _after_preposition(text, match.start()):
            continue
        number = int(group[2:])
        day = reference + timedelta(days=(number - reference.weekday()) % 7)
        found.append(DateMention(match.start(), match.end(), day, "weekday"))
    return sorted(found, key=lambda mention: mention.start)


_PREPOSITION_WORDS = frozenset({"on", "by", "until", "till", "before", "до", "к", "ко", "в", "во"})


def date_only(text: str, reference: date) -> date | None:
    """The date when the *whole* of ``text`` is one date expression ("пятница", "в четверг",
    "by 22.10"), else None ("send today" is a text with a date in it)."""
    mentions = find_dates(text, reference, strict=False)
    if len(mentions) != 1:
        return None
    (mention,) = mentions
    rest = _WORDS.findall((text[: mention.start] + " " + text[mention.end :]).casefold())
    return mention.day if all(word in _PREPOSITION_WORDS for word in rest) else None


def extract_dates(text: str, reference: date, *, strict: bool = True) -> list[date]:
    """The days :func:`find_dates` finds, in order of appearance, without repeats."""
    mentions = find_dates(text, reference, strict=strict)
    return list(dict.fromkeys(mention.day for mention in mentions))


def requested_dates(text: str, reference: date) -> list[date]:
    """Dates an incoming message asks for, for the conflict backstop: as :func:`extract_dates`,
    without a bare "today" / "сегодня" ("today we agreed…" is not a deadline) and without days
    before ``reference`` (a past date is not a request)."""
    days = [
        mention.day
        for mention in find_dates(text, reference)
        if mention.kind != "today" and mention.day >= reference
    ]
    return list(dict.fromkeys(days))


def find_conflicts(
    incoming_dates: Sequence[date],
    commitments: Sequence[CommitmentRef],
    tasks: Sequence[TaskItem],
) -> list[Conflict]:
    """Conflicts between a requested date and open commitments / task deadlines: the request
    asks for something *earlier* than the date already promised (commitment) or planned (task).
    Only an unambiguous request counts: exactly one requested date (several dates need the
    semantics only the model has). Items without a due date and finished tasks (a list named
    like Done / Готово / Closed) never conflict. Same-day double-booking of two different
    deliverables is left to the model."""
    requested_days = set(incoming_dates)
    if len(requested_days) != 1:
        return []
    (requested,) = requested_days
    conflicts: list[Conflict] = []
    for commitment in commitments:
        if commitment.due is not None and requested < commitment.due:
            conflicts.append(
                Conflict(
                    with_kind="commitment",
                    with_id=commitment.id,
                    description=(
                        f"asked for {format_day(requested)}, but you promised "
                        f"“{commitment.what}” for {format_day(commitment.due)}"
                    ),
                )
            )
    for task in tasks:
        if task.due is not None and requested < task.due and not is_done_status(task.status):
            conflicts.append(
                Conflict(
                    with_kind="task",
                    with_id=task.id,
                    description=(
                        f"asked for {format_day(requested)}, but task “{task.title}” is due "
                        f"{format_day(task.due)}"
                    ),
                )
            )
    return conflicts


# --- extraction ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class OutgoingForExtraction:
    """Everything the background extraction needs, loaded before it starts."""

    user_id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str
    text: str
    sent_at: datetime  # in the user's time zone, so weekdays resolve to the user's dates
    recent_incoming: tuple[MessageItem, ...]


async def chat_contact(session: AsyncSession, user_id: uuid.UUID, chat: WorkChat) -> Contact:
    """The contact an outgoing message in ``chat`` goes to (created when nobody wrote yet)."""
    if chat.kind != ChatKind.private:
        return await chats_repo.upsert_contact(
            session,
            user_id,
            telegram_peer_id=chat.telegram_chat_id,
            kind=ContactKind.group,
            display_name=chat.title,
        )
    contact = await chats_repo.get_contact_by_peer(session, user_id, chat.telegram_chat_id)
    if contact is not None:
        return contact
    recent = await messages_repo.list_recent_in_chat(
        session, user_id, chat.id, since=datetime.min.replace(tzinfo=ZoneInfo("UTC")), limit=50
    )
    for message in recent:
        if not message.is_outgoing and message.sender_contact_id is not None:
            sender = await chats_repo.get_contact(session, user_id, message.sender_contact_id)
            if sender is not None:
                return sender
    return await chats_repo.upsert_contact(
        session,
        user_id,
        telegram_peer_id=chat.telegram_chat_id,
        kind=ContactKind.person,
        display_name=chat.title,
    )


async def prepare_outgoing(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    work_chat_id: uuid.UUID,
    text: str,
    sent_at: datetime,
    contact_id: uuid.UUID | None = None,
) -> OutgoingForExtraction | None:
    user = await users_repo.get_user(session, user_id)
    chat = await chats_repo.get_work_chat(session, user_id, work_chat_id)
    if user is None or chat is None or not chat.enabled:
        return None
    contact = (
        await chats_repo.get_contact(session, user_id, contact_id)
        if contact_id is not None
        else None
    ) or await chat_contact(session, user_id, chat)
    recent = await messages_repo.list_recent_in_chat(
        session, user_id, chat.id, since=sent_at - RECENT_WINDOW, limit=RECENT_INCOMING * 4
    )
    incoming = [m for m in recent if not m.is_outgoing and m.sent_at <= sent_at and m.text.strip()]
    names: dict[uuid.UUID, str] = {}
    items: list[MessageItem] = []
    for message in reversed(incoming[:RECENT_INCOMING]):  # oldest first
        sender = message.sender_contact_id
        if sender is not None and sender not in names:
            found = await chats_repo.get_contact(session, user_id, sender)
            names[sender] = found.display_name if found is not None else ""
        items.append(
            MessageItem(
                id=f"msg:{message.id}",
                sender=names.get(sender, "") if sender is not None else "",
                text=message.text,
                sent_at=message.sent_at.astimezone(ZoneInfo(user.timezone)),
                **forward_fields(message),
            )
        )
    return OutgoingForExtraction(
        user_id=user_id,
        contact_id=contact.id,
        contact_name=contact.display_name,
        text=text,
        sent_at=sent_at.astimezone(ZoneInfo(user.timezone)),
        recent_incoming=tuple(items),
    )


class CommitmentExtractor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm_router: LLMRouter,
        *,
        clock: Clock = utcnow,
        concurrency: int = EXTRACT_CONCURRENCY,
        run_inline: bool = False,
    ) -> None:
        """``run_inline`` awaits the extraction inside :meth:`submit` instead of starting a
        background task: only for tests whose sessions share one database connection."""
        self._session_factory = session_factory
        self._router = llm_router
        self._clock = clock
        self._run_inline = run_inline
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(
        self,
        user_id: uuid.UUID,
        *,
        work_chat_id: uuid.UUID,
        text: str,
        sent_at: datetime,
        contact_id: uuid.UUID | None = None,
        draft_commitment: bool | None = None,
    ) -> None:
        """Load what the extraction needs, then start it in the background. Never raises.

        Issue #72: ``draft_commitment`` is the ``creates_commitment`` flag of an approved draft
        sent unchanged (None for anything else). False skips the extraction; True skips the
        pre-filter but still extracts, since the flag carries no grounded item (no evidence,
        confidence or ``to_whom``; ``due`` not checked). Without it, :func:`needs_extraction`
        decides from the text and the recent incoming messages it may answer."""
        if not text.strip():
            return
        if draft_commitment is False:
            log_decision(user_id, GateDecision(False, "draft_flag_false"), source="telegram")
            return
        try:
            async with self._session_factory() as session:
                job = await prepare_outgoing(
                    session,
                    user_id,
                    work_chat_id=work_chat_id,
                    text=text,
                    sent_at=sent_at,
                    contact_id=contact_id,
                )
                await session.commit()  # a contact created for a chat nobody wrote in yet
        except Exception as exc:
            log.warning(
                "commitment_extraction_failed", user_id=str(user_id), error=type(exc).__name__
            )
            return
        if job is None:
            return
        decision = (
            GateDecision(True, "draft_flag_true")
            if draft_commitment
            else needs_extraction(text, (m.text for m in job.recent_incoming))
        )
        log_decision(user_id, decision, source="telegram")
        if not decision.extract:
            return
        if self._run_inline:
            await self._run(job)
        else:
            self.schedule(job)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @property
    def pending(self) -> int:
        """Background extractions started and not finished yet."""
        return len(self._tasks)

    def cancel_pending(self) -> int:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        return len(tasks)

    def schedule(self, job: OutgoingForExtraction) -> None:
        task = asyncio.create_task(self._run(job), name="extract-commitments")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every extraction started so far (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, job: OutgoingForExtraction) -> None:
        async with self._semaphore:
            try:
                await self.extract_from_outgoing(job)
            except Exception as exc:  # never reaches sending or intake
                log.warning(
                    "commitment_extraction_failed",
                    user_id=str(job.user_id),
                    error=type(exc).__name__,
                )

    async def extract_from_outgoing(self, job: OutgoingForExtraction) -> list[Commitment]:
        result = await self._router.extract_commitments(
            CommitmentRequest(
                outgoing_text=job.text,
                contact_display_name=job.contact_name,
                sent_at=job.sent_at,
                recent_incoming=list(job.recent_incoming),
            ),
            user_id=job.user_id,
        )
        async with self._session_factory() as session:
            stored = await store_items(
                session,
                job.user_id,
                contact_id=job.contact_id,
                items=result.output,
                source_text=job.text,
                sent_at=job.sent_at,
            )
            if stored:
                await _link_named_issue(session, job, stored)
            await session.commit()
        log.info(
            "commitments_extracted",
            user_id=str(job.user_id),
            items=len(result.output),
            stored=len(stored),
        )
        return stored


async def _link_named_issue(
    session: AsyncSession, job: OutgoingForExtraction, stored: Sequence[Commitment]
) -> None:
    """A Telegram promise naming an issue of the user's selected Jira projects ("PAY-142 will
    be ready Wednesday") is about that issue (specs/003 J10): its date is then compared with
    the issue's (J12)."""
    conn = await tasks_conn_repo.get_connection(session, job.user_id)
    if conn is None or conn.provider != TaskProvider.jira:
        return
    projects = {str(k) for k in (conn.config_json or {}).get("project_keys") or []}
    keys = [k for k in ISSUE_KEY.findall(job.text) if k.rsplit("-", 1)[0] in projects]
    if not keys:
        return
    for commitment in stored:
        commitment.jira_issue_key = keys[0]
    await session.flush()


# --- process-wide default -------------------------------------------------------------------------
# One extractor per process, created by the first ``Composer`` (the one component built with the
# session factory and the LLM router) through :func:`ensure_default_extractor`; later composers
# with the same session factory (every 🔁 Regenerate builds one) reuse it. ``send.py`` and
# ``intake/events.py`` use it unless one is passed; the service drains it on shutdown
# (:func:`drain_default_extractor`) before disposing of the database engine. The service creates
# it explicitly at startup (``__main__.Service.start``) and hands it to its composer; the holder
# stays because the composer each 🔁 Regenerate builds, ``send.py`` and ``intake/events.py`` are
# not built by the service and must share that one instance (one drain on shutdown).

_default: CommitmentExtractor | None = None
DRAIN_TIMEOUT_S = 10.0


def install_default_extractor(extractor: CommitmentExtractor) -> None:
    """Replace the process default (tests; production uses :func:`ensure_default_extractor`)."""
    global _default
    _default = extractor


def ensure_default_extractor(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    *,
    clock: Clock = utcnow,
) -> CommitmentExtractor:
    """The process default for ``session_factory``, created and installed on first use.

    Idempotent: a default built for the same session factory is returned as is (its pending
    extractions are kept). A caller with another session factory gets its own extractor, which
    does not replace the default."""
    global _default
    if _default is not None and _default.session_factory is session_factory:
        return _default
    extractor = CommitmentExtractor(session_factory, llm_router, clock=clock)
    if _default is None:
        _default = extractor
    return extractor


def default_extractor() -> CommitmentExtractor | None:
    return _default


def reset_default_extractor() -> None:
    global _default
    _default = None


async def drain_default_extractor(timeout_s: float = DRAIN_TIMEOUT_S) -> int:
    """Shutdown: wait up to ``timeout_s`` for the default extractor's background extractions,
    then cancel what is left. Returns how many were pending. An extraction cut off here (or by
    a crash mid-call) is lost: the message is not extracted again (documented gap)."""
    extractor = _default
    pending = extractor.pending if extractor is not None else 0
    if extractor is None or not pending:
        log.info("commitment_extractions_drained", pending=0, cancelled=0)
        return 0
    cancelled = 0
    try:
        await asyncio.wait_for(extractor.drain(), timeout_s)
    except TimeoutError:
        cancelled = extractor.cancel_pending()
    log.info("commitment_extractions_drained", pending=pending, cancelled=cancelled)
    return pending


__all__ = [
    "COMMIT_PREFIX",
    "MIN_CONFIDENCE",
    "CommitmentExtractor",
    "OutgoingForExtraction",
    "chat_contact",
    "commitment_ref",
    "dedupe_key",
    "date_only",
    "default_extractor",
    "drain_default_extractor",
    "ensure_default_extractor",
    "find_dates",
    "ground_item",
    "requested_dates",
    "extract_dates",
    "find_conflicts",
    "format_day",
    "install_default_extractor",
    "normalize_what",
    "open_for",
    "parse_commitment_ref",
    "prepare_outgoing",
    "reset_default_extractor",
    "select_storable",
    "store_items",
    "to_ref",
]
