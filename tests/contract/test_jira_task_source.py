"""``JiraTaskSource`` against synthetic Cloud and self-hosted Jira (specs/003 T020).

Every scenario runs for both flavors; the mapped tasks must be identical (FR-J00, SC-008).
"""

from __future__ import annotations

import ipaddress
import ssl
import uuid
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

import scribe.context.tasks.jira.source as source_module
from scribe.context.tasks.base import (
    CertInfo,
    JiraAuthFailed,
    JiraCertificateUntrusted,
    JiraRateLimited,
    JiraUnreachable,
    JiraVersionUnsupported,
    Task,
    TaskSourceUnavailable,
)
from scribe.context.tasks.jira import JiraTaskSource
from scribe.context.tasks.jira.client import JiraOptions
from scribe.db.models import TaskProvider, TaskSourceConnection
from tests.contract.fixtures.jira import api
from tests.contract.fixtures.jira.api import (
    ANNA,
    FLAVORS,
    IVAN,
    ME,
    Comment,
    FakeJira,
    Flavor,
    Issue,
)
from tests.contract.fixtures.jira.render import MentionPart, Para, Quote, Text

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


def _refund_issue() -> Issue:
    return Issue(
        key="PAY-142",
        summary="Fix refund rounding",
        created=T0,
        duedate="2026-10-25",
        description=[Para(Text("Refunds round down; target: deadline 30 Oct."))],
        due_changes=[_at(5), _at(3)],
        comments=[
            Comment("10001", ANNA, [Para(Text("Can you look at it?"))], _at(10)),
            Comment(
                "10002",
                ME,
                [
                    Quote(Para(Text("Can you look at it?"))),
                    Para(MentionPart(ANNA.id, ANNA.name), Text(" sure, deadline moved to Nov 5")),
                ],
                _at(11),
                updated=_at(12),
            ),
            Comment("10003", IVAN, [Para(Text("internal note"))], _at(13), restricted=True),
            *[Comment(f"200{i}", IVAN, [Para(Text(f"update {i}"))], _at(14 + i)) for i in range(5)],
        ],
    )


def _conn(flavor: Flavor, **config: Any) -> TaskSourceConnection:
    return TaskSourceConnection(
        user_id=uuid.uuid4(),
        provider=TaskProvider.jira,
        credentials=api.credentials(flavor),
        config_json={**api.config(flavor), **config},
    )


def _source(router: respx.Router, **kwargs: Any) -> JiraTaskSource:
    return JiraTaskSource(JiraOptions(), transport_factory=api.transport_factory(router), **kwargs)


async def _tasks(flavor: Flavor, jira: FakeJira) -> list[Task]:
    router = respx.Router(assert_all_called=False)
    api.mount(router, jira)
    return await _source(router).list_open_tasks(_conn(flavor))


# --- connect / validate --------------------------------------------------------------------------


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_connect_returns_identity_and_config(flavor: Flavor) -> None:
    router = respx.Router(assert_all_called=False)
    api.mount(router, FakeJira(flavor))
    result = await _source(router).connect(api.credentials(flavor))
    assert result.identity.id == ME.id and result.identity.full_name == ME.name
    assert result.identity.time_zone == "Europe/Moscow"
    assert result.config["deployment"] == flavor
    assert result.config["site_url"] == api.site(flavor)
    assert result.config["api_base_url"] == api.api(flavor)
    assert result.config["identity"]["id"] == ME.id
    assert not {"api_token", "pat", "email"} & set(result.config)
    if flavor == "cloud":
        assert result.config["cloud_id"] == api.CLOUD_ID
        auth = router.calls[-1].request.headers["Authorization"]
        assert auth.startswith("Basic ")
        tenant = [c for c in router.calls if c.request.url.path == "/_edge/tenant_info"]
        assert tenant and "Authorization" not in tenant[0].request.headers
    else:
        assert result.identity.server_version == "10.3.4"
        assert router.calls[-1].request.headers["Authorization"] == f"Bearer {api.PAT}"


