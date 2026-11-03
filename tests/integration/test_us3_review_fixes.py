"""Phase 5 review fixes (US3): content at rest, locking, alerting, stale callbacks, credentials in
the chat, cross-user ids. Synthetic data only; Trello is served by respx, the LLM by the fake
adapter, Telegram by the fake gateway."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
import respx
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import DeleteMessage, SendMessage
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context import commitments as commitments_ctx
from scribe.context.tasks import service as tasks_service
from scribe.context.tasks.jira import JiraTaskSource
from scribe.context.tasks.service import ALERTED_KEY, FAILURES_KEY, TaskSources
from scribe.context.tasks.trello import TRELLO_API, TrelloTaskSource
from scribe.crypto import decrypt
from scribe.db.models import (
    ChatKind,
    Commitment,
    ConnectionStatus,
    Contact,
    ContactKind,
    Draft,
    DraftState,
    TaskProvider,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.stored_output import OUTPUT_CONTEXT
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import CommitmentExtraction, CommitmentItem, DraftOutput
from scribe.llm.registry import LLMRouter
from scribe.retention.purge import purge_messages
from scribe.telegram.control_bot.setup_flow import SetupStates
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import (
    FrozenClock,
    LogCapture,
    MakeChat,
    MakeContact,
    MakeUser,
    next_telegram_id,
)
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import draft_output, select_all_history
from tests.integration.us2_support import US2Harness
from tests.integration.us3_support import (
    BOARD_ID,
    TRELLO_CREDENTIALS,
    block,
    connect_trello,
    inline_extractor,
    mock_trello_board,
    trello_card,
)

FRIDAY = date(2026, 10, 16)  # the clock is frozen on Wednesday 2026-10-14
THURSDAY = date(2026, 10, 15)
KEY = "0123456789abcdef0123456789abcdef"
TOKEN = "ATTAsynthetictoken0123456789abcdef0123456789abcdef0123456789abcdef"


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


@pytest.fixture
def uncached_tasks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every draft asks Trello again: no positive and no negative cache."""
    sources = TaskSources(
        {"trello": TrelloTaskSource(cache_ttl_s=0), "jira": JiraTaskSource()}, negative_ttl_s=0
    )
    monkeypatch.setattr(tasks_service, "_default", sources)
    yield


