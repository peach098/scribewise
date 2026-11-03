"""Private-address guard and site URL validation (research J4). No real DNS or network."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from scribe.context.tasks.base import JiraAddressInvalid, JiraPortNotAllowed, JiraUnreachable
from scribe.context.tasks.jira import netguard
from scribe.context.tasks.jira.netguard import (
    GuardedTransport,
    IPAddress,
    allowed,
    check_address,
    is_blocked_ip,
    validate_site_url,
)

BLOCKED = [
    "127.0.0.1",
    "::1",
    "10.0.0.5",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "100.64.0.1",
    "fc00::1",
    "fe80::1",
    "0.0.0.0",
    "224.0.0.1",
    "::ffff:127.0.0.1",
]
PUBLIC = ["8.8.8.8", "2606:4700::1111"]


def _fake_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[str]] | None
) -> list[tuple[str, int]]:
    """Make the running loop's getaddrinfo answer from ``answers`` (None: DNS failure)."""
    calls: list[tuple[str, int]] = []
    loop = asyncio.get_running_loop()

    async def fake(host: str, port: int, **kwargs: Any) -> list[Any]:
        calls.append((host, port))
        assert kwargs.get("type") == socket.SOCK_STREAM
        if answers is None or host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        out: list[Any] = []
        for addr in answers[host]:
            if ":" in addr:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, port, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)))
        return out

    monkeypatch.setattr(loop, "getaddrinfo", fake)
    return calls


async def test_address_guard_blocks_private_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    for addr in BLOCKED:
        assert is_blocked_ip(ipaddress.ip_address(addr)), addr
    for addr in PUBLIC:
        assert not is_blocked_ip(ipaddress.ip_address(addr)), addr

    _fake_getaddrinfo(
        monkeypatch,
        {
            "jira.example.com": ["8.8.8.8", "2606:4700::1111", "8.8.8.8"],
            "rebind.example.com": ["8.8.8.8", "10.0.0.5"],
            "metadata.example.com": ["169.254.169.254"],
            "jira.corp.internal": ["10.1.2.3"],
            "127.0.0.1": ["127.0.0.1"],
        },
    )

    # Public: every address returned once, in resolver order.
    assert await check_address("jira.example.com", 443) == [
        ipaddress.ip_address("8.8.8.8"),
        ipaddress.ip_address("2606:4700::1111"),
    ]
    # One private answer among public ones refuses the host.
    for host in ("rebind.example.com", "metadata.example.com", "jira.corp.internal", "127.0.0.1"):
        with pytest.raises(JiraUnreachable) as err:
            await check_address(host, 443)
        assert err.value.blocked is True
        assert err.value.error_code == "blocked_address"
        assert host not in str(err.value)

    # Allowlist by hostname (case-insensitive) and by CIDR.
    assert await check_address("jira.corp.internal", 443, ["JIRA.corp.internal"]) == [
        ipaddress.ip_address("10.1.2.3")
    ]
    assert await check_address("jira.corp.internal", 443, ["10.0.0.0/8"]) == [
        ipaddress.ip_address("10.1.2.3")
    ]
    with pytest.raises(JiraUnreachable):
        await check_address("jira.corp.internal", 443, ["other.internal", "192.168.0.0/16"])
    # The allowlist admits the named host only, not the other private answers.
    with pytest.raises(JiraUnreachable):
        await check_address("rebind.example.com", 443, ["jira.corp.internal"])

    # DNS failure is "unreachable", not "blocked".
    with pytest.raises(JiraUnreachable) as err:
        await check_address("nxdomain.example.com", 443)
    assert err.value.blocked is False
    assert err.value.error_code == "unreachable"


def test_allowed_entries() -> None:
    ip = ipaddress.ip_address("10.1.2.3")
    assert allowed("jira.corp", ip, ["jira.corp"])
    assert allowed("Jira.Corp.", ip, [" jira.corp "])
    assert allowed("x", ip, ["10.1.2.3"])
    assert allowed("x", ip, ["10.1.0.0/16"])
    assert not allowed("x", ip, ["10.2.0.0/16", "jira.corp", "fc00::/7", ""])
    assert allowed("x", ipaddress.ip_address("fd00::5"), ["fc00::/7"])


