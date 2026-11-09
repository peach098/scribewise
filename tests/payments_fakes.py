"""A fake of :class:`scribe.payments.telegram.StarsApi` for the payments tests (specs/015).

Records every call, hands out ``https://t.me/$fake-<payload>`` links, serves a settable list of
:class:`IncomingStars` and raises :class:`StarsApiError` for the methods listed in ``fail``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import PaymentOffer, PaymentSource
from scribe.payments import credit
from scribe.payments.telegram import IncomingStars, StarsApiError


@dataclass
class FakeStarsApi:
    links: list[dict[str, object]] = field(default_factory=list)
    renewals: list[dict[str, object]] = field(default_factory=list)
    refunds: list[dict[str, object]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)  # method names in call order
    incoming: list[IncomingStars] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)  # method names that raise StarsApiError
    # Awaited when the method is entered, before it can fail: lets a test look at the database
    # at the moment Telegram is called.
    hooks: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)

    async def _enter(self, method: str) -> None:
        self.calls.append(method)
        if method in self.hooks:
            await self.hooks[method]()
        if method in self.fail:
            raise StarsApiError(f"fake {method} failure")

    async def create_subscription_link(
        self, *, payload: str, price: int, title: str, description: str, label: str
    ) -> str:
        await self._enter("create_subscription_link")
        self.links.append(
            {
                "payload": payload,
                "price": price,
                "title": title,
                "description": description,
                "label": label,
            }
        )
        return f"https://t.me/$fake-{payload}"

    async def set_renewal(self, *, telegram_id: int, first_charge_id: str, canceled: bool) -> None:
        await self._enter("set_renewal")
        self.renewals.append(
            {"telegram_id": telegram_id, "first_charge_id": first_charge_id, "canceled": canceled}
        )

    async def refund(self, *, telegram_id: int, charge_id: str) -> None:
        await self._enter("refund")
        self.refunds.append({"telegram_id": telegram_id, "charge_id": charge_id})

    async def incoming_transactions(self, *, since: datetime) -> list[IncomingStars]:
        await self._enter("incoming_transactions")
        return [entry for entry in self.incoming if entry.date >= since]


# --- rows credited the way the bot does it ------------------------------------------------------

MONTH = timedelta(days=30)


async def credit_first(
    db: AsyncSession,
    *,
    telegram_id: int,
    charge_id: str,
    at: datetime,
    amount: int = 150,
) -> credit.Credited:
    """A shown offer and its first payment, credited at ``at`` (the period runs 30 days)."""
    offer = PaymentOffer(telegram_id=telegram_id, price_stars=amount, created_at=at)
    db.add(offer)
    await db.flush()
    result = await credit.credit_payment(
        db,
        payer_id=telegram_id,
        charge_id=charge_id,
        amount=amount,
        payload=f"sub1:{offer.id}",
        paid_at=at,
        expires_at=at + MONTH,
        is_first=True,
        via=PaymentSource.update,
        now=at,
    )
    assert isinstance(result, credit.Credited)
    return result


async def credit_renewal(
    db: AsyncSession, first: credit.Credited, *, charge_id: str, at: datetime
) -> credit.Credited:
    """The next monthly charge of the subscription ``first`` started."""
    result = await credit.credit_payment(
        db,
        payer_id=first.stars.telegram_id,
        charge_id=charge_id,
        amount=first.payment.amount_stars,
        payload=f"sub1:{first.stars.offer_id}",
        paid_at=at,
        expires_at=first.stars.period_end + MONTH,
        is_first=False,
        via=PaymentSource.update,
        now=at,
    )
    assert isinstance(result, credit.Credited)
    return result
