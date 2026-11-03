"""``/commitments`` (FR-031; contracts/control-chat.md "Commitments").

Lists the caller's open commitments (what · to whom · due), one message each with
**✅ Resolve** ``cmtres:<id>``, **✏️ Edit** ``cmtedit:<id>`` and **🗑 Delete** ``cmtdel:<id>``.
Resolve and delete close the row (``closed_at`` set: the retention job removes it 30 days later
with its source quote). Edit asks for the new text, a new date, both as ``text; date``, or
``no date``. Every callback loads the commitment scoped to the calling user, and only an open one
can be changed. No message content is logged; the actions are logged as events (the
``activity_kind`` enum has no commitment kinds, and this release adds no enum values).

Jira (specs/003-jira-commitments J10, J11): an item names its source (``📎 Jira PAY-142
comment`` with the link, ``Telegram``, restricted) and a ``⚠️`` line when its issue is no longer
reachable. Below the open items, **Recently closed by Scribe** lists what the comment sync
closed by itself in the last 30 days (issue done, comment removed or changed) with **↩️ Reopen**
``cmtreopen:<id>``: only a row Scribe closed can be reopened, and it then counts as changed by
the user. Like every commitment callback it loads the row scoped to the calling user.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.context.commitments import date_only
from scribe.db.models import (
    Commitment,
    CommitmentAutoNote,
    CommitmentSourceKind,
    CommitmentState,
    User,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.i18n import Lang, LocalizedError, format_weekday_date, t
from scribe.logging import get_logger
from scribe.telegram.control_bot.render import (
    InvalidCallbackData,
    button,
    format_callback,
    keyboard,
    parse_callback,
)

log = get_logger("scribe.control_bot")

LIST_MAX = 20
QUOTE_CHARS = 80
_NO_DATE = {"no date", "без даты", "-", "none"}
REOPEN_WINDOW = timedelta(days=30)


class CommitmentStates(StatesGroup):
    edit = State()


class InvalidCommitmentEdit(LocalizedError):
    """An edit that cannot be applied; the message is shown to the user."""


def due_label(commitment: Commitment, lang: str) -> str:
    if commitment.due is None:
        return t("commitments.no_date", lang)
    return t("commitments.due", lang, day=format_weekday_date(commitment.due, lang))


def commitment_text(commitment: Commitment, contact_name: str, lang: str) -> str:
    line = t(
        "commitments.item",
        lang,
        what=commitment.what,
        contact=contact_name or t("commitments.someone", lang),
        due=due_label(commitment, lang),
    )
    if commitment.source_message_text and not commitment.restricted:
        quote = " ".join(commitment.source_message_text.split())
        if len(quote) > QUOTE_CHARS:
            quote = quote[: QUOTE_CHARS - 1].rstrip() + "…"
        line += f"\n› “{quote}”"
    source = source_line(commitment, lang)
    if source:
        line += f"\n{source}"
    if (
        commitment.state == CommitmentState.open
        and commitment.auto_note == CommitmentAutoNote.source_unreachable
    ):
        line += "\n" + t("commitments.unreachable", lang, key=commitment.jira_issue_key or "?")
    return line


def source_line(commitment: Commitment, lang: str) -> str | None:
    """Where the promise was found: Telegram, a Jira comment (with its link), or both."""
    parts: list[str] = []
    telegram = (commitment.source_kind or CommitmentSourceKind.telegram_message) == (
        CommitmentSourceKind.telegram_message
    )
    if telegram:
        parts.append(t("commitments.source_telegram", lang))
    if commitment.jira_comment_id and commitment.jira_issue_key:
        jira = t("commitments.source_jira", lang, key=commitment.jira_issue_key)
        if commitment.restricted:
            jira += " " + t("commitments.restricted", lang)
        if commitment.jira_comment_url:
            jira += f": {commitment.jira_comment_url}"
        parts.append(jira)
    elif commitment.jira_issue_key:
        parts.append(t("commitments.source_about", lang, key=commitment.jira_issue_key))
    if not parts or (len(parts) == 1 and telegram):
        return None  # the plain Telegram case: nothing to add
    return "📎 " + " · ".join(parts)


def closed_text(commitment: Commitment, contact_name: str, lang: str) -> str:
    text = t(
        "commitments.closed_item",
        lang,
        what=commitment.what,
        contact=contact_name or t("commitments.someone", lang),
        due=due_label(commitment, lang),
    )
    if commitment.auto_note is not None:
        note = t(f"commitments.note_{commitment.auto_note.value}", lang)
        text += "\n" + t("commitments.closed_by_scribe", lang, note=note)
    return text


def reopen_keyboard(commitment_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [[button(t("btn.commitment_reopen", lang), format_callback("cmtreopen", commitment_id))]]
    )


def commitment_keyboard(commitment_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button(t("btn.commitment_resolve", lang), format_callback("cmtres", commitment_id)),
                button(t("btn.edit", lang), format_callback("cmtedit", commitment_id)),
                button(t("btn.commitment_delete", lang), format_callback("cmtdel", commitment_id)),
            ]
        ]
    )


def parse_edit(text: str, today: Any) -> dict[str, Any]:
    """``{"what": ...}``, ``{"due": date}``, ``{"clear_due": True}`` or a combination."""
    raw = text.strip()
    if not raw:
        raise InvalidCommitmentEdit("err.commitment_empty")
    if raw.casefold() in _NO_DATE:
        return {"clear_due": True}
    if ";" in raw:
        what, _, when = (part.strip() for part in raw.partition(";"))
        values: dict[str, Any] = {}
        if what:
            values["what"] = what
        if when.casefold() in _NO_DATE:
            values["clear_due"] = True
        elif when:
            day = date_only(when, today)
            if day is None:
                raise InvalidCommitmentEdit("err.commitment_date")
            values["due"] = day
        if not values:
            raise InvalidCommitmentEdit("err.commitment_empty")
        return values
    day = date_only(raw, today)  # only when the whole input is a date
    if day is not None:
        return {"due": day}
    return {"what": " ".join(raw.split())}


async def _contact_name(session: AsyncSession, user_id: uuid.UUID, commitment: Commitment) -> str:
    """The Telegram contact's name, else the stored counterparty name (Jira, specs/003 J10)."""
    if commitment.contact_id is not None:
        contact = await chats_repo.get_contact(session, user_id, commitment.contact_id)
        if contact is not None:
            return contact.display_name
    return commitment.to_whom or ""


