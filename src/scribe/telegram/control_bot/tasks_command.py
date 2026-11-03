"""``/tasks``: connect, reconnect or disconnect the task source and pick boards (FR-019, FR-021;
contracts/control-chat.md "Task source").

- Status: not connected / connected (number of boards) / ``error`` after repeated failures
  (``context/tasks/service.py``) with a reconnect hint.
- **🔌 Connect Trello** ``tasks:connect`` asks for the API key, then for a read-only token (the
  authorize link uses ``scope=read``, so it has to carry the key); both messages are deleted
  from the chat (best effort), and so is the bot's token prompt once the token step ends
  (token sent, /cancel, a key older than :data:`KEY_TTL`, any other command or button, see
  :class:`AbandonedConnectMiddleware`, or ``retention/pending_credentials.py`` once the step is
  untouched for :data:`KEY_TTL`). The key waits in the encrypted FSM data only until then.
  The pair is checked with ``TaskSource.validate`` before anything is stored; the connection
  keeps them in ``task_source_connections.credentials`` (encrypted) with
  ``config_json.board_ids`` and ``config_json.member_id`` (the Trello account). Reconnecting the
  same account keeps the board selection; another account starts with none.
- **📋 Boards** ``tasks:boards``: the user's open boards, paged (``tpage:<n>``), toggled with
  ``tboard:<board_id>`` (only ids Trello returned for these credentials), ``tboard:done``.
- **❌ Disconnect** ``tasks:disconnect`` removes the connection; drafts no longer use tasks.

Jira is the release version (the stub raises ``NotImplementedError``). The guided-setup steps
``tasks_connect`` / ``tasks_boards`` are deferred to T073 (setup polish): this command is the
whole Trello connection. The buttons that start a text step (``tasks:*``) answer only when no
other flow (setup, Telegram login) is active. Never logs the key or the token; the key is echoed
only inside the authorize link, and that message is deleted when the step ends.

Jira (specs/003-jira-commitments): **🔌 Connect Jira** ``jira:connect`` starts the flow in
``jira_connect.py``; the picker lists Jira projects (``config_json.project_keys``, at most
:data:`MAX_JIRA_PROJECTS`) instead of boards, and finishing it schedules the first comment scan.
Connecting one provider while the other is connected asks first (``tasks:replace`` /
``jira:replace``): the connection is replaced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.context.tasks.base import Container, TaskSourceUnavailable
from scribe.context.tasks.service import TaskSources, default_task_sources
from scribe.db.models import ConnectionStatus, TaskProvider, TaskSourceConnection, User
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.i18n import Lang, t
from scribe.logging import get_logger
from scribe.telegram.control_bot.render import (
    InvalidCallbackData,
    button,
    format_callback,
    keyboard,
    parse_callback,
)

log = get_logger("scribe.control_bot")

BOARDS_PER_PAGE = 8
KEY_TTL = timedelta(minutes=15)
MAX_JIRA_PROJECTS = 20


class TasksStates(StatesGroup):
    key = State()
    token = State()


# --- rendering ----------------------------------------------------------------------------------


def selection_key(conn: TaskSourceConnection) -> str:
    """Where the picked containers live in ``config_json`` (boards / Jira projects)."""
    return "project_keys" if conn.provider == TaskProvider.jira else "board_ids"


def _jira_name(conn: TaskSourceConnection, lang: str) -> str:
    config = conn.config_json or {}
    host = str(config.get("site_url") or "").removeprefix("https://")
    if config.get("deployment") == "self_hosted":
        version = config.get("server_version")
        suffix = f" · {version}" if version else ""
        return t("tasks.jira_name_self_hosted", lang, host=host, version=suffix)
    return t("tasks.jira_name_cloud", lang, host=host)


def status_text(conn: TaskSourceConnection | None, lang: str) -> str:
    lines = [t("tasks.title", lang)]
    if conn is None or conn.status == ConnectionStatus.disconnected:
        lines.append(t("tasks.not_connected_status", lang))
        return "\n".join(lines)
    picked = (conn.config_json or {}).get(selection_key(conn)) or []
    jira = conn.provider == TaskProvider.jira
    name = _jira_name(conn, lang) if jira else "Trello"
    if conn.status == ConnectionStatus.error:
        lines.append(t("tasks.status_error", lang, name=name))
    else:
        lines.append(t("tasks.status_connected", lang, name=name))
    if jira:
        lines.append(
            t("tasks.projects_selected", lang, keys=", ".join(str(k) for k in picked))
            if picked
            else t("tasks.projects_none", lang)
        )
    else:
        boards = len(picked)
        lines.append(
            t("tasks.boards_selected", lang, n=boards) if boards else t("tasks.boards_none", lang)
        )
    return "\n".join(lines)


def status_keyboard(conn: TaskSourceConnection | None, lang: str) -> InlineKeyboardMarkup:
    if conn is None or conn.status == ConnectionStatus.disconnected:
        return keyboard(
            [
                [
                    button(t("btn.tasks_connect", lang), format_callback("tasks", "connect")),
                    button(t("btn.jira_connect", lang), format_callback("jira", "connect")),
                ]
            ]
        )
    jira = conn.provider == TaskProvider.jira
    return keyboard(
        [
            [
                button(
                    t("btn.jira_projects" if jira else "btn.tasks_boards", lang),
                    format_callback("tasks", "boards"),
                ),
                button(
                    t("btn.tasks_reconnect", lang),
                    format_callback("jira" if jira else "tasks", "connect"),
                ),
            ],
            [button(t("btn.tasks_disconnect", lang), format_callback("tasks", "disconnect"))],
        ]
    )


def boards_keyboard(
    boards: list[Container], selected: set[str], page: int, lang: str
) -> InlineKeyboardMarkup:
    pages = max(1, (len(boards) + BOARDS_PER_PAGE - 1) // BOARDS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    shown = boards[page * BOARDS_PER_PAGE : (page + 1) * BOARDS_PER_PAGE]
    rows = [
        [
            button(
                (("✅ " if board.id in selected else "⬜ ") + board.name)[:60],
                format_callback("tboard", board.id),
            )
        ]
        for board in shown
    ]
    nav = []
    if page > 0:
        nav.append(button("◀️", format_callback("tpage", page - 1)))
    if page < pages - 1:
        nav.append(button("▶️", format_callback("tpage", page + 1)))
    if nav:
        rows.append(nav)
    rows.append([button(t("btn.done", lang), format_callback("tboard", "done"))])
    return keyboard(rows)


def _plausible(value: str) -> bool:
    return 16 <= len(value) <= 300 and not any(ch.isspace() for ch in value)


# --- helpers ------------------------------------------------------------------------------------


async def _connection(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> TaskSourceConnection | None:
    async with session_factory() as session:
        return await tasks_conn_repo.get_connection(session, user.id)


async def delete_quietly(bot: Bot, message: Message) -> None:
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception as exc:  # too old, no rights: the user can delete it themselves
        log.info("credential_message_not_deleted", error=type(exc).__name__)


async def _boards(sources: TaskSources, conn: TaskSourceConnection) -> list[Container]:
    source = sources.get(conn.provider.value)
    if conn.provider == TaskProvider.jira:  # the stored address / cloud id are needed too
        return await source.list_containers(
            {**(conn.config_json or {}), **(conn.credentials or {})}
        )
    return await source.list_containers(dict(conn.credentials or {}))


# --- handlers -----------------------------------------------------------------------------------


async def tasks_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    conn = await _connection(session_factory, user)
    await message.answer(status_text(conn, lang), reply_markup=status_keyboard(conn, lang))


async def tasks_menu(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
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
    if item == "connect":
        current = await _connection(session_factory, user)
        if (
            current is not None
            and current.provider == TaskProvider.jira
            and current.status != ConnectionStatus.disconnected
        ):
            await callback.answer()
            await bot.send_message(
                chat_id,
                t("tasks.replace_jira", lang),
                reply_markup=keyboard(
                    [
                        [
                            button(
                                t("btn.replace_with_trello", lang),
                                format_callback("tasks", "replace"),
                            ),
                            button(t("btn.cancel", lang), format_callback("jira", "cancel")),
                        ]
                    ]
                ),
            )
            return
    if item in {"connect", "replace"}:
        await state.set_state(TasksStates.key)
        await state.set_data({})
        await callback.answer()
        await bot.send_message(chat_id, t("tasks.key_prompt", lang))
        return
    conn = await _connection(session_factory, user)  # the caller's own connection only
    if conn is None:
        await callback.answer()
        await bot.send_message(chat_id, t("tasks.not_connected", lang))
        return
    if item == "disconnect":
        async with session_factory() as session:
            await tasks_conn_repo.delete_connection(session, user.id)
            await session.commit()
        default_task_sources().invalidate(user.id)
        log.info("task_source_disconnected", user_id=str(user.id), provider=conn.provider.value)
        await callback.answer(t("alert.tasks_disconnected", lang))
        jira = conn.provider == TaskProvider.jira
        await bot.send_message(
            chat_id,
            t("tasks.jira_disconnected" if jira else "tasks.disconnected", lang),
            reply_markup=status_keyboard(None, lang),
        )
        return
    if item == "boards":
        await callback.answer()
        await show_containers(bot, user, conn, page=0, lang=lang)


async def show_containers(
    bot: Bot, user: User, conn: TaskSourceConnection, *, page: int, lang: str
) -> None:
    """The board (Trello) or project (Jira) picker."""
    chat_id = user.telegram_control_chat_id
    jira = conn.provider == TaskProvider.jira
    try:
        boards = await _boards(default_task_sources(), conn)
    except TaskSourceUnavailable:
        text = t("jira.unavailable" if jira else "tasks.unavailable", lang)
        await bot.send_message(chat_id, text)
        return
    if not boards:
        text = t("tasks.no_projects" if jira else "tasks.no_boards", lang)
        await bot.send_message(chat_id, text)
        return
    selected = set((conn.config_json or {}).get(selection_key(conn)) or [])
    await bot.send_message(
        chat_id,
        t("tasks.pick_projects" if jira else "tasks.pick", lang),
        reply_markup=boards_keyboard(boards, selected, page, lang),
    )


async def _forget_key(bot: Bot, state: FSMContext) -> None:
    """End the credential step: delete the prompt that echoed the key, drop the FSM data."""
    data = await state.get_data()
    await state.clear()
    prompt_id, chat_id = data.get("prompt_id"), data.get("chat_id")
    if isinstance(prompt_id, int) and isinstance(chat_id, int):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=prompt_id)
        except Exception as exc:  # too old: the user can delete it themselves
            log.info("credential_message_not_deleted", error=type(exc).__name__)


async def key_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    key = (message.text or "").strip()
    await delete_quietly(bot, message)
    if user is None:
        await state.clear()
        return
    if not _plausible(key):
        await message.answer(t("tasks.invalid_input", lang))
        return
    await state.set_state(TasksStates.token)
    # Encrypted at rest (bot_fsm_state.data); removed when the token step ends.
    await state.set_data({"key": key, "key_at": clock().isoformat()})
    prompt = await message.answer(t("tasks.token_prompt", lang, key=key))
    await state.update_data(prompt_id=prompt.message_id, chat_id=prompt.chat.id)


def _key_expired(data: dict[str, Any], now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(str(data.get("key_at"))) > KEY_TTL
    except ValueError:
        return True


async def token_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    token = (message.text or "").strip()
    await delete_quietly(bot, message)
    data = await state.get_data()
    if user is None:
        await _forget_key(bot, state)
        return
    if _key_expired(data, clock()):
        await _forget_key(bot, state)
        await message.answer(t("tasks.expired", lang))
        return
    if not _plausible(token):
        await message.answer(t("tasks.invalid_input", lang))
        return
    key = data.get("key")
    await _forget_key(bot, state)  # the key leaves the FSM data whatever happens next
    if not isinstance(key, str) or not key:
        await message.answer(t("tasks.rejected", lang))
        return
    credentials = {"key": key, "token": token}
    sources = default_task_sources()
    try:
        identity = await sources.get("trello").validate(credentials)
    except TaskSourceUnavailable as exc:
        log.warning("task_source_connect_rejected", user_id=str(user.id), reason=str(exc))
        await message.answer(t("tasks.rejected", lang))
        return
    async with session_factory() as session:
        previous = await tasks_conn_repo.get_connection_for_update(session, user.id)
        same_account = (
            previous is not None
            and previous.provider == TaskProvider.trello
            and (previous.config_json or {}).get("member_id") == identity.id
        )
        # Another Trello account (or one connected before member_id was kept): its boards are
        # not this account's, so the selection starts empty.
        keep = (
            list((previous.config_json or {}).get("board_ids") or [])
            if previous is not None and same_account
            else []
        )
        conn = await tasks_conn_repo.upsert_connection(
            session,
            user.id,
            provider=TaskProvider.trello,
            credentials=credentials,
            config_json={"board_ids": keep, "member_id": identity.id},
        )
        await session.commit()
    sources.invalidate(user.id)
    log.info("task_source_connected", user_id=str(user.id), provider="trello")
    await message.answer(t("tasks.connected", lang))
    await show_containers(bot, user, conn, page=0, lang=lang)


async def board_toggle(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        data = parse_callback(callback.data)
    except InvalidCallbackData:
        await callback.answer()
        return
    conn = await _connection(session_factory, user)
    if conn is None:
        await callback.answer(t("tasks.not_connected", lang))
        return
    key = selection_key(conn)
    jira = conn.provider == TaskProvider.jira
    if data.action == "tboard" and data.entity_id == "done":
        if jira and not (conn.config_json or {}).get(key):
            await callback.answer(t("alert.no_projects", lang))
            return
        if jira:  # the first comment scan runs on the next sync tick
            async with session_factory() as session:
                await tasks_conn_repo.set_sync_state(session, user.id, next_sync_at=clock())
                await session.commit()
        await callback.answer(t("alert.saved", lang))
        await bot.send_message(
            user.telegram_control_chat_id,
            status_text(conn, lang),
            reply_markup=status_keyboard(conn, lang),
        )
        return
    try:
        boards = await _boards(default_task_sources(), conn)
    except TaskSourceUnavailable:
        await callback.answer(t("tasks.unavailable", lang))
        return
    selected = set((conn.config_json or {}).get(key) or [])
    page = 0
    if data.action == "tpage":
        try:
            page = int(data.entity_id)
        except ValueError:
            await callback.answer()
            return
    else:
        index = next((i for i, b in enumerate(boards) if b.id == data.entity_id), None)
        if index is None:  # not one of this account's boards
            await callback.answer(t("alert.unknown_board", lang))
            return
        if jira and data.entity_id not in selected and len(selected) >= MAX_JIRA_PROJECTS:
            await callback.answer(t("alert.too_many_projects", lang))
            return
        selected ^= {data.entity_id}
        page = index // BOARDS_PER_PAGE
        ordered = [b.id for b in boards if b.id in selected]
        async with session_factory() as session:
            stored = await tasks_conn_repo.get_connection_for_update(session, user.id)
            if stored is None:
                await callback.answer(t("tasks.not_connected", lang))
                return
            stored.config_json = {**(stored.config_json or {}), key: ordered}
            await session.commit()
        default_task_sources().invalidate(user.id)
        log.info("task_boards_changed", user_id=str(user.id), boards=len(ordered))
    await callback.answer()
    if callback.message is not None:
        await bot.edit_message_reply_markup(
            chat_id=user.telegram_control_chat_id,
            message_id=callback.message.message_id,
            reply_markup=boards_keyboard(boards, selected, page, lang),
        )


async def cancel_input(message: Message, bot: Bot, state: FSMContext, lang: Lang) -> None:
    await _forget_key(bot, state)
    await message.answer(t("settings.cancelled", lang))


TRELLO_FLOW_ACTIONS = frozenset({"tasks"})  # tasks:connect / tasks:replace restart the step
JIRA_FLOW_ACTIONS = frozenset({"jira", "jdep", "jcert"})  # menu, deployment, certificate card
JIRA_STATE_PREFIX = "JiraStates:"  # jira_connect.JiraStates (that module imports this one)


def _leaves_flow(event: TelegramObject, flow_actions: frozenset[str]) -> bool:
    """A command other than /cancel, or a button whose action is not one of the flow's."""
    if isinstance(event, CallbackQuery):
        return (event.data or "").split(":", 1)[0] not in flow_actions
    text = event.text if isinstance(event, Message) else None
    return text is not None and text.startswith("/") and not text.startswith("/cancel")


