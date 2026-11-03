"""JQL builders and Jira string helpers (specs/003-jira-commitments, research J7, J8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from scribe.context.tasks.jira.jql import (
    ISSUE_KEY,
    jql_datetime,
    parse_version,
    quote_keys,
    scan_jql,
    tasks_jql,
)

INVOLVED = "(assignee = currentUser() OR reporter = currentUser() OR watcher = currentUser())"


def test_tasks_jql_exact() -> None:
    assert tasks_jql(["PAY", "CORE"]) == (
        'project in ("PAY","CORE") AND assignee = currentUser() '
        "AND statusCategory != Done ORDER BY duedate ASC"
    )


def test_scan_jql_relative_since_is_inserted_as_is() -> None:
    assert scan_jql(["PAY"], "-30d") == (
        f'project in ("PAY") AND {INVOLVED} AND updated >= -30d ORDER BY updated ASC'
    )


def test_scan_jql_absolute_since_is_quoted() -> None:
    assert scan_jql(("PAY", "CORE"), "2026/10/21 09:25") == (
        f'project in ("PAY","CORE") AND {INVOLVED} '
        'AND updated >= "2026/10/21 09:25" ORDER BY updated ASC'
    )


@pytest.mark.parametrize(
    "since",
    ["30d", "-30w", "2026-10-21 09:25", '2026/10/21 09:25" OR 1=1', "", "now()", "-5d OR x"],
)
def test_scan_jql_rejects_other_since(since: str) -> None:
    with pytest.raises(ValueError):
        scan_jql(["PAY"], since)


def test_quote_keys_dedupes_and_keeps_order() -> None:
    assert quote_keys(["CORE", "PAY", "CORE", "A1_B"]) == '"CORE","PAY","A1_B"'
    assert quote_keys(iter(["PAY"])) == '"PAY"'


@pytest.mark.parametrize(
    "keys", [["pay"], ["PAY-1"], ['P"; DROP'], [], ["PAY", "P"], ["1PAY"], ["PAY\n"]]
)
def test_invalid_keys_raise(keys: list[str]) -> None:
    with pytest.raises(ValueError):
        quote_keys(keys)
    with pytest.raises(ValueError):
        tasks_jql(keys)
    with pytest.raises(ValueError):
        scan_jql(keys, "-30d")


def test_jql_datetime_in_user_zone() -> None:
    moment = datetime(2026, 10, 21, 21, 5, 59, tzinfo=UTC)
    assert jql_datetime(moment, "Europe/Moscow") == "2026/10/22 00:05"
    assert jql_datetime(moment, "UTC") == "2026/10/21 21:05"
    assert jql_datetime(moment, None) == "2026/10/21 21:05"
    assert jql_datetime(moment, "Mars/Olympus") == "2026/10/21 21:05"
    assert jql_datetime(moment, "") == "2026/10/21 21:05"


def test_jql_datetime_converts_other_offsets_and_is_valid_since() -> None:
    moment = datetime(2026, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=-5)))
    rendered = jql_datetime(moment, "Europe/Moscow")
    assert rendered == "2026/01/02 11:04"
    assert f'updated >= "{rendered}"' in scan_jql(["PAY"], rendered)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.3.4", (10, 3, 4)),
        ("11.3.1-SNAPSHOT", (11, 3, 1)),
        ("9.12.0.1", (9, 12, 0, 1)),
        (" 8.20 ", (8, 20)),
        ("10", (10,)),
    ],
)
def test_parse_version(text: str, expected: tuple[int, ...]) -> None:
    assert parse_version(text) == expected
    assert parse_version("9.12.0") < parse_version("10.3.4")


@pytest.mark.parametrize("text", ["", "SNAPSHOT", "v10.3", "."])
def test_parse_version_rejects_non_versions(text: str) -> None:
    with pytest.raises(ValueError):
        parse_version(text)


def test_issue_key_pattern() -> None:
    assert ISSUE_KEY.findall("see PAY-142, ok?") == ["PAY-142"]
    assert ISSUE_KEY.findall("pay-142") == []
    assert ISSUE_KEY.findall("CORE_2-7 and XPAY-142x") == ["CORE_2-7"]