async def test_old_self_hosted_version_is_refused() -> None:
    router = respx.Router(assert_all_called=False)
    api.mount(router, FakeJira("self_hosted", version=(9, 12, 0)))
    with pytest.raises(JiraVersionUnsupported) as info:
        await _source(router).connect(api.credentials("self_hosted"))
    assert (info.value.found, info.value.minimum) == ("9.12.0", "10.3")
    assert info.value.error_code == "version_unsupported"


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_validate_error_mapping(flavor: Flavor) -> None:
    router = respx.Router(assert_all_called=False)
    api.mount(router, FakeJira(flavor, myself_status=401))
    with pytest.raises(JiraAuthFailed) as info:
        await _source(router).validate(api.credentials(flavor))
    assert info.value.hint is None
    assert api.API_TOKEN not in str(info.value) and api.PAT not in str(info.value)


@pytest.mark.parametrize("status", [401, 403])
async def test_self_hosted_refused_token_on_server_info_is_auth_failed(status: int) -> None:
    """Self-hosted Jira where serverInfo needs a valid token, so a bad PAT fails there first."""
    router = respx.Router(assert_all_called=False)
    api.mount(router, FakeJira("self_hosted", server_info_status=status, myself_status=status))
    with pytest.raises(JiraAuthFailed) as info:
        await _source(router).validate(api.credentials("self_hosted"))
    assert info.value.error_code == "auth_failed"
    assert api.PAT not in str(info.value)


async def test_classic_cloud_token_gets_a_hint() -> None:
    router = respx.Router(assert_all_called=False)
    api.mount(router, FakeJira("cloud", myself_status=401, site_myself_status=200))
    with pytest.raises(JiraAuthFailed) as info:
        await _source(router).validate(api.credentials("cloud"))
    assert info.value.hint == "classic_token"


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_connect_error_is_unreachable(flavor: Flavor) -> None:
    router = respx.Router(assert_all_called=False)
    router.route().mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(JiraUnreachable) as info:
        await _source(router).validate(api.credentials(flavor))
    assert info.value.error_code == "unreachable"


async def test_untrusted_self_hosted_certificate_carries_the_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cert = CertInfo(
        subject="CN=jira", issuer="CN=Corp CA", not_after="2027-03-01", sha256="AB", pem="x"
    )
    fetched: list[tuple[str, int, str | None]] = []

    async def fetch(host: str, port: int, *, address: str | None = None) -> CertInfo:
        fetched.append((host, port, address))
        return cert

    async def resolve(*_: Any, **__: Any) -> list[Any]:
        return [ipaddress.ip_address("203.0.113.7")]

    def tls_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("verify failed") from ssl.SSLCertVerificationError("self signed")

    # respx drops the exception's cause, so this test uses a bare mock transport.
    source = JiraTaskSource(
        JiraOptions(),
        transport_factory=lambda _base: httpx.MockTransport(tls_failure),
        cert_fetcher=fetch,
    )
    monkeypatch.setattr(source_module, "check_address", resolve)
    with pytest.raises(JiraCertificateUntrusted) as info:
        await source.connect(api.credentials("self_hosted"))
    assert info.value.cert == cert
    assert fetched == [("jira.example.com", 443, "203.0.113.7")]


async def test_invalid_site_address_is_refused_without_a_request() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.route()
    with pytest.raises(TaskSourceUnavailable):
        await _source(router).validate({**api.credentials("cloud"), "site_url": "http://x.example"})
    assert not route.called


# --- projects and tasks ---------------------------------------------------------------------------


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_list_containers_returns_projects(flavor: Flavor) -> None:
    router = respx.Router(assert_all_called=False)
    api.mount(router, FakeJira(flavor))
    found = await _source(router).list_containers({**api.config(flavor), **api.credentials(flavor)})
    assert [(c.id, c.name) for c in found] == [("CORE", "Core (CORE)"), ("PAY", "Payments (PAY)")]


