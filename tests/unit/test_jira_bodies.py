"""Jira comment bodies as plain text: ADF (Cloud v3) and wiki markup (self-hosted, Cloud v2)
give the same result for the same comment (SC-008)."""

from __future__ import annotations

from typing import Any

import pytest

from scribe.context.tasks.jira.bodies import CommentText, Mention, adf_to_text, wiki_to_text
from tests.contract.fixtures.jira.render import (
    Block,
    Bold,
    Bullets,
    Code,
    Link,
    MentionPart,
    Panel,
    Para,
    Quote,
    Table,
    Text,
    to_adf,
    to_wiki,
)

NAMES = {"acc-anna": "Anna Testova", "ivan.petrov": "Ivan Petrov", "acc_b-2": "Boris Kim"}


def resolve(user_id: str) -> str | None:
    return NAMES.get(user_id)


THREADS: dict[str, tuple[list[Block], str, tuple[str, ...], tuple[str, ...]]] = {
    # name: (blocks, expected plain, expected quoted, expected mention names)
    "promise_with_mention": (
        [Para(MentionPart("acc-anna", "Anna Testova"), Text(" sure, I'll send it by Friday."))],
        "@Anna Testova sure, I'll send it by Friday.",
        (),
        ("Anna Testova",),
    ),
    "quote_is_excluded": (
        [
            Quote(Para(MentionPart("ivan.petrov", "Ivan Petrov"), " can you fix it by 5 Nov?")),
            Para("Ok, will do by ", Bold("2026-11-05"), "-ish — well-known issue."),
        ],
        "Ok, will do by 2026-11-05-ish — well-known issue.",
        ("@Ivan Petrov can you fix it by 5 Nov?",),
        (),
    ),
    "formatting_links_lists": (
        [
            Para(Bold("Plan"), Text(" for "), Link("the spec", "https://x.example/a_b-c")),
            Bullets("first step", Para(Text("ping "), MentionPart("acc_b-2", "Boris Kim"))),
            Para("Done  by   end of week"),
        ],
        "Plan for the spec\n- first step\n- ping @Boris Kim\nDone by end of week",
        (),
        ("Boris Kim",),
    ),
    "code_panel_table": (
        [
            Para("Repro:"),
            Code("run_job --since -30d"),
            Panel("Deploy freeze until Monday"),
            Table(("Task", "Due"), ("PAY-142", "2026-11-05")),
        ],
        "Repro:\nrun_job --since -30d\nDeploy freeze until Monday\n"
        "Task | Due\nPAY-142 | 2026-11-05",
        (),
        (),
    ),
    "repeated_mentions_once": (
        [
            Para(MentionPart("acc-anna", "Anna Testova"), " and ", MentionPart("ivan.petrov", "x")),
            Para(MentionPart("acc-anna", "Anna Testova"), " again"),
        ],
        "@Anna Testova and @Ivan Petrov\n@Anna Testova again",
        (),
        ("Anna Testova", "Ivan Petrov"),
    ),
}


def _flavors(blocks: list[Block]) -> list[CommentText]:
    return [
        adf_to_text(to_adf(blocks), resolve),
        wiki_to_text(to_wiki(blocks, cloud=True), resolve),
        wiki_to_text(to_wiki(blocks, cloud=False), resolve),
    ]


@pytest.mark.parametrize("name", sorted(THREADS))
def test_same_text_for_both_flavors(name: str) -> None:
    blocks, plain, quoted, mention_names = THREADS[name]
    results = _flavors(blocks)
    for result in results:
        assert result.plain == plain
        assert result.quoted == quoted
        assert tuple(m.display_name for m in result.mentions) == mention_names
    assert results[0] == results[1] == results[2]


def test_mentions_carry_ids() -> None:
    blocks = THREADS["promise_with_mention"][0]
    assert adf_to_text(to_adf(blocks), resolve).mentions == (
        Mention(id="acc-anna", display_name="Anna Testova"),
    )


def test_mention_fallbacks_without_resolver() -> None:
    blocks: list[Block] = [Para(MentionPart("acc-x", "Lena Orlova"), " ok")]
    assert adf_to_text(to_adf(blocks)).plain == "@Lena Orlova ok"
    assert wiki_to_text(to_wiki(blocks, cloud=True)).plain == "@acc-x ok"
    assert wiki_to_text("[~lena]: ok", lambda _id: None).mentions == (
        Mention(id="lena", display_name="lena"),
    )


def test_wiki_bq_lines_and_headings() -> None:
    text = wiki_to_text("h2. Status\nbq. Can you ship it?\n# yes\n# by Friday\n----\nthanks\\\\bye")
    assert text.plain == "Status\n- yes\n- by Friday\nthanks\nbye"
    assert text.quoted == ("Can you ship it?",)


def test_wiki_markers_kept_inside_words_and_code() -> None:
    markup = (
        "Use {{snake_case_name}} and see [https://x.example/a-b_c_]\n"
        "C++ and 2*3*4 and a - b - c, +under+ -gone- _it_\n"
        "{color:red}red{color} {noformat}*raw* text{noformat}\n"
        "{code:java}int x = a_b_c;{code}"
    )
    assert wiki_to_text(markup).plain == (
        "Use snake_case_name and see https://x.example/a-b_c_\n"
        "C++ and 2*3*4 and a - b - c, under gone it\n"
        "red\n*raw* text\nint x = a_b_c;"
    )


def test_wiki_literal_brackets_kept() -> None:
    assert wiki_to_text("[draft] see [docs|https://d.example] or [#anchor]").plain == (
        "[draft] see docs or #anchor"
    )


def test_adf_other_nodes_degrade_to_text() -> None:
    doc: dict[str, Any] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "emoji", "attrs": {"shortName": ":ok:", "text": "👌"}},
                    {"type": "text", "text": " due "},
                    {"type": "date", "attrs": {"timestamp": "1793836800000"}},
                    {"type": "hardBreak"},
                    {"type": "status", "attrs": {"text": "IN REVIEW"}},
                    {"type": "text", "text": " "},
                    {"type": "inlineCard", "attrs": {"url": "https://x.example/PAY-1"}},
                ],
            },
            {"type": "rule"},
            {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m"}}]},
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "H"}]},
            {"type": "futureContainer", "content": [{"type": "text", "text": "kept"}]},
            {"type": "futureLeaf", "attrs": {"text": "dropped"}},
            {"type": "orderedList", "content": [{"type": "listItem", "content": ["bad", 3]}]},
            "not a node",
            {"type": "blockCard", "attrs": {"url": "https://x.example/card"}},
        ],
    }
    assert adf_to_text(doc).plain == (
        "👌 due 2026-11-05\nIN REVIEW https://x.example/PAY-1\nH\nkept\nhttps://x.example/card"
    )


@pytest.mark.parametrize(
    "doc", [None, [], "text", 3, {"type": "doc", "content": "x"}, {"content": [None, 1]}, {}]
)
def test_malformed_adf_yields_empty(doc: Any) -> None:
    assert adf_to_text(doc) == CommentText(plain="", quoted=(), mentions=())


@pytest.mark.parametrize("markup", [None, 3, "", "   \n\n ", "{quote}{quote}"])
def test_malformed_wiki_yields_empty(markup: Any) -> None:
    assert wiki_to_text(markup) == CommentText()


def test_unterminated_wiki_blocks_do_not_raise() -> None:
    assert wiki_to_text("{quote}half a quote").plain == "half a quote"
    assert wiki_to_text("{code}x = 1").plain == "x = 1"
