"""What is stored about one user: the ``/my_data`` summary and export, and account deletion
(FR-026, FR-030; Constitution II).

- :func:`data_summary` counts the caller's rows by type with the oldest item and when the
  retention job next removes something (messages 30 days after they were sent, draft text 30 days
  after the draft's decision, closed commitments 30 days after closing, retired and previous
  profile versions 30 days after they were replaced; the undo target of the latest refresh is
  kept while that refresh can be undone).
- :func:`export_user_data` returns every row of the caller, grouped by table, decrypted, as
  JSON-ready values: the user row, Telegram session status (never the session string), work
  chats, contacts, messages, conversation turns, drafts (with the decrypted draft content and
  the cited earlier messages of specs/004), style profiles, rule proposals, commitments, the
  task source connection (never its credentials), activity records and the control-chat FSM
  state (its state name and time, never its ``data``: a connect flow keeps pending Trello/Jira
  credentials there, issue #56). Every query is scoped to ``user_id``. The
  ``access`` key holds the person's access rows keyed to their Telegram id (specs/005 T032):
  ``beta``, ``subscriptions`` and ``events``, ids and dates only (no operator note or reason),
  and ``applications``, their beta applications with the use case they wrote (US5; not the
  deciding operator's id).
- :func:`data_summary` also gives the **Access** section: the status kind, beta since, each
  subscription (start, end, state), each beta application (submitted, status, decided) and the
  count of access events.
- :func:`delete_user_data` deletes the user row; every per-user table cascades and a trigger
  removes the FSM row (migration 0001). In the same transaction it deletes the access rows keyed
  to the person's Telegram id (``beta_entries``, ``subscriptions``, ``access_events``,
  ``refused_contacts``, ``beta_applications``; specs/005 research Q10, FR-029), logging only
  their counts. The user's release notes deliveries (specs/011) cascade with the user row; the
  announcements hold only operator-written text and are kept.
- The daily LLM spend (``llm_spend``, issue #67) is operator accounting (counts and cost, no
  content): it is not in the export, is deleted with the account (cascade) and is purged after
  ``SCRIBE_LLM_SPEND_RETENTION_DAYS`` by the retention job.
- Personal data encrypted at rest (issue #57: user and contact names, chat titles, the
  out-of-office text, the Jira identity and site in the connection config, Jira comment links,
  beta application username and use case, the Telegram session's last error) is read through
  the column types like every other encrypted column, so the export holds the decrypted values.
- The grammatical gender for drafts (issue #46) is a column of the user row: exported with it,
  shown in the :func:`data_summary` (``grammatical_gender``) and deleted with the account.
- Release notes (specs/011 FR-024): the export's ``release_notes`` key lists the user's
  deliveries (versions of the announcement, state, delivered at), and :func:`data_summary`
  counts the ones received (``sent`` or ``unknown``).

- Auto-send (specs/014): the user's and chats' new columns are exported with their rows, and the
  ``draft_decisions`` key lists what the user decided on each draft (a code, a flag, a time and
  ids, never text); :func:`data_summary` counts them (``decisions``), and the retention job
  deletes them 31 days after the decision.

Nothing here logs content.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.access import applications as applications_repo
from scribe.access import policy
from scribe.access import repo as access_repo
from scribe.db.models import (
    RELEASE_RECEIVED_STATES,
    AccessEvent,
    ActivityRecord,
    BetaApplication,
    BetaEntry,
    BotFsmState,
    Commitment,
    Contact,
    ConversationTurn,
    Draft,
    DraftDecision,
    Message,
    ProfileState,
    RefusedContact,
    ReleaseDelivery,
    StyleProfile,
    StyleRuleProposal,
    Subscription,
    TaskSourceConnection,
    User,
    WorkChat,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.stored_output import load_stored_output
from scribe.logging import get_logger
from scribe.release_notes import repo as release_notes_repo

log = get_logger("scribe.retention")

DERIVED_RETENTION = timedelta(days=30)
DECISION_RETENTION = timedelta(days=31)  # specs/014: draft_decisions (the 30-day rate window + 1)
EXPORT_FORMAT_VERSION = 1
# Keyed by the person's Telegram id (= users.telegram_control_chat_id), no FK to users.
ACCESS_TABLES: tuple[type[Any], ...] = (
    BetaEntry,
    Subscription,
    AccessEvent,
    RefusedContact,
    BetaApplication,
)
# Secrets are never exported: they are credentials, not data about the user (column names).
EXCLUDED_COLUMNS = {
    "telegram_sessions": {"session_string"},
    "task_source_connections": {"credentials"},
    # Issue #56: an open Trello/Jira connect flow keeps the pending key or token here.
    "bot_fsm_state": {"data"},
}
PER_USER_TABLES: tuple[type[Any], ...] = (
    WorkChat,
    Contact,
    Message,
    ConversationTurn,
    Draft,
    StyleProfile,
    StyleRuleProposal,
    Commitment,
    ActivityRecord,
    DraftDecision,  # specs/014: ids, a code and a flag per decision, no text
)


def _value(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_value(v) for v in value]
    return value


def row_dict(row: Any) -> dict[str, Any]:
    """Every mapped column of ``row`` (decrypted by the column types), minus secrets."""
    mapper = inspect(type(row))
    table = mapper.local_table.name
    excluded = EXCLUDED_COLUMNS.get(table, set())
    return {
        attr.key: _value(getattr(row, attr.key))
        for attr in mapper.column_attrs
        if attr.key not in excluded and not {c.name for c in attr.columns} & excluded
    }


def _draft_dict(draft: Draft) -> dict[str, Any]:
    data = row_dict(draft)
    stored = load_stored_output(draft.output_json, draft.text)
    data["output_json"] = {
        "output": stored.output.model_dump(mode="json") if stored.output is not None else None,
        "system_warnings": stored.system_warnings,
        "reply_to_message_id": stored.reply_to_message_id,
        # specs/004: the earlier messages the draft cites, decrypted (chat, sender, excerpt).
        "earlier_messages": [_value(asdict(item)) for item in stored.earlier_messages],
    }
    return data


# --- access rows (specs/005 T032) ----------------------------------------------------------------


async def _access_rows(
    session: AsyncSession, telegram_id: int, now: datetime
) -> tuple[BetaEntry | None, list[Subscription], list[AccessEvent]]:
    """The person's own access rows. Events of unlinked username-only entries are keyed to
    :data:`access_repo.UNLINKED_TELEGRAM_ID` and never belong to anyone's data."""
    if telegram_id == access_repo.UNLINKED_TELEGRAM_ID:
        return None, [], []
    beta = await access_repo.find_beta(session, telegram_id, None, now=now)  # by id only
    subs = await access_repo.subscriptions_for(session, telegram_id)
    events = (
        await session.scalars(
            select(AccessEvent)
            .where(AccessEvent.telegram_id == telegram_id)
            .order_by(AccessEvent.at, AccessEvent.id)
        )
    ).all()
    return beta, subs, list(events)


