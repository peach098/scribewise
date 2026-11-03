"""Only one instance per database (#60): the advisory lock taken by ``python -m scribe``."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from scribe import __main__ as main_module
from scribe.config import Settings
from scribe.instance_lock import InstanceLock


async def test_second_lock_is_refused_until_the_first_is_released(
    migrated_database_url: str,
) -> None:
    first = await InstanceLock.try_acquire(migrated_database_url)
    assert first is not None
    try:
        assert await InstanceLock.try_acquire(migrated_database_url) is None
    finally:
        await first.release()
    again = await InstanceLock.try_acquire(migrated_database_url)
    assert again is not None
    await again.release()


async def test_run_refuses_to_start_while_another_instance_runs(
    migrated_database_url: str,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configured = settings.model_copy(update={"database_url": SecretStr(migrated_database_url)})

    class MustNotStart:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("the service started while another instance runs")

    monkeypatch.setattr(main_module, "Service", MustNotStart)
    held = await InstanceLock.try_acquire(migrated_database_url)
    assert held is not None
    try:
        assert await main_module.run(configured) == main_module.EXIT_RUNTIME_ERROR
    finally:
        await held.release()
    assert "another instance is already running" in capsys.readouterr().err
