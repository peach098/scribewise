"""Operator CLI (T072): ``python -m scribe.admin``.

- ``list-users``: one line per account (id, state, LLM tier, created); no names, chats or content.
- ``set-tier <user> quality|economy``: the ``compose.draft`` model tier of one user (research R5);
  ``<user>`` is the ``users.id`` or the control-chat id.
- ``beta add <who> [--note TEXT]``, ``beta remove <who> [--note TEXT]``, ``beta list``: the
  closed-beta list (specs/005-beta-subscription-access, contracts/operator-cli.md). ``<who>`` is
  a numeric Telegram id or ``@username`` (matched ignoring case). Each change writes one
  ``access_events`` row (``actor=operator``, the note as ``reason``); the running service applies
  it to the bot on the person's next message and to the agent within about 2 minutes.
- ``beta requests [--all]``, ``beta approve <id>``, ``beta reject <id>``: beta applications sent
  from the bot (US5). Approve and reject do what the operator's buttons do (the decision is
  recorded with no operator id) and, with a bot, tell the applicant and update the operators'
  copies; ``python -m scribe.admin`` builds that bot from ``SCRIBE_BOT_TOKEN``.
- ``sub grant <who> (--days N | --until YYYY-MM-DD) [--note TEXT]``, ``sub extend <who> (...)``,
  ``sub end <who> [--note TEXT]``, ``sub list [--all]``: subscriptions granted by the operator.
  ``<who>`` must be a Telegram id or a username already linked to one.
- ``access show <who>``: the person's access, account and the last 5 access events;
  ``access stats [--days N]``: beta, subscription, application, refusal and suspension counts.
- ``release-notes preview <version>``, ``release-notes check``,
  ``release-notes send <version> [--dry-run]``, ``release-notes status [--all]``: release notes
  for users after an update (specs/011-release-notes, contracts/operator-cli.md). ``send`` only
  queues deliveries; the running service sends them. These commands never contact Telegram.
- Plugins (``SCRIBE_PLUGINS``, docs/plugins.md) may add their own commands
  (``register_admin(subparsers)``).
- ``cost [--days N]``: LLM spend per user per UTC day, split by route, for the last ``N`` days
  including today (default 7), from ``llm_spend`` (issue #67): calls, tokens and USD. User ids
  only, no names. A day on which a user reached the daily soft or hard cap (issue #74) is marked;
  today's mark is the user's current state.

Exit codes: 0 success, 1 not found or nothing to change (message on stderr), 2 usage error.
Uses the service's configuration (``SCRIBE_*`` environment) and touches only ``users``, the
access tables and the release notes tables; ``cost`` only reads ``llm_spend``.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import applications, policy
from scribe.access import repo as access_repo
from scribe.access.apply import notify_decision
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    BetaApplication,
    BetaApplicationStatus,
    BetaEntry,
    LLMSpend,
    LLMTier,
    RefusedContact,
    Subscription,
    User,
    UserState,
)
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import BudgetCaps, LimitsResolver, user_limits
from scribe.plugins import AdminHandler, PluginError, admin_commands
from scribe.release_notes import announce as release_announce
from scribe.release_notes import version as release_version
from scribe.release_notes.notes import (
    NotesError,
    ReleaseNotes,
    build_parts,
    notes_path,
    read_notes,
)

OPERATOR = "operator"
APPLIES_SOON = "the running service applies this within about 2 minutes"
BETA_GOAL = 30  # active beta users the closed beta aims for (SC-006)
MAX_NOTE_CHARS = 200
SHOW_EVENTS = 5  # access show: the last N events
_USERNAME = re.compile(r"^[a-z0-9_]+$")


async def find_user(session: AsyncSession, ref: str) -> User | None:
    try:
        return await users_repo.get_user(session, uuid.UUID(ref))
    except ValueError:
        pass
    try:
        return await users_repo.get_by_control_chat(session, int(ref))
    except ValueError:
        return None


def user_line(user: User) -> str:
    return f"{user.id}  {user.state.value:<13} {user.llm_tier.value:<8} {user.created_at:%Y-%m-%d}"


async def list_users(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with session_factory() as session:
        return [user_line(user) for user in await users_repo.list_users(session)]


async def set_tier(
    session_factory: async_sessionmaker[AsyncSession], ref: str, tier: LLMTier
) -> User | None:
    async with session_factory() as session:
        user = await find_user(session, ref)
        if user is None:
            return None
        user.llm_tier = tier
        await session.commit()
        return user


class Refused(Exception):
    """Nothing to change, or the person cannot be resolved: exit 1 with this message."""


# --- access: who, beta list (specs/005) -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Who:
    """A person on the command line: a Telegram id, or a username (lowercase, no ``@``)."""

    telegram_id: int | None = None
    username: str | None = None

    def __str__(self) -> str:
        return str(self.telegram_id) if self.telegram_id is not None else f"@{self.username}"


def parse_who(value: str) -> Who:
    """``123456789`` -> a Telegram id; ``@Some_User`` (or ``Some_User``) -> ``some_user``."""
    text = value.strip()
    if text.isdigit():
        return Who(telegram_id=int(text))
    name = access_repo.normalize_username(text)
    if name is None or not _USERNAME.match(name):
        raise argparse.ArgumentTypeError(f"expected a Telegram id or @username, got {value!r}")
    return Who(username=name)


def parse_note(value: str) -> str:
    if len(value) > MAX_NOTE_CHARS:
        raise argparse.ArgumentTypeError(f"at most {MAX_NOTE_CHARS} characters")
    return value


def _beta_match(who: Who, *, unlinked_only: bool = False) -> object:
    if who.telegram_id is not None:
        return BetaEntry.telegram_id == who.telegram_id
    by_name = func.lower(BetaEntry.username) == who.username
    return BetaEntry.telegram_id.is_(None) & by_name if unlinked_only else by_name


def _event_id(entry: BetaEntry) -> int:
    return entry.telegram_id if entry.telegram_id is not None else access_repo.UNLINKED_TELEGRAM_ID


async def beta_add(
    session_factory: async_sessionmaker[AsyncSession], who: Who, note: str | None, now: datetime
) -> bool:
    """False when the id, or the unlinked username, is already on the list."""
    async with session_factory() as session:
        existing = await session.scalar(
            select(BetaEntry.id).where(_beta_match(who, unlinked_only=True))  # type: ignore[arg-type]
        )
        if existing is not None:
            return False
        entry = BetaEntry(
            telegram_id=who.telegram_id,
            username=who.username,
            note=note,
            added_by=OPERATOR,
            added_at=now,
        )
        session.add(entry)
        try:
            await session.flush()
        except IntegrityError:  # added concurrently
            return False
        await access_repo.record_event(
            session,
            _event_id(entry),
            AccessEventKind.beta_added,
            actor=OPERATOR,
            reason=note,
            meta={"beta_entry_id": entry.id},
            now=now,
        )
        await session.commit()
        return True


async def beta_remove(
    session_factory: async_sessionmaker[AsyncSession], who: Who, note: str | None, now: datetime
) -> int:
    """Delete the matching entries (by id, or by username linked or not); how many."""
    async with session_factory() as session:
        result = await session.scalars(select(BetaEntry).where(_beta_match(who)))  # type: ignore[arg-type]
        entries = list(result.all())
        for entry in entries:
            await access_repo.record_event(
                session,
                _event_id(entry),
                AccessEventKind.beta_removed,
                actor=OPERATOR,
                reason=note,
                meta={"beta_entry_id": entry.id},
                now=now,
            )
            await session.delete(entry)
        await session.commit()
        return len(entries)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


async def beta_list(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """One line per entry, oldest first, and the footer with the active-user count."""
    async with session_factory() as session:
        entries = list(
            (
                await session.scalars(select(BetaEntry).order_by(BetaEntry.added_at, BetaEntry.id))
            ).all()
        )
        ids = [e.telegram_id for e in entries if e.telegram_id is not None]
        accounts: dict[int, UserState] = {}
        if ids:
            rows = await session.execute(
                select(User.telegram_control_chat_id, User.state).where(
                    User.telegram_control_chat_id.in_(ids)
                )
            )
            accounts = {chat_id: state for chat_id, state in rows}
    lines = []
    for entry in entries:
        has_account = entry.telegram_id is not None and entry.telegram_id in accounts
        line = "  ".join(
            [
                str(entry.telegram_id) if entry.telegram_id is not None else "-",
                f"@{entry.username}" if entry.username else "-",
                f"{entry.added_at:%Y-%m-%d}",
                _yes_no(entry.telegram_id is not None),
                _yes_no(has_account),
                entry.note or "",
            ]
        )
        lines.append(line.rstrip())
    active = sum(1 for state in accounts.values() if state != UserState.setting_up)
    lines.append(f"{len(entries)} entries, {active} active users (goal {BETA_GOAL})")
    return lines


# --- access: beta applications (specs/005 US5) ---------------------------------------------------

NOTIFYING_COMMANDS = frozenset({"approve", "reject"})  # need a bot to tell the applicant


def parse_application_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an application id, got {value!r}") from None


def _application_lines(app: BetaApplication) -> list[str]:
    head = "  ".join(
        [
            str(app.id),
            app.status.value,
            f"{app.consent_at.astimezone(UTC):%Y-%m-%d %H:%M}",
            str(app.telegram_id),
            f"@{app.username}" if app.username else "-",
            app.language,
        ]
    )
    return [head, *(f"    {line}" for line in (app.use_case or "").splitlines())]


async def beta_requests(
    session_factory: async_sessionmaker[AsyncSession], *, show_all: bool, now: datetime
) -> list[str]:
    async with session_factory() as session:
        apps = await applications.list_applications(session, include_decided=show_all, now=now)
    return [line for app in apps for line in _application_lines(app)]


async def beta_decide(
    session_factory: async_sessionmaker[AsyncSession],
    application_id: uuid.UUID,
    *,
    approve: bool,
    bot: Bot | None,
    now: datetime,
) -> BetaApplication:
    """Approve or reject a pending application, then notify as the bot's buttons do."""
    async with session_factory() as session:
        decision = await applications.decide(
            session, application_id, approve=approve, operator_id=None, now=now
        )
        if decision is None:
            raise Refused(f"no application {application_id}")
        if not decision.changed:
            status = decision.application.status.value
            raise Refused(f"application {application_id} is not pending ({status})")
        await session.commit()
    if bot is not None:
        await notify_decision(bot, session_factory, decision)
    return decision.application


