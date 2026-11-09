"""Paying with Telegram Stars inside the control bot (specs/015 US1, US3, FR-001..007, FR-012).

The dispatcher runs on an offline Bot API with ``FakeStarsApi`` as ``stars_api``, the gate on and
payments available (``stars_price=150``). Synthetic ids only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerPreCheckoutQuery, SendMessage
from aiogram.types import Chat, InlineKeyboardMarkup, Message, PhotoSize, Update
from aiogram.types import User as TgUser
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import applications
from scribe.access.reconcile import reconcile_access
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    BetaEntry,
    Payment,
    PaymentKind,
    PaymentOffer,
    PaymentSource,
    StarsEndReason,
    StarsSubscription,
    StarsSubState,
    Subscription,
    SubscriptionSource,
    User,
    UserState,
)
from scribe.db.models import (
    RefusedContact as RefusedContactRow,
)
from scribe.i18n import format_day_month_year, t
from scribe.payments import credit
from scribe.payments.states import PaySupportStates
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import (
    NOW,
    RecordingSession,
    callback_update,
    offline_bot,
    precheckout_update,
    refunded_payment_update,
    successful_payment_update,
    text_update,
)
from tests.payments_fakes import FakeStarsApi, credit_first

STRANGER = 900000601
SUSPENDED = 900000602
TESTER = 900000603
SUBSCRIBER = 900000604
PENDING = 900000605
REJECTED = 900000606
OPERATOR = 900000607
PRICE = 150
PRICE_TEXT = "150 ⭐"


@pytest.fixture
def paid(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "access_gate": True,
            "payments_enabled": True,
            "stars_price": PRICE,
            "operator_telegram_ids": [OPERATOR],
        }
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


def _day(moment: datetime) -> str:
    return format_day_month_year(moment.astimezone(UTC).date(), "en")


async def _press_subscription(
    dispatcher: Dispatcher, bot: Bot, telegram_id: int, *, data: str = "apply:subinfo"
) -> None:
    await dispatcher.feed_update(bot, callback_update(telegram_id, data))


async def _latest_offer(db: AsyncSession, telegram_id: int) -> PaymentOffer:
    db.expire_all()
    offer = await db.scalar(
        select(PaymentOffer)
        .where(PaymentOffer.telegram_id == telegram_id)
        .order_by(PaymentOffer.created_at.desc())
        .limit(1)
    )
    assert offer is not None
    return offer


async def _pay(
    dispatcher: Dispatcher,
    bot: Bot,
    clock: FrozenClock,
    telegram_id: int,
    offer: PaymentOffer,
    charge_id: str,
    *,
    is_first: bool = True,
) -> None:
    await dispatcher.feed_update(
        bot,
        successful_payment_update(
            telegram_id,
            f"sub1:{offer.id}",
            charge_id,
            amount=PRICE,
            expires_at=clock.now + timedelta(days=30),
            is_first=is_first,
        ),
    )


async def _count(db: AsyncSession, model: type) -> int:
    db.expire_all()
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


def _last_markup(api_session: RecordingSession) -> InlineKeyboardMarkup:
    markup = api_session.of_type(SendMessage)[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return markup


# --- 1-2: the offer -----------------------------------------------------------------------------


async def test_subscription_button_shows_the_offer_with_a_url_button(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)

    await _press_subscription(dispatcher, bot, STRANGER)

    offer = await _latest_offer(db_session, STRANGER)
    assert offer.price_stars == PRICE and offer.used_at is None
    assert session.sent_texts == [t("pay.offer", "en", price=PRICE_TEXT)]
    ((button,),) = _last_markup(session).inline_keyboard
    assert button.url == f"https://t.me/$fake-sub1:{offer.id}"
    assert PRICE_TEXT in button.text
    assert [link["payload"] for link in api.links] == [f"sub1:{offer.id}"]
    assert api.links[0]["price"] == PRICE


async def test_pay_offer_button_shows_the_same_offer(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await _press_subscription(dispatcher, bot, STRANGER, data="pay:offer")
    assert session.sent_texts == [t("pay.offer", "en", price=PRICE_TEXT)]


@pytest.mark.parametrize("off", ["payments", "price", "gate"])
async def test_payments_off_keeps_the_placeholder_and_creates_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    off: str,
) -> None:
    updates: dict[str, dict[str, Any]] = {
        "payments": {"payments_enabled": False},
        "price": {"stars_price": None},
        "gate": {"access_gate": False, "open_bot": True},
    }
    settings = paid.model_copy(update=updates[off])
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, settings, clock, api)

    await _press_subscription(dispatcher, bot, STRANGER)

    if off != "gate":  # with the gate off the button does nothing at all
        assert session.sent_texts == [t("access.subscription_placeholder", "en")]
    assert await _count(db_session, PaymentOffer) == 0
    assert api.links == []


async def test_offer_failure_leaves_no_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    api.fail.add("create_subscription_link")
    bot, session = offline_bot()
    await _press_subscription(_dispatcher(session_factory, paid, clock, api), bot, STRANGER)
    assert session.sent_texts == [t("pay.try_again", "en")]
    assert await _count(db_session, PaymentOffer) == 0


# --- 3: pre-checkout ----------------------------------------------------------------------------


async def test_precheckout_accepts_a_fresh_offer_and_declines_an_outdated_one(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await _press_subscription(dispatcher, bot, STRANGER)
    offer = await _latest_offer(db_session, STRANGER)
    payload = f"sub1:{offer.id}"

    await dispatcher.feed_update(bot, precheckout_update(STRANGER, payload, amount=PRICE))
    (answer,) = session.of_type(AnswerPreCheckoutQuery)
    assert answer.ok is True

    session.clear()
    clock.advance(minutes=61)
    await dispatcher.feed_update(
        bot, precheckout_update(STRANGER, payload, amount=PRICE, language_code="ru")
    )
    (answer,) = session.of_type(AnswerPreCheckoutQuery)
    assert answer.ok is False
    assert answer.error_message == t("pay.declined_expired", "ru")


async def test_precheckout_declines_a_changed_price_and_payments_off(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    await _press_subscription(_dispatcher(session_factory, paid, clock, api), bot, STRANGER)
    payload = f"sub1:{(await _latest_offer(db_session, STRANGER)).id}"

    off = _dispatcher(
        session_factory, paid.model_copy(update={"payments_enabled": False}), clock, api
    )
    await off.feed_update(bot, precheckout_update(STRANGER, payload, amount=PRICE))
    await off.feed_update(bot, precheckout_update(STRANGER, payload, amount=PRICE, currency="USD"))
    reasons = [r.error_message for r in session.of_type(AnswerPreCheckoutQuery)]
    assert reasons == [t("pay.declined_off", "en"), t("pay.declined_error", "en")]


# --- 4-5: the payment is credited once ------------------------------------------------------------


async def test_successful_payment_grants_access_and_is_credited_once(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await _press_subscription(dispatcher, bot, STRANGER)
    offer = await _latest_offer(db_session, STRANGER)
    session.clear()

    await _pay(dispatcher, bot, clock, STRANGER, offer, "charge-1")

    db_session.expire_all()
    payment = await db_session.scalar(select(Payment))
    stars = await db_session.scalar(select(StarsSubscription))
    access = await db_session.scalar(select(Subscription))
    assert payment is not None and stars is not None and access is not None
    expires = clock.now + timedelta(days=30)
    assert (payment.kind, payment.credited_via) == (PaymentKind.first, PaymentSource.update)
    assert (payment.charge_id, payment.amount_stars) == ("charge-1", PRICE)
    assert payment.stars_subscription_id == stars.id
    assert (stars.state, stars.first_charge_id, stars.period_end) == (
        StarsSubState.active,
        "charge-1",
        expires,
    )
    assert stars.price_stars == PRICE and stars.telegram_id == STRANGER
    assert (access.source, access.granted_by, access.payment_id) == (
        SubscriptionSource.stars,
        "stars",
        payment.id,
    )
    assert (access.starts_at, access.ends_at) == (payment.period_start, expires)
    kinds = (await db_session.scalars(select(AccessEvent.kind))).all()
    assert kinds == [AccessEventKind.payment_received]
    db_session.expire_all()
    assert (await _latest_offer(db_session, STRANGER)).used_at == clock.now
    assert session.sent_texts == [
        f"{t('pay.thanks', 'en', date=_day(expires), price=PRICE_TEXT)}\n"
        f"{t('pay.thanks_start', 'en')}"
    ]

    # The same delivery again: nothing new, nothing sent.
    session.clear()
    await _pay(dispatcher, bot, clock, STRANGER, offer, "charge-1")
    assert await _count(db_session, Payment) == 1
    assert await _count(db_session, StarsSubscription) == 1
    assert await _count(db_session, Subscription) == 1
    assert await _count(db_session, AccessEvent) == 1
    assert session.sent_texts == []

    # The paid person is served: /start begins setup instead of being refused.
    await dispatcher.feed_update(bot, text_update(STRANGER, "/start"))
    assert t("access.refused", "en") not in session.sent_texts
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.telegram_control_chat_id == STRANGER)
        )
        == 1
    )


async def test_two_charges_with_different_ids_are_two_payments_only_if_renewals(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    """A repeated first-payment message with a new charge id is a renewal, not a second
    subscription (defensive: Telegram sends one first payment per offer)."""
    bot, _ = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await _press_subscription(dispatcher, bot, STRANGER)
    offer = await _latest_offer(db_session, STRANGER)
    await _pay(dispatcher, bot, clock, STRANGER, offer, "charge-1")
    await _pay(dispatcher, bot, clock, STRANGER, offer, "charge-2")
    assert await _count(db_session, StarsSubscription) == 1
    assert await _count(db_session, Payment) == 2


async def test_payment_with_a_foreign_payload_is_kept_not_dropped(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await dispatcher.feed_update(
        bot,
        successful_payment_update(
            STRANGER, "garbage", "charge-x", expires_at=clock.now + timedelta(days=30)
        ),
    )
    payment = await db_session.scalar(select(Payment))
    assert payment is not None and payment.stars_subscription_id is None
    assert await _count(db_session, Subscription) == 1  # the payer is not left without it
    assert await _count(db_session, StarsSubscription) == 0
    assert session.sent_texts == []


# --- 6: a suspended user pays -----------------------------------------------------------------


async def test_suspended_user_who_pays_is_restored_by_the_reconcile_job(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
    make_user: MakeUser,
) -> None:
    user = await make_user(
        control_chat_id=SUSPENDED, state=UserState.active, access_suspended_at=clock.now
    )
    await db_session.commit()
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await _press_subscription(dispatcher, bot, SUSPENDED)
    offer = await _latest_offer(db_session, SUSPENDED)
    session.clear()

    await _pay(dispatcher, bot, clock, SUSPENDED, offer, "charge-2")

    (thanks,) = session.sent_texts
    assert thanks.startswith(
        t("pay.thanks", "en", date=_day(clock.now + timedelta(days=30)), price=PRICE_TEXT)
    )
    assert t("pay.thanks_start", "en") not in thanks  # they have an account already

    gateway = FakeMessagingGateway()
    result = await reconcile_access(session_factory, gateway, bot, paid, clock=clock)
    assert result.regained == 1
    db_session.expire_all()
    await db_session.refresh(user)
    assert user.access_suspended_at is None


# --- 7-8: people who already have access ------------------------------------------------------


async def test_beta_tester_sees_the_beta_line_in_the_offer(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    db_session.add(BetaEntry(telegram_id=TESTER, added_by="operator", added_at=clock.now))
    await db_session.commit()
    bot, session = offline_bot()
    await _press_subscription(_dispatcher(session_factory, paid, clock, api), bot, TESTER)
    (text,) = session.sent_texts
    assert t("pay.offer_has_beta", "en") in text


async def test_active_subscriber_gets_the_manage_view_not_a_new_offer(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await _press_subscription(dispatcher, bot, SUBSCRIBER)
    offer = await _latest_offer(db_session, SUBSCRIBER)
    await _pay(dispatcher, bot, clock, SUBSCRIBER, offer, "charge-3")
    session.clear()
    links_before = len(api.links)

    await _press_subscription(dispatcher, bot, SUBSCRIBER)

    (text,) = session.sent_texts
    renews = t(
        "pay.account_renews", "en", date=_day(clock.now + timedelta(days=30)), price=PRICE_TEXT
    )
    assert renews in text
    ((cancel,),) = _last_markup(session).inline_keyboard
    assert cancel.callback_data == "pay:cancel"
    assert len(api.links) == links_before  # no new offer
    assert await _count(db_session, PaymentOffer) == 1


# --- 9: pending and rejected applicants -------------------------------------------------------


async def test_pending_and_rejected_applicants_see_the_subscription_button(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    pending = await applications.submit(
        db_session,
        telegram_id=PENDING,
        username=None,
        use_case="x" * 20,
        language="en",
        now=clock.now,
    )
    rejected = await applications.submit(
        db_session,
        telegram_id=REJECTED,
        username=None,
        use_case="x" * 20,
        language="en",
        now=clock.now - timedelta(days=4),
    )
    await applications.decide(
        db_session, rejected.id, approve=False, operator_id=None, now=clock.now - timedelta(days=3)
    )
    del pending
    await db_session.commit()

    for settings, expect_button in (
        (paid, True),
        (paid.model_copy(update={"payments_enabled": False}), False),
    ):
        for who in (PENDING, REJECTED):
            bot, session = offline_bot()
            dispatcher = _dispatcher(session_factory, settings, clock, api)
            await dispatcher.feed_update(bot, text_update(who, "hello"))
            (text,) = session.sent_texts
            assert text in {
                t("access.apply_pending", "en"),
                t(
                    "access.apply_rejected_recently",
                    "en",
                    date=_day(clock.now - timedelta(days=3) + applications.REAPPLY_AFTER),
                ),
            }
            markup = session.of_type(SendMessage)[-1].reply_markup
            if expect_button:
                assert isinstance(markup, InlineKeyboardMarkup)
                ((button,),) = markup.inline_keyboard
                assert button.callback_data == "apply:subinfo"
            else:
                assert markup is None
            await db_session.execute(__import__("sqlalchemy").text("DELETE FROM refused_contacts"))
            await db_session.commit()


async def test_credit_payment_is_idempotent_on_the_charge_id(
    db_session: AsyncSession, clock: FrozenClock
) -> None:
    offer = PaymentOffer(telegram_id=STRANGER, price_stars=PRICE, created_at=clock.now)
    db_session.add(offer)
    await db_session.flush()
    args = {
        "payer_id": STRANGER,
        "charge_id": "charge-idem",
        "amount": PRICE,
        "payload": f"sub1:{offer.id}",
        "paid_at": clock.now,
        "expires_at": clock.now + timedelta(days=30),
        "is_first": True,
        "via": PaymentSource.update,
        "now": clock.now,
    }
    first = await credit.credit_payment(db_session, **args)  # type: ignore[arg-type]
    second = await credit.credit_payment(db_session, **args)  # type: ignore[arg-type]
    assert isinstance(first, credit.Credited) and isinstance(second, credit.AlreadyCredited)
    assert uuid.UUID(str(first.payment.id))


# --- /paysupport and /terms (T034, research P11) ---------------------------------------------

SECOND_OPERATOR = 900000608
SUPPORT_TEXT = "My payment <b>failed</b> twice & I was charged"


def _photo_update(chat_id: int) -> Update:
    chat = Chat(id=chat_id, type="private")
    return Update(
        update_id=900_000 + chat_id % 1000,
        message=Message(
            message_id=77,
            date=NOW,
            chat=chat,
            from_user=TgUser(id=chat_id, is_bot=False, first_name="Synthetic"),
            photo=[PhotoSize(file_id="synthetic", file_unique_id="u", width=1, height=1)],
        ),
    )


async def _fsm_state(dispatcher: Dispatcher, bot: Bot, chat_id: int) -> str | None:
    return await dispatcher.fsm.get_context(bot, chat_id, chat_id).get_state()


async def test_paysupport_forwards_plain_text_to_every_operator_and_stores_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    settings = paid.model_copy(update={"operator_telegram_ids": [OPERATOR, SECOND_OPERATOR]})
    await credit_first(db_session, telegram_id=STRANGER, charge_id="charge-sup-1", at=clock.now)
    await db_session.commit()
    clock.advance(days=3)
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, settings, clock, api)

    await dispatcher.feed_update(bot, text_update(STRANGER, "/paysupport"))
    price_line = t("pay.support_price_line", "en", price=PRICE_TEXT)
    assert session.sent_texts == [t("pay.support_intro", "en", price_line=price_line)]
    assert await _fsm_state(dispatcher, bot, STRANGER) == PaySupportStates.describe.state

    session.clear()
    await dispatcher.feed_update(bot, text_update(STRANGER, SUPPORT_TEXT, username="payer_x"))

    forwarded = [
        r for r in session.of_type(SendMessage) if r.chat_id in (OPERATOR, SECOND_OPERATOR)
    ]
    assert sorted(int(r.chat_id) for r in forwarded) == [OPERATOR, SECOND_OPERATOR]
    for request in forwarded:
        assert request.parse_mode is None  # plain text: the person's markup stays literal
        assert f"id {STRANGER}" in request.text and "@payer_x" in request.text
        assert "charge-sup-1" in request.text
        assert SUPPORT_TEXT in request.text  # verbatim, the <b> included
    assert session.sent_texts[-1] == t("pay.support_sent", "en")
    assert await _fsm_state(dispatcher, bot, STRANGER) is None

    # Nothing keeps the text: no row of any table holds it.
    tables = (
        await db_session.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        )
    ).all()
    for table in tables:
        dump = (await db_session.scalars(text(f'SELECT t::text FROM "{table}" t'))).all()
        assert not any("failed</b>" in row for row in dump), table


async def test_paysupport_without_payments_lists_none_and_asks_again_on_bad_input(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await dispatcher.feed_update(bot, text_update(STRANGER, "/paysupport"))
    session.clear()

    await dispatcher.feed_update(bot, text_update(STRANGER, "x" * 1001))
    await dispatcher.feed_update(bot, _photo_update(STRANGER))
    assert session.sent_texts == [t("pay.support_again", "en")] * 2
    assert await _fsm_state(dispatcher, bot, STRANGER) == PaySupportStates.describe.state

    await dispatcher.feed_update(bot, text_update(STRANGER, "x" * 1000))
    (operator_copy,) = [r for r in session.of_type(SendMessage) if r.chat_id == OPERATOR]
    assert t("pay.support_none", "en") in operator_copy.text


async def test_paysupport_cancel_leaves_and_cooldown_holds_for_ten_minutes(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await dispatcher.feed_update(bot, text_update(STRANGER, "/paysupport"))
    await dispatcher.feed_update(bot, text_update(STRANGER, "/cancel"))
    assert session.sent_texts[-1] == t("pay.support_cancelled", "en")
    assert await _fsm_state(dispatcher, bot, STRANGER) is None
    assert [r for r in session.of_type(SendMessage) if r.chat_id == OPERATOR] == []

    await dispatcher.feed_update(bot, text_update(STRANGER, "/paysupport"))
    await dispatcher.feed_update(bot, text_update(STRANGER, "first request"))
    session.clear()

    clock.advance(minutes=9)
    await dispatcher.feed_update(bot, text_update(STRANGER, "/paysupport"))
    assert session.sent_texts == [t("pay.support_wait", "en")]
    assert await _fsm_state(dispatcher, bot, STRANGER) is None

    session.clear()
    clock.advance(minutes=2)
    await dispatcher.feed_update(bot, text_update(STRANGER, "/paysupport"))
    assert await _fsm_state(dispatcher, bot, STRANGER) == PaySupportStates.describe.state


async def test_paysupport_without_an_operator_shows_the_contact(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    nobody = paid.model_copy(
        update={"operator_telegram_ids": [], "subscription_contact": "@help_x"}
    )
    dispatcher = _dispatcher(session_factory, nobody, clock, api)
    await dispatcher.feed_update(bot, text_update(STRANGER, "/paysupport"))
    assert session.sent_texts == [t("pay.support_contact", "en", contact="@help_x")]
    assert await _fsm_state(dispatcher, bot, STRANGER) is None

    session.clear()
    bare = paid.model_copy(update={"operator_telegram_ids": []})
    await _dispatcher(session_factory, bare, clock, api).feed_update(
        bot, text_update(STRANGER, "/paysupport")
    )
    assert session.sent_texts == [t("pay.support_contact_none", "en")]


async def test_terms_from_anyone_with_and_without_a_price(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    await _dispatcher(session_factory, paid, clock, api).feed_update(
        bot, text_update(STRANGER, "/terms")
    )
    (terms,) = session.sent_texts
    assert terms == t("pay.terms", "en", price=PRICE_TEXT)
    assert "renews automatically until cancelled" in terms and "3 years" in terms
    assert await _count(db_session, RefusedContactRow) == 0  # the gate let it through

    session.clear()
    off = paid.model_copy(update={"payments_enabled": False})
    await _dispatcher(session_factory, off, clock, api).feed_update(
        bot, text_update(STRANGER, "/terms")
    )
    assert session.sent_texts == [t("pay.terms_no_price", "en")]


# --- refunds seen by the bot (T036) --------------------------------------------------------------


async def test_refunded_payment_message_is_applied_once(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    dispatcher = _dispatcher(session_factory, paid, clock, api)
    await _press_subscription(dispatcher, bot, STRANGER)
    offer = await _latest_offer(db_session, STRANGER)
    payload = f"sub1:{offer.id}"
    await _pay(dispatcher, bot, clock, STRANGER, offer, "charge-ref-1")
    session.clear()
    clock.advance(hours=1)

    await dispatcher.feed_update(bot, refunded_payment_update(STRANGER, payload, "charge-ref-1"))

    db_session.expire_all()
    payment = await db_session.scalar(select(Payment))
    stars = await db_session.scalar(select(StarsSubscription))
    access = await db_session.scalar(select(Subscription))
    assert payment is not None and stars is not None and access is not None
    assert (payment.refunded_at, payment.refunded_by) == (clock.now, "telegram")
    assert access.cancelled_at == clock.now
    assert (stars.state, stars.ended_reason) == (StarsSubState.ended, StarsEndReason.refunded)
    assert session.sent_texts == [
        t("pay.refunded", "en", price=PRICE_TEXT, date=_day(payment.paid_at))
    ]

    session.clear()
    await dispatcher.feed_update(bot, refunded_payment_update(STRANGER, payload, "charge-ref-1"))
    assert session.sent_texts == []
    db_session.expire_all()
    assert (await db_session.scalar(select(Payment.refunded_at))) == clock.now


# --- a changed price (T043, FR-015) --------------------------------------------------------------


async def test_a_price_change_applies_to_new_offers_not_to_running_subscriptions(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    paid: Settings,
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    bot, session = offline_bot()
    old = _dispatcher(session_factory, paid, clock, api)
    await _press_subscription(old, bot, STRANGER)
    old_offer = await _latest_offer(db_session, STRANGER)
    old_payload = f"sub1:{old_offer.id}"
    await _pay(
        old, bot, clock, SUBSCRIBER, await _offer_for(db_session, clock, SUBSCRIBER), "charge-p1"
    )
    session.clear()

    pricier = _dispatcher(session_factory, paid.model_copy(update={"stars_price": 200}), clock, api)

    # A new offer shows, and charges, 200.
    await _press_subscription(pricier, bot, OPERATOR)
    assert session.sent_texts == [t("pay.offer", "en", price="200 ⭐")]
    assert api.links[-1]["price"] == 200

    # The older 150 offer is declined at pre-checkout.
    session.clear()
    await pricier.feed_update(bot, precheckout_update(STRANGER, old_payload, amount=PRICE))
    (answer,) = session.of_type(AnswerPreCheckoutQuery)
    assert answer.ok is False and answer.error_message == t("pay.declined_price", "en")

    # A subscription that started at 150 renews at 150 and is credited at 150.
    stars = await db_session.scalar(
        select(StarsSubscription).where(StarsSubscription.telegram_id == SUBSCRIBER)
    )
    assert stars is not None
    payload = f"sub1:{stars.offer_id}"
    await pricier.feed_update(bot, precheckout_update(SUBSCRIBER, payload, amount=PRICE))
    assert session.of_type(AnswerPreCheckoutQuery)[-1].ok is True
    await pricier.feed_update(
        bot,
        successful_payment_update(
            SUBSCRIBER,
            payload,
            "charge-p2",
            amount=PRICE,
            expires_at=stars.period_end + timedelta(days=30),
            is_first=False,
            date=stars.period_end,
        ),
    )
    db_session.expire_all()
    renewal = await db_session.scalar(select(Payment).where(Payment.charge_id == "charge-p2"))
    assert renewal is not None and renewal.amount_stars == PRICE
    stars = await db_session.scalar(
        select(StarsSubscription).where(StarsSubscription.telegram_id == SUBSCRIBER)
    )
    assert stars is not None and stars.price_stars == PRICE


async def _offer_for(db: AsyncSession, clock: FrozenClock, telegram_id: int) -> PaymentOffer:
    offer = PaymentOffer(telegram_id=telegram_id, price_stars=PRICE, created_at=clock.now)
    db.add(offer)
    await db.flush()
    await db.commit()
    return offer
