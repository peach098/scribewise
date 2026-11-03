"""Issue #73 without a database: a stored selection replaces filter, earlier search and ranking
on 🔁 Regenerate when it still applies, and is recomputed when it does not; the stored form holds
identifiers only. Synthetic data only."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from scribe.config import Settings
from scribe.context.earlier import EarlierRef, EarlierSearch, SearchChat, SearchScope
from scribe.db.models import ChatKind
from scribe.drafting.composer import ComposeInput, compose_output
from scribe.drafting.selection import (
    StoredSelection,
    carried,
    regenerations,
    stored_selection,
    task_fingerprint,
)
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.aliases import is_earlier_alias
from scribe.llm.provider import (
    CommitmentRef,
    ContactInfo,
    DraftOutput,
    FilterContextOutput,
    HistoryItem,
    MessageItem,
    ProfileContext,
    ProviderUnavailable,
    TaskItem,
)
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import FoundMessage

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
CHAT_ID = 7_100_002
SENDER = 7_100_002
USER_PEER = 7_100_001
HISTORY = [
    HistoryItem(id=f"msg:h{i}", is_user=i % 2 == 0, sender="Anna", text=f"line {i}", sent_at=NOW)
    for i in range(4)
]
TASKS = [
    TaskItem(id="task:t1", title="Q3 report", status="Doing"),
    TaskItem(id="task:t2", title="Onboarding", status="To do"),
]
COMMITMENT = CommitmentRef(id="commit:c1", what="send the deck")


def _draft(text: str = "Synthetic reply.") -> DraftOutput:
    return DraftOutput.model_validate(
        {
            "text": text,
            "language": "en",
            "needs_info": False,
            "placeholders": [],
            "sources": [{"kind": "message", "id": "msg:in1", "used_for": "the question"}],
            "flags": {
                "creates_commitment": {"value": False, "what": None, "due": None},
                "sensitive": {"value": False, "categories": []},
            },
            "conflicts": [],
            "tone_shift_applied": "none",
            "context_warnings": [],
        }
    )


def _input(**values: Any) -> ComposeInput:
    fields: dict[str, Any] = {
        "incoming": [MessageItem(id="msg:in1", sender="Anna", text="where is the report?")],
        "history": list(HISTORY),
        "contact": ContactInfo(display_name="Anna", kind="person"),
        "profile": ProfileContext(),
        "tasks": list(TASKS),
        "commitments": [COMMITMENT],
        "timezone": "UTC",
    }
    return ComposeInput(**{**fields, **values})


def _select_some(fake_llm: FakeAdapter, **extra: Any) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(
            history_ids=["msg:h1", "msg:h3"],
            task_ids=["task:t2"],
            commitment_ids=["commit:c1"],
            **extra,
        ),
    )


def _versions(fake_llm: FakeAdapter) -> None:
    texts = iter(["First version.", "Second version.", "Third version."])
    fake_llm.set_response("compose.draft", lambda _call: _draft(next(texts)))


async def test_reuse_skips_the_filter_and_reruns_compose(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    _select_some(fake_llm)
    _versions(fake_llm)
    first = await compose_output(llm_router, _input())
    stored = first.stored_selection()
    assert stored is not None and first.reused is None
    assert stored.history_ids == ("msg:h1", "msg:h3")
    assert stored.task_fingerprints == (task_fingerprint("task:t2"),)
    assert "task:t2" not in str(stored.to_json())  # tracker ids never in the clear

    again = await compose_output(llm_router, _input(reuse=stored))

    assert len(fake_llm.calls_for("retrieve.filter_context")) == 1  # only the first draft
    assert len(fake_llm.calls_for("compose.draft")) == 2  # compose runs again
    assert again.output.text == "Second version." != first.output.text
    assert again.request.history == first.request.history
    assert again.request.tasks == first.request.tasks
    assert again.request.commitments == first.request.commitments
    assert again.reused is stored and again.stored_selection() is stored
    usage = again.usage_json()
    assert usage["filter"] is None and usage["selection_reused"] is True


async def test_stale_selection_is_recomputed(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    _select_some(fake_llm)
    _versions(fake_llm)
    stored = (await compose_output(llm_router, _input())).stored_selection()
    assert stored is not None

    # A selected history message left the window, a selected task was closed.
    await compose_output(llm_router, _input(reuse=stored, history=HISTORY[:2]))
    await compose_output(llm_router, _input(reuse=stored, tasks=TASKS[:1]))
    assert len(fake_llm.calls_for("retrieve.filter_context")) == 3  # first + both recomputed


async def test_no_selection_kept_after_a_filter_failure(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("retrieve.filter_context", ProviderUnavailable("synthetic outage"))
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(llm_router, _input())
    assert "filter_unavailable" in result.system_warnings
    assert result.stored_selection() is None  # the next regenerate tries the filter again


async def test_no_selection_without_candidates(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(llm_router, _input(history=[], tasks=[], commitments=[]))
    assert result.stored_selection() is None
    assert fake_llm.calls_for("retrieve.filter_context") == []


# --- earlier matches ------------------------------------------------------------------------------


def _found(message_id: int, text: str, days_ago: int) -> FoundMessage:
    return FoundMessage(
        chat_id=CHAT_ID,
        message_id=message_id,
        sender_peer_id=SENDER,
        sender_display_name="Anna",
        is_outgoing=False,
        text=text,
        sent_at=NOW - timedelta(days=days_ago),
    )


def _scope() -> SearchScope:
    return SearchScope(
        user_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        turn_chat_id=CHAT_ID,
        sender_peer_id=SENDER,
        user_peer_id=USER_PEER,
    )


def _search_input(**values: Any) -> ComposeInput:
    return _input(
        search_scope=_scope(),
        search_chats=[SearchChat(CHAT_ID, "Anna", ChatKind.private)],
        **values,
    )


def _is_rank(call: FakeCall) -> bool:
    import json

    block = next(json.loads(b.content) for b in call.user_blocks if b.name == "candidates")
    ids = [h["id"] for h in block["history"]]
    return bool(ids) and all(is_earlier_alias(i) for i in ids)  # e<n> in the prompt (#71)


def _search_script(fake_llm: FakeAdapter) -> None:
    def respond(call: FakeCall) -> FilterContextOutput:
        if _is_rank(call):
            return FilterContextOutput(history_ids=["e2"], task_ids=[], commitment_ids=[])
        return FilterContextOutput(
            history_ids=["msg:h1"],
            task_ids=[],
            commitment_ids=[],
            needs_earlier_context=True,
            search_queries=["invoice 4521"],
        )

    fake_llm.set_response("retrieve.filter_context", respond)


async def test_reuse_fetches_ranked_earlier_matches_by_id(
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    gateway.search_index[CHAT_ID] = [
        _found(901, "invoice 4521 is paid", 40),
        _found(902, "invoice 4521 was sent", 50),
    ]
    search = EarlierSearch(gateway, settings, clock=lambda: NOW)
    _search_script(fake_llm)
    _versions(fake_llm)
    first = await compose_output(llm_router, _search_input(), earlier_search=search)
    stored = first.stored_selection()
    assert stored is not None and stored.search
    assert [e.message_id for e in stored.earlier] == [902]  # the ranked one only
    assert len(fake_llm.calls_for("retrieve.filter_context")) == 2  # filter + rank
    gateway.reset_calls()

    again = await compose_output(llm_router, _search_input(reuse=stored), earlier_search=search)

    assert len(fake_llm.calls_for("retrieve.filter_context")) == 2  # no new filter or rank
    assert gateway.search_calls == []
    assert [ids for *_, ids in gateway.get_calls] == [(902,)]
    assert again.request.history == first.request.history
    assert [h.id for h in again.request.history] == ["msg:h1", "earlier:2"]


async def test_failed_refetch_or_search_switch_recomputes(
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    gateway.search_index[CHAT_ID] = [
        _found(901, "invoice 4521 is paid", 40),
        _found(902, "invoice 4521 was sent", 50),
    ]
    search = EarlierSearch(gateway, settings, clock=lambda: NOW)
    _search_script(fake_llm)
    _versions(fake_llm)
    stored = (
        await compose_output(llm_router, _search_input(), earlier_search=search)
    ).stored_selection()
    assert stored is not None
    calls = len(fake_llm.calls_for("retrieve.filter_context"))

    gateway.fail_search = RuntimeError("synthetic")
    await compose_output(llm_router, _search_input(reuse=stored), earlier_search=search)
    assert len(fake_llm.calls_for("retrieve.filter_context")) == calls + 1  # filter again
    gateway.fail_search = None

    off = await compose_output(
        llm_router, _search_input(reuse=stored, search_enabled=False), earlier_search=search
    )
    assert off.reused is None  # the search was switched off since: select afresh


# --- stored form ----------------------------------------------------------------------------------


def test_stored_selection_round_trip_and_malformed() -> None:
    selection = StoredSelection(
        history_ids=("msg:a",),
        task_fingerprints=("f" * 32,),
        commitment_ids=("commit:b",),
        earlier=(EarlierRef("earlier:1", -100, 5, "sender"),),
        warnings=("earlier_search_no_match",),
        search=True,
        last_message_id=str(uuid.uuid4()),
    )
    usage = {"selection": selection.to_json(), "regenerations": 2, "cost_usd": 0.1}
    assert stored_selection(usage) == selection
    assert regenerations(usage) == 2
    assert carried(usage) == {"selection": selection.to_json(), "regenerations": 2}
    bad_link = {
        **selection.to_json(),
        "earlier": [{**selection.to_json()["earlier"][0], "link": "x"}],
    }
    cases: list[dict[str, Any]] = [
        {},
        {"selection": "x"},
        {"selection": {"history": []}},
        {"selection": bad_link},
    ]
    for broken in cases:
        assert stored_selection(broken) is None
    assert regenerations({}) == 0 and regenerations({"regenerations": "3"}) == 0
    assert replace(selection, last_message_id=None).to_json()["last_message_id"] is None