async def _setup(
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Anna Testova")
    chat = await make_chat(user, title="Anna Testova", telegram_chat_id=contact.telegram_peer_id)
    return user, chat, contact


async def _draft(
    harness: US2Harness, user: User, chat: WorkChat, contact: Contact, message: str
) -> Draft:
    await harness.incoming(user, chat, contact, message)
    await harness.tick()
    drafts = await harness.drafts(user)
    assert drafts, "no draft was prepared"
    # Drafts of different turns share version 1, so their order is the heap order: prefer the
    # pending one (the earlier turns' drafts were handled).
    pending = [d for d in drafts if d.state == DraftState.pending]
    return pending[-1] if pending else drafts[-1]


def _incoming_id(call: FakeCall) -> str:
    return str(block(call, "incoming_messages")[0]["id"])


def _cards(harness: US2Harness) -> list[str]:
    return [m.text for m in harness.api.of_type(SendMessage) if "✍️ Draft" in m.text]


async def _commitment(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    contact: Contact,
    what: str,
    due: date | None,
    sent_at: datetime,
) -> str:
    async with session_factory() as session:
        stored = await commitments_repo.add_commitment(
            session,
            user.id,
            contact_id=contact.id,
            what=what,
            due=due,
            source_sent_at=sent_at,
            confidence=0.9,
            source_message_text="Отправлю презентацию в четверг",
        )
        await session.commit()
        return f"commit:{stored.id}"


async def _raw_output_json(session_factory: async_sessionmaker[AsyncSession], draft: Draft) -> str:
    async with session_factory() as session:
        return str(
            (
                await session.execute(
                    text("SELECT output_json::text FROM drafts WHERE id = :d"), {"d": draft.id}
                )
            ).scalar_one()
        )


# --- A: drafts.output_json holds no content in the clear ---------------------------------------


async def test_output_json_holds_no_content_at_rest(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_trello(session_factory, user)
    ref = await _commitment(
        session_factory, user, contact, "send the presentation", THURSDAY, clock()
    )
    marker = "[[номер счёта]]"
    reply = f"Отчёт будет в пятницу, {marker}"

    def respond(call: FakeCall) -> DraftOutput:
        (task,) = block(call, "tasks")
        return draft_output(
            reply,
            needs_info=True,
            placeholders=[{"marker": marker, "question": "Which invoice number?"}],
            sources=[
                {"kind": "message", "id": _incoming_id(call), "used_for": "the synthetic ask"},
                {"kind": "task", "id": task["id"], "used_for": "report due date"},
            ],
            flags={
                "creates_commitment": {
                    "value": True,
                    "what": "deliver the synthetic report",
                    "due": "2026-10-16",
                },
                "sensitive": {"value": True, "categories": ["payment"]},
            },
        )

    fake_llm.set_response("compose.draft", respond)
    with respx.mock(assert_all_called=True) as router:
        mock_trello_board(router, [trello_card("c1", "Q3 report", FRIDAY)])
        draft = await _draft(
            harness, user, chat, contact, "Сможешь прислать презентацию в среду? Что с отчётом?"
        )

    raw = await _raw_output_json(session_factory, draft)
    for content in (
        "Отчёт будет",
        marker,
        "Which invoice",
        "synthetic ask",
        "report due date",
        "task:c1",
        "deliver the synthetic report",
        "2026-10-16",
        "payment",
        "Q3 report",  # the backstop conflict quotes the card title
        "send the presentation",
        ref,
    ):
        assert content not in raw, content
    stored = json.loads(raw)
    assert set(stored) == {
        "language",
        "needs_info",
        "tone_shift_applied",
        "context_warnings",
        "system_warnings",
        "reply_to_message_id",
        "creates_commitment",
        "sensitive",
        "enc",
    }
    assert stored["needs_info"] is True and stored["sensitive"] is True
    assert "Q3 report" in decrypt(stored["enc"], OUTPUT_CONTEXT)  # encrypted, not gone

    # The card still renders placeholders, sources and conflicts from the decrypted blob.
    (card,) = _cards(harness)
    assert "❓ Needs info:\n1. Which invoice number?" in card
    assert "Trello “Q3 report” (due Fri 16 Oct)" in card
    assert '⚠️ conflicts with "send the presentation (due Thu 15 Oct)"' in card
    assert "🤝 commits you to deliver the synthetic report (by 2026-10-16)" in card
    await harness.feed(callback_update(user.telegram_control_chat_id, f"sources:{draft.id}"))
    assert "— report due date" in harness.bot_texts()[-1]

    # Retention removes the blob with the draft text 30 days after the final state.
    await harness.feed(callback_update(user.telegram_control_chat_id, f"reject:{draft.id}"))
    await purge_messages(session_factory, settings, clock() + timedelta(days=29))
    assert "enc" in json.loads(await _raw_output_json(session_factory, draft))
    await purge_messages(session_factory, settings, clock() + timedelta(days=31))
    purged = json.loads(await _raw_output_json(session_factory, draft))
    assert "enc" not in purged and purged["language"] == "ru"
    from scribe.drafting.stored_output import load_stored_output

    (row,) = await harness.drafts(user)
    assert row.text is None
    assert load_stored_output(row.output_json, row.text).output is None


async def test_legacy_plaintext_output_json_is_read_and_purged(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    from scribe.db.repos import turns as turns_repo
    from scribe.drafting.stored_output import load_stored_output

    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    legacy_output = draft_output(
        "Synthetic legacy reply",
        sources=[{"kind": "task", "id": "task:legacy", "used_for": "legacy note"}],
    )
    async with session_factory() as session:
        turn = await turns_repo.create_turn(
            session,
            user.id,
            work_chat_id=chat.id,
            contact_id=contact.id,
            first_message_id=None,
            arrived_at=clock(),
            delay_s=0,
            due_at=clock(),
        )
        draft = await drafts_repo.create_draft(
            session,
            user.id,
            conversation_turn_id=turn.id,
            text="Synthetic legacy reply",
            output_json={
                **legacy_output.model_dump(mode="json"),
                "system_warnings": ["weak_style_profile"],
            },
            prepared_at=clock(),
        )
        draft.state, draft.finalized_at = DraftState.rejected, clock()
        await session.commit()
    stored = load_stored_output(draft.output_json, draft.text)
    assert stored.output is not None and stored.output.sources[0].used_for == "legacy note"
    assert stored.system_warnings == ["weak_style_profile"]

    await purge_messages(session_factory, settings, clock() + timedelta(days=31))
    purged = json.loads(await _raw_output_json(session_factory, draft))
    for key in ("text", "sources", "placeholders", "flags", "conflicts"):
        assert key not in purged
    assert purged["system_warnings"] == ["weak_style_profile"] and purged["language"] == "ru"


# --- B: commitments.what is encrypted at rest ---------------------------------------------------


async def test_commitment_what_encrypted_at_rest(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    ref = await _commitment(
        session_factory, user, contact, "send the synthetic deck", None, clock()
    )
    async with session_factory() as session:
        raw = (
            await session.execute(
                text("SELECT what FROM commitments WHERE id = :c"),
                {"c": ref.removeprefix("commit:")},
            )
        ).scalar_one()
        (stored,) = await commitments_repo.list_open(session, user.id)
    assert "synthetic deck" not in raw and raw.startswith("e2:v1:")
    assert decrypt(raw, "commitments:what") == "send the synthetic deck" == stored.what


async def test_commitment_what_written_before_encryption_is_still_read(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    ref = await _commitment(session_factory, user, contact, "placeholder", None, clock())
    async with session_factory() as session:
        await session.execute(
            text("UPDATE commitments SET what = 'legacy plaintext promise' WHERE id = :c"),
            {"c": ref.removeprefix("commit:")},
        )
        await session.commit()
    async with session_factory() as session:
        (stored,) = await commitments_repo.list_open(session, user.id)
    assert stored.what == "legacy plaintext promise"


# --- J: task_source_connections.config_json writers lock the row --------------------------------


async def _committed_user(factory: async_sessionmaker[AsyncSession], settings: Settings) -> User:
    async with factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings, display_name="U"
        )
        user.state = UserState.active
        await tasks_conn_repo.upsert_connection(
            session,
            user.id,
            provider=TaskProvider.trello,
            credentials=dict(TRELLO_CREDENTIALS),
            config_json={"board_ids": [BOARD_ID]},
        )
        await session.commit()
        return user


async def test_concurrent_failures_are_all_counted(
    committed_session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    factory = committed_session_factory
    user = await _committed_user(factory, settings)
    await asyncio.gather(*(TaskSources._record(factory, user.id, ok=False) for _ in range(6)))
    async with factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
    assert conn is not None and conn.config_json[FAILURES_KEY] == 6
    assert conn.status == ConnectionStatus.error


async def test_failure_recording_does_not_revert_a_board_toggle(
    committed_session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """A board toggle holds the row lock while a failure is recorded: both changes survive."""
    factory = committed_session_factory
    user = await _committed_user(factory, settings)
    async with factory() as session:
        await session.execute(
            text("SELECT id FROM task_source_connections WHERE user_id = :u FOR UPDATE"),
            {"u": user.id},
        )
        recording = asyncio.create_task(TaskSources._record(factory, user.id, ok=False))
        await asyncio.sleep(0.3)  # the recorder has read the row and waits for the lock
        await session.execute(
            text(
                "UPDATE task_source_connections SET config_json = "
                'config_json || \'{"board_ids": ["b-new"]}\'::jsonb WHERE user_id = :u'
            ),
            {"u": user.id},
        )
        await session.commit()
    await recording
    async with factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
    assert conn is not None
    assert conn.config_json["board_ids"] == ["b-new"]
    assert conn.config_json[FAILURES_KEY] == 1


# --- K: the task-source alert is sent once per failure streak -----------------------------------


async def test_task_source_alert_once_per_streak(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    uncached_tasks: None,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_trello(session_factory, user)
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            "Гляну и отпишусь",
            sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "the question"}],
        ),
    )

    def alerts() -> list[str]:
        return [t for t in harness.bot_texts() if "Reconnect it in /tasks" in t]

    async def draft_and_reject(n: int) -> None:
        draft = await _draft(harness, user, chat, contact, f"Как дела с задачей {n}?")
        await harness.feed(callback_update(user.telegram_control_chat_id, f"reject:{draft.id}"))

    async def connection() -> tuple[ConnectionStatus, dict[str, object]]:
        async with session_factory() as session:
            conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None
        return conn.status, dict(conn.config_json)

    def failing(router: respx.MockRouter) -> None:
        for part in ("cards", "lists", "members"):
            router.get(f"{TRELLO_API}/1/boards/{BOARD_ID}/{part}").respond(401, text="no")

    with respx.mock(assert_all_called=False) as router:
        failing(router)
        for n in range(4):  # the 4th failure in a row sends no second alert
            await draft_and_reject(n)
    assert len(alerts()) == 1
    status, config = await connection()
    assert status == ConnectionStatus.error and config[FAILURES_KEY] == 4 and config[ALERTED_KEY]

    with respx.mock(assert_all_called=True) as router:
        mock_trello_board(router, [trello_card("c1", "Q3 report", FRIDAY)])
        await draft_and_reject(10)  # a success resets counter, status and alert flag
    status, config = await connection()
    assert status == ConnectionStatus.active
    assert FAILURES_KEY not in config and ALERTED_KEY not in config

    with respx.mock(assert_all_called=False) as router:
        failing(router)
        for n in range(20, 23):  # a new streak alerts again
            await draft_and_reject(n)
    assert len(alerts()) == 2


# --- L: concurrent extraction stores a commitment once ------------------------------------------


async def test_concurrent_stores_dedupe(
    committed_session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    factory = committed_session_factory
    async with factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings, display_name="U"
        )
        contact = await chats_repo.upsert_contact(
            session,
            user.id,
            telegram_peer_id=next_telegram_id(),
            kind=ContactKind.person,
            display_name="Anna",
        )
        await session.commit()
    item = CommitmentItem(
        what="send the deck",
        to_whom="Anna",
        due=THURSDAY,
        confidence=0.9,
        evidence="пришлю презентацию",
    )
    text_ = "Пришлю презентацию в четверг"

    async def store() -> None:
        async with factory() as session:
            await commitments_ctx.store_items(
                session,
                user.id,
                contact_id=contact.id,
                items=[item],
                source_text=text_,
                sent_at=datetime(2026, 10, 14, 10, 0, tzinfo=UTC),
            )
            await asyncio.sleep(0.2)  # keep the transaction open while the other one runs
            await session.commit()

    await asyncio.gather(store(), store())
    async with factory() as session:
        rows = (
            (await session.execute(select(Commitment).where(Commitment.user_id == user.id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


# --- N: stale /tasks and /commitments buttons do not override setup -----------------------------


async def test_stale_buttons_leave_the_setup_flow_alone(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.setting_up)
    contact = await make_contact(user)
    ref = await _commitment(session_factory, user, contact, "send the deck", None, clock())
    chat_id = user.telegram_control_chat_id
    key = StorageKey(bot_id=harness.bot.id, chat_id=chat_id, user_id=chat_id)
    storage = harness.dispatcher.storage
    await storage.set_state(key, SetupStates.pick_chats)
    await storage.set_data(key, {"synthetic_setup": "kept"})

    await harness.feed(callback_update(chat_id, "tasks:connect"))
    await harness.feed(callback_update(chat_id, f"cmtedit:{ref.removeprefix('commit:')}"))

    assert await storage.get_state(key) == SetupStates.pick_chats.state
    assert await storage.get_data(key) == {"synthetic_setup": "kept"}
    assert not any("API key" in t or "Send the new text" in t for t in harness.bot_texts())


# --- D: the Trello key does not linger in the chat or the FSM data ------------------------------


async def test_trello_key_prompt_deleted_and_key_cleared(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    key = StorageKey(bot_id=harness.bot.id, chat_id=chat_id, user_id=chat_id)
    storage = harness.dispatcher.storage

    # Abandoned after the key: the next unrelated command clears the key and the prompt.
    await harness.feed(callback_update(chat_id, "tasks:connect"))
    await harness.feed(text_update(chat_id, KEY))
    prompt = harness.messages()[-1]
    assert f"key={KEY}" in prompt.text
    assert (await storage.get_data(key)).get("key") == KEY
    await harness.feed(text_update(chat_id, "/settings"))
    assert await storage.get_state(key) is None
    assert "key" not in await storage.get_data(key)
    deleted = harness.api.of_type(DeleteMessage)
    assert len(deleted) == 2  # the key message, then the prompt that echoed it

    # Completed: the prompt with the key is deleted after the token step.
    await harness.feed(callback_update(chat_id, "tasks:connect"))
    await harness.feed(text_update(chat_id, KEY))
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{TRELLO_API}/1/members/me").respond(
            200, json={"id": "m1", "username": "synthetic", "fullName": "Synthetic User"}
        )
        router.get(f"{TRELLO_API}/1/members/me/boards").respond(200, json=[])
        await harness.feed(text_update(chat_id, TOKEN))
    assert len(harness.api.of_type(DeleteMessage)) == 5  # + key, token, prompt
    assert await storage.get_data(key) == {}


async def test_trello_key_cleared_on_cancel(harness: US2Harness, make_user: MakeUser) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    key = StorageKey(bot_id=harness.bot.id, chat_id=chat_id, user_id=chat_id)
    await harness.feed(callback_update(chat_id, "tasks:connect"))
    await harness.feed(text_update(chat_id, KEY))
    await harness.feed(text_update(chat_id, "/cancel"))
    assert await harness.dispatcher.storage.get_data(key) == {}
    assert len(harness.api.of_type(DeleteMessage)) == 2  # the key, the prompt


# --- O: reconnecting another Trello account drops the board selection ---------------------------


async def test_reconnect_with_another_account_resets_boards(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id

    async def connect(member_id: str) -> list[str]:
        await harness.feed(callback_update(chat_id, "tasks:connect"))
        await harness.feed(text_update(chat_id, KEY))
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{TRELLO_API}/1/members/me").respond(
                200, json={"id": member_id, "username": "synthetic", "fullName": "Synthetic"}
            )
            router.get(f"{TRELLO_API}/1/members/me/boards").respond(200, json=[])
            await harness.feed(text_update(chat_id, TOKEN))
        async with session_factory() as session:
            conn = await tasks_conn_repo.get_connection(session, user.id)
            assert conn is not None
            if not conn.config_json.get("board_ids"):
                conn.config_json = {**conn.config_json, "board_ids": ["b-picked"]}
                await session.commit()
                return []
            return list(conn.config_json["board_ids"])

    assert await connect("member-a") == []  # first connection: boards picked afterwards
    assert await connect("member-a") == ["b-picked"]  # same account: selection kept
    assert await connect("member-b") == []  # another account: selection dropped


# --- Q: ids of another user's tasks or commitments are never accepted ---------------------------


async def test_draft_citing_another_users_items_is_rejected(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    other, _, other_contact = await _setup(make_user, make_chat, make_contact)
    foreign_ref = await _commitment(
        session_factory, other, other_contact, "foreign promise", THURSDAY, clock()
    )
    await connect_trello(session_factory, other)
    user, chat, contact = await _setup(make_user, make_chat, make_contact)

    def respond(call: FakeCall) -> DraftOutput:
        return draft_output(
            "Посмотрю",
            sources=[
                {"kind": "message", "id": _incoming_id(call), "used_for": "the question"},
                {"kind": "commitment", "id": foreign_ref, "used_for": "someone else's"},
                {"kind": "task", "id": "task:foreign-card", "used_for": "someone else's"},
            ],
            conflicts=[
                {"with_kind": "commitment", "with_id": foreign_ref, "description": "foreign"}
            ],
        )

    from scribe.drafting.stored_output import load_stored_output

    fake_llm.set_response("compose.draft", respond)
    draft = await _draft(harness, user, chat, contact, "Как дела?")
    assert len(fake_llm.calls_for("compose.draft")) == 2  # rejected, regenerated once
    stored = load_stored_output(draft.output_json, draft.text)
    assert stored.output is not None
    assert [s.kind for s in stored.output.sources] == ["message"]
    assert stored.output.conflicts == [] and stored.output.needs_info
    assert "unsupported_sources" in stored.system_warnings
    (card,) = _cards(harness)
    assert "foreign" not in card


async def test_background_extraction_scoped_to_its_user(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
) -> None:
    """Shutdown drain: an extraction still running when the service stops is finished first."""
    factory = committed_session_factory
    async with factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings, display_name="U"
        )
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
    extractor = commitments_ctx.ensure_default_extractor(factory, llm_router, clock=clock)
    fake_llm.set_response(
        "extract.commitments",
        lambda _: commitments_ctx_extraction("send the deck", "пришлю презентацию"),
    )
    fake_llm.latency_s = 0.3
    await extractor.submit(
        user.id, work_chat_id=chat.id, text="Пришлю презентацию в четверг", sent_at=clock()
    )
    drained = await commitments_ctx.drain_default_extractor(timeout_s=5)
    assert drained == 1
    async with factory() as session:
        rows = (
            (await session.execute(select(Commitment).where(Commitment.user_id == user.id)))
            .scalars()
            .all()
        )
    assert [(r.what, r.due) for r in rows] == [("send the deck", THURSDAY)]


def commitments_ctx_extraction(what: str, evidence: str) -> CommitmentExtraction:
    return CommitmentExtraction(
        items=[
            CommitmentItem(
                what=what, to_whom="Anna", due=THURSDAY, confidence=0.9, evidence=evidence
            )
        ]
    )


# --- O: task source robustness ------------------------------------------------------------------


class _ExplodingSource:
    """A task source whose bug raises something no one planned for."""

    provider = "trello"

    def __init__(self) -> None:
        self.calls = 0

    async def validate(self, credentials: dict[str, object]) -> object:
        raise NotImplementedError

    async def list_containers(self, credentials: dict[str, object]) -> list[object]:
        raise NotImplementedError

    async def list_open_tasks(self, conn: object) -> list[object]:
        self.calls += 1
        raise RuntimeError("synthetic bug with synthetic-trello-token inside")


class _Monotonic:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def test_any_task_source_error_is_unavailable_and_negatively_cached(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active)
    await connect_trello(session_factory, user)
    source, clock = _ExplodingSource(), _Monotonic()
    sources = TaskSources({"trello": source}, monotonic=clock)  # type: ignore[dict-item]

    first = await sources.load_open_tasks(session_factory, user.id)
    assert first.unavailable and first.tasks == [] and source.calls == 1
    clock.now += 29  # within the negative cache: Trello is not asked again
    second = await sources.load_open_tasks(session_factory, user.id)
    assert second.unavailable and source.calls == 1
    clock.now += 2
    await sources.load_open_tasks(session_factory, user.id)
    assert source.calls == 2
    sources.invalidate(user.id)  # reconnect / board change: ask again right away
    await sources.load_open_tasks(session_factory, user.id)
    assert source.calls == 3

    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
    assert conn is not None and conn.config_json[FAILURES_KEY] == 3  # skipped loads not counted
    (event, *_) = log_capture.events("task_source_unavailable")
    assert event["reason"] == "RuntimeError"
    assert (
        "synthetic-trello-token" not in log_capture.raw and "synthetic bug" not in log_capture.raw
    )


async def test_partial_board_load_drafts_with_a_warning(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_trello(session_factory, user, board_ids=[BOARD_ID, "gone-board"])
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            "Ок", sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "q"}]
        ),
    )
    with respx.mock(assert_all_called=True) as router:
        mock_trello_board(router, [trello_card("c1", "Q3 report", FRIDAY)])
        for part in ("cards", "lists", "members"):
            router.get(f"{TRELLO_API}/1/boards/gone-board/{part}").respond(404, text="gone")
        draft = await _draft(harness, user, chat, contact, "Как отчёт?")
    (compose,) = fake_llm.calls_for("compose.draft")
    assert [t["id"] for t in block(compose, "tasks")] == ["t1"]  # short id (#71)
    assert draft.output_json["system_warnings"] == ["weak_style_profile", "tasks_partial"]
    (card,) = _cards(harness)
    assert "⚠️ some task boards could not be read" in card
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
    assert conn is not None and conn.status == ConnectionStatus.active
    assert FAILURES_KEY not in conn.config_json  # a partial load is not a failure


# --- I: the card shows the user's local due date ------------------------------------------------


async def test_card_shows_the_local_due_date(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    async with session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
        assert stored is not None
        stored.timezone = "Europe/Moscow"
        await session.commit()
    await connect_trello(session_factory, user)

    def respond(call: FakeCall) -> DraftOutput:
        (task,) = block(call, "tasks")
        assert task["due"] == "2026-10-17"  # Sat 01:30 in Moscow, not the UTC Friday
        return draft_output(
            "Будет в субботу",
            sources=[
                {"kind": "message", "id": _incoming_id(call), "used_for": "q"},
                {"kind": "task", "id": task["id"], "used_for": "due"},
            ],
        )

    fake_llm.set_response("compose.draft", respond)
    card_json = trello_card("c1", "Q3 report", None)
    card_json["due"] = "2026-10-16T22:30:00.000Z"
    with respx.mock(assert_all_called=True) as router:
        mock_trello_board(router, [card_json])
        await _draft(harness, user, chat, contact, "Когда отчёт?")
    (card,) = _cards(harness)
    assert "Trello “Q3 report” (due Sat 17 Oct)" in card
