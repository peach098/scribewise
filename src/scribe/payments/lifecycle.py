"""The life of a Stars subscription (specs/015-telegram-stars-payments research P5, FR-008..FR-011;
data-model.md "State transitions").

- :func:`cancel_renewal` / :func:`resume_renewal`: **Telegram first, then the row**. The
  ``editUserStarSubscription`` call comes first; if Telegram refuses (:class:`StarsApiError`)
  nothing changes locally, so we never believe a subscription is cancelled while Telegram goes
  on charging it.
- :func:`on_subscription_update`: changes made inside Telegram (``canceled``, ``active``,
  ``failed``) arrive as ``subscription`` updates; the row follows them.
- :func:`end_due`: called by ``access_reconcile``. A ``canceled`` subscription ends at its
  ``period_end``; an ``active`` one with no renewal one hour after ``period_end``
  (``STARS_GRACE``) ends as ``expired`` (FR-011).

Every change writes an ``access_events`` row (ids and reasons only) and a
``stars_subscription_state`` log line. The caller commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.access import repo as access_repo
from scribe.access.policy import STARS_GRACE
from scribe.db.models import (
    AccessEventKind,
    StarsCancelSource,
    StarsEndReason,
    StarsSubscription,
    StarsSubState,
)
from scribe.logging import get_logger
from scribe.payments import repo
from scribe.payments.offers import parse_payload
from scribe.payments.telegram import StarsApi

log = get_logger("scribe.payments")

_ACTORS = {
    StarsCancelSource.user_bot: "user",
    StarsCancelSource.deletion: "user",
    StarsCancelSource.telegram: "system",
    StarsCancelSource.refund: "operator",
    StarsCancelSource.operator: "operator",
}


@dataclass(frozen=True, slots=True)
class Canceled:
    stars: StarsSubscription


@dataclass(frozen=True, slots=True)
class NothingRunning:
    pass


@dataclass(frozen=True, slots=True)
class Resumed:
    stars: StarsSubscription


@dataclass(frozen=True, slots=True)
class NothingToResume:
    pass


@dataclass(frozen=True, slots=True)
class EndedStars:
    telegram_id: int
    stars_id: uuid.UUID
    reason: StarsEndReason


def _log_state(old: StarsSubState, new: StarsSubState, by: str) -> None:
    log.info("stars_subscription_state", from_=old.value, to=new.value, by=by)


async def _cancel_row(
    session: AsyncSession,
    stars: StarsSubscription,
    by: StarsCancelSource,
    now: datetime,
    *,
    reason: str | None = None,
) -> None:
    _log_state(stars.state, StarsSubState.canceled, by.value)
    stars.state = StarsSubState.canceled
    stars.canceled_at = now
    stars.canceled_by = by
    await access_repo.record_event(
        session,
        stars.telegram_id,
        AccessEventKind.stars_canceled,
        actor=_ACTORS[by],
        reason=reason,
        meta={"by": by.value},
        now=now,
    )


async def cancel_renewal(
    session: AsyncSession,
    api: StarsApi,
    *,
    telegram_id: int,
    by: StarsCancelSource,
    now: datetime,
    note: str | None = None,
) -> Canceled | NothingRunning:
    """Stop renewal of the person's ``active`` Stars subscription. Raises
    :class:`StarsApiError` (and changes nothing) when Telegram refuses."""
    stars = await repo.running_for(session, telegram_id)
    if stars is None or stars.state is not StarsSubState.active:
        return NothingRunning()
    await api.set_renewal(
        telegram_id=telegram_id, first_charge_id=stars.first_charge_id, canceled=True
    )
    await _cancel_row(session, stars, by, now, reason=note)
    return Canceled(stars)


async def resume_renewal(
    session: AsyncSession, api: StarsApi, *, telegram_id: int, now: datetime
) -> Resumed | NothingToResume:
    """Turn renewal back on for a ``canceled`` subscription before its ``period_end``."""
    stars = await repo.running_for(session, telegram_id)
    if stars is None or stars.state is not StarsSubState.canceled or now >= stars.period_end:
        return NothingToResume()
    await api.set_renewal(
        telegram_id=telegram_id, first_charge_id=stars.first_charge_id, canceled=False
    )
    await _resume_row(session, stars, "user_bot", now)
    return Resumed(stars)


async def _resume_row(
    session: AsyncSession, stars: StarsSubscription, by: str, now: datetime
) -> None:
    _log_state(stars.state, StarsSubState.active, by)
    stars.state = StarsSubState.active
    stars.canceled_at = None
    stars.canceled_by = None
    await access_repo.record_event(
        session,
        stars.telegram_id,
        AccessEventKind.stars_resumed,
        actor="user" if by == "user_bot" else "system",
        meta={"by": by},
        now=now,
    )


async def on_subscription_update(
    session: AsyncSession, *, payer_id: int, payload: str, state: str, now: datetime
) -> None:
    """Follow a ``subscription`` update. The subscription is found by the payload's offer and
    must belong to the payer; anything else, and unknown states, are logged and ignored."""
    offer_id = parse_payload(payload)
    stars = await repo.by_offer(session, offer_id) if offer_id is not None else None
    if stars is None or stars.telegram_id != payer_id:
        log.info("stars_subscription_state", by="telegram", result="ignored", reason="unknown")
        return
    if state == "canceled":
        if stars.state is StarsSubState.active:
            await _cancel_row(session, stars, StarsCancelSource.telegram, now)
    elif state == "active":
        if stars.state is StarsSubState.canceled and now < stars.period_end:
            await _resume_row(session, stars, "telegram", now)
    elif state == "failed":
        if stars.state in (StarsSubState.active, StarsSubState.canceled):
            # The paid period stays in place; the subscription ends with it (FR-010).
            _log_state(stars.state, StarsSubState.ended, "failed")
            stars.state = StarsSubState.ended
            stars.ended_at = stars.period_end
            stars.ended_reason = StarsEndReason.failed
            await access_repo.record_event(
                session,
                stars.telegram_id,
                AccessEventKind.stars_failed,
                actor="system",
                meta={"by": "telegram"},
                now=now,
            )
    else:
        log.info("stars_subscription_state", by="telegram", result="ignored", reason="state")


async def end_due(session: AsyncSession, *, now: datetime) -> list[EndedStars]:
    """End ``canceled`` subscriptions past ``period_end`` and ``active`` ones with no renewal
    one grace hour after it (``expired``). Returns what ended."""
    due = (
        await session.scalars(
            select(StarsSubscription)
            .where(
                StarsSubscription.state.in_(repo.RUNNING_STATES),
                StarsSubscription.period_end <= now,
            )
            .order_by(StarsSubscription.period_end)
            .with_for_update()
        )
    ).all()
    ended: list[EndedStars] = []
    for stars in due:
        if stars.state is StarsSubState.canceled:
            reason = StarsEndReason.canceled
        elif stars.period_end + STARS_GRACE <= now:
            reason = StarsEndReason.expired
        else:
            continue  # active, inside the grace hour: a renewal may still arrive
        _log_state(stars.state, StarsSubState.ended, reason.value)
        stars.state = StarsSubState.ended
        stars.ended_at = now
        stars.ended_reason = reason
        await access_repo.record_event(
            session,
            stars.telegram_id,
            AccessEventKind.stars_ended,
            actor="system",
            meta={"reason": reason.value},
            now=now,
        )
        ended.append(EndedStars(stars.telegram_id, stars.id, reason))
    return ended
