"""``drafts`` rows, always scoped by ``user_id``.

State changes that end a draft go through :func:`finalize` so ``finalized_at`` (the purge key)
is always set. Sending is not done here: ``sending/send.py`` is the only sender.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from scribe.db.models import DRAFT_FINAL_STATES, ConversationTurn, Draft, DraftState

REMIND_AFTER = timedelta(hours=2)
EXPIRE_AFTER = timedelta(hours=24)


def agent_send(
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID | ColumnElement[uuid.UUID] | InstrumentedAttribute[uuid.UUID],
    telegram_message_id: int | ColumnElement[int] | InstrumentedAttribute[int],
) -> Exists:
    """``EXISTS``: the outgoing message ``telegram_message_id`` in ``work_chat_id`` was sent
    through the agent (a draft of a turn in that chat carries it as its sent message id), not
    written by hand. Values or the outer query's columns (e.g. ``Message.work_chat_id``,
    ``Message.telegram_message_id``): one ``EXISTS`` over ``drafts`` joined to its turn, which
    correlates to the outer row (no subquery of its own that could pull ``messages`` into a
    cross join). The one definition of "agent send" (intake, the grammatical gender guess)."""
    return (
        select(Draft.id)
        .join(ConversationTurn, Draft.conversation_turn_id == ConversationTurn.id)
        .where(
            Draft.user_id == user_id,
            Draft.sent_telegram_message_id == telegram_message_id,
            ConversationTurn.user_id == user_id,
            ConversationTurn.work_chat_id == work_chat_id,
        )
        .exists()
    )


async def create_draft(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    conversation_turn_id: uuid.UUID,
    text: str,
    output_json: dict[str, Any],
    prepared_at: datetime,
    version: int = 1,
    style_profile_id: uuid.UUID | None = None,
    prompt_version: str = "",
    llm_provider: str = "",
    llm_model: str = "",
    llm_usage_json: dict[str, Any] | None = None,
    remind_after: timedelta = REMIND_AFTER,
    expire_after: timedelta = EXPIRE_AFTER,
) -> Draft:
    draft = Draft(
        user_id=user_id,
        conversation_turn_id=conversation_turn_id,
        version=version,
        text=text,
        output_json=output_json,
        style_profile_id=style_profile_id,
        prompt_version=prompt_version,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_usage_json=llm_usage_json or {},
        state=DraftState.pending,
        prepared_at=prepared_at,
        remind_at=prepared_at + remind_after,
        expires_at=prepared_at + expire_after,
    )
    session.add(draft)
    await session.flush()
    return draft


async def get_draft(session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID) -> Draft | None:
    result = await session.execute(
        select(Draft).where(Draft.user_id == user_id, Draft.id == draft_id)
    )
    return result.scalar_one_or_none()


async def get_pending_for_turn(
    session: AsyncSession, user_id: uuid.UUID, turn_id: uuid.UUID
) -> Draft | None:
    result = await session.execute(
        select(Draft).where(
            Draft.user_id == user_id,
            Draft.conversation_turn_id == turn_id,
            Draft.state == DraftState.pending,
        )
    )
    return result.scalar_one_or_none()


async def get_by_control_message(
    session: AsyncSession, user_id: uuid.UUID, control_message_id: int
) -> Draft | None:
    """The draft whose card is ``control_message_id`` in the user's control chat."""
    result = await session.execute(
        select(Draft)
        .where(Draft.user_id == user_id, Draft.control_message_id == control_message_id)
        .order_by(Draft.prepared_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_pending_card(session: AsyncSession, user_id: uuid.UUID) -> Draft | None:
    """The pending draft whose card was posted last (the "active" card of the control chat)."""
    result = await session.execute(
        select(Draft)
        .where(
            Draft.user_id == user_id,
            Draft.state == DraftState.pending,
            Draft.control_message_id.is_not(None),
        )
        .order_by(Draft.control_message_id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def next_version(session: AsyncSession, user_id: uuid.UUID, turn_id: uuid.UUID) -> int:
    current = await session.scalar(
        select(func.max(Draft.version)).where(
            Draft.user_id == user_id, Draft.conversation_turn_id == turn_id
        )
    )
    return int(current or 0) + 1


async def update_draft(
    session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID, **values: Any
) -> Draft | None:
    draft = await get_draft(session, user_id, draft_id)
    if draft is None:
        return None
    for key, value in values.items():
        setattr(draft, key, value)
    await session.flush()
    return draft


async def finalize(
    session: AsyncSession,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    state: DraftState,
    *,
    at: datetime,
    **values: Any,
) -> Draft | None:
    """Move a draft to a final state and stamp ``finalized_at``."""
    if state not in DRAFT_FINAL_STATES:
        raise ValueError(f"{state} is not a final draft state")
    return await update_draft(session, user_id, draft_id, state=state, finalized_at=at, **values)
