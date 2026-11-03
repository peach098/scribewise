"""Private-address guard for user-supplied Jira addresses (research J4, FR-J02).

The service is multi-tenant: a self-hosted Jira base URL comes from the user, so without a guard
anyone could make the server call internal addresses (Postgres, the health port, cloud
metadata at ``169.254.169.254``).

- :func:`validate_site_url`: the URL itself — ``https`` only, a host, a port from the operator's
  list (``SCRIBE_JIRA_ALLOWED_PORTS``, by default 443 and 8443: issue #61, so the connection
  cannot be used to probe arbitrary ports), no credentials, query or fragment; normalized
  (trimmed, no trailing slash).
- :func:`check_address`: resolves the host and checks **every** address
  (:func:`is_blocked_ip`): loopback, link-local, private (RFC 1918, ULA), CGNAT, multicast,
  unspecified, reserved and site-local addresses (also as IPv4-mapped IPv6) are refused unless
  the host or a CIDR containing the address is in the operator allowlist
  (``SCRIBE_JIRA_ALLOWED_PRIVATE_HOSTS``, :func:`allowed`).
- :class:`GuardedTransport`: an httpx transport that runs the check for every request and then
  connects to the *checked* IP, keeping the original hostname for TLS SNI, certificate
  hostname verification and the ``Host`` header, so DNS rebinding between check and connect
  is not possible. Transport errors propagate unchanged (the client maps them).

Error messages never contain the URL (it may carry credentials the user pasted by mistake).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from scribe.context.tasks.base import JiraAddressInvalid, JiraPortNotAllowed, JiraUnreachable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int, Sequence[str]], Awaitable[list[IPAddress]]]

# Ports a new Jira address may use unless the operator sets ``SCRIBE_JIRA_ALLOWED_PORTS``.
DEFAULT_ALLOWED_PORTS: tuple[int, ...] = (443, 8443)

UNREACHABLE_MESSAGE = (
    "Scribe could not reach this server; it must be reachable from the internet or allowed by "
    "the operator"
)

# ``is_private`` (Python 3.12) does not cover the shared address space of carrier-grade NAT.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class SiteUrl:
    """A validated Jira site address: ``url`` is normalized (no trailing slash), ``path`` is the
    context path (``""`` or ``/jira``), ``port`` defaults to 443."""

    url: str
    host: str
    port: int
    path: str


def validate_site_url(
    url: str, *, allowed_ports: Collection[int] | None = DEFAULT_ALLOWED_PORTS
) -> SiteUrl:
    """Check and normalize a user-supplied Jira address; :class:`JiraAddressInvalid` otherwise.

    A port outside ``allowed_ports`` raises :class:`JiraPortNotAllowed`. ``None`` accepts any
    port: only for a connection already stored (saved before the port rule, issue #61)."""
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError for a non-numeric / out-of-range port
        host = parts.hostname
    except ValueError:
        raise JiraAddressInvalid("the jira address is not a valid URL") from None
    if parts.scheme.lower() != "https":
        raise JiraAddressInvalid("the jira address must start with https://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise JiraAddressInvalid("the jira address must not contain a user name or password")
    if parts.query or raw.rstrip("/").endswith("?"):
        raise JiraAddressInvalid("the jira address must not contain a query (?...)")
    if parts.fragment or "#" in raw:
        raise JiraAddressInvalid("the jira address must not contain a fragment (#...)")
    if not host or any(ch.isspace() for ch in raw):
        raise JiraAddressInvalid("the jira address has no valid host")
    host = host.lower().rstrip(".")
    if not host:
        raise JiraAddressInvalid("the jira address has no valid host")
    path = parts.path.rstrip("/")
    port = port if port is not None else 443
    if port == 0:
        raise JiraAddressInvalid("the jira address has an invalid port")
    if allowed_ports is not None and port not in allowed_ports:
        raise JiraPortNotAllowed(sorted(set(allowed_ports)))
    netloc = f"[{host}]" if ":" in host else host
    if port != 443:
        netloc = f"{netloc}:{port}"
    return SiteUrl(url=f"https://{netloc}{path}", host=host, port=port, path=path)


def is_blocked_ip(ip: IPAddress) -> bool:
    """True for an address a user-supplied host must not reach (see the module docstring)."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return is_blocked_ip(mapped)
        if ip.is_site_local:
            return True
    elif ip in _CGNAT:
        return True
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def allowed(host: str, ip: IPAddress, allowlist: Sequence[str]) -> bool:
    """Whether the operator allowlist admits ``host`` / ``ip``: an entry is a hostname
    (case-insensitive exact match) or a CIDR / single address (``ip`` inside it)."""
    name = host.lower().rstrip(".")
    for entry in allowlist:
        item = entry.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            if item.lower().rstrip(".") == name:
                return True
            continue
        if ip.version == network.version and ip in network:
            return True
    return False


async def check_address(host: str, port: int, allowlist: Sequence[str] = ()) -> list[IPAddress]:
    """Resolve ``host`` and check every address; returns them deduplicated, in resolver order.

    DNS failure -> :class:`JiraUnreachable`; any blocked address not admitted by the allowlist
    -> :class:`JiraUnreachable` with ``blocked=True`` (one bad answer refuses the host: a
    rebinding resolver could otherwise hand out the private one at connect time).
    """
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(bare, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise JiraUnreachable("could not resolve the jira host") from None
    addresses: list[IPAddress] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        try:
            # Drop an IPv6 scope id (``fe80::1%eth0``); the address itself is what is checked.
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            continue
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise JiraUnreachable("could not resolve the jira host")
    for ip in addresses:
        if is_blocked_ip(ip) and not allowed(bare, ip, allowlist):
            raise JiraUnreachable(UNREACHABLE_MESSAGE, blocked=True)
    return addresses


class GuardedTransport(httpx.AsyncBaseTransport):
    """Checks the request's host before every request and connects to the checked IP.

    The request URL's host is rewritten to the IP literal; ``sni_hostname`` (an httpcore request
    extension) carries the original hostname, which httpcore passes as ``server_hostname`` to
    the TLS handshake, so SNI and certificate hostname verification use the original name. The
    ``Host`` header keeps the original host. One inner transport (connection pool) is kept per
    original hostname: httpcore pools by origin, which is now the IP, so sharing one pool
    between two names on the same IP would reuse a TLS session made for the other name.
    """

    def __init__(
        self,
        allowlist: Sequence[str] = (),
        *,
        verify: ssl.SSLContext | bool = True,
        resolver: Resolver = check_address,
    ) -> None:
        self._allowlist = tuple(allowlist)
        self._verify = verify
        self._resolver = resolver
        self._inner: dict[str, httpx.AsyncHTTPTransport] = {}

    def _transport_for(self, host: str) -> httpx.AsyncHTTPTransport:
        transport = self._inner.get(host)
        if transport is None:
            transport = httpx.AsyncHTTPTransport(verify=self._verify)
            self._inner[host] = transport
        return transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        addresses = await self._resolver(host, port, self._allowlist)
        if not addresses:
            raise JiraUnreachable("could not resolve the jira host")
        if "host" not in request.headers:
            request.headers["Host"] = request.url.netloc.decode("ascii")
        request.extensions = {**request.extensions, "sni_hostname": host}
        request.url = request.url.copy_with(host=str(addresses[0]))
        return await self._transport_for(host).handle_async_request(request)

    async def aclose(self) -> None:
        inner, self._inner = list(self._inner.values()), {}
        for transport in inner:
            await transport.aclose()


__all__ = [
    "DEFAULT_ALLOWED_PORTS",
    "UNREACHABLE_MESSAGE",
    "GuardedTransport",
    "IPAddress",
    "Resolver",
    "SiteUrl",
    "allowed",
    "check_address",
    "is_blocked_ip",
    "validate_site_url",
]
