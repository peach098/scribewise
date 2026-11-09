"""Data rights of Stars payments (specs/015 FR-019, FR-020, Clarifications Q3): ``/my_data`` lists
payments, ``/delete_my_data`` warns, cancels the renewal on Telegram first (and aborts if Telegram
refuses) and keeps only the minimal payment record, and the retention job removes what is due.
Offline Bot API, ``FakeStarsApi``; synthetic ids only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from aiogram import Dispatcher
from aiogram.methods import SendDocument
from aiogram.types import BufferedInputFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import admin
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    Payment,
    PaymentKind,
    PaymentOffer,
    PaymentSource,
    StarsSubscription,
    StarsSubState,
    Subscription,
    User,
    UserState,
)
from scribe.i18n import format_day_month_year, t
from scribe.retention.purge import purge_messages
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import callback_update, offline_bot, text_update
from tests.payments_fakes import FakeStarsApi, credit_first, credit_renewal

PAYER = 900000951
CHARGE = "charge-data-1"


@pytest.fixture
def paid(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"access_gate": True, "payments_enabled": True, "stars_price": 150}
    )


@pytest.fixture
def api() -> FakeStarsApi:
    return FakeStarsApi()


def _dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> Dispatcher:
    return create_dispatcher(
        session_factory=session_factory,
        settings=settings,
        storage=PostgresFSMStorage(session_factory),
        clock=clock,
        gateway=FakeMessagingGateway(),
        stars_api=api,
    )


async def _count(db: AsyncSession, model: type, *where: object) -> int:
    db.expire_all()
    return int(
        await db.scalar(select(func.count()).select_from(model).where(*where))  # type: ignore[arg-type]
        or 0
    )


# --- /my_data -------------------------------------------------------------------------------------


async def test_my_data_lists_the_payments_and_the_subscription(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    make_user: MakeUser,
) -> None:
    await make_user(control_chat_id=PAYER, state=UserState.active)
    first = await credit_first(db_session, telegram_id=PAYER, charge_id=CHARGE, at=clock.now)
    renewal_at = first.stars.period_end
    renewal = await credit_renewal(db_session, first, charge_id="charge-data-2", at=renewal_at)
    renewal.payment.refunded_at = renewal_at + timedelta(days=1)
    await db_session.commit()
    clock.set(renewal_at + timedelta(days=2))
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)

    await dispatcher.feed_update(bot, text_update(PAYER, "/my_data"))

    (summary,) = session.sent_texts

    def day(moment: datetime) -> str:
        return format_day_month_year(moment.date(), "en")

    assert (
        t(
            "pay.mydata_stars",
            "en",
            state=t(
                "pay.mydata_stars_active",
                "en",
                date=day(first.stars.period_end),
                price="150 ⭐",
            ),
        )
        in summary
    )
    assert (
        t(
            "pay.mydata_payment",
            "en",
            date=day(clock.now - timedelta(days=2)),
            price="150 ⭐",
            kind=t("pay.mydata_kind_renewal", "en"),
            refunded=t("pay.mydata_refunded", "en", date=day(renewal_at + timedelta(days=1))),
        )
        in summary
    )
    assert t("pay.mydata_kind_first", "en") in summary

    session.clear()
    await dispatcher.feed_update(bot, callback_update(PAYER, "mydata:export"))
    (document,) = session.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    export = json.loads(document.document.data)
    assert sorted(p["charge_id"] for p in export["payments"]) == [CHARGE, "charge-data-2"]
    assert len(export["stars_subscriptions"]) == 1


# --- /delete_my_data ------------------------------------------------------------------------------


async def _seed_payer(
    db: AsyncSession, clock: FrozenClock, make_user: MakeUser
) -> tuple[User, StarsSubscription]:
    user = await make_user(control_chat_id=PAYER, state=UserState.active)
    first = await credit_first(db, telegram_id=PAYER, charge_id=CHARGE, at=clock.now)
    await db.commit()
    return user, first.stars


async def test_delete_warns_about_the_running_subscription_and_the_payment_record(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    make_user: MakeUser,
) -> None:
    await _seed_payer(db_session, clock, make_user)
    bot, session = offline_bot()
    await _dispatcher(session_factory, paid, clock, api).feed_update(
        bot, text_update(PAYER, "/delete_my_data")
    )
    (ask,) = session.sent_texts
    assert t("pay.delete_warning_running", "en") in ask
    assert t("pay.delete_warning_record", "en") in ask


async def test_delete_cancels_the_renewal_before_anything_is_deleted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    make_user: MakeUser,
) -> None:
    _, stars = await _seed_payer(db_session, clock, make_user)
    period_end = stars.period_end
    seen: dict[str, int] = {}

    async def snapshot() -> None:
        async with session_factory() as other:
            seen["users"] = await _count(other, User)
            seen["stars"] = await _count(other, StarsSubscription)
            seen["subscriptions"] = await _count(other, Subscription)

    api.hooks["set_renewal"] = snapshot
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await dispatcher.feed_update(bot, text_update(PAYER, "/delete_my_data"))

    await dispatcher.feed_update(bot, text_update(PAYER, "DELETE"))

    assert api.renewals == [{"telegram_id": PAYER, "first_charge_id": CHARGE, "canceled": True}]
    assert seen == {
        "users": 1,
        "stars": 1,
        "subscriptions": 1,
    }  # all there when Telegram was called
    assert session.sent_texts[-1] == t("delete.done", "en")
    assert await _count(db_session, User, User.telegram_control_chat_id == PAYER) == 0
    assert await _count(db_session, PaymentOffer) == 0
    assert await _count(db_session, StarsSubscription) == 0
    assert await _count(db_session, Subscription) == 0
    assert await _count(db_session, AccessEvent, AccessEvent.telegram_id == PAYER) == 0

    # Only the minimal payment record is left.
    db_session.expire_all()
    payment = await db_session.scalar(select(Payment))
    assert payment is not None
    assert payment.owner_deleted_at == clock.now and payment.stars_subscription_id is None
    assert (payment.charge_id, payment.amount_stars, payment.telegram_id) == (CHARGE, 150, PAYER)
    assert (payment.kind, payment.paid_at) == (PaymentKind.first, clock.now)
    assert payment.credited_via is PaymentSource.update
    assert payment.period_end == period_end

    # The operator still sees it, and can still refund it.
    assert await admin.run(["pay", "list"], session_factory, clock=clock) == 0
    code = await admin.run(
        ["pay", "refund", CHARGE], session_factory, clock=clock, stars_api=api, bot=bot
    )
    assert code == 0
    assert api.refunds == [{"telegram_id": PAYER, "charge_id": CHARGE}]


async def test_delete_is_aborted_when_telegram_refuses_to_cancel(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    make_user: MakeUser,
) -> None:
    await _seed_payer(db_session, clock, make_user)
    api.fail.add("set_renewal")
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await dispatcher.feed_update(bot, text_update(PAYER, "/delete_my_data"))

    await dispatcher.feed_update(bot, text_update(PAYER, "DELETE"))

    assert session.sent_texts[-1] == t("pay.delete_cancel_failed", "en")
    assert await _count(db_session, User, User.telegram_control_chat_id == PAYER) == 1
    assert await _count(db_session, StarsSubscription) == 1
    assert await _count(db_session, PaymentOffer) == 1
    assert await _count(db_session, Subscription) == 1
    assert (await db_session.scalar(select(Payment.owner_deleted_at))) is None
    stars = await db_session.scalar(select(StarsSubscription))
    assert stars is not None and stars.state is StarsSubState.active
    assert await dispatcher.fsm.get_context(bot, PAYER, PAYER).get_state() is None

    # The person can retry once Telegram answers.
    api.fail.clear()
    await dispatcher.feed_update(bot, text_update(PAYER, "/delete_my_data"))
    await dispatcher.feed_update(bot, text_update(PAYER, "DELETE"))
    assert session.sent_texts[-1] == t("delete.done", "en")


# --- the retention job ----------------------------------------------------------------------------


def _payment(charge_id: str, *, paid_ago: timedelta, clock: FrozenClock) -> Payment:
    paid_at = clock.now - paid_ago
    return Payment(
        telegram_id=PAYER,
        charge_id=charge_id,
        amount_stars=150,
        kind=PaymentKind.first,
        paid_at=paid_at,
        period_start=paid_at,
        period_end=paid_at + timedelta(days=30),
        credited_via=PaymentSource.update,
    )


async def test_purge_removes_old_deleted_owner_records_expired_offers_and_orphans(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    old = _payment("charge-old-deleted", paid_ago=timedelta(days=1200), clock=clock)
    old.owner_deleted_at = clock.now - timedelta(days=1097)
    recent = _payment("charge-recent-deleted", paid_ago=timedelta(days=1200), clock=clock)
    recent.owner_deleted_at = clock.now - timedelta(days=1095)
    live = _payment("charge-live", paid_ago=timedelta(days=5 * 365), clock=clock)
    stale_offer = PaymentOffer(
        telegram_id=PAYER, price_stars=150, created_at=clock.now - timedelta(days=8)
    )
    fresh_offer = PaymentOffer(
        telegram_id=PAYER, price_stars=150, created_at=clock.now - timedelta(days=6)
    )
    orphan_offer = PaymentOffer(
        telegram_id=PAYER, price_stars=150, created_at=clock.now - timedelta(days=40)
    )
    db_session.add_all([old, recent, live, stale_offer, fresh_offer, orphan_offer])
    await db_session.flush()
    fresh_id, orphan_offer_id = fresh_offer.id, orphan_offer.id
    orphan = StarsSubscription(
        telegram_id=PAYER,
        offer_id=orphan_offer_id,
        first_charge_id="charge-orphan",
        price_stars=150,
        started_at=clock.now - timedelta(days=40),
        period_end=clock.now - timedelta(days=10),
        state=StarsSubState.ended,
    )
    db_session.add(orphan)
    await db_session.commit()

    stats = await purge_messages(session_factory, settings, clock.now)

    db_session.expire_all()
    remaining = set((await db_session.scalars(select(Payment.charge_id))).all())
    assert remaining == {"charge-recent-deleted", "charge-live"}
    assert stats.payments_deleted == 1
    # The 8-day offer goes and the 6-day one stays. The ended subscription without a payment is
    # deleted, which frees its 40-day-old offer in the same run.
    offers = await db_session.scalars(select(PaymentOffer.id))
    assert set(offers.all()) == {fresh_id}
    assert stats.stars_subscriptions_deleted == 1 and stats.offers_deleted == 2
    assert await _count(db_session, StarsSubscription) == 0

    again = await purge_messages(session_factory, settings, clock.now)
    assert (again.payments_deleted, again.stars_subscriptions_deleted, again.offers_deleted) == (
        0,
        0,
        0,
    )
