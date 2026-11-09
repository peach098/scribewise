"""The pre-checkout decision table (specs/015 research P3, SC-003). Plain rows and a frozen
clock; every decline reason is covered. Synthetic ids only."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from scribe.config import Settings
from scribe.db.models import PaymentOffer, StarsSubscription, StarsSubState
from scribe.i18n import CATALOGS
from scribe.payments.checkout import Accept, Decline, DeclineReason, decide
from tests.conftest import FROZEN_NOW

PAYER = 900000501
OTHER = 900000502
PRICE = 150


@pytest.fixture
def payments(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"access_gate": True, "payments_enabled": True, "stars_price": PRICE}
    )


def _offer(
    *, age: timedelta = timedelta(minutes=59), price: int = PRICE, owner: int = PAYER
) -> PaymentOffer:
    return PaymentOffer(
        id=uuid.uuid4(), telegram_id=owner, price_stars=price, created_at=FROZEN_NOW - age
    )


def _stars(offer: PaymentOffer, state: StarsSubState, *, payer: int = PAYER) -> StarsSubscription:
    return StarsSubscription(
        id=uuid.uuid4(),
        telegram_id=payer,
        offer_id=offer.id,
        first_charge_id="charge-1",
        price_stars=PRICE,
        started_at=FROZEN_NOW - timedelta(days=5),
        period_end=FROZEN_NOW + timedelta(days=25),
        state=state,
    )


def _decide(settings: Settings, offer: PaymentOffer | None, **overrides: Any) -> Accept | Decline:
    args: dict[str, Any] = {
        "payer_id": PAYER,
        "currency": "XTR",
        "total_amount": PRICE,
        "now": FROZEN_NOW,
        "offer": offer,
        "linked": None,
        "payer_running": None,
        **overrides,
    }
    return decide(settings, **args)


def test_fresh_offer_is_accepted(payments: Settings) -> None:
    assert _decide(payments, _offer()) == Accept()


def test_payer_may_differ_from_the_offer_owner(payments: Settings) -> None:
    assert _decide(payments, _offer(owner=OTHER)) == Accept()


@pytest.mark.parametrize("state", [StarsSubState.active, StarsSubState.canceled])
def test_renewal_path_accepts_running_subscription_whatever_the_settings(
    settings: Settings, state: StarsSubState
) -> None:
    offer = _offer(age=timedelta(days=40), price=100)
    off = settings.model_copy(update={"payments_enabled": False, "stars_price": 999})
    result = _decide(off, offer, total_amount=100, linked=_stars(offer, state))
    assert result == Accept()


def test_offer_exactly_one_hour_old_is_still_accepted(payments: Settings) -> None:
    assert _decide(payments, _offer(age=timedelta(hours=1))) == Accept()


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("currency", DeclineReason.error),
        ("no_offer", DeclineReason.expired),
        ("old_offer", DeclineReason.expired),
        ("amount", DeclineReason.price),
        ("offer_price", DeclineReason.price),
        ("off", DeclineReason.off),
        ("no_price", DeclineReason.off),
        ("gate_off", DeclineReason.off),
        ("canceled_running", DeclineReason.running),
        ("other_payer_subscription", DeclineReason.running),
        ("ended_own_subscription", DeclineReason.expired),
    ],
)
def test_decline_reasons(payments: Settings, case: str, reason: DeclineReason) -> None:
    settings = payments
    offer: PaymentOffer | None = _offer()
    assert offer is not None
    overrides: dict[str, Any] = {}
    if case == "currency":
        overrides["currency"] = "USD"
    elif case == "no_offer":
        offer = None
    elif case == "old_offer":
        offer = _offer(age=timedelta(minutes=61))
    elif case == "amount":
        overrides["total_amount"] = PRICE + 1
    elif case == "offer_price":
        offer = _offer(price=PRICE - 50)
        overrides["total_amount"] = PRICE - 50
    elif case == "off":
        settings = payments.model_copy(update={"payments_enabled": False})
    elif case == "no_price":
        settings = payments.model_copy(update={"stars_price": None})
    elif case == "gate_off":
        settings = payments.model_copy(update={"access_gate": False})
    elif case == "canceled_running":
        overrides["payer_running"] = _stars(_offer(), StarsSubState.canceled)
    elif case == "other_payer_subscription":
        overrides["linked"] = _stars(offer, StarsSubState.active, payer=OTHER)
    elif case == "ended_own_subscription":
        overrides["linked"] = _stars(offer, StarsSubState.ended)
    assert _decide(settings, offer, **overrides) == Decline(reason)


def test_every_decline_reason_has_short_texts_in_both_languages() -> None:
    for catalog in CATALOGS.values():
        for reason in DeclineReason:
            assert len(str(catalog[f"pay.declined_{reason.value}"])) <= 255
