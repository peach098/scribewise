"""``chats_repo.add_work_chats`` (specs/007 T005, research R4): insert-only, never touches an
existing row, returns only the rows it created. Synthetic data only."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import ChatKind
from scribe.db.repos import chats as chats_repo
from scribe.db.repos.chats import NewWorkChat
from tests.conftest import MakeChat, MakeUser


async def test_inserts_enabled_rows_with_defaults_and_returns_them(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    created = await chats_repo.add_work_chats(
        db_session,
        user.id,
        [
            NewWorkChat(-100900000001, "Project X (synthetic)", ChatKind.group),
            NewWorkChat(900000002, "Anna (synthetic)", ChatKind.private),
        ],
    )
    assert sorted(c.telegram_chat_id for c in created) == [-100900000001, 900000002]
    for chat in created:
        assert chat.enabled is True
        assert chat.user_id == user.id
        assert chat.override_language is None and chat.override_formality is None
        assert chat.override_delay_min_s is None and chat.skip_closing is None
    stored = await chats_repo.list_work_chats(db_session, user.id)
    assert {c.title for c in stored} == {"Project X (synthetic)", "Anna (synthetic)"}


async def test_second_call_adds_nothing(db_session: AsyncSession, make_user: MakeUser) -> None:
    user = await make_user()
    chats = [NewWorkChat(-100900000003, "Design (synthetic)", ChatKind.group)]
    assert len(await chats_repo.add_work_chats(db_session, user.id, chats)) == 1
    assert await chats_repo.add_work_chats(db_session, user.id, chats) == []
    assert len(await chats_repo.list_work_chats(db_session, user.id)) == 1


async def test_existing_disabled_row_is_left_as_it_is(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    user = await make_user()
    old = await make_chat(user, telegram_chat_id=-100900000004, title="Old", enabled=False)
    created = await chats_repo.add_work_chats(
        db_session,
        user.id,
        [
            NewWorkChat(-100900000004, "Old renamed (synthetic)", ChatKind.group),
            NewWorkChat(-100900000005, "New (synthetic)", ChatKind.group),
        ],
    )
    assert [c.telegram_chat_id for c in created] == [-100900000005]
    user_id, old_id = user.id, old.id
    db_session.expire_all()
    again = await chats_repo.get_work_chat(db_session, user_id, old_id)
    assert again is not None and again.enabled is False and again.title == "Old"


async def test_other_users_rows_are_never_touched(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    alice, bob = await make_user(), await make_user()
    bobs = await make_chat(bob, telegram_chat_id=-100900000006, title="Bob's", enabled=False)
    created = await chats_repo.add_work_chats(
        db_session, alice.id, [NewWorkChat(-100900000006, "Shared (synthetic)", ChatKind.group)]
    )
    assert len(created) == 1 and created[0].user_id == alice.id
    bob_id, bobs_id = bob.id, bobs.id
    db_session.expire_all()
    again = await chats_repo.get_work_chat(db_session, bob_id, bobs_id)
    assert again is not None and again.enabled is False and again.title == "Bob's"


async def test_empty_input(db_session: AsyncSession, make_user: MakeUser) -> None:
    user = await make_user()
    assert await chats_repo.add_work_chats(db_session, user.id, []) == []
