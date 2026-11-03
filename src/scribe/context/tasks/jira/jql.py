"""JQL builders and small Jira string helpers (specs/003-jira-commitments, research J7, J8).

Every query is built from stored configuration (the project keys the user picked) and from
values Scribe computes itself (the sync watermark), never from free text. Still, each piece is
validated before it reaches the query so a corrupted or hand-edited config row cannot inject
JQL: project keys must match :data:`PROJECT_KEY` and are always double-quoted; the ``since``
bound of the comment-sync search is either a relative literal (``-30d``) or a
``yyyy/MM/dd HH:mm`` date-time, which is double-quoted.

- :func:`tasks_jql`: the user's open issues for the task list (J7);
- :func:`scan_jql`: issues the user is involved in that changed since the watermark (J8);
- :func:`jql_datetime`: a moment rendered for JQL in the Jira user's own time zone (JQL
  date-times without a zone are read in the searching user's zone);
- :func:`parse_version`: ``serverInfo.version`` as a comparable tuple (J6);
- :data:`ISSUE_KEY`: issue keys (``PAY-142``) in free text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Jira's default project key pattern: an uppercase letter, then uppercase letters, digits or
# underscores (at least two characters in total).
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]+$")
ISSUE_KEY = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

_RELATIVE_SINCE = re.compile(r"^-\d+[dhm]$")
_ABSOLUTE_SINCE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}$")
_VERSION = re.compile(r"\d+(?:\.\d+)*")
JQL_DATETIME_FORMAT = "%Y/%m/%d %H:%M"


def quote_keys(keys: Iterable[str]) -> str:
    """``"PAY","CORE"``: the keys double-quoted, deduplicated in their first order.

    Raises :class:`ValueError` for an empty list or a key that is not a valid project key.
    """
    seen: list[str] = []
    for key in keys:
        if not isinstance(key, str) or not PROJECT_KEY.fullmatch(key):
            raise ValueError(f"invalid Jira project key: {key!r}")
        if key not in seen:
            seen.append(key)
    if not seen:
        raise ValueError("at least one Jira project key is required")
    return ",".join(f'"{key}"' for key in seen)


def tasks_jql(keys: Iterable[str]) -> str:
    """The user's open issues in the chosen projects, nearest due date first (J7)."""
    return (
        f"project in ({quote_keys(keys)}) AND assignee = currentUser() "
        "AND statusCategory != Done ORDER BY duedate ASC"
    )


def _since_literal(since: str) -> str:
    if _RELATIVE_SINCE.fullmatch(since):
        return since
    if _ABSOLUTE_SINCE.fullmatch(since):
        return f'"{since}"'
    raise ValueError(f"invalid JQL 'since' value: {since!r}")


def scan_jql(keys: Iterable[str], since: str, *, newest_first: bool = False) -> str:
    """Issues the user is assigned to, reported or watches, updated since ``since`` (J8).

    ``since`` is a relative literal (``-30d``, ``-5h``, ``-15m``, inserted as is) or a
    :func:`jql_datetime` value (``2026/10/21 09:30``, double-quoted); anything else raises
    :class:`ValueError`. ``newest_first`` (the capped first scan) orders by ``updated DESC``.
    """
    order = "DESC" if newest_first else "ASC"
    return (
        f"project in ({quote_keys(keys)}) AND (assignee = currentUser() "
        "OR reporter = currentUser() OR watcher = currentUser()) "
        f"AND updated >= {_since_literal(since)} ORDER BY updated {order}"
    )


def _zone(time_zone: str | None) -> ZoneInfo | None:
    if not time_zone:
        return None
    try:
        return ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def jql_datetime(moment: datetime, time_zone: str | None) -> str:
    """``yyyy/MM/dd HH:mm`` (unquoted) of ``moment`` in the IANA ``time_zone``.

    UTC when the zone is missing or unknown; a naive ``moment`` is taken as UTC.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    zone = _zone(time_zone)
    local = moment.astimezone(zone if zone is not None else UTC)
    return local.strftime(JQL_DATETIME_FORMAT)


def parse_version(text: str) -> tuple[int, ...]:
    """``"10.3.4"`` -> ``(10, 3, 4)``; suffixes are ignored (``"11.3.1-SNAPSHOT"`` ->
    ``(11, 3, 1)``). Raises :class:`ValueError` when the text does not start with a number."""
    match = _VERSION.match(text.strip())
    if match is None:
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(part) for part in match.group(0).split("."))


__all__ = [
    "ISSUE_KEY",
    "JQL_DATETIME_FORMAT",
    "PROJECT_KEY",
    "jql_datetime",
    "parse_version",
    "quote_keys",
    "scan_jql",
    "tasks_jql",
]
