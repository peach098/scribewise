"""The ``stars_reconcile`` job (specs/015 T019, research P4, SC-002): a payment polling lost is
credited from ``getStarTransactions`` exactly once. ``FakeStarsApi`` serves the transactions and
an offline Bot API records what the person is told. Synthetic ids only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    Payment,
    PaymentKind,
    PaymentOffer,
    PaymentSource,
    ScheduledJob,
    StarsEndReason,
    StarsSubscription,
    StarsSubState,
    Subscription,
    SubscriptionSource,
)
from scribe.i18n import format_day_month_year, t
from scribe.payments import credit
from scribe.payments.reconcile import (
    STARS_RECONCILE_JOB,
    StarsReconcileResult,
    reconcile_stars,
    register_stars_reconcile,
)
from scribe.payments.telegram import IncomingStars
from scribe.scheduling.scheduler import Scheduler
from tests.conftest import FrozenClock
from tests.integration.bot_support import RecordingSession, offline_bot
from tests.payments_fakes import FakeStarsApi

PAYER = 900000701
PRICE = 150
MONTH_S = 2592000


@pytest.fixture
def paid(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"access_gate": True, "payments_enabled": True, "stars_price": PRICE}
    )


@pytest.fixture
def bot_api() -> tuple[Bot, RecordingSession]:
    return offline_bot()


async def _offer(db: AsyncSession, clock: FrozenClock, *, age: timedelta) -> PaymentOffer:
    offer = PaymentOffer(telegram_id=PAYER, price_stars=PRICE, created_at=clock.now - age)
    db.add(offer)
    await db.flush()
    await db.commit()
    return offer


def _incoming(
    offer: PaymentOffer | None,
    charge_id: str,
    *,
    date_ago: timedelta,
    clock: FrozenClock,
    **kw: object,
) -> IncomingStars:
    values: dict[str, object] = {
        "charge_id": charge_id,
        "telegram_id": PAYER,
        "amount": PRICE,
        "payload": f"sub1:{offer.id}" if offer is not None else None,
        "date": clock.now - date_ago,
        "subscription_period": MONTH_S,
        "is_refund": False,
        **kw,
    }
    return IncomingStars(**values)  # type: ignore[arg-type]


async def _count(db: AsyncSession, model: type) -> int:
    db.expire_all()
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def test_missing_first_payment_is_credited_and_the_person_is_told(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, session = bot_api
    offer = await _offer(db_session, clock, age=timedelta(minutes=30))
    api = FakeStarsApi(
        incoming=[_incoming(offer, "charge-b1", date_ago=timedelta(minutes=20), clock=clock)]
    )

    result = await reconcile_stars(session_factory, paid, api, bot, clock=clock)

    assert result == StarsReconcileResult(checked=1, credited=1, refunds=0)
    db_session.expire_all()
    payment = await db_session.scalar(select(Payment))
    stars = await db_session.scalar(select(StarsSubscription))
    access = await db_session.scalar(select(Subscription))
    assert payment is not None and stars is not None and access is not None
    assert (payment.kind, payment.credited_via) == (PaymentKind.first, PaymentSource.backfill)
    expected_end = clock.now - timedelta(minutes=20) + timedelta(seconds=MONTH_S)
    assert payment.period_end == expected_end == stars.period_end == access.ends_at
    assert access.source is SubscriptionSource.stars and stars.first_charge_id == "charge-b1"
    day = format_day_month_year(expected_end.date(), "en")
    assert session.sent_texts == [
        f"{t('pay.thanks', 'en', date=day, price='150 ⭐')}\n{t('pay.thanks_start', 'en')}"
    ]


async def test_missing_renewal_is_credited_silently(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, session = bot_api
    offer = await _offer(db_session, clock, age=timedelta(days=31))
    first_paid = clock.now - timedelta(days=30, minutes=2)
    await credit.credit_payment(
        db_session,
        payer_id=PAYER,
        charge_id="charge-r1",
        amount=PRICE,
        payload=f"sub1:{offer.id}",
        paid_at=first_paid,
        expires_at=first_paid + timedelta(days=30),
        is_first=True,
        via=PaymentSource.update,
        now=first_paid,
    )
    await db_session.commit()
    renewal_date = clock.now - timedelta(minutes=1)
    api = FakeStarsApi(
        incoming=[_incoming(offer, "charge-r2", date_ago=timedelta(minutes=1), clock=clock)]
    )

    result = await reconcile_stars(session_factory, paid, api, bot, clock=clock)

    assert result.credited == 1
    db_session.expire_all()
    renewal = await db_session.scalar(select(Payment).where(Payment.charge_id == "charge-r2"))
    assert renewal is not None and renewal.kind is PaymentKind.renewal
    assert renewal.credited_via is PaymentSource.backfill
    stars = await db_session.scalar(select(StarsSubscription))
    assert stars is not None and stars.period_end == renewal_date + timedelta(seconds=MONTH_S)
    assert session.sent_texts == []


async def test_already_credited_not_ours_and_second_run_change_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, session = bot_api
    offer = await _offer(db_session, clock, age=timedelta(minutes=30))
    api = FakeStarsApi(
        incoming=[
            _incoming(offer, "charge-n1", date_ago=timedelta(minutes=20), clock=clock),
            _incoming(None, "charge-other", date_ago=timedelta(minutes=10), clock=clock),
            _incoming(
                None,
                "charge-foreign",
                date_ago=timedelta(minutes=9),
                clock=clock,
                payload="garbage",
            ),
        ]
    )
    first = await reconcile_stars(session_factory, paid, api, bot, clock=clock)
    assert first.credited == 1 and first.checked == 3
    sent = list(session.sent_texts)
    counts = [await _count(db_session, m) for m in (Payment, StarsSubscription, Subscription)]
    assert counts == [1, 1, 1]  # only the one that is ours

    second = await reconcile_stars(session_factory, paid, api, bot, clock=clock)
    assert second.credited == 0
    assert [
        await _count(db_session, m) for m in (Payment, StarsSubscription, Subscription)
    ] == counts
    assert session.sent_texts == sent  # no second thank-you


async def test_old_transactions_outside_the_window_are_not_read(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = bot_api
    offer = await _offer(db_session, clock, age=timedelta(days=9))
    api = FakeStarsApi(
        incoming=[_incoming(offer, "charge-old", date_ago=timedelta(days=8), clock=clock)]
    )
    result = await reconcile_stars(session_factory, paid, api, bot, clock=clock)
    assert result == StarsReconcileResult(checked=0)


async def test_payments_off_and_nothing_running_makes_no_api_call(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = bot_api
    api = FakeStarsApi()
    result = await reconcile_stars(session_factory, settings, api, bot, clock=clock)
    assert result == StarsReconcileResult()
    assert api.calls == []


async def test_payments_off_but_a_subscription_running_still_reconciles(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = bot_api
    offer = await _offer(db_session, clock, age=timedelta(days=1))
    await credit.credit_payment(
        db_session, payer_id=PAYER, charge_id="charge-q1", amount=PRICE,
        payload=f"sub1:{offer.id}", paid_at=clock.now - timedelta(days=1),
        expires_at=clock.now + timedelta(days=29), is_first=True,
        via=PaymentSource.update, now=clock.now,
    )  # fmt: skip
    await db_session.commit()
    api = FakeStarsApi()
    await reconcile_stars(session_factory, settings, api, bot, clock=clock)
    assert api.calls == ["incoming_transactions"]


async def test_a_telegram_error_is_logged_and_the_job_finishes(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = bot_api
    api = FakeStarsApi()
    api.fail.add("incoming_transactions")
    assert (
        await reconcile_stars(session_factory, paid, api, bot, clock=clock)
        == StarsReconcileResult()
    )


async def test_registered_as_a_ten_minute_job_due_at_start(
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = bot_api
    scheduler = Scheduler(session_factory, tick_s=5, clock=clock)
    register_stars_reconcile(scheduler, session_factory, paid, FakeStarsApi(), bot, clock)
    await scheduler.ensure_jobs()

    async with session_factory() as session:
        row = (
            await session.execute(
                select(ScheduledJob).where(ScheduledJob.name == STARS_RECONCILE_JOB)
            )
        ).scalar_one()
    assert STARS_RECONCILE_JOB == "stars_reconcile"
    assert row.next_run_at == clock()

    await scheduler.tick(drain=True)
    await scheduler.wait_idle()
    async with session_factory() as session:
        row = (
            await session.execute(
                select(ScheduledJob).where(ScheduledJob.name == STARS_RECONCILE_JOB)
            )
        ).scalar_one()
    assert row.next_run_at == clock() + timedelta(minutes=10)
    assert row.last_status == "ok"


async def test_a_refund_seen_in_the_transactions_is_applied_once(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, session = bot_api
    offer = await _offer(db_session, clock, age=timedelta(minutes=30))
    charge = "charge-refund-1"
    api = FakeStarsApi(
        incoming=[
            _incoming(offer, charge, date_ago=timedelta(minutes=20), clock=clock),
            _incoming(offer, charge, date_ago=timedelta(minutes=5), clock=clock, is_refund=True),
        ]
    )
    session.clear()

    first = await reconcile_stars(session_factory, paid, api, bot, clock=clock)

    assert (first.credited, first.refunds) == (1, 1)  # credited, then refunded, oldest first
    db_session.expire_all()
    payment = await db_session.scalar(select(Payment))
    stars = await db_session.scalar(select(StarsSubscription))
    access = await db_session.scalar(select(Subscription))
    assert payment is not None and stars is not None and access is not None
    assert payment.refunded_by == "telegram" and payment.refunded_at == clock.now
    assert access.cancelled_at == clock.now
    assert (stars.state, stars.ended_reason) == (StarsSubState.ended, StarsEndReason.refunded)
    texts = list(session.sent_texts)
    assert texts[-1].startswith(t("pay.refunded", "en", price="150 ⭐", date="")[:12])

    second = await reconcile_stars(session_factory, paid, api, bot, clock=clock)
    assert (second.credited, second.refunds) == (0, 0)
    assert session.sent_texts == texts
    db_session.expire_all()
    assert (await db_session.scalar(select(Payment.refunded_at))) == clock.now


async def test_a_refund_of_an_unknown_charge_is_ignored(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, session = bot_api
    api = FakeStarsApi(
        incoming=[
            _incoming(
                None, "charge-unknown", date_ago=timedelta(minutes=5), clock=clock, is_refund=True
            )
        ]
    )
    result = await reconcile_stars(session_factory, paid, api, bot, clock=clock)
    assert (result.credited, result.refunds) == (0, 0)
    assert session.sent_texts == []