async def run_beta(
    args: argparse.Namespace,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime,
    bot: Bot | None = None,
) -> int:
    if args.beta_command == "list":
        print("\n".join(await beta_list(session_factory)))
        return 0
    if args.beta_command == "requests":
        lines = await beta_requests(session_factory, show_all=args.all, now=now)
        empty = "no applications" if args.all else "no pending applications"
        print("\n".join(lines) if lines else empty)
        return 0
    if args.beta_command in NOTIFYING_COMMANDS:
        approve = args.beta_command == "approve"
        try:
            app = await beta_decide(
                session_factory, args.application_id, approve=approve, bot=bot, now=now
            )
        except Refused as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{'approved' if approve else 'rejected'} {app.telegram_id}")
        if approve:
            print(APPLIES_SOON)
        return 0
    who: Who = args.who
    if args.beta_command == "add":
        if not await beta_add(session_factory, who, args.note, now):
            print(f"{who} is already on the beta list", file=sys.stderr)
            return 1
        print(f"added {who}")
    else:
        if not await beta_remove(session_factory, who, args.note, now):
            print(f"{who} is not on the beta list", file=sys.stderr)
            return 1
        print(f"removed {who}")
    print(APPLIES_SOON)
    return 0


# --- access: subscriptions (specs/005 US4) --------------------------------------------------------


