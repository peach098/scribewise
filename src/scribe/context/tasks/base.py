"""Task source protocol (contracts/internal-interfaces.md "TaskSource", research R6, FR-019).

A task source is read-only (FR-021): no method changes anything in the remote system. Any
provider failure surfaces as :class:`TaskSourceUnavailable`, whose message never contains
credentials; the composer then drafts without task context and says so on the card.

``Task.due_at`` is the provider's aware due moment (the card's due field). A provider may also
fill ``description`` and its last few ``comments`` (third-party text: never logged, never
persisted, sent only to LLM routes whose provider has ``data_use_confirmed``) and
``due_set_at`` (when the due field was last changed, when the provider knows). The *effective*
deadline is resolved from all of these by :mod:`scribe.context.tasks.deadlines` and kept in
``Task.deadline`` with its source; :meth:`Task.due_on` returns that deadline, so the conflict
check, the draft context and card labels all use it. Wherever a date is shown or compared it is
the *user's* local date (:meth:`Task.due_on` with ``users.timezone``), never the UTC date.
``Task.done`` is true for a task in a list/state named like Done / Готово / Closed: drafting
may still mention it, but it never counts as an open deadline (conflict backstop).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from scribe.db.models import TaskSourceConnection

TaskProviderName = Literal["trello", "jira"]


class TaskSourceUnavailable(Exception):
    """The task source failed (network, HTTP error, revoked token, unexpected payload).

    The message is safe to log: it never contains a key, a token or a request URL.
    """

    error_code: str = "unavailable"


# --- Jira errors (specs/003-jira-commitments, contracts/internal-interfaces.md) ---------------


class CertInfo(BaseModel):
    """A server certificate as shown to the user before trusting it (research J5)."""

    model_config = ConfigDict(frozen=True)

    subject: str
    issuer: str
    not_after: str
    sha256: str  # colon-separated upper-case hex
    pem: str = Field(repr=False)


class JiraAddressInvalid(TaskSourceUnavailable):
    error_code = "address_invalid"


class JiraPortNotAllowed(JiraAddressInvalid):
    """The address names a port the operator does not allow (issue #61)."""

    error_code = "port_not_allowed"

    def __init__(self, allowed_ports: Sequence[int]) -> None:
        super().__init__("the jira address uses a port that is not allowed")
        self.allowed_ports = tuple(allowed_ports)


class JiraUnreachable(TaskSourceUnavailable):
    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked
        self.error_code = "blocked_address" if blocked else "unreachable"


class JiraCertificateUntrusted(TaskSourceUnavailable):
    error_code = "cert_untrusted"

    def __init__(self, message: str, *, cert: CertInfo | None = None) -> None:
        super().__init__(message)
        self.cert = cert


class JiraVersionUnsupported(TaskSourceUnavailable):
    error_code = "version_unsupported"

    def __init__(self, found: str, minimum: str) -> None:
        super().__init__(f"jira {found} is older than the supported {minimum}")
        self.found = found
        self.minimum = minimum


class JiraAuthFailed(TaskSourceUnavailable):
    error_code = "auth_failed"

    def __init__(self, message: str, *, hint: Literal["classic_token"] | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class JiraRateLimited(TaskSourceUnavailable):
    error_code = "rate_limited"

    def __init__(self, retry_after_s: float | None) -> None:
        super().__init__("jira is rate limiting requests")
        self.retry_after_s = retry_after_s


class JiraIssueGone(TaskSourceUnavailable):
    """One issue answered 403/404 (deleted, moved, or the user lost access)."""

    error_code = "issue_gone"

    def __init__(self, key: str) -> None:
        super().__init__(f"jira issue {key} is not reachable")
        self.key = key


class ReadOnlyViolation(RuntimeError):
    """A non-GET request to Jira was attempted: a programming error, never an outage (J3)."""

    def __init__(self, method: str) -> None:
        super().__init__(f"jira client is read-only; refused {method}")
        self.method = method


_DONE_STATUS = re.compile(
    r"\b(?:done|closed|complete[d]?|finished|archived?|"
    r"готов\w*|сделан\w*|выполнен\w*|закрыт\w*|завершен\w*|заверш[её]н\w*)\b",
    re.IGNORECASE,
)


def is_done_status(status: str) -> bool:
    """A list / state name that means the work is finished (Done, Готово, Closed, ...)."""
    return bool(_DONE_STATUS.search(status or ""))


DeadlineSource = Literal["due", "comment", "description"]


class TaskComment(BaseModel):
    """One comment on a task (third-party content: never logged or persisted)."""

    model_config = ConfigDict(frozen=True)

    id: str
    text: str = Field(repr=False)
    author: str
    at: datetime  # timezone-aware


class Deadline(BaseModel):
    """The effective deadline of a task and where it comes from.

    ``on`` is the deadline's date: for ``source="due"`` the due moment's date in the time zone
    the deadline was resolved in, for a comment or the description the date the text names.
    ``quote`` (comment / description only) is the words that give the date, ``author`` and
    ``noted_at`` the comment's author and time."""

    model_config = ConfigDict(frozen=True)

    on: date
    source: DeadlineSource
    at: datetime | None = None  # the provider's due moment (source "due" only)
    quote: str = Field(default="", repr=False)
    author: str = ""
    noted_at: datetime | None = None


class Task(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    status: str
    due_at: datetime | None  # timezone-aware; the provider's own due field
    assignees: list[str]
    url: str
    source: TaskProviderName
    description: str = Field(default="", repr=False)
    description_at: datetime | None = None  # best known time the description was written
    comments: tuple[TaskComment, ...] = Field(default=(), repr=False)  # oldest first
    due_set_at: datetime | None = None  # when ``due_at`` was last changed (None: unknown/old)
    deadline: Deadline | None = None  # the effective deadline (``deadlines.resolve_deadline``)
    # The provider's due field is a calendar date, not a moment (Jira ``duedate``): ``due_at``
    # then holds that date at noon UTC and :meth:`due_local_date` returns the date itself in
    # every time zone instead of converting it.
    due_is_date: bool = False

    def due_local_date(self, moment: datetime, timezone: str) -> date:
        """The local date of the due field's ``moment`` (a date-only due field is never
        shifted by the time zone)."""
        if self.due_is_date:
            return moment.date()
        return moment.astimezone(ZoneInfo(timezone)).date()

    def due_on(self, timezone: str) -> date | None:
        """The effective deadline's date in the user's time zone (the due field when the
        deadline has not been resolved)."""
        if self.deadline is not None and self.deadline.source != "due":
            return self.deadline.on
        moment = self.deadline.at if self.deadline is not None else self.due_at
        if moment is None:
            return None
        return self.due_local_date(moment, timezone)

    @property
    def deadline_source(self) -> DeadlineSource | None:
        if self.deadline is not None:
            return self.deadline.source
        return "due" if self.due_at is not None else None

    @property
    def deadline_quote(self) -> str:
        return self.deadline.quote if self.deadline is not None else ""

    @property
    def done(self) -> bool:
        return is_done_status(self.status)


class TaskList(list[Task]):
    """``list_open_tasks`` result that also says how many boards/projects could not be read
    (a partial load; all of them failing raises :class:`TaskSourceUnavailable` instead)."""

    def __init__(self, tasks: Any = (), *, skipped_containers: int = 0) -> None:
        super().__init__(tasks)
        self.skipped_containers = skipped_containers


class Container(BaseModel):
    """A board (Trello) or project (Jira) the user can pick."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str


class TaskSourceIdentity(BaseModel):
    """Who the credentials belong to (shown in /tasks)."""

    model_config = ConfigDict(frozen=True)

    id: str
    username: str
    full_name: str
    time_zone: str | None = None  # the account's profile time zone (Jira: JQL date literals)
    server_version: str | None = None  # self-hosted Jira only


class TaskSource(Protocol):
    @property
    def provider(self) -> TaskProviderName: ...

    async def validate(self, credentials: dict[str, Any]) -> TaskSourceIdentity: ...

    async def list_containers(self, credentials: dict[str, Any]) -> list[Container]: ...

    async def list_open_tasks(self, conn: TaskSourceConnection) -> list[Task]: ...


__all__ = [
    "CertInfo",
    "Container",
    "Deadline",
    "DeadlineSource",
    "JiraAddressInvalid",
    "JiraAuthFailed",
    "JiraCertificateUntrusted",
    "JiraIssueGone",
    "JiraPortNotAllowed",
    "JiraRateLimited",
    "JiraUnreachable",
    "JiraVersionUnsupported",
    "ReadOnlyViolation",
    "Task",
    "TaskComment",
    "TaskProviderName",
    "TaskSource",
    "TaskSourceIdentity",
    "TaskSourceUnavailable",
    "TaskList",
    "is_done_status",
]
