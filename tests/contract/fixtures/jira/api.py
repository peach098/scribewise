"""Synthetic Jira REST payloads for both deployments, built from one neutral description.

``Flavor = "cloud" | "self_hosted"``: Cloud answers API v3 shapes with ADF bodies and
``accountId`` users; self-hosted answers v2 shapes with wiki-markup bodies and ``key`` / ``name``
users. :func:`mount` registers a whole fake Jira (myself, server info, tenant info, projects,
search, issues, comments) on a ``respx.Router`` the way the real endpoints are laid out, so
contract and integration tests run the same scenario against both flavors. Every name, id and
text here is invented.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx
import respx

from tests.contract.fixtures.jira.render import Block, to_adf, to_wiki

Flavor = Literal["cloud", "self_hosted"]
FLAVORS: tuple[Flavor, ...] = ("cloud", "self_hosted")

CLOUD_SITE = "https://acme.atlassian.net"
CLOUD_ID = "11111111-2222-3333-4444-555555555555"
CLOUD_API = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"
DC_SITE = "https://jira.example.com/jira"
EMAIL = "me@example.com"
API_TOKEN = "synthetic-scoped-token"
PAT = "synthetic-personal-access-token"


def site(flavor: Flavor) -> str:
    return CLOUD_SITE if flavor == "cloud" else DC_SITE


def api(flavor: Flavor) -> str:
    return CLOUD_API if flavor == "cloud" else DC_SITE


def prefix(flavor: Flavor) -> str:
    return "/rest/api/3" if flavor == "cloud" else "/rest/api/2"


def credentials(flavor: Flavor) -> dict[str, Any]:
    if flavor == "cloud":
        return {
            "deployment": "cloud",
            "site_url": CLOUD_SITE,
            "email": EMAIL,
            "api_token": API_TOKEN,
        }
    return {"deployment": "self_hosted", "site_url": DC_SITE, "pat": PAT}


def config(flavor: Flavor, project_keys: Sequence[str] = ("PAY",)) -> dict[str, Any]:
    """A stored ``config_json`` as the connect flow writes it."""
    base: dict[str, Any] = {
        "deployment": flavor,
        "site_url": site(flavor),
        "api_base_url": api(flavor),
        "project_keys": list(project_keys),
        "identity": {"id": ME.id, "name": "me", "display_name": ME.name, "time_zone": "UTC"},
    }
    if flavor == "cloud":
        base["cloud_id"] = CLOUD_ID
    return base


@dataclass(frozen=True)
class Person:
    id: str  # accountId on Cloud, user key / name on self-hosted
    name: str


ME = Person("u-me", "Mia Example")
ANNA = Person("u-anna", "Anna Testova")
IVAN = Person("u-ivan", "Ivan Sample")


def user(flavor: Flavor, person: Person | None) -> dict[str, Any] | None:
    if person is None:
        return None
    if flavor == "cloud":
        return {"accountId": person.id, "displayName": person.name, "active": True}
    return {"key": person.id, "name": person.id, "displayName": person.name, "active": True}


def jira_time(moment: datetime) -> str:
    """Jira's timestamp format: ``2026-10-21T14:05:33.000+0000``."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def body(flavor: Flavor, blocks: Sequence[Block]) -> Any:
    return to_adf(blocks) if flavor == "cloud" else to_wiki(blocks, cloud=False)


@dataclass
class Comment:
    id: str
    author: Person
    blocks: Sequence[Block]
    created: datetime
    updated: datetime | None = None
    restricted: bool = False

    def payload(self, flavor: Flavor) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "author": user(flavor, self.author),
            "body": body(flavor, self.blocks),
            "created": jira_time(self.created),
            "updated": jira_time(self.updated or self.created),
        }
        if self.restricted:
            data["visibility"] = {"type": "group", "value": "staff"}
        return data


@dataclass
class Issue:
    key: str
    summary: str
    created: datetime
    status: str = "In Progress"
    status_category: str = "indeterminate"  # "done" for finished issues
    duedate: str | None = None
    assignee: Person | None = ME
    reporter: Person | None = ANNA
    description: Sequence[Block] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    due_changes: list[datetime] = field(default_factory=list)
    updated: datetime | None = None

    def payload(self, flavor: Flavor, *, with_changelog: bool = True) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "summary": self.summary,
            "status": {"name": self.status, "statusCategory": {"key": self.status_category}},
            "duedate": self.duedate,
            "assignee": user(flavor, self.assignee),
            "reporter": user(flavor, self.reporter),
            "description": body(flavor, self.description) if self.description else None,
            "created": jira_time(self.created),
            "updated": jira_time(self.updated or self.created),
            "comment": {
                "comments": [c.payload(flavor) for c in self.comments],
                "total": len(self.comments),
                "maxResults": len(self.comments),
                "startAt": 0,
            },
        }
        data: dict[str, Any] = {"id": f"id-{self.key}", "key": self.key, "fields": fields}
        if with_changelog:
            data["changelog"] = {
                "histories": [
                    {
                        "id": f"h{i}",
                        "created": jira_time(at),
                        "items": [{"field": "duedate", "fieldId": "duedate"}],
                    }
                    for i, at in enumerate(self.due_changes)
                ]
                + [{"id": "hx", "created": jira_time(self.created), "items": [{"field": "status"}]}]
            }
        return data


