"""The Jira client only reads (specs/003-jira-commitments T019, research J3, SC-007)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from scribe.context.tasks.base import ReadOnlyViolation, TaskSourceUnavailable
from scribe.context.tasks.jira.client import JiraClient, JiraOptions, build_http_client
from scribe.context.tasks.jira.flavors import flavor_for
from tests.contract.fixtures.jira import api
from tests.contract.fixtures.jira.api import FLAVORS, FakeJira, Flavor, Issue

ROOT = Path(__file__).resolve().parents[2]
JIRA_SRC = ROOT / "src/scribe/context/tasks/jira"
CREATED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _client(flavor: Flavor, router: respx.Router) -> JiraClient:
    f = flavor_for(api.config(flavor), api.credentials(flavor))
    http = build_http_client(f, JiraOptions(), transport=httpx.MockTransport(router.async_handler))
    return JiraClient(f, http)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("flavor", FLAVORS)
async def test_jira_client_rejects_non_get(flavor: Flavor, method: str) -> None:
    router = respx.Router(assert_all_called=False)
    route = router.route().mock(return_value=httpx.Response(200, json={}))
    f = flavor_for(api.config(flavor), api.credentials(flavor))
    async with build_http_client(
        f, JiraOptions(), transport=httpx.MockTransport(router.async_handler)
    ) as http:
        with pytest.raises(ReadOnlyViolation):
            await http.request(method, "/rest/api/2/issue/PAY-1/comment", json={"body": "x"})
    assert not route.called  # refused before anything left the process


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_redirects_are_not_followed(flavor: Flavor) -> None:
    router = respx.Router(assert_all_called=False)
    elsewhere = router.get("https://evil.example/").mock(return_value=httpx.Response(200))
    router.get(url__startswith=api.api(flavor)).mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example/"})
    )
    async with _client(flavor, router) as client:
        with pytest.raises(TaskSourceUnavailable, match="redirect"):
            await client.myself()
    assert not elsewhere.called


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_every_client_method_only_gets(flavor: Flavor) -> None:
    router = respx.Router(assert_all_called=False)
    jira = FakeJira(flavor, issues=[Issue("PAY-1", "Synthetic", created=CREATED)])
    api.mount(router, jira)
    async with _client(flavor, router) as client:
        await client.myself()
        await client.server_info()
        await client.projects()
        _ = [i async for i in client.search('project in ("PAY")', fields=["summary"], limit=5)]
        await client.issue("PAY-1", fields=["status"])
        await client.comment_ids("PAY-1")
    assert router.calls
    assert {call.request.method for call in router.calls} == {"GET"}


def test_jira_package_never_writes() -> None:
    """No module of the Jira package names a write method or calls httpx directly."""
    for path in JIRA_SRC.glob("*.py"):
        code = path.read_text(encoding="utf-8")
        assert not re.search(r"\.(post|put|patch|delete)\(", code), path.name
        assert not re.search(r"""["'](POST|PUT|PATCH|DELETE)["']\s*[,)]""", code), path.name
        if path.name not in {"client.py", "netguard.py", "source.py"}:
            assert "httpx.AsyncClient(" not in code, path.name


def test_build_http_client_ignores_proxy_environment() -> None:
    f = flavor_for(api.config("self_hosted"), api.credentials("self_hosted"))
    client = build_http_client(f, JiraOptions())
    assert client.trust_env is False
    assert client.follow_redirects is False
