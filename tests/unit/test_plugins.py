"""Plugin loading and the plugin host (docs/plugins.md): ``SCRIBE_PLUGINS``, ``setup(host)``,
plugin texts, the registries the core reads, and plugin operator commands. Fake plugin modules
are put in ``sys.modules``; no database."""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from aiogram import Router
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from scribe import admin
from scribe.access.gate import OpenPaths
from scribe.config import Settings
from scribe.db.models import User
from scribe.drafting.budget import (
    BudgetCaps,
    ExcludingRoutes,
    SettingsLimitsResolver,
    UsageLimits,
)
from scribe.i18n import has_text, t
from scribe.plugins import (
    PluginError,
    PluginHost,
    admin_commands,
    import_plugin_models,
    load_plugins,
    plugin_modules_from_env,
)
from tests.conftest import FROZEN_NOW

PLUGIN = "scribe_test_fake_plugin"


def _module(monkeypatch: pytest.MonkeyPatch, name: str = PLUGIN, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _with_plugins(settings: Settings, *names: str) -> Settings:
    return settings.model_copy(update={"plugins": list(names)})


# --- loading --------------------------------------------------------------------------------------


def test_plugins_setting_is_a_comma_separated_list(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings.plugins == []
    monkeypatch.setenv("SCRIBE_PLUGINS", " first.plugin, second ,,first.plugin")
    parsed = Settings(**{k: v for k, v in settings.model_dump().items() if k != "plugins"})
    assert parsed.plugins == ["first.plugin", "second"]


def test_empty_plugins_load_nothing_and_change_nothing(settings: Settings) -> None:
    host = PluginHost(settings)
    assert load_plugins(settings, host) == []
    options = host.dispatcher_options()
    assert options["routers_first"] == [] and options["routers"] == []
    assert options["offer_provider"] is None
    assert options["open_paths"] == OpenPaths()
    assert isinstance(options["limits"], SettingsLimitsResolver)
    assert options["limits"].limits == UsageLimits(caps=BudgetCaps.from_settings(settings))
    assert host.hooks.notice_decorator() is None


def test_setup_is_called_once_with_the_host(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[PluginHost] = []
    _module(monkeypatch, setup=calls.append)
    configured = _with_plugins(settings, PLUGIN)
    host = PluginHost(configured)

    assert load_plugins(configured, host) == [PLUGIN]
    assert calls == [host]
    assert host.settings is configured


def test_an_unknown_module_fails_startup(settings: Settings) -> None:
    configured = _with_plugins(settings, "scribe_test_no_such_plugin")
    with pytest.raises(PluginError, match="cannot be imported"):
        load_plugins(configured, PluginHost(configured))


def test_a_module_without_setup_fails_startup(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _module(monkeypatch)
    configured = _with_plugins(settings, PLUGIN)
    with pytest.raises(PluginError, match="no setup"):
        load_plugins(configured, PluginHost(configured))


def test_a_raising_setup_fails_startup(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def setup(host: PluginHost) -> None:
        raise RuntimeError("synthetic failure")

    _module(monkeypatch, setup=setup)
    configured = _with_plugins(settings, PLUGIN)
    with pytest.raises(PluginError, match="setup failed: RuntimeError"):
        load_plugins(configured, PluginHost(configured))


# --- texts ----------------------------------------------------------------------------------------


def test_add_messages_adds_keys_in_both_languages(settings: Settings) -> None:
    host = PluginHost(settings)
    host.add_messages("en", {"plugin.hello": "Hello, {name}"})
    host.add_messages("ru", {"plugin.hello": "Привет, {name}"})

    assert has_text("plugin.hello")
    assert t("plugin.hello", "en", name="A") == "Hello, A"
    assert t("plugin.hello", "ru", name="A") == "Привет, A"


def test_add_messages_refuses_a_key_that_exists(settings: Settings) -> None:
    host = PluginHost(settings)
    with pytest.raises(PluginError, match="already exist"):
        host.add_messages("en", {"budget.paused_notice": "Overridden"})  # a core key
    host.add_messages("en", {"plugin.once": "Once"})
    with pytest.raises(PluginError, match="already exist"):
        host.add_messages("en", {"plugin.once": "Twice"})  # another plugin's key
    with pytest.raises(PluginError, match="unknown language"):
        host.add_messages("de", {"plugin.other": "Andere"})
    assert t("budget.paused_notice", "en", time="00:00").startswith("⏸")


# --- registries -----------------------------------------------------------------------------------


def test_routers_go_first_or_last(settings: Settings) -> None:
    host = PluginHost(settings)
    first, last, checkout = Router(name="first"), Router(name="last"), Router(name="checkout")
    host.add_router(first, first=True)
    host.add_router(last)
    host.handle_pre_checkout(checkout)
    options = host.dispatcher_options()
    assert options["routers_first"] == [first]
    assert options["routers"] == [last, checkout]


def _message(text: str | None = None, **values: Any) -> Message:
    return Message(
        message_id=1,
        date=FROZEN_NOW,
        chat=Chat(id=1, type="private"),
        from_user=TgUser(id=1, is_bot=False, first_name="Synthetic"),
        text=text,
        **values,
    )


def test_gate_allow_builds_open_paths(settings: Settings) -> None:
    host = PluginHost(settings)
    host.gate_allow(commands={"/Example"}, callback_prefixes=("ex:",))
    host.gate_allow(message_filter=lambda message: message.contact is not None)
    paths = host.hooks.open_paths()

    assert paths.commands == frozenset({"example"})
    assert paths.allows(_message("/example@scribe_bot more"))
    assert not paths.allows(_message("/start"))
    assert not paths.allows(_message("plain text"))
    assert paths.allows(
        _message(contact={"phone_number": "+0000000000", "first_name": "Synthetic"})
    )
    assert not OpenPaths().allows(_message("/example"))


def test_only_one_limits_resolver_and_one_offer(settings: Settings) -> None:
    host = PluginHost(settings)
    resolver = SettingsLimitsResolver(BudgetCaps(0.5, 1.0))

    async def offer(telegram_id: int, lang: str, session: AsyncSession) -> list[Any]:
        return []

    host.set_limits_resolver(resolver)
    host.set_no_access_offer(offer)
    with pytest.raises(PluginError):
        host.set_limits_resolver(resolver)
    with pytest.raises(PluginError):
        host.set_no_access_offer(offer)
    options = host.dispatcher_options()
    assert options["offer_provider"] is offer
    assert isinstance(options["limits"], ExcludingRoutes)
    assert options["limits"].inner is resolver


async def test_a_plugin_resolver_keeps_the_deployment_excluded_routes(settings: Settings) -> None:
    configured = settings.model_copy(update={"llm_budget_excluded_routes": ["profile.build"]})

    class Fixed:
        async def limits_for(self, session: AsyncSession, user: User, now: datetime) -> UsageLimits:
            return UsageLimits(caps=BudgetCaps(0.1, 0.2), max_work_chats=3)

    host = PluginHost(configured)
    host.set_limits_resolver(Fixed())
    resolver = host.hooks.limits_resolver(configured)
    limits = await resolver.limits_for(None, User(), FROZEN_NOW)  # type: ignore[arg-type]

    assert limits.caps == BudgetCaps(0.1, 0.2, frozenset({"profile.build"}))
    assert limits.max_work_chats == 3


async def test_limits_changed_reaches_the_listeners(settings: Settings) -> None:
    host = PluginHost(settings)
    seen: list[uuid.UUID] = []

    async def listener(user_id: uuid.UUID) -> None:
        seen.append(user_id)

    host.on_limits_changed(listener)
    user_id = uuid.UUID(int=7)
    await host.limits_changed(user_id)
    assert seen == [user_id]


def test_privacy_hooks_are_collected(settings: Settings) -> None:
    host = PluginHost(settings)

    async def lines(session: AsyncSession, telegram_id: int, lang: str) -> list[str]:
        return []

    async def delete(session: AsyncSession, telegram_id: int) -> None:
        return None

    host.on_my_data(lines)
    host.on_delete_warning(lines)
    host.on_delete_user(delete)
    privacy = host.dispatcher_options()["privacy_hooks"]
    assert privacy.my_data == [lines]
    assert privacy.delete_warning == [lines]
    assert privacy.delete_user == [delete]


# --- operator commands (E8) -----------------------------------------------------------------------


def _admin_plugin(
    monkeypatch: pytest.MonkeyPatch, calls: list[argparse.Namespace], name: str = PLUGIN
) -> Callable[[Any], dict[str, Any]]:
    def register_admin(subparsers: Any) -> dict[str, Any]:
        command = subparsers.add_parser(f"{name}-cmd", help="a synthetic plugin command")
        command.add_argument("--flag", action="store_true")

        async def handler(args: argparse.Namespace, *_: Any) -> int:
            calls.append(args)
            return 0

        return {f"{name}-cmd": handler}

    _module(monkeypatch, name, register_admin=register_admin)
    return register_admin


async def test_admin_runs_a_plugin_command(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[argparse.Namespace] = []
    _admin_plugin(monkeypatch, calls)
    configured = _with_plugins(settings, PLUGIN)

    no_database: Any = None  # the plugin command does not use it
    code = await admin.run([f"{PLUGIN}-cmd", "--flag"], no_database, settings=configured)

    assert code == 0
    assert len(calls) == 1 and calls[0].flag is True


def test_admin_without_plugins_has_only_core_commands(settings: Settings) -> None:
    with pytest.raises(SystemExit):
        admin._parser().parse_args([f"{PLUGIN}-cmd"])


def test_modules_without_register_admin_add_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _module(monkeypatch, setup=lambda host: None)
    parser = argparse.ArgumentParser()
    assert admin_commands(parser.add_subparsers(), [PLUGIN]) == {}


def test_a_command_added_twice_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[argparse.Namespace] = []
    register = _admin_plugin(monkeypatch, calls)
    _module(monkeypatch, "scribe_test_second_plugin", register_admin=register)
    parser = argparse.ArgumentParser()
    with pytest.raises((PluginError, argparse.ArgumentError)):
        admin_commands(parser.add_subparsers(), [PLUGIN, "scribe_test_second_plugin"])


# --- SCRIBE_PLUGINS for alembic (docs/plugins.md "Database tables and migrations") -----------


@pytest.fixture
def plugin_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No SCRIBE_PLUGINS in the environment, and a working directory without the repo's .env."""
    monkeypatch.delenv("SCRIBE_PLUGINS", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_plugin_modules_default_to_none(plugin_env: Path) -> None:
    assert plugin_modules_from_env() == []


def test_plugin_modules_parse_like_settings(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCRIBE_PLUGINS", " a.plugin , b ,, a.plugin ")
    assert plugin_modules_from_env() == ["a.plugin", "b"]


def test_plugin_modules_need_no_other_setting(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SCRIBE_CONTROL_BOT_TOKEN", "SCRIBE_DATABASE_URL", "SCRIBE_MASTER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SCRIBE_PLUGINS", "a")
    assert plugin_modules_from_env() == ["a"]


def test_plugin_modules_from_dotenv_and_environment_wins(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (plugin_env / ".env").write_text("SCRIBE_PLUGINS=from_file,other\n")
    assert plugin_modules_from_env() == ["from_file", "other"]
    monkeypatch.setenv("SCRIBE_PLUGINS", "from_env")
    assert plugin_modules_from_env() == ["from_env"]


@pytest.fixture
def plugin_packages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A directory on ``sys.path`` for throwaway plugin packages; their modules are removed
    from ``sys.modules`` afterwards."""
    root = tmp_path / "site"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    before = set(sys.modules)
    yield root
    for name in set(sys.modules) - before:
        if name.startswith("scribe_test_models_"):
            del sys.modules[name]


def _package(root: Path, name: str, models: str | None = None) -> None:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    if models is not None:
        (package / "models.py").write_text(models)


def test_plugin_models_are_imported(plugin_packages: Path) -> None:
    _package(plugin_packages, "scribe_test_models_a", models="IMPORTED = True\n")
    _package(plugin_packages, "scribe_test_models_b")  # no models: skipped
    (plugin_packages / "scribe_test_models_c.py").write_text("")  # a plain module: skipped

    imported = import_plugin_models(
        ["scribe_test_models_a", "scribe_test_models_b", "scribe_test_models_c"]
    )

    assert imported == ["scribe_test_models_a.models"]
    assert sys.modules["scribe_test_models_a.models"].IMPORTED is True


def test_plugin_models_that_fail_stop_alembic(plugin_packages: Path) -> None:
    _package(
        plugin_packages, "scribe_test_models_broken", models="import scribe_test_models_absent\n"
    )
    with pytest.raises(PluginError, match="scribe_test_models_broken.models"):
        import_plugin_models(["scribe_test_models_broken"])


def test_a_missing_plugin_stops_alembic(plugin_packages: Path) -> None:
    with pytest.raises(PluginError, match="scribe_test_models_missing"):
        import_plugin_models(["scribe_test_models_missing"])