async def test_flavors_same_scenarios() -> None:
    by_flavor = {f: await _tasks(f, FakeJira(f, issues=[_refund_issue()])) for f in FLAVORS}
    cloud, dc = by_flavor["cloud"], by_flavor["self_hosted"]
    strip = {"url"}
    assert [t.model_dump(exclude=strip) for t in cloud] == [t.model_dump(exclude=strip) for t in dc]
    (task,) = cloud
    assert task.id == "PAY-142" and task.title == "Fix refund rounding"
    assert task.source == "jira" and task.status == "In Progress"
    assert task.due_is_date and task.due_on("Pacific/Kiritimati") == date(2026, 10, 25)
    assert task.assignees == [ME.name]
    assert task.due_set_at == _at(5)
    assert task.description == "Refunds round down; target: deadline 30 Oct."
    assert task.description_at == T0
    assert [c.id for c in task.comments] == ["2000", "2001", "2002", "2003", "2004"]
    assert all("internal note" not in c.text for c in task.comments)  # restricted: left out
    assert by_flavor["cloud"][0].url == f"{api.CLOUD_SITE}/browse/PAY-142"
    assert by_flavor["self_hosted"][0].url == f"{api.DC_SITE}/browse/PAY-142"


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_comments_keep_the_users_words_without_quotes(flavor: Flavor) -> None:
    issue = _refund_issue()
    issue.comments = issue.comments[:3]
    (task,) = await _tasks(flavor, FakeJira(flavor, issues=[issue]))
    texts = {c.id: c for c in task.comments}
    assert texts["10002"].text == f"@{ANNA.name} sure, deadline moved to Nov 5"
    assert texts["10002"].author == ME.name and texts["10002"].at == _at(12)
    assert "10003" not in texts


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_search_pages_and_cap(flavor: Flavor) -> None:
    issues = [Issue(f"PAY-{n}", f"Task {n}", created=T0) for n in range(1, 121)]
    jira = FakeJira(flavor, issues=issues, page_size=50)
    tasks = await _tasks(flavor, jira)
    assert len(tasks) == 100  # MAX_TASKS
    assert len(jira.searches) == 2
    params = jira.searches[0].url.params
    assert params["jql"].startswith('project in ("PAY") AND assignee = currentUser()')
    assert "comment" in params["fields"] and params["expand"] == "changelog"


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_tasks_are_cached_for_a_minute(flavor: Flavor) -> None:
    now = [0.0]
    router = respx.Router(assert_all_called=False)
    jira = FakeJira(flavor, issues=[_refund_issue()])
    api.mount(router, jira)
    source = _source(router, monotonic=lambda: now[0])
    conn = _conn(flavor)
    await source.list_open_tasks(conn)
    await source.list_open_tasks(conn)
    assert len(jira.searches) == 1
    assert source.cached_tasks(conn.user_id)
    now[0] = 61.0
    await source.list_open_tasks(conn)
    assert len(jira.searches) == 2
    source.invalidate(conn.user_id)
    assert source.cached_tasks(conn.user_id) is None


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_rate_limit_is_typed(flavor: Flavor) -> None:
    jira = FakeJira(flavor, issues=[_refund_issue()], search_status=429, retry_after="120")
    with pytest.raises(JiraRateLimited) as info:
        await _tasks(flavor, jira)
    assert info.value.retry_after_s == 120.0


async def test_no_projects_means_no_request() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.route()
    assert await _source(router).list_open_tasks(_conn("cloud", project_keys=[])) == []
    assert not route.called


async def test_classic_token_probe_only_on_atlassian_hosts() -> None:
    """A Cloud site address that is not Atlassian's never receives the token."""
    router = respx.Router(assert_all_called=False)
    other = "https://jira-proxy.example.com"
    router.get(f"{other}/_edge/tenant_info").respond(200, json={"cloudId": api.CLOUD_ID})
    router.get(f"{api.CLOUD_API}/rest/api/3/myself").respond(401, json={})
    site_route = router.get(f"{other}/rest/api/3/myself").respond(200, json={})
    with pytest.raises(JiraAuthFailed) as info:
        await _source(router).validate({**api.credentials("cloud"), "site_url": other})
    assert info.value.hint is None and not site_route.called