# --- handlers -----------------------------------------------------------------------------------


async def commitments_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    items: list[tuple[str, uuid.UUID]] = []
    closed: list[tuple[str, uuid.UUID]] = []
    async with session_factory() as session:
        open_items = await commitments_repo.list_open(session, user.id)
        for commitment in open_items[:LIST_MAX]:
            name = await _contact_name(session, user.id, commitment)
            items.append((commitment_text(commitment, name, lang), commitment.id))
        recent = await commitments_repo.list_recently_closed_by_system(
            session, user.id, since=clock() - REOPEN_WINDOW
        )
        for commitment in recent[:LIST_MAX]:
            name = await _contact_name(session, user.id, commitment)
            closed.append((closed_text(commitment, name, lang), commitment.id))
    if not items:
        await message.answer(t("commitments.none", lang))
    else:
        await message.answer(t("commitments.header", lang, n=len(open_items)))
    for text, commitment_id in items:
        await message.answer(text, reply_markup=commitment_keyboard(commitment_id, lang))
    if len(open_items) > LIST_MAX:
        await message.answer(t("waiting.more", lang, n=len(open_items) - LIST_MAX))
    if closed:
        await message.answer(t("commitments.recently_closed", lang))
        for text, commitment_id in closed:
            await message.answer(text, reply_markup=reopen_keyboard(commitment_id, lang))


async def _owned_open(
    callback: CallbackQuery,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: str,
) -> Commitment | None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return None
    try:
        commitment_id = uuid.UUID(parse_callback(callback.data).entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("commitments.not_open", lang))
        return None
    async with session_factory() as session:
        commitment = await commitments_repo.get_commitment(session, user.id, commitment_id)
    if commitment is None or commitment.state != CommitmentState.open:
        await callback.answer(t("commitments.not_open", lang))
        return None
    return commitment


