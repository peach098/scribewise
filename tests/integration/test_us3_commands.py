"""US3 control-chat commands: /tasks (T056: connect, validate, pick boards, disconnect, status)
and /commitments (T059: list, resolve, edit, delete), the commitment retention rule and
background extraction with real commits. Synthetic data only; Trello is served by respx."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import respx
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendDocument,
    SendMessage,
)
from aiogram.types import BufferedInputFile
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context.tasks.trello import TRELLO_API
from scribe.db.models import (
    ChatKind,
    Commitment,
    CommitmentState,
    ConnectionStatus,
    ContactKind,
    User,
    UserState,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.db.repos import users as users_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import CommitmentExtraction, CommitmentItem
from scribe.llm.registry import LLMRouter
from scribe.retention.purge import purge_messages
from scribe.telegram.control_bot.setup_flow import SetupStates
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, LogCapture, MakeContact, MakeUser, next_telegram_id
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import select_all_history
from tests.integration.us2_support import US2Harness, button_texts, callback_datas
from tests.integration.us3_support import commitments_of, connect_trello, inline_extractor

KEY = "0123456789abcdef0123456789abcdef"
TOKEN = "ATTAsynthetictoken0123456789abcdef0123456789abcdef0123456789abcdef"
BOARDS = [
    {"id": f"b{n:023d}", "name": f"Synthetic board {n}", "closed": False} for n in range(1, 11)
]


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    harness.composer.commitments = inline_extractor(session_factory, llm_router, clock)
    return harness


def _last(harness: US2Harness) -> SendMessage:
    return harness.messages()[-1]


# --- /tasks -------------------------------------------------------------------------------------


async def test_tasks_connect_pick_boards_and_disconnect(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id

    await harness.feed(text_update(chat_id, "/tasks"))
    assert "Not connected" in _last(harness).text
    assert callback_datas(_last(harness).reply_markup) == ["tasks:connect", "jira:connect"]

    await harness.feed(callback_update(chat_id, "tasks:connect"))
    assert "API key" in _last(harness).text
    await harness.feed(text_update(chat_id, "short"))  # implausible: asked again
    assert "doesn't look like" in _last(harness).text
    await harness.feed(text_update(chat_id, KEY))
    assert "scope=read" in _last(harness).text and f"key={KEY}" in _last(harness).text

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{TRELLO_API}/1/members/me").respond(
            200, json={"id": "m1", "username": "synthetic", "fullName": "Synthetic User"}
        )
        boards_route = router.get(f"{TRELLO_API}/1/members/me/boards").respond(200, json=BOARDS)
        await harness.feed(text_update(chat_id, TOKEN))
        assert all(call.request.method == "GET" for call in router.calls)

        picker = _last(harness)
        assert "Pick the boards" in picker.text
        datas = callback_datas(picker.reply_markup)
        assert datas[:8] == [f"tboard:{b['id']}" for b in BOARDS[:8]]  # paged: 8 per page
        assert "tpage:1" in datas and datas[-1] == "tboard:done"

        first, ninth = BOARDS[0]["id"], BOARDS[8]["id"]
        await harness.feed(callback_update(chat_id, f"tboard:{first}"))
        await harness.feed(callback_update(chat_id, "tpage:1"))
        await harness.feed(callback_update(chat_id, f"tboard:{ninth}"))
        await harness.feed(callback_update(chat_id, "tboard:not-my-board"))
        assert boards_route.call_count >= 4
    edits = harness.api.of_type(EditMessageReplyMarkup)
    assert any("✅ Synthetic board 1" in t for t in button_texts(edits[0].reply_markup))

    # Both credential messages and the prompt that echoed the key were deleted (best effort).
    assert len(harness.api.of_type(DeleteMessage)) == 4  # "short", the key, the token, prompt

    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.status == ConnectionStatus.active
        assert conn.credentials == {"key": KEY, "token": TOKEN}
        assert conn.config_json["board_ids"] == [first, ninth]  # the foreign id was refused
        raw = (
            await session.execute(
                text("SELECT credentials::text FROM task_source_connections WHERE user_id = :u"),
                {"u": user.id},
            )
        ).scalar_one()
    assert TOKEN not in raw and KEY not in raw  # encrypted at rest

    await harness.feed(callback_update(chat_id, "tboard:done"))
    assert "✅ Trello: connected (read-only)" in _last(harness).text
    assert "📋 Boards: 2 selected" in _last(harness).text

    await harness.feed(callback_update(chat_id, "tasks:disconnect"))
    assert "disconnected" in _last(harness).text
    async with session_factory() as session:
        assert await tasks_conn_repo.get_connection(session, user.id) is None

    assert TOKEN not in log_capture.raw and KEY not in log_capture.raw


async def test_tasks_rejected_credentials_store_nothing(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await harness.feed(callback_update(chat_id, "tasks:connect"))
    await harness.feed(text_update(chat_id, KEY))
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{TRELLO_API}/1/members/me").respond(401, text="invalid token")
        await harness.feed(text_update(chat_id, TOKEN))
    assert "did not accept" in _last(harness).text
    assert TOKEN not in _last(harness).text
    async with session_factory() as session:
        assert await tasks_conn_repo.get_connection(session, user.id) is None
    # The flow ended: the next text is not taken as a credential.
    await harness.feed(text_update(chat_id, TOKEN))
    assert "did not accept" in harness.bot_texts()[-1]
    assert len(harness.api.of_type(DeleteMessage)) == 3  # the key, the token, the prompt


async def test_tasks_button_outside_the_token_step_drops_the_key(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    """Issue #56: an older 📦 Export tapped at the token step ends the step first (the key
    leaves the FSM data, the prompt that echoed it is deleted), so the file never holds it."""
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await harness.feed(callback_update(chat_id, "tasks:connect"))
    await harness.feed(text_update(chat_id, KEY))
    assert KEY in _last(harness).text  # the authorize link in the token prompt

    await harness.feed(callback_update(chat_id, "mydata:export"))

    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT count(*) FROM bot_fsm_state WHERE chat_id = :c"), {"c": chat_id}
        )
        assert rows.scalar_one() == 0
    assert len(harness.api.of_type(DeleteMessage)) == 2  # the key, the prompt
    assert any(
        "Trello connection cancelled" in (m.text or "") for m in harness.api.of_type(SendMessage)
    )
    (document,) = harness.api.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    assert KEY not in document.document.data.decode()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{TRELLO_API}/1/members/me").respond(200, json={"id": "m1"})
        await harness.feed(text_update(chat_id, TOKEN))  # no longer taken as the token
    assert not route.called


async def test_tasks_status_shows_error_after_repeated_failures(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    await connect_trello(session_factory, user)
    async with session_factory() as session:
        await tasks_conn_repo.set_status(session, user.id, ConnectionStatus.error)
        await session.commit()
    await harness.feed(text_update(user.telegram_control_chat_id, "/tasks"))
    status = _last(harness)
    assert "⚠️ Trello: error" in status.text and "Reconnect" in status.text
    assert callback_datas(status.reply_markup) == [
        "tasks:boards",
        "tasks:connect",
        "tasks:disconnect",
    ]


async def test_tasks_and_commitments_yield_to_the_setup_flow(
    harness: US2Harness, make_user: MakeUser
) -> None:
    user = await make_user(state=UserState.setting_up)
    chat_id = user.telegram_control_chat_id
    key = StorageKey(bot_id=harness.bot.id, chat_id=chat_id, user_id=chat_id)
    await harness.dispatcher.storage.set_state(key, SetupStates.pick_chats)
    await harness.feed(text_update(chat_id, "/tasks"))
    await harness.feed(text_update(chat_id, "/commitments"))
    assert not any("Task source" in t or "commitments" in t for t in harness.bot_texts())


# --- /commitments -------------------------------------------------------------------------------


async def _add(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    contact_id: uuid.UUID,
    what: str,
    due: date | None,
    clock: FrozenClock,
) -> Commitment:
    async with session_factory() as session:
        stored = await commitments_repo.add_commitment(
            session,
            user.id,
            contact_id=contact_id,
            what=what,
            due=due,
            source_sent_at=clock(),
            confidence=0.9,
            source_message_text=f"Synthetic promise: {what}",
        )
        await session.commit()
        return stored


async def test_commitments_list_resolve_edit_delete(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    other = await make_user(state=UserState.active)
    anna = await make_contact(user, display_name="Anna Testova")
    foreign_contact = await make_contact(other, display_name="Other Contact")
    deck = await _add(session_factory, user, anna.id, "send the deck", date(2026, 10, 15), clock)
    call = await _add(session_factory, user, anna.id, "call back", None, clock)
    report = await _add(
        session_factory, user, anna.id, "send the report", date(2026, 10, 16), clock
    )
    foreign = await _add(session_factory, other, foreign_contact.id, "foreign", None, clock)
    chat_id = user.telegram_control_chat_id

    await harness.feed(text_update(chat_id, "/commitments"))
    texts = harness.bot_texts()
    assert texts[0] == "🤝 Open commitments (3):"
    assert texts[1].startswith("🤝 send the deck · Anna Testova · due Thu 15 Oct")
    assert "› “Synthetic promise: send the deck”" in texts[1]
    assert texts[2].startswith("🤝 send the report · Anna Testova · due Fri 16 Oct")
    assert texts[3].startswith("🤝 call back · Anna Testova · no date")
    assert callback_datas(harness.messages()[1].reply_markup) == [
        f"cmtres:{deck.id}",
        f"cmtedit:{deck.id}",
        f"cmtdel:{deck.id}",
    ]
    assert not any("foreign" in t for t in texts)

    # Another user's commitment cannot be touched.
    await harness.feed(callback_update(chat_id, f"cmtres:{foreign.id}"))
    await harness.feed(callback_update(chat_id, f"cmtdel:{foreign.id}"))

    await harness.feed(callback_update(chat_id, f"cmtres:{deck.id}"))
    assert "✅ Resolved: send the deck" in (harness.api.of_type(EditMessageText)[-1].text or "")
    await harness.feed(callback_update(chat_id, f"cmtres:{deck.id}"))  # already closed: no-op

    await harness.feed(callback_update(chat_id, f"cmtedit:{call.id}"))
    assert "Send the new text" in harness.bot_texts()[-1]
    await harness.feed(text_update(chat_id, "/help"))  # a command is never the new text
    await harness.feed(text_update(chat_id, "call Anna back; 2026-10-20"))
    assert "🤝 call Anna back · Anna Testova · due Tue 20 Oct" in harness.bot_texts()[-1]
    await harness.feed(callback_update(chat_id, f"cmtedit:{report.id}"))
    await harness.feed(text_update(chat_id, "пятница"))  # a date alone moves the due date only
    await harness.feed(callback_update(chat_id, f"cmtedit:{report.id}"))
    await harness.feed(text_update(chat_id, "no date"))

    await harness.feed(callback_update(chat_id, f"cmtdel:{call.id}"))
    assert "🗑 Deleted: call Anna back" in (harness.api.of_type(EditMessageText)[-1].text or "")

    by_id = {c.id: c for c in await commitments_of(session_factory, user)}
    assert by_id[deck.id].state == CommitmentState.resolved
    assert by_id[deck.id].closed_at == clock()
    assert by_id[call.id].state == CommitmentState.deleted
    assert by_id[call.id].closed_at == clock() and by_id[call.id].what == "call Anna back"
    assert by_id[call.id].due == date(2026, 10, 20)
    assert by_id[report.id].state == CommitmentState.open and by_id[report.id].due is None
    assert by_id[report.id].what == "send the report"
    (other_row,) = await commitments_of(session_factory, other)
    assert other_row.state == CommitmentState.open

    # Retention: closed commitments go 30 days after closed_at; open ones stay.
    settings = harness.settings
    await purge_messages(session_factory, settings, clock() + timedelta(days=29))
    assert len(await commitments_of(session_factory, user)) == 3
    await purge_messages(session_factory, settings, clock() + timedelta(days=31))
    remaining = await commitments_of(session_factory, user)
    assert [c.id for c in remaining] == [report.id]


# --- background extraction with real commits ----------------------------------------------------


async def test_extraction_runs_in_the_background(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> None:
    """The manual reply is handled (and its turn dropped) before the slow extraction ends."""
    factory = committed_session_factory
    async with factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings, display_name="U"
        )
        user.state = UserState.active
        peer = next_telegram_id()
        await chats_repo.upsert_contact(
            session, user.id, telegram_peer_id=peer, kind=ContactKind.person, display_name="Anna"
        )
        chat = await chats_repo.upsert_work_chat(
            session,
            user.id,
            telegram_chat_id=peer,
            title="Anna",
            kind=ChatKind.private,
            enabled=True,
        )
        await session.commit()
    harness = US2Harness(factory, settings, llm_router, fake_llm, gateway, clock)
    item = CommitmentItem(
        what="send the deck",
        to_whom="Anna",
        due=date(2026, 10, 15),
        confidence=0.9,
        evidence="Пришлю презентацию",
    )

    def slow(_: object) -> CommitmentExtraction:
        return CommitmentExtraction(items=[item])

    fake_llm.set_response("extract.commitments", slow)
    fake_llm.latency_s = 0.3
    await harness.manual_reply(user, chat, "Пришлю презентацию в четверг")
    mine = select(Commitment).where(Commitment.user_id == user.id)
    async with factory() as session:
        found = (await session.execute(mine)).scalars().all()
    assert found == []  # not yet: the LLM call is still running in the background
    await harness.composer.commitments.drain()
    async with factory() as session:
        (stored,) = (await session.execute(mine)).scalars().all()
    assert stored.what == "send the deck" and stored.user_id == user.id
