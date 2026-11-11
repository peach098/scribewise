"""The shared Telegram chat picker (specs/007 research R1): one picker for every flow that lets
the user choose chats of their Telegram account. Setup uses it (purpose ``"setup"``), ➕ Add work
chats in ``/chats`` uses it (purpose ``"add"``, or ``"add_groups"`` with work account mode on),
and spec 008's exclusion picker in ``/chats`` (purpose ``"exclude"``, ``exclusions.py``).

How a flow uses it:

1. At import time, register a :class:`PickerPurpose`: its prompt key, what Done does (the
   *finisher*), what to say instead of an empty picker, and the command that retries after a
   Telegram failure (``register_purpose``).
2. Register the picker callbacks in the flow's router under the flow's own FSM state
   (:func:`register_picker_handlers`), so each flow keeps its own ``/cancel`` and command rules.
3. Open the picker with :func:`show_chat_picker`, passing the purpose name, the FSM state to
   enter, and the Telegram chat ids to leave out (``exclude``; for example the chats that are
   already work chats). The picker never decides on its own which chats to hide.

Screen: one toggle row per dialog (⬜ / ✅, titles cut at 40 characters), 8 per page with ◀️ / ▶️,
and **Done (N selected)**. Callbacks: ``pickchat:<telegram_chat_id>``, ``pickpage:<n>`` and
``pickdone:0``; they act only on the caller's FSM data (the snapshot of their own dialogs).

FSM data (data-model.md): ``{purpose, dialogs, selected, page, message_id}``. ``dialogs`` is the
snapshot ``[[telegram_chat_id, title, kind], ...]`` taken when the picker opened (excluded chats
already removed), ``selected`` the ticked chat ids, ``page`` the page shown, ``message_id`` the
picker message. For a purpose with ``single_live_message`` a tap on any other message (an older
picker) answers "This list is closed" and changes nothing (research R6); so does a tap when no
picker is open (:func:`picker_closed`, registered last without a state filter).

Work account mode (specs/008, contracts/control-chat.md):

- A purpose with ``work_account_switch`` (setup) shows a 💼 row on top (``pickwa:<on|off>``).
  With the mode on, a private dialog's tap toggles its *exclusion* (✅ included / 🚫 excluded;
  every private chat is a work chat by default), groups still toggle their selection, the prompt
  is ``work_account_prompt_key`` and Done reads "all private chats; excluded: N; groups: G".
  Turning the switch on drops private chats from the selection, turning it off drops the
  exclusions. FSM data gains ``work_account`` and ``excluded`` (Telegram chat ids).
- A purpose with ``exclusions_only`` (the /chats exclusion picker) marks every row ✅ / 🚫 and
  each tap toggles the exclusion; it opens with the chats already excluded (``excluded``).
- ``kinds`` keeps only dialogs of those kinds (private chats only for exclusions; groups only
  for ➕ while the mode is on). "Exclusion" here never means the ``exclude`` parameter, which
  leaves chats out of the list.

Usage limits (docs/plugins.md): under a ``max_work_chats`` limit (the ``limits`` resolver of
the workflow data) a tap that would tick more chats than may still be enabled answers
``chats.limit_reached``, and the 💼 switch cannot be turned on. With the default resolver
nothing is checked (no query).

:class:`AbandonedPickerMiddleware` closes an open picker when another command is sent, so the
command is answered as usual and the picker can no longer add anything (research R5).
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import ChatKind, User
from scribe.drafting.budget import LimitsResolver
from scribe.i18n import Lang, t
from scribe.logging import get_logger
from scribe.telegram.control_bot.limits import chat_slots, usage_of
from scribe.telegram.control_bot.render import button, format_callback, parse_callback
from scribe.telegram.gateway import Dialog, MessagingGateway

log = get_logger("scribe.control_bot")

PAGE_SIZE = 8
TITLE_CHARS = 40
# FSM data written before 007 carries no purpose: only setup had a picker then.
LEGACY_PURPOSE = "setup"
_KIND = {
    "private": ChatKind.private,
    "group": ChatKind.group,
    "channel_comments": ChatKind.channel_comments,
}

DialogRow = list[Any]  # [telegram_chat_id: int, title: str, kind: str], as stored in FSM data


def chat_kind(kind: str) -> ChatKind:
    """The ``work_chats.kind`` of a picker row's dialog kind (unknown kinds count as groups)."""
    return _KIND.get(kind, ChatKind.group)


# --- pure helpers -------------------------------------------------------------------------------


def available_dialogs(dialogs: Iterable[Dialog], exclude: Collection[int]) -> list[Dialog]:
    """``dialogs`` without the Telegram chat ids in ``exclude``, in their original order."""
    return [d for d in dialogs if d.chat_id not in exclude]