async def _close(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    state: CommitmentState,
    lang: str,
) -> None:
    commitment = await _owned_open(callback, user, session_factory, lang)
    if commitment is None or user is None:
        return
    async with session_factory() as session:
        stored = await commitments_repo.get_commitment(session, user.id, commitment.id)
        if stored is None or stored.state != CommitmentState.open:
            await callback.answer(t("commitments.not_open", lang))
            return
        await commitments_repo.close(session, user.id, commitment.id, state, at=clock())
        name = await _contact_name(session, user.id, stored)
        await session.commit()
    resolved = state == CommitmentState.resolved
    log.info("commitment_closed", user_id=str(user.id), state=state.value)
    await callback.answer(t("alert.resolved" if resolved else "alert.deleted", lang))
    label = t(
        "commitments.resolved" if resolved else "commitments.deleted",
        lang,
        what=stored.what,
        contact=name or t("commitments.someone", lang),
    )
    if callback.message is not None:
        try:
            await bot.edit_message_text(
                text=label,
                chat_id=user.telegram_control_chat_id,
                message_id=callback.message.message_id,
            )
            return
        except Exception as exc:  # too old to edit: say it in a new message
            log.warning("commitment_edit_failed", error=type(exc).__name__)
    await bot.send_message(user.telegram_control_chat_id, label)


async def resolve_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    await _close(callback, bot, user, session_factory, clock, CommitmentState.resolved, lang)


async def delete_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    await _close(callback, bot, user, session_factory, clock, CommitmentState.deleted, lang)


async def edit_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    commitment = await _owned_open(callback, user, session_factory, lang)
    if commitment is None or user is None:
        return
    await state.set_state(CommitmentStates.edit)
    await state.set_data({"commitment_id": str(commitment.id)})
    await callback.answer()
    await bot.send_message(user.telegram_control_chat_id, t("commitments.edit_prompt", lang))


async def edit_input(
    message: Message,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock = utcnow,
) -> None:
    data = await state.get_data()
    if user is None:
        await state.clear()
        return
    lang = user.language
    today = clock().astimezone(ZoneInfo(user.timezone)).date()
    try:
        values = parse_edit(message.text or "", today)
    except InvalidCommitmentEdit as exc:
        await message.answer(t("common.try_again", lang, problem=exc.msg))
        return
    await state.clear()
    try:
        commitment_id = uuid.UUID(str(data.get("commitment_id")))
    except ValueError:
        await message.answer(t("commitments.not_open", lang))
        return
    async with session_factory() as session:
        stored = await commitments_repo.update_open(session, user.id, commitment_id, **values)
        if stored is None:
            await message.answer(t("commitments.not_open", lang))
            return
        name = await _contact_name(session, user.id, stored)
        await session.commit()
    log.info("commitment_edited", user_id=str(user.id), fields=sorted(values))
    await message.answer(
        t("settings.saved", lang, settings=commitment_text(stored, name, lang)),
        reply_markup=commitment_keyboard(stored.id, lang),
    )


async def reopen_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    """↩️ Reopen a commitment Scribe closed by itself (the caller's own rows only)."""
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        commitment_id = uuid.UUID(parse_callback(callback.data).entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("alert.not_found", lang))
        return
    async with session_factory() as session:
        reopened = await commitments_repo.reopen(session, user.id, commitment_id)
        if reopened is None:
            await callback.answer(t("alert.not_found", lang))
            return
        name = await _contact_name(session, user.id, reopened)
        await session.commit()
    log.info("commitment_reopened", user_id=str(user.id), commitment_id=str(commitment_id))
    await callback.answer(t("alert.reopened", lang))
    await bot.send_message(
        user.telegram_control_chat_id,
        t("commitments.reopened", lang, item=commitment_text(reopened, name, lang)),
        reply_markup=commitment_keyboard(reopened.id, lang),
    )


async def cancel_input(message: Message, state: FSMContext, lang: Lang) -> None:
    await state.clear()
    await message.answer(t("settings.cancelled", lang))


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


def create_router(idle: StateFilter) -> Router:
    """``idle``: the states in which control-chat commands answer (see ``settings.py``)."""
    router = Router(name="commitments")
    router.message.register(commitments_command, Command("commitments"), idle)
    router.message.register(cancel_input, Command("cancel"), StateFilter(CommitmentStates))
    router.message.register(
        edit_input, StateFilter(CommitmentStates.edit), F.text & ~F.text.startswith("/")
    )
    router.callback_query.register(resolve_callback, _action("cmtres"))
    # Outside other flows only: a stale button must not replace setup / login FSM data.
    router.callback_query.register(edit_callback, _action("cmtedit"), idle)
    router.callback_query.register(delete_callback, _action("cmtdel"))
    router.callback_query.register(reopen_callback, _action("cmtreopen"))
    return router
