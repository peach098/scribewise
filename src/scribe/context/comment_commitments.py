"""Commitments from the user's own Jira comments (specs/003-jira-commitments J9 – J11).

:func:`extract_from_comment` handles one new or edited comment written by the user:

1. **Extraction** reuses ``extract.commitments`` (prompt v3) with ``source="jira_comment"``:
   ``outgoing_text`` is the comment without quoted text, ``recent_incoming`` at most the one
   request comment it may answer, ``contact_display_name`` the counterparty (display names
   only; no account ids or emails leave for the LLM).
2. **Grounding** is the Telegram rule (:func:`~scribe.context.commitments.ground_item`:
   evidence quoted from the user's comment, confidence ≥ 0.7, ``due`` only when the
   deterministic date finder finds that date) except that a ``due`` may also come from the
   answered request, resolved against the request's own date (an accepted request). The most
   confident grounded item is kept: one open commitment per comment.
3. **Storing** under a per-(user, comment) advisory lock: a comment of a row the user closed
   is never stored again; a commitment already linked to the comment is updated in place (an
   edit) unless the user changed it; an edit that no longer promises anything closes a
   Jira-only row as ``source_changed`` (or only unlinks a merged Telegram row); otherwise the
   item is **merged** into a matching open commitment (same counterparty, same due, same or
   ≥ 0.6 overlapping words) or stored as a new Jira-sourced commitment.

:func:`resolve_for_issue`, :func:`mark_comment_gone` and :func:`mark_issue_unreachable` are
the automatic state changes of research J11; every change is logged with ids and the note only.
Comment text is never logged.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.context.commitment_gate import GateDecision, log_decision, needs_extraction
from scribe.context.commitments import MIN_CONFIDENCE, extract_dates, ground_item, normalize_what
from scribe.context.tasks.jira.jql import ISSUE_KEY
from scribe.db.models import (
    Commitment,
    CommitmentAutoNote,
    CommitmentClosedBy,
    CommitmentSourceKind,
    CommitmentState,
    Contact,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import jira_cursor
from scribe.llm.provider import CommitmentItem, CommitmentRequest, IssueInfo, MessageItem
from scribe.llm.registry import LLMRouter
from scribe.logging import get_logger

log = get_logger("scribe.commitments")

EXCERPT_MAX_CHARS = 280
MERGE_OVERLAP = 0.6
Outcome = Literal["created", "updated", "merged", "none", "skipped", "closed"]


@dataclass(frozen=True)
class CommentCommitmentInput:
    """One of the user's Jira comments, ready for extraction (built by the comment sync)."""

    user_id: uuid.UUID
    issue_key: str
    issue_title: str
    comment_id: str
    comment_url: str
    comment_text: str  # the user's own words, quotes removed
    restricted: bool
    posted_at: datetime
    updated_at: datetime
    counterparty: str  # mentioned person, else the request's author, else the issue reporter
    request_text: str | None = None
    request_author: str | None = None
    request_at: datetime | None = None


# --- pure helpers -------------------------------------------------------------------------------


def fold_name(name: str) -> str:
    """Case- and diacritics-insensitive form of a person's name."""
    decomposed = unicodedata.normalize("NFKD", name.casefold().replace("ё", "е"))
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).split())


_WORDS = re.compile(r"\w+")
# Words that say nothing about *what* was promised: shared articles or prepositions alone must
# never make two promises "the same" ("send the report" / "send the invoice").
_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "to",
        "of",
        "for",
        "on",
        "by",
        "in",
        "at",
        "with",
        "and",
        "or",
        "my",
        "your",
        "our",
        "it",
        "this",
        "that",
    ]
    + [
        "и",
        "в",
        "во",
        "на",
        "к",
        "ко",
        "по",
        "для",
        "до",
        "с",
        "со",
        "от",
        "о",
        "об",
        "за",
        "из",
        "у",
        "мой",
        "мою",
        "твой",
        "это",
        "эту",
    ]
)


