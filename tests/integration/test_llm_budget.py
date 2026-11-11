"""Issue #74: per-user daily LLM budget against Postgres. At the soft cap drafts use the economy
tier (the stored tier is kept); at the hard cap turns are skipped as ``budget`` without a model
call, with one notice per UTC day, regenerate / draft anyway / the pending-draft classifier are
refused, and drafting resumes the next UTC day; the operators' daily spend alert; the cap marks
of ``python -m scribe.admin cost``. Synthetic ids and texts only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import admin
from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    Contact,
    ConversationTurn,
    LLMTier,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import BudgetCaps, BudgetGuard, SettingsLimitsResolver
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import RouteName, Usage
from scribe.llm.registry import LLMRouter
from scribe.llm.spend import SpendEntry
from scribe.observability.spend_alert import send_spend_alert
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, offline_bot, text_update
from tests.integration.us1_support import (
    US1Harness,
    grounded_reply,
    reply_needed_output,
    select_all_history,
)

REPLY = "Synthetic reply."


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US1Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    return US1Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _spend(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    cost: float,
    at: datetime,
    route: RouteName = "compose.draft",
) -> None:
    record = llm_spend_repo.SpendRecorder(session_factory)
    await record(SpendEntry(user_id=user.id, route=route, at=at, usage=Usage(), cost_usd=cost))


async def _chat(
    make_chat: MakeChat, make_contact: MakeContact, user: User, name: str
) -> tuple[WorkChat, Contact]:
    contact = await make_contact(user, display_name=name)
    chat = await make_chat(user, title=name, telegram_chat_id=contact.telegram_peer_id)
    return chat, contact


async def _turns(harness: US1Harness, user: User) -> list[ConversationTurn]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.user_id == user.id)
            .order_by(ConversationTurn.arrived_at)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())


async def _paused_records(harness: US1Harness, user: User) -> list[ActivityRecord]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user.id,
                ActivityRecord.kind == ActivityKind.budget_paused,
            )
        )
        return list(result.scalars().all())


def _compose_models(fake_llm: FakeAdapter) -> list[str]:
    return [call.model for call in fake_llm.calls_for("compose.draft")]


def _alerts(harness: US1Harness) -> list[tuple[str | None, bool | None]]:
    return [(a.text, a.show_alert) for a in harness.api.of_type(AnswerCallbackQuery)]


# --- soft cap -------------------------------------------------------------------------------------


async def test_soft_cap_drafts_on_the_economy_tier_until_the_next_utc_day(
    harness: US1Harness,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    first, first_contact = await _chat(make_chat, make_contact, user, "Synthetic Colleague")
    second, second_contact = await _chat(make_chat, make_contact, user, "Synthetic Manager")
    await _spend(harness.session_factory, user, 0.99, clock())

    await harness.incoming(user, first, first_contact, "Could you send the synthetic report?")
    await harness.tick()
    assert _compose_models(fake_llm) == ["fake-large"]  # below the soft cap: quality

    await _spend(harness.session_factory, user, 0.01, clock())  # exactly $1.00: at the cap
    await harness.incoming(user, second, second_contact, "Is the synthetic plan ready?")
    await harness.tick()
    assert _compose_models(fake_llm) == ["fake-large", "fake-small"]  # economy tier
    async with harness.session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
    assert stored is not None and stored.llm_tier == LLMTier.quality  # preference untouched
    assert len(await harness.drafts(user)) == 2
    assert harness.notifications == []  # the soft cap is silent

    clock.advance(days=1)  # a new UTC day: yesterday's spend no longer counts
    third, third_contact = await _chat(make_chat, make_contact, user, "Synthetic Client")
    await harness.incoming(user, third, third_contact, "When can we meet about the draft?")
    await harness.tick()
    assert _compose_models(fake_llm)[-1] == "fake-large"


# --- hard cap -------------------------------------------------------------------------------------


async def test_hard_cap_skips_turns_without_a_model_call_and_notifies_once(
    harness: US1Harness,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active, timezone="Europe/Moscow")
    first, first_contact = await _chat(make_chat, make_contact, user, "Synthetic Colleague")
    second, second_contact = await _chat(make_chat, make_contact, user, "Synthetic Manager")
    await _spend(harness.session_factory, user, 3.0, clock())

    await harness.incoming(user, first, first_contact, "ok thanks")  # a classifier candidate
    await harness.incoming(user, second, second_contact, "Is the synthetic plan ready?")
    await harness.tick()

    assert fake_llm.calls == []  # neither the reply gate nor the composer ran
    turns = await _turns(harness, user)
    assert [(t.state, t.skip_category, t.decision_source) for t in turns] == [
        (TurnState.skipped, "budget", "pregate"),
        (TurnState.skipped, "budget", "pregate"),
    ]
    assert all(t.read_at is not None for t in turns)  # read like any other turn
    ((notified_user, notice),) = harness.notifications
    assert notified_user == user.id
    assert "today's AI usage limit" in notice
    assert "03:00 your time (midnight UTC)" in notice  # Moscow is UTC+3
    (record,) = await _paused_records(harness, user)
    assert record.meta_json == {"day": "2026-10-14"}

    clock.advance(hours=2)  # later the same day: skipped again, no second notice
    await harness.incoming(user, first, first_contact, "One more synthetic question?")
    await harness.tick()
    assert [t.skip_category for t in await _turns(harness, user)] == ["budget"] * 3
    assert len(harness.notifications) == 1
    # A restart (a new guard) still finds today's notice.
    restarted = BudgetGuard(
        harness.session_factory,
        SettingsLimitsResolver(BudgetCaps(1.0, 3.0)),
        notifier=None,
        clock=clock,
    )
    assert await restarted.notify_paused(user.id) is False

    # /skipped lists them with the reason, and no per-chat skip switch.
    harness.api.clear()
    await harness.feed(text_update(user.telegram_control_chat_id, "/skipped"))
    (sent,) = harness.api.of_type(SendMessage)
    assert (sent.text or "").count("Reason: daily AI limit reached") == 3
    assert "Always draft in this chat" not in str(sent.reply_markup)


async def test_drafting_resumes_the_next_utc_day(
    harness: US1Harness,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    chat, contact = await _chat(make_chat, make_contact, user, "Synthetic Colleague")
    await _spend(harness.session_factory, user, 5.0, clock())
    await harness.incoming(user, chat, contact, "Can you check the synthetic numbers?")
    await harness.tick()
    (paused,) = await _turns(harness, user)
    assert paused.skip_category == "budget"
    control = user.telegram_control_chat_id

    # "Draft anyway" is refused while the cap holds: the turn stays skipped.
    await harness.feed(callback_update(control, f"draftany:{paused.id}"))
    ((alert, show_alert),) = _alerts(harness)
    assert show_alert is True and "resumes automatically at 00:00 your time" in (alert or "")
    assert (await _turns(harness, user))[0].state == TurnState.skipped

    clock.advance(days=1)  # Thursday 10:00 UTC: a new budget day
    harness.api.clear()
    await harness.feed(callback_update(control, f"draftany:{paused.id}"))
    assert _alerts(harness) == [("Drafting…", None)]
    await harness.tick()
    assert (await _turns(harness, user))[0].state == TurnState.pending_review

    other, other_contact = await _chat(make_chat, make_contact, user, "Synthetic Manager")
    await harness.incoming(user, other, other_contact, "Is the synthetic plan ready?")
    await harness.tick()
    assert len(await harness.drafts(user)) == 2
    assert len(_compose_models(fake_llm)) == 2
    assert len(harness.notifications) == 1  # only the first day's notice


async def test_regenerate_and_pending_classifier_are_refused_at_the_hard_cap(
    harness: US1Harness,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    chat, contact = await _chat(make_chat, make_contact, user, "Synthetic Colleague")
    await harness.incoming(user, chat, contact, "Could you send the synthetic report?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    await _spend(harness.session_factory, user, 3.0, clock())
    calls = len(fake_llm.calls)

    harness.api.clear()
    await harness.feed(callback_update(user.telegram_control_chat_id, f"regen:{draft.id}"))
    ((alert, show_alert),) = _alerts(harness)
    assert show_alert is True and "Today's AI usage limit is reached" in (alert or "")
    assert len(await harness.drafts(user)) == 1

    # A closing message while the draft is pending: no classifier call, the draft is outdated.
    harness.set_reply_needed(reply_needed_output(False, "thanks"))
    await harness.incoming(user, chat, contact, "ok thanks")
    assert len(fake_llm.calls) == calls
    (stored,) = await harness.drafts(user)
    assert stored.outdated is True


async def test_cap_of_zero_never_pauses(
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
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    off = settings.model_copy(update={"llm_daily_soft_cap_usd": 0, "llm_daily_hard_cap_usd": 0})
    harness = US1Harness(session_factory, off, llm_router, fake_llm, gateway, clock)
    user = await make_user(state=UserState.active)
    chat, contact = await _chat(make_chat, make_contact, user, "Synthetic Colleague")
    await _spend(session_factory, user, 50.0, clock())
    await harness.incoming(user, chat, contact, "Could you send the synthetic report?")
    await harness.tick()
    assert _compose_models(fake_llm) == ["fake-large"]


# --- operator alert -------------------------------------------------------------------------------


def _operator_settings(settings: Settings, *operators: int) -> Settings:
    return settings.model_copy(update={"operator_telegram_ids": list(operators)})


async def test_operator_alert_lists_p95_and_cap_hits_of_yesterday(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    yesterday = clock() - timedelta(days=1)
    users = [await make_user() for _ in range(20)]
    for number, user in enumerate(users[:17]):
        await _spend(session_factory, user, 0.10 + number / 100, yesterday)
    soft, hard, heavy = users[17:]
    await _spend(session_factory, soft, 1.5, yesterday)
    await _spend(session_factory, hard, 3.2, yesterday)
    await _spend(session_factory, heavy, 4.0, yesterday)
    await _spend(session_factory, users[0], 9.0, clock())  # today: not reported yet
    operator = 900000001
    russian = await make_user(language="ru")
    bot, api = offline_bot()

    sent = await send_spend_alert(
        session_factory,
        bot,
        _operator_settings(settings, operator, russian.telegram_control_chat_id),
        clock=clock,
    )

    assert sent == 2
    english, translated = api.of_type(SendMessage)
    assert english.chat_id == operator and english.parse_mode is None
    text = english.text or ""
    assert text.startswith("📊 LLM spend on 2026-10-13 (UTC): 20 users, $11.76 in total.")
    p95, hard_section, soft_section = text.split("\n\n")[1:]
    # Nearest rank 19 of 20 is the $3.20 user: only the $4.00 one is above it.
    assert p95 == f"Above the 95th percentile ($3.20):\n• {heavy.id} — $4.00"
    assert hard_section == (
        f"Reached the hard cap ($3.00, drafting paused):\n• {heavy.id} — $4.00\n• {hard.id} — $3.20"
    )
    assert soft_section == (
        f"Reached the soft cap ($1.00, drafts on the economy tier):\n• {soft.id} — $1.50"
    )
    assert "Расходы на LLM за 2026-10-13" in (translated.text or "")


async def test_operator_alert_skips_the_percentile_with_few_users(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    yesterday = clock() - timedelta(days=1)
    calm, capped = await make_user(), await make_user()
    await _spend(session_factory, calm, 0.2, yesterday)
    bot, api = offline_bot()
    operators = _operator_settings(settings, 900000002)

    assert await send_spend_alert(session_factory, bot, operators, clock=clock) == 0  # nothing
    assert api.requests == []

    await _spend(session_factory, capped, 3.0, yesterday)
    assert await send_spend_alert(session_factory, bot, operators, clock=clock) == 1
    (message,) = api.of_type(SendMessage)
    assert "No percentile: fewer than 20 users spent anything." in (message.text or "")
    assert f"• {capped.id} — $3.00" in (message.text or "")
    assert str(calm.id) not in (message.text or "")
    # No operator configured: nothing is read or sent.
    assert await send_spend_alert(session_factory, bot, settings, clock=clock) == 0


# --- admin cost -----------------------------------------------------------------------------------


async def test_admin_cost_marks_cap_hits(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calm, soft, hard = await make_user(), await make_user(), await make_user()
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    await _spend(session_factory, calm, 0.5, now)
    await _spend(session_factory, soft, 1.0, now)
    await _spend(session_factory, hard, 3.5, now - timedelta(days=1))

    code = await admin.run(["cost", "--days", "2"], session_factory, clock=lambda: now,
                           settings=settings)  # fmt: skip

    assert code == 0
    out = capsys.readouterr().out
    assert "daily caps per user: soft $1.00, hard $3.00" in out
    lines: dict[uuid.UUID, str] = {}
    for line in out.splitlines():
        for user in (calm, soft, hard):
            if line.startswith("2026-") and str(user.id) in line:
                lines[user.id] = line
    assert lines[calm.id].endswith("calls")
    assert lines[soft.id].endswith("[soft cap reached: economy tier]")
    assert lines[hard.id].startswith("2026-10-13") and lines[hard.id].endswith(
        "[hard cap reached: paused]"
    )
