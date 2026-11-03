"""The GET-only Jira HTTP client (research J1 – J5, contracts/internal-interfaces.md).

:func:`build_http_client` makes the only kind of ``httpx.AsyncClient`` the Jira code uses:

- a request hook raises :class:`ReadOnlyViolation` for any method other than GET / HEAD before
  anything leaves the process (SC-007, research J3: a self-hosted token cannot be read-only);
- redirects are never followed (a redirect cannot move credentials to another host) and proxy
  environment variables are ignored (a proxy would bypass the address guard);
- the transport is :class:`~scribe.context.tasks.jira.netguard.GuardedTransport`: every
  request's host is resolved and checked against private / loopback / link-local / metadata
  addresses (research J4) and the connection goes to the checked address;
- with a pinned certificate (research J5) TLS trusts exactly that certificate.

:class:`JiraClient` maps every failure to a typed :class:`TaskSourceUnavailable` whose message
never contains a token or a URL; httpx/httpcore loggers stay at WARNING (request URLs may carry
issue keys and JQL).
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from scribe.context.tasks.base import (
    Container,
    JiraAuthFailed,
    JiraCertificateUntrusted,
    JiraIssueGone,
    JiraRateLimited,
    JiraUnreachable,
    ReadOnlyViolation,
    TaskSourceUnavailable,
)
from scribe.context.tasks.jira.flavors import JiraFlavor
from scribe.context.tasks.jira.netguard import DEFAULT_ALLOWED_PORTS, GuardedTransport
from scribe.context.tasks.jira.tls import is_cert_verify_error, pinned_ssl_context

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 15.0
PAGE_SIZE = 50
COMMENT_PAGE_SIZE = 100
RETRY_AFTER_DEFAULT_S = 60.0
READ_METHODS = frozenset({"GET", "HEAD"})

for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


@dataclass(frozen=True)
class JiraOptions:
    """Operator settings the Jira code needs (``SCRIBE_JIRA_*``)."""

    allowed_private_hosts: tuple[str, ...] = ()
    dc_min_version: str = "10.3"
    allowed_ports: tuple[int, ...] = DEFAULT_ALLOWED_PORTS  # new addresses only (issue #61)

    @classmethod
    def from_settings(cls, settings: Any) -> JiraOptions:
        return cls(
            allowed_private_hosts=tuple(settings.jira_allowed_private_hosts),
            dc_min_version=str(settings.jira_dc_min_version),
            allowed_ports=tuple(settings.jira_allowed_ports),
        )


@dataclass(frozen=True)
class JiraIdentity:
    id: str  # accountId (Cloud) / key (self-hosted)
    name: str  # email or user name, for display only
    display_name: str
    time_zone: str | None = None


@dataclass(frozen=True)
class ServerInfo:
    version: str
    deployment_type: str = ""


async def _read_only(request: httpx.Request) -> None:
    if request.method.upper() not in READ_METHODS:
        raise ReadOnlyViolation(request.method.upper())


def build_http_client(
    flavor: JiraFlavor,
    options: JiraOptions,
    *,
    pinned_pem: str | None = None,
    base_url: str | None = None,
    authenticated: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The client for ``flavor`` (``base_url`` defaults to its API base). ``transport`` is for
    tests only; production always uses the guarded transport."""
    try:
        verify: ssl.SSLContext | bool = pinned_ssl_context(pinned_pem) if pinned_pem else True
    except (ssl.SSLError, ValueError):
        raise TaskSourceUnavailable("the trusted jira certificate cannot be read") from None
    return httpx.AsyncClient(
        base_url=base_url or flavor.api_base,
        headers={"Accept": "application/json", **(flavor.auth_headers() if authenticated else {})},
        timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        follow_redirects=False,
        trust_env=False,
        transport=transport or GuardedTransport(options.allowed_private_hosts, verify=verify),
        event_hooks={"request": [_read_only]},
    )


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(value))
    except ValueError:
        return RETRY_AFTER_DEFAULT_S


async def get_json(
    http: httpx.AsyncClient,
    path: str,
    params: dict[str, str] | None = None,
    *,
    auth_path: bool = False,
    issue_key: str | None = None,
) -> Any:
    """One GET, every failure mapped (``auth_path``: 401/403 mean bad credentials;
    ``issue_key``: 403/404 mean that issue is gone)."""
    try:
        response = await http.get(path, params=params)
    except (JiraUnreachable, ReadOnlyViolation):
        raise
    except httpx.HTTPError as exc:  # str(exc) may contain the URL: never use it
        if is_cert_verify_error(exc):
            raise JiraCertificateUntrusted("the jira server certificate is not trusted") from None
        if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | httpx.NetworkError):
            raise JiraUnreachable(f"jira did not answer ({type(exc).__name__})") from None
        raise TaskSourceUnavailable(f"jira request failed ({type(exc).__name__})") from None
    except ssl.SSLError:
        raise JiraCertificateUntrusted("the jira server certificate is not trusted") from None
    status = response.status_code
    if status in (429, 503):
        raise JiraRateLimited(_retry_after(response))
    if auth_path and status in (401, 403):
        raise JiraAuthFailed(f"jira rejected the credentials (HTTP {status})")
    if issue_key is not None and status in (403, 404):
        raise JiraIssueGone(issue_key)
    if 300 <= status < 400:
        raise TaskSourceUnavailable(f"jira redirected the request (HTTP {status})")
    if status != 200:
        raise TaskSourceUnavailable(f"jira returned HTTP {status}")
    try:
        return response.json()
    except ValueError:
        raise TaskSourceUnavailable("jira returned invalid JSON") from None


