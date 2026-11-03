"""SC-009 (T043): 100 active users with 20 turns each, all due at the same instant, and a slow
LLM (2 s per call): every turn is drafted and none is read more than 60 s after its ``due_at``.

The run is real (Postgres, the real scheduler loop, real asyncio concurrency) but time is scaled:
the clock runs ``SPEED`` times faster than the wall clock, and the tick period and the fake LLM
latency are scaled down by the same factor. Database and CPU work are *not* scaled, so they
count ``SPEED`` times heavier against the 60 s budget than in production: the assertion is
conservative. Wall time is dominated by the CPU cost of composing 2000 drafts (~25 s).

004 T037: the same load with the reply gate enabled and half of the turns closing messages
("thanks!", pre-gate candidates that the classifier skips). The gate runs after mark_read, so
reading stays within 60 s of ``due_at``; every turn ends ``pending_review`` (questions, never
sent to the classifier) or ``skipped`` (no draft).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ChatKind,
    Contact,
    ContactKind,
    ConversationTurn,
    Message,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import users as users_repo
from scribe.drafting.composer import Composer
from scribe.drafting.reply_gate import ReplyGate
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.scheduling.scheduler import DueTurnsProcessor, Scheduler
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FROZEN_NOW, next_telegram_id
from tests.integration.us1_support import draft_output, reply_needed_output

USERS = 100
TURNS_PER_USER = 20
LLM_LATENCY_S = 2.0
TICK_S = 5.0
MAX_LATENESS = timedelta(seconds=60)
SPEED = 50  # virtual seconds per wall-clock second
WALL_TIMEOUT_S = 120


class ScaledClock:
    """``start + SPEED x (wall time elapsed)``: a clock that really advances, only faster."""

    def __init__(self, start: datetime, speed: float) -> None:
        self._start, self._speed = start, speed
        self._origin = time.monotonic()

    def __call__(self) -> datetime:
        return self._start + timedelta(seconds=(time.monotonic() - self._origin) * self._speed)


class TimedGateway(FakeMessagingGateway):
    """Records the (scaled) time of every mark_read."""

    def __init__(self, clock: ScaledClock) -> None:
        super().__init__()
        self.clock = clock
        self.read_at: dict[tuple[uuid.UUID, int, int], datetime] = {}

    async def mark_read(self, user_id: uuid.UUID, chat_id: int, up_to_message_id: int) -> None:
        await super().mark_read(user_id, chat_id, up_to_message_id)
        self.read_at[(user_id, chat_id, up_to_message_id)] = self.clock()


CLOSING_TEXT = "thanks!"


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    due_at: datetime,
    *,
    closing_share: int = 0,  # every n-th turn is a closing message (0: none)
) -> dict[tuple[uuid.UUID, int, int], datetime]:
    """100 active users x 20 private chats, one waiting turn each, all due at ``due_at``."""
    number = 0
    expected: dict[tuple[uuid.UUID, int, int], datetime] = {}
    async with session_factory() as session:
        for _ in range(USERS):
            user = await users_repo.create_user(
                session, control_chat_id=next_telegram_id(), settings=settings
            )
            user.state = UserState.active
            parents: list[object] = []
            messages: list[Message] = []
            turns: list[ConversationTurn] = []
            for _ in range(TURNS_PER_USER):
                peer = next_telegram_id()
                chat = WorkChat(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    telegram_chat_id=peer,
                    title="Synthetic chat",
                    kind=ChatKind.private,
                    enabled=True,
                )
                contact = Contact(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    kind=ContactKind.person,
                    telegram_peer_id=peer,
                    display_name="Synthetic Contact",
                )
                message = Message(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    work_chat_id=chat.id,
                    telegram_message_id=7,
                    sender_contact_id=contact.id,
                    text=(
                        CLOSING_TEXT
                        if closing_share and number % closing_share == 0
                        else "Synthetic question?"
                    ),
                    sent_at=due_at - timedelta(minutes=5),
                )
                turn = ConversationTurn(
                    user_id=user.id,
                    work_chat_id=chat.id,
                    contact_id=contact.id,
                    first_message_id=message.id,
                    last_message_id=message.id,
                    arrived_at=message.sent_at,
                    delay_s=300,
                    due_at=due_at,
                )
                number += 1
                parents += [chat, contact]
                messages.append(message)
                turns.append(turn)
                expected[(user.id, peer, 7)] = due_at
            for group in (parents, messages, turns):  # no ORM relationships: insert in FK order
                session.add_all(group)
                await session.flush()
        await session.commit()
    return expected


async def _count(session_factory: async_sessionmaker[AsyncSession], *states: TurnState) -> int:
    async with session_factory() as session:
        query = select(func.count()).where(ConversationTurn.state.in_(states))
        return int((await session.execute(query)).scalar_one())


async def test_scheduler_throughput(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
) -> None:
    await _run_throughput(committed_session_factory, settings, llm_router, fake_llm, gate=False)


async def test_scheduler_throughput_with_reply_gate(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
) -> None:
    """004 T037: half of the turns are closing messages that the gate skips."""
    await _run_throughput(committed_session_factory, settings, llm_router, fake_llm, gate=True)


async def _run_throughput(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    *,
    gate: bool,
) -> None:
    due_at = FROZEN_NOW
    expected = await _seed(session_factory, settings, due_at, closing_share=2 if gate else 0)
    assert len(expected) == USERS * TURNS_PER_USER
    closings = len(expected) // 2 if gate else 0

    clock = ScaledClock(due_at - timedelta(seconds=1), SPEED)
    fake_llm.latency_s = LLM_LATENCY_S / SPEED
    fake_llm.set_response("compose.draft", draft_output("Synthetic reply"))
    fake_llm.set_response("classify.reply_needed", reply_needed_output(False, "thanks", 0.95))
    gateway = TimedGateway(clock)
    scheduler = Scheduler(session_factory, tick_s=TICK_S / SPEED, clock=clock)
    scheduler.on_due_turns(
        DueTurnsProcessor(
            session_factory,
            gateway,
            Composer(session_factory, llm_router, settings, clock=clock),
            None,
            gate=ReplyGate(session_factory, llm_router, settings, clock=clock) if gate else None,
            clock=clock,
            read_concurrency=settings.read_concurrency,
            compose_concurrency=settings.compose_concurrency,
        )
    )

    stop = asyncio.Event()
    runner = asyncio.create_task(scheduler.run(stop))
    started = time.monotonic()
    try:
        while (await _count(session_factory, TurnState.pending_review, TurnState.skipped)) < len(
            expected
        ):
            assert time.monotonic() - started < WALL_TIMEOUT_S, "turns were not all drafted"
            await asyncio.sleep(0.2)
    finally:
        stop.set()
        await runner
        await scheduler.wait_idle()

    assert set(gateway.read_at) == set(expected)  # every turn read exactly where expected
    assert len(gateway.mark_read_calls) == len(expected)  # and only once
    lateness = [gateway.read_at[key] - due for key, due in expected.items()]
    assert max(lateness) <= MAX_LATENESS, f"read up to {max(lateness)} late"
    assert min(lateness) >= timedelta(0)  # never early
    assert len(fake_llm.calls_for("compose.draft")) == len(expected) - closings
    assert len(fake_llm.calls_for("classify.reply_needed")) == closings  # candidates only
    assert await _count(session_factory, TurnState.skipped) == closings
    assert await _count(session_factory, TurnState.pending_review) == len(expected) - closings
    async with session_factory() as session:
        users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        assert users == USERS
