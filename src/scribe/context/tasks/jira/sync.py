"""Jira comment sync (specs/003-jira-commitments research J8 – J11, FR-J07 – FR-J14).

Two scheduler jobs (registered by :func:`register`):

- ``jira_comments`` (every ``SCRIBE_JIRA_SYNC_INTERVAL_S``): :meth:`JiraCommentSync.run_due`
  claims the due Jira connections (``tasks_conn.claim_due_jira``: ``FOR UPDATE SKIP LOCKED``,
  ``next_sync_at`` moved forward in the same transaction) and syncs them with
  ``SCRIBE_JIRA_SYNC_CONCURRENCY``. :meth:`sync_user` searches the issues the user is assigned
  to, reported or watches that changed since the watermark (minus 5 minutes, rendered in the
  Jira account's time zone; the first scan covers ``SCRIBE_JIRA_LOOKBACK_DAYS``, newest first,
  capped at ``SCRIBE_JIRA_FIRST_SCAN_MAX_ISSUES`` and reported once in the control chat). Per
  issue: a done issue resolves its commitments; each of the user's own comments changed within
  the lookback whose fingerprint (``crypto.comment_hmac``) is not in ``jira_comment_cursor``
  goes to :func:`~scribe.context.comment_commitments.extract_from_comment` with at most one
  earlier comment by someone else as the request it may answer.
- ``jira_verify`` (daily): :meth:`verify_user` re-reads every issue with an open Jira-sourced
  commitment: done → resolved, deleted comment → ``source_removed``, 403 / 404 or a deselected
  project → ``source_unreachable`` (kept open).

No database session is held during HTTP or LLM calls. Every failure of the source is counted
through ``TaskSources.record_result`` (the same streak, error status and one-time alert as task
loads, FR-J19); a rate limit moves ``next_sync_at`` to the ``Retry-After`` (at most 15 min). A
failed extraction leaves the comment's cursor untouched, so it is retried next time. Logs carry
counts and error codes only (``jira_sync_finished``, ``jira_verify_finished``).
"""

from __future__ import annotations

import asyncio
import time as monotonic_time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.context import comment_commitments
from scribe.context.comment_commitments import CommentCommitmentInput, Outcome
from scribe.context.tasks.base import (
    JiraIssueGone,
    JiraRateLimited,
    TaskSourceUnavailable,
)
from scribe.context.tasks.jira.flavors import JiraFlavor
from scribe.context.tasks.jira.jql import jql_datetime, scan_jql
from scribe.context.tasks.jira.source import (
    JiraComment,
    JiraTaskSource,
    issue_comments,
    parse_jira_time,
)
from scribe.context.tasks.service import TaskSources, default_task_sources
from scribe.crypto import comment_hmac
from scribe.db.models import ConnectionStatus, JiraCursorOutcome, TaskProvider, TaskSourceConnection
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import jira_cursor
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Msg
from scribe.llm.registry import LLMRouter
from scribe.logging import get_logger
from scribe.scheduling.scheduler import Daily, Every, Scheduler

log = get_logger("scribe.jira")

SYNC_JOB = "jira_comments"
VERIFY_JOB = "jira_verify"
VERIFY_SCHEDULE = Daily(at=time(3, 40))  # UTC, after the retention purge
OVERLAP = timedelta(minutes=5)
MAX_BACKOFF = timedelta(minutes=15)
CLAIM_LIMIT = 100
SCAN_FIELDS = ("summary", "status", "duedate", "reporter", "comment", "updated")
FIRST_SCAN_DONE = "first_scan_done"
Notifier = Callable[[uuid.UUID, Msg], Awaitable[None]]
_CURSOR_OUTCOME: dict[Outcome, JiraCursorOutcome] = {
    "created": JiraCursorOutcome.commitment,
    "updated": JiraCursorOutcome.commitment,
    "merged": JiraCursorOutcome.merged,
    "none": JiraCursorOutcome.no_commitment,
    "closed": JiraCursorOutcome.no_commitment,
    "skipped": JiraCursorOutcome.skipped,
}


class SyncReport(BaseModel):
    issues_read: int = 0
    comments_read: int = 0
    comments_sent_to_llm: int = 0
    created: int = 0
    updated: int = 0
    merged: int = 0
    auto_resolved: int = 0
    unreachable: int = 0
    removed: int = 0
    extraction_errors: int = 0
    error: str | None = None


