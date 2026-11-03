"""Jira task source for Jira Cloud and self-hosted Jira (research J1 – J7, FR-J00 – FR-J06a).

- :meth:`JiraTaskSource.connect` checks a new connection: address (``netguard``, including the
  operator's allowed ports), Cloud
  ``cloudId`` from ``<site>/_edge/tenant_info``, self-hosted version (``serverInfo`` against
  ``SCRIBE_JIRA_DC_MIN_VERSION``), then ``/myself``. A company-issued certificate surfaces as
  :class:`JiraCertificateUntrusted` carrying the server's certificate (read with a bare TLS
  handshake, no request sent) so the user can trust it; a Cloud token the gateway rejects but
  the site accepts is a classic, unscoped token (``JiraAuthFailed(hint="classic_token")``).
- :meth:`list_open_tasks`: one paged search (``tasks_jql``, at most :data:`MAX_TASKS`) with
  comments, description and the changelog, mapped by :func:`issue_to_task`: ``due_at`` is the
  ``duedate`` at noon UTC with ``due_is_date``, ``comments`` the last :data:`COMMENTS_PER_TASK`
  visible comments without quoted text (restricted comments are left out), ``due_set_at`` the
  newest ``duedate`` change in the changelog. The task service then resolves the effective
  deadline exactly as for Trello (issue #6). Cached 60 s per user, keyed by projects, site and
  a token fingerprint; nothing is persisted or logged.

Read-only: every request goes through :func:`~scribe.context.tasks.jira.client.build_http_client`.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as dtime
from typing import Any, Literal

import httpx

from scribe.context.tasks.base import (
    CertInfo,
    Container,
    JiraAuthFailed,
    JiraCertificateUntrusted,
    JiraVersionUnsupported,
    Task,
    TaskComment,
    TaskList,
    TaskSourceIdentity,
    TaskSourceUnavailable,
)
from scribe.context.tasks.jira.attempts import AttemptLimiter
from scribe.context.tasks.jira.bodies import CommentText
from scribe.context.tasks.jira.client import (
    JiraClient,
    JiraIdentity,
    JiraOptions,
    build_http_client,
    get_json,
)
from scribe.context.tasks.jira.flavors import CloudFlavor, JiraFlavor, flavor_for
from scribe.context.tasks.jira.jql import parse_version, tasks_jql
from scribe.context.tasks.jira.netguard import check_address, validate_site_url
from scribe.context.tasks.jira.tls import fetch_leaf_certificate
from scribe.db.models import TaskSourceConnection

CACHE_TTL_S = 60.0
CACHE_MAX_AGE_S = 24 * 3600.0
MAX_TASKS = 100
COMMENTS_PER_TASK = 5
COMMENT_MAX_CHARS = 1000
DESCRIPTION_MAX_CHARS = 2000
TASK_FIELDS = ("summary", "status", "duedate", "assignee", "reporter", "description", "comment")
TASK_FIELDS_ALL = (*TASK_FIELDS, "created")
ATLASSIAN_HOSTS = (".atlassian.net", ".jira.com")

TransportFactory = Callable[[str], httpx.AsyncBaseTransport]
CertFetcher = Callable[..., Any]


@dataclass(frozen=True)
class JiraComment:
    """One comment as the Jira code uses it (third-party text: never logged or persisted)."""

    id: str
    author_id: str
    author_name: str
    created: datetime
    updated: datetime
    text: CommentText
    restricted: bool


@dataclass(frozen=True)
class JiraConnectResult:
    """A checked connection: who it is and the non-secret config to store."""

    identity: TaskSourceIdentity
    config: dict[str, Any]


def parse_jira_time(value: Any) -> datetime | None:
    """Jira timestamps (``2026-10-21T14:05:33.123+0300``) as aware datetimes."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _due(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        day = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return datetime.combine(day, dtime(12, 0), tzinfo=UTC)


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _name(user: Any) -> str:
    return str(user.get("displayName") or "") if isinstance(user, dict) else ""