NOT_LINKED = "ask them to message the bot first"


@dataclass(frozen=True, slots=True)
class Period:
    """``--days N`` (counted from the period's start) or ``--until DAY`` (end of day UTC)."""

    days: int | None = None
    until: date | None = None

    def end(self, start: datetime) -> datetime:
        if self.until is not None:
            return datetime.combine(self.until, time.max, tzinfo=UTC)
        assert self.days is not None
        return start + timedelta(days=self.days)


def parse_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        days = 0
    if days <= 0:
        raise argparse.ArgumentTypeError(f"expected a whole number of days > 0, got {value!r}")
    return days


def parse_day(value: str) -> date:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def _day(moment: datetime) -> str:
    return f"{moment.astimezone(UTC):%Y-%m-%d}"


async def _telegram_id(session: AsyncSession, who: Who) -> int | None:
    """A numeric id as is; a username through the beta entry linked to it, if any."""
    if who.telegram_id is not None:
        return who.telegram_id
    return await session.scalar(
        select(BetaEntry.telegram_id).where(
            BetaEntry.telegram_id.is_not(None), func.lower(BetaEntry.username) == who.username
        )
    )


async def _require_id(session: AsyncSession, who: Who) -> int:
    telegram_id = await _telegram_id(session, who)
    if telegram_id is None:
        raise Refused(f"{who} has no linked Telegram id; {NOT_LINKED}")
    return telegram_id


async def _open_subs(session: AsyncSession, telegram_id: int, now: datetime) -> list[Subscription]:
    """Active and scheduled (not cancelled, not ended) rows, latest end last, locked."""
    result = await session.scalars(
        select(Subscription)
        .where(
            Subscription.telegram_id == telegram_id,
            Subscription.cancelled_at.is_(None),
            Subscription.ends_at > now,
        )
        .order_by(Subscription.ends_at, Subscription.created_at)
        .with_for_update()
    )
    return list(result.all())


async def _sub_event(
    session: AsyncSession,
    sub: Subscription,
    kind: AccessEventKind,
    note: str | None,
    now: datetime,
) -> None:
    await access_repo.record_event(
        session,
        sub.telegram_id,
        kind,
        actor=OPERATOR,
        reason=note,
        meta={"subscription_id": sub.id, "ends_at": sub.ends_at},
        now=now,
    )


