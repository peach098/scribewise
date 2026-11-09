"""Operator CLI access commands (005 T014; contracts/operator-cli.md). In-process
``scribe.admin.run`` against the test DB; synthetic ids and usernames only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import EditMessageText
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import admin
from scribe.access import applications, repo
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    BetaApplication,
    BetaApplicationStatus,
    BetaEntry,
    Subscription,
    UserState,
)
from scribe.i18n import format_day_month_year, t
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import offline_bot

TESTER = 900000001
OTHER = 900000002
APPLIES = "the running service applies this within about 2 minutes"


async def _run(
    argv: list[str],
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> tuple[int, str, str]:
    code = await admin.run(argv, session_factory, clock=clock)
    out, err = capsys.readouterr()
    return code, out, err


async def _events(db: AsyncSession) -> list[AccessEvent]:
    result = await db.scalars(
        select(AccessEvent)
        .order_by(AccessEvent.at, AccessEvent.kind)
        .execution_options(populate_existing=True)
    )
    return list(result.all())


async def _entries(db: AsyncSession) -> list[BetaEntry]:
    result = await db.scalars(
        select(BetaEntry).order_by(BetaEntry.added_at).execution_options(populate_existing=True)
    )
    return list(result.all())


async def test_beta_add_by_id_and_username(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    added_at = clock.now
    code, out, err = await _run(
        ["beta", "add", str(TESTER), "--note", "x"], session_factory, capsys, clock
    )
    assert (code, err) == (0, "")
    assert out.splitlines() == [f"added {TESTER}", APPLIES]

    clock.advance(seconds=1)
    code, out, _ = await _run(["beta", "add", "@Some_User"], session_factory, capsys, clock)
    assert code == 0 and out.splitlines()[0] == "added @some_user"

    by_id, by_name = await _entries(db_session)
    assert (by_id.telegram_id, by_id.username, by_id.note) == (TESTER, None, "x")
    assert (by_name.telegram_id, by_name.username) == (None, "some_user")
    assert by_id.added_by == "operator" and by_id.added_at == added_at

    first, second = await _events(db_session)
    assert (first.kind, first.actor, first.reason, first.telegram_id) == (
        AccessEventKind.beta_added,
        "operator",
        "x",
        TESTER,
    )
    # A username entry has no Telegram id yet: its event is keyed to the entry until it links.
    assert second.kind == AccessEventKind.beta_added and second.reason is None
    assert second.telegram_id == repo.UNLINKED_TELEGRAM_ID
    assert second.meta_json == {"beta_entry_id": str(by_name.id)}


async def test_beta_add_duplicate_exits_1(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    assert (await _run(["beta", "add", str(TESTER)], session_factory, capsys, clock))[0] == 0
    code, out, err = await _run(["beta", "add", str(TESTER)], session_factory, capsys, clock)
    assert (code, out) == (1, "") and "already on the beta list" in err

    assert (await _run(["beta", "add", "@some_user"], session_factory, capsys, clock))[0] == 0
    code, _, err = await _run(["beta", "add", "@SOME_USER"], session_factory, capsys, clock)
    assert code == 1 and "already on the beta list" in err


async def test_beta_add_rejects_long_note_and_bad_who(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    with pytest.raises(SystemExit) as exc:
        await admin.run(["beta", "add", str(TESTER), "--note", "n" * 201], session_factory)
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        await admin.run(["beta", "add", "@"], session_factory)
    assert exc.value.code == 2
    capsys.readouterr()


async def test_beta_remove(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _run(["beta", "add", "@some_user"], session_factory, capsys, clock)
    await _run(["beta", "add", str(OTHER)], session_factory, capsys, clock)

    code, out, err = await _run(
        ["beta", "remove", "@Some_User", "--note", "left"], session_factory, capsys, clock
    )
    assert (code, err) == (0, "")
    assert out.splitlines() == ["removed @some_user", APPLIES]
    code, out, _ = await _run(["beta", "remove", str(OTHER)], session_factory, capsys, clock)
    assert code == 0 and out.splitlines()[0] == f"removed {OTHER}"
    assert await _entries(db_session) == []

    removed = [e for e in await _events(db_session) if e.kind == AccessEventKind.beta_removed]
    assert sorted((e.telegram_id, e.actor, e.reason) for e in removed) == [
        (repo.UNLINKED_TELEGRAM_ID, "operator", "left"),
        (OTHER, "operator", None),
    ]

    code, out, err = await _run(["beta", "remove", "@some_user"], session_factory, capsys, clock)
    assert (code, out) == (1, "") and "not on the beta list" in err
    code, _, _ = await _run(["beta", "remove", str(TESTER)], session_factory, capsys, clock)
    assert code == 1


async def test_beta_remove_linked_entry_by_username(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _run(["beta", "add", "@beta_tester_1"], session_factory, capsys, clock)
    clock.advance(minutes=1)
    async with session_factory() as session:
        await repo.find_beta(session, TESTER, "Beta_Tester_1", now=clock.now)
        await session.commit()
    clock.advance(minutes=1)

    code, _, _ = await _run(["beta", "remove", "@beta_tester_1"], session_factory, capsys, clock)
    assert code == 0
    events = await _events(db_session)
    # Linking re-keys the entry's earlier events to the Telegram id.
    assert [(e.kind, e.telegram_id) for e in events] == [
        (AccessEventKind.beta_added, TESTER),
        (AccessEventKind.beta_linked, TESTER),
        (AccessEventKind.beta_removed, TESTER),
    ]


async def test_beta_list(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    await make_user(control_chat_id=TESTER, state=UserState.active)
    await make_user(control_chat_id=OTHER, state=UserState.setting_up)
    await _run(["beta", "add", str(TESTER), "--note", "first"], session_factory, capsys, clock)
    clock.advance(days=1)
    await _run(["beta", "add", str(OTHER)], session_factory, capsys, clock)
    clock.advance(days=1)
    await _run(["beta", "add", "@beta_tester_3"], session_factory, capsys, clock)

    code, out, err = await _run(["beta", "list"], session_factory, capsys, clock)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].split() == [str(TESTER), "-", "2026-10-14", "yes", "yes", "first"]
    assert lines[1].split() == [str(OTHER), "-", "2026-10-15", "yes", "yes"]
    assert lines[2].split() == ["-", "@beta_tester_3", "2026-10-16", "no", "no"]
    assert lines[3] == "3 entries, 1 active users (goal 30)"


async def test_beta_list_empty(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    code, out, _ = await _run(["beta", "list"], session_factory, capsys, clock)
    assert code == 0 and out.splitlines() == ["0 entries, 0 active users (goal 30)"]


# --- subscriptions (T027) -----------------------------------------------------------------------


async def _subs(db: AsyncSession) -> list[Subscription]:
    result = await db.scalars(
        select(Subscription)
        .order_by(Subscription.starts_at)
        .execution_options(populate_existing=True)
    )
    return list(result.all())


async def test_sub_grant_days_and_until(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    code, out, err = await _run(
        ["sub", "grant", str(TESTER), "--days", "30", "--note", "pay-1"],
        session_factory,
        capsys,
        clock,
    )
    assert (code, err) == (0, "")
    assert out.splitlines() == [f"granted {TESTER} until 2026-11-13", APPLIES]
    (sub,) = await _subs(db_session)
    assert (sub.telegram_id, sub.starts_at, sub.ends_at) == (
        TESTER,
        clock.now,
        clock.now + timedelta(days=30),
    )
    assert (sub.granted_by, sub.note, sub.cancelled_at, sub.reminded_at) == (
        "operator",
        "pay-1",
        None,
        None,
    )
    (event,) = await _events(db_session)
    assert (event.kind, event.actor, event.reason, event.telegram_id) == (
        AccessEventKind.subscription_granted,
        "operator",
        "pay-1",
        TESTER,
    )
    assert event.meta_json == {"subscription_id": str(sub.id), "ends_at": sub.ends_at.isoformat()}

    code, out, _ = await _run(
        ["sub", "grant", str(OTHER), "--until", "2026-12-31"], session_factory, capsys, clock
    )
    assert code == 0 and out.splitlines()[0] == f"granted {OTHER} until 2026-12-31"
    other = next(s for s in await _subs(db_session) if s.telegram_id == OTHER)
    # End of day UTC: the whole of 31 December is included.
    assert other.ends_at == datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


async def test_sub_grant_chains_after_the_active_one(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _run(["sub", "grant", str(TESTER), "--days", "10"], session_factory, capsys, clock)
    clock.advance(days=2)
    code, out, _ = await _run(
        ["sub", "grant", str(TESTER), "--days", "5"], session_factory, capsys, clock
    )
    assert code == 0 and out.splitlines()[0] == f"granted {TESTER} until 2026-10-29"
    first, second = await _subs(db_session)
    assert second.starts_at == first.ends_at
    assert second.ends_at == first.ends_at + timedelta(days=5)


async def test_sub_grant_rejects_unlinked_username_and_bad_args(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _run(["beta", "add", "@beta_tester_1"], session_factory, capsys, clock)
    for who in ("@beta_tester_1", "@nobody_here"):
        code, out, err = await _run(
            ["sub", "grant", who, "--days", "30"], session_factory, capsys, clock
        )
        assert (code, out) == (1, "") and "ask them to message the bot first" in err

    for argv in (
        ["sub", "grant", str(TESTER)],
        ["sub", "grant", str(TESTER), "--days", "0"],
        ["sub", "grant", str(TESTER), "--days", "3", "--until", "2026-12-31"],
        ["sub", "grant", str(TESTER), "--until", "31.12.2026"],
    ):
        with pytest.raises(SystemExit) as exc:
            await admin.run(argv, session_factory, clock=clock)
        assert exc.value.code == 2
    capsys.readouterr()

    code, _, err = await _run(
        ["sub", "grant", str(TESTER), "--until", "2026-10-01"], session_factory, capsys, clock
    )
    assert code == 1 and "before" in err


async def test_sub_grant_by_linked_username(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _run(["beta", "add", "@beta_tester_1"], session_factory, capsys, clock)
    async with session_factory() as session:
        await repo.find_beta(session, TESTER, "beta_tester_1", now=clock.now)
        await session.commit()

    code, out, _ = await _run(
        ["sub", "grant", "@Beta_Tester_1", "--days", "1"], session_factory, capsys, clock
    )
    assert code == 0 and out.splitlines()[0] == "granted @beta_tester_1 until 2026-10-15"
    (sub,) = await _subs(db_session)
    assert sub.telegram_id == TESTER


async def test_sub_extend(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    code, _, err = await _run(
        ["sub", "extend", str(TESTER), "--days", "5"], session_factory, capsys, clock
    )
    assert code == 1 and "no subscription" in err

    await _run(["sub", "grant", str(TESTER), "--days", "10"], session_factory, capsys, clock)
    (sub,) = await _subs(db_session)
    sub.reminded_at = clock.now
    await db_session.commit()
    original_end = sub.ends_at

    code, out, err = await _run(
        ["sub", "extend", str(TESTER), "--days", "5", "--note", "pay-2"],
        session_factory,
        capsys,
        clock,
    )
    assert (code, err) == (0, "")
    assert out.splitlines() == [f"extended {TESTER} until 2026-10-29", APPLIES]
    (sub,) = await _subs(db_session)  # no new row
    assert sub.ends_at == original_end + timedelta(days=5) and sub.reminded_at is None
    extended = [
        e for e in await _events(db_session) if e.kind == AccessEventKind.subscription_extended
    ]
    assert [(e.actor, e.reason, e.meta_json) for e in extended] == [
        ("operator", "pay-2", {"subscription_id": str(sub.id), "ends_at": sub.ends_at.isoformat()})
    ]

    code, out, _ = await _run(
        ["sub", "extend", str(TESTER), "--until", "2027-01-31"], session_factory, capsys, clock
    )
    assert code == 0 and out.splitlines()[0] == f"extended {TESTER} until 2027-01-31"


async def test_sub_extend_after_expiry_and_latest_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _run(["sub", "grant", str(TESTER), "--days", "2"], session_factory, capsys, clock)
    await _run(["sub", "grant", str(TESTER), "--days", "3"], session_factory, capsys, clock)
    code, _, _ = await _run(
        ["sub", "extend", str(TESTER), "--days", "1"], session_factory, capsys, clock
    )
    assert code == 0
    first, second = await _subs(db_session)
    assert first.ends_at == clock.now + timedelta(days=2)  # the scheduled (latest) one moved
    assert second.ends_at == clock.now + timedelta(days=6)

    clock.advance(days=10)  # both ended: --days counts from now
    code, out, _ = await _run(
        ["sub", "extend", str(TESTER), "--days", "7"], session_factory, capsys, clock
    )
    assert code == 0 and out.splitlines()[0] == f"extended {TESTER} until 2026-10-31"
    _, second = await _subs(db_session)
    assert second.ends_at == clock.now + timedelta(days=7)


async def test_sub_end(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _run(["sub", "grant", str(TESTER), "--days", "2"], session_factory, capsys, clock)
    await _run(["sub", "grant", str(TESTER), "--days", "3"], session_factory, capsys, clock)
    clock.advance(hours=1)

    code, out, err = await _run(
        ["sub", "end", str(TESTER), "--note", "refund"], session_factory, capsys, clock
    )
    assert (code, err) == (0, "")
    assert out.splitlines() == [f"ended {TESTER}", APPLIES]
    subs = await _subs(db_session)
    assert [s.cancelled_at for s in subs] == [clock.now, clock.now]
    ended = [e for e in await _events(db_session) if e.kind == AccessEventKind.subscription_ended]
    assert sorted(e.meta_json["subscription_id"] for e in ended) == sorted(str(s.id) for s in subs)
    assert {(e.actor, e.reason) for e in ended} == {("operator", "refund")}

    code, out, err = await _run(["sub", "end", str(TESTER)], session_factory, capsys, clock)
    assert (code, out) == (1, "") and "no active or scheduled subscription" in err


async def test_sub_list(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    code, out, _ = await _run(["sub", "list"], session_factory, capsys, clock)
    assert code == 0 and out.splitlines() == ["no subscriptions"]

    await _run(["sub", "grant", str(TESTER), "--days", "1"], session_factory, capsys, clock)
    await _run(
        ["sub", "grant", str(TESTER), "--days", "2", "--note", "next"],
        session_factory,
        capsys,
        clock,
    )
    await _run(["sub", "grant", str(OTHER), "--days", "1"], session_factory, capsys, clock)
    await _run(["sub", "end", str(OTHER)], session_factory, capsys, clock)
    clock.advance(hours=1)

    code, out, _ = await _run(["sub", "list"], session_factory, capsys, clock)
    assert code == 0
    assert [line.split() for line in out.splitlines()] == [
        [str(TESTER), "active", "2026-10-14", "2026-10-15"],
        [str(TESTER), "scheduled", "2026-10-15", "2026-10-17", "next"],
    ]

    clock.advance(days=1)
    code, out, _ = await _run(["sub", "list", "--all"], session_factory, capsys, clock)
    assert sorted(line.split()[1] for line in out.splitlines()) == [
        "active",
        "cancelled",
        "ended",
    ]


# --- access show / stats (T027) -----------------------------------------------------------------


async def test_access_show(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    make_user: MakeUser,
) -> None:
    async def show(who: str, s: Settings = settings) -> tuple[int, list[str], str]:
        code = await admin.run(["access", "show", who], session_factory, clock=clock, settings=s)
        out, err = capsys.readouterr()
        return code, out.splitlines(), err

    code, lines, _ = await show(str(TESTER))
    assert code == 0 and lines == ["kind=none until=-", "account=no suspended=no"]

    await make_user(control_chat_id=TESTER, access_suspended_at=clock.now)
    await _run(["beta", "add", str(TESTER), "--note", "friend"], session_factory, capsys, clock)
    for _ in range(6):
        clock.advance(minutes=1)
        await _run(["sub", "grant", str(TESTER), "--days", "1"], session_factory, capsys, clock)

    code, lines, _ = await show(str(TESTER))
    assert code == 0
    assert lines[0] == "kind=beta_and_subscribed until=2026-10-15"
    assert lines[1] == "account=yes suspended=2026-10-14 10:00"
    assert len(lines) == 2 + 5  # the last 5 events, newest first
    assert lines[2].split() == ["2026-10-14", "10:06", "subscription_granted", "operator", "-"]

    code, lines, _ = await show(
        str(TESTER), settings.model_copy(update={"operator_telegram_ids": [TESTER]})
    )
    assert lines[0] == "kind=operator until=-"

    # An unlinked username entry: beta, no account; its events are keyed to the entry.
    await _run(["beta", "add", "@beta_tester_2"], session_factory, capsys, clock)
    code, lines, _ = await show("@Beta_Tester_2")
    assert code == 0 and lines[:2] == ["kind=beta until=-", "account=no suspended=no"]
    assert lines[2].split()[2:] == ["beta_added", "operator", "-"]

    code, lines, err = await show("@nobody_here")
    assert (code, lines) == (1, []) and "@nobody_here" in err


async def test_access_stats(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    await make_user(control_chat_id=TESTER, state=UserState.active)
    await make_user(state=UserState.active, access_suspended_at=clock.now)
    await _run(["beta", "add", str(TESTER)], session_factory, capsys, clock)
    await _run(["beta", "add", "@beta_tester_3"], session_factory, capsys, clock)
    await _run(["sub", "grant", str(OTHER), "--days", "3"], session_factory, capsys, clock)
    await _run(["sub", "grant", "900000003", "--days", "3"], session_factory, capsys, clock)
    await _run(["sub", "end", "900000003"], session_factory, capsys, clock)
    async with session_factory() as session:
        # Answered within the window; seen in the window but answered before; seen long ago.
        await repo.touch_refused(session, 900000011, now=clock.now, cooldown_s=3600)
        await repo.touch_refused(
            session, 900000012, now=clock.now - timedelta(days=10), cooldown_s=3600
        )
        await repo.touch_refused(session, 900000012, now=clock.now, cooldown_s=30 * 86400)
        await repo.touch_refused(
            session, 900000013, now=clock.now - timedelta(days=10), cooldown_s=3600
        )
        await session.commit()

    code, out, err = await _run(["access", "stats"], session_factory, capsys, clock)
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "beta entries: 2",
        "beta active users: 1",
        "subscriptions active: 1",
        "refused people (last 7 days): 2",
        "applications pending: 0",
        "applications approved / rejected (last 7 days): 0 / 0",
        "refusal answers sent (last 7 days): 1",
        "suspended users: 1",
    ]

    code, out, _ = await _run(["access", "stats", "--days", "30"], session_factory, capsys, clock)
    assert "refused people (last 30 days): 3" in out.splitlines()
    assert "refusal answers sent (last 30 days): 3" in out.splitlines()


# --- beta applications (T037) ---------------------------------------------------------------------

APPLICANT = 900000021
USE_CASE = "Synthetic use case\nsecond synthetic line"


async def _submit(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    now: datetime,
    *,
    username: str | None = "synthetic_applicant",
    language: str = "en",
) -> BetaApplication:
    async with session_factory() as session:
        application = await applications.submit(
            session,
            telegram_id=telegram_id,
            username=username,
            use_case=USE_CASE,
            language=language,
            now=now,
        )
        application.operator_messages = [[900000031, 77]]
        await session.commit()
        return application


async def test_beta_requests(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    code, out, _ = await _run(["beta", "requests"], session_factory, capsys, clock)
    assert (code, out.splitlines()) == (0, ["no pending applications"])

    first = await _submit(session_factory, APPLICANT, clock.now - timedelta(hours=2))
    second = await _submit(session_factory, APPLICANT + 1, clock.now, username=None, language="ru")
    old = await _submit(session_factory, APPLICANT + 2, clock.now - timedelta(days=40))
    recent = await _submit(session_factory, APPLICANT + 3, clock.now - timedelta(days=3))
    async with session_factory() as session:
        await applications.decide(
            session, old.id, approve=False, operator_id=None, now=clock.now - timedelta(days=31)
        )
        await applications.decide(
            session, recent.id, approve=True, operator_id=None, now=clock.now - timedelta(days=1)
        )
        await session.commit()

    def block(app: BetaApplication, status: str, who: str, lang: str) -> list[str]:
        created = f"{app.consent_at.astimezone(UTC):%Y-%m-%d %H:%M}"
        return [
            f"{app.id}  {status}  {created}  {app.telegram_id}  {who}  {lang}",
            "    Synthetic use case",
            "    second synthetic line",
        ]

    code, out, err = await _run(["beta", "requests"], session_factory, capsys, clock)
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        *block(first, "pending", "@synthetic_applicant", "en"),
        *block(second, "pending", "-", "ru"),
    ]

    code, out, _ = await _run(["beta", "requests", "--all"], session_factory, capsys, clock)
    assert out.splitlines() == [
        *block(recent, "approved", "@synthetic_applicant", "en"),
        *block(first, "pending", "@synthetic_applicant", "en"),
        *block(second, "pending", "-", "ru"),
    ]


async def test_beta_approve_and_reject(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    approved = await _submit(session_factory, APPLICANT, clock.now)
    rejected = await _submit(session_factory, APPLICANT + 1, clock.now, language="ru")
    bot, api = offline_bot()

    code = await admin.run(
        ["beta", "approve", str(approved.id)], session_factory, clock=clock, bot=bot
    )
    out, err = capsys.readouterr()
    assert (code, err) == (0, "")
    assert out.splitlines() == [f"approved {APPLICANT}", APPLIES]
    entry = await db_session.scalar(select(BetaEntry).where(BetaEntry.telegram_id == APPLICANT))
    assert entry is not None and entry.added_by == "operator"
    kinds = [e.kind for e in await _events(db_session) if e.telegram_id == APPLICANT]
    assert set(kinds) == {
        AccessEventKind.application_submitted,
        AccessEventKind.application_approved,
        AccessEventKind.beta_added,
    }
    assert api.sent_texts == [t("access.apply_approved")]
    (edit,) = api.of_type(EditMessageText)
    assert (edit.chat_id, edit.message_id) == (900000031, 77)
    cli = t("access.apply_operator_by_cli")
    when = f"{clock.now.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    assert (edit.text or "").endswith(t("access.apply_operator_approved", by=cli, when=when))
    db_session.expire_all()
    stored = await db_session.get(BetaApplication, approved.id)
    assert stored is not None and stored.decided_by is None
    assert stored.status == BetaApplicationStatus.approved

    api.clear()
    code = await admin.run(
        ["beta", "reject", str(rejected.id)], session_factory, clock=clock, bot=bot
    )
    out, _ = capsys.readouterr()
    assert (code, out.splitlines()) == (0, [f"rejected {APPLICANT + 1}"])
    day = format_day_month_year((clock.now + timedelta(days=30)).date(), "ru")
    assert api.sent_texts == [t("access.apply_rejected", "ru", date=day)]
    assert (
        await db_session.scalar(select(BetaEntry).where(BetaEntry.telegram_id == APPLICANT + 1))
        is None
    )

    # Not pending any more, or unknown: exit 1, nothing sent.
    api.clear()
    for argv in (
        ["beta", "approve", str(approved.id)],
        ["beta", "reject", str(approved.id)],
        ["beta", "approve", str(uuid.uuid4())],
    ):
        code = await admin.run(argv, session_factory, clock=clock, bot=bot)
        out, err = capsys.readouterr()
        assert (code, out) == (1, "") and err, argv
    assert api.requests == []

    with pytest.raises(SystemExit) as exc:
        await admin.run(["beta", "approve", "not-a-uuid"], session_factory)
    assert exc.value.code == 2
    capsys.readouterr()


async def test_access_stats_counts_applications(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _submit(session_factory, APPLICANT, clock.now)
    approved = await _submit(session_factory, APPLICANT + 1, clock.now)
    rejected = await _submit(session_factory, APPLICANT + 2, clock.now)
    old = await _submit(session_factory, APPLICANT + 3, clock.now - timedelta(days=20))
    async with session_factory() as session:
        for app, approve, at in (
            (approved, True, clock.now),
            (rejected, False, clock.now),
            (old, False, clock.now - timedelta(days=10)),
        ):
            await applications.decide(session, app.id, approve=approve, operator_id=None, now=at)
        await session.commit()

    code, out, _ = await _run(["access", "stats"], session_factory, capsys, clock)
    lines = out.splitlines()
    assert "applications pending: 1" in lines
    assert "applications approved / rejected (last 7 days): 1 / 1" in lines
    code, out, _ = await _run(["access", "stats", "--days", "30"], session_factory, capsys, clock)
    assert "applications approved / rejected (last 30 days): 1 / 2" in out.splitlines()


# --- Stars subscriptions in the access commands (specs/015 T042) --------------------------------


async def _stars_period(
    db: AsyncSession, clock: FrozenClock, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.payments_fakes import credit_first

    await credit_first(db, telegram_id=TESTER, charge_id="charge-adm-1", at=clock.now)
    await db.commit()
    capsys.readouterr()  # the credit logs a line to stdout; not command output


async def test_sub_list_and_access_show_print_the_source_once_stars_are_in_use(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
) -> None:
    await _run(["sub", "grant", str(OTHER), "--days", "5"], session_factory, capsys, clock)
    await _stars_period(db_session, clock, capsys)

    code, out, _ = await _run(["sub", "list"], session_factory, capsys, clock)

    assert code == 0
    rows = sorted(line.split() for line in out.splitlines())
    assert rows == [
        [str(TESTER), "active", "stars", "2026-10-14", "2026-11-13"],
        [str(OTHER), "active", "operator", "2026-10-14", "2026-10-19"],
    ] or rows == sorted(
        [
            [str(TESTER), "active", "stars", "2026-10-14", "2026-11-13"],
            [str(OTHER), "active", "operator", "2026-10-14", "2026-10-19"],
        ]
    )

    code = await admin.run(
        ["access", "show", str(TESTER)], session_factory, clock=clock, settings=settings
    )
    lines = capsys.readouterr()[0].splitlines()
    assert code == 0
    assert "subscription active stars 2026-10-14 2026-11-13" in lines


async def test_sub_extend_and_end_leave_stars_rows_alone_and_point_to_pay(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    await _stars_period(db_session, clock, capsys)
    hint = "stars subscription running: use `pay cancel`/`pay refund`"

    code, out, err = await _run(["sub", "end", str(TESTER)], session_factory, capsys, clock)
    assert code == 1 and "no active or scheduled subscription" in err and hint in err
    code, out, err = await _run(
        ["sub", "extend", str(TESTER), "--days", "3"], session_factory, capsys, clock
    )
    assert code == 1 and hint in err

    await _run(["sub", "grant", str(TESTER), "--days", "2"], session_factory, capsys, clock)
    code, out, _ = await _run(["sub", "end", str(TESTER)], session_factory, capsys, clock)
    assert code == 0 and hint in out.splitlines()

    db_session.expire_all()
    rows = (await db_session.scalars(select(Subscription).order_by(Subscription.created_at))).all()
    by_source = {row.source.value: row for row in rows}
    assert by_source["stars"].cancelled_at is None  # untouched
    assert by_source["operator"].cancelled_at == clock.now
    assert by_source["stars"].ends_at == clock.now + timedelta(days=30)


async def test_access_stats_counts_paying_subscribers(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
) -> None:
    _, out, _ = await _run(["access", "stats"], session_factory, capsys, clock)
    assert not any(line.startswith("paying subscribers") for line in out.splitlines())

    await _stars_period(db_session, clock, capsys)
    _, out, _ = await _run(["access", "stats"], session_factory, capsys, clock)
    lines = out.splitlines()
    assert "paying subscribers: 1" in lines
    assert lines.index("paying subscribers: 1") == lines.index("subscriptions active: 1") + 1