def _words(text: str) -> set[str]:
    return {w for w in _WORDS.findall(fold_name(text)) if w not in _STOP_WORDS}


def same_promise(a_what: str, b_what: str) -> bool:
    """Equal normalized ``what``, or at least :data:`MERGE_OVERLAP` of the smaller set of
    content words in common and at least two shared content words (``send the logs`` / ``send
    logs to Ivan``; not ``send the invoice`` / ``send the load test report``)."""
    if normalize_what(a_what) == normalize_what(b_what):
        return True
    a, b = _words(a_what), _words(b_what)
    shared = a & b
    if len(shared) < 2:
        return False
    return len(shared) / min(len(a), len(b)) >= MERGE_OVERLAP


def issue_keys_in(text: str, project_keys: Iterable[str]) -> list[str]:
    """Issue keys named in ``text`` that belong to the selected projects (first one first)."""
    projects = set(project_keys)
    found = [key for key in ISSUE_KEY.findall(text or "") if key.rsplit("-", 1)[0] in projects]
    return list(dict.fromkeys(found))


def excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT_MAX_CHARS else text[: EXCERPT_MAX_CHARS - 1] + "…"


def ground_comment_item(
    item: CommitmentItem, job: CommentCommitmentInput, timezone: str
) -> CommitmentItem | None:
    """:func:`ground_item` on the comment; a ``due`` the comment does not give is kept when the
    answered request gives it (resolved against the request's local date)."""
    zone = ZoneInfo(timezone)
    grounded = ground_item(item, job.comment_text, job.posted_at.astimezone(zone).date())
    if grounded is None:
        return None
    if grounded.due is None and item.due is not None and job.request_text and job.request_at:
        request_day = job.request_at.astimezone(zone).date()
        if item.due in extract_dates(job.request_text, request_day):
            return grounded.model_copy(update={"due": item.due})
    return grounded


def best_item(
    items: Sequence[CommitmentItem], job: CommentCommitmentInput, timezone: str
) -> CommitmentItem | None:
    kept = [
        grounded
        for item in items
        if item.confidence >= MIN_CONFIDENCE and normalize_what(item.what)
        if (grounded := ground_comment_item(item, job, timezone)) is not None
    ]
    return max(kept, key=lambda i: i.confidence) if kept else None


def request_for(job: CommentCommitmentInput, timezone: str) -> CommitmentRequest:
    zone = ZoneInfo(timezone)
    incoming = (
        [
            MessageItem(
                id="jira:request",
                sender=job.request_author or "",
                text=job.request_text,
                sent_at=job.request_at.astimezone(zone) if job.request_at else None,
            )
        ]
        if job.request_text
        else []
    )
    return CommitmentRequest(
        outgoing_text=job.comment_text,
        contact_display_name=job.counterparty,
        sent_at=job.posted_at.astimezone(zone),
        recent_incoming=incoming,
        source="jira_comment",
        issue=IssueInfo(key=job.issue_key, title=job.issue_title),
    )


# --- storing ------------------------------------------------------------------------------------


async def _contact_named(session: AsyncSession, user_id: uuid.UUID, name: str) -> Contact | None:
    wanted = fold_name(name)
    if not wanted:
        return None
    for contact in await chats_repo.list_contacts(session, user_id):
        if fold_name(contact.display_name) == wanted:
            return contact
    return None


async def _merge_target(
    session: AsyncSession,
    user_id: uuid.UUID,
    contact: Contact | None,
    to_whom: str,
    item: CommitmentItem,
) -> Commitment | None:
    if contact is not None:
        candidates = await commitments_repo.list_open_for_contacts(session, user_id, [contact.id])
    else:
        wanted = fold_name(to_whom)
        candidates = [
            c
            for c in await commitments_repo.list_open_without_contact(session, user_id)
            if fold_name(c.to_whom or "") == wanted
        ]
    for candidate in candidates:
        if (
            candidate.jira_comment_id is None
            and candidate.due == item.due
            and same_promise(candidate.what, item.what)
        ):
            return candidate
    return None


