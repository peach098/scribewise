"""``EarlierSearch.chats_for``: which enabled work chats a draft may search (research S4, FR-S01,
T015). Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from scribe.config import Settings
from scribe.context.earlier import EarlierSearch, SearchChat
from scribe.db.models import ChatKind
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeUser
from tests.integration.earlier_support import (
    DISABLED_CHAT_ID,
    EARLIER_NOW,
    GROUP_CHAT_ID,
    PRIVATE_CHAT_ID,
    SENDER_PEER_ID,
    group_found,
    load_search_index,
    make_scope,
    seed_earlier_user,
    store_message,
)


def _search(gateway: FakeMessagingGateway, settings: Settings) -> EarlierSearch:
    return EarlierSearch(gateway, settings, clock=FrozenClock(EARLIER_NOW))


async def test_private_turn_scope_is_private_chat_plus_groups_where_sender_wrote(
    db_session: AsyncSession,
    make_user: MakeUser,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    seed = await seed_earlier_user(db_session, await make_user())

    chats = await _search(gateway, settings).chats_for(
        db_session, seed.user.id, seed.private, seed.sender.id, SENDER_PEER_ID
    )

    assert chats == [
        SearchChat(PRIVATE_CHAT_ID, "Anna Testova", ChatKind.private),
        SearchChat(GROUP_CHAT_ID, "Synthetic team", ChatKind.group),
    ]
    assert DISABLED_CHAT_ID not in {c.telegram_chat_id for c in chats}


async def test_group_turn_adds_the_enabled_private_chat_once(
    db_session: AsyncSession,
    make_user: MakeUser,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    seed = await seed_earlier_user(db_session, await make_user())

    chats = await _search(gateway, settings).chats_for(
        db_session, seed.user.id, seed.group, seed.sender.id, SENDER_PEER_ID
    )

    # The turn's group comes first and is not repeated among the extra groups.
    assert [c.telegram_chat_id for c in chats] == [GROUP_CHAT_ID, PRIVATE_CHAT_ID]


async def test_disabled_private_chat_is_not_searched(
    db_session: AsyncSession,
    make_user: MakeUser,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    seed = await seed_earlier_user(db_session, await make_user())
    seed.private.enabled = False
    await db_session.flush()

    chats = await _search(gateway, settings).chats_for(
        db_session, seed.user.id, seed.group, seed.sender.id, SENDER_PEER_ID
    )

    assert [c.telegram_chat_id for c in chats] == [GROUP_CHAT_ID]


async def test_disabled_turn_chat_is_not_searched(
    db_session: AsyncSession,
    make_user: MakeUser,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    seed = await seed_earlier_user(db_session, await make_user())

    chats = await _search(gateway, settings).chats_for(
        db_session, seed.user.id, seed.disabled, seed.sender.id, SENDER_PEER_ID
    )

    assert DISABLED_CHAT_ID not in {c.telegram_chat_id for c in chats}
    assert [c.telegram_chat_id for c in chats] == [PRIVATE_CHAT_ID, GROUP_CHAT_ID]


async def test_at_most_two_extra_groups_most_recent_first_within_retention(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    seed = await seed_earlier_user(db_session, await make_user())
    user, sender = seed.user, seed.sender
    newest = await make_chat(user, kind=ChatKind.group, title="Newest group")
    older = await make_chat(user, kind=ChatKind.group, title="Older group")
    stale = await make_chat(user, kind=ChatKind.group, title="Stale group")
    silent = await make_chat(user, kind=ChatKind.group, title="Sender never wrote")
    retention = settings.message_retention_days
    await store_message(db_session, user, newest, sender, sent_at=EARLIER_NOW - timedelta(hours=1))
    await store_message(db_session, user, older, sender, sent_at=EARLIER_NOW - timedelta(days=5))
    await store_message(
        db_session, user, stale, sender, sent_at=EARLIER_NOW - timedelta(days=retention + 1)
    )
    await store_message(db_session, user, silent, None, sent_at=EARLIER_NOW)

    chats = await _search(gateway, settings).chats_for(
        db_session, user.id, seed.private, sender.id, SENDER_PEER_ID
    )

    # seed.group: sender wrote 2 days ago, between "newest" and "older".
    assert [c.title for c in chats] == ["Anna Testova", "Newest group", "Synthetic team"]
    assert len(chats) <= 4


async def test_other_users_chats_are_never_in_scope(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_chat: MakeChat,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    seed = await seed_earlier_user(db_session, await make_user())
    other = await make_user(display_name="Other User")
    other_seed = await seed_earlier_user(db_session, other)
    foreign = await make_chat(other, kind=ChatKind.group, title="Foreign group")
    await store_message(db_session, other, foreign, other_seed.sender, sent_at=EARLIER_NOW)

    chats = await _search(gateway, settings).chats_for(
        db_session, seed.user.id, seed.private, seed.sender.id, SENDER_PEER_ID
    )

    assert "Foreign group" not in {c.title for c in chats}


async def test_unknown_sender_searches_only_the_turn_chat(
    db_session: AsyncSession,
    make_user: MakeUser,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    seed = await seed_earlier_user(db_session, await make_user())

    chats = await _search(gateway, settings).chats_for(
        db_session, seed.user.id, seed.group, None, None
    )

    assert [c.telegram_chat_id for c in chats] == [GROUP_CHAT_ID]


async def test_scope_then_search_never_touches_the_disabled_chat(
    db_session: AsyncSession,
    make_user: MakeUser,
    gateway: FakeMessagingGateway,
    settings: Settings,
) -> None:
    seed = await seed_earlier_user(db_session, await make_user())
    load_search_index(
        gateway,
        [
            group_found("budget approved", chat_id=DISABLED_CHAT_ID),
            group_found("budget approved", chat_id=GROUP_CHAT_ID),
        ],
    )
    search = _search(gateway, settings)

    chats = await search.chats_for(
        db_session, seed.user.id, seed.private, seed.sender.id, SENDER_PEER_ID
    )
    result = await search.search(make_scope(user_id=seed.user.id), chats, ["budget", "approved"])

    assert result.outcome == "ok"
    assert all(chat != DISABLED_CHAT_ID for _, chat, _ in gateway.search_calls)
    assert {m.found.chat_id for m in result.matches} == {GROUP_CHAT_ID}
