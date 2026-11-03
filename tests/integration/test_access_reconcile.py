"""The ``access_reconcile`` job (specs/005 T019, research Q4, Q6, Q7; data-model state transitions).

Synthetic ids and texts only. The pool is the ``FakeMessagingGateway`` (its ``started`` set shows
start/stop), the bot an offline aiogram bot that records Bot API requests.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage, TelegramMethod
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import admin
from scribe.access import repo
from scribe.access.reconcile import (
    RECONCILE_JOB,
    ReconcileResult,
    reconcile_access,
    register_access_reconcile,
)
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    BetaEntry,
    ConversationTurn,
    Draft,
    DraftState,
    LoginMethod,
    ScheduledJob,
    SessionStatus,
    Subscription,
    TurnState,
    User,
    UserState,
)
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Msg, t
from scribe.scheduling.scheduler import Scheduler
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import RecordingSession, offline_bot


@pytest.fixture
def gate_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"access_gate": True})


@pytest.fixture
def bot_api() -> tuple[Bot, RecordingSession]:
    return offline_bot()


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot: Bot,
    settings: Settings,
    clock: FrozenClock,
) -> ReconcileResult:
    return await reconcile_access(session_factory, gateway, bot, settings, clock=clock)


async def _add_beta(db: AsyncSession, user: User, clock: FrozenClock) -> BetaEntry:
    entry = BetaEntry(
        telegram_id=user.telegram_control_chat_id, added_by="operator", added_at=clock()
    )
    db.add(entry)
    await repo.record_event(
        db, user.telegram_control_chat_id, AccessEventKind.beta_added, actor="operator", now=clock()
    )
    await db.commit()
    return entry


async def _remove_beta(db: AsyncSession, user: User, clock: FrozenClock) -> None:
    """What ``scribe.admin beta remove`` does."""
    await db.execute(
        delete(BetaEntry).where(BetaEntry.telegram_id == user.telegram_control_chat_id)
    )
    await repo.record_event(
        db,
        user.telegram_control_chat_id,
        AccessEventKind.beta_removed,
        actor="operator",
        now=clock(),
    )
    await db.commit()


async def _connect(db: AsyncSession, user: User, clock: FrozenClock) -> None:
    await users_repo.upsert_telegram_session(
        db, user.id, "synthetic-session", method=LoginMethod.qr, connected_at=clock()
    )
    await db.commit()


async def _pending_draft(
    db: AsyncSession,
    user: User,
    clock: FrozenClock,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> Draft:
    chat, contact = await make_chat(user), await make_contact(user)
    turn = await turns_repo.create_turn(
        db,
        user.id,
        work_chat_id=chat.id,
        contact_id=contact.id,
        first_message_id=None,
        arrived_at=clock(),
        delay_s=0,
        due_at=clock(),
        state=TurnState.pending_review,
    )
    draft = await drafts_repo.create_draft(
        db,
        user.id,
        conversation_turn_id=turn.id,
        text="Synthetic draft",
        output_json={},
        prepared_at=clock(),
    )
    await db.commit()
    return draft


async def _events(
    db: AsyncSession, user: User, kind: AccessEventKind | None = None
) -> list[AccessEvent]:
    query = select(AccessEvent).where(AccessEvent.telegram_id == user.telegram_control_chat_id)
    if kind is not None:
        query = query.where(AccessEvent.kind == kind)
    return list((await db.scalars(query.order_by(AccessEvent.at))).all())


async def _reload(db: AsyncSession, user: User) -> User:
    await db.refresh(user)
    return user


def _sent(api: RecordingSession) -> list[tuple[int, str]]:
    return [(int(r.chat_id), r.text) for r in api.of_type(SendMessage)]


def _ended(settings: Settings, reason: str, lang: str = "en") -> str:
    return Msg("access.ended_notice", reason_text=Msg(f"access.reason_{reason}")).render(lang)


# --- the independent test of US3 ----------------------------------------------------------------


async def test_lost_then_regained_then_idle(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    bot, api = bot_api
    user = await make_user(state=UserState.active)
    await _add_beta(db_session, user, clock)
    await _connect(db_session, user, clock)
    draft = await _pending_draft(db_session, user, clock, make_chat, make_contact)
    await gateway.start(user.id)

    # Still listed: nothing changes.
    assert await _run(session_factory, gateway, bot, gate_settings, clock) == ReconcileResult(
        checked=1
    )
    assert api.requests == [] and user.id in gateway.started

    # beta remove -> lost
    clock.advance(minutes=5)
    await _remove_beta(db_session, user, clock)
    clock.advance(seconds=30)
    result = await _run(session_factory, gateway, bot, gate_settings, clock)

    assert result == ReconcileResult(checked=1, lost=1)
    assert (await _reload(db_session, user)).access_suspended_at == clock()
    assert user.state == UserState.active  # untouched
    assert user.id not in gateway.started  # stop(user_id)
    stored = await drafts_repo.get_draft(db_session, user.id, draft.id)
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.state == DraftState.expired and stored.finalized_at == clock()
    turn = await db_session.get(ConversationTurn, stored.conversation_turn_id)
    assert turn is not None
    await db_session.refresh(turn)
    assert turn.state == TurnState.handle_manually
    assert _sent(api) == [(user.telegram_control_chat_id, _ended(gate_settings, "beta_removed"))]
    (lost,) = await _events(db_session, user, AccessEventKind.access_lost)
    assert (lost.actor, lost.meta_json) == ("system", {"reason": "beta_removed"})

    # beta add -> regained
    api.clear()
    clock.advance(minutes=5)
    await _add_beta(db_session, user, clock)
    result = await _run(session_factory, gateway, bot, gate_settings, clock)

    assert result == ReconcileResult(checked=1, regained=1)
    assert (await _reload(db_session, user)).access_suspended_at is None
    assert user.id in gateway.started  # start(user_id)
    assert _sent(api) == [(user.telegram_control_chat_id, t("access.restored_notice", "en"))]
    (restored,) = await _events(db_session, user, AccessEventKind.access_restored)
    assert restored.actor == "system"

    # A third run changes nothing.
    api.clear()
    events_before = len(await _events(db_session, user))
    clock.advance(minutes=1)
    assert await _run(session_factory, gateway, bot, gate_settings, clock) == ReconcileResult(
        checked=1
    )
    assert api.requests == []
    assert len(await _events(db_session, user)) == events_before


# --- edges --------------------------------------------------------------------------------------


async def test_gate_off_is_a_no_op(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    bot, api = bot_api
    user = await make_user(state=UserState.active)  # no access rows at all
    await db_session.commit()
    await gateway.start(user.id)

    assert await _run(session_factory, gateway, bot, settings, clock) == ReconcileResult()

    assert (await _reload(db_session, user)).access_suspended_at is None
    assert user.id in gateway.started and api.requests == []
    assert await _events(db_session, user) == []


async def test_operator_is_never_suspended(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    bot, api = bot_api
    user = await make_user(state=UserState.active)
    await db_session.commit()
    operator_settings = gate_settings.model_copy(
        update={"operator_telegram_ids": [user.telegram_control_chat_id]}
    )

    result = await _run(session_factory, gateway, bot, operator_settings, clock)

    assert result == ReconcileResult(checked=1)
    assert (await _reload(db_session, user)).access_suspended_at is None
    assert api.requests == []


async def test_user_without_session_is_suspended_and_notified(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    """A pre-gate user who was never listed: reason ``beta_removed``; stop is harmless."""
    bot, api = bot_api
    user = await make_user(state=UserState.active)
    await db_session.commit()

    result = await _run(session_factory, gateway, bot, gate_settings, clock)

    assert result == ReconcileResult(checked=1, lost=1)
    assert (await _reload(db_session, user)).access_suspended_at == clock()
    assert gateway.started == set()
    assert _sent(api) == [(user.telegram_control_chat_id, _ended(gate_settings, "beta_removed"))]


async def test_ended_subscription_gives_its_reason_in_users_language(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    bot, api = bot_api
    user = await make_user(state=UserState.active, language="ru")
    await _add_beta(db_session, user, clock)
    clock.advance(days=1)
    await _remove_beta(db_session, user, clock)  # earlier than the subscription's end
    db_session.add(
        Subscription(
            telegram_id=user.telegram_control_chat_id,
            starts_at=clock() - timedelta(days=30),
            ends_at=clock() + timedelta(days=1),
            granted_by="operator",
        )
    )
    await db_session.commit()
    assert await _run(session_factory, gateway, bot, gate_settings, clock) == ReconcileResult(
        checked=1, reminded=1
    )
    api.clear()

    clock.advance(days=1, seconds=1)  # past ends_at
    result = await _run(session_factory, gateway, bot, gate_settings, clock)

    assert result == ReconcileResult(checked=1, lost=1)
    ((_, text),) = _sent(api)
    assert text == _ended(gate_settings, "subscription_ended", "ru")
    assert t("access.reason_subscription_ended", "ru") in text
    (lost,) = await _events(db_session, user, AccessEventKind.access_lost)
    assert lost.meta_json == {"reason": "subscription_ended"}


async def test_regain_with_revoked_session_adds_reconnect_hint(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    bot, api = bot_api
    user = await make_user(state=UserState.active, access_suspended_at=clock())
    await _connect(db_session, user, clock)
    await users_repo.set_telegram_session_status(db_session, user.id, SessionStatus.revoked)
    await _add_beta(db_session, user, clock)

    result = await _run(session_factory, gateway, bot, gate_settings, clock)

    assert result == ReconcileResult(checked=1, regained=1)
    ((_, text),) = _sent(api)
    assert t("access.restored_notice", "en") in text
    assert t("access.reconnect_hint", "en") in text


async def test_self_paused_user_stays_paused_after_regain(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    bot, api = bot_api
    user = await make_user(state=UserState.paused, access_suspended_at=clock())
    await _connect(db_session, user, clock)
    await _add_beta(db_session, user, clock)

    await _run(session_factory, gateway, bot, gate_settings, clock)

    user = await _reload(db_session, user)
    assert user.access_suspended_at is None and user.state == UserState.paused
    ((_, text),) = _sent(api)
    assert text == t("access.restored_notice", "en")  # the session is active: no hint


async def test_telegram_error_for_one_user_does_not_stop_the_others(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    blocked, other = await make_user(), await make_user()
    await db_session.commit()

    class BlockedSession(RecordingSession):
        async def make_request(
            self,
            bot: Bot,
            method: TelegramMethod[Any],
            timeout: int | None = None,  # noqa: ASYNC109 - aiogram BaseSession signature
        ) -> Any:
            if (
                isinstance(method, SendMessage)
                and method.chat_id == blocked.telegram_control_chat_id
            ):
                raise TelegramForbiddenError(method=method, message="Forbidden: synthetic block")
            return await super().make_request(bot, method, timeout)

    api = BlockedSession()
    bot = Bot(token="123456:TEST-TOKEN-NOT-REAL", session=api)

    result = await _run(session_factory, gateway, bot, gate_settings, clock)

    assert result == ReconcileResult(checked=2, lost=2)
    assert [chat for chat, _ in _sent(api)] == [other.telegram_control_chat_id]
    for user in (blocked, other):
        assert (await _reload(db_session, user)).access_suspended_at == clock()


async def test_registered_as_a_one_minute_job_due_at_start(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
) -> None:
    bot, _ = bot_api
    scheduler = Scheduler(session_factory, tick_s=5, clock=clock)
    register_access_reconcile(scheduler, session_factory, gateway, bot, gate_settings, clock)
    await scheduler.ensure_jobs()

    async with session_factory() as session:
        row = (
            await session.execute(select(ScheduledJob).where(ScheduledJob.name == RECONCILE_JOB))
        ).scalar_one()
    assert RECONCILE_JOB == "access_reconcile"
    assert row.next_run_at == clock()

    await scheduler.tick(drain=True)
    await scheduler.wait_idle()
    async with session_factory() as session:
        row = (
            await session.execute(select(ScheduledJob).where(ScheduledJob.name == RECONCILE_JOB))
        ).scalar_one()
    assert row.next_run_at == clock() + timedelta(seconds=60)
    assert row.last_status == "ok"


# --- gate turned off (T030a) --------------------------------------------------------------------


async def test_gate_off_restores_users_suspended_while_it_was_on(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    """With the gate off nobody is gated: suspended users come back, nothing else happens."""
    bot, api = bot_api
    suspended = await make_user(state=UserState.active, access_suspended_at=clock())
    revoked = await make_user(state=UserState.active, access_suspended_at=clock(), language="ru")
    untouched = await make_user(state=UserState.active)  # no access rows, not suspended
    await _connect(db_session, suspended, clock)
    await _connect(db_session, revoked, clock)
    await users_repo.set_telegram_session_status(db_session, revoked.id, SessionStatus.revoked)
    db_session.add(  # would be due for a reminder with the gate on
        Subscription(
            telegram_id=untouched.telegram_control_chat_id,
            starts_at=clock() - timedelta(days=1),
            ends_at=clock() + timedelta(days=1),
            granted_by="operator",
        )
    )
    await db_session.commit()
    clock.advance(minutes=1)

    result = await _run(session_factory, gateway, bot, settings, clock)

    assert result == ReconcileResult(checked=2, regained=2)
    for user in (suspended, revoked):
        assert (await _reload(db_session, user)).access_suspended_at is None
        (restored,) = await _events(db_session, user, AccessEventKind.access_restored)
        assert (restored.actor, restored.meta_json) == ("system", {"reason": "gate_off"})
    assert gateway.started == {suspended.id, revoked.id}
    assert sorted(_sent(api)) == sorted(
        [
            (suspended.telegram_control_chat_id, t("access.restored_notice", "en")),
            (
                revoked.telegram_control_chat_id,
                f"{t('access.restored_notice', 'ru')}\n{t('access.reconnect_hint', 'ru')}",
            ),
        ]
    )
    assert (await _reload(db_session, untouched)).access_suspended_at is None
    assert await _events(db_session, untouched) == []
    (sub,) = (await db_session.scalars(select(Subscription))).all()
    await db_session.refresh(sub)
    assert sub.reminded_at is None

    # A second run changes nothing.
    api.clear()
    assert await _run(session_factory, gateway, bot, settings, clock) == ReconcileResult()
    assert api.requests == []


# --- subscriptions (T028) -----------------------------------------------------------------------


def _reminder(settings: Settings, day: str, lang: str = "en") -> str:
    renew = settings.subscription_contact or t("access.renew_send_start", lang)
    return t("access.sub_reminder", lang, date=day, renew=renew)


async def _admin(
    argv: list[str], session_factory: async_sessionmaker[AsyncSession], clock: FrozenClock
) -> None:
    assert await admin.run(argv, session_factory, clock=clock) == 0


async def test_subscription_reminder_expiry_and_extend(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The independent test of US4."""
    bot, api = bot_api
    user = await make_user(state=UserState.active)
    await db_session.commit()
    chat_id = user.telegram_control_chat_id
    await _admin(["sub", "grant", str(chat_id), "--days", "2"], session_factory, clock)

    result = await _run(session_factory, gateway, bot, gate_settings, clock)

    assert result == ReconcileResult(checked=1, reminded=1)
    assert _sent(api) == [(chat_id, _reminder(gate_settings, "16 Oct 2026"))]
    (sub,) = (await db_session.scalars(select(Subscription))).all()
    await db_session.refresh(sub)
    assert sub.reminded_at == clock()
    (reminded,) = await _events(db_session, user, AccessEventKind.subscription_reminded)
    assert (reminded.actor, reminded.meta_json) == (
        "system",
        {"subscription_id": str(sub.id), "ends_at": sub.ends_at.isoformat()},
    )

    # Reminded once only.
    api.clear()
    clock.advance(hours=1)
    assert await _run(session_factory, gateway, bot, gate_settings, clock) == ReconcileResult(
        checked=1
    )
    assert api.requests == []

    # Expiry by clock suspends the user.
    clock.advance(days=2)
    result = await _run(session_factory, gateway, bot, gate_settings, clock)
    assert result == ReconcileResult(checked=1, lost=1)
    assert _sent(api) == [(chat_id, _ended(gate_settings, "subscription_ended"))]
    assert (await _reload(db_session, user)).access_suspended_at == clock()

    # sub extend after expiry restores access on the next run, and re-arms the reminder.
    api.clear()
    clock.advance(minutes=5)
    await _admin(["sub", "extend", str(chat_id), "--days", "30"], session_factory, clock)
    result = await _run(session_factory, gateway, bot, gate_settings, clock)
    assert result == ReconcileResult(checked=1, regained=1)
    assert (await _reload(db_session, user)).access_suspended_at is None
    assert _sent(api) == [(chat_id, t("access.restored_notice", "en"))]
    await db_session.refresh(sub)
    assert sub.reminded_at is None
    capsys.readouterr()


