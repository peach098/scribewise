"""Drafts that use earlier conversations (specs/004 T016, T027): the filter decides, the fake
gateway answers the server-side search, the filter route ranks the matches, the draft cites them
and the card lists them. The fake LLM answers the first filter call and the ranking call
differently: the ranking request offers only ``earlier:*`` history. Synthetic data only."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context.earlier import EarlierSearch
from scribe.db.models import ChatKind, Contact, Draft, User, UserState, WorkChat
from scribe.drafting import composer as composer_mod
from scribe.drafting.stored_output import load_stored_output
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.aliases import is_earlier_alias
from scribe.llm.provider import DraftOutput, FilterContextOutput, HistoryItem, ProviderUnavailable
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import FoundMessage, GatewayBusy
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update
from tests.integration.earlier_support import (
    GROUP_CHAT_ID,
    PRIVATE_CHAT_ID,
    SENDER_PEER_ID,
    THIRD_PEER_ID,
    USER_PEER_ID,
    found,
    load_search_index,
    store_message,
)
from tests.integration.us1_support import draft_output
from tests.integration.us2_support import US2Harness
from tests.integration.us3_support import block

EARLIER_AT = datetime(2026, 5, 12, 9, 30, tzinfo=UTC)  # far outside the 30-day window
QUERIES = ["счёт 4521", "счет"]


# --- harness -------------------------------------------------------------------------------------

HarnessFactory = Callable[..., US2Harness]


@pytest.fixture
def make_harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> HarnessFactory:
    def _make(**overrides: Any) -> US2Harness:
        effective = settings.model_copy(update=overrides) if overrides else settings
        harness = US2Harness(session_factory, effective, llm_router, fake_llm, gateway, clock)
        harness.composer.earlier_search = EarlierSearch(gateway, effective, clock=clock)
        return harness

    return _make


@pytest.fixture
def harness(make_harness: HarnessFactory) -> US2Harness:
    return make_harness()


async def _private(
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact, **user_values: Any
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active, telegram_user_id=USER_PEER_ID, **user_values)
    contact = await make_contact(user, display_name="Anna Testova", telegram_peer_id=SENDER_PEER_ID)
    chat = await make_chat(user, title="Anna Testova", telegram_chat_id=PRIVATE_CHAT_ID)
    return user, chat, contact


async def _draft(
    harness: US2Harness,
    user: User,
    chat: WorkChat,
    contact: Contact,
    text: str,
    **values: Any,
) -> Draft:
    await harness.incoming(user, chat, contact, text, **values)
    await harness.tick()
    drafts = await harness.drafts(user)
    assert drafts, "no draft was prepared"
    return drafts[-1]


# --- fake LLM scripts ----------------------------------------------------------------------------


def _history_ids(call: FakeCall) -> list[str]:
    return [str(h["id"]) for h in block(call, "candidates")["history"]]


def is_rank(call: FakeCall) -> bool:
    ids = _history_ids(call)
    return bool(ids) and all(is_earlier_alias(i) for i in ids)  # e<n> in prompts (#71)


def rank_calls(fake_llm: FakeAdapter) -> list[FakeCall]:
    return [c for c in fake_llm.calls_for("retrieve.filter_context") if is_rank(c)]


def scripted_filter(
    queries: Sequence[str] = QUERIES,
    *,
    pick: Callable[[FakeCall], list[str]] | None = None,
) -> Callable[[FakeCall], FilterContextOutput]:
    """First call: select all recent context, say whether to search; ranking call: ``pick``."""

    def respond(call: FakeCall) -> FilterContextOutput:
        if is_rank(call):
            chosen = pick(call) if pick is not None else _history_ids(call)
            return FilterContextOutput(history_ids=chosen, task_ids=[], commitment_ids=[])
        candidates = block(call, "candidates")
        return FilterContextOutput(
            history_ids=[h["id"] for h in candidates["history"]],
            task_ids=[t["id"] for t in candidates["tasks"]],
            commitment_ids=[c["id"] for c in candidates["commitments"]],
            needs_earlier_context=bool(queries),
            search_queries=list(queries),
        )

    return respond


def draft_history(call: FakeCall) -> list[dict[str, Any]]:
    try:
        return list(block(call, "history"))
    except KeyError:
        return []


def earlier_in(call: FakeCall) -> list[dict[str, Any]]:
    return [h for h in draft_history(call) if is_earlier_alias(str(h["id"]))]


def cite_earlier(
    text: str, *, containing: str | None = None, **overrides: Any
) -> Callable[[FakeCall], DraftOutput]:
    """Cite the incoming message and the earlier message whose text contains ``containing``
    (the first offered one when None)."""

    def respond(call: FakeCall) -> DraftOutput:
        incoming = block(call, "incoming_messages")
        sources = [{"kind": "message", "id": incoming[0]["id"], "used_for": "the question"}]
        offered = [h for h in earlier_in(call) if containing is None or containing in h["text"]]
        if offered:
            sources.append(
                {"kind": "message", "id": offered[0]["id"], "used_for": "earlier agreement"}
            )
        return draft_output(text, sources=sources, **overrides)

    return respond


def all_llm_text(fake_llm: FakeAdapter) -> str:
    return "\n".join(b.content for c in fake_llm.calls for b in c.user_blocks)


def _warnings(draft: Draft) -> list[str]:
    return load_stored_output(draft.output_json, draft.text).system_warnings


def _output(draft: Draft) -> DraftOutput:
    output = load_stored_output(draft.output_json, draft.text).output
    assert output is not None
    return output


EARLIER_WARNINGS = ("earlier_search_unavailable", "earlier_search_no_match", "earlier_search_off")


def _many_private_matches(count: int) -> list[FoundMessage]:
    """``count`` private-chat hits one day apart, version ``n`` on day ``n``."""
    return [
        found(
            PRIVATE_CHAT_ID,
            f"счёт 4521 версия {n}",
            message_id=7000 + n,
            sent_at=EARLIER_AT + timedelta(days=n),
        )
        for n in range(count)
    ]


# --- (a) an agreement outside the recent window is found and cited -------------------------------


async def test_earlier_agreement_is_cited_and_stored(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(
        gateway,
        [
            found(
                PRIVATE_CHAT_ID,
                "Договорились: счёт 4521 на 120 000 ₽, оплата до конца месяца",
                message_id=4001,
                sent_at=EARLIER_AT,
            ),
            found(PRIVATE_CHAT_ID, "Счёт 4521 пока не нужен", message_id=3990, days_ago=200),
        ],
    )
    fake_llm.set_response(
        "retrieve.filter_context",
        scripted_filter(pick=lambda call: _history_ids(call)[:1]),  # newest first: 4001
    )
    fake_llm.set_response(
        "compose.draft", cite_earlier("Да, сумма 120 000 ₽, как договаривались", containing="120")
    )

    draft = await _draft(harness, user, chat, contact, "Какая там была сумма по счёту?")

    # Only enabled work chats of this user were searched, with the filter's queries.
    assert gateway.search_calls
    assert {chat_id for _, chat_id, _ in gateway.search_calls} == {PRIVATE_CHAT_ID}
    assert {q for _, _, q in gateway.search_calls} == set(QUERIES)
    (rank,) = rank_calls(fake_llm)
    assert block(rank, "candidates")["tasks"] == []
    # The ranked match is in the draft request after the recent history.
    (compose,) = fake_llm.calls_for("compose.draft")
    (offered,) = earlier_in(compose)
    assert "120 000" in offered["text"]

    output = _output(draft)
    assert is_earlier_alias(offered["id"])  # the prompt's short id, stored as the real ref (#71)
    assert [s.id for s in output.sources if s.id.startswith("earlier:")] == ["earlier:1"]
    assert not set(EARLIER_WARNINGS) & set(_warnings(draft))
    stored = load_stored_output(draft.output_json, draft.text)
    (entry,) = stored.earlier_messages  # only the cited one
    assert entry.ref == "earlier:1"
    assert (entry.chat_id, entry.message_id) == (PRIVATE_CHAT_ID, 4001)
    assert entry.chat_title == "Anna Testova"
    assert entry.sender == "Anna Testova"
    assert "120 000" in entry.excerpt and len(entry.excerpt) <= 200
    assert entry.sent_at == EARLIER_AT
    # Content is encrypted at rest.
    raw = json.dumps(draft.output_json, ensure_ascii=False)
    assert "120 000" not in raw and "Anna Testova" not in raw
    assert draft.llm_usage_json is not None and draft.llm_usage_json.get("rank") is not None


async def test_earlier_source_on_card_and_sources_view(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(
        gateway,
        [found(PRIVATE_CHAT_ID, "Счёт 4521: 120 000 ₽", message_id=4001, sent_at=EARLIER_AT)],
    )
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Да, 120 000 ₽"))
    draft = await _draft(harness, user, chat, contact, "Какая там была сумма по счёту?")

    cards = [m.text for m in harness.api.of_type(SendMessage) if "✍️ Draft" in m.text]
    assert "💬 Anna Testova · 12 May 2026" in cards[-1]
    await harness.feed(callback_update(user.telegram_control_chat_id, f"sources:{draft.id}"))
    assert any("«Счёт 4521: 120 000 ₽»" in text for text in harness.api.sent_texts)


# --- (b) no earlier context needed ---------------------------------------------------------------


async def test_no_search_when_not_needed(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "ok", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", scripted_filter(queries=[]))
    fake_llm.set_response("compose.draft", cite_earlier("Пожалуйста!"))

    draft = await _draft(harness, user, chat, contact, "Ок, спасибо")

    assert gateway.search_calls == []
    assert rank_calls(fake_llm) == []
    assert not set(EARLIER_WARNINGS) & set(_warnings(draft))
    assert load_stored_output(draft.output_json, draft.text).earlier_messages == []


# --- (c) search rate-limited ---------------------------------------------------------------------


async def test_rate_limited_search_drafts_from_recent_window(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    gateway.fail_search = GatewayBusy(120)
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Уточню сумму и вернусь"))

    draft = await _draft(harness, user, chat, contact, "Какая там была сумма по счёту?")

    assert len(gateway.search_calls) >= 1
    assert rank_calls(fake_llm) == []
    (compose,) = fake_llm.calls_for("compose.draft")
    assert earlier_in(compose) == []
    assert "earlier_search_unavailable" in _warnings(draft)


# --- (d) nothing found ---------------------------------------------------------------------------


async def test_no_match_keeps_placeholder(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Сумма была [[сумма по счёту]]"))

    draft = await _draft(harness, user, chat, contact, "Какая там была сумма по счёту?")

    assert gateway.search_calls
    assert rank_calls(fake_llm) == []
    warnings = _warnings(draft)
    assert "earlier_search_no_match" in warnings
    assert "earlier_search_unavailable" not in warnings
    assert _output(draft).needs_info


async def test_ranking_selects_nothing_is_no_match(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "счет за обед", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", scripted_filter(pick=lambda call: []))
    fake_llm.set_response("compose.draft", cite_earlier("Сумма была [[сумма]]"))

    draft = await _draft(harness, user, chat, contact, "Какая там была сумма по счёту?")

    assert len(rank_calls(fake_llm)) == 1
    (compose,) = fake_llm.calls_for("compose.draft")
    assert earlier_in(compose) == []
    assert "earlier_search_no_match" in _warnings(draft)


# --- (e) ranking fails ---------------------------------------------------------------------------


async def test_rank_failure_uses_newest_ten(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, _many_private_matches(12))
    first = scripted_filter()

    def respond(call: FakeCall) -> FilterContextOutput:
        if is_rank(call):
            raise ProviderUnavailable("synthetic outage")
        return first(call)

    fake_llm.set_response("retrieve.filter_context", respond)
    fake_llm.set_response("compose.draft", cite_earlier("Версия 11"))

    draft = await _draft(harness, user, chat, contact, "Какая последняя версия счёта?")

    compose = fake_llm.calls_for("compose.draft")[-1]
    offered = earlier_in(compose)
    assert len(offered) == 10
    texts = [h["text"] for h in offered]
    # The newest ten (versions 2..11), oldest first.
    assert texts == [f"счёт 4521 версия {n}" for n in range(2, 12)]
    warnings = _warnings(draft)
    assert "filter_unavailable" in warnings
    assert "earlier_search_unavailable" not in warnings


# --- (f) group linking ---------------------------------------------------------------------------


async def test_group_reply_linked_match_cited_unrelated_never_sent(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, telegram_user_id=USER_PEER_ID)
    anna = await make_contact(user, display_name="Anna Testova", telegram_peer_id=SENDER_PEER_ID)
    group = await make_chat(
        user,
        title="Synthetic team",
        kind=ChatKind.group,
        telegram_chat_id=GROUP_CHAT_ID,
    )
    question = found(
        GROUP_CHAT_ID, "Кто согласует бюджет проекта Гамма?", message_id=8001, sent_at=EARLIER_AT
    )
    approval = found(
        GROUP_CHAT_ID,
        "Бюджет проекта Гамма согласован: 300 000",
        message_id=8002,
        sender_peer_id=THIRD_PEER_ID,
        sender_display_name="Synthetic Manager",
        reply_to_id=8001,
        sent_at=EARLIER_AT + timedelta(hours=1),
    )
    unrelated = found(
        GROUP_CHAT_ID,
        "Бюджет на корпоратив секретный пароль 991",
        message_id=8003,
        sender_peer_id=THIRD_PEER_ID + 1,
        sender_display_name="Synthetic Stranger",
        sent_at=EARLIER_AT + timedelta(hours=2),
    )
    load_search_index(gateway, [question, approval, unrelated])
    fake_llm.set_response("retrieve.filter_context", scripted_filter(queries=["бюджет"]))
    fake_llm.set_response(
        "compose.draft", cite_earlier("Бюджет согласован, 300 000", containing="согласован:")
    )

    draft = await _draft(
        harness, user, group, anna, "Напомни, бюджет по Гамме согласовали?", mentions_me=True
    )

    assert {chat_id for _, chat_id, _ in gateway.search_calls} == {GROUP_CHAT_ID}
    assert "секретный пароль" not in all_llm_text(fake_llm)
    (compose,) = fake_llm.calls_for("compose.draft")
    cited_ids = {s.id for s in _output(draft).sources}
    approval_ref = next(h["id"] for h in earlier_in(compose) if "согласован:" in h["text"])
    assert is_earlier_alias(approval_ref)  # stored as its real ``earlier:<n>`` ref (#71)
    assert any(i.startswith("earlier:") for i in cited_ids)
    (entry,) = load_stored_output(draft.output_json, draft.text).earlier_messages
    assert entry.message_id == 8002
    assert entry.chat_title == "Synthetic team"
    assert entry.sender == "Synthetic Manager"


# --- (g) a new chat with no stored history -------------------------------------------------------


async def test_new_chat_still_runs_filter_and_search(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "счёт 4521 оплачен", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Да, оплачен"))

    await _draft(harness, user, chat, contact, "Счёт оплатили?")

    first = [c for c in fake_llm.calls_for("retrieve.filter_context") if not is_rank(c)]
    assert len(first) == 1
    assert block(first[0], "candidates")["history"] == []
    assert gateway.search_calls
    assert len(rank_calls(fake_llm)) == 1


# --- (h) 🔁 Regenerate reuses the ranked matches (issue #73) --------------------------------------


async def test_regenerate_reuses_ranked_matches_without_searching(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "счёт 4521 оплачен", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Да, оплачен"))
    v1 = await _draft(harness, user, chat, contact, "Счёт оплатили?")
    assert gateway.search_calls

    filter_calls = len(fake_llm.calls_for("retrieve.filter_context"))  # filter + rank
    gateway.reset_calls()
    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{v1.id}"))

    drafts = await harness.drafts(user)
    assert len(drafts) == 2
    assert gateway.search_calls == []  # fetched again by id, not searched
    assert gateway.get_calls
    assert len(fake_llm.calls_for("retrieve.filter_context")) == filter_calls  # no new ranking
    assert len(fake_llm.calls_for("compose.draft")) == 2
    assert load_stored_output(drafts[-1].output_json, drafts[-1].text).earlier_messages


# --- (i) an earlier id that was not offered ------------------------------------------------------


async def test_unoffered_earlier_id_regenerates_then_strips(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "счёт 4521 оплачен", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", scripted_filter())

    def respond(call: FakeCall) -> DraftOutput:
        incoming = block(call, "incoming_messages")
        sources = [
            {"kind": "message", "id": incoming[0]["id"], "used_for": "the question"},
            {"kind": "message", "id": "earlier:99", "used_for": "invented"},
        ]
        return draft_output("Да, оплачен", sources=sources)

    fake_llm.set_response("compose.draft", respond)

    draft = await _draft(harness, user, chat, contact, "Счёт оплатили?")

    assert len(fake_llm.calls_for("compose.draft")) == 2
    output = _output(draft)
    assert "earlier:99" not in {s.id for s in output.sources}
    assert output.needs_info
    assert "unsupported_sources" in _warnings(draft)
    assert load_stored_output(draft.output_json, draft.text).earlier_messages == []


async def test_offered_earlier_id_is_a_valid_source(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "счёт 4521 оплачен", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Да, оплачен"))

    draft = await _draft(harness, user, chat, contact, "Счёт оплатили?")

    assert len(fake_llm.calls_for("compose.draft")) == 1
    assert "unsupported_sources" not in _warnings(draft)


# --- (j) ranking slower than the remaining budget ------------------------------------------------


async def test_slow_ranking_falls_back_within_budget(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    llm_router: LLMRouter,
    monkeypatch: pytest.MonkeyPatch,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Scaled down: a 0.6 s total budget with a 0.1 s minimum stands for 10 s / 2 s; the search
    takes 0.3 s (stands for 6 s) and the ranking call never answers."""
    monkeypatch.setattr(composer_mod, "EARLIER_BUDGET_S", 0.6)
    monkeypatch.setattr(composer_mod, "MIN_RANK_S", 0.1)
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, _many_private_matches(12))
    gateway.search_delay_s = 0.15  # two queries, in parallel: ~0.15-0.3 s
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Версия 11"))

    async def blocked(
        incoming: Sequence[Any], matches: Sequence[HistoryItem], *, user_tier: Any, user_id: Any
    ) -> Any:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    monkeypatch.setattr(llm_router, "rank_earlier", blocked)

    started = time.monotonic()
    draft = await _draft(harness, user, chat, contact, "Какая последняя версия счёта?")
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    compose = fake_llm.calls_for("compose.draft")[-1]
    assert [h["text"] for h in earlier_in(compose)] == [
        f"счёт 4521 версия {n}" for n in range(2, 12)
    ]
    warnings = _warnings(draft)
    assert "earlier_search_unavailable" in warnings
    assert "filter_unavailable" not in warnings


