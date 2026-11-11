"""``python -m scribe``: one process running the control bot, the users' Telegram clients,
the scheduler and the health endpoint.

Startup order (fail fast on configuration): settings (an open bot needs SCRIBE_OPEN_BOT=true,
issue #53) -> logging -> encryption keyring ->
LLM route table validation -> database -> LLMRouter -> ClientPool -> scheduler -> plugins
(``SCRIBE_PLUGINS``, docs/plugins.md: each module's ``setup(host)``; a failing plugin is a
configuration error) -> control bot (logins interrupted by the last restart are cleaned up,
then polling) -> scheduler jobs and loop -> health server.
SIGTERM/SIGINT stop everything gracefully.

Before any of it, ``run`` takes the single-instance advisory lock (``scribe.instance_lock``,
#60): if another instance holds it, nothing starts and the process exits with code 1. Exit
codes: 0 stopped normally, 1 runtime failure or another instance running, 2 configuration
error.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import uuid
from collections.abc import Callable, Mapping
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardMarkup
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from scribe.access.reconcile import register_access_reconcile
from scribe.chats import work_account
from scribe.clock import Clock, utcnow
from scribe.config import Settings, get_settings
from scribe.context import commitments as commitments_ctx
from scribe.context.earlier import EarlierSearch
from scribe.context.tasks.jira import JiraTaskSource
from scribe.context.tasks.jira.client import JiraOptions
from scribe.context.tasks.jira.sync import JiraCommentSync
from scribe.context.tasks.jira.sync import register as register_jira_jobs
from scribe.context.tasks.service import TaskSources, install_default_task_sources
from scribe.context.tasks.trello import TrelloTaskSource
from scribe.crypto import configure_keyring, keyring_from_settings
from scribe.db.models import UserState
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.db.repos import users as users_repo
from scribe.db.session import create_engine_from_settings, create_session_factory
from scribe.drafting.budget import BudgetGuard
from scribe.drafting.composer import Composer
from scribe.drafting.reply_gate import ReplyGate
from scribe.i18n import Msg
from scribe.instance_lock import InstanceLock
from scribe.intake.events import ChatEventHandler
from scribe.intake.router import IntakeRouter
from scribe.jobs import register_profile_jobs
from scribe.llm.pricing import PricingError, load_price_table
from scribe.llm.registry import KNOWN_PROVIDERS, LLMRouter, PromptStore, build_adapters
from scribe.llm.routes import RouteTableError, load_route_table, validate_route_table
from scribe.logging import configure_logging, get_logger
from scribe.observability.health import HealthServer, HealthStatus
from scribe.observability.spend_alert import register_spend_alert_job
from scribe.plugins import PluginError, PluginHost, load_plugins
from scribe.release_notes.sender import recover_interrupted, register_release_notes_job
from scribe.retention.pending_credentials import register_pending_credentials_job
from scribe.retention.purge import register_purge_job
from scribe.scheduling.deadlines import register_review_deadlines
from scribe.scheduling.scheduler import DueTurnsProcessor, Scheduler
from scribe.sending.auto_send import AutoSendAdvisor, AutoSender
from scribe.telegram.client_pool import ClientPool
from scribe.telegram.control_bot.bot import create_bot, create_dispatcher
from scribe.telegram.control_bot.connect_flow import (
    login_activity_recorder,
    on_late_scan,
    recover_interrupted_logins,
)
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.control_bot.grammatical_gender import register_gender_prompt_job
from scribe.telegram.control_bot.menu import set_bot_commands
from scribe.telegram.control_bot.review import DraftPresenter
from scribe.telegram.login import LoginManager, telethon_login_factory
from scribe.telegram.unread_mark import UnreadMarker

log = get_logger("scribe")

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2
BOT_RETRY_MAX_S = 300.0
EXTRACTION_DRAIN_S = 10.0


class ConfigError(Exception):
    pass


def build_llm_router(
    settings: Settings, environ: Mapping[str, str], *, clock: Clock = utcnow
) -> LLMRouter:
    """Load and validate the route table; any problem is a startup failure."""
    try:
        routes = load_route_table(settings.llm_routes_path, environ)
        prices = load_price_table(settings.llm_prices_path)
        validate_route_table(routes, prices, KNOWN_PROVIDERS)
        adapters = build_adapters(routes, environ)
    except (RouteTableError, PricingError) as exc:
        raise ConfigError(str(exc)) from None
    return LLMRouter(
        routes=routes,
        prices=prices,
        adapters=adapters,
        prompts=PromptStore(settings.prompts_dir),
        clock=clock,
    )


class Service:
    def __init__(
        self,
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
        bot_factory: Callable[[Settings], Bot] = create_bot,
        clock: Clock = utcnow,
    ) -> None:
        self.settings = settings
        self._environ = os.environ if environ is None else environ
        self._bot_factory = bot_factory
        self._clock = clock
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.bot_polling = False
        self._logins_recovered = False
        self.engine: AsyncEngine | None = None
        self.health: HealthServer | None = None

    async def start(self) -> None:
        settings = self.settings
        if settings.access_gate and not settings.operator_telegram_ids:
            # specs/005 research Q8: still starts; the operator can add themselves (beta add).
            log.warning("access_gate_no_operator")
        if not settings.access_gate:
            log.warning("access_gate_off_open_bot")  # issue #53: SCRIBE_OPEN_BOT=true
        configure_keyring(keyring_from_settings(settings))
        self.llm_router = build_llm_router(settings, self._environ, clock=self._clock)

        self.engine = create_engine_from_settings(settings)
        self.session_factory: async_sessionmaker[AsyncSession] = create_session_factory(self.engine)
        # Issue #67: every billed LLM call adds to the user's daily spend (llm_spend).
        self.llm_router.spend_sink = llm_spend_repo.SpendRecorder(self.session_factory)
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        # One commitment extractor per process: the composers, the send path and intake share it,
        # and stop() drains it before the engine is disposed of.
        self.commitments = commitments_ctx.ensure_default_extractor(
            self.session_factory, self.llm_router, clock=self._clock
        )

        self.bot = self._bot_factory(settings)
        self.pool = ClientPool(
            self.session_factory,
            api_id=settings.tg_api_id,
            api_hash=settings.tg_api_hash.get_secret_value(),
            notifier=self._notify_user,
            device_model=settings.tg_device_model,
            system_version=settings.tg_system_version,
            app_version=settings.tg_app_version,
        )
        self.scheduler = Scheduler(
            self.session_factory, tick_s=settings.scheduler_tick_s, clock=self._clock
        )
        # Plugins (docs/plugins.md) register their hooks before anything below reads them.
        self.plugin_host = PluginHost(
            settings,
            session_factory=self.session_factory,
            bot=self.bot,
            scheduler=self.scheduler,
            clock=self._clock,
            pool=self.pool,
        )
        try:
            load_plugins(settings, self.plugin_host)
        except PluginError as exc:
            raise ConfigError(str(exc)) from exc
        hooks = self.plugin_host.hooks
        limits = hooks.limits_resolver(settings)
        decorate = hooks.notice_decorator()
        # 004: the reply gate skips turns that only close the exchange (no draft, no card) and
        # keeps such a message from leaving a pending draft outdated. It never sends.
        self.reply_gate = ReplyGate(
            self.session_factory, self.llm_router, settings, clock=self._clock, limits=limits
        )
        # specs/004: one search over earlier conversations (the sender's enabled work chats).
        self.earlier_search = EarlierSearch(self.pool, settings, clock=self._clock)
        # US1: incoming work messages -> waiting turns (other chats are never stored).
        self.intake = IntakeRouter(
            self.session_factory,
            settings,
            clock=self._clock,
            notifier=self._notify_user,
            gate=self.reply_gate,
            limits=limits,
        )
        self.pool.on_new_message(self.intake.handle_new_message)
        self.telegram_login = LoginManager(
            telethon_login_factory(
                settings.tg_api_id,
                settings.tg_api_hash.get_secret_value(),
                device_model=settings.tg_device_model,
                system_version=settings.tg_system_version,
                app_version=settings.tg_app_version,
            ),
            record=login_activity_recorder(self.session_factory),
            clock=self._clock,
            # A drained code scanned late (research Q4).
            on_late_scan=on_late_scan(self.bot, self.session_factory),
        )
        # specs/009: chats the agent read or answered are marked unread right afterwards.
        self.unread_marker = UnreadMarker(self.session_factory, self.pool)
        self.pool.on_unread_mark_cleared(self.unread_marker.handle_unread_mark_cleared)
        # specs/014: the offer of auto-send and the suggestion to turn it off, after a decision.
        self.auto_send_advisor = AutoSendAdvisor(self.session_factory, self.bot, clock=self._clock)
        self.dispatcher: Dispatcher = create_dispatcher(
            session_factory=self.session_factory,
            settings=settings,
            storage=PostgresFSMStorage(self.session_factory),
            llm_router=self.llm_router,
            gateway=self.pool,
            client_pool=self.pool,
            clock=self._clock,
            telegram_login=self.telegram_login,
            unread_marker=self.unread_marker,
            auto_send_advisor=self.auto_send_advisor,
            **self.plugin_host.dispatcher_options(),
        )
        await self.pool.start_all()
        self._tasks.append(asyncio.create_task(self._run_bot(), name="control-bot"))

        presenter = DraftPresenter(self.bot, self.session_factory, clock=self._clock)
        # US2: manual replies drop open turns, reading elsewhere makes them due now, new
        # messages flag a pending draft as outdated.
        self.chat_events = ChatEventHandler(
            self.session_factory,
            clock=self._clock,
            notifier=self._notify_user,
            cards=presenter,
            gate=self.reply_gate,  # 004: closing turns a manual reply dropped feed the habit rule
            marker=self.unread_marker,  # specs/009: reads elsewhere / manual replies are "seen"
            limits=limits,
        )
        self.pool.on_outgoing(self.chat_events.handle_outgoing)
        self.pool.on_read_elsewhere(self.chat_events.handle_read_elsewhere)
        self.intake.on_draft_outdated(presenter.show_outdated)
        # Step 1: due turns are read; closing messages are skipped by the reply gate (004), the
        # rest are drafted and shown as cards. Nothing here can send.
        self.scheduler.on_due_turns(
            DueTurnsProcessor(
                self.session_factory,
                self.pool,
                Composer(
                    self.session_factory,
                    self.llm_router,
                    settings,
                    clock=self._clock,
                    earlier_search=self.earlier_search,
                    commitments=self.commitments,
                    limits=limits,
                ),
                presenter,
                gate=self.reply_gate,
                marker=self.unread_marker,
                # Issue #74: at the daily hard cap turns are skipped, with one notice a day.
                budget=BudgetGuard(
                    self.session_factory,
                    limits,
                    notifier=self._notify_user,
                    clock=self._clock,
                    decorate=decorate,
                ),
                clock=self._clock,
                read_concurrency=settings.read_concurrency,
                compose_concurrency=settings.compose_concurrency,
            )
        )
        # Step 1b (specs/014): the one step that can send without a tap, only for drafts of chats
        # the user opted in to whose cancel window ended (AutoSender re-checks everything).
        self.scheduler.on_auto_sends(
            AutoSender(
                self.session_factory,
                self.pool,
                presenter,
                clock=self._clock,
                marker=self.unread_marker,
                commitments=self.commitments,
                advisor=self.auto_send_advisor,
            )
        )
        # Steps 2-3: one reminder, then expiry (control chat only; never sends to a contact).
        register_review_deadlines(self.scheduler, presenter, clock=self._clock)
        register_purge_job(self.scheduler, self.session_factory, settings, self._clock)
        # Issue #56: abandoned Trello/Jira connect flows drop their pending credentials.
        register_pending_credentials_job(
            self.scheduler, self.session_factory, self.bot, self._clock
        )
        # specs/005: losing or regaining access stops or resumes the agent within ~1 minute.
        register_access_reconcile(
            self.scheduler,
            self.session_factory,
            self.pool,
            self.bot,
            settings,
            self._clock,
            decorate=decorate,
        )
        # specs/011: after a new version, each user with access gets one "what's new" message.
        release_notes = register_release_notes_job(
            self.scheduler, self.session_factory, self.bot, settings, self._clock
        )
        # Issue #46: users set up before it are asked their grammatical gender once, paced by
        # the release notes pacer (one budget for both).
        register_gender_prompt_job(
            self.scheduler, self.session_factory, self.bot, release_notes.pacer, self._clock
        )
        # Issue #74: the operators' daily LLM spend alert (p95 and cap hits of yesterday).
        register_spend_alert_job(
            self.scheduler, self.session_factory, self.bot, settings, self._clock, limits=limits
        )
        # US4: weekly profile refresh with a "what changed" note and undo (FR-037).
        register_profile_jobs(
            self.scheduler,
            self.session_factory,
            self.llm_router,
            self.bot,
            settings,
            self._clock,
            gateway=self.pool,  # refreshes read the history from Telegram, like a Rebuild
            limits=limits,
        )
        # specs/003: the Jira source with the operator's settings, and its comment sync jobs.
        install_default_task_sources(
            TaskSources(
                {
                    "trello": TrelloTaskSource(),
                    "jira": JiraTaskSource(JiraOptions.from_settings(settings)),
                }
            )
        )
        register_jira_jobs(
            self.scheduler,
            JiraCommentSync(
                self.session_factory,
                self.llm_router,
                settings,
                clock=self._clock,
                notifier=self._notify_user,
            ),
            settings,
        )
        await self.scheduler.requeue_interrupted_turns()  # left in "reading" by a restart
        # specs/008 R8: finish a work account mode switch a restart cut short.
        await work_account.reconcile_recent(self.session_factory, now=self._clock())
        # specs/011: release notes left in "sending" by a crash are never resent (FR-016).
        await recover_interrupted(self.session_factory, now=self._clock())
        await self.scheduler.ensure_jobs()
        self._tasks.append(asyncio.create_task(self.scheduler.run(self._stop), name="scheduler"))

        self.health = HealthServer(
            self.health_status,
            host=settings.health_host,
            port=settings.health_port,
            access_gate=settings.access_gate,
        )
        await self.health.start()
        log.info("scribe_started", users_running=len(self.pool.running_user_ids))

    def request_stop(self) -> None:
        self._stop.set()

    async def wait(self) -> None:
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()
        log.info("scribe_stopping")
        if getattr(self, "dispatcher", None) is not None and self.bot_polling:
            with contextlib.suppress(RuntimeError):  # polling not running
                await self.dispatcher.stop_polling()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if getattr(self, "telegram_login", None) is not None:
            await self.telegram_login.close_all()  # pending logins end as "interrupted"
        if getattr(self, "scheduler", None) is not None:
            await self.scheduler.shutdown()
        if getattr(self, "pool", None) is not None:
            await self.pool.stop_all()
        if self.health is not None:
            await self.health.stop()
        if getattr(self, "bot", None) is not None:
            await self.bot.session.close()
        # Background commitment extractions still need the database and the LLM router.
        await commitments_ctx.drain_default_extractor(timeout_s=EXTRACTION_DRAIN_S)
        if self.engine is not None:
            await self.engine.dispose()
        log.info("scribe_stopped")

    async def health_status(self) -> HealthStatus:
        async with self.session_factory() as session:
            users_active = await users_repo.count_by_state(session, UserState.active)
        last_tick = self.scheduler.last_tick_at
        stall_limit = timedelta(seconds=3 * self.settings.scheduler_tick_s + 30)
        scheduler_ok = last_tick is not None and self._clock() - last_tick <= stall_limit
        return HealthStatus(ok=scheduler_ok and self.bot_polling, users_active=users_active)

    async def _run_bot(self) -> None:
        """Poll the Bot API; on failure (bad token, network) log and retry with backoff."""
        delay = 5.0
        while not self._stop.is_set():
            try:
                me = await self.bot.get_me()
                with contextlib.suppress(Exception):
                    await set_bot_commands(self.bot)
                if not self._logins_recovered:  # once, before the first update (research Q10)
                    await recover_interrupted_logins(
                        self.bot, self.dispatcher, self.session_factory
                    )
                    self._logins_recovered = True
                self.bot_polling = True
                delay = 5.0
                log.info("control_bot_polling", bot_username=me.username)
                await self.dispatcher.start_polling(
                    self.bot, handle_signals=False, close_bot_session=False
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("control_bot_unavailable", error=type(exc).__name__, retry_in_s=delay)
            finally:
                self.bot_polling = False
            if self._stop.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, BOT_RETRY_MAX_S)

    async def _notify_user(
        self,
        user_id: uuid.UUID,
        message: Msg,
        reply_markup: Callable[[str], InlineKeyboardMarkup] | None = None,
    ) -> None:
        """A background notice in the user's stored language (``users.language``)."""
        async with self.session_factory() as session:
            user = await users_repo.get_user(session, user_id)
        if user is not None:
            lang = user.language
            await self.bot.send_message(
                user.telegram_control_chat_id,
                message.render(lang),
                reply_markup=reply_markup(lang) if reply_markup is not None else None,
            )