async def sub_grant(
    session_factory: async_sessionmaker[AsyncSession],
    who: Who,
    period: Period,
    note: str | None,
    now: datetime,
) -> Subscription:
    """A new subscription from now, or chained after the person's latest open one."""
    async with session_factory() as session:
        telegram_id = await _require_id(session, who)
        open_subs = await _open_subs(session, telegram_id, now)
        starts_at = max([now, *(sub.ends_at for sub in open_subs)])
        ends_at = period.end(starts_at)
        if ends_at <= starts_at:
            raise Refused(f"the end {_day(ends_at)} is before the start {_day(starts_at)}")
        sub = Subscription(
            id=uuid.uuid4(),
            telegram_id=telegram_id,
            starts_at=starts_at,
            ends_at=ends_at,
            granted_by=OPERATOR,
            note=note,
        )
        session.add(sub)
        await session.flush()
        await _sub_event(session, sub, AccessEventKind.subscription_granted, note, now)
        await session.commit()
        return sub


async def sub_extend(
    session_factory: async_sessionmaker[AsyncSession],
    who: Who,
    period: Period,
    note: str | None,
    now: datetime,
) -> Subscription:
    """Move the end of the latest open subscription (or, with none open, of the latest ended
    one, so access can resume) and clear its reminder. ``--days`` counts from its end, or from
    now when it has already ended."""
    async with session_factory() as session:
        telegram_id = await _require_id(session, who)
        open_subs = await _open_subs(session, telegram_id, now)
        sub = open_subs[-1] if open_subs else await _latest_ended(session, telegram_id)
        if sub is None:
            raise Refused(f"{who} has no subscription to extend")
        ends_at = period.end(max(sub.ends_at, now))
        if ends_at <= max(sub.starts_at, now):
            raise Refused(f"the end {_day(ends_at)} is in the past or before the start")
        sub.ends_at = ends_at
        sub.reminded_at = None
        await session.flush()
        await _sub_event(session, sub, AccessEventKind.subscription_extended, note, now)
        await session.commit()
        return sub


async def _latest_ended(session: AsyncSession, telegram_id: int) -> Subscription | None:
    return await session.scalar(
        select(Subscription)
        .where(Subscription.telegram_id == telegram_id, Subscription.cancelled_at.is_(None))
        .order_by(Subscription.ends_at.desc(), Subscription.created_at.desc())
        .limit(1)
        .with_for_update()
    )


async def sub_end(
    session_factory: async_sessionmaker[AsyncSession], who: Who, note: str | None, now: datetime
) -> int:
    """Cancel every active and scheduled subscription; how many."""
    async with session_factory() as session:
        telegram_id = await _require_id(session, who)
        open_subs = await _open_subs(session, telegram_id, now)
        if not open_subs:
            raise Refused(f"{who} has no active or scheduled subscription")
        for sub in open_subs:
            sub.cancelled_at = now
            await _sub_event(session, sub, AccessEventKind.subscription_ended, note, now)
        await session.commit()
        return len(open_subs)


async def sub_list(
    session_factory: async_sessionmaker[AsyncSession], *, show_all: bool, now: datetime
) -> list[str]:
    """``<telegram_id> <state> <starts> <ends> <note>``; open ones only unless ``show_all``."""
    async with session_factory() as session:
        query = select(Subscription).order_by(
            Subscription.telegram_id, Subscription.starts_at, Subscription.created_at
        )
        if not show_all:
            query = query.where(Subscription.cancelled_at.is_(None), Subscription.ends_at > now)
        subs = list((await session.scalars(query)).all())
    lines = []
    for sub in subs:
        state = policy.subscription_state(sub, now)
        line = "  ".join(
            [
                str(sub.telegram_id),
                f"{state:<9}",
                _day(sub.starts_at),
                _day(sub.ends_at),
                sub.note or "",
            ]
        )
        lines.append(line.rstrip())
    return lines


async def run_sub(
    args: argparse.Namespace, session_factory: async_sessionmaker[AsyncSession], now: datetime
) -> int:
    if args.sub_command == "list":
        lines = await sub_list(session_factory, show_all=args.all, now=now)
        print("\n".join(lines) if lines else "no subscriptions")
        return 0
    who: Who = args.who
    try:
        if args.sub_command == "grant":
            sub = await sub_grant(session_factory, who, _period(args), args.note, now)
            print(f"granted {who} until {_day(sub.ends_at)}")
        elif args.sub_command == "extend":
            sub = await sub_extend(session_factory, who, _period(args), args.note, now)
            print(f"extended {who} until {_day(sub.ends_at)}")
        else:
            await sub_end(session_factory, who, args.note, now)
            print(f"ended {who}")
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(APPLIES_SOON)
    return 0


def _period(args: argparse.Namespace) -> Period:
    return Period(days=args.days, until=args.until)


# --- access: show, stats (specs/005 FR-018, FR-020) -----------------------------------------------