def _status_done(issue: dict[str, Any]) -> bool:
    fields = issue.get("fields")
    status = fields.get("status") if isinstance(fields, dict) else None
    category = status.get("statusCategory") if isinstance(status, dict) else None
    return isinstance(category, dict) and category.get("key") == "done"


def _name(user: Any) -> str:
    return str(user.get("displayName") or "") if isinstance(user, dict) else ""


def pick_request(comment: JiraComment, earlier: list[JiraComment], me: str) -> JiraComment | None:
    """The earlier comment by someone else that ``comment`` may answer: the newest one that
    mentions the user or whose author the comment mentions, else the newest one."""
    others = [c for c in earlier if c.author_id != me and c.text.plain]
    if not others:
        return None
    mentioned = {m.id for m in comment.text.mentions}
    related = [
        c for c in others if c.author_id in mentioned or me in {m.id for m in c.text.mentions}
    ]
    return (related or others)[-1]


def counterparty(comment: JiraComment, request: JiraComment | None, reporter: str, me: str) -> str:
    """Whom the promise is to: the first person mentioned (not the user), else the request's
    author, else the issue reporter (spec FR-J08)."""
    for mention in comment.text.mentions:
        if mention.id != me and mention.display_name:
            return mention.display_name
    if request is not None and request.author_name:
        return request.author_name
    return reporter


def fingerprint(comment: JiraComment) -> bytes:
    return comment_hmac(f"{comment.text.plain}\x00{int(comment.restricted)}")