def _link(row: Commitment, job: CommentCommitmentInput) -> None:
    row.jira_issue_key = job.issue_key
    row.jira_comment_id = job.comment_id
    row.jira_comment_url = job.comment_url
    row.restricted = job.restricted


def _unlink(row: Commitment) -> None:
    row.jira_comment_id = None
    row.jira_comment_url = None
    row.restricted = False


def _note(event: str, row: Commitment, note: CommitmentAutoNote | None = None) -> None:
    log.info(
        "commitment_auto_updated",
        user_id=str(row.user_id),
        commitment_id=str(row.id),
        change=event,
        note=note.value if note is not None else None,
        issue_key=row.jira_issue_key,
    )


async def store_comment_item(
    session: AsyncSession,
    job: CommentCommitmentInput,
    item: CommitmentItem | None,
    *,
    at: datetime,
) -> Outcome:
    """Apply one extraction result (rules in the module docstring). Caller commits."""
    user_id = job.user_id
    await session.execute(
        sql_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{user_id}:jira:{job.comment_id}"},
    )
    rows = await commitments_repo.get_by_comment(session, user_id, job.comment_id, open_only=False)
    if any(
        r.state != CommitmentState.open and r.closed_by == CommitmentClosedBy.user for r in rows
    ):
        return "skipped"  # the user closed what this comment gave: never again (FR-J14)
    linked = [r for r in rows if r.state == CommitmentState.open]
    if linked:
        row = linked[0]
        if row.user_modified:
            return "skipped"
        if item is None:
            if row.source_kind == CommitmentSourceKind.telegram_message:
                _unlink(row)
                row.auto_note = CommitmentAutoNote.source_changed
            else:
                row.state = CommitmentState.resolved
                row.closed_at = at
                row.closed_by = CommitmentClosedBy.system
                row.auto_note = CommitmentAutoNote.source_changed
            _note("source_changed", row, CommitmentAutoNote.source_changed)
            await session.flush()
            return "closed"
        row.what = " ".join(item.what.split())
        row.due = item.due
        row.source_sent_at = job.updated_at
        if row.source_kind == CommitmentSourceKind.jira_comment:
            row.source_message_text = None if job.restricted else excerpt(item.evidence)
        _link(row, job)
        await session.flush()
        return "updated"
    if item is None:
        return "none"
    to_whom = (item.to_whom or job.counterparty or "").strip() or job.counterparty
    contact = await _contact_named(session, user_id, to_whom)
    target = await _merge_target(session, user_id, contact, to_whom, item)
    if target is not None:
        _link(target, job)
        await session.flush()
        _note("merged", target)
        return "merged"
    await commitments_repo.add_commitment(
        session,
        user_id,
        contact_id=contact.id if contact is not None else None,
        to_whom=to_whom,
        what=" ".join(item.what.split()),
        due=item.due,
        source_sent_at=job.updated_at,
        confidence=round(item.confidence, 2),
        source_message_text=None if job.restricted else excerpt(item.evidence),
        source_kind=CommitmentSourceKind.jira_comment,
        jira_issue_key=job.issue_key,
        jira_comment_id=job.comment_id,
        jira_comment_url=job.comment_url,
        restricted=job.restricted,
    )
    return "created"


def comment_gate(job: CommentCommitmentInput) -> GateDecision:
    """The pre-filter for one comment; the request it may answer is its context."""
    return needs_extraction(job.comment_text, [job.request_text] if job.request_text else [])


