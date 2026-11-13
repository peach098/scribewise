"""US1 wiring for integration tests: intake -> scheduler -> composer -> card -> review callbacks,
all against the Fake LLM, the Fake gateway and an offline Bot API session. Synthetic data only.
"""

from __future__ import annotations

import itertools
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Dispatcher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import OfferProvider
from scribe.config import Settings
from scribe.db.models import Contact, ConversationTurn, Draft, Message, User, WorkChat
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import (
    BudgetGuard,
    LimitsResolver,
    MarkupFactory,
    NoticeDecorator,
    SettingsLimitsResolver,
)
from scribe.drafting.composer import Composer
from scribe.drafting.reply_gate import ReplyGate
from scribe.i18n import Msg
from scribe.intake.router import IntakeRouter
from scribe.llm.adapters.fake import FakeAdapter, FakeCall, FakeResponse
from scribe.llm.provider import DraftOutput, FilterContextOutput, ReplyNeededOutput
from scribe.llm.registry import LLMRouter
from scribe.scheduling.scheduler import DueTurnsProcessor, Scheduler
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.control_bot.review import DraftPresenter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage
from scribe.telegram.unread_mark import UnreadMarker
from tests.conftest import FrozenClock
from tests.integration.bot_support import RecordingSession, offline_bot

_telegram_message_ids = itertools.count(100)


def next_message_id() -> int:
    """The next Telegram message id, shared by incoming and outgoing test messages."""
    return next(_telegram_message_ids)


def draft_output(text: str, **overrides: Any) -> DraftOutput:
    payload: dict[str, Any] = {
        "text": text,
        "language": "ru",
        "needs_info": False,
        "placeholders": [],
        "sources": [],
        "flags": {
            "creates_commitment": {"value": False, "what": None, "due": None},
            "sensitive": {"value": False, "categories": []},
        },
        "conflicts": [],
        "tone_shift_applied": "none",
        "context_warnings": [],
    }
    payload.update(overrides)
    return DraftOutput.model_validate(payload)


def reply_needed_output(
    reply_needed: bool,
    category: str = "none",
    confidence: float = 0.95,
    resolves_pending: bool = False,
    decided_by: str = "default",
) -> ReplyNeededOutput:
    return ReplyNeededOutput.model_validate(
        {
            "reply_needed": reply_needed,
            "category": category,
            "confidence": confidence,
            "resolves_pending": resolves_pending,
            "decided_by": decided_by,
        }
    )


def _block(call: FakeCall, name: str) -> Any:
    for block in call.user_blocks:
        if block.name == name:
            return json.loads(block.content)
    raise KeyError(name)


def grounded_reply(text: str, **overrides: Any) -> Callable[[FakeCall], DraftOutput]:
    """Fake compose response citing the first incoming message of the actual request."""

    def respond(call: FakeCall) -> DraftOutput:
        incoming = _block(call, "incoming_messages")
        sources = [{"kind": "message", "id": incoming[0]["id"], "used_for": "the question"}]
        return draft_output(text, sources=sources, **overrides)

    return respond


def select_all_history(call: FakeCall) -> FilterContextOutput:
    candidates = _block(call, "candidates")
    return FilterContextOutput(
        history_ids=[h["id"] for h in candidates["history"]],
        task_ids=[t["id"] for t in candidates["tasks"]],
        commitment_ids=[c["id"] for c in candidates["commitments"]],
    )