class AbandonedConnectMiddleware(BaseMiddleware):
    """Leaving a task-source connect flow drops the credentials it holds (issue #56).

    - Any command other than /cancel during the Trello key/token step ends that step first (the
      key is dropped from the FSM data and the prompt that echoed it is deleted); during the
      Jira flow it clears the FSM data (the email and token, or the card's pending ones).
    - So does any button outside the flow (an older **📦 Export**, a draft card, …); the flow's
      own buttons (:data:`TRELLO_FLOW_ACTIONS`, :data:`JIRA_FLOW_ACTIONS`, e.g. the certificate
      card's **Trust this certificate**) keep it.

    Registered as an outer middleware on the settings router for messages (it sees every
    control-chat message but /help) and on the dispatcher for callback queries (every button,
    whichever router answers it). Flows abandoned without any further input are purged by
    ``retention/pending_credentials.py``."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        if isinstance(state, FSMContext):
            current = str(await state.get_state() or "")
            bot = data.get("bot")
            notice: str | None = None
            if current in {TasksStates.key.state, TasksStates.token.state}:
                if _leaves_flow(event, TRELLO_FLOW_ACTIONS):
                    if isinstance(bot, Bot):
                        await _forget_key(bot, state)
                    else:
                        await state.clear()
                    notice = "tasks.connect_abandoned"
            elif current.startswith(JIRA_STATE_PREFIX) and _leaves_flow(event, JIRA_FLOW_ACTIONS):
                await state.clear()  # a Jira token (or the card's pending one) never lingers
                notice = "jira.connect_abandoned"
            if notice is not None and isinstance(bot, Bot):
                # Say so: a key pasted next would otherwise reach whatever handles plain text
                # (e.g. the values of a needs-info draft).
                await _tell_abandoned(bot, event, notice, data.get("lang"))
        return await handler(event, data)


async def _tell_abandoned(bot: Bot, event: TelegramObject, key: str, lang: object) -> None:
    chat_id: int | None = None
    if isinstance(event, Message):
        chat_id = event.chat.id
    elif isinstance(event, CallbackQuery):
        chat_id = event.message.chat.id if event.message is not None else event.from_user.id
    if chat_id is None:
        return
    try:
        await bot.send_message(chat_id, t(key, lang if isinstance(lang, str) else None))
    except Exception as exc:  # the notice must never block the button the user pressed
        log.warning("connect_abandoned_notice_failed", error=type(exc).__name__)


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


def create_router(idle: StateFilter) -> Router:
    """``idle``: the states in which control-chat commands answer (see ``settings.py``)."""
    router = Router(name="tasks")
    router.message.register(tasks_command, Command("tasks"), idle)
    router.message.register(cancel_input, Command("cancel"), StateFilter(TasksStates))
    text = F.text & ~F.text.startswith("/")
    router.message.register(key_input, StateFilter(TasksStates.key), text)
    router.message.register(token_input, StateFilter(TasksStates.token), text)
    # Outside other flows only: a stale button must not replace setup / login FSM data.
    router.callback_query.register(tasks_menu, _action("tasks"), idle)
    router.callback_query.register(board_toggle, _action("tboard"))
    router.callback_query.register(board_toggle, _action("tpage"))
    return router
