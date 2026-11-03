"""QR image rendering (T007, contracts/login-client.md "QR image")."""

from __future__ import annotations

import struct

from scribe.telegram.qr_image import render_qr_png

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
URL = "tg://login?token=abc"


def _png_size(data: bytes) -> tuple[int, int]:
    # The first chunk after the signature is IHDR: length(4) type(4) width(4) height(4).
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_returns_png() -> None:
    assert render_qr_png(URL).startswith(PNG_SIGNATURE)


def test_width_is_about_512_px() -> None:
    width, height = _png_size(render_qr_png(URL))
    assert 400 <= width <= 700
    assert width == height


def test_realistic_token_length_stays_in_range() -> None:
    url = "tg://login?token=" + "A" * 43  # base64url of a 32-byte token
    width, _ = _png_size(render_qr_png(url))
    assert 400 <= width <= 700


def test_deterministic() -> None:
    assert render_qr_png(URL) == render_qr_png(URL)
    assert render_qr_png(URL) != render_qr_png("tg://login?token=xyz")