# --- (k) the first filter call fails -------------------------------------------------------------


async def test_filter_failure_skips_search_with_both_warnings(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "счёт 4521 оплачен", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", ProviderUnavailable("synthetic outage"))
    fake_llm.set_response("compose.draft", cite_earlier("Уточню"))

    draft = await _draft(harness, user, chat, contact, "Счёт оплатили?")

    assert gateway.search_calls == []
    warnings = _warnings(draft)
    assert "filter_unavailable" in warnings
    assert "earlier_search_unavailable" in warnings


# --- T027: the user setting and the operator switch ----------------------------------------------


async def test_setting_off_with_needed_context_warns_off(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    log_capture: LogCapture,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(
        make_user, make_chat, make_contact, search_earlier_messages=False
    )
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "счёт 4521 оплачен", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Уточню"))
    # Some recent history so the filter call runs (with search off it is skipped otherwise).
    await store_message(
        db_session, user, chat, contact, sent_at=harness.clock() - timedelta(days=2)
    )

    draft = await _draft(harness, user, chat, contact, "Счёт оплатили?")

    assert gateway.search_calls == []
    assert rank_calls(fake_llm) == []
    assert "earlier_search_off" in _warnings(draft)
    events = [e for e in log_capture.events("earlier_search") if e.get("decision") == "off"]
    assert len(events) == 1


async def test_setting_off_without_needed_context_no_warning(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _private(
        make_user, make_chat, make_contact, search_earlier_messages=False
    )
    fake_llm.set_response("retrieve.filter_context", scripted_filter(queries=[]))
    fake_llm.set_response("compose.draft", cite_earlier("Пожалуйста"))

    draft = await _draft(harness, user, chat, contact, "Ок, спасибо")

    assert gateway.search_calls == []
    assert not set(EARLIER_WARNINGS) & set(_warnings(draft))


async def test_operator_switch_off_no_search_no_warning(
    make_harness: HarnessFactory,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    harness = make_harness(earlier_search_enabled=False)
    user, chat, contact = await _private(make_user, make_chat, make_contact)
    load_search_index(gateway, [found(PRIVATE_CHAT_ID, "счёт 4521 оплачен", sent_at=EARLIER_AT)])
    fake_llm.set_response("retrieve.filter_context", scripted_filter())
    fake_llm.set_response("compose.draft", cite_earlier("Уточню"))

    draft = await _draft(harness, user, chat, contact, "Счёт оплатили?")

    assert gateway.search_calls == []
    assert rank_calls(fake_llm) == []
    assert not set(EARLIER_WARNINGS) & set(_warnings(draft))
