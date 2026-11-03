"""Access rows and audit events (specs/005-beta-subscription-access/research.md Q5, Q10).

Every function takes the caller's ``AsyncSession`` and never commits. Rows are keyed by the
person's Telegram id (= ``users.telegram_control_chat_id``, research Q1). Nothing here stores
message content, names or usernames of refused senders (FR-007).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.access import policy
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    BetaEntry,
    RefusedContact,
    Subscription,
    User,
)

MAX_REASON_CHARS = 200
# ``access_events.telegram_id`` of events about a username-only beta entry that no one has
# linked yet (``beta add @name``); ``meta.beta_entry_id`` names the entry, and linking re-keys
# them to the person's Telegram id so they are exported and deleted with that person's data.
UNLINKED_TELEGRAM_ID = 0


def normalize_username(username: str | None) -> str | None:
    """``@Some_User`` -> ``some_user``; ``None`` for an empty value."""
    if username is None:
        return None
    value = username.strip().lstrip("@").strip().lower()
    return value or None


async def find_beta(
    session: AsyncSession, telegram_id: int, username: str | None, *, now: datetime
) -> BetaEntry | None:
    """The person's beta entry: by Telegram id, else an unlinked entry with their username.

    A username match is linked to the Telegram id in the caller's transaction and recorded as
    a ``beta_linked`` event (FR-009, FR-019); the entry's earlier events (keyed to
    :data:`UNLINKED_TELEGRAM_ID`) move to the Telegram id. Later username changes still match
    by id.
    """
    entry = await _beta_by_id(session, telegram_id)
    if entry is not None:
        return entry
    name = normalize_username(username)
    if name is None:
        return None
    # A conditional UPDATE links at most once, even if two updates of the same person race.
    linked: BetaEntry | None = await session.scalar(
        update(BetaEntry)
        .where(BetaEntry.telegram_id.is_(None), func.lower(BetaEntry.username) == name)
        .values(telegram_id=telegram_id, linked_at=now)
        .returning(BetaEntry)
    )
    if linked is None:
        # A concurrent update may have linked it already.
        return await _beta_by_id(session, telegram_id)
    await session.execute(
        update(AccessEvent)
        .where(
            AccessEvent.telegram_id == UNLINKED_TELEGRAM_ID,
            AccessEvent.meta_json["beta_entry_id"].astext == str(linked.id),
        )
        .values(telegram_id=telegram_id)
        .execution_options(synchronize_session=False)
    )
    await record_event(
        session,
        telegram_id,
        AccessEventKind.beta_linked,
        actor="system",
        meta={"beta_entry_id": linked.id},
        now=now,
    )
    return linked


async def _beta_by_id(session: AsyncSession, telegram_id: int) -> BetaEntry | None:
    result = await session.scalars(select(BetaEntry).where(BetaEntry.telegram_id == telegram_id))
    return result.one_or_none()


async def subscriptions_for(session: AsyncSession, telegram_id: int) -> list[Subscription]:
    """All of the person's subscription rows, oldest start first."""
    result = await session.scalars(
        select(Subscription)
        .where(Subscription.telegram_id == telegram_id)
        .order_by(Subscription.starts_at, Subscription.created_at)
    )
    return list(result.all())


async def access_status(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int,
    username: str | None,
    *,
    now: datetime,
) -> policy.AccessStatus:
    """Load the person's rows and apply :func:`scribe.access.policy.status`."""
    beta = await find_beta(session, telegram_id, username, now=now)
    subs = await subscriptions_for(session, telegram_id)
    return policy.status(beta, subs, is_operator=policy.is_operator(telegram_id, settings), now=now)


async def betas_by_id(session: AsyncSession, ids: Sequence[int]) -> dict[int, BetaEntry]:
    """Linked beta entries of these Telegram ids, keyed by id."""
    rows = await session.scalars(select(BetaEntry).where(BetaEntry.telegram_id.in_(ids)))
    return {row.telegram_id: row for row in rows.all() if row.telegram_id is not None}


async def subscriptions_by_id(
    session: AsyncSession, ids: Sequence[int]
) -> dict[int, list[Subscription]]:
    """Every subscription row of these Telegram ids, grouped by id."""
    rows = await session.scalars(select(Subscription).where(Subscription.telegram_id.in_(ids)))
    grouped: dict[int, list[Subscription]] = defaultdict(list)
    for row in rows.all():
        grouped[row.telegram_id].append(row)
    return grouped


async def bulk_status(
    session: AsyncSession, settings: Settings, users: Sequence[User], *, now: datetime
) -> dict[uuid.UUID, policy.AccessStatus]:
    """:func:`scribe.access.policy.status` of many accounts at once (keyed by ``users.id``),
    with two queries. Shared by the reconcile job and release notes (specs/011 research R4).
    Unlike :func:`access_status` it never links a username-only beta entry."""
    ids = [user.telegram_control_chat_id for user in users]
    betas = await betas_by_id(session, ids)
    subs = await subscriptions_by_id(session, ids)
    return {
        user.id: policy.status(
            betas.get(user.telegram_control_chat_id),
            subs.get(user.telegram_control_chat_id, []),
            is_operator=policy.is_operator(user.telegram_control_chat_id, settings),
            now=now,
        )
        for user in users
    }


async def record_event(
    session: AsyncSession,
    telegram_id: int,
    kind: AccessEventKind,
    *,
    actor: str,
    reason: str | None = None,
    meta: dict[str, Any] | None = None,
    now: datetime,
) -> AccessEvent:
    """Write one ``access_events`` row. ``meta`` holds ids and dates only (as strings)."""
    event = AccessEvent(
        telegram_id=telegram_id,
        kind=kind,
        actor=actor,
        reason=reason[:MAX_REASON_CHARS] if reason is not None else None,
        meta_json={key: _meta_value(value) for key, value in (meta or {}).items()},
        at=now,
    )
    session.add(event)
    await session.flush()
    return event


def _meta_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def touch_refused(
    session: AsyncSession, telegram_id: int, *, now: datetime, cooldown_s: int
) -> bool:
    """Count a refused update; True when the sender should be answered (FR-006, research Q5).

    The sender is answered when never answered before or last answered more than
    ``cooldown_s`` seconds ago; ``last_answered_at`` then moves to ``now``.
    """
    upsert = insert(RefusedContact).values(
        telegram_id=telegram_id, first_seen_at=now, last_seen_at=now, attempts=1
    )
    await session.execute(
        upsert.on_conflict_do_update(
            index_elements=[RefusedContact.telegram_id],
            set_={"attempts": RefusedContact.attempts + 1, "last_seen_at": now},
        )
    )
    # The upsert holds the row lock until the caller commits, so this check cannot race.
    answered = await session.scalar(
        update(RefusedContact)
        .where(
            RefusedContact.telegram_id == telegram_id,
            RefusedContact.last_answered_at.is_(None)
            | (RefusedContact.last_answered_at < now - timedelta(seconds=cooldown_s)),
        )
        .values(last_answered_at=now)
        .returning(RefusedContact.telegram_id)
    )
    return answered is not None
