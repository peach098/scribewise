"""What the control bot shows about a subscription (specs/015 contracts/control-chat.md): the
**Subscription** screen (an offer, the manage view of a running subscription, or today's
placeholder) and the ``/account`` view. Rendering only; handlers live in
:mod:`scribe.payments.bot` and :mod:`scribe.access.apply`.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.access import repo as access_repo
from scribe.config import Settings, payments_available
from scribe.db.models import StarsSubState, SubscriptionSource, User
from scribe.i18n import format_day_month_year, t
from scribe.payments import offers, repo
from scribe.payments.offers import Offer, Running, Unavailable, price_text
from scribe.payments.telegram import StarsApi
from scribe.telegram.control_bot.render import button, keyboard


def day(moment: datetime, tz: str, lang: str) -> str:
    """The date in the person's time zone (UTC for people without an account)."""
    return format_day_month_year(moment.astimezone(ZoneInfo(tz)).date(), lang)


def placeholder_text(settings: Settings, lang: str) -> str:
    """Today's text when payments are not available (specs/005 ``subscription_placeholder``)."""
    text = t("access.subscription_placeholder", lang)
    if settings.subscription_contact:
        line = t(
            "access.subscription_line", lang, subscription_contact=settings.subscription_contact
        )
        text = f"{text}\n{line}"
    return text


async def account_view(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int,
    *,
    user: User | None,
    lang: str,
    now: datetime,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """``pay.account``: the access status, the Stars subscription or operator subscription, and
    the buttons that fit (cancel, turn renewal back on, or **Subscription**)."""
    tz = user.timezone if user is not None else "UTC"
    status = await access_repo.access_status(session, settings, telegram_id, None, now=now)
    lines = [t("access.mydata_title", lang, status=t(f"access.mydata_status_{status.kind}", lang))]
    stars = await repo.running_for(session, telegram_id)
    rows = []
    if stars is not None and stars.state is StarsSubState.active:
        lines.append(
            t(
                "pay.account_renews",
                lang,
                date=day(stars.period_end, tz, lang),
                price=price_text(stars.price_stars),
            )
        )
        rows.append([button(t("btn.pay_cancel", lang), "pay:cancel")])
    elif stars is not None:
        lines.append(t("pay.account_canceled", lang, date=day(stars.period_end, tz, lang)))
        rows.append([button(t("btn.pay_resume", lang), "pay:resume")])
    else:
        operator_ends = [
            sub.ends_at
            for sub in await access_repo.subscriptions_for(session, telegram_id)
            if sub.source is SubscriptionSource.operator
            and sub.cancelled_at is None
            and sub.starts_at <= now < sub.ends_at
        ]
        if operator_ends:
            lines.append(
                t("pay.account_operator_sub", lang, date=day(max(operator_ends), tz, lang))
            )
        if payments_available(settings):
            rows.append([button(t("btn.pay_subscription", lang), "pay:offer")])
    lines += ["", t("pay.account_help", lang)]
    return t("pay.account", lang, lines="\n".join(lines)), keyboard(rows) if rows else None


async def subscription_screen(
    session: AsyncSession,
    api: StarsApi | None,
    settings: Settings,
    telegram_id: int,
    *,
    user: User | None,
    lang: str,
    now: datetime,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The reply to **Subscription**: an offer (a new invoice link; the caller commits), the
    manage view when a Stars subscription runs, or the placeholder when payments are not
    available. May raise :class:`scribe.payments.telegram.StarsApiError`."""
    if api is None:
        return placeholder_text(settings, lang), None
    result = await offers.make_offer(session, api, settings, telegram_id, lang=lang, now=now)
    if isinstance(result, Unavailable):
        return placeholder_text(settings, lang), None
    if isinstance(result, Running):
        return await account_view(session, settings, telegram_id, user=user, lang=lang, now=now)
    assert isinstance(result, Offer)
    access = await access_repo.access_status(session, settings, telegram_id, None, now=now)
    return offers.offer_view(
        result, lang, access=access, tz=user.timezone if user is not None else "UTC"
    )