async def test_ip_literals_resolve_without_dns() -> None:
    # The real loop resolves a literal without DNS.
    assert await check_address("8.8.8.8", 443) == [ipaddress.ip_address("8.8.8.8")]
    with pytest.raises(JiraUnreachable) as err:
        await check_address("[::1]", 443)
    assert err.value.blocked
    assert await check_address("127.0.0.1", 443, ["127.0.0.0/8"]) == [
        ipaddress.ip_address("127.0.0.1")
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://jira.example.com",
        "ftp://jira.example.com",
        "https://user:s3cr3t-synthetic@jira.example.com",
        "https://user@jira.example.com",
        "https://jira.example.com/?a=1",
        "https://jira.example.com/#frag",
        "https://",
        "https:///path",
        "https://:443/",
        "jira.example.com",
        "not a url",
        "",
        "https://jira.example.com:99999/",
        "https://jira.example.com:abc/",
        "https://jira example.com/",
    ],
)
def test_validate_site_url_rejects(url: str) -> None:
    with pytest.raises(JiraAddressInvalid) as err:
        validate_site_url(url)
    assert err.value.error_code == "address_invalid"
    if "s3cr3t" in url:
        assert "s3cr3t" not in str(err.value)


def test_validate_site_url_accepts() -> None:
    site = validate_site_url("  https://jira.example.com:8443/jira/ ")
    assert site == netguard.SiteUrl(
        url="https://jira.example.com:8443/jira", host="jira.example.com", port=8443, path="/jira"
    )
    cloud = validate_site_url("https://acme.atlassian.net/")
    assert (cloud.url, cloud.host, cloud.port, cloud.path) == (
        "https://acme.atlassian.net",
        "acme.atlassian.net",
        443,
        "",
    )
    assert validate_site_url("HTTPS://Jira.Example.COM:443").url == "https://jira.example.com"
    assert validate_site_url("https://[2606:4700::1111]:8443").url == (
        "https://[2606:4700::1111]:8443"
    )


async def test_guarded_transport_pins_checked_ip() -> None:
    seen: list[httpx.Request] = []
    checked: list[tuple[str, int, tuple[str, ...]]] = []

    async def resolver(host: str, port: int, allowlist: Sequence[str]) -> list[IPAddress]:
        checked.append((host, port, tuple(allowlist)))
        return [ipaddress.ip_address("203.0.113.7"), ipaddress.ip_address("203.0.113.8")]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    transport = GuardedTransport(["jira.corp"], resolver=resolver)
    mock = httpx.MockTransport(handler)
    transport._transport_for = lambda host: mock  # type: ignore[method-assign,assignment,return-value]
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://jira.example.com:8443/rest/api/2/myself?x=1")
    assert resp.status_code == 200
    assert checked == [("jira.example.com", 8443, ("jira.corp",))]
    (req,) = seen
    assert req.url.host == "203.0.113.7"
    assert req.url.port == 8443
    assert req.url.raw_path == b"/rest/api/2/myself?x=1"
    assert req.headers["host"] == "jira.example.com:8443"
    assert req.extensions["sni_hostname"] == "jira.example.com"


async def test_guarded_transport_refuses_before_connecting() -> None:
    async def resolver(host: str, port: int, allowlist: Sequence[str]) -> list[IPAddress]:
        raise JiraUnreachable("blocked", blocked=True)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("request must not be sent")

    transport = GuardedTransport(resolver=resolver)
    transport._transport_for = lambda host: httpx.MockTransport(handler)  # type: ignore[method-assign,assignment,return-value]
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(JiraUnreachable) as err:
            await client.get("https://internal.example.com/")
    assert err.value.blocked


async def test_guarded_transport_closes_inner_transports() -> None:
    transport = GuardedTransport()
    inner = transport._transport_for("a.example.com")
    assert transport._transport_for("a.example.com") is inner
    assert transport._transport_for("b.example.com") is not inner
    await transport.aclose()
    assert transport._inner == {}


# --- ports (issue #61) ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jira.example.com:22",
        "https://jira.example.com:8080/jira",
        "https://[2606:4700::1]:25",
    ],
)
def test_validate_site_url_refuses_other_ports(url: str) -> None:
    with pytest.raises(JiraPortNotAllowed) as err:
        validate_site_url(url)
    assert isinstance(err.value, JiraAddressInvalid)
    assert err.value.error_code == "port_not_allowed"
    assert err.value.allowed_ports == (443, 8443)


def test_validate_site_url_default_ports() -> None:
    assert netguard.DEFAULT_ALLOWED_PORTS == (443, 8443)
    assert validate_site_url("https://jira.example.com").port == 443
    assert validate_site_url("https://jira.example.com:8443").port == 8443


def test_validate_site_url_operator_ports() -> None:
    assert validate_site_url("https://jira.example.com:9443", allowed_ports=[9443]).port == 9443
    with pytest.raises(JiraPortNotAllowed) as err:
        validate_site_url("https://jira.example.com", allowed_ports=[9443])
    assert err.value.allowed_ports == (9443,)


def test_validate_site_url_any_port_for_stored_connections() -> None:
    """A connection saved before the port rule keeps working (``allowed_ports=None``)."""
    assert validate_site_url("https://jira.example.com:8080", allowed_ports=None).port == 8080
