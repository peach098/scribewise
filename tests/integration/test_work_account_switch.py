"""Work account mode switch, exclusion and repair (specs/008 T025, T031): the ``auto`` invariant
``enabled = users.work_account AND NOT excluded`` after every writer, what turning off drops,
``picked`` chats left alone, and the composer refusing a paused chat. Synthetic data only."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    ConversationTurn,
    DraftState,
    DroppedReason,
    TurnState,
    User,
    UserState,
    WorkChat,
    WorkChatOrigin,
)
from scribe.db.repos import chats as chats_repo
from scribe.drafting.composer import ComposeError
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import Dialog, IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeUser
from tests.integration.us1_support import grounded_reply, next_message_id
from tests.integration.us2_support import US2Harness
from tests.integration.us4_support import make_harness

A, B, C = 900000101, 900000102, 900000103
PICKED = 900000104


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
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _message(harness: US2Harness, user: User, chat_id: int, **values: Any) -> None:
    await harness.intake.route(
        IncomingMessage(
            user_id=user.id,
            chat_id=chat_id,
            message_id=next_message_id(),
            sender_peer_id=chat_id,
            sender_display_name=values.pop("name", "Test Contact A"),
            text="Synthetic question?",
            has_media=False,
            sent_at=harness.clock(),
            peer_kind="user",
        )
    )


async def _chats(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> dict[int, WorkChat]:
    async with session_factory() as session:
        return {c.telegram_chat_id: c for c in await chats_repo.list_work_chats(session, user.id)}


async def _kinds(session_factory: async_sessionmaker[AsyncSession], user: User) -> list[str]:
    async with session_factory() as session:
        rows = await session.execute(
            select(ActivityRecord.kind)
            .where(ActivityRecord.user_id == user.id)
            .order_by(ActivityRecord.at, ActivityRecord.created_at)
        )
        return [kind.value for kind in rows.scalars().all()]


def _assert_invariant(chats: dict[int, WorkChat], mode: bool) -> None:
    for chat in chats.values():
        if chat.origin == WorkChatOrigin.auto:
            assert chat.enabled == (mode and not chat.excluded), chat.telegram_chat_id
        else:
            assert chat.excluded is False


async def _auto(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    chat_id: int,
    *,
    enabled: bool,
    excluded: bool = False,
) -> WorkChat:
    async with session_factory() as session:
        chat, _ = await chats_repo.create_auto_chat(
            session,
            user.id,
            telegram_chat_id=chat_id,
            title=f"Test Contact {chat_id}",
            kind=ChatKind.private,
            enabled=enabled,
            excluded=excluded,
        )
        await session.commit()
    return chat


# --- US3: the switch ------------------------------------------------------------------------------


async def test_turning_on_enables_only_non_excluded_auto_chats(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    await _auto(session_factory, user, A, enabled=False)
    await _auto(session_factory, user, B, enabled=False, excluded=True)
    await make_chat(user, telegram_chat_id=PICKED, enabled=False)

    result = await work_account.switch(session_factory, user.id, on=True, now=clock())
    assert result.changed and result.enabled_chats == 1
    chats = await _chats(session_factory, user)
    assert chats[A].enabled and not chats[B].enabled and not chats[PICKED].enabled
    _assert_invariant(chats, True)
    async with session_factory() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        await session.refresh(stored)
        assert stored.work_account is True and stored.work_account_changed_at == clock()

    again = await work_account.switch(session_factory, user.id, on=True, now=clock())
    assert again.changed is False
    assert (await _kinds(session_factory, user)).count("work_account_on") == 1


async def test_turning_off_pauses_auto_chats_and_drops_their_work(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    picked = await make_chat(user, telegram_chat_id=PICKED, title="Test Contact P")
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    await _message(harness, user, A)
    await harness.tick()  # A: a pending draft
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending
    clock.advance(minutes=1)
    user.delay_min_s = user.delay_max_s = 3600  # B and the picked chat: waiting turns
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.id == user.id).values(delay_min_s=3600, delay_max_s=3600)
        )
        await session.commit()
    await _message(harness, user, B, name="Test Contact B")
    await _message(harness, user, PICKED, name="Test Contact P")

    async with session_factory() as session:
        preview = await work_account.preview_off(session, user.id)
    assert (preview.chats, preview.pending_drafts, preview.waiting) == (2, 1, 1)

    result = await work_account.switch(session_factory, user.id, on=False, now=clock())
    assert result.changed
    assert (result.paused_chats, result.dropped_draft_ids, result.dropped_waiting) == (
        2,
        [draft.id],
        1,
    )
    chats = await _chats(session_factory, user)
    _assert_invariant(chats, False)
    assert chats[PICKED].enabled  # picked chats are never touched
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.dropped
    turns = {t.work_chat_id: t for t in await harness.turns(user)}
    assert turns[chats[A].id].dropped_reason == DroppedReason.work_account_off
    assert turns[chats[B].id].state == TurnState.dropped
    assert turns[chats[B].id].dropped_reason == DroppedReason.work_account_off
    assert turns[picked.id].state == TurnState.waiting
    assert (await _kinds(session_factory, user)).count("work_account_off") == 1


async def test_on_off_on_keeps_exclusions(
    session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser, clock: FrozenClock
) -> None:
    user = await make_user(state=UserState.active)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    a = await _auto(session_factory, user, A, enabled=True)
    await _auto(session_factory, user, B, enabled=True)
    await work_account.exclude_chat(session_factory, user.id, work_chat_id=a.id, now=clock())
    before = {c: (r.enabled, r.excluded) for c, r in (await _chats(session_factory, user)).items()}
    await work_account.switch(session_factory, user.id, on=False, now=clock())
    _assert_invariant(await _chats(session_factory, user), False)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    after = {c: (r.enabled, r.excluded) for c, r in (await _chats(session_factory, user)).items()}
    assert after == before == {A: (False, True), B: (True, False)}


async def test_a_pause_never_lets_the_composer_store_a_draft(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    await _message(harness, user, A)
    (turn,) = await harness.turns(user)
    async with session_factory() as session:  # the scheduler claimed it before the switch
        await session.execute(
            update(ConversationTurn)
            .where(ConversationTurn.id == turn.id)
            .values(state=TurnState.reading)
        )
        await session.commit()
    await work_account.switch(session_factory, user.id, on=False, now=clock())
    with pytest.raises(ComposeError):
        await harness.composer.compose(user.id, turn.id)
    assert await harness.drafts(user) == []


async def test_reconcile_finishes_a_half_done_switch(
    session_factory: async_sessionmaker[AsyncSession], make_user: MakeUser, clock: FrozenClock
) -> None:
    user = await make_user(state=UserState.active)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    await _auto(session_factory, user, A, enabled=True)
    await _auto(session_factory, user, B, enabled=True)
    async with session_factory() as session:  # the flag committed, the sweep cut short
        await session.execute(update(User).where(User.id == user.id).values(work_account=False))
        await session.commit()
    assert await work_account.reconcile_user(session_factory, user.id, now=clock()) == 2
    _assert_invariant(await _chats(session_factory, user), False)
    assert await work_account.reconcile_user(session_factory, user.id, now=clock()) == 0

    async with session_factory() as session:  # on again, but the enable did not run
        await session.execute(update(User).where(User.id == user.id).values(work_account=True))
        await session.commit()
    assert await work_account.reconcile_recent(session_factory, now=clock()) == 2
    _assert_invariant(await _chats(session_factory, user), True)


# --- US2: exclude / include -----------------------------------------------------------------------


async def test_exclude_and_include_an_auto_chat(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    await _message(harness, user, A)
    await harness.tick()
    (draft,) = await harness.drafts(user)
    chat = (await _chats(session_factory, user))[A]

    dropped = await work_account.exclude_chat(
        session_factory, user.id, work_chat_id=chat.id, now=clock()
    )
    assert dropped == [draft.id]
    chat = (await _chats(session_factory, user))[A]
    assert (chat.excluded, chat.enabled) == (True, False)
    (turn,) = await harness.turns(user)
    assert turn.dropped_reason == DroppedReason.chat_disabled
    assert (
        await work_account.exclude_chat(session_factory, user.id, work_chat_id=chat.id, now=clock())
        == []
    )  # already excluded: nothing more

    assert await work_account.include_chat(session_factory, user.id, chat.id, now=clock())
    chat = (await _chats(session_factory, user))[A]
    assert (chat.excluded, chat.enabled) == (False, True)
    kinds = await _kinds(session_factory, user)
    assert kinds.count("chat_excluded") == 1 and kinds.count("chat_included") == 1

    # Included again while the mode is off: paused, not enabled.
    await work_account.exclude_chat(session_factory, user.id, work_chat_id=chat.id, now=clock())
    await work_account.switch(session_factory, user.id, on=False, now=clock())
    await work_account.include_chat(session_factory, user.id, chat.id, now=clock())
    chat = (await _chats(session_factory, user))[A]
    assert (chat.excluded, chat.enabled) == (False, False)


async def test_exclude_a_chat_that_never_wrote_and_a_picked_chat(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    await work_account.switch(session_factory, user.id, on=True, now=clock())
    picked = await make_chat(user, telegram_chat_id=PICKED)
    dialog = Dialog(chat_id=C, title="Test Contact C", kind="private")
    excluded = await work_account.exclude_chat(session_factory, user.id, dialog=dialog, now=clock())
    assert excluded == []
    assert (
        await work_account.exclude_chat(
            session_factory, user.id, work_chat_id=picked.id, now=clock()
        )
        == []
    )
    chats = await _chats(session_factory, user)
    assert chats[C].origin == WorkChatOrigin.auto and chats[C].excluded and not chats[C].enabled
    assert chats[PICKED].origin == WorkChatOrigin.picked
    assert (chats[PICKED].enabled, chats[PICKED].excluded) == (False, False)
    async with session_factory() as session:
        assert await chats_repo.count_excluded(session, user.id) == 2
    await work_account.include_chat(session_factory, user.id, picked.id, now=clock())
    assert (await _chats(session_factory, user))[PICKED].enabled
    async with session_factory() as session:
        records = (
            await session.execute(
                select(ActivityRecord).where(
                    ActivityRecord.user_id == user.id,
                    ActivityRecord.kind == ActivityKind.chat_excluded,
                )
            )
        ).scalars()
        ids = sorted(r.meta_json["work_chat_id"] for r in records)
    assert ids == sorted([str(chats[C].id), str(picked.id)])
