"""Plugins: optional Python modules that extend the service without editing the core
(docs/plugins.md).

``SCRIBE_PLUGINS`` lists module names (comma-separated, empty by default). At startup
``python -m scribe`` builds one :class:`PluginHost` once the bot, the database, the client pool
and the scheduler exist, and :func:`load_plugins` imports each module and calls its
``setup(host)`` once, before the control bot's dispatcher is built. A module that cannot be
imported, has no ``setup`` or whose ``setup`` raises stops the startup (:class:`PluginError`).
With no plugin every hook below stays empty and the service behaves exactly as without this
module.

What ``setup`` can register on the host:

- texts for the control bot (:meth:`PluginHost.add_messages`; a key that exists is an error);
- per-user usage limits (:meth:`PluginHost.set_limits_resolver`, a
  :class:`scribe.drafting.budget.LimitsResolver`), and listeners a plugin fires itself when it
  changes them (:meth:`PluginHost.on_limits_changed`, :meth:`PluginHost.limits_changed`);
- a decoration of the "drafting paused" and "access ends soon" notices
  (:meth:`PluginHost.wrap_budget_notifier`);
- control-bot routers, before or after the core ones (:meth:`PluginHost.add_router`), for
  example with ``pre_checkout_query`` handlers (:meth:`PluginHost.handle_pre_checkout`);
- commands, callback prefixes and message filters that pass the access gate for people without
  access (:meth:`PluginHost.gate_allow`);
- the buttons of the "no access" and usage-limit replies (:meth:`PluginHost.set_no_access_offer`);
- sections of ``/my_data``, lines of the ``/delete_my_data`` question and a hook in its delete
  transaction (:meth:`PluginHost.on_my_data`, :meth:`PluginHost.on_delete_warning`,
  :meth:`PluginHost.on_delete_user`).

Operator commands are separate: ``python -m scribe.admin`` calls a module's
``register_admin(subparsers)`` (:func:`admin_commands`), without a host.
"""

from __future__ import annotations

import argparse
import importlib
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import OfferProvider
from scribe.access.gate import MessageFilter, OpenPaths
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.drafting.budget import (
    ExcludingRoutes,
    LimitsResolver,
    MarkupFactory,
    NoticeDecorator,
    SettingsLimitsResolver,
)
from scribe.i18n import Entry, Msg, register_messages
from scribe.logging import get_logger
from scribe.telegram.control_bot.account_command import PrivacyDelete, PrivacyHooks, PrivacyLines

if TYPE_CHECKING:
    from scribe.scheduling.scheduler import Scheduler
    from scribe.telegram.client_pool import ClientPool

log = get_logger("scribe.plugins")

LimitsListener = Callable[[uuid.UUID], Awaitable[None]]
# A plugin operator command: (parsed arguments, session factory, settings, clock) -> exit code.
AdminHandler = Callable[
    [argparse.Namespace, async_sessionmaker[AsyncSession], Settings | None, Clock],
    Awaitable[int],
]


class PluginError(Exception):
    """A plugin could not be loaded or set up: the service does not start."""


@dataclass
class PluginHooks:
    """Everything plugins registered; the defaults are "no plugin"."""

    routers_first: list[Router] = field(default_factory=list)
    routers: list[Router] = field(default_factory=list)
    open_commands: set[str] = field(default_factory=set)
    open_callback_prefixes: list[str] = field(default_factory=list)
    open_message_filters: list[MessageFilter] = field(default_factory=list)
    offer_provider: OfferProvider | None = None
    limits: LimitsResolver | None = None
    limits_listeners: list[LimitsListener] = field(default_factory=list)
    notice_decorators: list[NoticeDecorator] = field(default_factory=list)
    privacy: PrivacyHooks = field(default_factory=PrivacyHooks)

    def open_paths(self) -> OpenPaths:
        return OpenPaths.build(
            self.open_commands, self.open_callback_prefixes, self.open_message_filters
        )

    def limits_resolver(self, settings: Settings) -> LimitsResolver:
        """The plugin's resolver (with the deployment's excluded routes applied), else the
        deployment caps for everyone."""
        if self.limits is None:
            return SettingsLimitsResolver.from_settings(settings)
        return ExcludingRoutes(self.limits, settings.llm_budget_excluded_routes)

    def notice_decorator(self) -> NoticeDecorator | None:
        """The registered decorators applied in order (None without any)."""
        if not self.notice_decorators:
            return None
        decorators = list(self.notice_decorators)

        async def decorate(user_id: uuid.UUID, message: Msg) -> tuple[Msg, MarkupFactory | None]:
            markup: MarkupFactory | None = None
            for decorator in decorators:
                message, added = await decorator(user_id, message)
                markup = added if added is not None else markup
            return message, markup

        return decorate


