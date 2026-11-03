"""Per-chat lock registry (#60): idle locks are evicted, live ones are shared."""

from __future__ import annotations

import asyncio
import gc
import uuid

from scribe.intake.locks import ChatLocks

USER = uuid.uuid4()


async def test_idle_locks_are_evicted() -> None:
    locks = ChatLocks()
    for chat_id in range(100):
        async with locks.for_chat(USER, chat_id):
            pass
    gc.collect()
    assert len(locks) == 0


async def test_a_held_lock_is_shared_and_serialises_its_chat() -> None:
    locks = ChatLocks()
    order: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with locks.for_chat(USER, 5):
            order.append("first in")
            entered.set()
            await release.wait()
            order.append("first out")

    async def second() -> None:
        await entered.wait()
        async with locks.for_chat(USER, 5):
            order.append("second in")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await entered.wait()
    await asyncio.sleep(0)
    gc.collect()  # a held lock with a waiter is not evicted
    assert len(locks) == 1
    assert locks.for_chat(USER, 5).locked()
    release.set()
    await asyncio.gather(*tasks)
    assert order == ["first in", "first out", "second in"]
    gc.collect()
    assert len(locks) == 0


async def test_chats_and_users_get_separate_locks() -> None:
    locks = ChatLocks()
    a = locks.for_chat(USER, 1)
    assert locks.for_chat(USER, 1) is a
    assert locks.for_chat(USER, 2) is not a
    assert locks.for_chat(uuid.uuid4(), 1) is not a
