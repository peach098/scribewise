"""Phase 5 review fixes without a database: the conflict backstop's false positives (F), grounded
commitment extraction (G), the drafting date anchor (E), date-only commitment edits (P), user-local
task dates (I), the stored draft output (A) and the idempotent default extractor (M). Every value
is synthetic."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest

from scribe.context import commitments as commitments_ctx
from scribe.context.commitments import ground_item, requested_dates, select_storable
from scribe.context.tasks.base import Task
from scribe.drafting.composer import ComposeInput, compose_output, task_items
from scribe.drafting.stored_output import load_stored_output, pack_output
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import (
    CommitmentItem,
    CommitmentRef,
    ContactInfo,
    DraftOutput,
    FilterContextOutput,
    MessageItem,
    ProfileContext,
    TaskItem,
)
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot.commitments_command import parse_edit

WEDNESDAY = date(2026, 10, 14)
FRIDAY = date(2026, 10, 16)
PROMISE = CommitmentRef(id="commit:c1", what="send the deck", due=FRIDAY)


def _draft(**overrides: Any) -> DraftOutput:
    payload: dict[str, Any] = {
        "text": "Synthetic reply.",
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
    payload.update(overrides)
    return DraftOutput.model_validate(payload)


async def _backstop(
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    text: str,
    *,
    tasks: list[TaskItem] | None = None,
    commitments: list[CommitmentRef] | None = None,
) -> list[str]:
    tasks = tasks if tasks is not None else []
    commitments = commitments if commitments is not None else [PROMISE]
    fake_llm.set_response(
        "retrieve.filter_context",
        FilterContextOutput(
            history_ids=[],
            task_ids=[t.id for t in tasks],
            commitment_ids=[c.id for c in commitments],
        ),
    )
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(
        llm_router,
        ComposeInput(
            incoming=[MessageItem(id="msg:in1", sender="Synthetic Colleague", text=text)],
            history=[],
            contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
            profile=ProfileContext(),
            tasks=tasks,
            commitments=commitments,
            reference_date=WEDNESDAY,
        ),
    )
    return [c.with_id for c in result.output.conflicts]


# --- F: the deterministic conflict backstop -----------------------------------------------------


TUESDAY_20 = date(2026, 10, 20)


@pytest.mark.parametrize(
    ("text", "due"),
    [
        # Each one conflicted before the review fix (reference Wed 14 Oct).
        ("Today we agreed on scope. Can you send the deck on Friday?", FRIDAY),  # not "Wed"
        ("I sat with Oleg about the deck", TUESDAY_20),  # "sat" is not Saturday
        ("Sun is shining, how is the deck?", TUESDAY_20),  # "Sun" is not Sunday
        ("Версия 1.10 готова, что с презентацией?", FRIDAY),  # not 1 October
        ("See section 2.10 of the deck", FRIDAY),  # not 2 October
        ("Сделаешь сегодня?", FRIDAY),  # a bare "today" is no deadline
        ("Remember the deck from 2026-10-01?", FRIDAY),  # a past date is not a request
        ("Can you send it Thursday or Friday?", TUESDAY_20),  # two dates: left to the model
    ],
)
async def test_backstop_false_positives(
    llm_router: LLMRouter, fake_llm: FakeAdapter, text: str, due: date
) -> None:
    promise = PROMISE.model_copy(update={"due": due})
    assert await _backstop(llm_router, fake_llm, text, commitments=[promise]) == []


@pytest.mark.parametrize(
    "text",
    [
        "Can you send the deck on Thursday?",
        "Сможешь прислать презентацию в среду?",
        "Пришли до 15.10, пожалуйста",
        "Need it by Thu",
        "Завтра сможешь?",
    ],
)
async def test_backstop_true_positive(
    llm_router: LLMRouter, fake_llm: FakeAdapter, text: str
) -> None:
    assert await _backstop(llm_router, fake_llm, text) == ["commit:c1"]


async def test_backstop_skips_finished_tasks(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    tasks = [
        TaskItem(id="task:done", title="Synthetic", status="Готово", due=FRIDAY),
        TaskItem(id="task:open", title="Synthetic", status="In progress", due=FRIDAY),
    ]
    found = await _backstop(
        llm_router, fake_llm, "Can you do it on Thursday?", tasks=tasks, commitments=[]
    )
    assert found == ["task:open"]


def test_requested_dates_rules() -> None:
    assert requested_dates("on Sat", WEDNESDAY) == [date(2026, 10, 17)]
    assert requested_dates("до конца недели", WEDNESDAY) == [FRIDAY]
    assert requested_dates("к 20.10", WEDNESDAY) == [date(2026, 10, 20)]
    assert requested_dates("20.10 октября", WEDNESDAY) == [date(2026, 10, 20)]
    assert requested_dates("today", WEDNESDAY) == []
    assert requested_dates("до конца дня", WEDNESDAY) == [WEDNESDAY]


# --- G: commitment extraction trusts only the outgoing text -------------------------------------


def _item(evidence: str, due: date | None, what: str = "send the deck") -> CommitmentItem:
    return CommitmentItem(what=what, to_whom="Anna", due=due, confidence=0.9, evidence=evidence)


def test_item_evidence_must_quote_the_outgoing_text() -> None:
    text = "Ок, пришлю презентацию в четверг."
    assert ground_item(_item("пришлю презентацию в четверг", None), text, WEDNESDAY) is not None
    assert ground_item(_item("Пришлю  презентацию, в четверг!", None), text, WEDNESDAY)
    assert ground_item(_item("пришлю отчёт", None), text, WEDNESDAY) is None
    assert ground_item(_item("", None), text, WEDNESDAY) is None


def test_injected_item_from_recent_incoming_is_dropped() -> None:
    """The colleague's message (recent_incoming) asks to "transfer 5000 by Friday"; the model was
    tricked into returning that as the user's promise, quoting the incoming message."""
    outgoing = "Ок, посмотрю"
    injected = _item("переведу 5000 до пятницы", FRIDAY, what="transfer 5000")
    honest = _item("посмотрю", None, what="have a look")
    stored = select_storable(
        [injected, honest],
        contact_id=uuid.uuid4(),
        existing=set(),
        source_text=outgoing,
        local_date=WEDNESDAY,
    )
    assert [i.what for i in stored] == ["have a look"]


