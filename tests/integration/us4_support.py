"""US4 wiring for integration tests: the US2 harness with inline commitment extraction, style
profile outputs for the fake ``profile.build`` route, profile rows and the user's own messages.
Synthetic data only."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    Contact,
    ProfileScope,
    ProfileState,
    StyleProfile,
    User,
    WorkChat,
)
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import CommitmentExtraction, StyleProfileOutput
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FROZEN_NOW, FrozenClock
from tests.integration.us1_support import next_message_id, select_all_history
from tests.integration.us2_support import US2Harness
from tests.integration.us3_support import inline_extractor


def rules(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "languages": ["ru"],
        "formality": 3,
        "typical_length_words": 20,
        "greetings": ["Привет"],
        "sign_offs": [],
        "emoji_usage": "rare",
        "punctuation_habits": [],
        "typical_phrases": [],
        "never_does": [],
    }
    base.update(overrides)
    return base


def style_output(summary: str = "Synthetic summary.", **rule_overrides: Any) -> StyleProfileOutput:
    return StyleProfileOutput.model_validate(
        {"rules_json": rules(**rule_overrides), "summary_text": summary,
         "chosen_example_indices": [0, 1]}
    )  # fmt: skip


def scope_of(call: FakeCall) -> str:
    for block in call.user_blocks:
        if block.name == "scope":
            return str(json.loads(block.content))
    raise KeyError("scope")


def by_scope(
    global_output: StyleProfileOutput, contact_output: StyleProfileOutput
) -> Callable[[FakeCall], StyleProfileOutput]:
    def respond(call: FakeCall) -> StyleProfileOutput:
        return global_output if scope_of(call) == "global" else contact_output

    return respond


def make_harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("extract.commitments", CommitmentExtraction(items=[]))
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    harness.composer.commitments = inline_extractor(session_factory, llm_router, clock)
    return harness


async def add_profile(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    *,
    contact: Contact | None = None,
    summary: str = "Synthetic summary.",
    user_rules: list[str] | None = None,
    version: int = 1,
    sample_size: int = 120,
    weak: bool = False,
    at: datetime = FROZEN_NOW,
    **rule_overrides: Any,
) -> StyleProfile:
    """A current profile row, created at ``at`` on the test clock (the refresh counts own
    messages written after it)."""
    async with session_factory() as session:
        profile = await profiles_repo.create_profile(
            session,
            user.id,
            scope=ProfileScope.global_ if contact is None else ProfileScope.contact,
            contact_id=contact.id if contact is not None else None,
            rules_json={**rules(**rule_overrides), "weak": weak},
            summary_text=summary,
            examples=["Synthetic example one", "Synthetic example two"],
            sample_size=sample_size,
            version=version,
            user_rules_json={"rules": [{"text": t, "source": "user"} for t in user_rules or []]},
        )
        profile.created_at = at
        await session.commit()
        return profile


async def add_own_messages(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    chat: WorkChat,
    count: int,
    *,
    at: datetime,
    text: str = "Synthetic own message",
) -> None:
    async with session_factory() as session:
        for n in range(count):
            await messages_repo.add_message(
                session,
                user.id,
                work_chat_id=chat.id,
                telegram_message_id=next_message_id(),
                text=f"{text} {n}",
                sent_at=at,
                is_outgoing=True,
            )
        await session.commit()


async def profiles_of(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    *,
    state: ProfileState | None = None,
    scope: ProfileScope | None = None,
) -> list[StyleProfile]:
    async with session_factory() as session:
        query = select(StyleProfile).where(StyleProfile.user_id == user.id)
        if state is not None:
            query = query.where(StyleProfile.state == state)
        if scope is not None:
            query = query.where(StyleProfile.scope == scope)
        result = await session.execute(query.order_by(StyleProfile.version))
        return list(result.scalars().all())


async def profile(
    session_factory: async_sessionmaker[AsyncSession], user: User, profile_id: uuid.UUID
) -> StyleProfile:
    async with session_factory() as session:
        found = await profiles_repo.get_profile(session, user.id, profile_id)
        assert found is not None
        return found