async def test_reminder_only_when_nothing_extends_access(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot_api: tuple[Bot, RecordingSession],
    gate_settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bot, api = bot_api
    beta = await make_user(state=UserState.active)
    chained = await make_user(state=UserState.active)
    later = await make_user(state=UserState.active)
    moscow = await make_user(state=UserState.active, language="ru", timezone="Europe/Moscow")
    await _add_beta(db_session, beta, clock)
    no_account = 900000099
    for user in (beta, chained, moscow):
        await _admin(
            ["sub", "grant", str(user.telegram_control_chat_id), "--until", "2026-10-16"],
            session_factory,
            clock,
        )
    await _admin(  # chained after the first one
        ["sub", "grant", str(chained.telegram_control_chat_id), "--days", "30"],
        session_factory,
        clock,
    )
    await _admin(
        ["sub", "grant", str(later.telegram_control_chat_id), "--days", "4"], session_factory, clock
    )
    await _admin(["sub", "grant", str(no_account), "--days", "1"], session_factory, clock)
    settings = gate_settings.model_copy(update={"subscription_contact": "@scribe_billing"})

    result = await _run(session_factory, gateway, bot, settings, clock)

    # The moscow user (no beta, no chain, <= 3 days) and the person with no account.
    assert result == ReconcileResult(checked=4, reminded=2)
    # 2026-10-16 23:59:59 UTC is 17 Oct in Moscow.
    assert _sent(api) == [
        (moscow.telegram_control_chat_id, _reminder(settings, "17 окт 2026", "ru"))
    ]
    assert "@scribe_billing" in _sent(api)[0][1]
    reminded = (
        await db_session.scalars(select(Subscription).where(Subscription.reminded_at.is_not(None)))
    ).all()
    assert sorted(s.telegram_id for s in reminded) == sorted(
        [moscow.telegram_control_chat_id, no_account]
    )
    events = (
        await db_session.scalars(
            select(AccessEvent).where(AccessEvent.kind == AccessEventKind.subscription_reminded)
        )
    ).all()
    assert sorted(e.telegram_id for e in events) == sorted(
        [moscow.telegram_control_chat_id, no_account]
    )

    # The 4-day one is reminded once it is within 3 days.
    api.clear()
    clock.advance(days=1, seconds=1)
    result = await _run(session_factory, gateway, bot, settings, clock)
    assert result.reminded == 1
    assert [chat for chat, _ in _sent(api)] == [later.telegram_control_chat_id]
    capsys.readouterr()
