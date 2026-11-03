"""Certificate pinning for self-hosted Jira (research J5) against a local TLS server.

The server listens on 127.0.0.1 only and uses throwaway self-signed certificates generated
here; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from scribe.context.tasks.base import JiraUnreachable
from scribe.context.tasks.jira.netguard import GuardedTransport, IPAddress, check_address
from scribe.context.tasks.jira.tls import (
    cert_info,
    fetch_leaf_certificate,
    is_cert_verify_error,
    pinned_ssl_context,
)


@dataclass(frozen=True)
class Cert:
    pem: str
    key_pem: str
    sha256: str


def make_cert(common_name: str, sans: Sequence[x509.GeneralName]) -> Cert:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Synthetic Test Co"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(datetime(2031, 6, 30, 12, tzinfo=UTC))
        .add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    fp = cert.fingerprint(hashes.SHA256())
    return Cert(
        pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        sha256=":".join(f"{b:02X}" for b in fp),
    )


LOCALHOST_SANS = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]


@pytest.fixture(scope="module")
def localhost_cert() -> Cert:
    return make_cert("localhost", LOCALHOST_SANS)


@pytest.fixture(scope="module")
def other_localhost_cert() -> Cert:
    return make_cert("localhost", LOCALHOST_SANS)


@pytest.fixture(scope="module")
def other_host_cert() -> Cert:
    return make_cert("other.example", [x509.DNSName("other.example")])


@dataclass
class TLSServer:
    tmp: Path
    port: int = 0
    received: list[bytes] = field(default_factory=list)
    done: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    _server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = b""
        try:
            async with asyncio.timeout(0.5):
                while b"\r\n\r\n" not in data:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    data += chunk
        except (TimeoutError, OSError, ssl.SSLError):
            pass
        self.received.append(data)
        try:
            if b"\r\n\r\n" in data:
                body = b"ok"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
                await writer.drain()
            writer.close()
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass
        self.done.put_nowait(data)

    async def start(self, cert: Cert) -> None:
        cert_file = self.tmp / "server.pem"
        cert_file.write_text(cert.pem + cert.key_pem)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert_file)
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.port, ssl=ctx, reuse_address=True
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[TLSServer]:
    srv = TLSServer(tmp_path)
    yield srv
    await srv.stop()


async def ipv4_only(host: str, port: int, allowlist: Sequence[str]) -> list[IPAddress]:
    """The real address check, then only the IPv4 answers (the server listens on 127.0.0.1;
    ``localhost`` may also resolve to ``::1`` first)."""
    return [ip for ip in await check_address(host, port, allowlist) if ip.version == 4]


def client(verify: ssl.SSLContext | bool = True) -> httpx.AsyncClient:
    transport = GuardedTransport(["localhost"], verify=verify, resolver=ipv4_only)
    return httpx.AsyncClient(transport=transport, timeout=2.0)


async def test_pinned_cert_accepts_same_rejects_changed(
    server: TLSServer, localhost_cert: Cert, other_localhost_cert: Cert
) -> None:
    await server.start(localhost_cert)
    url = f"https://localhost:{server.port}/rest/api/2/serverInfo"

    # 1. System CAs: the self-signed certificate is refused with a verification error.
    async with client() as http:
        with pytest.raises(httpx.ConnectError) as err:
            await http.get(url)
    assert is_cert_verify_error(err.value)

    # 2. The leaf certificate is read by a bare handshake: no application bytes are sent.
    info = await fetch_leaf_certificate("localhost", server.port)
    assert info.sha256 == localhost_cert.sha256
    assert await asyncio.wait_for(server.done.get(), 2.0) == b""
    assert server.received == [b""]

    # 3. Pinned: the same certificate is accepted (hostname still checked: localhost).
    async with client(pinned_ssl_context(info.pem)) as http:
        resp = await http.get(url)
    assert resp.status_code == 200
    assert resp.text == "ok"
    request = await asyncio.wait_for(server.done.get(), 2.0)
    assert request.startswith(b"GET /rest/api/2/serverInfo HTTP/1.1\r\n")
    assert f"host: localhost:{server.port}".encode() in request.lower()

    # 4. The server comes back with another certificate: the pinned client refuses it.
    await server.stop()
    await server.start(other_localhost_cert)
    async with client(pinned_ssl_context(info.pem)) as http:
        with pytest.raises(httpx.ConnectError) as err:
            await http.get(url)
    assert is_cert_verify_error(err.value)
    changed = await fetch_leaf_certificate("localhost", server.port, address="127.0.0.1")
    assert changed.sha256 == other_localhost_cert.sha256 != info.sha256


async def test_pinned_cert_still_checks_hostname(server: TLSServer, other_host_cert: Cert) -> None:
    await server.start(other_host_cert)
    async with client(pinned_ssl_context(other_host_cert.pem)) as http:
        with pytest.raises(httpx.ConnectError) as err:
            await http.get(f"https://localhost:{server.port}/")
    assert is_cert_verify_error(err.value)
    assert "hostname mismatch" in str(err.value).lower()


async def test_fetch_leaf_certificate_unreachable(server: TLSServer, localhost_cert: Cert) -> None:
    await server.start(localhost_cert)
    port = server.port
    await server.stop()
    with pytest.raises(JiraUnreachable) as err:
        await fetch_leaf_certificate("localhost", port, address="127.0.0.1", timeout_s=1.0)
    assert err.value.blocked is False


def test_cert_info_fields(localhost_cert: Cert) -> None:
    info = cert_info(localhost_cert.pem)
    assert info.subject == "CN=localhost,O=Synthetic Test Co"
    assert info.issuer == info.subject
    assert info.not_after == "2031-06-30"
    assert info.sha256 == localhost_cert.sha256
    assert len(info.sha256) == 32 * 3 - 1
    assert info.pem.strip() == localhost_cert.pem.strip()
    assert "BEGIN CERTIFICATE" not in repr(info)


def test_is_cert_verify_error_walks_chain() -> None:
    inner = ssl.SSLCertVerificationError("certificate verify failed")
    try:
        try:
            raise inner
        except ssl.SSLError as exc:
            raise httpx.ConnectError("wrapped") from exc
    except httpx.ConnectError as wrapped:
        assert is_cert_verify_error(wrapped)
    assert not is_cert_verify_error(httpx.ConnectError("refused"))
    assert not is_cert_verify_error(ssl.SSLError("handshake failure"))
