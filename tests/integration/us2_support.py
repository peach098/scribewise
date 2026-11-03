"""US2 wiring for integration tests: the US1 harness plus manual-reply / read-elsewhere handling,
the outdated card, reminders and expiry. Synthetic data only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select

from scribe.db.models import ConversationTurn, User, WorkChat
from scribe.intake.events import ChatEventHandler
from scribe.scheduling.deadlines import register_review_deadlines
from scribe.telegram.gateway import OutgoingEvent, ReadEvent
from tests.integration.us1_support import US1Harness, next_message_id


@dataclass
class US2Harness(US1Harness):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.events = ChatEventHandler(
            self.session_factory,
            clock=self.clock,
            notifier=self._notify,
            cards=self.presenter,
            gate=self.gate,
            marker=self.marker,
        )
        if self.marker is not None:
            self.gateway.on_unread_mark_cleared(self.marker.handle_unread_mark_cleared)
        self.gateway.on_outgoing(self.events.handle_outgoing)
        self.gateway.on_read_elsewhere(self.events.handle_read_elsewhere)
        self.intake.on_draft_outdated(self.presenter.show_outdated)
        register_review_deadlines(self.scheduler, self.presenter, clock=self.clock)

    async def manual_reply(
        self, user: User, chat: WorkChat, text: str = "Synthetic reply", **values: Any
    ) -> int:
        # Telegram message ids grow in one sequence per chat for both directions.
        message_id = next_message_id()
        await self.gateway.emit_outgoing(
            OutgoingEvent(
                user_id=user.id,
                chat_id=chat.telegram_chat_id,
                message_id=message_id,
                text=text,
                sent_at=self.clock(),
                **values,
            )
        )
        return message_id

    async def read_elsewhere(self, user: User, chat: WorkChat, max_message_id: int) -> None:
        await self.gateway.emit_read_elsewhere(
            ReadEvent(user_id=user.id, chat_id=chat.telegram_chat_id, max_message_id=max_message_id)
        )

    async def turns(self, user: User) -> list[ConversationTurn]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.user_id == user.id)
                .order_by(ConversationTurn.arrived_at, ConversationTurn.created_at)
            )
            return list(result.scalars().all())

    async def at(self, moment: datetime) -> None:
        """Move the clock to ``moment`` and run one scheduler tick."""
        self.clock.set(moment)
        await self.tick()

    def bot_texts(self) -> list[str]:
        return self.api.sent_texts

    def edits(self) -> list[EditMessageText]:
        return self.api.of_type(EditMessageText)

    def messages(self) -> list[SendMessage]:
        return self.api.of_type(SendMessage)


def callback_datas(markup: Any) -> list[str]:
    """All callback_data values of an inline keyboard (empty when there is none)."""
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def button_texts(markup: Any) -> list[str]:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]
