"""Pure access policy (specs/005-beta-subscription-access/research.md Q3).

No DB access and no clock reads: callers load the rows and pass ``now``. Used by the bot gate,
the reconciliation job, the operator CLI and the send-time check.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from scribe.config import Settings
from scribe.db.models import BetaEntry, Subscription, SubscriptionSource

# specs/015 research P5: a renewing Stars period stays active this long after its end, so a
# renewal confirmed a little late never cuts access (SC-004). Never granted to other rows.
STARS_GRACE = timedelta(hours=1)


class AccessKind(enum.StrEnum):
    operator = "operator"
    beta = "beta"
    subscribed = "subscribed"
    beta_and_subscribed = "beta_and_subscribed"
    none = "none"


@dataclass(frozen=True, slots=True)
class AccessStatus:
    kind: AccessKind
    until: datetime | None = None  # latest ends_at among active subscriptions

    @property
    def has_access(self) -> bool:
        return self.kind is not AccessKind.none


def subscription_active(
    sub: Subscription, now: datetime, renewing_payment_ids: frozenset[uuid.UUID] = frozenset()
) -> bool:
    """Active when not cancelled and ``starts_at <= now < ends_at``; a ``stars`` row whose
    payment is in ``renewing_payment_ids`` (its Stars subscription is ``active``) lasts
    :data:`STARS_GRACE` longer."""
    if sub.cancelled_at is not None or now < sub.starts_at:
        return False
    grace = (
        STARS_GRACE
        if sub.source is SubscriptionSource.stars and sub.payment_id in renewing_payment_ids
        else timedelta(0)
    )
    return now < sub.ends_at + grace


def subscription_state(sub: Subscription, now: datetime) -> str:
    """``cancelled``, ``scheduled`` (not started), ``ended`` (``now >= ends_at``) or ``active``."""
    if sub.cancelled_at is not None:
        return "cancelled"
    if now < sub.starts_at:
        return "scheduled"
    if now >= sub.ends_at:
        return "ended"
    return "active"


def status(
    beta: BetaEntry | None,
    subs: Sequence[Subscription],
    *,
    is_operator: bool,
    now: datetime,
    renewing_payment_ids: frozenset[uuid.UUID] = frozenset(),
) -> AccessStatus:
    """The person's access from their beta entry and subscriptions (data-model.md).
    ``until`` is the latest real ``ends_at``, without any grace."""
    if is_operator:
        return AccessStatus(AccessKind.operator)
    active_ends = [
        sub.ends_at for sub in subs if subscription_active(sub, now, renewing_payment_ids)
    ]
    until = max(active_ends) if active_ends else None
    if beta is not None:
        kind = AccessKind.beta_and_subscribed if until is not None else AccessKind.beta
    else:
        kind = AccessKind.subscribed if until is not None else AccessKind.none
    return AccessStatus(kind, until)


def is_operator(telegram_id: int, settings: Settings) -> bool:
    """Operators always have access (FR-022, research Q8)."""
    return telegram_id in settings.operator_telegram_ids
