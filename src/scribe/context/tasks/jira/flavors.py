"""Jira Cloud vs self-hosted Jira (research J1): everything that differs between the two lives
here, so the client, the task source and the comment sync are shared.

| | :class:`CloudFlavor` | :class:`SelfHostedFlavor` |
|---|---|---|
| API base | ``https://api.atlassian.com/ex/jira/<cloudId>`` (scoped tokens) | the site address |
| Auth | Basic ``email:api_token`` | Bearer personal access token |
| API | ``/rest/api/3`` | ``/rest/api/2`` |
| Search | ``GET /search/jql`` + ``nextPageToken`` | ``GET /search`` + ``startAt`` |
| Bodies | Atlassian Document Format | wiki markup |
| User id | ``accountId`` | ``key`` (else ``name``) |

Browse links always use the site address (``<site>/browse/<KEY>``).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from scribe.context.tasks.base import JiraAddressInvalid, TaskSourceUnavailable
from scribe.context.tasks.jira.bodies import CommentText, adf_to_text, wiki_to_text
from scribe.context.tasks.jira.netguard import validate_site_url

CLOUD_GATEWAY = "https://api.atlassian.com/ex/jira"
Deployment = Literal["cloud", "self_hosted"]
MentionResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class SearchPage:
    issues: list[dict[str, Any]]
    next_cursor: str | None  # None: last page


class JiraFlavor(Protocol):
    @property
    def deployment(self) -> Deployment: ...

    @property
    def site_url(self) -> str: ...

    @property
    def api_base(self) -> str: ...

    @property
    def api_prefix(self) -> str: ...

    def auth_headers(self) -> dict[str, str]: ...

    def search_request(
        self, jql: str, fields: list[str], expand: list[str], page_size: int, cursor: str | None
    ) -> tuple[str, dict[str, str]]: ...

    def search_page(self, data: Any) -> SearchPage: ...

    def normalize_body(self, raw: Any, resolve: MentionResolver | None = None) -> CommentText: ...

    def user_id(self, user: Any) -> str: ...

    def browse_url(self, key: str, comment_id: str | None = None) -> str: ...


def _issues(data: Any) -> list[dict[str, Any]]:
    issues = data.get("issues") if isinstance(data, dict) else None
    if not isinstance(issues, list):
        raise TaskSourceUnavailable("unexpected jira search payload")
    return [issue for issue in issues if isinstance(issue, dict)]


def _common_params(
    jql: str, fields: list[str], expand: list[str], page_size: int
) -> dict[str, str]:
    params = {"jql": jql, "fields": ",".join(fields), "maxResults": str(page_size)}
    if expand:
        params["expand"] = ",".join(expand)
    return params


@dataclass(frozen=True)
class _Base:
    site_url: str

    def browse_url(self, key: str, comment_id: str | None = None) -> str:
        url = f"{self.site_url}/browse/{key}"
        return f"{url}?focusedCommentId={comment_id}" if comment_id else url


@dataclass(frozen=True)
class CloudFlavor(_Base):
    cloud_id: str
    email: str
    api_token: str
    deployment: Deployment = "cloud"
    api_prefix: str = "/rest/api/3"

    @property
    def api_base(self) -> str:
        return f"{CLOUD_GATEWAY}/{self.cloud_id}"

    def auth_headers(self) -> dict[str, str]:
        raw = f"{self.email}:{self.api_token}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}

    def search_request(
        self, jql: str, fields: list[str], expand: list[str], page_size: int, cursor: str | None
    ) -> tuple[str, dict[str, str]]:
        params = _common_params(jql, fields, expand, page_size)
        if cursor:
            params["nextPageToken"] = cursor
        return f"{self.api_prefix}/search/jql", params

    def search_page(self, data: Any) -> SearchPage:
        issues = _issues(data)
        token = data.get("nextPageToken")
        last = data.get("isLast") is True or not isinstance(token, str) or not token
        return SearchPage(issues=issues, next_cursor=None if last else token)

    def normalize_body(self, raw: Any, resolve: MentionResolver | None = None) -> CommentText:
        if isinstance(raw, str):  # some Cloud fields still answer plain text
            return wiki_to_text(raw, resolve)
        return adf_to_text(raw, resolve)

    def user_id(self, user: Any) -> str:
        return str(user.get("accountId") or "") if isinstance(user, dict) else ""


@dataclass(frozen=True)
class SelfHostedFlavor(_Base):
    pat: str
    deployment: Deployment = "self_hosted"
    api_prefix: str = "/rest/api/2"

    @property
    def api_base(self) -> str:
        return self.site_url

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.pat}"}

    def search_request(
        self, jql: str, fields: list[str], expand: list[str], page_size: int, cursor: str | None
    ) -> tuple[str, dict[str, str]]:
        params = _common_params(jql, fields, expand, page_size)
        params["startAt"] = cursor or "0"
        return f"{self.api_prefix}/search", params

    def search_page(self, data: Any) -> SearchPage:
        issues = _issues(data)
        try:
            start = int(data.get("startAt") or 0)
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            raise TaskSourceUnavailable("unexpected jira search payload") from None
        following = start + len(issues)
        return SearchPage(
            issues=issues, next_cursor=str(following) if issues and following < total else None
        )

    def normalize_body(self, raw: Any, resolve: MentionResolver | None = None) -> CommentText:
        return wiki_to_text(raw if isinstance(raw, str) else "", resolve)

    def user_id(self, user: Any) -> str:
        if not isinstance(user, dict):
            return ""
        return str(user.get("key") or user.get("name") or "")


def flavor_for(config: dict[str, Any], credentials: dict[str, Any]) -> JiraFlavor:
    """The flavor of a stored (or about to be stored) connection.

    ``config`` holds the non-secret part (``deployment``, ``site_url``, ``cloud_id``),
    ``credentials`` the secret one; a value missing from either raises
    :class:`TaskSourceUnavailable` (``JiraAddressInvalid`` for a bad site address)."""
    deployment = config.get("deployment") or credentials.get("deployment")
    # Any port: a stored connection may predate the port rule; new addresses are checked
    # against the operator's ports in ``JiraTaskSource.connect`` (issue #61).
    site = validate_site_url(
        str(config.get("site_url") or credentials.get("site_url") or ""), allowed_ports=None
    )
    if deployment == "cloud":
        cloud_id = config.get("cloud_id") or credentials.get("cloud_id")
        email, token = credentials.get("email"), credentials.get("api_token")
        if not (isinstance(cloud_id, str) and cloud_id):
            raise TaskSourceUnavailable("jira cloud id is missing")
        if not (isinstance(email, str) and email and isinstance(token, str) and token):
            raise TaskSourceUnavailable("jira credentials are incomplete")
        return CloudFlavor(site_url=site.url, cloud_id=cloud_id, email=email, api_token=token)
    if deployment == "self_hosted":
        pat = credentials.get("pat")
        if not (isinstance(pat, str) and pat):
            raise TaskSourceUnavailable("jira credentials are incomplete")
        return SelfHostedFlavor(site_url=site.url, pat=pat)
    raise JiraAddressInvalid("unknown jira deployment")


__all__ = [
    "CLOUD_GATEWAY",
    "CloudFlavor",
    "Deployment",
    "JiraFlavor",
    "SearchPage",
    "SelfHostedFlavor",
    "flavor_for",
]
