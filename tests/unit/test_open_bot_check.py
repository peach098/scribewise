"""Issue #53: ``python -m scribe`` refuses to start an open bot unless the operator says so.

The check runs in ``__main__.main`` (service startup), not in ``Settings`` validation, so
``python -m scribe.admin`` and Alembic keep working with the gate off. Synthetic values only."""

from __future__ import annotations

from typing import Any

import pytest

import scribe.__main__ as main_module
from scribe.__main__ import EXIT_CONFIG_ERROR, EXIT_OK, open_bot_refusal
from scribe.config import Settings
from tests.conftest import TEST_MASTER_KEY_B64


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "master_key": TEST_MASTER_KEY_B64,
        "bot_token": "placeholder",
        "tg_api_id": 1,
        "tg_api_hash": "placeholder",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("gate", "open_bot", "refused"),
    [(False, False, True), (False, True, False), (True, False, False), (True, True, False)],
)
def test_open_bot_refusal(gate: bool, open_bot: bool, refused: bool) -> None:
    message = open_bot_refusal(_settings(access_gate=gate, open_bot=open_bot))
    assert (message is not None) is refused
    if message is not None:
        assert "SCRIBE_ACCESS_GATE=true" in message
        assert "SCRIBE_OPEN_BOT=true" in message


def _run_main(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> tuple[int, list[Settings]]:
    started: list[Settings] = []

    async def fake_run(s: Settings) -> int:
        started.append(s)
        return EXIT_OK

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "run", fake_run)
    return main_module.main(), started


def test_main_refuses_an_open_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, started = _run_main(monkeypatch, _settings(access_gate=False))
    assert code == EXIT_CONFIG_ERROR
    assert started == []
    err = capsys.readouterr().err
    assert "scribe: invalid configuration" in err
    assert "SCRIBE_OPEN_BOT=true" in err


@pytest.mark.parametrize("overrides", [{"access_gate": True}, {"open_bot": True}])
def test_main_starts_with_the_gate_or_an_explicit_open_bot(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    code, started = _run_main(monkeypatch, _settings(**overrides))
    assert code == EXIT_OK
    assert len(started) == 1
