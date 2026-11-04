"""US2 wiring for integration tests: the US1 harness plus manual-reply / read-elsewhere handling,
the outdated card, reminders and expiry. Synthetic data only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select

from scribe.db.models import (
    AutoSendSource,
    Contact,
    ConversationTurn,
    DraftDecision,
    DraftDecisionKind,
    DraftState,
    TurnState,
    User,
    WorkChat,
)
from scribe.db.repos import decisions as decisions_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import turns as turns_repo
from scribe.intake.events import ChatEventHandler
from scribe.scheduling.deadlines import register_review_deadlines
from scribe.sending.auto_send import AutoSendAdvisor, AutoSender
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
        # specs/014: the one scheduler step that can send, as in the service.
        self.advisor = AutoSendAdvisor(self.session_factory, self.bot, clock=self.clock)
        self.dispatcher["auto_send_advisor"] = self.advisor
        self.auto_sender = AutoSender(
            self.session_factory,
            self.gateway,
            self.presenter,
            clock=self.clock,
            marker=self.marker,
            commitments=None,
            advisor=self.advisor,
        )
        self.scheduler.on_auto_sends(self.auto_sender)

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


async def arm_chat(harness: US2Harness, chat: WorkChat) -> None:
    """Turn auto-send on for ``chat`` directly (specs/014 contracts/internal-interfaces.md §9)."""
    async with harness.session_factory() as session:
        stored = await session.get(WorkChat, chat.id)
        assert stored is not None
        stored.auto_send = True
        stored.auto_send_source = AutoSendSource.manual
        stored.auto_send_changed_at = harness.clock()
        await session.commit()


async def decision_rows(harness: US2Harness, chat: WorkChat) -> list[DraftDecision]:
    """The chat's ``draft_decisions`` rows, oldest first."""
    async with harness.session_factory() as session:
        result = await session.execute(
            select(DraftDecision)
            .where(DraftDecision.work_chat_id == chat.id)
            .order_by(DraftDecision.at, DraftDecision.created_at)
        )
        return list(result.scalars().all())


async def seed_decisions(
    harness: US2Harness,
    user: User,
    chat: WorkChat,
    *,
    unchanged: int = 0,
    edited: int = 0,
    rejected: int = 0,
    at: datetime | None = None,
) -> None:
    """``draft_decisions`` rows without drafts (the rate behind the offer), all at ``at``."""
    moment = at or harness.clock()
    async with harness.session_factory() as session:
        for kind, count in (
            (DraftDecisionKind.sent_unchanged, unchanged),
            (DraftDecisionKind.sent_edited, edited),
            (DraftDecisionKind.rejected, rejected),
        ):
            for _ in range(count):
                await decisions_repo.record(
                    session,
                    user_id=user.id,
                    work_chat_id=chat.id,
                    draft_id=None,
                    decision=kind,
                    during_auto_send=False,
                    at=moment,
                )
        await session.commit()


async def seed_auto_drafts(
    harness: US2Harness,
    user: User,
    chat: WorkChat,
    contact: Contact,
    outcomes: list[tuple[DraftDecisionKind, ...]],
) -> None:
    """One finished draft per entry of ``outcomes`` (oldest first), each with its decision rows
    written ``during_auto_send`` (the sample behind the suggestion to turn auto-send off)."""
    now = harness.clock()
    async with harness.session_factory() as session:
        for index, kinds in enumerate(outcomes):
            moment = now - timedelta(minutes=len(outcomes) - index)
            turn = await turns_repo.create_turn(
                session,
                user.id,
                work_chat_id=chat.id,
                contact_id=contact.id,
                first_message_id=None,
                arrived_at=moment,
                delay_s=0,
                due_at=moment,
            )
            turn.state = TurnState.rejected
            draft = await drafts_repo.create_draft(
                session,
                user.id,
                conversation_turn_id=turn.id,
                text="synthetic draft",
                output_json={},
                prepared_at=moment,
            )
            await drafts_repo.finalize(
                session, user.id, draft.id, DraftState.sent, at=moment, approved_by_user_at=moment
            )
            for kind in kinds:
                await decisions_repo.record(
                    session,
                    user_id=user.id,
                    work_chat_id=chat.id,
                    draft_id=draft.id,
                    decision=kind,
                    during_auto_send=True,
                    at=moment,
                )
        await session.commit()
