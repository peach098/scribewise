"""Pure rules of commitments from Jira comments (specs/003-jira-commitments T030, J9, J10)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from scribe.context.comment_commitments import (
    CommentCommitmentInput,
    best_item,
    excerpt,
    fold_name,
    ground_comment_item,
    issue_keys_in,
    request_for,
    same_promise,
)
from scribe.context.tasks.jira.bodies import CommentText, Mention
from scribe.context.tasks.jira.source import JiraComment
from scribe.context.tasks.jira.sync import counterparty, pick_request
from scribe.llm.provider import CommitmentItem

TZ = "Europe/Moscow"
WED = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)  # Wednesday


def _job(text: str, request: str | None = None, **values: object) -> CommentCommitmentInput:
    return CommentCommitmentInput(
        user_id=uuid.uuid4(),
        issue_key="PAY-142",
        issue_title="Fix refund rounding",
        comment_id="10002",
        comment_url="https://acme.atlassian.net/browse/PAY-142?focusedCommentId=10002",
        comment_text=text,
        restricted=False,
        posted_at=WED,
        updated_at=WED,
        counterparty="Anna Testova",
        request_text=request,
        request_author="Anna Testova" if request else None,
        request_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC) if request else None,
        **values,
    )


def _item(what: str, due: date | None, evidence: str, confidence: float = 0.9) -> CommitmentItem:
    return CommitmentItem(
        what=what, to_whom="Anna Testova", due=due, confidence=confidence, evidence=evidence
    )


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("send the logs", "Send the logs.", True),
        ("send the logs", "send logs to Ivan", True),
        ("прислать логи", "Прислать логи сервера", True),
        ("send the logs", "fix the refund bug", False),
        ("send the load test report to Anna", "send the invoice", False),  # stop words only
        ("send the report", "send the invoice", False),
        ("", "send", False),
    ],
)
def test_same_promise(a: str, b: str, same: bool) -> None:
    assert same_promise(a, b) is same


def test_names_fold_case_and_diacritics() -> None:
    assert fold_name("  Anna  Testová ") == fold_name("anna testova")
    assert fold_name("Алёна") == fold_name("алена")


def test_issue_keys_only_from_selected_projects() -> None:
    text = "PAY-142 and CORE-7 and OTHER-1, again PAY-142; pay-9"
    assert issue_keys_in(text, ["PAY", "CORE"]) == ["PAY-142", "CORE-7"]
    assert issue_keys_in(text, []) == []


def test_excerpt_is_short() -> None:
    assert len(excerpt("x " * 400)) == 280
    assert excerpt(" a  b ") == "a b"


def test_own_promise_keeps_its_date() -> None:
    job = _job("@Anna Testova I'll have the fix on staging by Friday")
    item = _item("have the fix on staging", date(2026, 10, 23), "I'll have the fix on staging")
    grounded = ground_comment_item(item, job, TZ)
    assert grounded is not None and grounded.due == date(2026, 10, 23)


def test_evidence_must_be_the_users_words() -> None:
    job = _job("sure", request="can you send the logs by Friday?")
    assert ground_comment_item(_item("send the logs", None, "send the logs"), job, TZ) is None


def test_accepted_request_takes_the_requests_date() -> None:
    job = _job("sure, will do", request="@Mia can you send the logs by Friday?")
    item = _item("send the logs", date(2026, 10, 23), "sure, will do")
    grounded = ground_comment_item(item, job, TZ)
    assert grounded is not None and grounded.due == date(2026, 10, 23)


def test_a_date_nobody_gave_is_dropped() -> None:
    job = _job("sure, will do", request="@Mia can you send the logs?")
    grounded = ground_comment_item(_item("send the logs", date(2026, 10, 23), "will do"), job, TZ)
    assert grounded is not None and grounded.due is None


def test_best_item_is_the_most_confident_grounded_one() -> None:
    job = _job("I'll send the logs today and fix it by Friday")
    items = [
        _item("send the logs", date(2026, 10, 21), "send the logs today", 0.8),
        _item("fix it", date(2026, 10, 23), "fix it by Friday", 0.95),
        _item("invented", None, "not in the comment", 0.99),
        _item("weak", None, "send the logs", 0.5),
    ]
    best = best_item(items, job, TZ)
    assert best is not None and best.what == "fix it"
    assert best_item([], job, TZ) is None


def test_request_shape_has_display_names_only() -> None:
    request = request_for(_job("sure", request="can you?"), TZ)
    assert request.source == "jira_comment" and request.issue is not None
    assert request.issue.key == "PAY-142"
    assert request.contact_display_name == "Anna Testova"
    (incoming,) = request.recent_incoming
    assert incoming.sender == "Anna Testova" and incoming.text == "can you?"
    assert request.sent_at.utcoffset() is not None
    assert request_for(_job("sure"), TZ).recent_incoming == []


def _comment(
    cid: str, author: str, name: str, text: str, day: int, mentions: tuple[str, ...] = ()
) -> JiraComment:
    at = datetime(2026, 10, day, 9, 0, tzinfo=UTC)
    return JiraComment(
        id=cid,
        author_id=author,
        author_name=name,
        created=at,
        updated=at,
        text=CommentText(
            plain=text, mentions=tuple(Mention(id=m, display_name=m.upper()) for m in mentions)
        ),
        restricted=False,
    )


def test_request_is_the_related_earlier_comment() -> None:
    mine = _comment("3", "me", "Mia", "sure", 12, mentions=("anna",))
    earlier = [
        _comment("1", "anna", "Anna", "can you fix it?", 10),
        _comment("2", "ivan", "Ivan", "unrelated", 11),
    ]
    assert pick_request(mine, earlier, "me") is earlier[0]  # its author is mentioned
    plain = _comment("4", "me", "Mia", "ok", 12)
    assert pick_request(plain, earlier, "me") is earlier[1]  # else the newest other comment
    asking = _comment("5", "ivan", "Ivan", "@Mia can you?", 11, mentions=("me",))
    assert pick_request(plain, [earlier[0], asking], "me") is asking
    assert pick_request(plain, [_comment("6", "me", "Mia", "mine", 9)], "me") is None


def test_counterparty_order() -> None:
    request = _comment("1", "anna", "Anna Testova", "can you?", 10)
    mentioning = _comment("2", "me", "Mia", "@IVAN sure", 11, mentions=("me", "ivan"))
    assert counterparty(mentioning, request, "Rita Reporter", "me") == "IVAN"
    plain = _comment("3", "me", "Mia", "sure", 11)
    assert counterparty(plain, request, "Rita Reporter", "me") == "Anna Testova"
    assert counterparty(plain, None, "Rita Reporter", "me") == "Rita Reporter"