class JiraCommentSync:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        router: LLMRouter,
        settings: Settings,
        *,
        task_sources: TaskSources | None = None,
        clock: Clock = utcnow,
        notifier: Notifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._router = router
        self._settings = settings
        self._task_sources = task_sources
        self._clock = clock
        self._notifier = notifier
        self._interval = timedelta(seconds=settings.jira_sync_interval_s)
        self._lookback = timedelta(days=settings.jira_lookback_days)

    @property
    def task_sources(self) -> TaskSources:
        return self._task_sources or default_task_sources()

    def _source(self) -> JiraTaskSource:
        source = self.task_sources.get("jira")
        if not isinstance(source, JiraTaskSource):
            raise TaskSourceUnavailable("jira source is not available")
        return source

    # --- jobs ---------------------------------------------------------------------------------

    async def run_due(self) -> int:
        """Sync every due connection once; returns how many were synced."""
        now = self._clock()
        async with self._session_factory() as session:
            user_ids = await tasks_conn_repo.claim_due_jira(
                session, now, interval=self._interval, limit=CLAIM_LIMIT
            )
            await session.commit()
        semaphore = asyncio.Semaphore(self._settings.jira_sync_concurrency)

        async def one(user_id: uuid.UUID) -> None:
            async with semaphore:
                await self.sync_user(user_id)

        await asyncio.gather(*(one(u) for u in user_ids))
        return len(user_ids)

    async def verify_all(self) -> int:
        async with self._session_factory() as session:
            user_ids = await tasks_conn_repo.list_jira_user_ids(session)
        for user_id in user_ids:
            await self.verify_user(user_id)
        return len(user_ids)

    # --- one user -------------------------------------------------------------------------------

    async def _snapshot(self, user_id: uuid.UUID) -> tuple[TaskSourceConnection, str] | None:
        async with self._session_factory() as session:
            conn = await tasks_conn_repo.get_connection(session, user_id)
            if (
                conn is None
                or conn.provider != TaskProvider.jira
                or conn.status == ConnectionStatus.disconnected
            ):
                return None
            user = await users_repo.get_user(session, user_id)
            snapshot = TaskSourceConnection(
                user_id=conn.user_id,
                provider=conn.provider,
                credentials=dict(conn.credentials or {}),
                config_json=dict(conn.config_json or {}),
                status=conn.status,
                sync_watermark=conn.sync_watermark,
            )
            return snapshot, (user.timezone if user is not None else "UTC")

    async def sync_user(self, user_id: uuid.UUID) -> SyncReport:
        report = SyncReport()
        started = monotonic_time.monotonic()
        loaded = await self._snapshot(user_id)
        if loaded is None:
            return report
        conn, timezone = loaded
        config = conn.config_json or {}
        keys = [str(k) for k in config.get("project_keys") or []]
        identity = config.get("identity") or {}
        me = str(identity.get("id") or "")
        if not keys or not me:
            return report
        now = self._clock()
        first_scan = conn.sync_watermark is None
        cap = self._settings.jira_first_scan_max_issues if first_scan else 10_000
        if first_scan:
            jql = scan_jql(keys, f"-{self._settings.jira_lookback_days}d", newest_first=True)
        else:
            assert conn.sync_watermark is not None
            since = jql_datetime(conn.sync_watermark - OVERLAP, identity.get("time_zone"))
            jql = scan_jql(keys, since)
        watermark: datetime | None = None
        try:
            async with self._source().client(config, conn.credentials or {}) as client:
                async for issue in client.search(jql, fields=SCAN_FIELDS, limit=cap):
                    report.issues_read += 1
                    updated = _issue_updated(issue)
                    if updated is not None and (watermark is None or updated > watermark):
                        watermark = updated
                    await self._issue(user_id, issue, client.flavor, me, timezone, now, report)
        except TaskSourceUnavailable as exc:
            report.error = exc.error_code
            await self._failed(user_id, exc, now)
            self._log("jira_sync_finished", user_id, config, report, started)
            return report
        async with self._session_factory() as session:
            await tasks_conn_repo.set_sync_state(session, user_id, watermark=watermark or now)
            if first_scan:
                stored = await tasks_conn_repo.get_connection_for_update(session, user_id)
                if stored is not None:
                    stored.config_json = {**(stored.config_json or {}), FIRST_SCAN_DONE: True}
            await session.commit()
        await self.task_sources.record_result(self._session_factory, user_id, ok=True)
        if first_scan and not config.get(FIRST_SCAN_DONE):
            capped = Msg("jira.first_scan_capped", n=cap) if report.issues_read >= cap else ""
            await self._notify(
                user_id, Msg("jira.first_scan", n=report.created + report.merged, capped=capped)
            )
        self._log("jira_sync_finished", user_id, config, report, started)
        return report

    async def _issue(
        self,
        user_id: uuid.UUID,
        issue: dict[str, Any],
        flavor: JiraFlavor,
        me: str,
        timezone: str,
        now: datetime,
        report: SyncReport,
    ) -> None:
        key = str(issue.get("key") or "")
        if not key:
            return
        if _status_done(issue):
            async with self._session_factory() as session:
                report.auto_resolved += await comment_commitments.resolve_for_issue(
                    session, user_id, key, at=now
                )
                await session.commit()
            return
        raw_fields = issue.get("fields")
        fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
        comments = issue_comments(issue, flavor)
        report.comments_read += len(comments)
        cutoff = now - self._lookback
        own = [c for c in comments if c.author_id == me and c.updated >= cutoff and c.text.plain]
        if not own:
            return
        async with self._session_factory() as session:
            cursors = await jira_cursor.get_many(session, user_id, [c.id for c in own])
        for comment in own:
            digest = fingerprint(comment)
            seen = cursors.get(comment.id)
            if seen is not None and seen.body_hmac == digest:
                continue
            earlier = [c for c in comments if c.created < comment.created]
            request = pick_request(comment, earlier, me)
            job = CommentCommitmentInput(
                user_id=user_id,
                issue_key=key,
                issue_title=str(fields.get("summary") or ""),
                comment_id=comment.id,
                comment_url=flavor.browse_url(key, comment.id),
                comment_text=comment.text.plain,
                restricted=comment.restricted,
                posted_at=comment.created,
                updated_at=comment.updated,
                counterparty=counterparty(comment, request, _name(fields.get("reporter")), me),
                request_text=request.text.plain if request is not None else None,
                request_author=request.author_name if request is not None else None,
                request_at=request.created if request is not None else None,
            )
            if comment_commitments.comment_gate(job).extract:  # issue #72: else no LLM call
                report.comments_sent_to_llm += 1
            try:
                outcome = await comment_commitments.extract_from_comment(
                    self._session_factory, self._router, job, timezone=timezone, clock=self._clock
                )
            except Exception as exc:  # the comment is retried on the next sync
                report.extraction_errors += 1
                log.warning(
                    "jira_comment_extraction_failed",
                    user_id=str(user_id),
                    issue_key=key,
                    error=type(exc).__name__,
                )
                continue
            if outcome == "created":
                report.created += 1
            elif outcome == "updated":
                report.updated += 1
            elif outcome == "merged":
                report.merged += 1
            async with self._session_factory() as session:
                await jira_cursor.upsert(
                    session,
                    user_id,
                    issue_key=key,
                    comment_id=comment.id,
                    comment_updated_at=comment.updated,
                    body_hmac=digest,
                    outcome=_CURSOR_OUTCOME[outcome],
                )
                await session.commit()

    async def verify_user(self, user_id: uuid.UUID) -> SyncReport:
        report = SyncReport()
        started = monotonic_time.monotonic()
        loaded = await self._snapshot(user_id)
        if loaded is None:
            return report
        conn, _timezone = loaded
        config = conn.config_json or {}
        projects = {str(k) for k in config.get("project_keys") or []}
        now = self._clock()
        async with self._session_factory() as session:
            rows = await commitments_repo.list_open_jira(session, user_id)
            by_issue: dict[str, set[str]] = {}
            for row in rows:
                if row.jira_issue_key and row.jira_comment_id:
                    by_issue.setdefault(row.jira_issue_key, set()).add(row.jira_comment_id)
        try:
            async with self._source().client(config, conn.credentials or {}) as client:
                for key, comment_ids in sorted(by_issue.items()):
                    report.issues_read += 1
                    if key.rsplit("-", 1)[0] not in projects:
                        await self._unreachable(user_id, key, report)
                        continue
                    try:
                        issue = await client.issue(key, fields=["status"])
                        present = await client.comment_ids(key)
                    except JiraIssueGone:
                        await self._unreachable(user_id, key, report)
                        continue
                    async with self._session_factory() as session:
                        if _status_done(issue):
                            report.auto_resolved += await comment_commitments.resolve_for_issue(
                                session, user_id, key, at=now
                            )
                        else:
                            for comment_id in sorted(comment_ids - present):
                                report.removed += await comment_commitments.mark_comment_gone(
                                    session, user_id, comment_id, at=now
                                )
                        await session.commit()
        except TaskSourceUnavailable as exc:
            report.error = exc.error_code
            await self._failed(user_id, exc, now)
        self._log("jira_verify_finished", user_id, config, report, started)
        return report

    # --- helpers ----------------------------------------------------------------------------------

    async def _unreachable(self, user_id: uuid.UUID, key: str, report: SyncReport) -> None:
        async with self._session_factory() as session:
            report.unreachable += await comment_commitments.mark_issue_unreachable(
                session, user_id, key
            )
            await session.commit()

    async def _failed(self, user_id: uuid.UUID, exc: TaskSourceUnavailable, now: datetime) -> None:
        await self.task_sources.record_result(self._session_factory, user_id, ok=False)
        if isinstance(exc, JiraRateLimited):
            wait = timedelta(seconds=exc.retry_after_s or 0)
            async with self._session_factory() as session:
                await tasks_conn_repo.set_sync_state(
                    session, user_id, next_sync_at=now + min(max(wait, self._interval), MAX_BACKOFF)
                )
                await session.commit()

    async def _notify(self, user_id: uuid.UUID, message: Msg) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier(user_id, message)
        except Exception as exc:  # a lost notice never breaks the sync
            log.warning("jira_notice_not_sent", user_id=str(user_id), error=type(exc).__name__)

    @staticmethod
    def _log(
        event: str,
        user_id: uuid.UUID,
        config: dict[str, Any],
        report: SyncReport,
        started: float,
    ) -> None:
        log.info(
            event,
            user_id=str(user_id),
            deployment=config.get("deployment"),
            duration_ms=round((monotonic_time.monotonic() - started) * 1000),
            **report.model_dump(),
        )


def _issue_updated(issue: dict[str, Any]) -> datetime | None:
    fields = issue.get("fields")
    return parse_jira_time(fields.get("updated")) if isinstance(fields, dict) else None


def register(scheduler: Scheduler, sync: JiraCommentSync, settings: Settings) -> None:
    async def run_due() -> None:
        await sync.run_due()

    async def verify() -> None:
        await sync.verify_all()

    scheduler.register_job(
        SYNC_JOB, Every(timedelta(seconds=settings.jira_sync_interval_s)), run_due
    )
    scheduler.register_job(VERIFY_JOB, VERIFY_SCHEDULE, verify)


__all__ = [
    "SYNC_JOB",
    "VERIFY_JOB",
    "JiraCommentSync",
    "SyncReport",
    "counterparty",
    "fingerprint",
    "pick_request",
    "register",
]