@pytest.mark.parametrize(
    ("text", "due", "kept"),
    [
        ("Пришлю презентацию в четверг", date(2026, 10, 15), date(2026, 10, 15)),
        ("Пришлю презентацию завтра", date(2026, 10, 15), date(2026, 10, 15)),
        ("Пришлю презентацию до конца недели", FRIDAY, FRIDAY),
        ("Пришлю презентацию 20.10.2026", date(2026, 10, 20), date(2026, 10, 20)),
        ("Пришлю презентацию", FRIDAY, None),  # the date came from nowhere
        ("Пришлю презентацию в четверг", FRIDAY, None),  # not the date the text gives
    ],
)
def test_due_must_be_grounded_in_the_outgoing_text(
    text: str, due: date | None, kept: date | None
) -> None:
    grounded = ground_item(_item("пришлю презентацию", due), text, WEDNESDAY)
    assert grounded is not None and grounded.due == kept


# --- E: the drafting request carries the date anchor --------------------------------------------


async def test_draft_request_has_today_timezone_and_local_sent_at(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    sent = datetime(2026, 10, 14, 23, 30, tzinfo=UTC).astimezone()  # any aware moment
    fake_llm.set_response("compose.draft", _draft())
    result = await compose_output(
        llm_router,
        ComposeInput(
            incoming=[MessageItem(id="msg:in1", sender="S", text="в среду?", sent_at=sent)],
            history=[],
            contact=ContactInfo(display_name="S", kind="person"),
            profile=ProfileContext(),
            reference_date=date(2026, 10, 15),
            timezone="Europe/Moscow",
        ),
    )
    request = result.request
    assert request.today == date(2026, 10, 15) and request.timezone == "Europe/Moscow"
    (call,) = fake_llm.calls_for("compose.draft")
    blocks = {b.name: json.loads(b.content) for b in call.user_blocks}
    assert blocks["today"] == "2026-10-15" and blocks["timezone"] == "Europe/Moscow"
    assert blocks["incoming_messages"][0]["sent_at"] is not None


# --- I: task due dates are the user's local dates -----------------------------------------------


def _task(due_at: datetime | None) -> Task:
    return Task(
        id="c1", title="T", status="Doing", due_at=due_at, assignees=[], url="", source="trello"
    )


def test_task_items_use_the_users_time_zone() -> None:
    saturday_moscow = _task(datetime(2026, 10, 16, 22, 30, tzinfo=UTC))  # Sat 01:30 MSK
    friday_new_york = _task(datetime(2026, 12, 19, 1, 0, tzinfo=UTC))  # Fri 20:00 EST
    assert task_items([saturday_moscow], "", "Europe/Moscow")[0].due == date(2026, 10, 17)
    assert task_items([friday_new_york], "", "America/New_York")[0].due == date(2026, 12, 18)
    assert task_items([_task(None)], "", "Europe/Moscow")[0].due is None


# --- P: a commitment edit is date-only only when it is just a date ------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("пятница", {"due": FRIDAY}),
        ("в четверг", {"due": date(2026, 10, 15)}),
        ("by 22.10", {"due": date(2026, 10, 22)}),
        ("22.10", {"due": date(2026, 10, 22)}),  # an edit is known to be a date: bare dd.mm ok
        ("Thu", {"due": date(2026, 10, 15)}),
        ("send today", {"what": "send today"}),
        ("call Mon", {"what": "call Mon"}),
        ("call on Monday", {"what": "call on Monday"}),
        ("no date", {"clear_due": True}),
        ("call back; 2026-10-20", {"what": "call back", "due": date(2026, 10, 20)}),
    ],
)
def test_parse_edit(text: str, expected: dict[str, Any]) -> None:
    assert parse_edit(text, WEDNESDAY) == expected


