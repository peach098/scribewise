"""Issue #71: the filter, rank and compose prompts carry short per-request ids (``h1``, ``t1``,
``c1``, ``e1``, ``m1``); the router maps the answer back to the real ids before validation, so
nothing downstream (stored drafts, ``llm_usage_json["selection"]``) ever sees an alias."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from scribe.drafting.composer import ComposeInput, compose_output, invalid_source_ids
from scribe.llm.adapters.fake import FakeAdapter, FakeCall, fingerprint_request
from scribe.llm.aliases import (
    alias_draft_request,
    alias_filter_request,
    is_alias,
    unalias_draft_output,
    unalias_filter_output,
)
from scribe.llm.provider import (
    CommitmentRef,
    ContactInfo,
    DraftOutput,
    DraftRequest,
    FilterCandidates,
    FilterContextOutput,
    FilterContextRequest,
    HistoryItem,
    MessageItem,
    ProfileContext,
    TaskItem,
)
from scribe.llm.registry import LLMRouter

ALIAS_STRING = re.compile(r'"[tchem][1-9][0-9]*"')
T0 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
UUIDS = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(10)]


def _history(n: int, prefix: str = "msg:") -> list[HistoryItem]:
    return [
        HistoryItem(
            id=f"{prefix}{UUIDS[i]}" if prefix == "msg:" else f"{prefix}{i + 1}",
            is_user=i % 2 == 0,
            sender="me" if i % 2 == 0 else "Synthetic Colleague",
            text=f"Synthetic history line {i}",
            sent_at=T0 - timedelta(minutes=n - i),
        )
        for i in range(n)
    ]


INCOMING = [MessageItem(id="msg:incoming-1", sender="Synthetic Colleague", text="Status?")]
TASKS = [
    TaskItem(
        id="task:trello:card-a", title="Synthetic report", status="Doing", due=date(2026, 10, 16)
    ),
    TaskItem(id="task:jira:SYN-2", title="Synthetic review", status="To do"),
]
COMMITMENTS = [CommitmentRef(id=f"commit:{UUIDS[9]}", what="send the deck", due=date(2026, 10, 17))]


def _filter_request(history: list[HistoryItem] | None = None) -> FilterContextRequest:
    return FilterContextRequest(
        incoming_messages=INCOMING,
        candidates=FilterCandidates(
            history=_history(3) if history is None else history,
            tasks=TASKS,
            commitments=COMMITMENTS,
        ),
    )


def _draft_request(**values: Any) -> DraftRequest:
    fields: dict[str, Any] = {
        "incoming_messages": INCOMING,
        "history": [*_history(2), *_history(2, prefix="earlier:")],
        "contact": ContactInfo(display_name="Synthetic Colleague", kind="person"),
        "tasks": TASKS,
        "commitments": COMMITMENTS,
        "today": date(2026, 10, 14),
        "timezone": "UTC",
    }
    fields.update(values)
    return DraftRequest(**fields)


def _draft(
    sources: list[tuple[str, str]], conflicts: list[tuple[str, str]] | None = None
) -> DraftOutput:
    return DraftOutput.model_validate(
        {
            "text": "Synthetic reply.",
            "language": "en",
            "needs_info": False,
            "placeholders": [],
            "sources": [{"kind": k, "id": i, "used_for": "x"} for k, i in sources],
            "flags": {
                "creates_commitment": {"value": False, "what": None, "due": None},
                "sensitive": {"value": False, "categories": []},
            },
            "conflicts": [
                {"with_kind": k, "with_id": i, "description": "clash"} for k, i in conflicts or []
            ],
            "tone_shift_applied": "none",
            "context_warnings": [],
        }
    )


def _payload(call: FakeCall) -> str:
    return "".join(b.content for b in call.user_blocks)


# --- mapping ------------------------------------------------------------------------------------


def test_filter_request_ids_become_short_aliases_and_map_back() -> None:
    real = _filter_request()
    sent, aliases = alias_filter_request(real)
    candidates = sent.candidates
    assert [h.id for h in candidates.history] == ["h1", "h2", "h3"]  # oldest first
    assert [t.id for t in candidates.tasks] == ["t1", "t2"]
    assert [c.id for c in candidates.commitments] == ["c1"]
    assert [m.id for m in sent.incoming_messages] == ["m1"]
    # Everything but the ids is untouched, and the original request is not mutated.
    assert [h.text for h in candidates.history] == [h.text for h in real.candidates.history]
    assert real.candidates.history[0].id.startswith("msg:")
    blocks = "".join(b.content for b in sent.to_blocks())
    assert "msg:" not in blocks and "task:" not in blocks and "commit:" not in blocks

    answer = FilterContextOutput(history_ids=["h3", "h1"], task_ids=["t2"], commitment_ids=["c1"])
    back = unalias_filter_output(answer, aliases)
    assert back.history_ids == [real.candidates.history[2].id, real.candidates.history[0].id]
    assert back.task_ids == ["task:jira:SYN-2"]
    assert back.commitment_ids == [COMMITMENTS[0].id]


def test_draft_request_aliases_earlier_messages_apart() -> None:
    real = _draft_request()
    sent, aliases = alias_draft_request(real)
    assert [h.id for h in sent.history] == ["h1", "h2", "e1", "e2"]
    assert [m.id for m in sent.incoming_messages] == ["m1"]
    assert sent.calendar == real.calendar and sent.profile == real.profile
    output = _draft(
        [("message", "m1"), ("message", "e2"), ("task", "t1")], conflicts=[("commitment", "c1")]
    )
    back = unalias_draft_output(output, aliases)
    assert [s.id for s in back.sources] == ["msg:incoming-1", "earlier:2", "task:trello:card-a"]
    assert back.conflicts[0].with_id == COMMITMENTS[0].id
    assert invalid_source_ids(back, real) == []


def test_numbering_of_a_block_ignores_the_other_blocks() -> None:
    """One counter per kind: more incoming messages or tasks do not renumber the history."""
    one = alias_filter_request(_filter_request())[0]
    more = _filter_request().model_copy(
        update={"incoming_messages": [*INCOMING, INCOMING[0].model_copy(update={"id": "msg:x"})]}
    )
    two = alias_filter_request(more)[0]
    assert [h.id for h in one.candidates.history] == [h.id for h in two.candidates.history]


def test_ids_that_already_look_like_aliases_go_out_unchanged() -> None:
    """No ambiguous mapping: a request whose real ids look like aliases is sent as is."""
    req = FilterContextRequest(
        incoming_messages=[MessageItem(id="m1", sender="S", text="hi")],
        candidates=FilterCandidates(tasks=[TaskItem(id="t2", title="x", status="y")]),
    )
    sent, aliases = alias_filter_request(req)
    assert sent is req and len(aliases) == 0
    answer = FilterContextOutput(history_ids=[], task_ids=["t2"], commitment_ids=[])
    assert unalias_filter_output(answer, aliases).task_ids == ["t2"]


def test_alias_pattern() -> None:
    assert all(is_alias(a) for a in ["h1", "t12", "c3", "e40", "m2"])
    assert not any(is_alias(a) for a in ["h0", "x1", "msg:h1", "earlier:1", "h", "h1a"])


def test_fake_fingerprint_matches_what_the_router_sends() -> None:
    req = _draft_request()
    assert fingerprint_request(req) == fingerprint_request(alias_draft_request(req)[0])


# --- router: unknown aliases are invalid ids ----------------------------------------------------


async def test_filter_drops_unknown_aliases_like_unknown_ids(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    real = _filter_request()
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(
            history_ids=["h9", "h2", "m1", "t1", "h2"],  # unknown, ok, wrong kinds, duplicate
            task_ids=["t7", "t1", "h1"],
            commitment_ids=["c2", "c1"],
        ),
    )
    result = await llm_router.filter_context(real, user_tier="economy")
    assert result.output.history_ids == [real.candidates.history[1].id]
    assert result.output.task_ids == ["task:trello:card-a"]
    assert result.output.commitment_ids == [COMMITMENTS[0].id]
    [call] = fake_llm.calls_for("retrieve.filter_context")
    assert "msg:" not in _payload(call) and "task:" not in _payload(call)


async def test_rank_earlier_maps_e_aliases_back(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    matches = _history(3, prefix="earlier:")
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=["e3", "e9", "e1"], task_ids=[], commitment_ids=[]),
    )
    result = await llm_router.rank_earlier(INCOMING, matches, user_tier="economy")
    assert result.output.history_ids == ["earlier:3", "earlier:1"]


async def test_draft_reply_returns_real_ids_and_keeps_unknown_aliases_invalid(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    real = _draft_request()
    fake_llm.set_response(
        "compose.draft",
        _draft([("message", "m1"), ("task", "t9"), ("message", "t1")], [("task", "c1")]),
    )
    result = await llm_router.draft_reply(real, user_tier="quality")
    ids = [s.id for s in result.output.sources]
    assert ids == ["msg:incoming-1", "t9", "task:trello:card-a"]
    # t9 is no alias of this request; t1 is a task cited as a message; c1 is not a task.
    assert invalid_source_ids(result.output, real) == [
        "t9",
        "task:trello:card-a",
        COMMITMENTS[0].id,
    ]


# --- composer: nothing alias-shaped is stored ---------------------------------------------------


def _compose_input(**values: Any) -> ComposeInput:
    return ComposeInput(
        incoming=INCOMING,
        history=_history(4),
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
        profile=ProfileContext(rules_json={"formality": 2}, examples=["Synthetic example"]),
        tasks=TASKS,
        commitments=COMMITMENTS,
        **values,
    )


async def test_grounded_draft_with_aliases_stores_real_ids(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=["h4", "h2"], task_ids=["t1"], commitment_ids=["c1"]),
    )

    def respond(call: FakeCall) -> DraftOutput:
        # The draft request is numbered afresh: the two selected history messages are h1, h2.
        history = next(json.loads(b.content) for b in call.user_blocks if b.name == "history")
        assert [h["id"] for h in history] == ["h1", "h2"]
        return _draft([("message", "m1"), ("message", "h2"), ("task", "t1"), ("commitment", "c1")])

    fake_llm.set_response("compose.draft", respond)
    inp = _compose_input()
    result = await compose_output(llm_router, inp)

    assert result.system_warnings == []
    assert len(result.compose_results) == 1  # grounded on the first try
    assert [s.id for s in result.output.sources] == [
        "msg:incoming-1",
        inp.history[3].id,
        "task:trello:card-a",
        COMMITMENTS[0].id,
    ]
    assert [h.id for h in result.request.history] == [inp.history[1].id, inp.history[3].id]
    selection = result.stored_selection()
    assert selection is not None
    stored = json.dumps(selection.to_json()) + result.output.model_dump_json()
    assert ALIAS_STRING.search(stored) is None
    assert list(selection.history_ids) == [inp.history[1].id, inp.history[3].id]
    assert list(selection.commitment_ids) == [COMMITMENTS[0].id]


async def test_unknown_alias_in_sources_is_regenerated_then_stripped(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(history_ids=[], task_ids=[], commitment_ids=[]),
    )
    fake_llm.set_response(
        "compose.draft", _draft([("message", "m1"), ("message", "h7")], [("task", "t5")])
    )
    result = await compose_output(llm_router, _compose_input())
    assert len(result.compose_results) == 2  # regenerated once, as for an unknown real id
    assert result.system_warnings == ["unsupported_sources"]
    assert [s.id for s in result.output.sources] == ["msg:incoming-1"]
    assert result.output.conflicts == [] and result.output.needs_info is True
