"""A retry after an ambiguous send failure never delivers the reply twice (#54).

Synthetic data only. The fake gateway deduplicates by ``random_id`` as Telegram does.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import Draft, DraftState, User, UserState
from scribe.db.repos import drafts as drafts_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.sending.send import (
    DraftOutdated,
    SendAttemptUnresolved,
    SendRefused,
    send_approved_draft,
)
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import US1Harness, grounded_reply, select_all_history

REPLY = "Synthetic reply: the report is due on Friday."


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US1Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    return US1Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _approved(
    harness: US1Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> tuple[User, Draft]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert draft.send_random_id is None  # a key is given on the first send attempt only
    async with session_factory() as session:
        await drafts_repo.update_draft(session, user.id, draft.id, approved_by_user_at=clock())
        await session.commit()
    return user, draft


async def _stored(
    session_factory: async_sessionmaker[AsyncSession], user: User, draft: Draft
) -> Draft:
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
        assert stored is not None
        return stored


async def test_retry_after_delivered_then_timed_out_send_reuses_the_random_id(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, draft = await _approved(
        harness, session_factory, clock, make_user, make_chat, make_contact
    )
    # Telegram delivers the message, then the RPC times out.
    gateway.fail_send_after_delivery_with = TimeoutError()
    with pytest.raises(TimeoutError):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
        )
    (delivered,) = gateway.send_text_calls
    stored = await _stored(session_factory, user, draft)
    assert stored.state == DraftState.pending and stored.sent_telegram_message_id is None
    assert stored.send_random_id == delivered.random_id  # committed despite the failure

    # The user taps Send again: same random_id, Telegram deduplicates, nothing new is delivered.
    result = await send_approved_draft(
        session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
    )
    assert gateway.send_attempt_random_ids == [delivered.random_id, delivered.random_id]
    assert gateway.send_text_calls == [delivered]
    assert result.telegram_message_id == delivered.message_id
    stored = await _stored(session_factory, user, draft)
    assert stored.state == DraftState.sent
    assert stored.sent_telegram_message_id == delivered.message_id


async def test_retry_after_a_failure_before_delivery_sends_once(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, draft = await _approved(
        harness, session_factory, clock, make_user, make_chat, make_contact
    )
    gateway.fail_send_with = ConnectionError()
    with pytest.raises(ConnectionError):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
        )
    gateway.fail_send_with = None
    await send_approved_draft(
        session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
    )
    (sent,) = gateway.send_text_calls
    first, second = gateway.send_attempt_random_ids
    assert first == second == sent.random_id != 0


async def test_unapproved_draft_is_refused_and_nothing_is_sent(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    with pytest.raises(SendRefused):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
        )
    assert gateway.send_attempt_random_ids == []


async def test_a_refused_send_gives_no_key(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """A key means "an attempt reached Telegram": a send refused before that leaves none, so the
    draft can still be edited or regenerated."""
    user, draft = await _approved(
        harness, session_factory, clock, make_user, make_chat, make_contact
    )
    async with session_factory() as session:
        await drafts_repo.update_draft(session, user.id, draft.id, outdated=True)
        await session.commit()
    with pytest.raises(DraftOutdated):
        await send_approved_draft(
            session_factory, gateway, user_id=user.id, draft_id=draft.id, clock=clock
        )
    assert gateway.send_attempt_random_ids == []
    assert (await _stored(session_factory, user, draft)).send_random_id is None


async def test_an_edited_attempt_can_only_be_retried_with_the_same_text(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, draft = await _approved(
        harness, session_factory, clock, make_user, make_chat, make_contact
    )
    gateway.fail_send_after_delivery_with = TimeoutError()
    with pytest.raises(TimeoutError):
        await send_approved_draft(
            session_factory,
            gateway,
            user_id=user.id,
            draft_id=draft.id,
            edited_text="Synthetic edit: Friday morning.",
            clock=clock,
        )
    (delivered,) = gateway.send_text_calls
    stored = await _stored(session_factory, user, draft)
    assert stored.edited_text == "Synthetic edit: Friday morning."  # the attempted text is kept

    # Another text (or the original draft) would be dropped by Telegram as a duplicate while
    # the database recorded it as sent: refused, nothing is attempted.
    for other in ("Synthetic edit: Monday.", None):
        with pytest.raises(SendAttemptUnresolved):
            await send_approved_draft(
                session_factory,
                gateway,
                user_id=user.id,
                draft_id=draft.id,
                edited_text=other,
                clock=clock,
            )
    assert len(gateway.send_attempt_random_ids) == 1

    result = await send_approved_draft(
        session_factory,
        gateway,
        user_id=user.id,
        draft_id=draft.id,
        edited_text="Synthetic edit: Friday morning.",
        clock=clock,
    )
    assert gateway.send_text_calls == [delivered]
    assert result.telegram_message_id == delivered.message_id
    stored = await _stored(session_factory, user, draft)
    assert stored.state == DraftState.sent and stored.edited_text == delivered.text


async def test_after_an_unresolved_attempt_the_card_cannot_be_changed(
    harness: US1Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Edit and Regenerate are refused with an explanation (a regenerated version would get a new
    key and reach the contact a second time); Send finishes the same text exactly once."""
    user, draft = await _approved(
        harness, session_factory, clock, make_user, make_chat, make_contact
    )
    control = user.telegram_control_chat_id
    gateway.fail_send_after_delivery_with = TimeoutError()
    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    await harness.feed(text_update(control, "Synthetic edit: Friday morning."))
    (delivered,) = gateway.send_text_calls
    assert delivered.text == "Synthetic edit: Friday morning."
    harness.api.clear()

    await harness.feed(callback_update(control, f"regen:{draft.id}"))
    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    await harness.feed(text_update(control, "Synthetic edit: Monday."))
    notices = [t for t in harness.api.sent_texts if "may already have reached the chat" in t]
    assert len(notices) == 2
    assert [d.id for d in await harness.drafts(user)] == [draft.id]  # no new version
    assert len(gateway.send_attempt_random_ids) == 1

    await harness.feed(callback_update(control, f"approve:{draft.id}"))
    assert gateway.send_text_calls == [delivered]
    stored = await _stored(session_factory, user, draft)
    assert stored.state == DraftState.sent
    assert stored.edited_text == delivered.text
    assert stored.sent_telegram_message_id == delivered.message_id
