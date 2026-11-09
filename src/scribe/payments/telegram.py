"""The only door to Telegram's payment methods (specs/015 contracts/internal-interfaces.md,
constitution VI: narrow interfaces for external services).

:class:`StarsApi` is what the payments code needs; :class:`AiogramStarsApi` implements it on a
``Bot``. Tests use ``tests/payments_fakes.FakeStarsApi``. Every Telegram error is raised as
:class:`StarsApiError`, so callers can leave their rows untouched on failure (design decision 5).
No logging here: invoice links and charge ids are never logged (research P14).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LabeledPrice, StarTransaction, TransactionPartnerUser

SUBSCRIPTION_PERIOD_S = 2592000  # 30 days; Telegram allows no other period (research P1)
PAGE_SIZE = 100  # getStarTransactions: at most 100 a page


class StarsApiError(Exception):
    """A Telegram payment call failed (network, rejection, unknown charge)."""


@dataclass(frozen=True, slots=True)
class IncomingStars:
    """One entry of ``getStarTransactions`` from a user: a payment, or its refund."""

    charge_id: str
    telegram_id: int
    amount: int
    payload: str | None
    date: datetime
    subscription_period: int | None
    is_refund: bool


class StarsApi(Protocol):
    async def create_subscription_link(
        self, *, payload: str, price: int, title: str, description: str, label: str
    ) -> str: ...

    async def set_renewal(
        self, *, telegram_id: int, first_charge_id: str, canceled: bool
    ) -> None: ...  # editUserStarSubscription

    async def refund(self, *, telegram_id: int, charge_id: str) -> None: ...

    async def incoming_transactions(self, *, since: datetime) -> list[IncomingStars]: ...


class AiogramStarsApi:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def create_subscription_link(
        self, *, payload: str, price: int, title: str, description: str, label: str
    ) -> str:
        try:
            return await self._bot.create_invoice_link(
                title=title,
                description=description,
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label=label, amount=price)],
                subscription_period=SUBSCRIPTION_PERIOD_S,
            )  # Stars need no provider token
        except TelegramAPIError as exc:
            raise StarsApiError(type(exc).__name__) from exc

    async def set_renewal(self, *, telegram_id: int, first_charge_id: str, canceled: bool) -> None:
        try:
            await self._bot.edit_user_star_subscription(
                user_id=telegram_id,
                telegram_payment_charge_id=first_charge_id,
                is_canceled=canceled,
            )
        except TelegramAPIError as exc:
            raise StarsApiError(type(exc).__name__) from exc

    async def refund(self, *, telegram_id: int, charge_id: str) -> None:
        try:
            await self._bot.refund_star_payment(
                user_id=telegram_id, telegram_payment_charge_id=charge_id
            )
        except TelegramAPIError as exc:
            raise StarsApiError(type(exc).__name__) from exc

    async def incoming_transactions(self, *, since: datetime) -> list[IncomingStars]:
        """Payments and refunds of users dated ``since`` or later. Telegram lists transactions
        oldest first, so every page is read and filtered (a few hundred a year, research P4)."""
        found: list[IncomingStars] = []
        offset = 0
        while True:
            try:
                page = await self._bot.get_star_transactions(offset=offset, limit=PAGE_SIZE)
            except TelegramAPIError as exc:
                raise StarsApiError(type(exc).__name__) from exc
            for transaction in page.transactions:
                entry = _map(transaction)
                if entry is not None and entry.date >= since:
                    found.append(entry)
            if len(page.transactions) < PAGE_SIZE:
                return found
            offset += len(page.transactions)


def _map(transaction: StarTransaction) -> IncomingStars | None:
    """A user's payment (``source`` is a user) or a refund to a user (``receiver`` is a user);
    anything else - ads, gifts, withdrawals - is not ours."""
    date = transaction.date
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    source, receiver = transaction.source, transaction.receiver
    if isinstance(source, TransactionPartnerUser) and source.transaction_type == "invoice_payment":
        return IncomingStars(
            charge_id=transaction.id,
            telegram_id=source.user.id,
            amount=transaction.amount,
            payload=source.invoice_payload,
            date=date,
            subscription_period=source.subscription_period,
            is_refund=False,
        )
    if isinstance(receiver, TransactionPartnerUser):
        # A refund carries the original charge's id (Bot API: StarTransaction.id).
        return IncomingStars(
            charge_id=transaction.id,
            telegram_id=receiver.user.id,
            amount=transaction.amount,
            payload=receiver.invoice_payload,
            date=date,
            subscription_period=receiver.subscription_period,
            is_refund=True,
        )
    return None