async def access_show(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    who: Who,
    now: datetime,
) -> list[str]:
    """Status, account and the last events of one person (no names, chats or content)."""
    async with session_factory() as session:
        telegram_id = await _telegram_id(session, who)
        if telegram_id is None:
            return await _show_unlinked(session, who)
        status = await access_repo.access_status(session, settings, telegram_id, None, now=now)
        user = await users_repo.get_by_control_chat(session, telegram_id)
        events = await session.scalars(
            select(AccessEvent)
            .where(AccessEvent.telegram_id == telegram_id)
            .order_by(AccessEvent.at.desc(), AccessEvent.id)
            .limit(SHOW_EVENTS)
        )
        suspended = (
            f"{user.access_suspended_at.astimezone(UTC):%Y-%m-%d %H:%M}"
            if user is not None and user.access_suspended_at is not None
            else "no"
        )
        until = _day(status.until) if status.until is not None else "-"
        return [
            f"kind={status.kind.value} until={until}",
            f"account={_yes_no(user is not None)} suspended={suspended}",
            *(_event_line(event) for event in events.all()),
        ]


async def _show_unlinked(session: AsyncSession, who: Who) -> list[str]:
    """A username-only beta entry nobody has linked yet; raises if there is none."""
    entry = await session.scalar(
        select(BetaEntry).where(_beta_match(who, unlinked_only=True))  # type: ignore[arg-type]
    )
    if entry is None:
        raise Refused(f"{who} is not known: not on the beta list and not linked")
    events = await session.scalars(
        select(AccessEvent)
        .where(
            AccessEvent.telegram_id == access_repo.UNLINKED_TELEGRAM_ID,
            AccessEvent.meta_json["beta_entry_id"].astext == str(entry.id),
        )
        .order_by(AccessEvent.at.desc(), AccessEvent.id)
        .limit(SHOW_EVENTS)
    )
    return [
        f"kind={policy.AccessKind.beta.value} until=-",
        "account=no suspended=no",
        *(_event_line(event) for event in events.all()),
    ]


def event_name(event: AccessEvent) -> str:
    """The kind, or for a plugin's event (``kind=plugin``) the name it stored."""
    if event.kind == AccessEventKind.plugin:
        name = (event.meta_json or {}).get("event")
        return f"plugin:{name}" if isinstance(name, str) and name else AccessEventKind.plugin.value
    return event.kind.value


def _event_line(event: AccessEvent) -> str:
    return (
        f"{event.at.astimezone(UTC):%Y-%m-%d %H:%M}  {event_name(event)}  {event.actor}  "
        f"{event.reason or '-'}"
    )


async def access_stats(
    session_factory: async_sessionmaker[AsyncSession], days: int, now: datetime
) -> list[str]:
    """The operator's counts (FR-020, SC-006); refusals within the last ``days`` days."""
    since = now - timedelta(days=days)
    async with session_factory() as session:
        beta_entries = await session.scalar(select(func.count()).select_from(BetaEntry))
        beta_active = await session.scalar(
            select(func.count())
            .select_from(BetaEntry)
            .join(User, User.telegram_control_chat_id == BetaEntry.telegram_id)
            .where(User.state != UserState.setting_up)
        )
        subs_active = await session.scalar(
            select(func.count()).where(
                Subscription.cancelled_at.is_(None),
                Subscription.starts_at <= now,
                Subscription.ends_at > now,
            )
        )
        refused = await session.scalar(
            select(func.count()).where(RefusedContact.last_seen_at >= since)
        )
        answered = await session.scalar(
            select(func.count()).where(RefusedContact.last_answered_at >= since)
        )
        suspended = await session.scalar(
            select(func.count()).where(User.access_suspended_at.is_not(None))
        )
        decided = dict(
            (
                await session.execute(
                    select(BetaApplication.status, func.count())
                    .where(BetaApplication.decided_at >= since)
                    .group_by(BetaApplication.status)
                )
            ).all()
        )
        pending = await session.scalar(
            select(func.count()).where(BetaApplication.status == BetaApplicationStatus.pending)
        )
    approved = decided.get(BetaApplicationStatus.approved, 0)
    rejected = decided.get(BetaApplicationStatus.rejected, 0)
    return [
        f"beta entries: {beta_entries or 0}",
        f"beta active users: {beta_active or 0}",
        f"subscriptions active: {subs_active or 0}",
        f"refused people (last {days} days): {refused or 0}",
        f"applications pending: {pending or 0}",
        f"applications approved / rejected (last {days} days): {approved} / {rejected}",
        f"refusal answers sent (last {days} days): {answered or 0}",
        f"suspended users: {suspended or 0}",
    ]


async def run_access(
    args: argparse.Namespace,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    now: datetime,
) -> int:
    if args.access_command == "stats":
        print("\n".join(await access_stats(session_factory, args.days, now)))
        return 0
    if settings is None:
        from scribe.config import get_settings

        settings = get_settings()
    try:
        lines = await access_show(session_factory, settings, args.who, now)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


