"""Issue #67: per-user daily LLM spend (``llm_spend``) against Postgres: the upsert, the drafting
pipeline recording every route, deletion with the user, the retention purge, its absence from the
``/my_data`` export, and ``python -m scribe.admin cost``.
Synthetic ids and texts only."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import admin
from scribe.config import Settings
from scribe.db.models import LLMSpend, UserState
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import users as users_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import RouteName, Usage
from scribe.llm.registry import LLMRouter
from scribe.llm.spend import SpendEntry
from scribe.retention.purge import purge_messages
from scribe.retention.user_data import export_user_data
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.us1_support import US1Harness, grounded_reply, select_all_history

DAY = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


async def _rows(session_factory: async_sessionmaker[AsyncSession]) -> list[LLMSpend]:
    async with session_factory() as session:
        result = await session.scalars(
            select(LLMSpend)
            .order_by(LLMSpend.day, LLMSpend.route)
            .execution_options(populate_existing=True)
        )
        return list(result.all())


def _entry(
    user_id: uuid.UUID,
    route: RouteName = "compose.draft",
    *,
    at: datetime = DAY,
    cost: float = 0.01,
    failed: bool = False,
    **usage: int,
) -> SpendEntry:
    return SpendEntry(
        user_id=user_id,
        route=route,
        at=at,
        usage=Usage(**usage),
        cost_usd=cost,
        failed=failed,
    )


async def test_calls_add_up_per_user_day_and_route(
    session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser
) -> None:
    user = await make_user()
    record = llm_spend_repo.SpendRecorder(session_factory)
    await record(
        _entry(user.id, cost=0.010001, input_tokens=100, output_tokens=10, cached_input_tokens=5)
    )
    await record(
        _entry(
            user.id,
            cost=0.02,
            failed=True,
            input_tokens=200,
            output_tokens=20,
            reasoning_tokens=7,
            cache_write_input_tokens=3,
            cache_write_1h_input_tokens=4,
        )
    )
    await record(_entry(user.id, "retrieve.filter_context", cost=0.001, input_tokens=50))
    # 23:59 UTC is still the same day; 00:00 UTC the next one, whatever the caller's zone.
    await record(_entry(user.id, at=DAY.replace(hour=23, minute=59), cost=0.5))
    await record(_entry(user.id, at=DAY.replace(hour=0) + timedelta(days=1), cost=0.25))

    rows = await _rows(session_factory)
    assert [(r.day, r.route, r.calls) for r in rows] == [
        (date(2026, 10, 30), "compose.draft", 3),
        (date(2026, 10, 30), "retrieve.filter_context", 1),
        (date(2026, 10, 31), "compose.draft", 1),
    ]
    compose = rows[0]
    assert compose.failed_calls == 1
    assert (compose.input_tokens, compose.output_tokens, compose.reasoning_tokens) == (300, 30, 7)
    assert compose.cached_input_tokens == 5
    assert (compose.cache_write_input_tokens, compose.cache_write_1h_input_tokens) == (3, 4)
    assert compose.cost_usd == Decimal("0.530001")


async def test_drafting_pipeline_records_every_route_for_the_user(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    llm_router.spend_sink = llm_spend_repo.SpendRecorder(session_factory)
    llm_router.clock = clock
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    harness = US1Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    user = await make_user(state=UserState.active)
    other = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    async with session_factory() as session:  # history, so the filter route runs
        await messages_repo.add_message(
            session,
            user.id,
            work_chat_id=chat.id,
            telegram_message_id=1,
            text="earlier synthetic message",
            sent_at=clock() - timedelta(hours=1),
            sender_contact_id=contact.id,
        )
        await session.commit()

    await harness.incoming(user, chat, contact, "ok thanks")  # short: the classifier is asked
    await harness.tick()

    (draft,) = await harness.drafts(user)
    rows = await _rows(session_factory)
    assert {r.user_id for r in rows} == {user.id} and other.id != user.id
    by_route = {r.route: r for r in rows}
    assert set(by_route) == {"classify.reply_needed", "retrieve.filter_context", "compose.draft"}
    assert all(r.day == clock().astimezone(UTC).date() and r.calls == 1 for r in rows)
    drafted = draft.llm_usage_json
    assert float(by_route["compose.draft"].cost_usd) == pytest.approx(
        drafted["compose"]["cost_usd"]
    )
    assert float(by_route["retrieve.filter_context"].cost_usd) == pytest.approx(
        drafted["filter"]["cost_usd"]
    )


async def test_spend_is_deleted_with_the_user(
    session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser
) -> None:
    user, kept = await make_user(), await make_user()
    record = llm_spend_repo.SpendRecorder(session_factory)
    await record(_entry(user.id))
    await record(_entry(kept.id))
    async with session_factory() as session:
        await users_repo.delete_user(session, user.id)
        await session.commit()
    assert [r.user_id for r in await _rows(session_factory)] == [kept.id]


async def test_purge_removes_spend_older_than_the_retention_window(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, make_user: MakeUser
) -> None:
    user = await make_user()
    record = llm_spend_repo.SpendRecorder(session_factory)
    assert settings.llm_spend_retention_days == 90
    for days_ago in (91, 90, 89, 0):
        await record(_entry(user.id, at=DAY - timedelta(days=days_ago)))

    stats = await purge_messages(session_factory, settings, DAY)
    assert stats.llm_spend_deleted == 1
    assert [r.day for r in await _rows(session_factory)] == [
        (DAY - timedelta(days=d)).date() for d in (90, 89, 0)
    ]

    shorter = settings.model_copy(update={"llm_spend_retention_days": 7})
    stats = await purge_messages(session_factory, shorter, DAY)
    assert stats.llm_spend_deleted == 2
    assert [r.day for r in await _rows(session_factory)] == [DAY.date()]
    assert (await purge_messages(session_factory, shorter, DAY)).llm_spend_deleted == 0


async def test_spend_is_not_in_the_my_data_export(
    session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser
) -> None:
    user = await make_user()
    await llm_spend_repo.SpendRecorder(session_factory)(_entry(user.id))
    async with session_factory() as session:
        export = await export_user_data(session, user.id, now=DAY)
    assert "llm_spend" not in export


async def test_admin_cost_prints_spend_per_user_per_day_by_route(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alice, bob = await make_user(), await make_user()
    record = llm_spend_repo.SpendRecorder(session_factory)
    await record(_entry(alice.id, cost=0.3, input_tokens=1000, output_tokens=100))
    await record(_entry(alice.id, "classify.reply_needed", cost=0.1, cached_input_tokens=40))
    await record(_entry(bob.id, at=DAY - timedelta(days=1), cost=0.2))
    await record(_entry(bob.id, at=DAY - timedelta(days=30), cost=9.0))  # outside the window

    code = await admin.run(["cost", "--days", "2"], session_factory, clock=lambda: DAY)
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == "LLM spend, UTC days 2026-10-29 .. 2026-10-30"
    assert f"2026-10-29  {bob.id}  $0.2000  1 calls" in lines
    alice_day = lines.index(f"2026-10-30  {alice.id}  $0.4000  2 calls")
    compose, classify = lines[alice_day + 1], lines[alice_day + 2]
    assert compose.split()[:2] == ["compose.draft", "$0.3000"] and "in 1000" in compose
    assert classify.split()[:2] == ["classify.reply_needed", "$0.1000"]
    assert "cache hit 40" in classify
    assert f"  {alice.id}  $0.4000  ($0.2000/day)" in lines
    assert lines[-1] == "total $0.6000, 2 users, $0.1500 per user per day"
    assert "$9.0000" not in out


async def test_admin_cost_without_spend(
    session_factory: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    code = await admin.run(["cost"], session_factory, clock=lambda: DAY)
    assert code == 0
    assert capsys.readouterr().out.strip() == "no LLM spend recorded since 2026-10-24"
