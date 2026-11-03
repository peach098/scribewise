"""Issue #73: ``extract.deadlines`` answers are memoised per (task, offered texts, time zone) for
the user's local day. Synthetic data only."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from scribe.context.tasks.deadlines import DeadlineMemo, memo_key, pending_texts, refine_deadlines
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import DeadlineExtraction, ProviderUnavailable
from scribe.llm.registry import LLMRouter
from scribe.llm.spend import SpendEntry
from tests.conftest import FrozenClock
from tests.unit.test_task_deadlines import FUZZY, TZ, _at, _block, _item, _task


async def test_hit_miss_and_day_rollover(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    clock = FrozenClock(datetime(2026, 10, 20, 12, 0, tzinfo=UTC))  # 15:00 in Moscow
    memo = DeadlineMemo(clock=clock)
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[_item()]))
    task = _task((FUZZY, _at(20)))

    (first,) = await refine_deadlines(llm_router, [task], TZ, memo=memo)
    (second,) = await refine_deadlines(llm_router, [task], TZ, memo=memo)  # same day: memo
    assert len(fake_llm.calls_for("extract.deadlines")) == 1
    assert first.due_on(TZ) == second.due_on(TZ) == date(2026, 11, 9)
    assert second.deadline_quote == "two weeks after the demo"
    assert (memo.hits, memo.misses) == (1, 1)

    # A changed comment is a different question; so is another time zone.
    changed = _task((FUZZY + ", maybe later", _at(20)))
    await refine_deadlines(llm_router, [changed], TZ, memo=memo)
    await refine_deadlines(llm_router, [task], "UTC", memo=memo)
    assert len(fake_llm.calls_for("extract.deadlines")) == 3

    clock.set(datetime(2026, 10, 20, 21, 30, tzinfo=UTC))  # 00:30 the next day in Moscow
    (later,) = await refine_deadlines(llm_router, [task], TZ, memo=memo)
    assert len(fake_llm.calls_for("extract.deadlines")) == 4  # a new day asks again
    assert later.due_on(TZ) == date(2026, 11, 9)


async def test_asks_only_missing_tasks_and_keeps_empty_answers(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    memo = DeadlineMemo(clock=FrozenClock())
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[]))
    known = _task((FUZZY, _at(20)))
    await refine_deadlines(llm_router, [known], TZ, memo=memo)
    await refine_deadlines(llm_router, [known], TZ, memo=memo)  # "nothing found" is kept too
    assert len(fake_llm.calls_for("extract.deadlines")) == 1

    other = _task((FUZZY, _at(20)), task_id="t2")
    await refine_deadlines(llm_router, [known, other], TZ, memo=memo)
    calls = fake_llm.calls_for("extract.deadlines")
    assert len(calls) == 2
    assert [t["id"] for t in _block(calls[-1], "tasks")] == ["t2"]


async def test_a_failure_is_never_kept(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    memo = DeadlineMemo(clock=FrozenClock())
    task = _task((FUZZY, _at(20)))
    fake_llm.set_response("extract.deadlines", ProviderUnavailable("synthetic outage"))
    await refine_deadlines(llm_router, [task], TZ, memo=memo)
    assert len(memo) == 0
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[_item()]))
    (refined,) = await refine_deadlines(llm_router, [task], TZ, memo=memo)
    assert refined.due_on(TZ) == date(2026, 11, 9)
    assert len(fake_llm.calls_for("extract.deadlines")) == 2


async def test_asked_calls_are_attributed_and_memo_hits_cost_nothing(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    """Issue #67 with the memo: the call made is billed to the user, a memo hit makes none."""
    user_id = uuid.UUID("00000000-0000-4000-8000-000000000073")
    entries: list[SpendEntry] = []

    async def sink(entry: SpendEntry) -> None:
        entries.append(entry)

    llm_router.spend_sink = sink
    memo = DeadlineMemo(clock=FrozenClock())
    fake_llm.set_response("extract.deadlines", DeadlineExtraction(items=[_item()]))
    task = _task((FUZZY, _at(20)))
    await refine_deadlines(llm_router, [task], TZ, memo=memo, user_id=user_id)
    await refine_deadlines(llm_router, [task], TZ, memo=memo, user_id=user_id)
    assert [(e.user_id, e.route) for e in entries] == [(user_id, "extract.deadlines")]


def test_bounded_and_key_has_no_text() -> None:
    memo = DeadlineMemo(clock=FrozenClock(), max_entries=2)
    keys = [memo_key(_task(task_id=f"t{i}"), [], TZ) for i in range(3)]
    for key in keys:
        memo.put(key, [])
    assert len(memo) == 2 and memo.get(keys[0]) is None and memo.get(keys[2]) == ()

    task = _task((FUZZY, _at(20)))
    assert FUZZY not in repr(memo_key(task, pending_texts(task, TZ), TZ))
