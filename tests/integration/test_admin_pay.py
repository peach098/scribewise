"""Operator CLI ``pay`` commands (specs/015 T035, T041; contracts/operator-cli.md). In-process
``scribe.admin.run`` against the test DB with ``FakeStarsApi`` and an offline bot; synthetic ids
only. The output holds ids, amounts, dates and charge ids, never names or text."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import admin
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
    UserState,
)
from scribe.i18n import format_day_month_year, t
from scribe.payments import credit as credit_module
from scribe.payments.telegram import IncomingStars
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import RecordingSession, offline_bot
from tests.payments_fakes import FakeStarsApi, credit_first, credit_renewal


def _day(moment: datetime) -> str:
    return f"{moment.astimezone(UTC):%Y-%m-%d}"


PAYER = 900000901
OTHER = 900000902
CHARGE = "charge-admin-1"


@pytest.fixture(autouse=True)
def _quiet_logs() -> Iterator[None]:
    """Log lines are not command output: keep them out of the captured stdout."""
    with structlog.testing.capture_logs():
        yield


@pytest.fixture
def api() -> FakeStarsApi:
    return FakeStarsApi()


@pytest.fixture
def bot_api() -> tuple[Bot, RecordingSession]:
    return offline_bot()


@pytest.fixture
def paid(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"access_gate": True, "payments_enabled": True, "stars_price": 150}
    )


async def _run(
    argv: list[str],
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot: Bot | None = None,
    settings: Settings | None = None,
) -> tuple[int, str, str]:
    code = await admin.run(
        argv, session_factory, clock=clock, bot=bot, stars_api=api, settings=settings
    )
    out, err = capsys.readouterr()
    return code, out, err


async def _payment(db: AsyncSession, charge_id: str = CHARGE) -> Payment:
    db.expire_all()
    payment = await db.scalar(select(Payment).where(Payment.charge_id == charge_id))
    assert payment is not None
    return payment


# --- pay refund (T035) ----------------------------------------------------------------------------


async def test_refund_cancels_renewal_then_refunds_ends_access_and_tells_the_person(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot_api: tuple[Bot, RecordingSession],
    paid: Settings,
    make_user: MakeUser,
) -> None:
    bot, session = bot_api
    user = await make_user(control_chat_id=PAYER, state=UserState.active)
    first = await credit_first(db_session, telegram_id=PAYER, charge_id=CHARGE, at=clock.now)
    await db_session.commit()
    clock.advance(hours=2)

    code, out, err = await _run(
        ["pay", "refund", CHARGE, "--note", "duplicate"], session_factory, capsys, clock, api, bot
    )

    assert (code, err) == (0, "")
    assert out.splitlines() == [f"refunded {CHARGE} 150⭐ to {PAYER}"]
    assert api.calls == ["set_renewal", "refund"]  # renewal first, then the refund
    assert api.renewals == [{"telegram_id": PAYER, "first_charge_id": CHARGE, "canceled": True}]
    assert api.refunds == [{"telegram_id": PAYER, "charge_id": CHARGE}]

    payment = await _payment(db_session)
    assert payment.refunded_at == clock.now and payment.refunded_by == "operator:duplicate"
    access = await db_session.scalar(select(Subscription))
    assert access is not None and access.cancelled_at == clock.now
    stars = await db_session.scalar(select(StarsSubscription))
    assert stars is not None
    assert (stars.state, stars.ended_reason, stars.canceled_by) == (
        StarsSubState.ended,
        StarsEndReason.refunded,
        StarsCancelSource.refund,
    )
    events = await db_session.scalars(
        select(AccessEvent).where(AccessEvent.kind == AccessEventKind.payment_refunded)
    )
    (event,) = events.all()
    assert event.actor == "operator" and event.meta_json["charge_id"] == CHARGE
    day = format_day_month_year(first.payment.paid_at.date(), "en")
    assert session.sent_texts == [t("pay.refunded", "en", price="150 ⭐", date=day)]

    # access_reconcile then suspends the person; the refund message already explained it.
    session.clear()
    await reconcile_access(session_factory, FakeMessagingGateway(), bot, paid, clock=clock)
    await db_session.refresh(user)
    assert user.access_suspended_at is not None
    assert session.sent_texts == []


async def test_refund_of_an_older_period_ends_a_running_subscription(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = bot_api
    first = await credit_first(db_session, telegram_id=PAYER, charge_id=CHARGE, at=clock.now)
    renewal_at = first.stars.period_end
    await credit_renewal(db_session, first, charge_id="charge-admin-2", at=renewal_at)
    await db_session.commit()
    clock.set(renewal_at + timedelta(days=1))

    code, _, err = await _run(["pay", "refund", CHARGE], session_factory, capsys, clock, api, bot)

    assert (code, err) == (0, "")
    db_session.expire_all()
    stars = await db_session.scalar(select(StarsSubscription))
    assert stars is not None
    assert (stars.state, stars.ended_reason) == (StarsSubState.ended, StarsEndReason.refunded)
    periods = (
        await db_session.scalars(select(Subscription).order_by(Subscription.starts_at))
    ).all()
    assert [p.cancelled_at is not None for p in periods] == [True, False]  # only what it bought
    assert (await _payment(db_session)).refunded_by == "operator:cli"
    assert (await _payment(db_session, "charge-admin-2")).refunded_at is None


async def test_refund_refusals_change_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, session = bot_api
    await credit_first(db_session, telegram_id=PAYER, charge_id=CHARGE, at=clock.now)
    await db_session.commit()

    code, out, err = await _run(
        ["pay", "refund", "no-such-charge"], session_factory, capsys, clock, api, bot
    )
    assert (code, out) == (1, "") and "no-such-charge" in err
    assert api.calls == []

    api.fail.add("refund")  # Telegram refuses the refund
    code, out, err = await _run(["pay", "refund", CHARGE], session_factory, capsys, clock, api, bot)
    assert (code, out) == (1, "") and "nothing changed" in err
    payment = await _payment(db_session)
    assert payment.refunded_at is None
    access = await db_session.scalar(select(Subscription))
    stars = await db_session.scalar(select(StarsSubscription))
    assert access is not None and access.cancelled_at is None
    assert stars is not None and stars.state is StarsSubState.active
    assert session.sent_texts == []

    api.fail.clear()
    assert (await _run(["pay", "refund", CHARGE], session_factory, capsys, clock, api, bot))[0] == 0
    code, out, err = await _run(["pay", "refund", CHARGE], session_factory, capsys, clock, api, bot)
    assert (code, out) == (1, "") and "already refunded" in err
    assert len(api.refunds) == 1  # the refused attempt and the repeat never reached Telegram again


async def test_refund_continues_when_stopping_the_renewal_fails(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = bot_api
    await credit_first(db_session, telegram_id=PAYER, charge_id=CHARGE, at=clock.now)
    await db_session.commit()
    api.fail.add("set_renewal")

    code, out, err = await _run(["pay", "refund", CHARGE], session_factory, capsys, clock, api, bot)

    assert code == 0 and out.startswith(f"refunded {CHARGE}")
    assert "warning" in err
    assert (await _payment(db_session)).refunded_at is not None


async def test_refund_of_a_payment_whose_owner_deleted_their_data_still_works(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = bot_api
    await credit_first(db_session, telegram_id=PAYER, charge_id=CHARGE, at=clock.now)
    payment = await _payment(db_session)
    payment.owner_deleted_at = clock.now
    payment.stars_subscription_id = None
    await db_session.commit()

    code, out, _ = await _run(["pay", "refund", CHARGE], session_factory, capsys, clock, api, bot)

    assert code == 0 and out.startswith("refunded")
    assert api.calls == ["refund"]  # no Stars subscription left to stop


# --- pay list / stats / show / cancel / sync (T041) --------------------------------------------


async def _two_payers(
    db: AsyncSession, clock: FrozenClock
) -> tuple[credit_module.Credited, credit_module.Credited]:
    first = await credit_first(
        db, telegram_id=PAYER, charge_id=CHARGE, at=clock.now - timedelta(days=10)
    )
    other = await credit_first(
        db, telegram_id=OTHER, charge_id="charge-admin-other", at=clock.now - timedelta(days=5)
    )
    await db.commit()
    return first, other


async def test_pay_list_newest_first_with_range_and_who(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    await _two_payers(db_session, clock)
    await credit_first(
        db_session,
        telegram_id=900000903,
        charge_id="charge-admin-old",
        at=clock.now - timedelta(days=45),
    )  # outside the default window
    await db_session.commit()

    code, out, err = await _run(["pay", "list"], session_factory, capsys, clock, api)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines == [
        f"2026-10-09 10:00 {OTHER} 150⭐ first 2026-10-09–2026-11-08 charge-admin-other",
        f"2026-10-04 10:00 {PAYER} 150⭐ first 2026-10-04–2026-11-03 {CHARGE}",
    ]

    code, out, _ = await _run(
        ["pay", "list", "--from", "2026-08-01", "--to", "2026-09-01"],
        session_factory,
        capsys,
        clock,
        api,
    )
    assert code == 0 and out.splitlines() == [
        "2026-08-30 10:00 900000903 150⭐ first 2026-08-30–2026-09-29 charge-admin-old"
    ]

    code, out, _ = await _run(
        ["pay", "list", "--who", str(PAYER)], session_factory, capsys, clock, api
    )
    assert out.splitlines() == [lines[1]]
    code, out, _ = await _run(
        ["pay", "list", "--from", "2026-12-01"], session_factory, capsys, clock, api
    )
    assert (code, out.splitlines()) == (0, ["no payments"])


async def test_pay_list_marks_refunded_and_deleted_owner_rows(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    await _two_payers(db_session, clock)
    await _run(
        ["pay", "refund", CHARGE, "--note", "dup"], session_factory, capsys, clock, api, bot_api[0]
    )
    other = await _payment(db_session, "charge-admin-other")
    other.owner_deleted_at = clock.now
    await db_session.commit()

    _, out, _ = await _run(["pay", "list"], session_factory, capsys, clock, api)

    first_line, second_line = out.splitlines()
    assert first_line.endswith("charge-admin-other [deleted owner]")
    assert second_line.endswith(f"{CHARGE} [refunded 2026-10-14 by operator:dup]")


async def test_pay_stats_totals(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot_api: tuple[Bot, RecordingSession],
) -> None:
    first, _ = await _two_payers(db_session, clock)
    await credit_renewal(
        db_session, first, charge_id="charge-admin-renew", at=clock.now - timedelta(days=1)
    )
    await db_session.commit()
    await _run(
        ["pay", "refund", "charge-admin-other"], session_factory, capsys, clock, api, bot_api[0]
    )

    code, out, err = await _run(
        ["pay", "stats", "--from", "2026-10-01"], session_factory, capsys, clock, api
    )

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "payments 3 (first 2, renewal 1), received 450⭐, refunded 1 payments / 150⭐, net 300⭐",
        "stars subscriptions: active 1, cancelled but running 0, ended 1 (in range)",
    ]
    # Counts only: no usernames and no text.
    assert "@" not in out


async def test_pay_show_lists_the_subscription_and_its_payments(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    first, _ = await _two_payers(db_session, clock)
    await credit_renewal(db_session, first, charge_id="charge-admin-renew", at=clock.now)
    await db_session.commit()

    code, out, _ = await _run(["pay", "show", str(PAYER)], session_factory, capsys, clock, api)

    lines = out.splitlines()
    assert code == 0 and len(lines) == 3
    assert lines[0] == (
        f"stars subscription state=active price=150⭐ period_end={_day(first.stars.period_end)}"
    )
    assert "renewal" in lines[1] and "first" in lines[2]
    code, _, err = await _run(["pay", "show", "900000999"], session_factory, capsys, clock, api)
    assert code == 1 and "900000999" in err


async def test_pay_cancel_stops_the_renewal_without_a_refund(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    first, _ = await _two_payers(db_session, clock)
    end = first.stars.period_end

    code, out, err = await _run(
        ["pay", "cancel", str(PAYER), "--note", "asked by mail"],
        session_factory,
        capsys,
        clock,
        api,
    )

    assert (code, err) == (0, "")
    assert out.splitlines() == [f"cancelled renewal for {PAYER}, active until {_day(end)}"]
    assert api.renewals == [{"telegram_id": PAYER, "first_charge_id": CHARGE, "canceled": True}]
    assert api.refunds == []
    db_session.expire_all()
    stars = await db_session.scalar(
        select(StarsSubscription).where(StarsSubscription.telegram_id == PAYER)
    )
    assert stars is not None and stars.canceled_by is StarsCancelSource.operator
    assert stars.state is StarsSubState.canceled
    event = await db_session.scalar(
        select(AccessEvent).where(AccessEvent.kind == AccessEventKind.stars_canceled)
    )
    assert event is not None and (event.actor, event.reason) == ("operator", "asked by mail")

    # Nothing renewing is left to stop; and a Telegram error changes nothing.
    code, _, err = await _run(["pay", "cancel", str(PAYER)], session_factory, capsys, clock, api)
    assert code == 1 and err
    api.fail.add("set_renewal")
    code, _, err = await _run(["pay", "cancel", str(OTHER)], session_factory, capsys, clock, api)
    assert code == 1 and "nothing changed" in err
    db_session.expire_all()
    other = await db_session.scalar(
        select(StarsSubscription).where(StarsSubscription.telegram_id == OTHER)
    )
    assert other is not None and other.state is StarsSubState.active


async def test_pay_cancel_with_nothing_running_exits_1(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
) -> None:
    code, out, err = await _run(["pay", "cancel", str(PAYER)], session_factory, capsys, clock, api)
    assert (code, out) == (1, "") and err
    assert api.calls == []


async def test_pay_sync_runs_the_backfill_once(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    api: FakeStarsApi,
    bot_api: tuple[Bot, RecordingSession],
    paid: Settings,
) -> None:
    bot, _ = bot_api
    offer = PaymentOffer(
        telegram_id=PAYER, price_stars=150, created_at=clock.now - timedelta(hours=1)
    )
    db_session.add(offer)
    await db_session.commit()
    api.incoming = [
        IncomingStars(
            charge_id="charge-sync-1",
            telegram_id=PAYER,
            amount=150,
            payload=f"sub1:{offer.id}",
            date=clock.now - timedelta(days=2),
            subscription_period=2592000,
            is_refund=False,
        )
    ]

    code, out, err = await _run(
        ["pay", "sync", "--days", "3"], session_factory, capsys, clock, api, bot, paid
    )

    assert (code, err) == (0, "")
    assert out.splitlines() == ["checked 1 transactions, credited 1, refunds applied 0"]
    assert (await _payment(db_session, "charge-sync-1")).credited_via is PaymentSource.backfill

    code, out, _ = await _run(
        ["pay", "sync", "--days", "3"], session_factory, capsys, clock, api, bot, paid
    )
    assert out.splitlines() == ["checked 1 transactions, credited 0, refunds applied 0"]