# --- release-notes (specs/011-release-notes contracts/operator-cli.md) --------------------------

RELEASE_SENDS_SOON = "the running service sends this within about a minute"


def parse_release_version(value: str) -> release_version.Version:
    try:
        return release_version.parse(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"version must be X.Y.Z, got {value!r}") from None


def _load_release_notes(settings: Settings, version: release_version.Version) -> ReleaseNotes:
    path = notes_path(settings.release_notes_dir, version)
    if not path.is_file():
        raise Refused(f"no release notes file {path}")
    try:
        return read_notes(path)
    except NotesError as exc:
        raise Refused(str(exc)) from None


def release_preview_lines(notes: ReleaseNotes) -> list[str]:
    """The exact message of each language (heading and fallback included), then warnings."""
    lines: list[str] = []
    for lang in ("en", "ru"):
        lines.append(f"--- {lang} ---")
        parts = build_parts({notes.version: notes}, [notes.version], lang)
        lines.append("\n\n".join(parts) if parts else "(no text: nothing is sent)")
    lines += [f"warning: {warning}" for warning in notes.warnings]
    return lines


def release_preview(settings: Settings, version: release_version.Version) -> list[str]:
    notes = _load_release_notes(settings, version)
    lines = release_preview_lines(notes)
    running = release_version.running_version()
    if running is not None and version > running:
        lines.append(f"note: announced automatically when {release_version.format(version)} runs")
    return lines


def release_check(settings: Settings) -> tuple[list[str], list[str]]:
    """``(<version> ok / warning lines, errors)`` for every notes file in the directory."""
    directory = settings.release_notes_dir
    if not directory.is_dir():
        return [], [f"no release notes directory {directory}"]
    lines: list[str] = []
    errors: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".md" or path.name == "README.md":
            continue
        try:
            notes = read_notes(path)
        except NotesError as exc:
            errors.append(str(exc))
            continue
        name = release_version.format(notes.version)
        if notes.warnings:
            lines += [f"{name} warning: {warning}" for warning in notes.warnings]
        else:
            lines.append(f"{name} ok")
    return lines, errors


async def release_send(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    version: release_version.Version,
    now: datetime,
    *,
    dry_run: bool,
) -> list[str]:
    notes = _load_release_notes(settings, version)
    name = release_version.format(version)
    if not notes.has_text:
        raise Refused(f"{name} has no en or ru text")
    async with session_factory() as session:
        result = await release_announce.queue_manual(session, settings, notes, now=now)
        counts = (
            f"{result.recipients} recipients (en {result.by_lang['en']}, "
            f"ru {result.by_lang['ru']}), {result.already} already have it"
        )
        if dry_run:
            await session.rollback()
            return [f"dry run {name}: {counts}", *release_preview_lines(notes)]
        if result.announcement is None:
            await session.rollback()
            raise Refused(f"nothing to send: {counts}")
        await session.commit()
    return [f"queued {name}: {counts}", RELEASE_SENDS_SOON]


async def release_status(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, *, show_all: bool
) -> list[str]:
    async with session_factory() as session:
        return await release_announce.status_lines(
            session,
            show_all=show_all,
            running=release_version.running_version(),
            settings=settings,
        )


async def run_release_notes(
    args: argparse.Namespace,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    now: datetime,
) -> int:
    if settings is None:
        from scribe.config import get_settings

        settings = get_settings()
    command = args.release_command
    try:
        if command == "preview":
            lines = release_preview(settings, args.version)
        elif command == "check":
            lines, errors = release_check(settings)
            if lines:
                print("\n".join(lines))
            for error in errors:
                print(error, file=sys.stderr)
            return 1 if errors else 0
        elif command == "send":
            lines = await release_send(
                session_factory, settings, args.version, now, dry_run=args.dry_run
            )
        else:
            lines = await release_status(session_factory, settings, show_all=args.all)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


# --- cost (issue #67) ---------------------------------------------------------------------------


def _usd(value: Decimal) -> str:
    return f"${value:.4f}"


def _route_line(row: LLMSpend) -> str:
    failed = f" ({row.failed_calls} failed)" if row.failed_calls else ""
    written = row.cache_write_input_tokens + row.cache_write_1h_input_tokens
    return (
        f"    {row.route:<24} {_usd(row.cost_usd):>10}  {row.calls} calls{failed}  "
        f"in {row.input_tokens}  cache hit {row.cached_input_tokens}  "
        f"cache write {written}  out {row.output_tokens} (reasoning {row.reasoning_tokens})"
    )


CAP_MARKS = {"soft": "  [soft cap reached: economy tier]", "hard": "  [hard cap reached: paused]"}


def _caps_line(caps: BudgetCaps) -> str:
    def cap(value: float) -> str:
        return f"${value:.2f}" if value > 0 else "off"

    return f"daily caps per user: soft {cap(caps.soft_usd)}, hard {cap(caps.hard_usd)}"


