"""The life of a Stars subscription (specs/015 US2, FR-006, FR-008..FR-011, SC-004): silent
renewals, cancel and resume in the bot, ``subscription`` updates, the end of a subscription and
its notices, reminders and late renewals. Offline Bot API, ``FakeStarsApi``, ``FrozenClock``,
fake gateway; synthetic ids only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import repo as access_repo
from scribe.access.reconcile import reconcile_access
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    Payment,
    PaymentOffer,
    PaymentSource,
    StarsCancelSource,
    StarsEndReason,
    StarsSubscription,
    StarsSubState,
    Subscription,
    User,
    UserState,
)
from scribe.i18n import format_day_month_year, t
from scribe.payments import credit
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import (
    RecordingSession,
    callback_update,
    offline_bot,
    subscription_update,
    successful_payment_update,
    text_update,
)
from tests.payments_fakes import FakeStarsApi

PAYER = 900000801
OTHER = 900000802
PRICE = 150
PRICE_TEXT = "150 ⭐"
MONTH = timedelta(days=30)
CHARGE = "charge-life-1"


@pytest.fixture
def paid(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"access_gate": True, "payments_enabled": True, "stars_price": PRICE}
    )


@pytest.fixture
def api() -> FakeStarsApi:
    return FakeStarsApi()


@pytest.fixture
def gateway() -> FakeMessagingGateway:
    return FakeMessagingGateway()


def _dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> Dispatcher:
    return create_dispatcher(
        session_factory=session_factory,
        settings=settings,
        storage=PostgresFSMStorage(session_factory),
        clock=clock,
        gateway=gateway,
        stars_api=api,
    )


def _day(moment: datetime) -> str:
    return format_day_month_year(moment.astimezone(UTC).date(), "en")


async def _subscribe(
    db: AsyncSession, clock: FrozenClock, telegram_id: int = PAYER, charge_id: str = CHARGE
) -> tuple[PaymentOffer, StarsSubscription]:
    """A first payment credited now, as the ``successful_payment`` handler does."""
    offer = PaymentOffer(telegram_id=telegram_id, price_stars=PRICE, created_at=clock.now)
    db.add(offer)
    await db.flush()
    result = await credit.credit_payment(
        db,
        payer_id=telegram_id,
        charge_id=charge_id,
        amount=PRICE,
        payload=f"sub1:{offer.id}",
        paid_at=clock.now,
        expires_at=clock.now + MONTH,
        is_first=True,
        via=PaymentSource.update,
        now=clock.now,
    )
    assert isinstance(result, credit.Credited)
    await db.commit()
    return offer, result.stars


async def _renewal_update(
    dispatcher: Dispatcher,
    bot: Bot,
    offer_id: object,
    charge_id: str,
    *,
    paid_at: datetime,
    expires_at: datetime,
    telegram_id: int = PAYER,
) -> None:
    await dispatcher.feed_update(
        bot,
        successful_payment_update(
            telegram_id,
            f"sub1:{offer_id}",
            charge_id,
            amount=PRICE,
            expires_at=expires_at,
            is_first=False,
            date=paid_at,
        ),
    )


async def _stars(db: AsyncSession) -> StarsSubscription:
    db.expire_all()
    stars = await db.scalar(select(StarsSubscription).order_by(StarsSubscription.started_at.desc()))
    assert stars is not None
    return stars


async def _events(db: AsyncSession) -> list[AccessEventKind]:
    db.expire_all()
    rows = await db.scalars(select(AccessEvent.kind))
    return sorted(rows.all())  # events written in one instant have no defined order


def _markup(session: RecordingSession, index: int = -1) -> InlineKeyboardMarkup | None:
    markup = session.of_type(SendMessage)[index].reply_markup
    return markup if isinstance(markup, InlineKeyboardMarkup) else None


def _callbacks(markup: InlineKeyboardMarkup | None) -> list[str | None]:
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


async def _press(dispatcher: Dispatcher, bot: Bot, data: str, who: int = PAYER) -> None:
    await dispatcher.feed_update(bot, callback_update(who, data))


# --- renewal (FR-006, SC-004) ------------------------------------------------------------------


async def test_renewal_extends_access_silently_without_a_gap(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    offer, stars = await _subscribe(db_session, clock)
    first_end = stars.period_end
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)
    clock.set(first_end - timedelta(minutes=2))  # Telegram charges slightly before the end

    await _renewal_update(
        dispatcher, bot, offer.id, "charge-life-2", paid_at=clock.now, expires_at=first_end + MONTH
    )

    assert session.sent_texts == []  # FR-006: a renewal is silent
    stars = await _stars(db_session)
    assert stars.period_end == first_end + MONTH and stars.state is StarsSubState.active
    periods = (
        await db_session.scalars(select(Subscription).order_by(Subscription.starts_at))
    ).all()
    assert [(p.starts_at, p.ends_at) for p in periods] == [
        (periods[0].starts_at, first_end),
        (first_end, first_end + MONTH),
    ]
    assert await _events(db_session) == sorted(
        [AccessEventKind.payment_received, AccessEventKind.payment_renewed]
    )
    for moment in (first_end - timedelta(seconds=1), first_end, first_end + timedelta(minutes=5)):
        status = await access_repo.access_status(db_session, paid, PAYER, None, now=moment)
        assert status.has_access  # SC-004: never a lapse across the boundary


# --- cancel and resume in the bot -----------------------------------------------------------------


async def test_account_shows_the_renewal_and_a_cancel_button(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    _, stars = await _subscribe(db_session, clock)
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)

    await dispatcher.feed_update(bot, text_update(PAYER, "/account"))

    (text,) = session.sent_texts
    assert t("pay.account_renews", "en", date=_day(stars.period_end), price=PRICE_TEXT) in text
    assert t("pay.account_help", "en") in text
    assert _callbacks(_markup(session)) == ["pay:cancel"]


async def test_cancel_confirm_yes_calls_telegram_then_updates_the_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    _, stars = await _subscribe(db_session, clock)
    end = stars.period_end
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)

    await _press(dispatcher, bot, "pay:cancel")
    assert session.sent_texts == [t("pay.cancel_confirm", "en", date=_day(end))]
    assert _callbacks(_markup(session)) == ["pay:cancel:yes", "pay:cancel:no"]
    assert api.renewals == []  # nothing yet

    await _press(dispatcher, bot, "pay:cancel:yes")
    assert api.renewals == [{"telegram_id": PAYER, "first_charge_id": CHARGE, "canceled": True}]
    stars = await _stars(db_session)
    assert stars.state is StarsSubState.canceled
    assert stars.canceled_by is StarsCancelSource.user_bot and stars.canceled_at == clock.now
    assert AccessEventKind.stars_canceled in await _events(db_session)
    assert session.sent_texts[-1] == t("pay.canceled", "en", date=_day(end))
    # Access stays to the end of the paid period.
    status = await access_repo.access_status(
        db_session, paid, PAYER, None, now=end - timedelta(hours=1)
    )
    assert status.has_access

    # Pressing "yes" again finds nothing to cancel.
    await _press(dispatcher, bot, "pay:cancel:yes")
    assert session.sent_texts[-1].startswith(t("pay.stale", "en"))
    assert len(api.renewals) == 1


async def test_resume_before_the_end_turns_renewal_back_on(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    _, stars = await _subscribe(db_session, clock)
    end = stars.period_end
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)
    await _press(dispatcher, bot, "pay:cancel:yes")

    await dispatcher.feed_update(bot, text_update(PAYER, "/account"))
    assert _callbacks(_markup(session)) == ["pay:resume"]
    assert t("pay.account_canceled", "en", date=_day(end)) in session.sent_texts[-1]

    await _press(dispatcher, bot, "pay:resume")
    assert api.renewals[-1] == {"telegram_id": PAYER, "first_charge_id": CHARGE, "canceled": False}
    stars = await _stars(db_session)
    assert stars.state is StarsSubState.active and stars.canceled_by is None
    assert await _events(db_session) == sorted(
        [
            AccessEventKind.payment_received,
            AccessEventKind.stars_canceled,
            AccessEventKind.stars_resumed,
        ]
    )
    assert session.sent_texts[-1] == t("pay.resumed", "en", date=_day(end), price=PRICE_TEXT)


async def test_resume_after_the_end_is_stale(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    _, stars = await _subscribe(db_session, clock)
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)
    await _press(dispatcher, bot, "pay:cancel:yes")
    clock.set(stars.period_end + timedelta(minutes=1))
    api.renewals.clear()

    await _press(dispatcher, bot, "pay:resume")

    assert session.sent_texts[-1].startswith(t("pay.stale", "en"))
    assert api.renewals == []
    assert (await _stars(db_session)).state is StarsSubState.canceled


async def test_telegram_error_on_cancel_changes_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    await _subscribe(db_session, clock)
    api.fail.add("set_renewal")
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)

    await _press(dispatcher, bot, "pay:cancel:yes")

    assert session.sent_texts == [t("pay.try_again", "en")]
    stars = await _stars(db_session)
    assert stars.state is StarsSubState.active and stars.canceled_at is None
    assert await _events(db_session) == [AccessEventKind.payment_received]


async def test_buttons_act_only_on_the_pressers_own_subscription(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    await _subscribe(db_session, clock)
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)

    for data in ("pay:cancel", "pay:cancel:yes", "pay:resume"):
        await _press(dispatcher, bot, data, who=OTHER)

    assert api.renewals == []
    assert (await _stars(db_session)).state is StarsSubState.active
    assert all(text.startswith(t("pay.stale", "en")) for text in session.sent_texts)


# --- `subscription` updates -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "state", "canceled_by", "reason"),
    [
        (["canceled"], StarsSubState.canceled, StarsCancelSource.telegram, None),
        (["canceled", "active"], StarsSubState.active, None, None),
        (["failed"], StarsSubState.ended, None, StarsEndReason.failed),
    ],
)
async def test_subscription_updates_are_followed(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
    steps: list[str],
    state: StarsSubState,
    canceled_by: StarsCancelSource | None,
    reason: StarsEndReason | None,
) -> None:
    offer, stars = await _subscribe(db_session, clock)
    end = stars.period_end
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)

    for step in steps:
        await dispatcher.feed_update(bot, subscription_update(PAYER, f"sub1:{offer.id}", step))

    stars = await _stars(db_session)
    assert stars.state is state and stars.canceled_by == canceled_by
    assert stars.ended_reason == reason
    if reason is not None:
        assert stars.ended_at == end  # the paid period stays; the subscription ends with it
    assert session.sent_texts == []
    status = await access_repo.access_status(
        db_session, paid, PAYER, None, now=end - timedelta(days=1)
    )
    assert status.has_access


async def test_subscription_updates_with_a_foreign_payload_or_payer_are_ignored(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    offer, _ = await _subscribe(db_session, clock)
    bot, _ = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)

    await dispatcher.feed_update(bot, subscription_update(PAYER, "garbage", "canceled"))
    await dispatcher.feed_update(bot, subscription_update(OTHER, f"sub1:{offer.id}", "canceled"))
    await dispatcher.feed_update(bot, subscription_update(PAYER, f"sub1:{offer.id}", "mystery"))

    assert (await _stars(db_session)).state is StarsSubState.active
    assert await _events(db_session) == [AccessEventKind.payment_received]


# --- the end of a subscription --------------------------------------------------------------------


async def _account(make_user: MakeUser, db: AsyncSession) -> User:
    user = await make_user(control_chat_id=PAYER, state=UserState.active)
    await db.commit()
    return user


async def _reconcile(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    bot: Bot,
    settings: Settings,
    clock: FrozenClock,
) -> None:
    await reconcile_access(session_factory, gateway, bot, settings, clock=clock)


async def test_canceled_subscription_ends_with_its_notice(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    user = await _account(make_user, db_session)
    _, stars = await _subscribe(db_session, clock)
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)
    await _press(dispatcher, bot, "pay:cancel:yes")
    session.clear()
    clock.set(stars.period_end + timedelta(seconds=1))

    await _reconcile(session_factory, gateway, bot, paid, clock)

    stars = await _stars(db_session)
    assert (stars.state, stars.ended_reason) == (StarsSubState.ended, StarsEndReason.canceled)
    await db_session.refresh(user)
    assert user.access_suspended_at is not None
    assert session.sent_texts == [t("pay.ended_canceled", "en")]
    assert _callbacks(_markup(session)) == ["pay:offer"]
    assert AccessEventKind.stars_ended in await _events(db_session)


async def test_active_subscription_without_a_renewal_expires_after_the_grace_hour(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    user = await _account(make_user, db_session)
    _, stars = await _subscribe(db_session, clock)
    end = stars.period_end
    bot, session = offline_bot()

    clock.set(end + timedelta(minutes=59))  # still inside the grace hour
    await _reconcile(session_factory, gateway, bot, paid, clock)
    assert session.sent_texts == []
    assert (await _stars(db_session)).state is StarsSubState.active
    await db_session.refresh(user)
    assert user.access_suspended_at is None

    clock.set(end + timedelta(minutes=61))
    await _reconcile(session_factory, gateway, bot, paid, clock)
    stars = await _stars(db_session)
    assert (stars.state, stars.ended_reason) == (StarsSubState.ended, StarsEndReason.expired)
    await db_session.refresh(user)
    assert user.access_suspended_at is not None
    assert session.sent_texts == [t("pay.ended_expired", "en")]


async def test_failed_subscription_ends_with_the_failed_notice_at_the_period_end(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    await _account(make_user, db_session)
    offer, stars = await _subscribe(db_session, clock)
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)
    await dispatcher.feed_update(bot, subscription_update(PAYER, f"sub1:{offer.id}", "failed"))

    clock.set(stars.period_end - timedelta(days=1))
    await _reconcile(session_factory, gateway, bot, paid, clock)
    assert t("pay.ended_failed", "en") not in session.sent_texts  # only the 3-day reminder

    session.clear()
    clock.set(stars.period_end + timedelta(seconds=1))
    await _reconcile(session_factory, gateway, bot, paid, clock)
    assert session.sent_texts == [t("pay.ended_failed", "en")]


async def test_access_lost_through_a_refund_sends_no_ended_notice(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    user = await _account(make_user, db_session)
    _, stars = await _subscribe(db_session, clock)
    payment = await db_session.scalar(select(Payment))
    access = await db_session.scalar(select(Subscription))
    assert payment is not None and access is not None
    # What a refund leaves behind (the refund path itself is tested with `pay refund`).
    payment.refunded_at = clock.now
    access.cancelled_at = clock.now
    stars.state = StarsSubState.ended
    stars.ended_reason = StarsEndReason.refunded
    stars.ended_at = clock.now
    await db_session.commit()
    bot, session = offline_bot()

    await _reconcile(session_factory, gateway, bot, paid, clock)

    await db_session.refresh(user)
    assert user.access_suspended_at is not None
    assert session.sent_texts == []


# --- reminders (research P6) ----------------------------------------------------------------------


async def test_renewing_subscriber_is_reminded_once_per_period_with_a_cancel_button(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    await _account(make_user, db_session)
    offer, stars = await _subscribe(db_session, clock)
    end = stars.period_end
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)

    clock.set(end - timedelta(days=2))
    await _reconcile(session_factory, gateway, bot, paid, clock)
    assert session.sent_texts == [t("pay.renew_reminder", "en", date=_day(end), price=PRICE_TEXT)]
    assert _callbacks(_markup(session)) == ["pay:cancel"]
    await _reconcile(session_factory, gateway, bot, paid, clock)
    assert len(session.sent_texts) == 1  # reminded once

    # The renewal arrives; the next period is reminded again, once.
    clock.set(end - timedelta(minutes=1))
    await _renewal_update(
        dispatcher, bot, offer.id, "charge-life-2", paid_at=clock.now, expires_at=end + MONTH
    )
    clock.set(end + MONTH - timedelta(days=2))
    await _reconcile(session_factory, gateway, bot, paid, clock)
    assert session.sent_texts[-1] == t(
        "pay.renew_reminder", "en", date=_day(end + MONTH), price=PRICE_TEXT
    )
    assert len(session.sent_texts) == 2


async def test_cancelled_subscriber_is_reminded_with_a_resume_button(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    await _account(make_user, db_session)
    _, stars = await _subscribe(db_session, clock)
    bot, session = offline_bot()
    await _press(_dispatcher(session_factory, paid, clock, api, gateway), bot, "pay:cancel:yes")
    session.clear()

    clock.set(stars.period_end - timedelta(days=2))
    await _reconcile(session_factory, gateway, bot, paid, clock)

    assert session.sent_texts == [t("pay.end_reminder", "en", date=_day(stars.period_end))]
    assert _callbacks(_markup(session)) == ["pay:resume"]


async def test_operator_subscription_reminder_points_to_stars_when_payments_are_available(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
) -> None:
    await _account(make_user, db_session)
    ends = clock.now + timedelta(days=2)
    db_session.add(
        Subscription(
            telegram_id=PAYER, starts_at=clock.now - MONTH, ends_at=ends, granted_by="operator"
        )
    )
    await db_session.commit()
    bot, session = offline_bot()

    await _reconcile(session_factory, gateway, bot, paid, clock)

    from scribe.i18n import Msg

    assert session.sent_texts == [
        t("access.sub_reminder", "en", date=_day(ends), renew=Msg("pay.renew_with_stars"))
    ]
    assert _callbacks(_markup(session)) == ["pay:offer"]


# --- late renewal and starting again --------------------------------------------------------


@pytest.mark.parametrize("via", ["update", "backfill"])
async def test_late_renewal_revives_an_expired_subscription(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    via: str,
) -> None:
    user = await _account(make_user, db_session)
    offer, stars = await _subscribe(db_session, clock)
    end, offer_id = stars.period_end, offer.id
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)
    clock.set(end + timedelta(minutes=90))
    await _reconcile(session_factory, gateway, bot, paid, clock)
    assert (await _stars(db_session)).ended_reason is StarsEndReason.expired
    assert session.sent_texts == [t("pay.ended_expired", "en")]

    # Telegram charged the person after all: the renewal arrives 90 minutes late.
    if via == "update":
        await _renewal_update(
            dispatcher, bot, offer_id, "charge-life-2", paid_at=clock.now, expires_at=end + MONTH
        )
    else:
        async with session_factory() as session_:
            await credit.credit_payment(
                session_,
                payer_id=PAYER,
                charge_id="charge-life-2",
                amount=PRICE,
                payload=f"sub1:{offer_id}",
                paid_at=clock.now,
                expires_at=None,
                is_first=False,
                via=PaymentSource.backfill,
                now=clock.now,
            )
            await session_.commit()

    stars = await _stars(db_session)
    assert stars.state is StarsSubState.active
    assert stars.ended_at is None and stars.ended_reason is None
    assert stars.period_end > clock.now
    await _reconcile(session_factory, gateway, bot, paid, clock)
    await db_session.refresh(user)
    assert user.access_suspended_at is None  # access is back
    ended_notices = [x for x in session.sent_texts if x == t("pay.ended_expired", "en")]
    assert len(ended_notices) == 1  # no second "ended" notice


async def test_person_whose_subscription_ended_can_subscribe_again(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    gateway: FakeMessagingGateway,
) -> None:
    _, stars = await _subscribe(db_session, clock)
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api, gateway)
    await _press(dispatcher, bot, "pay:cancel:yes")
    clock.set(stars.period_end + timedelta(minutes=5))
    await _reconcile(session_factory, gateway, bot, paid, clock)
    assert (await _stars(db_session)).state is StarsSubState.ended
    session.clear()

    await _press(dispatcher, bot, "apply:subinfo")  # a fresh offer, not the manage view
    assert session.sent_texts == [t("pay.offer", "en", price=PRICE_TEXT)]
    db_session.expire_all()
    new_offer = await db_session.scalar(
        select(PaymentOffer).order_by(PaymentOffer.created_at.desc()).limit(1)
    )
    assert new_offer is not None
    await dispatcher.feed_update(
        bot,
        successful_payment_update(
            PAYER,
            f"sub1:{new_offer.id}",
            "charge-life-new",
            expires_at=clock.now + MONTH,
            date=clock.now,
        ),
    )

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(StarsSubscription)) == 2
    assert (await _stars(db_session)).state is StarsSubState.active
    status = await access_repo.access_status(db_session, paid, PAYER, None, now=clock.now)
    assert status.has_access
