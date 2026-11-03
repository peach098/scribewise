"""Jira comment sync end to end (specs/003-jira-commitments T031). Postgres, a fake Jira site
(both flavors) and the fake LLM adapter; names quickstart.md lists are the contract.
Synthetic data only."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context.tasks.jira.sync import JiraCommentSync
from scribe.db.models import (
    Commitment,
    CommitmentAutoNote,
    CommitmentClosedBy,
    CommitmentSourceKind,
    CommitmentState,
    ConnectionStatus,
    TaskProvider,
    User,
    UserState,
)
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import jira_cursor
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Msg
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import CommitmentExtraction, CommitmentItem
from scribe.llm.registry import LLMRouter
from tests.conftest import FrozenClock, LogCapture, MakeContact, MakeUser
from tests.contract.fixtures.jira import api
from tests.contract.fixtures.jira.api import (
    ANNA,
    FLAVORS,
    IVAN,
    ME,
    Comment,
    FakeJira,
    Flavor,
    Issue,
)
from tests.contract.fixtures.jira.render import MentionPart, Para, Quote, Text
from tests.integration.jira_support import JiraSite, connect_jira, install_jira_site

CREATED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> JiraSite:
    return install_jira_site(monkeypatch)


@pytest.fixture
def notices() -> list[tuple[Any, str]]:
    return []


@pytest.fixture
def sync(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    settings: Settings,
    clock: FrozenClock,
    notices: list[tuple[Any, str]],
) -> JiraCommentSync:
    async def notify(user_id: Any, message: Msg) -> None:
        notices.append((user_id, message.render("en")))

    one_at_a_time = settings.model_copy(update={"jira_sync_concurrency": 1})
    return JiraCommentSync(session_factory, llm_router, one_at_a_time, clock=clock, notifier=notify)


def _block(call: FakeCall, name: str) -> Any:
    for item in call.user_blocks:
        if item.name == name:
            return json.loads(item.content)
    raise KeyError(name)


def extraction(rules: dict[str, CommitmentItem | None]):  # type: ignore[no-untyped-def]
    """Fake ``extract.commitments``: an item when the comment contains a rule's key."""

    def respond(call: FakeCall) -> CommitmentExtraction:
        text = _block(call, "outgoing_text")
        for needle, item in rules.items():
            if needle in text:
                return CommitmentExtraction(items=[item] if item is not None else [])
        return CommitmentExtraction(items=[])

    return respond


def _item(
    what: str, due: date | None, evidence: str, to_whom: str = "Anna Testova"
) -> CommitmentItem:
    return CommitmentItem(what=what, to_whom=to_whom, due=due, confidence=0.9, evidence=evidence)


PROMISE = [
    Para(MentionPart(ANNA.id, ANNA.name), Text(" I'll have the fix on staging by Wednesday"))
]
STAGING = _item("have the fix on staging", date(2026, 10, 14), "I'll have the fix on staging")


def _issue(*comments: Comment, **values: Any) -> Issue:
    return Issue(
        values.pop("key", "PAY-142"), "Fix refund rounding", CREATED, comments=list(comments),
        updated=values.pop("updated", _at(12)), **values,
    )  # fmt: skip


async def _rows(session_factory: async_sessionmaker[AsyncSession], user: User) -> list[Commitment]:
    async with session_factory() as session:
        result = await session.execute(
            select(Commitment).where(Commitment.user_id == user.id).order_by(Commitment.created_at)
        )
        return list(result.scalars().all())


