"""`python -m scribe` wiring (T023): the service boots against Postgres with an offline Bot API
session, answers /healthz, ticks the scheduler, registers the purge job and shuts down."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import aiohttp
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.__main__ import ConfigError, Service, build_llm_router
from scribe.config import Settings
from scribe.db.models import ScheduledJob
from tests.conftest import LogCapture
from tests.integration.bot_support import offline_bot

ROOT = Path(__file__).resolve().parents[2]
ENVIRON = {"ANTHROPIC_API_KEY": "test-key-not-real"}


def _settings(settings: Settings, database_url: str) -> Settings:
    return settings.model_copy(
        update={
            "database_url": SecretStr(database_url),
            "health_host": "127.0.0.1",
            "health_port": 0,
            "scheduler_tick_s": 0.05,
            "llm_routes_path": ROOT / "config/llm_routes.yml",
            "llm_prices_path": ROOT / "config/llm_prices.yml",
            "prompts_dir": ROOT / "prompts",
        }
    )


async def test_service_boots_reports_health_and_stops(
    settings: Settings,
    migrated_database_url: str,
    committed_session_factory: async_sessionmaker[AsyncSession],  # cleans up committed rows
) -> None:
    bot, api = offline_bot()
    service = Service(
        _settings(settings, migrated_database_url), environ=ENVIRON, bot_factory=lambda s: bot
    )
    await service.start()
    try:
        for _ in range(100):  # wait for the first tick and the bot to start polling
            if service.bot_polling and service.scheduler.last_tick_at is not None:
                break
            await asyncio.sleep(0.05)
        assert service.health is not None and service.health.port
        async with (
            aiohttp.ClientSession() as http,
            http.get(f"http://127.0.0.1:{service.health.port}/healthz") as response,
        ):
            assert response.status == 200
            assert await response.json() == {
                "status": "ok",
                "users_active": 0,
                "access_gate": False,
            }
    finally:
        await service.stop()

    async with committed_session_factory() as session:
        jobs = (await session.execute(select(ScheduledJob.name))).scalars().all()
    assert sorted(jobs) == [
        "access_reconcile",
        "grammatical_gender_prompt",  # issue #46
        "jira_comments",
        "jira_verify",
        "llm_spend_alert",  # issue #74
        "purge_messages",
        "purge_pending_credentials",  # issue #56
        "reencrypt_all",  # issues #58, #59
        "reencrypt_profiles",
        "refresh_profiles",
        "release_notes",
    ]
    assert any(type(r).__name__ == "SetMyCommands" for r in api.requests)


def test_invalid_llm_config_fails_fast(settings: Settings) -> None:
    configured = _settings(settings, "postgresql+asyncpg://unused/db")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        build_llm_router(configured, environ={})
    with pytest.raises(ConfigError, match="unknown provider"):
        build_llm_router(
            configured,
            environ={**ENVIRON, "SCRIBE_LLM__COMPOSE_DRAFT__QUALITY__PROVIDER": "mystery"},
        )


async def test_stop_drains_background_commitment_extractions(
    settings: Settings,
    migrated_database_url: str,
    committed_session_factory: async_sessionmaker[AsyncSession],  # cleans up committed rows
    log_capture: LogCapture,
) -> None:
    """Phase 5 review C/M: one process-wide extractor (the composer's, reused by every later
    Composer) and stop() waits for its extractions before the engine is disposed."""
    from scribe.context import commitments as commitments_ctx

    bot, _ = offline_bot()
    service = Service(
        _settings(settings, migrated_database_url), environ=ENVIRON, bot_factory=lambda s: bot
    )
    await service.start()
    extractor = commitments_ctx.default_extractor()
    assert extractor is not None and extractor.session_factory is service.session_factory
    finished: list[bool] = []

    async def slow_extraction() -> None:  # stands in for an LLM call still running
        await asyncio.sleep(0.3)
        async with service.session_factory() as session:  # the engine is still there
            await session.execute(select(ScheduledJob.name))
        finished.append(True)

    task = asyncio.create_task(slow_extraction())
    extractor._tasks.add(task)
    task.add_done_callback(extractor._tasks.discard)
    await service.stop()
    assert finished == [True]
    (drained,) = log_capture.events("commitment_extractions_drained")
    assert drained["pending"] == 1 and drained["cancelled"] == 0


async def test_gate_without_operator_logs_a_warning(
    settings: Settings,
    migrated_database_url: str,
    committed_session_factory: async_sessionmaker[AsyncSession],  # cleans up committed rows
    log_capture: LogCapture,
) -> None:
    """specs/005 research Q8: the service still starts; the operator can add themselves."""
    bot, _ = offline_bot()
    configured = _settings(settings, migrated_database_url).model_copy(
        update={"access_gate": True, "operator_telegram_ids": []}
    )
    service = Service(configured, environ=ENVIRON, bot_factory=lambda s: bot)
    await service.start()
    await service.stop()
    assert len(log_capture.events("access_gate_no_operator")) == 1

    service = Service(
        configured.model_copy(update={"operator_telegram_ids": [900000001]}),
        environ=ENVIRON,
        bot_factory=lambda s: offline_bot()[0],
    )
    await service.start()
    await service.stop()
    assert len(log_capture.events("access_gate_no_operator")) == 1  # not logged again


# --- plugins (docs/plugins.md) ----------------------------------------------------------------


async def test_a_failing_plugin_stops_startup(
    settings: Settings,
    migrated_database_url: str,
    committed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def setup(host: object) -> None:
        raise RuntimeError("synthetic plugin failure")

    module = types.ModuleType("scribe_test_failing_plugin")
    module.setup = setup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    configured = _settings(settings, migrated_database_url).model_copy(
        update={"plugins": [module.__name__]}
    )
    bot, _ = offline_bot()
    service = Service(configured, environ=ENVIRON, bot_factory=lambda s: bot)
    try:
        with pytest.raises(ConfigError, match="setup failed: RuntimeError"):
            await service.start()
    finally:
        await service.stop()
    assert not service.bot_polling


async def test_a_plugin_is_set_up_once_before_the_dispatcher(
    settings: Settings,
    migrated_database_url: str,
    committed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram import Router

    from scribe.plugins import PluginHost

    hosts: list[PluginHost] = []
    router = Router(name="plugin-service-test")

    def setup(host: PluginHost) -> None:
        assert host.bot is not None and host.scheduler is not None and host.pool is not None
        assert host.session_factory is not None
        assert not hasattr(service, "dispatcher")  # the dispatcher is built afterwards
        host.add_router(router)
        hosts.append(host)

    module = types.ModuleType("scribe_test_plugin")
    module.setup = setup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    configured = _settings(settings, migrated_database_url).model_copy(
        update={"plugins": [module.__name__]}
    )
    bot, _ = offline_bot()
    service = Service(configured, environ=ENVIRON, bot_factory=lambda s: bot)
    await service.start()
    try:
        assert len(hosts) == 1
        assert router in service.dispatcher.sub_routers
    finally:
        await service.stop()
