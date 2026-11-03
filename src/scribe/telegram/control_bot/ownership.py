"""Callback ownership check (T072, FR-029, SC-010): a button press acts only on the caller's rows.

:class:`OwnershipMiddleware` is an inner ``callback_query`` middleware. Before the handler runs it
parses ``<action>:<entity_id>[:<extra>]``; for an action that names a stored entity (a draft,
turn, style profile, rule proposal, profile refresh, commitment or work chat) it checks that the
row exists **and** belongs to the calling user, and otherwise answers the callback and stops.
Handlers still load every row scoped by ``user_id``; this is the second, uniform line of defence.
Callbacks without an entity id (``settings:delay``, ``setday:3``, ``tasks:boards``, Trello board
ids, the Jira connect buttons ``jira`` / ``jdep`` / ``jcert``, which act on the caller's own
FSM data only; specs/007 ➕ ``chatadd:0`` and the chat picker's ``pickchat`` / ``pickpage`` /
``pickdone``, which read and write only the caller's own work chats and picker FSM data) pass
through. Malformed data for an entity action (not
``<action>:<uuid>[:<extra>]``) is refused too: the check fails closed.

It is attached to the routers whose callbacks name stored rows: the settings router (and the
routers it includes) and the review router. The login, reconnect and disconnect buttons of the
connect flow name no row; ``connect_flow.ControlChatOwnerMiddleware`` (dispatcher-level) keeps
them to the owner's control chat. Refusals are logged with the action and the caller only;
never the callback's message.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.db.models import (
    ActivityRecord,
    Commitment,
    ConversationTurn,
    Draft,
    StyleProfile,
    StyleRuleProposal,
    User,
    WorkChat,
)
from scribe.i18n import Msg
from scribe.logging import get_logger
from scribe.telegram.control_bot.render import InvalidCallbackData, parse_callback

log = get_logger("scribe.control_bot")

OwnedModel = (
    type[Draft]
    | type[ConversationTurn]
    | type[StyleProfile]
    | type[StyleRuleProposal]
    | type[ActivityRecord]
    | type[Commitment]
    | type[WorkChat]
)

# action -> (table whose row the entity id names, answer when it is not the caller's)
ENTITY_ACTIONS: dict[str, tuple[OwnedModel, Msg]] = {
    **{
        a: (Draft, Msg("review.not_found"))
        for a in ("approve", "edit", "regen", "reject", "sources")
    },
    "readnow": (ConversationTurn, Msg("review.turn_not_waiting")),
    "draftany": (ConversationTurn, Msg("alert.not_found")),  # 004 /skipped
    "skipchat": (WorkChat, Msg("alert.not_found")),  # 004 /skipped
    **{
        a: (StyleProfile, Msg("profile.not_found"))
        for a in ("pin", "unpin", "removeprofile", "profrules", "profview")
    },
    "rule_accept": (StyleRuleProposal, Msg("alert.proposal_closed")),
    "rule_dismiss": (StyleRuleProposal, Msg("alert.proposal_closed")),
    "undo_refresh": (ActivityRecord, Msg("alert.nothing_to_undo")),
    **{a: (Commitment, Msg("commitments.not_open")) for a in ("cmtres", "cmtedit", "cmtdel")},
    "cmtreopen": (Commitment, Msg("alert.not_found")),  # specs/003: Reopen / Restore
    **{
        a: (WorkChat, Msg("alert.chat_not_found"))
        for a in (
            "chatcfg",
            "chatdelay",
            "chatlang",
            "chatformal",
            "chatgroup",  # legacy (006): stale buttons still get the ownership check
            "chatenable",
            "chatskip",
        )
    },
}
NOT_SET_UP = Msg("common.not_set_up")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


async def owns(
    session: AsyncSession, model: OwnedModel, user_id: uuid.UUID, entity_id: uuid.UUID
) -> bool:
    found = await session.execute(
        select(model.id).where(model.id == entity_id, model.user_id == user_id).limit(1)
    )
    return found.first() is not None


async def check_callback(
    data: str | None,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> Msg | None:
    """None when the callback may proceed, else the refusal answer (rendered by the caller)."""
    try:
        parsed = parse_callback(data)
    except InvalidCallbackData:
        # Malformed data naming an entity action is refused (fail closed); anything else is
        # not ours to judge: its handler answers it.
        known = ENTITY_ACTIONS.get((data or "").split(":", 1)[0])
        if known is None:
            return None
        log.warning("callback_refused_malformed", user_id=str(user.id) if user else None)
        return known[1]
    target = ENTITY_ACTIONS.get(parsed.action)
    if target is None:
        return None
    model, refusal = target
    if user is None:
        return NOT_SET_UP
    try:
        entity_id = uuid.UUID(parsed.entity_id)
    except ValueError:
        log.warning("callback_refused_malformed", action=parsed.action, user_id=str(user.id))
        return refusal
    async with session_factory() as session:
        if await owns(session, model, user.id, entity_id):
            return None
    log.warning("callback_refused_not_owner", action=parsed.action, user_id=str(user.id))
    return refusal


class OwnershipMiddleware(BaseMiddleware):
    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if isinstance(event, CallbackQuery):
            session_factory = data.get("session_factory")
            if session_factory is not None:
                refusal = await check_callback(event.data, data.get("user"), session_factory)
                if refusal is not None:
                    await event.answer(refusal.render(data.get("lang")))
                    return None
        return await handler(event, data)


__all__ = ["ENTITY_ACTIONS", "OwnershipMiddleware", "check_callback", "owns"]
