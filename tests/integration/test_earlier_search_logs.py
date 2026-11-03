"""The ``earlier_search`` log event (specs/004 T017, research S10): exactly one per draft, counts
and timings only, never a query string or message text (FR-S05a, FR-S17). Synthetic data only."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context.earlier import EarlierSearch
from scribe.db.models import UserState
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.earlier_support import (
    PRIVATE_CHAT_ID,
    SENDER_PEER_ID,
    USER_PEER_ID,
    found,
    load_search_index,
)
from tests.integration.test_earlier_search_drafts import cite_earlier, scripted_filter
from tests.integration.us2_support import US2Harness

FIELDS = {
    "user_id",
    "turn_id",
    "decision",
    "outcome",
    "chats",
    "queries",
    "requests",
    "matches",
    "dropped_unlinked",
    "used",
    "duration_ms",
    "rank_ms",
    "total_ms",
}
QUERY_A = "квитанция zx7781"
QUERY_B = "накладная zx7781"
FOUND_TEXT = "Накладная zx7781 подписана, сумма 4242 у.е."
INCOMING_TEXT = "Что там было с накладной zx7781 и суммой?"
DRAFT_TEXT = "Подписана, сумма 4242 у.е."


async def test_one_event_per_draft_without_content(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    log_capture: LogCapture,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    harness.composer.earlier_search = EarlierSearch(gateway, settings, clock=clock)
    user = await make_user(state=UserState.active, telegram_user_id=USER_PEER_ID)
    contact = await make_contact(user, display_name="Anna Testova", telegram_peer_id=SENDER_PEER_ID)
    chat = await make_chat(user, title="Anna Testova", telegram_chat_id=PRIVATE_CHAT_ID)
    load_search_index(
        gateway,
        [found(PRIVATE_CHAT_ID, FOUND_TEXT, sent_at=datetime(2026, 5, 12, 9, 30, tzinfo=UTC))],
    )
    fake_llm.set_response("retrieve.filter_context", scripted_filter([QUERY_A, QUERY_B]))
    fake_llm.set_response("compose.draft", cite_earlier(DRAFT_TEXT))

    await harness.incoming(user, chat, contact, INCOMING_TEXT)
    await harness.tick()
    (draft,) = await harness.drafts(user)

    (event,) = log_capture.events("earlier_search")
    assert set(event) >= FIELDS
    assert event["user_id"] == str(user.id)
    assert event["turn_id"] == str(draft.conversation_turn_id)
    assert event["decision"] == "searched"
    assert event["outcome"] == "ok"
    # The two queries share "zx7781", which leads the search on its own (issue #108).
    assert (event["chats"], event["queries"], event["requests"]) == (1, 3, 3)
    assert event["matches"] >= 1 and event["used"] == 1
    assert event["dropped_unlinked"] == 0
    assert all(isinstance(event[k], int) for k in ("duration_ms", "rank_ms", "total_ms"))
    raw = log_capture.raw
    for secret in (QUERY_A, QUERY_B, "zx7781", FOUND_TEXT, INCOMING_TEXT, DRAFT_TEXT, "4242"):
        assert secret not in raw


async def test_not_needed_is_logged_once(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    log_capture: LogCapture,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    harness.composer.earlier_search = EarlierSearch(gateway, settings, clock=clock)
    user = await make_user(state=UserState.active, telegram_user_id=USER_PEER_ID)
    contact = await make_contact(user, display_name="Anna Testova", telegram_peer_id=SENDER_PEER_ID)
    chat = await make_chat(user, title="Anna Testova", telegram_chat_id=PRIVATE_CHAT_ID)
    fake_llm.set_response("retrieve.filter_context", scripted_filter([]))
    fake_llm.set_response("compose.draft", cite_earlier("Пожалуйста"))

    await harness.incoming(user, chat, contact, "Ок, спасибо")
    await harness.tick()

    (event,) = log_capture.events("earlier_search")
    assert event["decision"] == "not_needed"
    assert event["requests"] == 0 and event["used"] == 0
    assert gateway.search_calls == []
