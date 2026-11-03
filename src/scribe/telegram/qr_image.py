"""QR image for Telegram login (research Q2, contracts/login-client.md "QR image").

The URL carries a live login token: it is only rendered here and never logged.
"""

from __future__ import annotations

import io

import segno

TARGET_WIDTH_PX = 512
_BORDER = 4  # quiet zone in modules, as the QR standard requires


def render_qr_png(url: str) -> bytes:
    """PNG of ``url`` as a QR code (error correction M), about 512 px wide. Deterministic."""
    qr = segno.make_qr(url, error="m", boost_error=False)
    width, _ = qr.symbol_size(scale=1, border=_BORDER)
    scale = max(1, round(TARGET_WIDTH_PX / width))
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=scale, border=_BORDER)
    return buffer.getvalue()