def _access_export(
    beta: BetaEntry | None,
    subs: list[Subscription],
    events: list[AccessEvent],
    apps: list[BetaApplication],
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "beta": (
            [
                {
                    "id": _value(beta.id),
                    "telegram_id": beta.telegram_id,
                    "added_at": _value(beta.added_at),
                    "linked_at": _value(beta.linked_at),
                }
            ]
            if beta is not None
            else []
        ),
        "subscriptions": [
            {
                "id": _value(sub.id),
                "starts_at": _value(sub.starts_at),
                "ends_at": _value(sub.ends_at),
                "cancelled_at": _value(sub.cancelled_at),
                "reminded_at": _value(sub.reminded_at),
                "created_at": _value(sub.created_at),
                "state": policy.subscription_state(sub, now),
            }
            for sub in subs
        ],
        "events": [
            {
                "id": _value(event.id),
                "kind": _value(event.kind),
                "actor": event.actor,
                "at": _value(event.at),
                "meta": _value(event.meta_json),
            }
            for event in events
        ],
        "applications": [
            {
                "id": _value(app.id),
                "status": _value(app.status),
                "username": app.username,
                "use_case": app.use_case,
                "language": app.language,
                "consent_at": _value(app.consent_at),
                "decided_at": _value(app.decided_at),
            }
            for app in apps
        ],
    }