async def cost_report(
    session_factory: async_sessionmaker[AsyncSession],
    days: int,
    now: datetime,
    caps: BudgetCaps | None = None,
    limits: LimitsResolver | None = None,
) -> list[str]:
    """$/user/day split by route over the last ``days`` UTC days (today included); with
    ``caps`` (issue #74) each day of a user that reached a daily cap is marked (each user's
    current caps from ``limits`` when a plugin set them; the excluded routes not counted)."""
    today = now.astimezone(UTC).date()
    since = today - timedelta(days=days - 1)
    user_caps: dict[uuid.UUID, BudgetCaps] = {}
    async with session_factory() as session:
        rows = await llm_spend_repo.daily(session, since)
        if caps is not None and limits is not None:
            for user_id in {row.user_id for row in rows}:
                user_caps[user_id] = (await user_limits(session, user_id, limits, now)).caps
    if not rows:
        return [f"no LLM spend recorded since {since}"]
    by_day_user: dict[tuple[date, uuid.UUID], list[LLMSpend]] = defaultdict(list)
    for row in rows:
        by_day_user[(row.day, row.user_id)].append(row)
    lines = [f"LLM spend, UTC days {since} .. {today}"]
    if caps is not None:
        lines.append(_caps_line(caps))
    per_user: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for (day, user_id), routes in by_day_user.items():
        total = sum((r.cost_usd for r in routes), Decimal(0))
        per_user[user_id] += total
        calls = sum(r.calls for r in routes)
        mark = ""
        if caps is not None:
            own = user_caps.get(user_id, caps)
            counted = sum(
                (r.cost_usd for r in routes if r.route not in own.excluded_routes), Decimal(0)
            )
            mark = CAP_MARKS.get(own.level(counted), "")
        lines.append(f"{day}  {user_id}  {_usd(total)}  {calls} calls{mark}")
        lines.extend(_route_line(r) for r in sorted(routes, key=lambda r: -r.cost_usd))
    grand = sum(per_user.values(), Decimal(0))
    lines.append("")
    lines.append(f"per user over {days} days (average per day):")
    for user_id, total in sorted(per_user.items(), key=lambda item: -item[1]):
        lines.append(f"  {user_id}  {_usd(total)}  ({_usd(total / days)}/day)")
    lines.append(
        f"total {_usd(grand)}, {len(per_user)} users, "
        f"{_usd(grand / (len(per_user) * days))} per user per day"
    )
    return lines


def _parser(plugins: Sequence[str] = ()) -> argparse.ArgumentParser:
    return _build_parser(plugins)[0]


def _build_parser(
    plugins: Sequence[str] = (),
) -> tuple[argparse.ArgumentParser, dict[str, AdminHandler]]:
    """The parser with the core commands and those of the plugin modules (docs/plugins.md)."""
    parser = argparse.ArgumentParser(prog="python -m scribe.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-users", help="list accounts (no personal data)")
    tier = commands.add_parser("set-tier", help="set a user's compose model tier")
    tier.add_argument("user", help="users.id or control-chat id")
    tier.add_argument("tier", choices=[t.value for t in LLMTier])

    beta = commands.add_parser("beta", help="the closed-beta list")
    beta_commands = beta.add_subparsers(dest="beta_command", required=True)
    who_help = "Telegram id or @username"
    note_help = f"operator note, at most {MAX_NOTE_CHARS} characters"
    add = beta_commands.add_parser("add", help="add a person to the beta list")
    add.add_argument("who", type=parse_who, help=who_help)
    add.add_argument("--note", type=parse_note, help=note_help)
    remove = beta_commands.add_parser("remove", help="remove a person from the beta list")
    remove.add_argument("who", type=parse_who, help=who_help)
    remove.add_argument("--note", type=parse_note, help=note_help)
    beta_commands.add_parser("list", help="list the beta entries")
    requests = beta_commands.add_parser("requests", help="list beta applications from the bot")
    requests.add_argument(
        "--all", action="store_true", help="also those decided in the last 30 days"
    )
    for name, help_text in (
        ("approve", "approve an application: add to the beta list, tell the applicant"),
        ("reject", "reject an application and tell the applicant"),
    ):
        decide = beta_commands.add_parser(name, help=help_text)
        decide.add_argument("application_id", type=parse_application_id, help="application id")

    sub = commands.add_parser("sub", help="subscriptions granted by the operator")
    sub_commands = sub.add_subparsers(dest="sub_command", required=True)
    for name, help_text in (
        ("grant", "grant a subscription (chained after the current one)"),
        ("extend", "move the end of the latest subscription"),
    ):
        command = sub_commands.add_parser(name, help=help_text)
        command.add_argument("who", type=parse_who, help=who_help)
        period = command.add_mutually_exclusive_group(required=True)
        period.add_argument("--days", type=parse_days, help="number of days (> 0)")
        period.add_argument("--until", type=parse_day, help="last day, YYYY-MM-DD (end of day UTC)")
        command.add_argument("--note", type=parse_note, help=note_help)
    end = sub_commands.add_parser("end", help="cancel active and scheduled subscriptions")
    end.add_argument("who", type=parse_who, help=who_help)
    end.add_argument("--note", type=parse_note, help=note_help)
    sub_list_parser = sub_commands.add_parser("list", help="list subscriptions")
    sub_list_parser.add_argument("--all", action="store_true", help="include ended and cancelled")

    access = commands.add_parser("access", help="a person's access, and access counts")
    access_commands = access.add_subparsers(dest="access_command", required=True)
    show = access_commands.add_parser("show", help="one person's access and last events")
    show.add_argument("who", type=parse_who, help=who_help)
    stats = access_commands.add_parser("stats", help="beta, subscription and refusal counts")
    stats.add_argument("--days", type=parse_days, default=7, help="refusal window (default 7)")

    notes = commands.add_parser("release-notes", help="release notes for users after an update")
    notes_commands = notes.add_subparsers(dest="release_command", required=True)
    version_help = "the version, X.Y.Z"
    preview = notes_commands.add_parser("preview", help="show the message of each language")
    preview.add_argument("version", type=parse_release_version, help=version_help)
    notes_commands.add_parser("check", help="validate every release notes file")
    send = notes_commands.add_parser("send", help="queue a version for users who lack it")
    send.add_argument("version", type=parse_release_version, help=version_help)
    send.add_argument("--dry-run", action="store_true", help="show the counts, write nothing")
    status = notes_commands.add_parser("status", help="the latest announcement and its counts")
    status.add_argument("--all", action="store_true", help="every announcement, newest first")

    cost = commands.add_parser("cost", help="LLM spend per user per day, split by route")
    cost.add_argument(
        "--days", type=parse_days, default=7, help="UTC days back, today included (default 7)"
    )
    return parser, admin_commands(commands, plugins)


