"""aiogram FSM storage persisted in the ``bot_fsm_state`` table (research R2).

State and data survive restarts (mid-setup, mid-edit). ``data`` is an encrypted column because
it may hold draft text during an edit. The key is the control chat id: every control chat is a
private 1:1 chat with the one bot, so bot id, user id and thread are redundant.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.db.models import BotFsmState


def _state_name(state: StateType) -> str | None:
    if isinstance(state, State):
        return state.state
    return state


class PostgresFSMStorage(BaseStorage):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        name = _state_name(state)
        async with self._session_factory() as session:
            await session.execute(
                insert(BotFsmState)
                .values(chat_id=key.chat_id, state=name, data={})
                .on_conflict_do_update(
                    index_elements=[BotFsmState.chat_id],
                    set_={"state": name, "updated_at": func.now()},
                )
            )
            if name is None:
                await self._delete_if_empty(session, key.chat_id)
            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._get(key.chat_id)
        return row.state if row is not None else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        value = dict(data)
        async with self._session_factory() as session:
            statement = insert(BotFsmState).values(chat_id=key.chat_id, state=None, data=value)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[BotFsmState.chat_id],
                    set_={"data": statement.excluded.data, "updated_at": func.now()},
                )
            )
            if not value:
                await self._delete_if_empty(session, key.chat_id)
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._get(key.chat_id)
        return dict(row.data) if row is not None else {}

    async def close(self) -> None:
        return None

    async def _get(self, chat_id: int) -> BotFsmState | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(BotFsmState).where(BotFsmState.chat_id == chat_id)
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def _delete_if_empty(session: AsyncSession, chat_id: int) -> None:
        row = (
            await session.execute(select(BotFsmState).where(BotFsmState.chat_id == chat_id))
        ).scalar_one_or_none()
        if row is not None:
            await session.refresh(row)
            if row.state is None and not row.data:
                await session.execute(delete(BotFsmState).where(BotFsmState.chat_id == chat_id))