def _resolver(issue: dict[str, Any], flavor: JiraFlavor) -> Callable[[str], str | None]:
    """Mention id -> display name from the users this issue mentions anyway."""
    names: dict[str, str] = {}
    raw_fields = issue.get("fields")
    fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    users: list[Any] = [fields.get("assignee"), fields.get("reporter")]
    comment = fields.get("comment")
    if isinstance(comment, dict) and isinstance(comment.get("comments"), list):
        users += [c.get("author") for c in comment["comments"] if isinstance(c, dict)]
    for user in users:
        if not isinstance(user, dict) or not _name(user):
            continue
        for key in ("accountId", "key", "name"):
            if isinstance(user.get(key), str) and user[key]:
                names[user[key]] = _name(user)
    return names.get


def issue_comments(issue: dict[str, Any], flavor: JiraFlavor) -> list[JiraComment]:
    """All comments included in a search / issue payload, oldest first; malformed ones are
    skipped, never the issue."""
    fields = issue.get("fields")
    comment = fields.get("comment") if isinstance(fields, dict) else None
    raw = comment.get("comments") if isinstance(comment, dict) else None
    if not isinstance(raw, list):
        return []
    resolve = _resolver(issue, flavor)
    found: list[JiraComment] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        created = parse_jira_time(item.get("created"))
        if created is None:
            continue
        updated = parse_jira_time(item.get("updated")) or created
        author = item.get("author")
        found.append(
            JiraComment(
                id=str(item["id"]),
                author_id=flavor.user_id(author),
                author_name=_name(author),
                created=created,
                updated=updated,
                text=flavor.normalize_body(item.get("body"), resolve),
                restricted=isinstance(item.get("visibility"), dict),
            )
        )
    found.sort(key=lambda c: c.created)
    return found


def _due_set_at(issue: dict[str, Any]) -> datetime | None:
    changelog = issue.get("changelog")
    histories = changelog.get("histories") if isinstance(changelog, dict) else None
    if not isinstance(histories, list):
        return None
    newest: datetime | None = None
    for history in histories:
        if not isinstance(history, dict) or not isinstance(history.get("items"), list):
            continue
        if not any(
            isinstance(i, dict) and (i.get("fieldId") == "duedate" or i.get("field") == "duedate")
            for i in history["items"]
        ):
            continue
        at = parse_jira_time(history.get("created"))
        if at is not None and (newest is None or at > newest):
            newest = at
    return newest


def issue_to_task(issue: dict[str, Any], flavor: JiraFlavor) -> Task:
    """One search hit as a :class:`Task` (research J7); raises ``KeyError`` / ``TypeError`` on a
    payload without key or fields."""
    key = str(issue["key"])
    fields: dict[str, Any] = issue["fields"]
    status = fields.get("status")
    assignee = fields.get("assignee")
    visible = [c for c in issue_comments(issue, flavor) if not c.restricted and c.text.plain]
    comments = tuple(
        TaskComment(
            id=c.id, text=_cut(c.text.plain, COMMENT_MAX_CHARS), author=c.author_name, at=c.updated
        )
        for c in visible[-COMMENTS_PER_TASK:]
    )
    description = fields.get("description")
    return Task(
        id=key,
        title=str(fields.get("summary") or ""),
        status=str(status.get("name") or "") if isinstance(status, dict) else "",
        due_at=_due(fields.get("duedate")),
        due_is_date=True,
        assignees=[_name(assignee)] if _name(assignee) else [],
        url=flavor.browse_url(key),
        source="jira",
        description=_cut(flavor.normalize_body(description).plain, DESCRIPTION_MAX_CHARS)
        if description
        else "",
        description_at=parse_jira_time(fields.get("created")),
        comments=comments,
        due_set_at=_due_set_at(issue),
    )


@dataclass(frozen=True)
class _CacheEntry:
    key: tuple[tuple[str, ...], str, str]
    fetched_at: float
    tasks: tuple[Task, ...]


def _project_keys(conn: TaskSourceConnection) -> tuple[str, ...]:
    raw = (conn.config_json or {}).get("project_keys") or []
    if not isinstance(raw, list):
        return ()
    return tuple(dict.fromkeys(str(k) for k in raw if isinstance(k, str) and k))


def _fingerprint(credentials: dict[str, Any]) -> str:
    secret = str(credentials.get("api_token") or credentials.get("pat") or "")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


