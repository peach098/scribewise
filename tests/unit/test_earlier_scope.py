"""``EarlierSearch.search`` over ``FakeMessagingGateway`` (specs/004 research S4–S6, T015).

The database side of the scope (``chats_for``) lives in
``tests/integration/test_earlier_scope_db.py``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace

import pytest

from scribe.config import Settings
from scribe.context.earlier import (
    MAX_MATCHES,
    PER_REQUEST_LIMIT,
    EarlierMatch,
    EarlierSearch,
    SearchChat,
)
from scribe.db.models import ChatKind
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import (
    FoundMessage,
    GatewayBusy,
    GatewayNotStarted,
    SessionRevoked,
)
from tests.integration.earlier_support import (
    DISABLED_CHAT_ID,
    GROUP_CHAT_ID,
    OTHER_GROUP_CHAT_ID,
    PRIVATE_CHAT_ID,
    SENDER_PEER_ID,
    THIRD_PEER_ID,
    USER_PEER_ID,
    group_chat,
    group_found,
    load_search_index,
    make_scope,
    private_chat,
    private_found,
)


def _search(gateway: FakeMessagingGateway, settings: Settings) -> EarlierSearch:
    return EarlierSearch(gateway, settings)


def _ids(result_matches: list[EarlierMatch]) -> list[int]:
    return [m.found.message_id for m in result_matches]


# --- scope and request budget -------------------------------------------------------------------


async def test_searches_only_the_given_chats_with_every_query(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    hit = private_found("the budget is approved")
    load_search_index(
        gateway, [hit, group_found("budget in the disabled chat", chat_id=DISABLED_CHAT_ID)]
    )
    scope = make_scope()
    chats = [private_chat(), group_chat()]

    result = await _search(gateway, settings).search(scope, chats, ["budget", "approved"])

    assert result.outcome == "ok"
    assert {(chat, query) for _, chat, query in gateway.search_calls} == {
        (PRIVATE_CHAT_ID, "budget"),
        (PRIVATE_CHAT_ID, "approved"),
        (GROUP_CHAT_ID, "budget"),
        (GROUP_CHAT_ID, "approved"),
    }
    assert all(chat != DISABLED_CHAT_ID for _, chat, _ in gateway.search_calls)
    assert all(user == scope.user_id for user, _, _ in gateway.search_calls)
    assert _ids(result.matches) == [hit.message_id]
    assert result.stats.chats == 2 and result.stats.queries == 2


async def test_at_most_five_queries_by_four_chats(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    chats = [private_chat()] + [group_chat(-1_000_000_000_200 - n) for n in range(5)]
    queries = ["q1", "q2", "q2", " ", "q3", "q4", "q5", "q6", "q7"]

    result = await _search(gateway, settings).search(make_scope(), chats, queries)

    assert result.outcome == "no_match"
    assert len(gateway.search_calls) == 20
    assert {query for _, _, query in gateway.search_calls} == {"q1", "q2", "q3", "q4", "q5"}
    assert {chat for _, chat, _ in gateway.search_calls} == {c.telegram_chat_id for c in chats[:4]}
    assert result.queries == ("q1", "q2", "q3", "q4", "q5")
    assert result.stats.requests == 20


async def test_at_most_four_requests_in_flight(settings: Settings) -> None:
    @dataclass
    class CountingGateway(FakeMessagingGateway):
        active: int = 0
        peak: int = 0

        async def search_chat(
            self, user_id: uuid.UUID, chat_id: int, query: str, *, limit: int = 20
        ) -> list[FoundMessage]:
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return await super().search_chat(user_id, chat_id, query, limit=limit)
            finally:
                self.active -= 1

    gateway = CountingGateway()
    chats = [private_chat(), group_chat(), group_chat(OTHER_GROUP_CHAT_ID)]
    await _search(gateway, settings).search(make_scope(), chats, ["a", "b", "c", "d", "e"])

    assert len(gateway.search_calls) == 15
    assert gateway.peak == 4


async def test_no_chats_or_no_queries_makes_no_request(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    search = _search(gateway, settings)
    assert (await search.search(make_scope(), [], ["budget"])).outcome == "no_match"
    assert (await search.search(make_scope(), [private_chat()], [])).outcome == "no_match"
    assert (await search.search(make_scope(), [private_chat()], ["", "  "])).outcome == "no_match"
    assert gateway.search_calls == []


# --- de-duplication, cap, refs ------------------------------------------------------------------


async def test_known_empty_and_duplicate_matches(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    known = private_found("budget already in the recent window", days_ago=3)
    empty = private_found("   ")
    both = private_found("budget approved for the launch", days_ago=50)
    load_search_index(gateway, [known, empty, both])
    scope = make_scope(known={(PRIVATE_CHAT_ID, known.message_id)})

    result = await _search(gateway, settings).search(
        scope, [private_chat()], ["budget", "approved", " "]
    )

    assert _ids(result.matches) == [both.message_id]  # merged across the two queries
    assert result.stats.matches == 3  # raw hits: "known" and "both" for budget, "both" again


async def test_same_message_id_in_two_chats_is_two_matches(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    a = private_found("budget", message_id=42, days_ago=10)
    b = group_found("budget", message_id=42, days_ago=11)
    load_search_index(gateway, [a, b])

    result = await _search(gateway, settings).search(
        make_scope(), [private_chat(), group_chat()], ["budget"]
    )

    assert [(m.found.chat_id, m.found.message_id) for m in result.matches] == [
        (PRIVATE_CHAT_ID, 42),
        (GROUP_CHAT_ID, 42),
    ]


async def test_capped_at_sixty_newest_first_with_refs(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    queries = ["alpha", "beta", "gamma", "delta", "omega"]
    messages = [
        private_found(f"{queries[n % 5]} item {n}", days_ago=31 + n / 10) for n in range(100)
    ]
    load_search_index(gateway, messages)

    result = await _search(gateway, settings).search(make_scope(), [private_chat()], queries)

    assert result.outcome == "ok"
    assert len(result.matches) == MAX_MATCHES
    assert [m.ref for m in result.matches] == [f"earlier:{n}" for n in range(1, 61)]
    sent = [m.found.sent_at for m in result.matches]
    assert sent == sorted(sent, reverse=True)
    assert sent[0] == max(m.sent_at for m in messages)
    assert result.stats.matches == 5 * PER_REQUEST_LIMIT


async def test_matches_carry_chat_title_kind_and_reason(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    private = private_found("budget in private", days_ago=40)
    mine = group_found("budget from me", is_outgoing=True, days_ago=41)
    load_search_index(gateway, [private, mine])

    result = await _search(gateway, settings).search(
        make_scope(), [private_chat(), group_chat()], ["budget"]
    )

    assert [(m.ref, m.chat_title, m.chat_kind, m.link_reason) for m in result.matches] == [
        ("earlier:1", "Anna Testova", ChatKind.private, "private"),
        ("earlier:2", "Synthetic team", ChatKind.group, "own"),
    ]


# --- group linking ------------------------------------------------------------------------------


async def test_group_reply_targets_fetched_once_per_group(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    question = group_found("when do we ship?", sender_peer_id=SENDER_PEER_ID, days_ago=60)
    answer = group_found(
        "budget approved, ship friday",
        sender_peer_id=THIRD_PEER_ID,
        reply_to_id=question.message_id,
        days_ago=59,
    )
    unrelated = group_found("budget of another team", sender_peer_id=THIRD_PEER_ID, days_ago=58)
    private = private_found("budget private", reply_to_id=1)
    load_search_index(gateway, [question, answer, unrelated, private])

    result = await _search(gateway, settings).search(
        make_scope(), [private_chat(), group_chat()], ["budget"]
    )

    assert [(m.found.message_id, m.link_reason) for m in result.matches] == [
        (private.message_id, "private"),
        (answer.message_id, "reply"),
    ]
    assert result.stats.dropped_unlinked == 1
    assert [(chat, ids) for _, chat, ids in gateway.get_calls] == [
        (GROUP_CHAT_ID, (question.message_id,))
    ]


async def test_replied_by_the_sender_wins_over_a_third_party_reply(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    approved = group_found("budget approved", sender_peer_id=THIRD_PEER_ID, days_ago=60)
    by_sender = group_found(
        "budget noted", sender_peer_id=SENDER_PEER_ID, reply_to_id=approved.message_id
    )
    by_third = group_found(
        "budget cool", sender_peer_id=THIRD_PEER_ID, reply_to_id=approved.message_id
    )
    load_search_index(gateway, [approved, by_sender, by_third])

    result = await _search(gateway, settings).search(make_scope(), [group_chat()], ["budget"])

    reasons = {m.found.message_id: m.link_reason for m in result.matches}
    assert reasons[approved.message_id] == "replied_by"
    assert reasons[by_sender.message_id] == "sender"
    assert by_third.message_id not in reasons  # replies to a third party only
    assert result.stats.dropped_unlinked == 1


async def test_username_mentions_link_only_when_known(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    by_name = group_found(
        "@anna_test budget approved",
        sender_peer_id=THIRD_PEER_ID,
        mentioned_usernames={"anna_test"},
        days_ago=40,
    )
    by_id = group_found(
        "Anna, budget approved",
        sender_peer_id=THIRD_PEER_ID,
        mentioned_peer_ids={SENDER_PEER_ID},
        days_ago=41,
    )
    load_search_index(gateway, [by_name, by_id])
    search = _search(gateway, settings)

    unknown = await search.search(make_scope(), [group_chat()], ["budget"])
    assert [(m.found.message_id, m.link_reason) for m in unknown.matches] == [
        (by_id.message_id, "mention")
    ]

    gateway.usernames[SENDER_PEER_ID] = "Anna_Test"
    known = await search.search(make_scope(), [group_chat()], ["budget"])
    assert [(m.found.message_id, m.link_reason) for m in known.matches] == [
        (by_name.message_id, "mention"),
        (by_id.message_id, "mention"),
    ]


async def test_user_username_mention(gateway: FakeMessagingGateway, settings: Settings) -> None:
    message = group_found(
        "@me_user please confirm the budget",
        sender_peer_id=THIRD_PEER_ID,
        mentioned_usernames={"me_user"},
    )
    load_search_index(gateway, [message], usernames={USER_PEER_ID: "me_user"})

    result = await _search(gateway, settings).search(make_scope(), [group_chat()], ["budget"])

    assert [m.link_reason for m in result.matches] == ["mention"]


async def test_thread_link_uses_the_incoming_thread_top(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    message = group_found("budget in thread", sender_peer_id=THIRD_PEER_ID, thread_top_id=555)
    load_search_index(gateway, [message])
    search = _search(gateway, settings)

    assert (await search.search(make_scope(), [group_chat()], ["budget"])).outcome == "no_match"
    scope = make_scope(incoming_thread_top_id=555)
    result = await search.search(scope, [group_chat()], ["budget"])
    assert [m.link_reason for m in result.matches] == ["thread"]


# --- edits and deletions are whatever Telegram returns now --------------------------------------


async def test_edited_message_matched_by_new_text_and_removed_never_returned(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    edited = private_found("price is 100")
    removed = private_found("price is 200")
    load_search_index(gateway, [edited, removed])
    gateway.search_index[PRIVATE_CHAT_ID] = [dc_replace(edited, text="cost is 150")]
    search = _search(gateway, settings)

    old = await search.search(make_scope(), [private_chat()], ["price"])
    assert old.outcome == "no_match"
    new = await search.search(make_scope(), [private_chat()], ["cost"])
    assert [(m.found.message_id, m.found.text) for m in new.matches] == [
        (edited.message_id, "cost is 150")
    ]
    assert removed.message_id not in _ids(new.matches)


# --- failures -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (GatewayBusy(30), "rate_limited"),
        (GatewayNotStarted(), "not_started"),
        (SessionRevoked(), "not_started"),
        (RuntimeError("synthetic"), "failed"),
        (ValueError("synthetic"), "failed"),
    ],
)
async def test_errors_become_outcomes(
    gateway: FakeMessagingGateway,
    settings: Settings,
    error: Exception,
    outcome: str,
) -> None:
    load_search_index(gateway, [private_found("budget")])
    gateway.fail_search = error

    result = await _search(gateway, settings).search(
        make_scope(), [private_chat(), group_chat()], ["budget", "plan"]
    )

    assert result.outcome == outcome
    assert result.matches == []


async def test_get_messages_failure_becomes_outcome(settings: Settings) -> None:
    @dataclass
    class FailingLookup(FakeMessagingGateway):
        async def get_messages(
            self, user_id: uuid.UUID, chat_id: int, message_ids: object
        ) -> list[FoundMessage]:
            raise GatewayBusy(5)

    gateway = FailingLookup()
    load_search_index(gateway, [group_found("budget", sender_peer_id=THIRD_PEER_ID, reply_to_id=1)])

    result = await _search(gateway, settings).search(make_scope(), [group_chat()], ["budget"])

    assert result.outcome == "rate_limited"


async def test_flood_wait_cancels_the_remaining_requests(settings: Settings) -> None:
    @dataclass
    class FloodOnFirst(FakeMessagingGateway):
        finished: list[str] = field(default_factory=list)

        async def search_chat(
            self, user_id: uuid.UUID, chat_id: int, query: str, *, limit: int = 20
        ) -> list[FoundMessage]:
            self.search_calls.append((user_id, chat_id, query))
            if len(self.search_calls) == 1:
                raise GatewayBusy(120)
            await asyncio.sleep(5)
            self.finished.append(query)
            return []

    gateway = FloodOnFirst()
    chats = [private_chat(), group_chat(), group_chat(OTHER_GROUP_CHAT_ID)]
    started = time.monotonic()

    result = await _search(gateway, settings).search(make_scope(), chats, ["a", "b", "c", "d"])

    assert result.outcome == "rate_limited"
    assert time.monotonic() - started < 1
    # Only the requests already in flight (≤ 4, plus the one that took the freed slot) started;
    # the queued rest of the 12 never reached Telegram.
    assert len(gateway.search_calls) <= 5
    await asyncio.sleep(0)
    assert gateway.finished == []


async def test_deadline_exceeded_is_timeout(
    gateway: FakeMessagingGateway, settings: Settings
) -> None:
    load_search_index(gateway, [private_found("budget")])
    gateway.search_delay_s = 1.0
    fast = settings.model_copy(update={"earlier_search_timeout_s": 0.05})
    started = time.monotonic()

    result = await _search(gateway, fast).search(make_scope(), [private_chat()], ["budget"])

    assert result.outcome == "timeout"
    assert result.matches == []
    assert time.monotonic() - started < 0.5
    assert result.stats.duration_ms < 500


async def test_search_never_raises_on_a_broken_gateway(settings: Settings) -> None:
    @dataclass
    class Broken(FakeMessagingGateway):
        async def get_usernames(self, user_id: uuid.UUID, peer_ids: object) -> dict[int, str]:
            raise KeyError("synthetic")

    result = await _search(Broken(), settings).search(make_scope(), [private_chat()], ["budget"])

    assert result.outcome == "failed"


async def test_same_chat_twice_is_searched_once(settings: Settings) -> None:
    gateway = FakeMessagingGateway()
    chat = SearchChat(PRIVATE_CHAT_ID, "Anna Testova", ChatKind.private)
    await _search(gateway, settings).search(make_scope(), [chat, chat], ["budget"])
    assert len(gateway.search_calls) == 1


# --- forwarded hits (#47) -----------------------------------------------------------------------


def test_a_forwarded_hit_is_marked_forwarded_in_history() -> None:
    hit = dc_replace(
        private_found("the budget is approved"), is_forwarded=True, forwarded_from="Ivan"
    )
    match = EarlierMatch(
        ref="earlier:1",
        found=hit,
        chat_title="Anna Testova",
        chat_kind=ChatKind.private,
        link_reason="private",
    )
    item = match.history_item().model_dump(mode="json")
    assert (item["forwarded"], item["forwarded_from"]) == (True, "Ivan")
    plain = dc_replace(match, found=private_found("the budget is approved"))
    assert "forwarded" not in plain.history_item().model_dump(mode="json")
