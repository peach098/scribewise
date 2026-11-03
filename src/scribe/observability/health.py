"""``GET /healthz`` for container health checks (no public API exists).

Response: ``{"status": "ok" | "degraded", "users_active": <int>, "access_gate": <bool>}``;
HTTP 200 when ok, 503 when degraded (database unreachable, scheduler stalled or control bot not
polling). ``access_gate`` is ``Settings.access_gate`` (specs/005 research Q9), reported in both
cases so the operator sees the mode at a glance.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from scribe.logging import get_logger

log = get_logger("scribe.health")


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    users_active: int


StatusProvider = Callable[[], Awaitable[HealthStatus]]


def create_app(status: StatusProvider, *, access_gate: bool = False) -> web.Application:
    async def healthz(request: web.Request) -> web.Response:
        try:
            current = await status()
        except Exception:
            log.exception("health_check_failed")
            current = HealthStatus(ok=False, users_active=0)
        body = {
            "status": "ok" if current.ok else "degraded",
            "users_active": current.users_active,
            "access_gate": access_gate,
        }
        return web.json_response(body, status=200 if current.ok else 503)

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    return app


class HealthServer:
    def __init__(
        self, status: StatusProvider, *, host: str, port: int, access_gate: bool = False
    ) -> None:
        self._runner = web.AppRunner(create_app(status, access_gate=access_gate), access_log=None)
        self._host = host
        self._port = port
        self.port: int | None = None

    async def start(self) -> None:
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        address = self._runner.addresses[0]
        self.port = int(address[1])
        log.info("health_server_started", host=self._host, port=self.port)

    async def stop(self) -> None:
        await self._runner.cleanup()