class JiraTaskSource:
    provider: Literal["jira"] = "jira"

    def __init__(
        self,
        options: JiraOptions | None = None,
        *,
        cache_ttl_s: float = CACHE_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
        transport_factory: TransportFactory | None = None,
        cert_fetcher: CertFetcher = fetch_leaf_certificate,
    ) -> None:
        self._options = options
        self._ttl = cache_ttl_s
        self._monotonic = monotonic
        self._transport_factory = transport_factory  # tests: a mock transport per base URL
        self._cert_fetcher = cert_fetcher
        self._cache: dict[uuid.UUID, _CacheEntry] = {}
        # Issue #61: the connect flow takes an attempt per user before calling ``connect``.
        self.connect_attempts = AttemptLimiter(monotonic=monotonic)

    @property
    def options(self) -> JiraOptions:
        if self._options is None:
            try:
                from scribe.config import get_settings

                self._options = JiraOptions.from_settings(get_settings())
            except Exception:  # no environment (tests, tools): the safe defaults
                self._options = JiraOptions()
        return self._options

    # --- clients --------------------------------------------------------------------------------

    def _http(
        self,
        flavor: JiraFlavor,
        *,
        pinned_pem: str | None,
        base_url: str | None = None,
        authenticated: bool = True,
    ) -> httpx.AsyncClient:
        base = base_url or flavor.api_base
        transport = self._transport_factory(base) if self._transport_factory else None
        return build_http_client(
            flavor,
            self.options,
            pinned_pem=pinned_pem,
            base_url=base,
            authenticated=authenticated,
            transport=transport,
        )

    def client(self, config: dict[str, Any], credentials: dict[str, Any]) -> JiraClient:
        """A client for a stored connection (the comment sync uses it too)."""
        flavor = flavor_for(config, credentials)
        return JiraClient(flavor, self._http(flavor, pinned_pem=_pinned_pem(config, credentials)))

    # --- protocol ---------------------------------------------------------------------------------

    async def validate(self, credentials: dict[str, Any]) -> TaskSourceIdentity:
        return (await self.connect(credentials)).identity

    async def connect(self, credentials: dict[str, Any]) -> JiraConnectResult:
        """Check new credentials end to end; see the module docstring for the error cases."""
        deployment = credentials.get("deployment")
        site = validate_site_url(
            str(credentials.get("site_url") or ""), allowed_ports=self.options.allowed_ports
        )
        pinned = _pinned_pem({}, credentials)
        try:
            if deployment == "cloud":
                return await self._connect_cloud(credentials, site.url, pinned)
            return await self._connect_self_hosted(credentials, site.url, pinned)
        except JiraCertificateUntrusted as exc:
            if exc.cert is not None or deployment == "cloud":
                raise
            raise JiraCertificateUntrusted(
                str(exc), cert=await self._presented_certificate(site.host, site.port)
            ) from None

    async def _presented_certificate(self, host: str, port: int) -> CertInfo:
        addresses = await check_address(host, port, self.options.allowed_private_hosts)
        info: CertInfo = await self._cert_fetcher(host, port, address=str(addresses[0]))
        return info

    async def _connect_cloud(
        self, credentials: dict[str, Any], site_url: str, pinned: str | None
    ) -> JiraConnectResult:
        probe = CloudFlavor(
            site_url=site_url,
            cloud_id="",
            email=str(credentials.get("email") or ""),
            api_token=str(credentials.get("api_token") or ""),
        )
        async with self._http(
            probe, pinned_pem=pinned, base_url=site_url, authenticated=False
        ) as http:
            tenant = await get_json(http, "/_edge/tenant_info")
        cloud_id = tenant.get("cloudId") if isinstance(tenant, dict) else None
        if not isinstance(cloud_id, str) or not cloud_id:
            raise TaskSourceUnavailable("this address is not a jira cloud site")
        config = {"deployment": "cloud", "site_url": site_url, "cloud_id": cloud_id}
        flavor = flavor_for(config, credentials)
        try:
            async with JiraClient(flavor, self._http(flavor, pinned_pem=pinned)) as client:
                me = await client.myself()
        except JiraAuthFailed:
            # The token is only ever sent to the site itself when that is Atlassian's own host.
            host = validate_site_url(site_url, allowed_ports=None).host
            atlassian = host.endswith(ATLASSIAN_HOSTS)
            if atlassian and await self._site_accepts(flavor, site_url, pinned):
                raise JiraAuthFailed(
                    "jira rejected the token for the API gateway", hint="classic_token"
                ) from None
            raise
        return _result(config | {"api_base_url": flavor.api_base}, me, server_version=None)

    async def _site_accepts(self, flavor: JiraFlavor, site_url: str, pinned: str | None) -> bool:
        """Whether the site itself accepts the token (a classic, unscoped API token)."""
        try:
            async with self._http(flavor, pinned_pem=pinned, base_url=site_url) as http:
                await get_json(http, f"{flavor.api_prefix}/myself", auth_path=True)
        except TaskSourceUnavailable:
            return False
        return True

    async def _connect_self_hosted(
        self, credentials: dict[str, Any], site_url: str, pinned: str | None
    ) -> JiraConnectResult:
        config = {"deployment": "self_hosted", "site_url": site_url}
        flavor = flavor_for(config, credentials)
        async with JiraClient(flavor, self._http(flavor, pinned_pem=pinned)) as client:
            info = await client.server_info()
            version = info.version if info is not None else ""
            try:
                parsed = parse_version(version)
            except ValueError:
                raise TaskSourceUnavailable("this address is not a jira server") from None
            minimum = self.options.dc_min_version
            if parsed < parse_version(minimum):
                raise JiraVersionUnsupported(version, minimum)
            me = await client.myself()
        return _result(config | {"api_base_url": flavor.api_base}, me, server_version=version)

    async def list_containers(self, credentials: dict[str, Any]) -> list[Container]:
        """Projects for the picker; ``credentials`` holds the stored config fields too."""
        async with self.client(credentials, credentials) as client:
            return await client.projects()

    async def list_open_tasks(self, conn: TaskSourceConnection) -> list[Task]:
        config, credentials = dict(conn.config_json or {}), dict(conn.credentials or {})
        keys = _project_keys(conn)
        if not keys:
            return []
        key = (keys, str(config.get("site_url") or ""), _fingerprint(credentials))
        now = self._monotonic()
        self._prune(now)
        entry = self._cache.get(conn.user_id)
        if entry is not None and entry.key == key and now - entry.fetched_at < self._ttl:
            return TaskList(entry.tasks)
        tasks: list[Task] = []
        async with self.client(config, credentials) as client:
            async for issue in client.search(
                tasks_jql(keys), fields=TASK_FIELDS_ALL, expand=("changelog",), limit=MAX_TASKS
            ):
                try:
                    tasks.append(issue_to_task(issue, client.flavor))
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue  # a malformed issue is skipped, never the load
        self._cache[conn.user_id] = _CacheEntry(key=key, fetched_at=now, tasks=tuple(tasks))
        return TaskList(tasks)

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


def _pinned_pem(config: dict[str, Any], credentials: dict[str, Any]) -> str | None:
    pinned = config.get("pinned_cert") or credentials.get("pinned_cert")
    pem = pinned.get("pem") if isinstance(pinned, dict) else None
    return pem if isinstance(pem, str) and pem else None


def _result(
    config: dict[str, Any], me: JiraIdentity, *, server_version: str | None
) -> JiraConnectResult:
    identity = TaskSourceIdentity(
        id=me.id,
        username=me.name,
        full_name=me.display_name,
        time_zone=me.time_zone,
        server_version=server_version,
    )
    stored = dict(config)
    stored["identity"] = {
        "id": me.id,
        "name": me.name,
        "display_name": me.display_name,
        "time_zone": me.time_zone,
    }
    if server_version is not None:
        stored["server_version"] = server_version
    return JiraConnectResult(identity=identity, config=stored)


__all__ = [
    "COMMENTS_PER_TASK",
    "JiraComment",
    "JiraConnectResult",
    "JiraTaskSource",
    "MAX_TASKS",
    "issue_comments",
    "issue_to_task",
    "parse_jira_time",
]
