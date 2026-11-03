"""Trello task source (research R6, contracts/internal-interfaces.md "TaskSource").

Read-only REST client (FR-021): only ``GET`` requests, authenticated with the user's API key and
token as query parameters.

- ``validate``: ``GET /1/members/me`` -> who the token belongs to.
- ``list_containers``: ``GET /1/members/me/boards`` (open boards only).
- ``list_open_tasks``: for every selected board (``config_json.board_ids``)
  ``GET /1/boards/{id}/cards?filter=open&fields=name,desc,due,dueComplete,idList,idMembers,url``
  with the cards' recent actions nested in the same response
  (``actions=commentCard,updateCard:due&actions_limit=10``: still three requests per board, no
  request per card), plus ``/1/boards/{id}/lists`` and ``/1/boards/{id}/members``; ``status`` =
  list name, ``due_at`` = the card's aware due moment (shown and compared as the user's local
  date), ``assignees`` = member full names, ``description`` = the card description,
  ``comments`` = its last :data:`COMMENTS_PER_CARD` comments (oldest first, each cut to
  :data:`COMMENT_MAX_CHARS`), ``due_set_at`` = the newest due change among the fetched actions
  (None when it is older than all of them). The effective deadline is resolved from these by
  the task service (``deadlines.resolve_deadline``). Cards whose due date is marked complete
  (``dueComplete``) are skipped. A malformed action is skipped, never the board.
  Results are cached in memory for 60 s per user (keyed by the board selection and a
  fingerprint of the token); nothing from Trello is persisted or logged (comment and
  description text are third-party content).

A board that cannot be read (deleted, access removed) is skipped and counted in
``TaskList.skipped_containers`` (a partial load); only when every selected board fails does the
load raise. Every failure (network, timeout, non-2xx, unexpected payload) raises
:class:`TaskSourceUnavailable` with a message that never contains the key, the token or a
request URL. httpx logs request URLs (which carry the credentials) at INFO, so its loggers are
capped at WARNING here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from scribe.context.tasks.base import (
    Container,
    Task,
    TaskComment,
    TaskList,
    TaskSourceIdentity,
    TaskSourceUnavailable,
)
from scribe.db.models import TaskSourceConnection

TRELLO_API = "https://api.trello.com"
CACHE_TTL_S = 60.0
CACHE_MAX_AGE_S = 24 * 3600.0  # labels may use older entries; nothing stays longer than this
REQUEST_TIMEOUT_S = 5.0
CARD_FIELDS = "name,desc,due,dueComplete,idList,idMembers,url"
CARD_ACTIONS = "commentCard,updateCard:due"
ACTIONS_LIMIT = 10  # per card, newest first: comments and due changes together
COMMENTS_PER_CARD = 5
COMMENT_MAX_CHARS = 1000
DESCRIPTION_MAX_CHARS = 2000
CARD_PARAMS = {
    "filter": "open",
    "fields": CARD_FIELDS,
    "actions": CARD_ACTIONS,
    "actions_limit": str(ACTIONS_LIMIT),
    "action_fields": "type,date,data,idMemberCreator",
    "action_memberCreator_fields": "fullName",
}

# Request URLs carry key and token: never let httpx/httpcore log them.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


@dataclass(frozen=True)
class _CacheEntry:
    key: tuple[tuple[str, ...], str]
    fetched_at: float
    tasks: tuple[Task, ...]
    skipped_containers: int = 0


def _credentials(credentials: dict[str, Any]) -> tuple[str, str]:
    key, token = credentials.get("key"), credentials.get("token")
    if not isinstance(key, str) or not key or not isinstance(token, str) or not token:
        raise TaskSourceUnavailable("trello credentials are incomplete")
    return key, token


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _board_ids(conn: TaskSourceConnection) -> tuple[str, ...]:
    raw = (conn.config_json or {}).get("board_ids") or []
    if not isinstance(raw, list):
        return ()
    return tuple(dict.fromkeys(str(b) for b in raw if isinstance(b, str) and b))


def _due(value: Any) -> datetime | None:
    """The aware due moment (Trello sends UTC ``...Z``); never reduced to a UTC date here."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("due is not a string")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class TrelloTaskSource:
    provider: Literal["trello"] = "trello"

    def __init__(
        self,
        *,
        base_url: str = TRELLO_API,
        timeout_s: float = REQUEST_TIMEOUT_S,
        cache_ttl_s: float = CACHE_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s)
        self._ttl = cache_ttl_s
        self._monotonic = monotonic
        self._cache: dict[uuid.UUID, _CacheEntry] = {}

    # --- protocol ---------------------------------------------------------------------------

    async def validate(self, credentials: dict[str, Any]) -> TaskSourceIdentity:
        auth = _credentials(credentials)
        async with self._client() as client:
            data = await self._get(
                client, auth, "/1/members/me", {"fields": "id,username,fullName"}
            )
        try:
            return TaskSourceIdentity(
                id=str(data["id"]),
                username=str(data.get("username") or ""),
                full_name=str(data.get("fullName") or ""),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise TaskSourceUnavailable(
                f"unexpected trello payload ({type(exc).__name__})"
            ) from None

    async def list_containers(self, credentials: dict[str, Any]) -> list[Container]:
        auth = _credentials(credentials)
        async with self._client() as client:
            data = await self._get(
                client, auth, "/1/members/me/boards", {"filter": "open", "fields": "id,name,closed"}
            )
        try:
            return [
                Container(id=str(board["id"]), name=str(board.get("name") or ""))
                for board in _as_list(data)
                if not board.get("closed")
            ]
        except (KeyError, TypeError, AttributeError) as exc:
            raise TaskSourceUnavailable(
                f"unexpected trello payload ({type(exc).__name__})"
            ) from None

    async def list_open_tasks(self, conn: TaskSourceConnection) -> list[Task]:
        auth = _credentials(conn.credentials or {})
        boards = _board_ids(conn)
        if not boards:
            return []
        key = (boards, _fingerprint(auth[1]))
        now = self._monotonic()
        self._prune(now)
        entry = self._cache.get(conn.user_id)
        if entry is not None and entry.key == key and now - entry.fetched_at < self._ttl:
            return TaskList(entry.tasks, skipped_containers=entry.skipped_containers)
        async with self._client() as client:
            per_board = await asyncio.gather(
                *(self._board_tasks(client, auth, b) for b in boards), return_exceptions=True
            )
        failures = [r for r in per_board if isinstance(r, BaseException)]
        for failure in failures:
            if not isinstance(failure, Exception):  # cancellation and the like: not ours
                raise failure
        if len(failures) == len(per_board):
            first = failures[0]
            if isinstance(first, TaskSourceUnavailable):
                raise first
            raise TaskSourceUnavailable(f"trello load failed ({type(first).__name__})")
        tasks = [
            task for board in per_board if not isinstance(board, BaseException) for task in board
        ]
        self._cache[conn.user_id] = _CacheEntry(
            key=key, fetched_at=now, tasks=tuple(tasks), skipped_containers=len(failures)
        )
        return TaskList(tasks, skipped_containers=len(failures))

    # --- cache --------------------------------------------------------------------------------

    def cached_tasks(self, user_id: uuid.UUID) -> list[Task] | None:
        """The user's last fetched tasks, up to a day old (labels only; never for drafting)."""
        self._prune(self._monotonic())
        entry = self._cache.get(user_id)
        return list(entry.tasks) if entry is not None else None

    def _prune(self, now: float) -> None:
        for user_id in [u for u, e in self._cache.items() if now - e.fetched_at > CACHE_MAX_AGE_S]:
            del self._cache[user_id]

    def invalidate(self, user_id: uuid.UUID) -> None:
        self._cache.pop(user_id, None)

    def clear_cache(self) -> None:
        self._cache.clear()

    # --- internals ------------------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)

    async def _get(
        self,
        client: httpx.AsyncClient,
        auth: tuple[str, str],
        path: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        query = {**(params or {}), "key": auth[0], "token": auth[1]}
        try:
            response = await client.get(path, params=query)
        except httpx.HTTPError as exc:  # str(exc) may contain the URL: never use it
            raise TaskSourceUnavailable(f"trello request failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise TaskSourceUnavailable(f"trello returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise TaskSourceUnavailable("trello returned invalid JSON") from None

    async def _board_tasks(
        self, client: httpx.AsyncClient, auth: tuple[str, str], board_id: str
    ) -> list[Task]:
        base = f"/1/boards/{board_id}"
        cards, lists, members = await asyncio.gather(
            self._get(client, auth, f"{base}/cards", dict(CARD_PARAMS)),
            self._get(client, auth, f"{base}/lists", {"fields": "id,name"}),
            self._get(client, auth, f"{base}/members", {"fields": "id,fullName"}),
        )
        try:
            list_names = {str(item["id"]): str(item.get("name") or "") for item in _as_list(lists)}
            member_names = {
                str(item["id"]): str(item.get("fullName") or "") for item in _as_list(members)
            }
            tasks = []
            for card in _as_list(cards):
                if card.get("dueComplete") is True:  # finished: not an open task
                    continue
                comments, due_set_at = _card_actions(card.get("actions"), member_names)
                tasks.append(
                    Task(
                        id=str(card["id"]),
                        title=str(card.get("name") or ""),
                        status=list_names.get(str(card.get("idList")), ""),
                        due_at=_due(card.get("due")),
                        assignees=[
                            member_names[m]
                            for m in _ids(card.get("idMembers"))
                            if member_names.get(m)
                        ],
                        url=str(card.get("url") or ""),
                        source="trello",
                        description=_text(card.get("desc"), DESCRIPTION_MAX_CHARS),
                        description_at=_created_at(str(card["id"])),
                        comments=comments,
                        due_set_at=due_set_at,
                    )
                )
            return tasks
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise TaskSourceUnavailable(
                f"unexpected trello payload ({type(exc).__name__})"
            ) from None


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _created_at(card_id: str) -> datetime | None:
    """When the card was created: a Trello id starts with the creation time (8 hex digits of a
    Unix timestamp). The anchor for relative dates in the description (edits are not fetched)."""
    try:
        return datetime.fromtimestamp(int(card_id[:8], 16), UTC) if len(card_id) >= 8 else None
    except (ValueError, OverflowError, OSError):
        return None


def _card_actions(
    actions: Any, member_names: dict[str, str]
) -> tuple[tuple[TaskComment, ...], datetime | None]:
    """(last comments oldest first, newest due change) from a card's nested actions.

    Trello returns actions newest first; one that is not shaped as expected is skipped."""
    if not isinstance(actions, list):
        return (), None
    comments: list[TaskComment] = []
    due_set_at: datetime | None = None
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("data"), dict):
            continue
        data: dict[str, Any] = action["data"]
        try:
            at = _due(action.get("date"))
        except ValueError:
            continue
        if at is None:
            continue
        kind = action.get("type")
        if kind == "updateCard":
            old = data.get("old")
            if isinstance(old, dict) and "due" in old and (due_set_at is None or at > due_set_at):
                due_set_at = at
            continue
        if kind != "commentCard" or len(comments) >= COMMENTS_PER_CARD:
            continue
        text = _text(data.get("text"), COMMENT_MAX_CHARS)
        if not text:
            continue
        creator = action.get("memberCreator")
        author = creator.get("fullName") if isinstance(creator, dict) else None
        if not isinstance(author, str) or not author:
            author = member_names.get(str(action.get("idMemberCreator")), "")
        comments.append(
            TaskComment(id=str(action.get("id") or ""), text=text, author=author, at=at)
        )
    comments.sort(key=lambda comment: comment.at)
    return tuple(comments), due_set_at


def _as_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise TypeError("expected a list of objects")
    return data


def _ids(value: Any) -> Sequence[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("expected a list of ids")
    return [str(v) for v in value]


__all__ = [
    "ACTIONS_LIMIT",
    "CACHE_TTL_S",
    "CARD_PARAMS",
    "COMMENTS_PER_CARD",
    "TRELLO_API",
    "TrelloTaskSource",
]