class PluginHost:
    """What a plugin's ``setup(host)`` gets: the service's shared objects and the registration
    methods (see the module docstring)."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        bot: Bot | None = None,
        scheduler: Scheduler | None = None,
        clock: Clock = utcnow,
        pool: ClientPool | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.bot = bot
        self.scheduler = scheduler
        self.clock = clock
        self.pool = pool
        self.hooks = PluginHooks()

    # --- texts ------------------------------------------------------------------------------

    def add_messages(self, lang: str, texts: Mapping[str, Entry]) -> None:
        try:
            register_messages(lang, texts)
        except ValueError as exc:
            raise PluginError(str(exc)) from None

    # --- usage limits -----------------------------------------------------------------------

    def set_limits_resolver(self, resolver: LimitsResolver) -> None:
        if self.hooks.limits is not None:
            raise PluginError("a limits resolver is already set by another plugin")
        self.hooks.limits = resolver

    def on_limits_changed(self, listener: LimitsListener) -> None:
        """Called by :meth:`limits_changed`; the core itself never fires it."""
        self.hooks.limits_listeners.append(listener)

    async def limits_changed(self, user_id: uuid.UUID) -> None:
        """Tell every listener that the user's limits changed (a plugin calls this)."""
        for listener in self.hooks.limits_listeners:
            await listener(user_id)

    def wrap_budget_notifier(self, decorator: NoticeDecorator) -> None:
        self.hooks.notice_decorators.append(decorator)

    # --- control bot ------------------------------------------------------------------------

    def add_router(self, router: Router, first: bool = False) -> None:
        (self.hooks.routers_first if first else self.hooks.routers).append(router)

    def handle_pre_checkout(self, router: Router) -> None:
        """A router with ``pre_checkout_query`` handlers (the core registers none)."""
        self.add_router(router)

    def gate_allow(
        self,
        commands: Iterable[str] = (),
        callback_prefixes: Iterable[str] = (),
        message_filter: MessageFilter | None = None,
    ) -> None:
        self.hooks.open_commands.update(c.lstrip("/").lower() for c in commands)
        self.hooks.open_callback_prefixes.extend(callback_prefixes)
        if message_filter is not None:
            self.hooks.open_message_filters.append(message_filter)

    def set_no_access_offer(self, provider: OfferProvider) -> None:
        if self.hooks.offer_provider is not None:
            raise PluginError("a no-access offer is already set by another plugin")
        self.hooks.offer_provider = provider

    # --- personal data ----------------------------------------------------------------------

    def on_my_data(self, hook: PrivacyLines) -> None:
        self.hooks.privacy.my_data.append(hook)

    def on_delete_warning(self, hook: PrivacyLines) -> None:
        self.hooks.privacy.delete_warning.append(hook)

    def on_delete_user(self, hook: PrivacyDelete) -> None:
        self.hooks.privacy.delete_user.append(hook)

    # --- what the core reads ----------------------------------------------------------------

    def dispatcher_options(self) -> dict[str, Any]:
        """Keyword arguments for ``create_dispatcher``."""
        return {
            "routers_first": list(self.hooks.routers_first),
            "routers": list(self.hooks.routers),
            "open_paths": self.hooks.open_paths(),
            "offer_provider": self.hooks.offer_provider,
            "limits": self.hooks.limits_resolver(self.settings),
            "privacy_hooks": self.hooks.privacy,
        }


def import_plugin(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except Exception as exc:
        raise PluginError(f"plugin {name!r} cannot be imported: {type(exc).__name__}") from exc


def load_plugins(settings: Settings, host: PluginHost) -> list[str]:
    """Import each module of ``settings.plugins`` and call its ``setup(host)`` once; the names
    loaded. Raises :class:`PluginError` on the first module that fails."""
    loaded: list[str] = []
    for name in settings.plugins:
        module = import_plugin(name)
        setup = getattr(module, "setup", None)
        if not callable(setup):
            raise PluginError(f"plugin {name!r} has no setup(host) function")
        try:
            setup(host)
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError(f"plugin {name!r} setup failed: {type(exc).__name__}") from exc
        loaded.append(name)
        log.info("plugin_loaded", plugin=name)
    return loaded


def admin_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], modules: Sequence[str]
) -> dict[str, AdminHandler]:
    """``python -m scribe.admin``: for each module with ``register_admin(subparsers)``, add its
    commands; the handlers by command name. A name taken twice is a :class:`PluginError`."""
    handlers: dict[str, AdminHandler] = {}
    for name in modules:
        register = getattr(import_plugin(name), "register_admin", None)
        if not callable(register):
            continue
        added = register(subparsers)
        taken = sorted(set(added) & set(handlers))
        if taken:
            raise PluginError(f"plugin {name!r} adds commands that exist: {', '.join(taken)}")
        handlers.update(added)
    return handlers


__all__ = [
    "AdminHandler",
    "LimitsListener",
    "PluginError",
    "PluginHooks",
    "PluginHost",
    "admin_commands",
    "import_plugin",
    "load_plugins",
]