async def run(
    argv: Sequence[str],
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clock: Clock = utcnow,
    settings: Settings | None = None,
    bot: Bot | None = None,
    limits: LimitsResolver | None = None,
) -> int:
    """Run one command; ``settings`` (for the operator ids and the plugin modules) defaults to
    the environment's. ``bot`` sends the notices of ``beta approve`` / ``beta reject``; without
    one they are skipped. ``limits``: per-user caps for ``cost`` (default: the deployment's)."""
    parser, plugin_handlers = _build_parser(settings.plugins if settings is not None else ())
    args = parser.parse_args(list(argv))
    if args.command in plugin_handlers:
        return await plugin_handlers[args.command](args, session_factory, settings, clock)
    if args.command == "beta":
        return await run_beta(args, session_factory, clock(), bot)
    if args.command == "sub":
        return await run_sub(args, session_factory, clock())
    if args.command == "access":
        return await run_access(args, session_factory, settings, clock())
    if args.command == "release-notes":
        return await run_release_notes(args, session_factory, settings, clock())
    if args.command == "cost":
        caps = BudgetCaps.from_settings(settings) if settings is not None else None
        report = await cost_report(session_factory, args.days, clock(), caps, limits)
        print("\n".join(report))
        return 0
    if args.command == "list-users":
        lines = await list_users(session_factory)
        print("\n".join(lines) if lines else "no users")
        return 0
    user = await set_tier(session_factory, args.user, LLMTier(args.tier))
    if user is None:
        print(f"no user {args.user!r}", file=sys.stderr)
        return 1
    print(f"{user.id}: llm_tier={user.llm_tier.value}")
    return 0


async def _main(argv: Sequence[str]) -> int:
    from scribe.config import get_settings
    from scribe.crypto import configure_keyring, keyring_from_settings
    from scribe.db.session import create_engine_from_settings, create_session_factory

    settings = get_settings()
    configure_keyring(keyring_from_settings(settings))
    engine = create_engine_from_settings(settings)
    args = _parser(settings.plugins).parse_args(list(argv))
    bot = None
    if args.command == "beta" and args.beta_command in NOTIFYING_COMMANDS:
        from scribe.telegram.control_bot.bot import create_bot

        bot = create_bot(settings)
    try:
        return await run(argv, create_session_factory(engine), settings=settings, bot=bot)
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def _plugin_modules() -> list[str]:
    """``SCRIBE_PLUGINS`` for the usage check; none while the configuration is incomplete
    (``_main`` reports that)."""
    from pydantic import ValidationError

    from scribe.config import get_settings

    try:
        return get_settings().plugins
    except ValidationError:
        return []


def main(argv: Sequence[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        _parser(_plugin_modules()).parse_args(list(argv))  # usage errors before the database
    except PluginError as exc:
        print(f"scribe.admin: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
