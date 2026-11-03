"""Connect Jira from ``/tasks`` (specs/003-jira-commitments J15, contracts/control-chat.md).

Flow (``JiraStates``): **🔌 Connect Jira** ``jira:connect`` (when Trello is connected, a
confirmation ``jira:replace`` first) → deployment ``jdep:cloud`` / ``jdep:self`` → site address →
Cloud: account email → scoped API token (``read:jira-work`` + ``read:jira-user``);
self-hosted: personal access token → (a company-issued certificate) the certificate card with
**Trust this certificate** ``jcert:<sha256 prefix>`` → saved, then the project picker from
``tasks_command`` (``config_json.project_keys``, at most 20).

The token message is deleted right away; while the certificate card waits the credentials stay
only in the encrypted FSM data. They are dropped by /cancel, any other command or any button
outside the flow (``tasks_command.AbandonedConnectMiddleware``; the flow's own ``jira:``,
``jdep:`` and ``jcert:`` buttons keep them), the end of the step, a step taken after
:data:`~tasks_command.KEY_TTL` from the start (checked on that input), and otherwise by the
``purge_pending_credentials`` job once the flow is untouched for ``KEY_TTL``
(``retention/pending_credentials.py``, every minute). The FSM data is never exported
(``retention/user_data.py``).

Every check runs through ``JiraTaskSource.connect`` (address guard, version, ``/myself``)
before anything is stored. Issue #61: the address must use one of the operator's ports
(``SCRIBE_JIRA_ALLOWED_PORTS``); each user gets at most
:data:`~scribe.context.tasks.jira.attempts.MAX_ATTEMPTS` checks per
:data:`~scribe.context.tasks.jira.attempts.WINDOW_S` seconds; every network-level failure (DNS,
refused, timeout, private address, TLS) gets one message, so the replies cannot be used to
probe hosts and ports (the detail goes to the log). Answers that need a working HTTPS
connection to the site (the certificate card, "not Jira", version, token) keep their own
messages. Buttons act only on the caller's own FSM data and connection. Every text comes from
the catalogs (``jira.*``). Never logs tokens, addresses or certificates.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.context.tasks.base import (
    CertInfo,
    JiraAddressInvalid,
    JiraAuthFailed,
    JiraCertificateUntrusted,
    JiraPortNotAllowed,
    JiraUnreachable,
    JiraVersionUnsupported,
    TaskSourceUnavailable,
)
from scribe.context.tasks.jira import JiraTaskSource
from scribe.context.tasks.jira.netguard import DEFAULT_ALLOWED_PORTS, validate_site_url
from scribe.context.tasks.jira.source import JiraConnectResult
from scribe.context.tasks.service import default_task_sources
from scribe.db.models import ConnectionStatus, TaskProvider, TaskSourceConnection, User
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.i18n import Lang, t
from scribe.logging import get_logger
from scribe.telegram.control_bot import tasks_command
from scribe.telegram.control_bot.render import (
    InvalidCallbackData,
    button,
    format_callback,
    keyboard,
    parse_callback,
)

log = get_logger("scribe.control_bot")

CLOUD_EXAMPLE = "https://your-team.atlassian.net"
SELF_HOSTED_EXAMPLE = "https://jira.example.com"


class JiraStates(StatesGroup):
    deployment = State()
    site = State()
    email = State()
    token = State()
    pat = State()
    cert = State()


def deployment_keyboard(lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button(t("btn.jira_cloud", lang), format_callback("jdep", "cloud")),
                button(t("btn.jira_self_hosted", lang), format_callback("jdep", "self")),
            ],
            [button(t("btn.cancel", lang), format_callback("jira", "cancel"))],
        ]
    )


def cert_ref(sha256: str) -> str:
    return sha256.replace(":", "")[:16].lower()


def cert_keyboard(cert: CertInfo, lang: str) -> InlineKeyboardMarkup:
    trust = format_callback("jcert", cert_ref(cert.sha256))
    return keyboard(
        [
            [button(t("btn.jira_trust_cert", lang), trust)],
            [button(t("btn.cancel", lang), format_callback("jira", "cancel"))],
        ]
    )


def cert_text(cert: CertInfo, lang: str) -> str:
    return t(
        "jira.cert_card",
        lang,
        subject=cert.subject,
        issuer=cert.issuer,
        not_after=cert.not_after,
        sha256=cert.sha256,
    )


def error_text(exc: TaskSourceUnavailable, lang: str) -> str:
    """The user-facing message for a failed check (contracts/control-chat.md table).

    Network-level failures share one message (issue #61): unreachable, private address and an
    untrusted certificate that could not be shown."""
    if isinstance(exc, JiraPortNotAllowed):
        ports = ", ".join(str(p) for p in exc.allowed_ports)
        return t("jira.port_not_allowed", lang, ports=ports)
    if isinstance(exc, JiraAddressInvalid):
        return t("jira.address_invalid", lang)
    if isinstance(exc, JiraUnreachable | JiraCertificateUntrusted):
        return t("jira.unreachable", lang)
    if isinstance(exc, JiraVersionUnsupported):
        return t("jira.version", lang, found=exc.found or "?", minimum=exc.minimum)
    if isinstance(exc, JiraAuthFailed):
        hint = t("jira.classic_token", lang) if exc.hint == "classic_token" else ""
        return t("jira.auth", lang, hint=hint)
    return t("jira.unavailable", lang)


def _plausible_token(value: str) -> bool:
    return 16 <= len(value) <= 500 and not any(ch.isspace() for ch in value)


def _jira_source() -> JiraTaskSource:
    source = default_task_sources().get("jira")
    if not isinstance(source, JiraTaskSource):
        raise TaskSourceUnavailable("jira source is not available")
    return source


def _allowed_ports() -> tuple[int, ...]:
    try:
        return _jira_source().options.allowed_ports
    except TaskSourceUnavailable:
        return DEFAULT_ALLOWED_PORTS


# --- handlers -----------------------------------------------------------------------------------


async def jira_menu(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        item = parse_callback(callback.data).entity_id
    except InvalidCallbackData:
        await callback.answer()
        return
    chat_id = user.telegram_control_chat_id
    await callback.answer()
    if item == "cancel":
        await state.clear()
        await bot.send_message(chat_id, t("settings.cancelled", lang))
        return
    if item == "connect":
        async with session_factory() as session:
            conn = await tasks_conn_repo.get_connection(session, user.id)
        if (
            conn is not None
            and conn.provider == TaskProvider.trello
            and conn.status != ConnectionStatus.disconnected
        ):
            replace = format_callback("jira", "replace")
            await bot.send_message(
                chat_id,
                t("jira.replace_trello", lang),
                reply_markup=keyboard(
                    [
                        [
                            button(t("btn.replace_with_jira", lang), replace),
                            button(t("btn.cancel", lang), format_callback("jira", "cancel")),
                        ]
                    ]
                ),
            )
            return
    if item in {"connect", "replace"}:
        await state.set_state(JiraStates.deployment)
        await state.set_data({"started_at": clock().isoformat()})
        await bot.send_message(
            chat_id, t("jira.deployment_prompt", lang), reply_markup=deployment_keyboard(lang)
        )


async def deployment_choice(
    callback: CallbackQuery, bot: Bot, state: FSMContext, user: User | None, lang: Lang
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        item = parse_callback(callback.data).entity_id
    except InvalidCallbackData:
        await callback.answer()
        return
    await callback.answer()
    if item not in {"cloud", "self"}:
        return
    await state.set_state(JiraStates.site)
    await state.update_data(deployment="cloud" if item == "cloud" else "self_hosted")
    example = CLOUD_EXAMPLE if item == "cloud" else SELF_HOSTED_EXAMPLE
    await bot.send_message(
        user.telegram_control_chat_id, t("jira.site_prompt", lang, example=example)
    )


async def site_input(message: Message, state: FSMContext, user: User | None, lang: Lang) -> None:
    if user is None:
        await state.clear()
        return
    try:
        site = validate_site_url(message.text or "", allowed_ports=_allowed_ports())
    except JiraAddressInvalid as exc:
        await message.answer(error_text(exc, lang))
        return
    data = await state.get_data()
    await state.update_data(site_url=site.url)
    if data.get("deployment") == "cloud":
        await state.set_state(JiraStates.email)
        await message.answer(t("jira.email_prompt", lang))
    else:
        await state.set_state(JiraStates.pat)
        await message.answer(t("jira.pat_prompt", lang))


async def email_input(message: Message, state: FSMContext, user: User | None, lang: Lang) -> None:
    if user is None:
        await state.clear()
        return
    email = (message.text or "").strip()
    if "@" not in email or len(email) > 320 or any(ch.isspace() for ch in email):
        await message.answer(t("jira.invalid_email", lang))
        return
    await state.update_data(email=email)
    await state.set_state(JiraStates.token)
    await message.answer(t("jira.token_prompt", lang))


def _expired(data: dict[str, Any], now: datetime) -> bool:
    try:
        started = datetime.fromisoformat(str(data.get("started_at")))
    except ValueError:
        return True
    return now - started > tasks_command.KEY_TTL


async def secret_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """The scoped API token (Cloud) or personal access token (self-hosted)."""
    secret = (message.text or "").strip()
    await tasks_command.delete_quietly(bot, message)
    data = await state.get_data()
    if user is None:
        await state.clear()
        return
    if _expired(data, clock()):
        await state.clear()
        await message.answer(t("jira.expired", lang))
        return
    if not _plausible_token(secret):
        await message.answer(t("jira.invalid_token", lang))
        return
    credentials: dict[str, Any] = {
        "deployment": data.get("deployment"),
        "site_url": data.get("site_url"),
    }
    if data.get("deployment") == "cloud":
        credentials |= {"email": data.get("email"), "api_token": secret}
    else:
        credentials |= {"pat": secret}
    await _check_and_save(bot, state, user, session_factory, credentials, lang)


async def cert_trust(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    data = await state.get_data()
    pending, cert = data.get("pending"), data.get("cert")
    try:
        ref = parse_callback(callback.data).entity_id
    except InvalidCallbackData:
        await callback.answer()
        return
    if (
        await state.get_state() != JiraStates.cert.state
        or not isinstance(pending, dict)
        or not isinstance(cert, dict)
        or cert_ref(str(cert.get("sha256") or "")) != ref
    ):
        await callback.answer(t("alert.jira_cert_expired", lang))
        return
    await callback.answer()
    if _expired(data, clock()):
        await state.clear()
        await bot.send_message(user.telegram_control_chat_id, t("jira.expired", lang))
        return
    credentials = {**pending, "pinned_cert": cert}
    await _check_and_save(bot, state, user, session_factory, credentials, lang)


async def _check_and_save(
    bot: Bot,
    state: FSMContext,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    credentials: dict[str, Any],
    lang: str,
) -> None:
    chat_id = user.telegram_control_chat_id
    try:
        source = _jira_source()
    except TaskSourceUnavailable:
        await state.clear()
        await bot.send_message(chat_id, t("jira.unavailable", lang))
        return
    wait_s = source.connect_attempts.try_attempt(user.id)
    if wait_s > 0:
        await state.clear()  # the token leaves the FSM data
        log.warning("jira_connect_rate_limited", user_id=str(user.id))
        minutes = max(1, math.ceil(wait_s / 60))
        await bot.send_message(chat_id, t("jira.too_many_attempts", lang, minutes=minutes))
        return
    try:
        result = await source.connect(credentials)
    except JiraCertificateUntrusted as exc:
        if exc.cert is None:
            await state.clear()
            log.warning(
                "jira_connect_rejected",
                user_id=str(user.id),
                reason=exc.error_code,
                detail=str(exc),
            )
            await bot.send_message(chat_id, error_text(exc, lang))
            return
        await state.set_state(JiraStates.cert)
        pending = {k: v for k, v in credentials.items() if k != "pinned_cert"}
        await state.update_data(pending=pending, cert=exc.cert.model_dump())
        text = cert_text(exc.cert, lang)
        if credentials.get("pinned_cert"):
            text = t("jira.cert_changed", lang, card=text)
        await bot.send_message(chat_id, text, reply_markup=cert_keyboard(exc.cert, lang))
        log.info("jira_connect_certificate_shown", user_id=str(user.id))
        return
    except TaskSourceUnavailable as exc:
        await state.clear()
        log.warning(
            "jira_connect_rejected",
            user_id=str(user.id),
            reason=exc.error_code,
            detail=str(exc),  # TaskSourceUnavailable messages never carry URLs or secrets
        )
        await bot.send_message(chat_id, error_text(exc, lang))
        return
    await state.clear()  # credentials leave the FSM data
    conn = await _save(session_factory, user, credentials, result)
    default_task_sources().invalidate(user.id)
    log.info(
        "task_source_connected",
        user_id=str(user.id),
        provider="jira",
        deployment=result.config.get("deployment"),
    )
    await bot.send_message(chat_id, t("jira.connected", lang))
    await tasks_command.show_containers(bot, user, conn, page=0, lang=lang)


async def _save(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    credentials: dict[str, Any],
    result: JiraConnectResult,
) -> TaskSourceConnection:
    secret_keys = ("email", "api_token") if result.config["deployment"] == "cloud" else ("pat",)
    stored_credentials = {k: credentials[k] for k in secret_keys if k in credentials}
    config = dict(result.config)
    if isinstance(credentials.get("pinned_cert"), dict):
        config["pinned_cert"] = credentials["pinned_cert"]
    async with session_factory() as session:
        previous = await tasks_conn_repo.get_connection_for_update(session, user.id)
        previous_config = (previous.config_json or {}) if previous is not None else {}
        same_account = (
            previous is not None
            and previous.provider == TaskProvider.jira
            and previous_config.get("site_url") == config.get("site_url")
            and (previous_config.get("identity") or {}).get("id") == result.identity.id
        )
        config["project_keys"] = (
            list(previous_config.get("project_keys") or []) if same_account else []
        )
        conn = await tasks_conn_repo.upsert_connection(
            session,
            user.id,
            provider=TaskProvider.jira,
            credentials=stored_credentials,
            config_json=config,
        )
        if not same_account:
            conn.sync_watermark = None
        await session.commit()
    return conn


async def cancel_input(message: Message, state: FSMContext, lang: Lang) -> None:
    await state.clear()
    await message.answer(t("settings.cancelled", lang))


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


def create_router(idle: StateFilter) -> Router:
    router = Router(name="jira_connect")
    router.message.register(cancel_input, Command("cancel"), StateFilter(JiraStates))
    text = F.text & ~F.text.startswith("/")
    router.message.register(site_input, StateFilter(JiraStates.site), text)
    router.message.register(email_input, StateFilter(JiraStates.email), text)
    router.message.register(secret_input, StateFilter(JiraStates.token, JiraStates.pat), text)
    router.callback_query.register(jira_menu, _action("jira"), idle)
    router.callback_query.register(
        deployment_choice, _action("jdep"), StateFilter(JiraStates.deployment)
    )
    router.callback_query.register(cert_trust, _action("jcert"))
    return router


__all__ = ["JiraStates", "create_router", "error_text"]
