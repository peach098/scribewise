"""US3 end to end (T052): Trello tasks and extracted commitments as draft sources, conflicts,
placeholders for unknowable questions and drafting without task context. Synthetic data only;
Trello is served by respx, the LLM by the fake adapter, Telegram by the fake gateway."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
import respx
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context.tasks import service as tasks_service
from scribe.context.tasks.trello import TRELLO_API
from scribe.db.models import (
    CommitmentState,
    ConnectionStatus,
    Contact,
    Draft,
    DraftState,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.drafting.stored_output import load_stored_output
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import CommitmentExtraction, CommitmentItem, DraftOutput
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import OutgoingEvent
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import draft_output, select_all_history
from tests.integration.us2_support import US2Harness
from tests.integration.us3_support import (
    BOARD_ID,
    block,
    commitments_of,
    connect_trello,
    inline_extractor,
    mock_trello_board,
    trello_card,
)

FRIDAY = date(2026, 10, 16)  # the clock is frozen on Wednesday 2026-10-14
THURSDAY = date(2026, 10, 15)


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


async def _setup(
    make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, WorkChat, Contact]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Anna Testova")
    chat = await make_chat(user, title="Anna Testova", telegram_chat_id=contact.telegram_peer_id)
    return user, chat, contact


async def _draft(
    harness: US2Harness, user: User, chat: WorkChat, contact: Contact, text: str
) -> Draft:
    await harness.incoming(user, chat, contact, text)
    await harness.tick()
    # The new draft is the pending one: ``drafts()`` orders by version only, and every turn's
    # first draft is version 1, so the last item is not necessarily the newest.
    drafts = [d for d in await harness.drafts(user) if d.state == DraftState.pending]
    assert drafts, "no draft was prepared"
    return drafts[-1]


def _cards(harness: US2Harness) -> list[str]:
    return [m.text for m in harness.api.of_type(SendMessage) if "✍️ Draft" in m.text]


def _output(draft: Draft) -> DraftOutput:
    output = load_stored_output(draft.output_json, draft.text).output
    assert output is not None
    return output


def _incoming_id(call: FakeCall) -> str:
    return str(block(call, "incoming_messages")[0]["id"])


def _extraction(*items: CommitmentItem) -> CommitmentExtraction:
    return CommitmentExtraction(items=list(items))


def _item(
    what: str, due: date | None, confidence: float = 0.9, evidence: str = "synthetic"
) -> CommitmentItem:
    return CommitmentItem(
        what=what, to_whom="Anna Testova", due=due, confidence=confidence, evidence=evidence
    )


# --- acceptance 1: a task is used and shown as a source -----------------------------------------


async def test_task_cited_as_source(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_trello(session_factory, user)

    def respond(call: FakeCall) -> DraftOutput:
        (task,) = block(call, "tasks")
        return draft_output(
            "Привет! Отчёт за Q3 будет в пятницу",
            sources=[
                {"kind": "message", "id": _incoming_id(call), "used_for": "the question"},
                {"kind": "task", "id": task["id"], "used_for": "status and due date"},
            ],
        )

    fake_llm.set_response("compose.draft", respond)
    with respx.mock(assert_all_called=True) as router:
        mock_trello_board(router, [trello_card("c1", "Q3 report", FRIDAY)])
        draft = await _draft(harness, user, chat, contact, "Когда будет готов отчёт за Q3?")

    # The task reached the filter and the draft request with a namespaced id, title, status, due.
    (compose,) = fake_llm.calls_for("compose.draft")
    (task,) = block(compose, "tasks")
    assert task["id"] == "t1"  # short per-request id (#71); stored as task:c1 (below)
    assert (task["title"], task["status"], task["due"]) == (
        "Q3 report",
        "In progress",
        "2026-10-16",
    )
    assert task["assignees"] == ["Anna Testova"]
    (filter_call,) = fake_llm.calls_for("retrieve.filter_context")
    assert [t["id"] for t in block(filter_call, "candidates")["tasks"]] == ["t1"]

    assert "task:c1" in {s.id for s in _output(draft).sources}
    assert draft.output_json["system_warnings"] == ["weak_style_profile"]
    (card,) = _cards(harness)
    assert "пятницу" in card
    assert "📎 Sources:" in card and "Trello “Q3 report” (due Fri 16 Oct)" in card

    # 🧾 Sources expands the task (from the 60 s cache: no further Trello call needed).
    await harness.feed(callback_update(user.telegram_control_chat_id, f"sources:{draft.id}"))
    expanded = harness.bot_texts()[-1]
    assert "🧾 Sources for this draft:" in expanded
    assert "Trello “Q3 report” (due Fri 16 Oct) · In progress — status and due date" in expanded


# --- acceptance 2 + FR-031: outgoing messages create commitments --------------------------------


async def test_outgoing_message_creates_commitment(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Пришлёшь презентацию?")
    fake_llm.set_response(
        "extract.commitments",
        _extraction(
            _item("send the presentation", THURSDAY, evidence="Отправлю презентацию в четверг"),
            _item("maybe a call", None, 0.5),
        ),
    )

    message_id = await harness.manual_reply(user, chat, "Отправлю презентацию в четверг")
    await harness.composer.commitments.drain()

    (call,) = fake_llm.calls_for("extract.commitments")
    assert block(call, "outgoing_text") == "Отправлю презентацию в четверг"
    assert block(call, "contact_display_name") == "Anna Testova"
    assert [m["text"] for m in block(call, "recent_incoming")] == ["Пришлёшь презентацию?"]
    (stored,) = await commitments_of(session_factory, user)  # the 0.5 item is not stored
    assert (stored.contact_id, stored.what, stored.due) == (
        contact.id,
        "send the presentation",
        THURSDAY,
    )
    assert stored.state == CommitmentState.open and stored.confidence == Decimal("0.90")
    assert stored.source_message_text == "Отправлю презентацию в четверг"
    assert stored.source_sent_at == clock()

    # A redelivered update of the same message is not extracted again.
    await harness.gateway.emit_outgoing(
        OutgoingEvent(
            user_id=user.id,
            chat_id=chat.telegram_chat_id,
            message_id=message_id,
            text="Отправлю презентацию в четверг",
            sent_at=clock(),
        )
    )
    await harness.composer.commitments.drain()
    assert len(fake_llm.calls_for("extract.commitments")) == 1

    # /commitments lists it: what · to whom · due.
    await harness.feed(text_update(user.telegram_control_chat_id, "/commitments"))
    listing = harness.bot_texts()[-1]
    assert "send the presentation · Anna Testova · due Thu 15 Oct" in listing


async def test_a_forwarded_outgoing_message_is_not_extracted(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """#47: a message the user forwarded is not their promise; it still counts as a manual
    reply (the waiting turn is dropped)."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Пришлёшь презентацию?")
    fake_llm.set_response(
        "extract.commitments", _extraction(_item("send the presentation", THURSDAY))
    )

    await harness.manual_reply(
        user, chat, "Отправлю презентацию в четверг", is_forwarded=True, forwarded_from="Ivan"
    )
    await harness.composer.commitments.drain()

    assert fake_llm.calls_for("extract.commitments") == []
    assert await commitments_of(session_factory, user) == []
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.dropped