async def export_user_data(
    session: AsyncSession, user_id: uuid.UUID, *, now: datetime
) -> dict[str, Any]:
    user = await users_repo.get_user(session, user_id)
    if user is None:
        raise LookupError("user not found")
    export: dict[str, Any] = {
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": now.isoformat(),
        "users": [row_dict(user)],
    }
    telegram = await users_repo.get_telegram_session(session, user_id)
    export["telegram_sessions"] = [row_dict(telegram)] if telegram is not None else []
    connection = await session.scalar(
        select(TaskSourceConnection).where(TaskSourceConnection.user_id == user_id)
    )
    export["task_source_connections"] = [row_dict(connection)] if connection is not None else []
    for model in PER_USER_TABLES:
        rows = (
            (
                await session.execute(
                    select(model).where(model.user_id == user_id).order_by(model.created_at)
                )
            )
            .scalars()
            .all()
        )
        table = inspect(model).local_table.name
        export[table] = [_draft_dict(r) if model is Draft else row_dict(r) for r in rows]
    fsm = await session.get(BotFsmState, user.telegram_control_chat_id)
    export["bot_fsm_state"] = [row_dict(fsm)] if fsm is not None else []
    beta, subs, events = await _access_rows(session, user.telegram_control_chat_id, now)
    apps = await applications_repo.for_person(session, user.telegram_control_chat_id)
    export["access"] = _access_export(beta, subs, events, apps, now)
    export["release_notes"] = [
        {
            "versions": covered,
            "state": delivery.state.value,
            "delivered_at": _value(delivery.delivered_at),
        }
        for delivery, covered in await release_notes_repo.deliveries_of_user(session, user_id)
    ]
    return export


@dataclass(frozen=True)
class TableSummary:
    count: int
    oldest: datetime | None = None
    next_removal: datetime | None = None  # when the retention job next removes something


@dataclass(frozen=True)
class SubscriptionSummary:
    starts_at: datetime
    ends_at: datetime
    state: str  # active | ended | cancelled | scheduled


@dataclass(frozen=True)
class ApplicationSummary:
    submitted_at: datetime  # consent_at
    status: str  # pending | approved | rejected
    decided_at: datetime | None


@dataclass(frozen=True)
class AccessSummary:
    kind: str  # policy.AccessKind value
    beta_since: datetime | None
    subscriptions: tuple[SubscriptionSummary, ...]
    events: int
    applications: tuple[ApplicationSummary, ...] = ()


@dataclass(frozen=True)
class DataSummary:
    messages: TableSummary
    drafts: TableSummary
    profiles: TableSummary
    profiles_by_state: dict[str, int]
    proposals: int
    commitments_open: int
    commitments: TableSummary  # closed ones and their removal date
    activity: TableSummary
    contacts: int
    work_chats: int
    telegram_status: str | None
    task_source: str | None  # "trello (active)"
    access: AccessSummary | None = None
    # specs/008: work account mode; picked-up chats (origin auto) and excluded chats
    work_account: bool = False
    work_account_since: datetime | None = None
    auto_chats: int = 0
    excluded_chats: int = 0
    release_notes: int = 0  # specs/011: release notes received (sent or unknown)
    grammatical_gender: str | None = None  # issue #46: users.grammatical_gender (None: unset)
    decisions: TableSummary = TableSummary(0)  # specs/014: draft_decisions, removed after 31 days


async def _count(session: AsyncSession, model: type[Any], user_id: uuid.UUID, *where: Any) -> int:
    result = await session.execute(
        select(func.count()).select_from(model).where(model.user_id == user_id, *where)
    )
    return int(result.scalar_one())


async def _min(
    session: AsyncSession, column: Any, model: type[Any], user_id: uuid.UUID, *where: Any
) -> Any:
    return await session.scalar(select(func.min(column)).where(model.user_id == user_id, *where))


async def access_summary(
    session: AsyncSession, telegram_id: int, *, is_operator: bool, now: datetime
) -> AccessSummary:
    beta, subs, events = await _access_rows(session, telegram_id, now)
    apps = await applications_repo.for_person(session, telegram_id)
    status = policy.status(beta, subs, is_operator=is_operator, now=now)
    return AccessSummary(
        kind=status.kind.value,
        beta_since=beta.added_at if beta is not None else None,
        subscriptions=tuple(
            SubscriptionSummary(sub.starts_at, sub.ends_at, policy.subscription_state(sub, now))
            for sub in subs
        ),
        events=len(events),
        applications=tuple(
            ApplicationSummary(app.consent_at, app.status.value, app.decided_at) for app in apps
        ),
    )


