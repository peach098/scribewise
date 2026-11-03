"""Style import with work account mode (specs/008 T045, FR-016): the import targets, the cap,
what is never read, materialising imported chats, and 🔄 Rebuild ranking them. Fake LLM, fake
gateway; synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.config import Settings
from scribe.db.models import ActivityKind, ActivityRecord, ChatKind, Contact, User, UserState
from scribe.db.repos import chats as chats_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.style.importer import ImportTarget, import_own_messages
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import Dialog, IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeUser
from tests.integration.bot_support import callback_update
from tests.integration.us1_support import grounded_reply, next_message_id
from tests.integration.us2_support import US2Harness
from tests.integration.us4_support import by_scope, make_harness, style_output

PRIVATE = [900000400 + i for i in range(5)]
EXCLUDED, PAUSED, PICKED = 900000501, 900000502, 900000503
TEAM, OTHER_GROUP = -100900000601, -100900000602


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    fake_llm.set_response(
        "profile.build", by_scope(style_output("New global."), style_output("New contact."))
    )
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


def _own(user: User, chat_id: int, n: int, clock: FrozenClock) -> list[IncomingMessage]:
    return [
        IncomingMessage(
            user_id=user.id, chat_id=chat_id, message_id=next_message_id(), sender_peer_id=0,
            sender_display_name="me", text=f"Synthetic own text {i}", has_media=False,
            sent_at=clock() - timedelta(days=3), is_outgoing=True,
        )
        for i in range(n)
    ]  # fmt: skip


async def _account(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> User:
    """Dialogs newest first: five private ones without a row, an excluded and a paused
    ``auto`` chat, a picked private chat, a picked group and an unpicked group."""
    user = await make_user(state=UserState.active)
    gateway.dialogs[user.id] = [
        *(Dialog(chat_id=c, title=f"Test Contact {c}", kind="private") for c in PRIVATE),
        Dialog(chat_id=EXCLUDED, title="Excluded", kind="private"),
        Dialog(chat_id=PAUSED, title="Paused", kind="private"),
        Dialog(chat_id=PICKED, title="Picked", kind="private"),
        Dialog(chat_id=TEAM, title="Team", kind="group"),
        Dialog(chat_id=OTHER_GROUP, title="Other group", kind="group"),
    ]
    await make_chat(user, telegram_chat_id=PICKED, title="Picked")
    await make_chat(user, telegram_chat_id=TEAM, title="Team", kind=ChatKind.group)
    async with session_factory() as session:
        for chat_id, excluded in ((EXCLUDED, True), (PAUSED, False)):
            await chats_repo.create_auto_chat(
                session, user.id, telegram_chat_id=chat_id, title="x", kind=ChatKind.private,
                enabled=False, excluded=excluded,
            )  # fmt: skip
        await session.commit()
    return user


async def _targets(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    user: User,
    max_auto: int,
) -> list[ImportTarget]:
    async with session_factory() as session:
        return await work_account.import_targets(session, gateway, user.id, max_auto=max_auto)


async def test_targets_with_the_mode_off_and_on(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _account(session_factory, gateway, make_user, make_chat, clock)
    off = await _targets(session_factory, gateway, user, 3)
    assert {t.telegram_chat_id for t in off} == {PICKED, TEAM}

    async with session_factory() as session:  # on, without re-enabling the paused chat
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.work_account = True
        await session.commit()
    on = await _targets(session_factory, gateway, user, 3)
    assert [t.telegram_chat_id for t in on if t.work_chat_id is None] == PRIVATE[:3]  # newest
    ids = {t.telegram_chat_id for t in on}
    assert ids == {PICKED, TEAM, *PRIVATE[:3]}  # never excluded, paused or unpicked groups
    assert len(await _targets(session_factory, gateway, user, 0)) == 2

    gateway.fail_list_dialogs = RuntimeError("synthetic: Telegram is down")
    assert {t.telegram_chat_id for t in await _targets(session_factory, gateway, user, 3)} == {
        PICKED,
        TEAM,
    }


async def test_materialise_only_dialogs_with_own_messages(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    settings: Settings,
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _account(session_factory, gateway, make_user, make_chat, clock)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    gateway.own_history[(user.id, PRIVATE[0])] = _own(user, PRIVATE[0], 4, clock)
    gateway.own_history[(user.id, EXCLUDED)] = _own(user, EXCLUDED, 4, clock)  # never read
    targets = await _targets(session_factory, gateway, user, 3)
    imported = await import_own_messages(
        gateway, session_factory, user.id, settings=settings, clock=clock, targets=targets
    )
    assert imported.per_chat_counts.get(PRIVATE[0]) == 4
    assert EXCLUDED not in imported.per_chat_counts
    async with session_factory() as session:
        created = await work_account.materialise_imported(
            session, user.id, imported, targets, now=clock()
        )
        await session.commit()
    assert created == 1
    async with session_factory() as session:
        chat = await chats_repo.get_work_chat_by_telegram_id(session, user.id, PRIVATE[0])
        assert chat is not None and chat.enabled and chat.origin.value == "auto"
        assert await chats_repo.get_work_chat_by_telegram_id(session, user.id, PRIVATE[1]) is None
        records = (
            await session.execute(
                select(ActivityRecord).where(
                    ActivityRecord.user_id == user.id,
                    ActivityRecord.kind == ActivityKind.chats_added,
                )
            )
        ).scalars()
        (record,) = list(records)
    assert record.meta_json["origin"] == "auto" and record.meta_json["via"] == "import"


async def test_rebuild_reads_recent_private_chats_and_ranks_them(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    settings: Settings,
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await _account(session_factory, gateway, make_user, make_chat, clock)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    gateway.own_history[(user.id, PRIVATE[1])] = _own(user, PRIVATE[1], 6, clock)
    await harness.feed(callback_update(user.telegram_control_chat_id, "profrebuild:1"))
    async with session_factory() as session:
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.user_id == user.id, Contact.telegram_peer_id == PRIVATE[1]
                )
            )
        ).scalar_one()
    assert contact.own_message_count_6m == 6