async def test_a_received_forward_is_marked_in_extraction_context(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """#47: the extraction sees a received forward marked as forwarded, with its origin."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(
        user, chat, contact, "Отчёт пришлю в пятницу", is_forwarded=True, forwarded_from="Ivan"
    )
    await harness.incoming(user, chat, contact, "Ты успеешь к этому сроку?")
    fake_llm.set_response("extract.commitments", _extraction())

    await harness.manual_reply(user, chat, "Да, гляну завтра")
    await harness.composer.commitments.drain()

    (call,) = fake_llm.calls_for("extract.commitments")
    recent = block(call, "recent_incoming")
    assert sorted((m["text"], m.get("forwarded"), m.get("forwarded_from")) for m in recent) == [
        ("Отчёт пришлю в пятницу", True, "Ivan"),
        ("Ты успеешь к этому сроку?", None, None),
    ]


async def test_sent_draft_yields_exactly_one_commitment(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            "Пришлю отчёт в пятницу",
            sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "the question"}],
            flags={
                "creates_commitment": {
                    "value": True,
                    "what": "send the report",
                    "due": "2026-10-16",
                },
                "sensitive": {"value": False, "categories": []},
            },
        ),
    )
    fake_llm.set_response(
        "extract.commitments",
        _extraction(_item("send the report", FRIDAY, evidence="Пришлю отчёт в пятницу")),
    )
    draft = await _draft(harness, user, chat, contact, "Когда пришлёшь отчёт?")
    (card,) = _cards(harness)
    assert "🤝 commits you to send the report (by 2026-10-16)" in card

    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    (sent,) = gateway.send_text_calls
    # Telethon echoes the agent's own send as an outgoing message: not extracted a second time.
    await gateway.emit_outgoing(
        OutgoingEvent(
            user_id=user.id,
            chat_id=chat.telegram_chat_id,
            message_id=sent.message_id,
            text=sent.text,
            sent_at=clock(),
        )
    )
    await harness.composer.commitments.drain()

    assert len(fake_llm.calls_for("extract.commitments")) == 1
    # Issue #72: the unchanged draft's true flag still extracts (it holds no grounded item).
    (gate,) = log_capture.events("commitment_extraction_gate")
    assert (gate["extract"], gate["reason"]) == (True, "draft_flag_true")
    (stored,) = await commitments_of(session_factory, user)
    assert (stored.what, stored.due, stored.contact_id) == ("send the report", FRIDAY, contact.id)
    assert stored.source_message_text == "Пришлю отчёт в пятницу"


async def test_extraction_failure_never_affects_sending(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    from scribe.llm.provider import ProviderUnavailable

    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            "Ок, пришлю",
            sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "q"}],
            flags=_commits("send it"),
        ),
    )
    fake_llm.set_response("extract.commitments", ProviderUnavailable("synthetic outage"))
    draft = await _draft(harness, user, chat, contact, "Пришлёшь?")
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    await harness.composer.commitments.drain()
    assert len(fake_llm.calls_for("extract.commitments")) == 1  # attempted, and it failed
    assert len(gateway.send_text_calls) == 1
    assert await commitments_of(session_factory, user) == []
    (stored,) = await harness.drafts(user)
    assert stored.state.value == "sent"


def _commits(what: str | None, value: bool = True) -> dict[str, object]:
    return {
        "creates_commitment": {"value": value, "what": what, "due": None},
        "sensitive": {"value": False, "categories": []},
    }


# --- issue #72: no extract.commitments call without a promise cue -------------------------------


async def test_unchanged_draft_flagged_as_no_commitment_is_not_extracted(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    text = "Видел письмо в четверг"  # a date word: the text alone would go to the LLM
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            text,
            sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "q"}],
            flags=_commits(None, value=False),
        ),
    )
    fake_llm.set_response("extract.commitments", _extraction())
    draft = await _draft(harness, user, chat, contact, "Видел моё письмо?")
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    await harness.composer.commitments.drain()

    assert [c.text for c in gateway.send_text_calls] == [text]
    assert fake_llm.calls_for("extract.commitments") == []
    (gate,) = log_capture.events("commitment_extraction_gate")
    assert gate == gate | {
        "user_id": str(user.id),
        "source": "telegram",
        "extract": False,
        "reason": "draft_flag_false",
    }
    assert text not in log_capture.raw and "письмо" not in log_capture.raw  # Principle II


async def test_manual_trivial_reply_is_not_extracted(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Отчёт загрузил в папку")
    fake_llm.set_response("extract.commitments", _extraction())

    await harness.manual_reply(user, chat, "Спасибо!")
    await harness.composer.commitments.drain()

    assert fake_llm.calls_for("extract.commitments") == []
    (gate,) = log_capture.events("commitment_extraction_gate")
    assert (gate["extract"], gate["reason"]) == (False, "no_cue")
    assert "Спасибо" not in log_capture.raw


async def test_manual_short_acceptance_of_a_request_is_extracted(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Пришлёшь отчёт до пятницы?")
    fake_llm.set_response("extract.commitments", _extraction())

    await harness.manual_reply(user, chat, "Ок")
    await harness.composer.commitments.drain()

    (call,) = fake_llm.calls_for("extract.commitments")
    assert block(call, "outgoing_text") == "Ок"
    (gate,) = log_capture.events("commitment_extraction_gate")
    assert (gate["extract"], gate["reason"]) == (True, "acceptance")


# --- acceptance 3: conflicts --------------------------------------------------------------------


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


async def test_conflict_flagged(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    ref = await _commitment(
        session_factory, user, contact, "send the presentation", THURSDAY, clock()
    )

    def respond(call: FakeCall) -> DraftOutput:
        (commitment,) = block(call, "commitments")
        return draft_output(
            "Я обещал в четверг, в среду не успею",
            sources=[
                {"kind": "message", "id": _incoming_id(call), "used_for": "the request"},
                {"kind": "commitment", "id": commitment["id"], "used_for": "promised Thursday"},
            ],
            conflicts=[
                {
                    "with_kind": "commitment",
                    "with_id": commitment["id"],
                    "description": "asked for Wednesday, promised Thursday",
                }
            ],
        )

    fake_llm.set_response("compose.draft", respond)
    draft = await _draft(harness, user, chat, contact, "Can you send the presentation Wednesday?")

    (compose,) = fake_llm.calls_for("compose.draft")
    assert block(compose, "commitments") == [  # the prompt carries a short id (#71)
        {"id": "c1", "what": "send the presentation", "due": "2026-10-15"}
    ]
    assert [c.with_id for c in _output(draft).conflicts] == [ref]
    (card,) = _cards(harness)
    assert '⚠️ conflicts with "send the presentation (due Thu 15 Oct)"' in card
    assert "your promise “send the presentation” (due Thu 15 Oct)" in card


async def test_conflict_backstop_when_the_model_misses_it(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    ref = await _commitment(
        session_factory, user, contact, "send the presentation", THURSDAY, clock()
    )
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            "Посмотрю",
            sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "the request"}],
        ),
    )
    draft = await _draft(harness, user, chat, contact, "Сможешь прислать презентацию в среду?")
    (conflict,) = _output(draft).conflicts
    assert (conflict.with_kind, conflict.with_id) == ("commitment", ref)
    assert "Wed 14 Oct" in conflict.description and "Thu 15 Oct" in conflict.description
    (card,) = _cards(harness)
    assert '⚠️ conflicts with "send the presentation (due Thu 15 Oct)"' in card


# --- acceptance 4: unknowable question ---------------------------------------------------------


async def test_unknowable_question_yields_placeholder_and_confirmation(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    marker = "[[что решил Петя на встрече]]"
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            f"Привет! {marker}",
            needs_info=True,
            placeholders=[{"marker": marker, "question": "What did Petya decide at 3pm?"}],
            sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "the question"}],
        ),
    )
    draft = await _draft(harness, user, chat, contact, "Что решил Петя на встрече в 15:00?")
    (card,) = _cards(harness)
    assert marker in card and "❓ Needs info:\n1. What did Petya decide at 3pm?" in card

    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    assert gateway.send_text_calls == []  # Send asks first
    assert "unresolved placeholders" in harness.bot_texts()[-1]
    assert "Send anyway?" in harness.bot_texts()[-1]


# --- edge case: the task source is unavailable --------------------------------------------------


async def test_tasks_unavailable_warning(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every draft asks Trello again (no 30 s negative cache), so three drafts are three failures.
    monkeypatch.setattr(tasks_service, "NEGATIVE_CACHE_S", 0.0)
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_trello(session_factory, user)
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            "Гляну и отпишусь",
            sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "the question"}],
        ),
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{TRELLO_API}/1/boards/{BOARD_ID}/cards").respond(401, text="invalid token")
        router.get(f"{TRELLO_API}/1/boards/{BOARD_ID}/lists").respond(401, text="invalid token")
        router.get(f"{TRELLO_API}/1/boards/{BOARD_ID}/members").respond(401, text="invalid token")
        drafts: list[Draft] = []
        for n in range(3):
            drafts.append(await _draft(harness, user, chat, contact, f"Как дела с задачей {n}?"))
            await harness.feed(
                callback_update(user.telegram_control_chat_id, f"reject:{drafts[-1].id}")
            )

    for draft in drafts:
        # System-side warning; the model-facing DraftOutput stays as the model returned it.
        assert "tasks_unavailable" in draft.output_json["system_warnings"]
        assert draft.output_json["context_warnings"] == []
        assert block(fake_llm.calls_for("compose.draft")[0], "tasks") == []
    cards = _cards(harness)
    assert len(cards) == 3 and all("⚠️ drafted without task context" in c for c in cards)
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
    assert conn is not None and conn.status == ConnectionStatus.error  # after repeated failures
    alerts = [t for t in harness.bot_texts() if "/tasks" in t and "Trello" in t]
    assert len(alerts) == 1  # one alert, not one per draft
    assert "synthetic-trello-token" not in "".join(harness.bot_texts())


async def test_no_task_source_means_no_warning(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    fake_llm.set_response(
        "compose.draft",
        lambda call: draft_output(
            "Ок", sources=[{"kind": "message", "id": _incoming_id(call), "used_for": "q"}]
        ),
    )
    with respx.mock(assert_all_called=False) as router:
        draft = await _draft(harness, user, chat, contact, "Ок?")
        assert not router.calls
    assert "tasks_unavailable" not in draft.output_json["system_warnings"]


# --- issue #6: deadlines from Trello comments ---------------------------------------------------


async def test_deadline_from_a_newer_comment_reaches_the_draft_and_the_card(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """The card's due field says Friday 16 Oct; a newer comment moved it to 5 Nov. The draft
    context, the conflict backstop and the card all use 5 Nov and name the comment."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_trello(session_factory, user)
    card = trello_card("c1", "Q3 report", FRIDAY)
    card["actions"] = [
        {
            "id": "a1",
            "type": "commentCard",
            "date": "2026-10-13T08:00:00.000Z",
            "data": {"text": "Client asked for more time, moving to Nov 5"},
            "memberCreator": {"fullName": "Ivan Petrov"},
        }
    ]

    def respond(call: FakeCall) -> DraftOutput:
        (task,) = block(call, "tasks")
        return draft_output(
            "Отчёт будет к 5 ноября",
            sources=[
                {"kind": "message", "id": _incoming_id(call), "used_for": "the question"},
                {"kind": "task", "id": task["id"], "used_for": "due date"},
            ],
        )

    fake_llm.set_response("compose.draft", respond)
    with respx.mock(assert_all_called=True) as router:
        mock_trello_board(router, [card])
        draft = await _draft(harness, user, chat, contact, "Сможешь отдать отчёт за Q3 до 30.10?")

    assert fake_llm.calls_for("extract.deadlines") == []  # the parser found it: no LLM call
    (task,) = block(fake_llm.calls_for("compose.draft")[0], "tasks")
    assert task["due"] == "2026-11-05" and task["deadline_source"] == "comment"
    assert task["deadline_note"].startswith("from a comment by Ivan Petrov, Tue 13 Oct")
    assert [(c.with_kind, c.with_id) for c in _output(draft).conflicts] == [("task", "task:c1")]
    (card_text,) = _cards(harness)
    assert "Trello “Q3 report” (due Thu 5 Nov, from a comment by Ivan Petrov)" in card_text
