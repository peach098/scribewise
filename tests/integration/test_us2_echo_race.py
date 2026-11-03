"""T047 race: Telethon echoes the agent's own approved send as an outgoing event *while*
``send.py`` still holds the draft row lock (before ``sent_telegram_message_id`` is committed).
The echo must not be mistaken for a manual reply: the draft is sent, the turn answered, nothing
dropped. Real commits on separate connections (the race needs two transactions)."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    ContactKind,
    ConversationTurn,
    DraftState,
    Message,
    TurnState,
    UserState,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import OutgoingEvent
from tests.conftest import FrozenClock, next_telegram_id
from tests.integration.bot_support import callback_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import US2Harness


class EchoingGateway(FakeMessagingGateway):
    """Like Telethon: the agent's own send comes back as an outgoing update, handled in its own
    task, before ``send_text`` has returned to ``send.py``."""

    def __init__(self, clock: FrozenClock) -> None:
        super().__init__()
        self.clock = clock
        self.echoes: list[asyncio.Task[None]] = []
        self.echo_done_before_send_returned: bool | None = None

    async def send_text(
        self, user_id: uuid.UUID, chat_id: int, text: str, reply_to: int | None, *, random_id: int
    ) -> int:
        message_id = await super().send_text(user_id, chat_id, text, reply_to, random_id=random_id)
        echo = OutgoingEvent(
            user_id=user_id, chat_id=chat_id, message_id=message_id, text=text, sent_at=self.clock()
        )
        task = asyncio.create_task(self.emit_outgoing(echo))
        self.echoes.append(task)
        await asyncio.sleep(0.3)  # the echo handler runs now and must wait for the draft lock
        self.echo_done_before_send_returned = task.done()
        return message_id


async def test_agent_send_echo_during_send_is_not_a_manual_reply(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
) -> None:
    session_factory = committed_session_factory
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    gateway = EchoingGateway(clock)
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)

    async with session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings
        )
        user.state = UserState.active
        peer = next_telegram_id()
        contact = await chats_repo.upsert_contact(
            session, user.id, telegram_peer_id=peer, kind=ContactKind.person, display_name="Anna"
        )
        chat = await chats_repo.upsert_work_chat(
            session, user.id, telegram_chat_id=peer, title="Anna", kind=ChatKind.private,
            enabled=True,
        )  # fmt: skip
        await session.commit()

    await harness.incoming(user, chat, contact, "Synthetic question?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending

    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    await asyncio.gather(*gateway.echoes)

    (sent,) = gateway.send_text_calls
    assert gateway.echo_done_before_send_returned is False  # the echo really waited on the lock
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.sent
    assert draft.sent_telegram_message_id == sent.message_id
    async with session_factory() as session:
        turn = await session.get(ConversationTurn, draft.conversation_turn_id)
        assert turn is not None and turn.state == TurnState.answered
        dropped = await session.scalar(
            select(func.count()).where(
                ActivityRecord.user_id == user.id, ActivityRecord.kind == ActivityKind.dropped
            )
        )
        assert dropped == 0
        stored = await session.scalar(
            select(func.count()).where(
                Message.work_chat_id == chat.id, Message.telegram_message_id == sent.message_id
            )
        )
        assert stored == 1  # the outgoing message is stored once (idempotent insert)
    assert not any("replied" in text for _, text in harness.notifications)

    # A genuine manual reply afterwards is still recognised (nothing open to drop now).
    await harness.manual_reply(user, chat)
    assert harness.notifications == []