@dataclass
class US1Harness:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    llm_router: LLMRouter
    fake_llm: FakeAdapter
    gateway: FakeMessagingGateway
    clock: FrozenClock
    notifications: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    # specs/009: wire an UnreadMarker into the scheduler and the review callbacks.
    with_unread_marker: bool = False
    # Plugins (docs/plugins.md): per-user limits and the notice decorator, wired as in the service.
    limits: LimitsResolver | None = None
    decorate: NoticeDecorator | None = None
    offer_provider: OfferProvider | None = None
    notice_markups: list[MarkupFactory | None] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.marker = (
            UnreadMarker(self.session_factory, self.gateway, timeout_s=0.5)
            if self.with_unread_marker
            else None
        )
        # Default reply gate answer (004): "needs a reply", so short messages keep being drafted.
        self.set_reply_needed(reply_needed_output(True))
        self.bot: Bot
        self.api: RecordingSession
        self.bot, self.api = offline_bot()
        if self.limits is None:
            self.limits = SettingsLimitsResolver.from_settings(self.settings)
        self.gate = ReplyGate(
            self.session_factory, self.llm_router, self.settings, clock=self.clock,
            limits=self.limits,
        )  # fmt: skip
        self.intake = IntakeRouter(
            self.session_factory,
            self.settings,
            clock=self.clock,
            notifier=self._notify,
            gate=self.gate,
            limits=self.limits,
        )
        self.gateway.on_new_message(self.intake.handle_new_message)
        self.composer = Composer(
            self.session_factory, self.llm_router, self.settings, clock=self.clock,
            limits=self.limits,
        )  # fmt: skip
        self.presenter = DraftPresenter(self.bot, self.session_factory, clock=self.clock)
        # Issue #74, as in the service: the daily LLM hard cap skips turns with one notice.
        assert self.limits is not None
        self.budget = BudgetGuard(
            self.session_factory,
            self.limits,
            notifier=self._notify,
            clock=self.clock,
            decorate=self.decorate,
        )
        self.scheduler = Scheduler(self.session_factory, tick_s=5, clock=self.clock)
        self.scheduler.on_due_turns(
            DueTurnsProcessor(
                self.session_factory,
                self.gateway,
                self.composer,
                self.presenter,
                gate=self.gate,
                marker=self.marker,
                budget=self.budget,
                clock=self.clock,
                # All sessions share one test connection: one turn at a time.
                read_concurrency=1,
                compose_concurrency=1,
            )
        )
        self.dispatcher: Dispatcher = create_dispatcher(
            session_factory=self.session_factory,
            settings=self.settings,
            storage=PostgresFSMStorage(self.session_factory),
            llm_router=self.llm_router,
            gateway=self.gateway,
            clock=self.clock,
            unread_marker=self.marker,
            limits=self.limits,
            offer_provider=self.offer_provider,
        )

    def set_reply_needed(self, response: FakeResponse) -> None:
        """Override the ``classify.reply_needed`` answer (an output, an exception or a callable)."""
        self.fake_llm.set_response("classify.reply_needed", response)

    async def _notify(
        self, user_id: uuid.UUID, text: Msg, /, reply_markup: MarkupFactory | None = None
    ) -> None:
        # Like the service: rendered in the user's stored language (users.language).
        async with self.session_factory() as session:
            user = await users_repo.get_user(session, user_id)
        self.notifications.append((user_id, text.render(user.language if user else None)))
        self.notice_markups.append(reply_markup)

    async def incoming(
        self, user: User, chat: WorkChat, contact: Contact, text: str, **values: Any
    ) -> IncomingMessage:
        message = IncomingMessage(
            user_id=user.id,
            chat_id=chat.telegram_chat_id,
            message_id=values.pop("message_id", None) or next(_telegram_message_ids),
            sender_peer_id=contact.telegram_peer_id,
            sender_display_name=contact.display_name,
            text=text,
            has_media=values.pop("has_media", False),
            sent_at=values.pop("sent_at", None) or self.clock(),
            chat_kind=values.pop("chat_kind", chat.kind.value),
            chat_title=chat.title,
            **values,
        )
        await self.gateway.emit_new_message(message)
        return message

    async def tick(self) -> None:
        await self.scheduler.tick(drain=True)

    async def feed(self, update: Any) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def drafts(self, user: User) -> list[Draft]:
        """The user's drafts, oldest first, so ``[-1]`` is the newest one.

        ``version`` alone does not order them (every turn's first draft is version 1) and
        ``created_at`` cannot either: it is the database's ``now()``, which is the same for every
        row inside a test's outer transaction. Ties on such a key come back in whatever order
        Postgres finds the rows, which changes from run to run (issue #28). The test clock's
        ``prepared_at`` orders drafts made at different times; the turn's first Telegram message
        id (a counter in this harness) orders turns drafted at the same instant.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(Draft)
                .join(ConversationTurn, ConversationTurn.id == Draft.conversation_turn_id)
                .outerjoin(Message, Message.id == ConversationTurn.first_message_id)
                .where(Draft.user_id == user.id)
                .order_by(
                    Draft.prepared_at,
                    Draft.version,
                    Message.telegram_message_id,
                )
            )
            return list(result.scalars().all())