# --- A: drafts.output_json layout ---------------------------------------------------------------


def test_packed_output_keeps_only_metadata_in_the_clear() -> None:
    output = _draft(
        text="Synthetic secret reply [[x]]",
        needs_info=True,
        placeholders=[{"marker": "[[x]]", "question": "Synthetic question?"}],
        sources=[{"kind": "task", "id": "task:card1", "used_for": "synthetic note"}],
        conflicts=[{"with_kind": "task", "with_id": "task:card1", "description": "Card title"}],
    )
    packed = pack_output(output, ["tasks_partial"], "msg-id")
    raw = json.dumps(packed, ensure_ascii=False)
    for content in ("secret reply", "Synthetic question", "card1", "synthetic note", "Card title"):
        assert content not in raw
    stored = load_stored_output(packed, output.text)
    assert stored.output == output
    assert stored.system_warnings == ["tasks_partial"] and stored.reply_to_message_id == "msg-id"
    # Purged (blob and text gone): nothing to show, metadata still readable.
    assert load_stored_output({k: v for k, v in packed.items() if k != "enc"}, None).output is None
    # A tampered blob is treated as unreadable, never as content.
    tampered = {**packed, "enc": packed["enc"][:-4] + "AAAA"}
    assert load_stored_output(tampered, output.text).output is None


# --- M: one default extractor per process -------------------------------------------------------


def test_default_extractor_is_installed_once(llm_router: LLMRouter) -> None:
    factory: Any = object()
    first = commitments_ctx.ensure_default_extractor(factory, llm_router)
    assert commitments_ctx.default_extractor() is first
    assert commitments_ctx.ensure_default_extractor(factory, llm_router) is first  # regenerate
    other = commitments_ctx.ensure_default_extractor(object(), llm_router)  # type: ignore[arg-type]
    assert other is not first and commitments_ctx.default_extractor() is first