def search_pages(flavor: Flavor, issues: list[dict[str, Any]], page_size: int) -> list[Any]:
    """The pages a search answers, in order, for ``page_size`` issues per page."""
    chunks = [issues[i : i + page_size] for i in range(0, len(issues), page_size)] or [[]]
    pages: list[Any] = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        if flavor == "cloud":
            page: dict[str, Any] = {"issues": chunk, "isLast": last}
            if not last:
                page["nextPageToken"] = f"token-{index + 1}"
        else:
            page = {
                "issues": chunk,
                "startAt": index * page_size,
                "maxResults": page_size,
                "total": len(issues),
            }
        pages.append(page)
    return pages


@dataclass
class FakeJira:
    """A fake Jira site: set fields, then :func:`mount` it on a router."""

    flavor: Flavor
    issues: list[Issue] = field(default_factory=list)
    scan_issues: list[Issue] | None = None  # the comment-scan search; default ``issues``
    version: tuple[int, ...] = (10, 3, 4)
    projects: list[tuple[str, str]] = field(
        default_factory=lambda: [("PAY", "Payments"), ("CORE", "Core")]
    )
    myself_status: int = 200
    server_info_status: int = 200  # self-hosted: 401 when the site hides it from bad tokens
    site_myself_status: int = 401  # Cloud: what the site (not the gateway) answers
    search_status: int = 200
    gone: set[str] = field(default_factory=set)  # issue keys answering 404
    deleted_comments: set[str] = field(default_factory=set)
    retry_after: str | None = None
    page_size: int = 50
    searches: list[httpx.Request] = field(default_factory=list)

    def all_issues(self) -> dict[str, Issue]:
        found = {i.key: i for i in self.issues}
        for issue in self.scan_issues or []:
            found.setdefault(issue.key, issue)
        return found


def _paged_response(
    flavor: Flavor, request: httpx.Request, issues: list[dict[str, Any]], page_size: int
) -> httpx.Response:
    pages = search_pages(flavor, issues, page_size)
    if flavor == "cloud":
        token = request.url.params.get("nextPageToken")
        index = int(token.split("-")[1]) if token else 0
    else:
        index = int(request.url.params.get("startAt", "0")) // page_size
    return httpx.Response(200, json=pages[min(index, len(pages) - 1)])


def mount(router: respx.Router, jira: FakeJira) -> respx.Router:
    flavor, base, p = jira.flavor, api(jira.flavor), prefix(jira.flavor)
    me = {
        **(user(flavor, ME) or {}),
        "emailAddress": EMAIL if flavor == "cloud" else None,
        "timeZone": "Europe/Moscow",
    }
    router.get(f"{base}{p}/myself").mock(
        return_value=httpx.Response(
            jira.myself_status, json=me if jira.myself_status == 200 else {}
        )
    )
    if flavor == "cloud":
        router.get(f"{CLOUD_SITE}/_edge/tenant_info").mock(
            return_value=httpx.Response(200, json={"cloudId": CLOUD_ID})
        )
        router.get(f"{CLOUD_SITE}{p}/myself").mock(
            return_value=httpx.Response(jira.site_myself_status, json=me)
        )
        router.get(f"{base}{p}/project/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "values": [{"key": k, "name": n} for k, n in jira.projects],
                    "isLast": True,
                },
            )
        )
    else:
        router.get(f"{base}{p}/serverInfo").mock(
            return_value=httpx.Response(
                jira.server_info_status,
                json={
                    "version": ".".join(str(n) for n in jira.version),
                    "versionNumbers": list(jira.version),
                    "deploymentType": "Server",
                },
            )
        )
        router.get(f"{base}{p}/project").mock(
            return_value=httpx.Response(200, json=[{"key": k, "name": n} for k, n in jira.projects])
        )

    def search(request: httpx.Request) -> httpx.Response:
        jira.searches.append(request)
        if jira.search_status != 200:
            headers = {"Retry-After": jira.retry_after} if jira.retry_after else {}
            return httpx.Response(jira.search_status, headers=headers, json={})
        jql = request.url.params.get("jql", "")
        source = (
            jira.scan_issues if "watcher" in jql and jira.scan_issues is not None else jira.issues
        )
        changelog = "changelog" in request.url.params.get("expand", "")
        payloads = [i.payload(flavor, with_changelog=changelog) for i in source]
        return _paged_response(flavor, request, payloads, jira.page_size)

    router.get(f"{base}{p}/search/jql" if flavor == "cloud" else f"{base}{p}/search").mock(
        side_effect=search
    )

    def issue(request: httpx.Request, key: str) -> httpx.Response:
        found = jira.all_issues().get(key)
        if found is None or key in jira.gone:
            return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
        return httpx.Response(200, json=found.payload(flavor, with_changelog=False))

    def comments(request: httpx.Request, key: str) -> httpx.Response:
        found = jira.all_issues().get(key)
        if found is None or key in jira.gone:
            return httpx.Response(404, json={})
        visible = [c for c in found.comments if c.id not in jira.deleted_comments]
        return httpx.Response(
            200,
            json={
                "comments": [c.payload(flavor) for c in visible],
                "startAt": 0,
                "maxResults": len(visible),
                "total": len(visible),
            },
        )

    router.get(url__regex=rf"^{base}{p}/issue/(?P<key>[A-Z0-9_]+-\d+)/comment").mock(
        side_effect=comments
    )
    router.get(url__regex=rf"^{base}{p}/issue/(?P<key>[A-Z0-9_]+-\d+)(\?.*)?$").mock(
        side_effect=issue
    )
    return router


def transport_factory(router: respx.Router) -> Any:
    """``JiraTaskSource(transport_factory=...)``: every client goes to ``router``."""

    def factory(_base_url: str) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(router.async_handler)

    return factory
