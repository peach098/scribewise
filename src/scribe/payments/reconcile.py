"""``stars_reconcile``: credit what polling lost (specs/015-telegram-stars-payments research P4,
SC-002).

aiogram polling confirms an update by the next ``getUpdates`` offset, so a crash while a
``successful_payment`` is being handled can lose it. Every 10 minutes (and once at start) this job
reads ``getStarTransactions`` for the last 7 days and credits any payment whose charge id is
missing through the same :func:`scribe.payments.credit.credit_payment`, keyed by the unique charge
id, so running it twice changes nothing. A backfilled first payment tells the person
(``pay.thanks``); a renewal is silent. The job does nothing, and makes no Telegram call, while
payments are not available and no Stars subscription runs. Logs hold counts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings, payments_available
from scribe.db.models import PaymentSource, User
from scribe.db.repos import users as users_repo
from scribe.i18n import DEFAULT_LANG, normalize_lang
from scribe.logging import get_logger
from scribe.payments import credit, repo
from scribe.payments.bot import refunded_text, thanks_text
from scribe.payments.checkout import OFFER_TTL
from scribe.payments.offers import parse_payload
from scribe.payments.telegram import IncomingStars, StarsApi, StarsApiError
from scribe.scheduling.scheduler import Every, Scheduler

log = get_logger("scribe.payments")

STARS_RECONCILE_JOB = "stars_reconcile"
STARS_RECONCILE_SCHEDULE = Every(timedelta(minutes=10))
LOOKBACK_DAYS = 7
# A first payment follows its offer within the offer's hour; allow for clock skew.
FIRST_PAYMENT_WINDOW = OFFER_TTL * 2


@dataclass(frozen=True)
class StarsReconcileResult:
    checked: int = 0
    credited: int = 0
    refunds: int = 0


async def reconcile_stars(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    api: StarsApi,
    bot: Bot,
    *,
    clock: Clock = utcnow,
    days: int = LOOKBACK_DAYS,
    force: bool = False,
) -> StarsReconcileResult:
    """One run of the job; see the module docstring. ``force`` (``pay sync``) runs it even
    while payments are off and nothing runs."""
    now = clock()
    async with session_factory() as session:
        idle = not payments_available(settings) and not await repo.any_running(session)
        if idle and not force:
            return StarsReconcileResult()
    try:
        entries = await api.incoming_transactions(since=now - timedelta(days=days))
    except StarsApiError as exc:
        log.warning("stars_reconcile", result="error", error=str(exc))
        return StarsReconcileResult()

    credited = refunds = 0
    # Oldest first, so a first payment is credited before its renewals.
    for entry in sorted(entries, key=lambda item: (item.date, item.is_refund)):
        if not entry.is_refund and (entry.payload is None or parse_payload(entry.payload) is None):
            continue  # not ours (a refund is looked up by its charge id)
        try:
            if entry.is_refund:
                refunds += await _apply_refund(session_factory, bot, entry, clock)
            else:
                credited += await _credit(session_factory, bot, entry, clock)
        except Exception as exc:  # one bad entry never blocks the others
            log.error("stars_reconcile", result="entry_error", error=type(exc).__name__)
    result = StarsReconcileResult(len(entries), credited, refunds)
    log.info(
        "stars_reconcile", checked=result.checked, credited=result.credited, refunds=result.refunds
    )
    return result


async def _credit(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    entry: IncomingStars,
    clock: Clock,
) -> int:
    assert entry.payload is not None
    offer_id = parse_payload(entry.payload)
    now = clock()
    async with session_factory() as session:
        if await repo.by_charge(session, entry.charge_id) is not None:
            return 0
        offer = await repo.get_offer(session, offer_id) if offer_id is not None else None
        stars = await repo.by_offer(session, offer.id) if offer is not None else None
        # A transaction does not say whether it was the first charge: it is, when no
        # subscription exists yet and it follows its offer closely.
        is_first = (
            stars is None
            and offer is not None
            and entry.date - offer.created_at <= FIRST_PAYMENT_WINDOW
        )
        expires_at = (
            entry.date + timedelta(seconds=entry.subscription_period)
            if entry.subscription_period
            else None
        )
        result = await credit.credit_payment(
            session,
            payer_id=entry.telegram_id,
            charge_id=entry.charge_id,
            amount=entry.amount,
            payload=entry.payload,
            paid_at=entry.date,
            expires_at=expires_at,
            is_first=is_first,
            via=PaymentSource.backfill,
            now=now,
        )
        user = await users_repo.get_by_control_chat(session, entry.telegram_id)
        await session.commit()
    if isinstance(result, credit.AlreadyCredited):
        return 0
    if isinstance(result, credit.Credited) and result.first:
        await _tell(bot, entry.telegram_id, user, result.stars.period_end, entry.amount)
    return 1


async def _apply_refund(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    entry: IncomingStars,
    clock: Clock,
) -> int:
    async with session_factory() as session:
        result = await credit.apply_refund(
            session, charge_id=entry.charge_id, by="telegram", now=clock()
        )
        user = await users_repo.get_by_control_chat(session, entry.telegram_id)
        await session.commit()
    if not isinstance(result, credit.Refunded):
        return 0  # unknown here, or applied already
    lang = normalize_lang(user.language) if user is not None else DEFAULT_LANG
    try:
        await bot.send_message(entry.telegram_id, refunded_text(result.payment, user, lang))
    except Exception as exc:  # blocked bot, network: the refund is applied regardless
        log.warning("stars_reconcile", result="notice_error", error=type(exc).__name__)
    return 1


async def _tell(
    bot: Bot, chat_id: int, user: User | None, period_end: datetime, amount: int
) -> None:
    lang = normalize_lang(user.language) if user is not None else DEFAULT_LANG
    text = thanks_text(period_end, amount, user, lang)
    try:
        await bot.send_message(chat_id, text)
    except Exception as exc:  # blocked bot, network: the payment is credited regardless
        log.warning("stars_reconcile", result="notice_error", error=type(exc).__name__)


def register_stars_reconcile(
    scheduler: Scheduler,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    api: StarsApi,
    bot: Bot,
    clock: Clock = utcnow,
) -> None:
    """Register ``stars_reconcile`` (every 10 minutes, due at once at start)."""

    async def job() -> None:
        await reconcile_stars(session_factory, settings, api, bot, clock=clock)

    scheduler.register_job(STARS_RECONCILE_JOB, STARS_RECONCILE_SCHEDULE, job, run_at_start=True)
