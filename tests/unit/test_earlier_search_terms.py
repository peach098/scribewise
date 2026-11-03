"""Query planning for the search over earlier conversations (issue #108: ``search_terms``).

Telegram matches whole words, so phrases the filter writes ("workshop venue", «дата поставки»)
miss an earlier message that words the topic differently. The stems the filter's queries share
lead the list, within the same request budget.
"""

from __future__ import annotations

import pytest

from scribe.config import Settings
from scribe.context.earlier import MAX_QUERIES, EarlierSearch, search_terms, stem
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.integration.earlier_support import (
    load_search_index,
    make_scope,
    private_chat,
    private_found,
)


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("поставки", "поставк"),
        ("Поставку", "поставк"),
        ("аудита", "аудит"),
        ("конфигурация", "конфигураци"),
        ("счёт", "счёт"),  # too short to cut
        ("дата", "дата"),
        ("approved", "approv"),
        ("guidelines", "guidelin"),
        ("workshop", "workshop"),
        ("address", "address"),  # "ss" is not a plural
        ("SLA", "sla"),
    ],
)
def test_stem(word: str, expected: str) -> None:
    assert stem(word) == expected
    assert word.casefold().startswith(stem(word))  # a prefix: every word form still matches


@pytest.mark.parametrize(
    ("queries", "expected"),
    [
        # Issue #108 (eval threads en-meeting-room, en-media-caption, ru-inject-plus-legit):
        # the filter's phrases matched nothing, the shared word does.
        (
            ["workshop venue", "workshop location", "where workshop", "мастер-класс место"],
            (
                "workshop",
                "workshop venue",
                "workshop location",
                "where workshop",
                "мастер-класс место",
            ),
        ),
        (
            ["brand guide", "brand guidelines", "approved version", "version approved"],
            ("brand", "approv", "brand guide", "brand guidelines", "approved version"),
        ),
        (
            ["дата поставки", "поставки", "сроки поставки", "delivery date"],
            ("поставк", "дата поставки", "поставки", "сроки поставки", "delivery date"),
        ),
        # Nothing shared: the filter's queries as they are.
        (["budget", "approved"], ("budget", "approved")),
        (["счёт 4521", "счет"], ("счёт 4521", "счет")),
        # A shared stem equal to a query is not repeated; stop words and numbers never lead.
        (
            ["аудит", "стоимость аудита", "what audit", "what price"],
            ("аудит", "стоимость аудита", "what audit", "what price"),
        ),
        (["invoice 2207", "2207 paid"], ("invoice 2207", "2207 paid")),
        # Blank and duplicate queries dropped, capped like before.
        (["q1", "q2", "q2", " ", "q3", "q4", "q5", "q6"], ("q1", "q2", "q3", "q4", "q5")),
        ([], ()),
    ],
)
def test_search_terms(queries: list[str], expected: tuple[str, ...]) -> None:
    assert search_terms(queries) == expected


def test_search_terms_keep_the_budget() -> None:
    queries = ["budget pilot", "pilot budget", "approved budget", "pilot plan", "pilot cost"]
    terms = search_terms(queries)
    assert terms[:2] == ("pilot", "budget")  # most shared first
    assert len(terms) == MAX_QUERIES


async def test_search_finds_another_word_form(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    """End to end over the fake gateway (substring matching): «Поставку» is found although no
    query of the filter contains that form."""
    hit = private_found("Поставку согласовали на 3 декабря")
    load_search_index(gateway, [hit])

    result = await EarlierSearch(gateway, settings).search(
        make_scope(), [private_chat()], ["дата поставки", "поставки", "сроки поставки"]
    )

    assert result.outcome == "ok"
    assert [m.found.message_id for m in result.matches] == [hit.message_id]
    assert result.queries[0] == "поставк"