async def run(settings: Settings) -> int:
    try:
        lock = await InstanceLock.try_acquire(settings.database_url.get_secret_value())
    except Exception as exc:
        log.exception("startup_failed", error=type(exc).__name__)
        return EXIT_RUNTIME_ERROR
    if lock is None:
        log.error("startup_failed_another_instance_running")
        print(
            "scribe: another instance is already running against this database; "
            "only one may run at a time (stop it first)",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_ERROR
    try:
        return await _run_service(settings)
    finally:
        with contextlib.suppress(Exception):
            await lock.release()


async def _run_service(settings: Settings) -> int:
    service = Service(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, service.request_stop)
    try:
        await service.start()
    except ConfigError as exc:
        log.error("startup_failed_invalid_llm_config", error=str(exc))
        await service.stop()
        return EXIT_CONFIG_ERROR
    except Exception as exc:
        log.exception("startup_failed", error=type(exc).__name__)
        await service.stop()
        return EXIT_RUNTIME_ERROR
    await service.wait()
    await service.stop()
    return EXIT_OK


def _describe(exc: ValidationError) -> str:
    """Field names and messages only: inputs may be secrets."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<settings>'}: {err['msg']}"
        for err in exc.errors()
    )


def open_bot_refusal(settings: Settings) -> str | None:
    """Issue #53: why the service must not start an open bot, or None.

    With the access gate off every sender is served: anyone who finds the bot can log in their
    Telegram account, store messages on this server and spend the operator's LLM key. That is
    an explicit choice (``SCRIBE_OPEN_BOT=true``), never a default. Checked here, at service
    startup, not in ``Settings``: ``python -m scribe.admin`` and Alembic read the same settings
    and must keep working while the operator fixes the configuration."""
    if settings.access_gate or settings.open_bot:
        return None
    return (
        "the access gate is off, so anyone who finds the bot could use it; set "
        "SCRIBE_ACCESS_GATE=true (and SCRIBE_OPERATOR_TELEGRAM_IDS) to let in only people you "
        "approve, or SCRIBE_OPEN_BOT=true to serve everyone on purpose"
    )


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"scribe: invalid configuration: {_describe(exc)}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    refusal = open_bot_refusal(settings)
    if refusal is not None:
        print(f"scribe: invalid configuration: {refusal}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    configure_logging(settings.log_level, libraries_debug=settings.log_libraries_debug)
    return asyncio.run(run(settings))


if __name__ == "__main__":
    sys.exit(main())
