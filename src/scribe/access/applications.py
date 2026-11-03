"""Beta applications (specs/005-beta-subscription-access US5, research Q12, Q13).

Every function takes the caller's ``AsyncSession`` and never commits. An application row is the
only access row that holds text the person wrote, and only after they pressed **Send
application** (FR-007, FR-023): the bot keeps the typed use case in the FSM until then.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.access import repo
from scribe.db.models import AccessEventKind, BetaApplication, BetaApplicationStatus, BetaEntry

REAPPLY_AFTER = timedelta(days=30)  # after a rejection (FR-024)
DECIDED_RETENTION = timedelta(days=30)  # decided applications are purged after this (FR-029)
MIN_USE_CASE = 10
MAX_USE_CASE = 1000
OPERATOR = "operator"
BETA_NOTE = "application"  # the note of a beta entry created by an approval


class BlockKind(enum.StrEnum):
    pending = "pending"
    rejected = "rejected"


@dataclass(frozen=True, slots=True)
class ApplyBlock:
    """Why the person cannot apply now; ``until`` is when a rejection stops blocking."""

    kind: BlockKind
    until: datetime | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of :func:`decide`. ``changed`` is False when the application was already
    decided (the first decision wins); ``application`` then shows that earlier decision."""

    application: BetaApplication
    changed: bool
    beta_added: bool = False


async def can_apply(session: AsyncSession, telegram_id: int, now: datetime) -> ApplyBlock | None:
    """``None`` when the person may apply: no pending application and no rejection in the last
    30 days."""
    pending = await session.scalar(
        select(BetaApplication.id).where(
            BetaApplication.telegram_id == telegram_id,
            BetaApplication.status == BetaApplicationStatus.pending,
        )
    )
    if pending is not None:
        return ApplyBlock(BlockKind.pending)
    latest_rejection: datetime | None = await session.scalar(
        select(func.max(BetaApplication.decided_at)).where(
            BetaApplication.telegram_id == telegram_id,
            BetaApplication.status == BetaApplicationStatus.rejected,
            BetaApplication.decided_at > now - REAPPLY_AFTER,
        )
    )
    if latest_rejection is None:
        return None
    return ApplyBlock(BlockKind.rejected, latest_rejection + REAPPLY_AFTER)


async def submit(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    use_case: str,
    language: str,
    now: datetime,
) -> BetaApplication:
    """Store a pending application (consent given ``now``) and record
    ``application_submitted``. A second pending one for the same person violates the partial
    unique index (``IntegrityError``); callers check :func:`can_apply` first."""
    application = BetaApplication(
        id=uuid.uuid4(),
        telegram_id=telegram_id,
        username=repo.normalize_username(username),
        use_case=use_case,
        language=language,
        consent_at=now,
        status=BetaApplicationStatus.pending,
        operator_messages=[],
    )
    session.add(application)
    await session.flush()
    await repo.record_event(
        session,
        telegram_id,
        AccessEventKind.application_submitted,
        actor="system",
        meta={"application_id": application.id},
        now=now,
    )
    return application


async def get(session: AsyncSession, application_id: uuid.UUID) -> BetaApplication | None:
    return await session.get(BetaApplication, application_id)


async def decide(
    session: AsyncSession,
    application_id: uuid.UUID,
    *,
    approve: bool,
    operator_id: int | None,
    now: datetime,
) -> Decision | None:
    """Approve or reject a pending application (``None`` if it does not exist).

    The row is locked with ``FOR UPDATE``, so of two concurrent decisions the second sees the
    first and changes nothing. An approval creates the person's beta entry unless one exists
    (``added_by="operator"``, note ``application``) and records ``beta_added``. ``operator_id``
    is the deciding operator's Telegram id, ``None`` from the CLI.
    """
    application = await session.scalar(
        select(BetaApplication)
        .where(BetaApplication.id == application_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if application is None:
        return None
    if application.status != BetaApplicationStatus.pending:
        return Decision(application, changed=False)
    application.status = (
        BetaApplicationStatus.approved if approve else BetaApplicationStatus.rejected
    )
    application.decided_at = now
    application.decided_by = operator_id
    meta: dict[str, object] = {"application_id": application.id}
    if operator_id is not None:
        meta["operator_id"] = operator_id
    await session.flush()
    await repo.record_event(
        session,
        application.telegram_id,
        AccessEventKind.application_approved if approve else AccessEventKind.application_rejected,
        actor=OPERATOR,
        meta=meta,
        now=now,
    )
    beta_added = False
    if approve:
        entry_id = await session.scalar(
            insert(BetaEntry)
            .values(
                id=uuid.uuid4(),
                telegram_id=application.telegram_id,
                note=BETA_NOTE,
                added_by=OPERATOR,
                added_at=now,
            )
            .on_conflict_do_nothing(index_elements=[BetaEntry.telegram_id])
            .returning(BetaEntry.id)
        )
        if entry_id is not None:  # not on the list yet
            beta_added = True
            await repo.record_event(
                session,
                application.telegram_id,
                AccessEventKind.beta_added,
                actor=OPERATOR,
                reason=BETA_NOTE,
                meta={"beta_entry_id": entry_id, "application_id": application.id},
                now=now,
            )
    return Decision(application, changed=True, beta_added=beta_added)


async def add_operator_messages(
    session: AsyncSession, application_id: uuid.UUID, messages: list[list[int]]
) -> None:
    """Remember the ``[chat_id, message_id]`` copies sent to operators."""
    application = await session.get(BetaApplication, application_id, with_for_update=True)
    if application is not None:
        application.operator_messages = [*application.operator_messages, *messages]


async def list_applications(
    session: AsyncSession, *, include_decided: bool, now: datetime
) -> list[BetaApplication]:
    """Pending applications, oldest first; with ``include_decided`` also those decided in the
    last 30 days (older ones are purged)."""
    pending = BetaApplication.status == BetaApplicationStatus.pending
    where = (
        pending | (BetaApplication.decided_at >= now - DECIDED_RETENTION)
        if include_decided
        else pending
    )
    result = await session.scalars(
        select(BetaApplication)
        .where(where)
        .order_by(BetaApplication.consent_at, BetaApplication.id)
    )
    return list(result.all())


async def for_person(session: AsyncSession, telegram_id: int) -> list[BetaApplication]:
    """The person's own applications, oldest first (``/my_data``, the export)."""
    result = await session.scalars(
        select(BetaApplication)
        .where(BetaApplication.telegram_id == telegram_id)
        .order_by(BetaApplication.consent_at, BetaApplication.id)
    )
    return list(result.all())


__all__ = [
    "DECIDED_RETENTION",
    "MAX_USE_CASE",
    "MIN_USE_CASE",
    "REAPPLY_AFTER",
    "ApplyBlock",
    "BlockKind",
    "Decision",
    "add_operator_messages",
    "can_apply",
    "decide",
    "for_person",
    "get",
    "list_applications",
    "submit",
]
