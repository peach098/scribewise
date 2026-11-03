"""Company-issued certificates: show, then pin exactly one leaf certificate (research J5).

Normal system-CA verification is tried first. When it fails with a certificate verification
error (:func:`is_cert_verify_error`; httpx wraps ``ssl.SSLCertVerificationError`` in
``ConnectError``), :func:`fetch_leaf_certificate` reads the server's leaf certificate with a bare
TLS handshake — verification off, **no application bytes are written**, so no request and no
credentials ever reach an unverified server — and :func:`cert_info` describes it for the user
(subject, issuer, expiry, SHA-256 fingerprint). Once the user trusts it,
:func:`pinned_ssl_context` builds a context that trusts only that certificate (no system CAs;
``VERIFY_X509_PARTIAL_CHAIN`` lets a non-root leaf act as the trust anchor) with hostname
checking still on. A server that later presents any other certificate fails verification.
"""

from __future__ import annotations

import asyncio
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from scribe.context.tasks.base import CertInfo, JiraUnreachable


def _fingerprint(der: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    return ":".join(f"{b:02X}" for b in digest.finalize())


def _info(cert: x509.Certificate) -> CertInfo:
    der = cert.public_bytes(serialization.Encoding.DER)
    return CertInfo(
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        not_after=cert.not_valid_after_utc.date().isoformat(),
        sha256=_fingerprint(der),
        pem=cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    )


def cert_info(pem: str) -> CertInfo:
    """Describe a PEM certificate (the first one when several are given)."""
    return _info(x509.load_pem_x509_certificate(pem.encode("ascii")))


async def fetch_leaf_certificate(
    host: str, port: int, *, address: str | None = None, timeout_s: float = 5.0
) -> CertInfo:
    """The leaf certificate ``host`` presents (SNI = ``host``), read without verifying it.

    Connects to ``address`` (an already checked IP, see ``netguard.check_address``) or to
    ``host``; only the TLS handshake runs, then the connection is closed. Any failure ->
    :class:`JiraUnreachable`.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    target = address or host
    try:
        async with asyncio.timeout(timeout_s):
            _reader, writer = await asyncio.open_connection(
                target, port, ssl=ctx, server_hostname=host
            )
    except (OSError, TimeoutError, ssl.SSLError):
        raise JiraUnreachable("could not read the jira server certificate") from None
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    finally:
        writer.close()
        try:
            async with asyncio.timeout(timeout_s):
                await writer.wait_closed()
        except (OSError, TimeoutError, ssl.SSLError):
            pass
    if not der:
        raise JiraUnreachable("the jira server presented no certificate")
    try:
        return _info(x509.load_der_x509_certificate(der))
    except ValueError:
        raise JiraUnreachable("the jira server certificate could not be read") from None


def pinned_ssl_context(pem: str) -> ssl.SSLContext:
    """A client context that trusts only ``pem`` (not the system CAs), hostname check on."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=pem)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def is_cert_verify_error(exc: BaseException) -> bool:
    """Whether ``exc`` is, or was caused by, a certificate verification failure."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


__all__ = ["cert_info", "fetch_leaf_certificate", "is_cert_verify_error", "pinned_ssl_context"]