async def extract_from_comment(
    session_factory: async_sessionmaker[AsyncSession],
    router: LLMRouter,
    job: CommentCommitmentInput,
    *,
    timezone: str,
    clock: Clock = utcnow,
) -> Outcome:
    """Extract, ground and store (the LLM call runs outside any transaction).

    Issue #72: a comment with no promise cue (:func:`comment_gate`) skips the LLM call and is
    stored as an extraction that found nothing (an edit that no longer promises anything still
    closes or unlinks its row)."""
    decision = comment_gate(job)
    log_decision(job.user_id, decision, source="jira_comment")
    items: list[CommitmentItem] = []
    if decision.extract:
        items = (
            await router.extract_commitments(request_for(job, timezone), user_id=job.user_id)
        ).output
    item = best_item(items, job, timezone)
    now = clock()
    async with session_factory() as session:
        outcome = await store_comment_item(session, job, item, at=now)
        await session.commit()
    log.info(
        "jira_comment_extracted",
        user_id=str(job.user_id),
        issue_key=job.issue_key,
        items=len(items),
        outcome=outcome,
    )
    return outcome


# --- automatic changes (research J11) -------------------------------------------------------------


def _auto_close(row: Commitment, note: CommitmentAutoNote, at: datetime) -> bool:
    if row.user_modified:
        if row.auto_note != note:
            row.auto_note = note
            _note("note_only", row, note)
        return False
    row.state = CommitmentState.resolved
    row.closed_at = at
    row.closed_by = CommitmentClosedBy.system
    row.auto_note = note
    _note("auto_resolved", row, note)
    return True


async def resolve_for_issue(
    session: AsyncSession, user_id: uuid.UUID, issue_key: str, *, at: datetime
) -> int:
    """The issue is done: its open commitments are resolved (``issue_done``)."""
    closed = 0
    for row in await commitments_repo.list_open_for_issues(session, user_id, [issue_key]):
        closed += _auto_close(row, CommitmentAutoNote.issue_done, at)
    await session.flush()
    return closed


async def mark_comment_gone(
    session: AsyncSession, user_id: uuid.UUID, comment_id: str, *, at: datetime
) -> int:
    """The source comment was deleted: a merged Telegram row only loses the Jira link, a
    Jira-only row is resolved (``source_removed``, restorable)."""
    changed = 0
    for row in await commitments_repo.get_by_comment(session, user_id, comment_id):
        if row.source_kind == CommitmentSourceKind.telegram_message:
            _unlink(row)
            row.auto_note = CommitmentAutoNote.source_removed
            _note("unlinked", row, CommitmentAutoNote.source_removed)
            changed += 1
        else:
            changed += _auto_close(row, CommitmentAutoNote.source_removed, at)
    await session.flush()
    return changed


async def mark_issue_unreachable(session: AsyncSession, user_id: uuid.UUID, issue_key: str) -> int:
    """403 / 404 or a deselected project: kept open with ``source_unreachable``."""
    changed = 0
    for row in await commitments_repo.list_open_for_issues(session, user_id, [issue_key]):
        if row.auto_note != CommitmentAutoNote.source_unreachable:
            row.auto_note = CommitmentAutoNote.source_unreachable
            _note("unreachable", row, CommitmentAutoNote.source_unreachable)
            changed += 1
    await session.flush()
    return changed


async def jira_data_summary(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Counts for ``/my_data`` (001-T070): Jira-sourced commitments and comment markers."""
    commitments = await session.scalar(
        select(func.count())
        .select_from(Commitment)
        .where(
            Commitment.user_id == user_id,
            Commitment.source_kind == CommitmentSourceKind.jira_comment,
        )
    )
    return {
        "jira_commitments": int(commitments or 0),
        "jira_comment_markers": await jira_cursor.count_for_user(session, user_id),
    }


__all__ = [
    "CommentCommitmentInput",
    "EXCERPT_MAX_CHARS",
    "Outcome",
    "best_item",
    "comment_gate",
    "excerpt",
    "extract_from_comment",
    "fold_name",
    "ground_comment_item",
    "issue_keys_in",
    "jira_data_summary",
    "mark_comment_gone",
    "mark_issue_unreachable",
    "request_for",
    "resolve_for_issue",
    "same_promise",
    "store_comment_item",
]