async def _user(
    make_user: MakeUser, session_factory: async_sessionmaker[AsyncSession], flavor: Flavor
) -> User:
    user = await make_user(state=UserState.active, timezone="Europe/Moscow")
    await connect_jira(session_factory, user, flavor)
    return user


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_own_promise_creates_commitment_with_link(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    notices: list[tuple[Any, str]],
) -> None:
    user = await _user(make_user, session_factory, flavor)
    site.serve(FakeJira(flavor, issues=[_issue(Comment("10002", ME, PROMISE, _at(12)))]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))

    report = await sync.sync_user(user.id)
    assert (report.created, report.comments_sent_to_llm, report.error) == (1, 1, None)
    (row,) = await _rows(session_factory, user)
    assert row.source_kind == CommitmentSourceKind.jira_comment
    assert (row.what, row.due, row.to_whom, row.contact_id) == (
        "have the fix on staging",
        date(2026, 10, 14),
        "Anna Testova",
        None,
    )
    assert row.jira_issue_key == "PAY-142" and row.jira_comment_id == "10002"
    assert row.jira_comment_url == f"{api.site(flavor)}/browse/PAY-142?focusedCommentId=10002"
    assert row.source_message_text == "I'll have the fix on staging"
    (call,) = fake_llm.calls_for("extract.commitments")
    assert _block(call, "source") == "jira_comment"
    assert _block(call, "issue") == {"key": "PAY-142", "title": "Fix refund rounding"}
    assert notices and "Found 1 commitment in your Jira comments" in notices[0][1]


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_unaccepted_request_creates_nothing(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    request = Comment(
        "10001", ANNA, [Para(MentionPart(ME.id, ME.name), Text(" can you send logs by Friday?"))],
        _at(11),
    )  # fmt: skip
    site.serve(FakeJira(flavor, issues=[_issue(request)]))
    fake_llm.set_response("extract.commitments", extraction({}))
    report = await sync.sync_user(user.id)
    assert report.comments_sent_to_llm == 0  # other people's comments are never extracted
    assert await _rows(session_factory, user) == []


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_own_comment_without_promise_cue_skips_the_llm(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    """Issue #72: "Thanks, noted." answers no request and promises nothing."""
    user = await _user(make_user, session_factory, flavor)
    note = Comment("10001", ANNA, [Para(Text("FYI the build is green again."))], _at(11))
    thanks = Comment("10002", ME, [Para(Text("Thanks, noted."))], _at(12))
    site.serve(FakeJira(flavor, issues=[_issue(note, thanks)]))
    fake_llm.set_response("extract.commitments", extraction({}))
    report = await sync.sync_user(user.id)
    assert report.comments_sent_to_llm == 0 and report.error is None
    assert fake_llm.calls_for("extract.commitments") == []
    assert await _rows(session_factory, user) == []
    (gate,) = log_capture.events("commitment_extraction_gate")
    assert (gate["source"], gate["extract"], gate["reason"]) == ("jira_comment", False, "no_cue")
    assert "noted" not in log_capture.raw


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_accepted_request_uses_request_date(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    asking = [Para(MentionPart(ME.id, ME.name), Text(" can you send the logs by Friday?"))]
    request = Comment("10001", ANNA, asking, _at(12))
    reply = Comment("10002", ME, [Para(Text("sure, will do"))], _at(13))
    site.serve(FakeJira(flavor, issues=[_issue(request, reply)]))
    fake_llm.set_response(
        "extract.commitments",
        extraction({"sure": _item("send the logs", date(2026, 10, 16), "sure, will do")}),
    )
    await sync.sync_user(user.id)
    (row,) = await _rows(session_factory, user)
    assert (row.what, row.due, row.to_whom) == ("send the logs", date(2026, 10, 16), "Anna Testova")
    (call,) = fake_llm.calls_for("extract.commitments")
    (incoming,) = _block(call, "recent_incoming")
    assert incoming["sender"] == ANNA.name and "send the logs" in incoming["text"]


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_comment_edit_updates_not_duplicates(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    comment = Comment("10002", ME, PROMISE, _at(12))
    issue = _issue(comment)
    jira = site.serve(FakeJira(flavor, issues=[issue]))
    fake_llm.set_response(
        "extract.commitments",
        extraction(
            {
                "Wednesday": STAGING,
                "Thursday": _item(
                    "have the fix on staging", date(2026, 10, 15), "I'll have the fix on staging"
                ),
            }
        ),
    )
    await sync.sync_user(user.id)
    comment.blocks = [Para(Text("I'll have the fix on staging by Thursday"))]
    comment.updated = _at(13)
    issue.updated = _at(13)
    report = await sync.sync_user(user.id)
    assert report.updated == 1
    (row,) = await _rows(session_factory, user)
    assert row.due == date(2026, 10, 15) and row.state == CommitmentState.open
    assert len(jira.searches) == 2
    assert 'updated >= "' in jira.searches[1].url.params["jql"]  # incremental after the first


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_comment_delete_resolves_restorable(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    jira = site.serve(FakeJira(flavor, issues=[_issue(Comment("10002", ME, PROMISE, _at(12)))]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    await sync.sync_user(user.id)
    jira.deleted_comments.add("10002")
    report = await sync.verify_user(user.id)
    assert report.removed == 1
    (row,) = await _rows(session_factory, user)
    assert row.state == CommitmentState.resolved and row.closed_by == CommitmentClosedBy.system
    assert row.auto_note == CommitmentAutoNote.source_removed
    async with session_factory() as session:
        reopened = await commitments_repo.reopen(session, user.id, row.id)
        await session.commit()
    assert reopened is not None and reopened.user_modified
    await sync.verify_user(user.id)
    (row,) = await _rows(session_factory, user)
    assert row.state == CommitmentState.open  # a restored row is not closed again


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_user_modified_not_overwritten(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    comment = Comment("10002", ME, PROMISE, _at(12))
    issue = _issue(comment)
    site.serve(FakeJira(flavor, issues=[issue]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    await sync.sync_user(user.id)
    (row,) = await _rows(session_factory, user)
    async with session_factory() as session:
        await commitments_repo.update_open(session, user.id, row.id, what="my own wording")
        await session.commit()
    comment.blocks = [Para(Text("I'll have the fix on staging by Thursday"))]
    comment.updated = issue.updated = _at(13)
    await sync.sync_user(user.id)
    (row,) = await _rows(session_factory, user)
    assert (row.what, row.due) == ("my own wording", date(2026, 10, 14))


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_user_deleted_not_recreated(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    comment = Comment("10002", ME, PROMISE, _at(12))
    issue = _issue(comment)
    site.serve(FakeJira(flavor, issues=[issue]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    await sync.sync_user(user.id)
    (row,) = await _rows(session_factory, user)
    async with session_factory() as session:
        await commitments_repo.close(session, user.id, row.id, CommitmentState.deleted, at=clock())
        await session.commit()
    comment.blocks = [Para(Text("I'll have the fix on staging by Wednesday, really"))]
    comment.updated = issue.updated = _at(13)
    await sync.sync_user(user.id)
    rows = await _rows(session_factory, user)
    assert [r.state for r in rows] == [CommitmentState.deleted]


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_issue_done_auto_resolves(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    issue = _issue(Comment("10002", ME, PROMISE, _at(12)))
    site.serve(FakeJira(flavor, issues=[issue]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    await sync.sync_user(user.id)
    issue.status, issue.status_category, issue.updated = "Done", "done", _at(13)
    report = await sync.sync_user(user.id)
    assert report.auto_resolved == 1
    (row,) = await _rows(session_factory, user)
    assert row.state == CommitmentState.resolved and row.auto_note == CommitmentAutoNote.issue_done


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_unreachable_keeps_open(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    issues = [
        _issue(Comment("10002", ME, PROMISE, _at(12))),
        _issue(Comment("20002", ME, PROMISE, _at(12)), key="CORE-7"),
    ]
    jira = site.serve(FakeJira(flavor, issues=issues))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection_for_update(session, user.id)
        assert conn is not None
        conn.config_json = {**conn.config_json, "project_keys": ["PAY", "CORE"]}
        await session.commit()
    await sync.sync_user(user.id)
    assert len(await _rows(session_factory, user)) == 2
    jira.gone.add("PAY-142")  # 404 now
    async with session_factory() as session:  # CORE deselected
        conn = await tasks_conn_repo.get_connection_for_update(session, user.id)
        assert conn is not None
        conn.config_json = {**conn.config_json, "project_keys": ["PAY"]}
        await session.commit()
    report = await sync.verify_user(user.id)
    assert report.unreachable == 2
    rows = await _rows(session_factory, user)
    assert {r.state for r in rows} == {CommitmentState.open}
    assert {r.auto_note for r in rows} == {CommitmentAutoNote.source_unreachable}


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_unchanged_comment_not_resent_to_llm(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    site.serve(FakeJira(flavor, issues=[_issue(Comment("10002", ME, PROMISE, _at(12)))]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    await sync.sync_user(user.id)
    second = await sync.sync_user(user.id)
    assert second.comments_sent_to_llm == 0
    assert len(fake_llm.calls_for("extract.commitments")) == 1
    async with session_factory() as session:
        assert await jira_cursor.count_for_user(session, user.id) == 1


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_other_peoples_comments_never_sent(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    asked = "Anna asks: can you fix it by Friday?"
    comments = [
        Comment("1", IVAN, [Para(Text("Ivan's private thoughts"))], _at(9)),
        Comment("2", ANNA, [Para(Text(asked))], _at(10)),
        Comment("3", ME, [Quote(Para(Text(asked))), Para(Text("ok, will do"))], _at(11)),
        Comment("4", IVAN, [Para(Text("later chatter"))], _at(12)),
    ]  # fmt: skip
    site.serve(FakeJira(flavor, issues=[_issue(*comments)]))
    fake_llm.set_response("extract.commitments", extraction({}))
    await sync.sync_user(user.id)
    (call,) = fake_llm.calls_for("extract.commitments")
    sent = " ".join(b.content for b in call.user_blocks)
    assert "ok, will do" in sent
    assert "Anna asks" in sent  # the one request it answers
    assert "private thoughts" not in sent and "later chatter" not in sent
    assert _block(call, "outgoing_text") == "ok, will do"  # the quote is not the user's words
    assert ME.id not in sent and ANNA.id not in sent  # display names only


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_restricted_comment_has_no_excerpt(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    comment = Comment("10002", ME, PROMISE, _at(12), restricted=True)
    site.serve(FakeJira(flavor, issues=[_issue(comment)]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    await sync.sync_user(user.id)
    (row,) = await _rows(session_factory, user)
    assert row.restricted and row.source_message_text is None


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_to_whom_falls_back_to_reporter(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    comment = Comment("10002", ME, [Para(Text("I'll ship it by Wednesday"))], _at(12))
    site.serve(FakeJira(flavor, issues=[_issue(comment, reporter=IVAN)]))
    item = _item("ship it", date(2026, 10, 14), "I'll ship it", to_whom="")
    fake_llm.set_response("extract.commitments", extraction({"ship": item}))
    await sync.sync_user(user.id)
    (call,) = fake_llm.calls_for("extract.commitments")
    assert _block(call, "contact_display_name") == IVAN.name
    (row,) = await _rows(session_factory, user)
    assert row.to_whom == IVAN.name


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_first_scan_30_days_and_cap(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    notices: list[tuple[Any, str]],
    settings: Settings,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    issues = [_issue(key=f"PAY-{n}") for n in range(1, 8)]
    jira = site.serve(FakeJira(flavor, issues=issues, page_size=5))
    fake_llm.set_response("extract.commitments", extraction({}))
    capped = JiraCommentSync(
        sync._session_factory,  # noqa: SLF001
        sync._router,  # noqa: SLF001
        settings.model_copy(update={"jira_first_scan_max_issues": 5, "jira_sync_concurrency": 1}),
        clock=sync._clock,  # noqa: SLF001
        notifier=sync._notifier,  # noqa: SLF001
    )
    report = await capped.sync_user(user.id)
    assert report.issues_read == 5
    jql = jira.searches[0].url.params["jql"]
    assert "updated >= -30d" in jql and jql.endswith("ORDER BY updated DESC")
    assert "watcher = currentUser()" in jql
    assert "Only the 5 most recently updated issues were scanned." in notices[-1][1]
    await capped.sync_user(user.id)
    assert len(notices) == 1  # reported once


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_watermark_overlap_and_timezone(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    jira = site.serve(FakeJira(flavor, issues=[_issue(updated=_at(12, 10))]))
    fake_llm.set_response("extract.commitments", extraction({}))
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection_for_update(session, user.id)
        assert conn is not None
        conn.config_json = {
            **conn.config_json,
            "identity": {**conn.config_json["identity"], "time_zone": "Europe/Moscow"},
        }
        await session.commit()
    await sync.sync_user(user.id)
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.sync_watermark == _at(12, 10)
    await sync.sync_user(user.id)
    # 10:00 UTC minus 5 minutes, in Moscow (UTC+3)
    assert 'updated >= "2026/10/12 12:55"' in jira.searches[1].url.params["jql"]


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_sync_rate_limit_backoff(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    site.serve(FakeJira(flavor, search_status=429, retry_after="600"))
    report = await sync.sync_user(user.id)
    assert report.error == "rate_limited"
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.next_sync_at == clock() + timedelta(seconds=600)
        assert conn.sync_watermark is None


async def test_sync_failure_counts_toward_task_source_error(
    site: JiraSite,
    sync: JiraCommentSync,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await _user(make_user, session_factory, "cloud")
    site.serve(FakeJira("cloud", search_status=500))
    for _ in range(3):
        await sync.sync_user(user.id)
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.status == ConnectionStatus.error
    site.fake.search_status = 200  # type: ignore[union-attr]
    await sync.sync_user(user.id)
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.status == ConnectionStatus.active


async def test_comment_picked_up_within_two_runs(
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    """SC-005: a comment posted right after a sync is processed by the next due run (5 min),
    well inside 15 minutes."""
    user = await _user(make_user, session_factory, "cloud")
    issue = _issue()
    site.serve(FakeJira("cloud", issues=[issue]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    assert await sync.run_due() == 1
    assert await sync.run_due() == 0  # not due again yet
    issue.comments.append(Comment("10002", ME, PROMISE, clock()))
    issue.updated = clock()
    clock.advance(minutes=5)
    assert await sync.run_due() == 1
    assert len(await _rows(session_factory, user)) == 1


async def test_no_comment_text_in_logs(
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    user = await _user(make_user, session_factory, "self_hosted")
    site.serve(FakeJira("self_hosted", issues=[_issue(Comment("10002", ME, PROMISE, _at(12)))]))
    fake_llm.set_response("extract.commitments", extraction({"staging": STAGING}))
    await sync.sync_user(user.id)
    await sync.verify_user(user.id)
    (finished,) = log_capture.events("jira_sync_finished")
    assert finished["created"] == 1 and finished["deployment"] == "self_hosted"
    assert "staging" not in log_capture.raw and "Fix refund" not in log_capture.raw
    assert api.PAT not in log_capture.raw


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_telegram_and_jira_merge(
    flavor: Flavor,
    site: JiraSite,
    sync: JiraCommentSync,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    user = await _user(make_user, session_factory, flavor)
    ivan = await make_contact(user, display_name="Ivan Sample")
    async with session_factory() as session:
        telegram = await commitments_repo.add_commitment(
            session, user.id, contact_id=ivan.id, what="send the logs", due=date(2026, 10, 16),
            source_sent_at=_at(10), confidence=0.9, source_message_text="I'll send the logs Friday",
        )  # fmt: skip
        await session.commit()
    comment = Comment(
        "10002", ME, [Para(MentionPart(IVAN.id, IVAN.name), Text(" I'll send logs on Friday"))],
        _at(12),
    )  # fmt: skip
    site.serve(FakeJira(flavor, issues=[_issue(comment)]))
    fake_llm.set_response(
        "extract.commitments",
        extraction(
            {"logs": _item("send logs", date(2026, 10, 16), "I'll send logs on Friday", IVAN.name)}
        ),
    )
    report = await sync.sync_user(user.id)
    assert report.merged == 1
    (row,) = await _rows(session_factory, user)
    assert row.id == telegram.id and row.contact_id == ivan.id
    assert row.source_kind == CommitmentSourceKind.telegram_message
    assert row.jira_comment_id == "10002" and row.jira_issue_key == "PAY-142"


async def test_sync_skips_locked_connection(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    """Two schedulers never claim the same connection (``FOR UPDATE SKIP LOCKED``)."""
    async with committed_session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=88_000_001, settings=settings, display_name="Synthetic"
        )
        await tasks_conn_repo.upsert_connection(
            session, user.id, provider=TaskProvider.jira, credentials={"pat": "x"}
        )
        await session.commit()

    async def claim() -> list[Any]:
        async with committed_session_factory() as session:
            claimed = await tasks_conn_repo.claim_due_jira(
                session, clock(), interval=timedelta(minutes=5)
            )
            await asyncio.sleep(0.2)  # hold the lock while the other claim runs
            await session.commit()
            return claimed

    first, second = await asyncio.gather(claim(), claim())
    assert sorted(len(c) for c in (first, second)) == [0, 1]
