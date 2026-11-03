"""/healthz (T023; ``access_gate``: specs/005 T033)."""

from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from scribe.observability.health import HealthStatus, create_app


async def _get(
    status: HealthStatus | Exception, *, access_gate: bool = False
) -> tuple[int, dict[str, object]]:
    async def provider() -> HealthStatus:
        if isinstance(status, Exception):
            raise status
        return status

    async with TestClient(TestServer(create_app(provider, access_gate=access_gate))) as client:
        response = await client.get("/healthz")
        return response.status, await response.json()


async def test_ok() -> None:
    assert await _get(HealthStatus(ok=True, users_active=3)) == (
        200,
        {"status": "ok", "users_active": 3, "access_gate": False},
    )


async def test_degraded() -> None:
    assert await _get(HealthStatus(ok=False, users_active=1)) == (
        503,
        {"status": "degraded", "users_active": 1, "access_gate": False},
    )


async def test_provider_error_is_degraded() -> None:
    status, body = await _get(RuntimeError("database down"), access_gate=True)
    assert status == 503 and body["status"] == "degraded"
    assert body["access_gate"] is True  # the mode is known even when the check fails


async def test_access_gate_mode_is_reported() -> None:
    assert await _get(HealthStatus(ok=True, users_active=2), access_gate=True) == (
        200,
        {"status": "ok", "users_active": 2, "access_gate": True},
    )