async def data_summary(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    message_retention_days: int,
    now: datetime | None = None,
    is_operator: bool = False,
) -> DataSummary:
    """Counts by type; with ``now`` also the Access section (specs/005 T032)."""
    oldest_message = await _min(session, Message.sent_at, Message, user_id)
    oldest_draft = await _min(session, Draft.prepared_at, Draft, user_id)
    draft_purge = await _min(session, Draft.finalized_at, Draft, user_id, Draft.text.is_not(None))
    profile_states: dict[str, int] = {}
    for state in ProfileState:
        profile_states[state.value] = await _count(
            session, StyleProfile, user_id, StyleProfile.state == state
        )
    retired_since = await _min(
        session, StyleProfile.updated_at, StyleProfile, user_id,
        StyleProfile.state.in_((ProfileState.retired, ProfileState.previous)),
    )  # fmt: skip
    closed_at = await _min(
        session, Commitment.closed_at, Commitment, user_id, Commitment.closed_at.is_not(None)
    )
    oldest_activity = await _min(session, ActivityRecord.at, ActivityRecord, user_id)
    oldest_decision = await _min(session, DraftDecision.at, DraftDecision, user_id)
    telegram = await users_repo.get_telegram_session(session, user_id)
    connection = await session.scalar(
        select(TaskSourceConnection).where(TaskSourceConnection.user_id == user_id)
    )
    access = None
    user = await users_repo.get_user(session, user_id)  # the row once
    telegram_id = user.telegram_control_chat_id if user is not None else None
    grammatical_gender = user.grammatical_gender if user is not None else None
    if now is not None and telegram_id is not None:
        access = await access_summary(session, telegram_id, is_operator=is_operator, now=now)
    return DataSummary(
        messages=TableSummary(
            await _count(session, Message, user_id),
            oldest_message,
            oldest_message + timedelta(days=message_retention_days) if oldest_message else None,
        ),
        drafts=TableSummary(
            await _count(session, Draft, user_id),
            oldest_draft,
            draft_purge + DERIVED_RETENTION if draft_purge else None,
        ),
        profiles=TableSummary(
            sum(profile_states.values()),
            None,
            retired_since + DERIVED_RETENTION if retired_since else None,
        ),
        profiles_by_state=profile_states,
        proposals=await _count(session, StyleRuleProposal, user_id),
        commitments_open=await _count(session, Commitment, user_id, Commitment.closed_at.is_(None)),
        commitments=TableSummary(
            await _count(session, Commitment, user_id, Commitment.closed_at.is_not(None)),
            None,
            closed_at + DERIVED_RETENTION if closed_at else None,
        ),
        activity=TableSummary(await _count(session, ActivityRecord, user_id), oldest_activity),
        contacts=await _count(session, Contact, user_id),
        work_chats=await _count(session, WorkChat, user_id),
        telegram_status=telegram.status.value if telegram is not None else None,
        task_source=(
            f"{connection.provider.value} ({connection.status.value})"
            if connection is not None
            else None
        ),
        access=access,
        work_account=bool(user is not None and user.work_account),
        work_account_since=user.work_account_changed_at if user is not None else None,
        auto_chats=await chats_repo.count_auto(session, user_id),
        excluded_chats=await chats_repo.count_excluded(session, user_id),
        release_notes=await _count(
            session,
            ReleaseDelivery,
            user_id,
            ReleaseDelivery.state.in_(RELEASE_RECEIVED_STATES),
        ),
        grammatical_gender=grammatical_gender,
        decisions=TableSummary(
            await _count(session, DraftDecision, user_id),
            oldest_decision,
            oldest_decision + DECISION_RETENTION if oldest_decision else None,
        ),
    )


async def delete_user_data(session: AsyncSession, user_id: uuid.UUID) -> None:
    """``DELETE FROM users`` (cascade to every per-user table; the FSM row by trigger), and the
    access rows keyed to the person's Telegram id, in the caller's transaction."""
    telegram_id = await session.scalar(
        select(User.telegram_control_chat_id).where(User.id == user_id)
    )
    if telegram_id is not None:
        counts: dict[str, int] = {}
        for model in ACCESS_TABLES:
            result = await session.execute(
                delete(model)
                .where(model.telegram_id == telegram_id)
                .execution_options(synchronize_session=False)
            )
            counts[model.__tablename__] = int(getattr(result, "rowcount", 0) or 0)
        log.info("access_records_deleted", **counts)  # counts only, never the Telegram id
    await users_repo.delete_user(session, user_id)


__all__ = [
    "AccessSummary",
    "ApplicationSummary",
    "DataSummary",
    "SubscriptionSummary",
    "TableSummary",
    "access_summary",
    "data_summary",
    "delete_user_data",
    "export_user_data",
    "row_dict",
]