class JiraClient:
    def __init__(self, flavor: JiraFlavor, http: httpx.AsyncClient) -> None:
        self.flavor = flavor
        self._http = http

    async def __aenter__(self) -> JiraClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def myself(self) -> JiraIdentity:
        data = await get_json(self._http, f"{self.flavor.api_prefix}/myself", auth_path=True)
        if not isinstance(data, dict):
            raise TaskSourceUnavailable("unexpected jira payload")
        user_id = self.flavor.user_id(data)
        if not user_id:
            raise TaskSourceUnavailable("unexpected jira payload")
        zone = data.get("timeZone")
        return JiraIdentity(
            id=user_id,
            name=str(data.get("emailAddress") or data.get("name") or ""),
            display_name=str(data.get("displayName") or ""),
            time_zone=zone if isinstance(zone, str) and zone else None,
        )

    async def server_info(self) -> ServerInfo | None:
        """Version of a self-hosted Jira (``None`` on Cloud)."""
        if self.flavor.deployment == "cloud":
            return None
        # Many Jira Data Center sites close serverInfo to anonymous callers, so a 401/403 here
        # means the token was refused rather than that Jira is unavailable.
        data = await get_json(self._http, f"{self.flavor.api_prefix}/serverInfo", auth_path=True)
        if not isinstance(data, dict):
            raise TaskSourceUnavailable("unexpected jira payload")
        numbers = data.get("versionNumbers")
        if isinstance(numbers, list) and numbers and all(isinstance(n, int) for n in numbers):
            version = ".".join(str(n) for n in numbers)
        else:
            version = str(data.get("version") or "")
        return ServerInfo(version=version, deployment_type=str(data.get("deploymentType") or ""))

    async def projects(self) -> list[Container]:
        """Projects the user can browse, as ``Container(id=KEY, name="Name (KEY)")``."""
        found: list[Container] = []
        if self.flavor.deployment == "cloud":
            start = 0
            while True:
                data = await get_json(
                    self._http,
                    f"{self.flavor.api_prefix}/project/search",
                    {"startAt": str(start), "maxResults": str(PAGE_SIZE), "orderBy": "key"},
                )
                values = data.get("values") if isinstance(data, dict) else None
                if not isinstance(values, list):
                    raise TaskSourceUnavailable("unexpected jira payload")
                found += _containers(values)
                if data.get("isLast") is not False or not values:
                    break
                start += len(values)
        else:
            data = await get_json(self._http, f"{self.flavor.api_prefix}/project")
            if not isinstance(data, list):
                raise TaskSourceUnavailable("unexpected jira payload")
            found = _containers(data)
        return sorted(found, key=lambda c: c.id)

    async def search(
        self, jql: str, *, fields: Sequence[str], expand: Sequence[str] = (), limit: int
    ) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        seen = 0
        while seen < limit:
            path, params = self.flavor.search_request(
                jql, list(fields), list(expand), min(PAGE_SIZE, limit - seen), cursor
            )
            page = self.flavor.search_page(await get_json(self._http, path, params))
            for issue in page.issues:
                if seen >= limit:
                    return
                seen += 1
                yield issue
            if page.next_cursor is None or not page.issues:
                return
            cursor = page.next_cursor

    async def issue(self, key: str, *, fields: Sequence[str]) -> dict[str, Any]:
        data = await get_json(
            self._http,
            f"{self.flavor.api_prefix}/issue/{key}",
            {"fields": ",".join(fields)},
            issue_key=key,
        )
        if not isinstance(data, dict):
            raise TaskSourceUnavailable("unexpected jira payload")
        return data

    async def comment_ids(self, key: str) -> set[str]:
        """Ids of every comment the user can see on ``key`` (deletion check, research J11)."""
        ids: set[str] = set()
        start = 0
        while True:
            data = await get_json(
                self._http,
                f"{self.flavor.api_prefix}/issue/{key}/comment",
                {"startAt": str(start), "maxResults": str(COMMENT_PAGE_SIZE)},
                issue_key=key,
            )
            comments = data.get("comments") if isinstance(data, dict) else None
            if not isinstance(comments, list):
                raise TaskSourceUnavailable("unexpected jira payload")
            ids |= {str(c["id"]) for c in comments if isinstance(c, dict) and c.get("id")}
            total = data.get("total")
            start += len(comments)
            if not comments or not isinstance(total, int) or start >= total:
                return ids


def _containers(values: list[Any]) -> list[Container]:
    found = []
    for project in values:
        if not isinstance(project, dict) or not isinstance(project.get("key"), str):
            continue
        key = project["key"]
        name = str(project.get("name") or "")
        found.append(Container(id=key, name=f"{name} ({key})" if name else key))
    return found


__all__ = [
    "JiraClient",
    "JiraIdentity",
    "JiraOptions",
    "ServerInfo",
    "build_http_client",
    "get_json",
]
