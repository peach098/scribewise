"""Postgres-backed aiogram FSM storage (T020): persistence, encryption, restart survival."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import BotFsmState
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from tests.conftest import next_telegram_id
from tests.integration.bot_support import offline_bot, text_update


class EditFlow(StatesGroup):
    waiting_for_text = State()


def _key(chat_id: int) -> StorageKey:
    return StorageKey(bot_id=123456, chat_id=chat_id, user_id=chat_id)


async def test_state_and_data_round_trip_encrypted_and_cleared(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    storage = PostgresFSMStorage(session_factory)
    key = _key(next_telegram_id())
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}

    await storage.set_state(key, EditFlow.waiting_for_text)
    await storage.set_data(key, {"draft_id": "d1", "draft_text": "Synthetic draft text"})
    assert await storage.get_state(key) == "EditFlow:waiting_for_text"
    assert await storage.get_data(key) == {"draft_id": "d1", "draft_text": "Synthetic draft text"}
    assert await storage.update_data(key, {"step": 2}) == {
        "draft_id": "d1",
        "draft_text": "Synthetic draft text",
        "step": 2,
    }

    raw = (
        await db_session.execute(
            text("SELECT data FROM bot_fsm_state WHERE chat_id = :c"), {"c": key.chat_id}
        )
    ).scalar_one()
    assert raw.startswith("e2:v1:") and "Synthetic draft text" not in raw

    # FSMContext.clear() = set_state(None) + set_data({}) -> the row disappears.
    await storage.set_state(key, None)
    await storage.set_data(key, {})
    rows = await db_session.execute(select(BotFsmState).where(BotFsmState.chat_id == key.chat_id))
    assert rows.scalar_one_or_none() is None


async def test_state_survives_dispatcher_restart(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    chat_id = next_telegram_id()
    received: list[tuple[str | None, dict[str, object]]] = []

    def first_router() -> Router:
        router = Router()

        @router.message(Command("edit"))
        async def begin_edit(message: Message, state: FSMContext) -> None:
            await state.set_state(EditFlow.waiting_for_text)
            await state.update_data(draft_id="draft-123")

        return router

    def second_router() -> Router:
        router = Router()

        @router.message(StateFilter(EditFlow.waiting_for_text), F.text)
        async def finish_edit(message: Message, state: FSMContext) -> None:
            received.append((await state.get_state(), await state.get_data()))
            await state.clear()

        return router

    bot, _ = offline_bot()
    before = create_dispatcher(
        session_factory=session_factory,
        settings=settings,
        storage=PostgresFSMStorage(session_factory),
        routers=[first_router()],
    )
    await before.feed_update(bot, text_update(chat_id, "/edit"))
    await before.storage.close()

    # "Restart": brand-new dispatcher, storage instance and router; nothing kept in memory.
    after = create_dispatcher(
        session_factory=session_factory,
        settings=settings,
        storage=PostgresFSMStorage(session_factory),
        routers=[second_router()],
    )
    await after.feed_update(bot, text_update(chat_id, "My corrected synthetic reply"))

    assert received == [("EditFlow:waiting_for_text", {"draft_id": "draft-123"})]
    assert await after.storage.get_state(_key(chat_id)) is None
