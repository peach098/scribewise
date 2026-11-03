"""The user's task source at draft time (FR-019, research R6, the "task source unavailable" edge
case).

:meth:`TaskSources.load_open_tasks` reads the user's ``task_source_connections`` row, closes the
database session, then calls the provider with an overall timeout. It never raises:

- no connection (or ``disconnected``): no tasks, no warning;
- any provider failure (``TaskSourceUnavailable`` or one of its Jira subclasses, a
  timeout, or any unexpected exception, logged by type only): no tasks and
  ``unavailable=True``, so the composer adds ``tasks_unavailable`` to the draft's
  ``system_warnings``. ``config_json.consecutive_failures`` counts failures in a row; from
  :data:`FAILURES_BEFORE_ERROR` on the connection ``status`` becomes ``error`` (shown in
  /tasks; the card posts a one-line alert once per failure streak, see
  ``review.DraftPresenter``);
- after a failure the user's source is not asked again for :data:`NEGATIVE_CACHE_S` (an outage
  does not add the full timeout to every draft); those drafts are unavailable too, without
  counting another failure;
- some boards unreadable, others fine: the readable boards' tasks and ``partial=True`` (the
  composer adds ``tasks_partial``); this is not a failure;
- success resets the counter, the alert flag and an ``error`` status back to ``active``.

Every read-modify-write of ``config_json`` locks the row (``tasks_conn.get_connection_for_update``)
so concurrent drafts, the card alert and /tasks never lose each other's changes.

Every loaded task carries its effective deadline (``Task.deadline``), resolved in the user's
time zone from the due field, the comments and the description by
:func:`~scribe.context.tasks.deadlines.resolve_deadline` (deterministic; the LLM refinement for
the tasks selected for a turn runs in the composer).

Task ids are namespaced for the LLM as ``task:<provider id>``. A process-wide default instance
(:func:`default_task_sources`) holds the 60 s per-user cache shared by the composer, the draft
card and /tasks.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.context.tasks.base import Task, TaskSource, TaskSourceUnavailable
from scribe.context.tasks.deadlines import resolve_all
from scribe.context.tasks.jira import JiraTaskSource
from scribe.context.tasks.trello import TrelloTaskSource
from scribe.db.models import ConnectionStatus, TaskSourceConnection
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.db.repos import users as users_repo
from scribe.logging import get_logger

log = get_logger("scribe.tasks")

TASK_PREFIX = "task:"
LOAD_TIMEOUT_S = 10.0
NEGATIVE_CACHE_S = 30.0
FAILURES_BEFORE_ERROR = 3
FAILURES_KEY = "consecutive_failures"
ALERTED_KEY = "error_alerted"


def task_ref(task_id: str) -> str:
    return f"{TASK_PREFIX}{task_id}"


def parse_task_ref(ref: str) -> str | None:
    if not ref.startswith(TASK_PREFIX):
        return None
    return ref[len(TASK_PREFIX) :] or None


@dataclass(frozen=True)
class TaskLoad:
    tasks: list[Task] = field(default_factory=list)
    connected: bool = False
    unavailable: bool = False
    partial: bool = False  # some boards could not be read; the others' tasks are here


class TaskSources:
    def __init__(
        self,
        sources: Mapping[str, TaskSource] | None = None,
        *,
        negative_ttl_s: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sources: dict[str, TaskSource] = dict(
            sources
            if sources is not None
            else {"trello": TrelloTaskSource(), "jira": JiraTaskSource()}
        )
        self._negative_ttl = NEGATIVE_CACHE_S if negative_ttl_s is None else negative_ttl_s
        self._monotonic = monotonic
        self._failed_at: dict[uuid.UUID, float] = {}

    def get(self, provider: str) -> TaskSource:
        source = self._sources.get(provider)
        if source is None:
            raise TaskSourceUnavailable(f"unknown task source {provider!r}")
        return source

    async def load_open_tasks(
        self, session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> TaskLoad:
        async with session_factory() as session:
            conn = await tasks_conn_repo.get_connection(session, user_id)
            if conn is None or conn.status == ConnectionStatus.disconnected:
                return TaskLoad()
            snapshot = _detached(conn)
            user = await users_repo.get_user(session, user_id)
            timezone = user.timezone if user is not None else "UTC"
        provider = snapshot.provider.value
        failed_at = self._failed_at.get(user_id)
        if failed_at is not None and self._monotonic() - failed_at < self._negative_ttl:
            log.info("task_source_skipped_after_failure", user_id=str(user_id), provider=provider)
            return TaskLoad(connected=True, unavailable=True)
        try:
            source = self.get(provider)
            tasks = await asyncio.wait_for(source.list_open_tasks(snapshot), LOAD_TIMEOUT_S)
        except Exception as exc:  # whatever the provider does, drafting goes on without tasks
            reason = str(exc) if isinstance(exc, TaskSourceUnavailable) else type(exc).__name__
            log.warning(
                "task_source_unavailable", user_id=str(user_id), provider=provider, reason=reason
            )
            self._failed_at[user_id] = self._monotonic()
            await self._record(session_factory, user_id, ok=False)
            return TaskLoad(connected=True, unavailable=True)
        self._failed_at.pop(user_id, None)
        if snapshot.config_json.get(FAILURES_KEY) or snapshot.status == ConnectionStatus.error:
            await self._record(session_factory, user_id, ok=True)
        skipped = int(getattr(tasks, "skipped_containers", 0) or 0)
        log.info(
            "task_source_loaded",
            user_id=str(user_id),
            provider=provider,
            tasks=len(tasks),
            skipped_containers=skipped,
        )
        resolved = resolve_all(tasks, timezone)
        return TaskLoad(tasks=resolved, connected=True, partial=skipped > 0)

    def cached_tasks(self, user_id: uuid.UUID, timezone: str | None = None) -> list[Task]:
        """Last fetched tasks of the user from any source's cache (for card labels), with their
        effective deadlines when ``timezone`` (the user's) is given."""
        found: list[Task] = []
        for source in self._sources.values():
            cached = getattr(source, "cached_tasks", None)
            if cached is not None:
                found += cached(user_id) or []
        return resolve_all(found, timezone) if timezone is not None else found

    def invalidate(self, user_id: uuid.UUID) -> None:
        """Drop the user's cached tasks and failure (reconnect, board change, disconnect)."""
        self._failed_at.pop(user_id, None)
        for source in self._sources.values():
            invalidate = getattr(source, "invalidate", None)
            if invalidate is not None:
                invalidate(user_id)

    async def record_result(
        self, session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, *, ok: bool
    ) -> None:
        """Count a result of another reader of the same source (the Jira comment sync,
        specs/003 J12) toward the failure streak shown in /tasks and alerted once."""
        if ok:
            self._failed_at.pop(user_id, None)
        await self._record(session_factory, user_id, ok=ok)

    @staticmethod
    async def _record(
        session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, *, ok: bool
    ) -> None:
        """Count a failure (status ``error`` from the third in a row) or reset after a success."""
        try:
            async with session_factory() as session:
                conn = await tasks_conn_repo.get_connection_for_update(session, user_id)
                if conn is None or conn.status == ConnectionStatus.disconnected:
                    return
                config: dict[str, Any] = dict(conn.config_json or {})
                if ok:
                    config.pop(FAILURES_KEY, None)
                    config.pop(ALERTED_KEY, None)
                    conn.status = ConnectionStatus.active
                else:
                    config[FAILURES_KEY] = int(config.get(FAILURES_KEY) or 0) + 1
                    if config[FAILURES_KEY] >= FAILURES_BEFORE_ERROR:
                        if conn.status != ConnectionStatus.error:
                            log.warning("task_source_error", user_id=str(user_id))
                        conn.status = ConnectionStatus.error
                conn.config_json = config  # reassign: JSONB changes are not tracked in place
                await session.commit()
        except Exception as exc:  # bookkeeping only: drafting goes on regardless
            log.warning(
                "task_source_status_not_saved", user_id=str(user_id), error=type(exc).__name__
            )


def _detached(conn: TaskSourceConnection) -> TaskSourceConnection:
    """A plain copy usable after the session is closed (no lazy loads, no expiry)."""
    return TaskSourceConnection(
        id=conn.id,
        user_id=conn.user_id,
        provider=conn.provider,
        credentials=dict(conn.credentials or {}),
        config_json=dict(conn.config_json or {}),
        status=conn.status,
    )


_default: TaskSources | None = None


def default_task_sources() -> TaskSources:
    """The process-wide instance (one cache for composer, card and /tasks)."""
    global _default
    if _default is None:
        _default = TaskSources()
    return _default


def install_default_task_sources(sources: TaskSources) -> None:
    """The service installs its configured sources (the Jira operator settings)."""
    global _default
    _default = sources


def reset_default_task_sources() -> None:
    """Tests: drop the shared instance and its cache."""
    global _default
    _default = None


__all__ = [
    "ALERTED_KEY",
    "FAILURES_BEFORE_ERROR",
    "FAILURES_KEY",
    "NEGATIVE_CACHE_S",
    "TASK_PREFIX",
    "TaskLoad",
    "TaskSources",
    "default_task_sources",
    "install_default_task_sources",
    "parse_task_ref",
    "reset_default_task_sources",
    "task_ref",
]