def picker_keyboard(
    dialogs: list[DialogRow],
    selected: set[int],
    page: int,
    lang: str,
    *,
    work_account: bool | None = None,
    excluded: Collection[int] = frozenset(),
    exclusions_only: bool = False,
) -> InlineKeyboardMarkup:
    """``work_account``: None = no 💼 switch row (every purpose but setup); False / True = the
    switch row and, when True, exclusion marks on private chats. ``exclusions_only``: every row
    is an exclusion toggle (the /chats exclusion picker)."""
    pages = max(1, (len(dialogs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    rows: list[list[InlineKeyboardButton]] = []
    if work_account is not None and not exclusions_only:
        key = "btn.work_account_switch_off" if work_account else "btn.work_account_switch_on"
        switch_to = "off" if work_account else "on"
        rows.append([button(t(key, lang), format_callback("pickwa", switch_to))])
    for chat_id, title, kind in dialogs[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        if exclusions_only or (work_account and kind == "private"):
            mark = "🚫" if chat_id in excluded else "✅"
        else:
            mark = "✅" if chat_id in selected else "⬜"
        label = title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1] + "…"
        rows.append([button(f"{mark} {label}", format_callback("pickchat", chat_id))])
    nav = []
    if page > 0:
        nav.append(button("◀️", format_callback("pickpage", page - 1)))
    if page < pages - 1:
        nav.append(button("▶️", format_callback("pickpage", page + 1)))
    if nav:
        rows.append(nav)
    if exclusions_only:
        done = t("btn.exclude_done", lang)
    elif work_account:
        private = {row[0] for row in dialogs if row[2] == "private"}
        groups = sum(1 for row in dialogs if row[2] != "private" and row[0] in selected)
        done = t(
            "btn.pick_done_work_account",
            lang,
            excluded=len(private & set(excluded)),
            groups=groups,
        )
    else:
        done = t("btn.pick_done", lang, n=len(selected))
    rows.append([button(done, format_callback("pickdone", 0))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- purposes -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PickResult:
    """What a finisher gets on Done. ``chosen`` are the selected snapshot rows, in picker order
    (possibly empty: the finisher decides whether that is allowed). ``services`` is the
    handler's workflow data (``session_factory``, ``settings``, ``clock``, ``llm_router``,
    ``gateway``, ...). The finisher answers ``callback`` and sets or clears the FSM state."""

    callback: CallbackQuery
    bot: Bot
    state: FSMContext
    user: User
    lang: Lang
    chosen: list[DialogRow]
    services: Mapping[str, Any]
    # specs/008: the 💼 switch's state (setup), the snapshot rows marked 🚫, every snapshot row.
    work_account: bool = False
    excluded: list[DialogRow] = field(default_factory=list)
    dialogs: list[DialogRow] = field(default_factory=list)


Finisher = Callable[[PickResult], Awaitable[None]]


@dataclass(frozen=True)
class PickerPurpose:
    """One use of the picker.

    - ``name``: stored as the FSM ``purpose``; Done calls this purpose's ``finish``.
    - ``prompt_key``: the i18n key of the text above the picker.
    - ``empty_key``: the text sent instead of opening a picker with no chats in it; ``None``
      opens the empty picker anyway (setup, as before 007).
    - ``retry_command``: the command named in the "couldn't reach Telegram" text.
    - ``single_live_message``: taps on any message but the latest picker are refused as closed.
      Setup keeps ``False``: its behaviour is unchanged by 007.
    - ``work_account_switch`` / ``work_account_prompt_key``: the 💼 row and the prompt while it
      is on (specs/008, setup). ``exclusions_only``: the /chats exclusion picker (specs/008).
    """

    name: str
    prompt_key: str
    finish: Finisher
    empty_key: str | None = None
    retry_command: str = "/start"
    single_live_message: bool = True
    work_account_switch: bool = False
    work_account_prompt_key: str | None = None
    exclusions_only: bool = False


PURPOSES: dict[str, PickerPurpose] = {}


def register_purpose(purpose: PickerPurpose) -> PickerPurpose:
    PURPOSES[purpose.name] = purpose
    return purpose


class PickerOutcome(enum.StrEnum):
    shown = "shown"  # the picker is open (FSM state ``picking_state``)
    empty = "empty"  # nothing left to pick: ``empty_key`` was sent, no state set
    unavailable = "unavailable"  # list_dialogs failed: the Telegram text was sent, state cleared


async def show_chat_picker(
    bot: Bot,
    chat_id: int,
    user: User,
    state: FSMContext,
    gateway: MessagingGateway,
    *,
    purpose: str,
    picking_state: State,
    exclude: Collection[int] = frozenset(),
    kinds: Collection[str] | None = None,
    excluded: Collection[int] = frozenset(),
) -> PickerOutcome:
    """List the user's dialogs, leave out the Telegram chat ids in ``exclude`` (and, with
    ``kinds``, dialogs of other kinds), and open the picker for ``purpose`` (a registered
    :class:`PickerPurpose`) in FSM state ``picking_state``; ``excluded`` are the chats marked
    🚫 at first (specs/008). Sends every message of the outcome itself."""
    spec = PURPOSES[purpose]
    lang = user.language
    try:
        dialogs = await gateway.list_dialogs(user.id)
    except Exception as exc:
        log.error(f"{purpose}_list_dialogs_failed", user_id=str(user.id), error=type(exc).__name__)
        await state.clear()
        problem = t("connect.telegram_unavailable", lang)
        await bot.send_message(
            chat_id,
            t("common.send_to_continue", lang, problem=problem, command=spec.retry_command),
        )
        return PickerOutcome.unavailable
    rows = [
        [d.chat_id, d.title, d.kind]
        for d in available_dialogs(dialogs, exclude)
        if kinds is None or d.kind in kinds
    ]
    if not rows and spec.empty_key is not None:
        await bot.send_message(chat_id, t(spec.empty_key, lang))
        return PickerOutcome.empty
    data: dict[str, Any] = {"purpose": purpose, "dialogs": rows, "selected": [], "page": 0}
    if spec.work_account_switch:
        data["work_account"] = False
    if spec.work_account_switch or spec.exclusions_only:
        listed = {row[0] for row in rows}
        data["excluded"] = sorted(c for c in excluded if c in listed)
    sent = await bot.send_message(
        chat_id, _prompt(spec, data, lang), reply_markup=_picker(data, lang, spec)
    )
    await state.set_state(picking_state)
    data["message_id"] = sent.message_id
    await state.set_data(data)
    return PickerOutcome.shown


# --- callbacks ----------------------------------------------------------------------------------


async def _live_purpose(
    callback: CallbackQuery, data: dict[str, Any], lang: str
) -> PickerPurpose | None:
    """The purpose of the open picker, or ``None`` (answered "closed") when the tap comes from
    another message than the open picker, or the picker's purpose is unknown."""
    spec = PURPOSES.get(str(data.get("purpose", LEGACY_PURPOSE)))
    message_id = data.get("message_id")
    stale = (
        spec is not None
        and spec.single_live_message
        and message_id is not None
        and (callback.message is None or callback.message.message_id != message_id)
    )
    if spec is None or stale:
        await callback.answer(t("alert.picker_closed", lang))
        return None
    return spec


def _picker(data: dict[str, Any], lang: str, spec: PickerPurpose) -> InlineKeyboardMarkup:
    return picker_keyboard(
        data["dialogs"],
        set(data["selected"]),
        data["page"],
        lang,
        work_account=bool(data.get("work_account")) if spec.work_account_switch else None,
        excluded=set(data.get("excluded", [])),
        exclusions_only=spec.exclusions_only,
    )


def _prompt(spec: PickerPurpose, data: dict[str, Any], lang: str) -> str:
    if data.get("work_account") and spec.work_account_prompt_key is not None:
        return t(spec.work_account_prompt_key, lang)
    return t(spec.prompt_key, lang)


async def pick_toggle(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    lang: Lang,
    user: User | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
    limits: LimitsResolver | None = None,
) -> None:
    data = await state.get_data()
    spec = await _live_purpose(callback, data, lang)
    if spec is None:
        return
    try:
        chat_id = int(parse_callback(callback.data).entity_id)
    except ValueError:
        await callback.answer()
        return
    # Only ids of the caller's own snapshot are accepted.
    kinds = {row[0]: row[2] for row in data.get("dialogs", [])}
    if chat_id not in kinds:
        await callback.answer()
        return
    if spec.exclusions_only or (data.get("work_account") and kinds[chat_id] == "private"):
        data["excluded"] = sorted(set(data.get("excluded", [])) ^ {chat_id})
    else:
        selected = set(data.get("selected", []))
        if chat_id not in selected and user is not None and session_factory is not None:
            limit, room = await chat_slots(session_factory, user, limits, settings)
            if limit is not None and len(selected) >= room:  # a usage limit (docs/plugins.md)
                await callback.answer(t("chats.limit_reached", lang, n=limit), show_alert=True)
                return
        data["selected"] = sorted(selected ^ {chat_id})
    await state.set_data(data)
    await callback.answer()
    if callback.message is not None:
        await bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=_picker(data, lang, spec),
        )


async def pick_work_account(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    lang: Lang,
    user: User | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
    limits: LimitsResolver | None = None,
) -> None:
    """``pickwa:<on|off>`` (specs/008): the 💼 switch of a purpose that has one."""
    data = await state.get_data()
    spec = await _live_purpose(callback, data, lang)
    if spec is None:
        return
    try:
        value = parse_callback(callback.data).entity_id
    except ValueError:
        value = ""
    if not spec.work_account_switch or value not in {"on", "off"}:
        await callback.answer()
        return
    on = value == "on"
    if on and user is not None and session_factory is not None:
        usage = await usage_of(session_factory, user, limits, settings)
        if usage.max_work_chats is not None:  # a usage limit (docs/plugins.md)
            await callback.answer(t("settings.work_account_unavailable", lang), show_alert=True)
            return
    kinds = {row[0]: row[2] for row in data.get("dialogs", [])}
    data["work_account"] = on
    if on:  # private chats are covered by the mode: only groups stay selected
        data["selected"] = [c for c in data.get("selected", []) if kinds.get(c) != "private"]
    else:
        data["excluded"] = []
    await state.set_data(data)
    await callback.answer()
    if callback.message is not None:
        await bot.edit_message_text(
            text=_prompt(spec, data, lang),
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=_picker(data, lang, spec),
        )


async def pick_page(callback: CallbackQuery, bot: Bot, state: FSMContext, lang: Lang) -> None:
    data = await state.get_data()
    spec = await _live_purpose(callback, data, lang)
    if spec is None:
        return
    try:
        data["page"] = int(parse_callback(callback.data).entity_id)
    except ValueError:
        await callback.answer()
        return
    await state.set_data(data)
    await callback.answer()
    if callback.message is not None:
        await bot.edit_message_text(
            text=_prompt(spec, data, lang),
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=_picker(data, lang, spec),
        )


async def pick_done(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    lang: Lang,
    **services: Any,
) -> None:
    data = await state.get_data()
    spec = await _live_purpose(callback, data, lang)
    if spec is None:
        return
    if user is None:
        await callback.answer()
        return
    selected = set(data.get("selected", []))
    excluded = set(data.get("excluded", []))
    dialogs = list(data.get("dialogs", []))
    work_account = bool(data.get("work_account"))
    if work_account:  # private chats are covered by the mode, never "selected"
        selected = {row[0] for row in dialogs if row[0] in selected and row[2] != "private"}
    chosen = [row for row in dialogs if row[0] in selected]
    marked = [
        row
        for row in dialogs
        if row[0] in excluded and (spec.exclusions_only or (work_account and row[2] == "private"))
    ]
    await spec.finish(
        PickResult(
            callback,
            bot,
            state,
            user,
            lang,
            chosen,
            services,
            work_account=work_account,
            excluded=marked,
            dialogs=dialogs,
        )
    )


async def picker_closed(callback: CallbackQuery, lang: Lang) -> None:
    """A picker button when no picker is open (it was finished, cancelled or abandoned)."""
    await callback.answer(t("alert.picker_closed", lang))


def register_picker_handlers(router: Router, picking: StateFilter) -> None:
    """The toggle, page and Done handlers, active only in the ``picking`` state(s)."""
    router.callback_query.register(pick_toggle, picking, F.data.startswith("pickchat:"))
    router.callback_query.register(pick_page, picking, F.data.startswith("pickpage:"))
    router.callback_query.register(pick_done, picking, F.data.startswith("pickdone:"))
    router.callback_query.register(pick_work_account, picking, F.data.startswith("pickwa:"))


def register_closed_handler(router: Router) -> None:
    """The catch-all for picker buttons outside any picker state. Register it last, after every
    router's state-bound picker handlers."""
    for action in ("pickchat", "pickpage", "pickdone", "pickwa"):
        router.callback_query.register(picker_closed, F.data.startswith(f"{action}:"))


# --- leaving the picker -------------------------------------------------------------------------


class AbandonedPickerMiddleware(BaseMiddleware):
    """Any command other than ``/cancel`` while a picker in one of ``states`` is open closes it
    first (nothing is added), then the command is handled as usual. Register it as a
    dispatcher-level ``message.outer_middleware`` so commands answered before the routers with
    the picker (``/help``, ``/start``) are covered too (research R5)."""

    def __init__(self, states: Collection[State]) -> None:
        self._states = frozenset(s.state for s in states)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        text = event.text if isinstance(event, Message) else None
        if (
            isinstance(state, FSMContext)
            and text is not None
            and text.startswith("/")
            and not text.startswith("/cancel")
            and await state.get_state() in self._states
        ):
            await state.clear()
        return await handler(event, data)


__all__ = [
    "PAGE_SIZE",
    "TITLE_CHARS",
    "AbandonedPickerMiddleware",
    "DialogRow",
    "Finisher",
    "PickResult",
    "PickerOutcome",
    "PickerPurpose",
    "available_dialogs",
    "chat_kind",
    "picker_keyboard",
    "register_closed_handler",
    "register_picker_handlers",
    "register_purpose",
    "show_chat_picker",
]
