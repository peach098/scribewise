"""Drafts with Jira issues as tasks (specs/003-jira-commitments T023, T041). Synthetic data only;
Jira is a fake site (both flavors), the LLM the fake adapter, Telegram the fake gateway."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.context.task_dates import mismatch_parts
from scribe.db.models import CommitmentSourceKind, Contact, Draft, User, UserState, WorkChat
from scribe.db.repos import commitments as commitments_repo
from scribe.drafting.stored_output import load_stored_output
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import DraftOutput
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.contract.fixtures.jira.api import FLAVORS, IVAN, Comment, FakeJira, Flavor, Issue
from tests.contract.fixtures.jira.render import Para, Text
from tests.integration.jira_support import JiraSite, connect_jira, install_jira_site
from tests.integration.us1_support import draft_output, select_all_history
from tests.integration.us2_support import US2Harness
from tests.integration.us3_support import block, inline_extractor

CREATED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


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
def site(monkeypatch: pytest.MonkeyPatch) -> JiraSite:
    return install_jira_site(monkeypatch)


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
    drafts = await harness.drafts(user)
    assert drafts, "no draft was prepared"
    return drafts[-1]


def _cards(harness: US2Harness) -> list[str]:
    return [m.text for m in harness.api.of_type(SendMessage) if "✍️ Draft" in m.text]


def _output(draft: Draft) -> DraftOutput:
    output = load_stored_output(draft.output_json, draft.text).output
    assert output is not None
    return output


def _cite_task(text: str):  # type: ignore[no-untyped-def]
    def respond(call: FakeCall) -> DraftOutput:
        (task,) = block(call, "tasks")
        incoming = str(block(call, "incoming_messages")[0]["id"])
        return draft_output(
            text,
            sources=[
                {"kind": "message", "id": incoming, "used_for": "the question"},
                {"kind": "task", "id": task["id"], "used_for": "due date"},
            ],
        )

    return respond


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_jira_task_cited_as_source(
    flavor: Flavor,
    harness: US2Harness,
    site: JiraSite,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_jira(session_factory, user, flavor)
    site.serve(
        FakeJira(
            flavor, issues=[Issue("PAY-142", "Fix refund rounding", CREATED, duedate="2026-10-16")]
        )
    )
    fake_llm.set_response("compose.draft", _cite_task("Фикс будет в пятницу"))
    draft = await _draft(harness, user, chat, contact, "Когда будет готов фикс возвратов?")

    (task,) = block(fake_llm.calls_for("compose.draft")[0], "tasks")
    assert task["id"] == "t1"  # short per-request id (#71); stored as the real ref below
    assert (task["title"], task["status"], task["due"]) == (
        "Fix refund rounding",
        "In Progress",
        "2026-10-16",
    )
    assert "task:PAY-142" in {s.id for s in _output(draft).sources}
    (card,) = _cards(harness)
    assert "Jira PAY-142 “Fix refund rounding” (due Fri 16 Oct)" in card


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_jira_comment_deadline_wins(
    flavor: Flavor,
    harness: US2Harness,
    site: JiraSite,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """FR-J06a: a newer comment deadline (by anyone) overrides the Jira due date, as for
    Trello (issue #6)."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_jira(session_factory, user, flavor)
    issue = Issue(
        "PAY-142",
        "Fix refund rounding",
        CREATED,
        duedate="2026-10-16",
        due_changes=[datetime(2026, 10, 2, 9, 0, tzinfo=UTC)],
        comments=[
            Comment(
                "10001",
                IVAN,
                [Para(Text("Client asked for more time, deadline moved to Nov 5"))],
                datetime(2026, 10, 13, 8, 0, tzinfo=UTC),
            )
        ],
    )
    site.serve(FakeJira(flavor, issues=[issue]))
    fake_llm.set_response("compose.draft", _cite_task("Фикс будет к 5 ноября"))
    await _draft(harness, user, chat, contact, "Когда будет готов фикс возвратов?")

    (task,) = block(fake_llm.calls_for("compose.draft")[0], "tasks")
    assert task["due"] == "2026-11-05" and task["deadline_source"] == "comment"
    assert task["deadline_note"].startswith("from a comment by Ivan Sample, Tue 13 Oct")
    (card,) = _cards(harness)
    assert (
        "Jira PAY-142 “Fix refund rounding” (due Thu 5 Nov, from a comment by Ivan Sample)" in card
    )


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_jira_unavailable_warning(
    flavor: Flavor,
    harness: US2Harness,
    site: JiraSite,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_jira(session_factory, user, flavor)
    site.serve(FakeJira(flavor, search_status=500))
    fake_llm.set_response("compose.draft", lambda call: draft_output("Уточню и вернусь"))
    draft = await _draft(harness, user, chat, contact, "Когда будет готов фикс?")
    assert "tasks_unavailable" in draft.output_json["system_warnings"]
    (card,) = _cards(harness)
    assert "drafted without task context" in card


# --- US3: mismatches, conflicts, outages (T041) -------------------------------------------------


async def _promise(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    *,
    due: date,
    at: datetime,
    contact: Contact | None = None,
    comment: bool = True,
) -> None:
    async with session_factory() as session:
        await commitments_repo.add_commitment(
            session, user.id, contact_id=contact.id if contact else None, what="fix on staging",
            due=due, source_sent_at=at, confidence=0.9, to_whom="Anna Testova",
            source_kind=CommitmentSourceKind.jira_comment if comment
            else CommitmentSourceKind.telegram_message,
            jira_issue_key="PAY-142", jira_comment_id="10002" if comment else None,
        )  # fmt: skip
        await session.commit()


def _refund(due: str = "2026-10-16") -> Issue:
    return Issue(
        "PAY-142", "Fix refund rounding", CREATED, duedate=due,
        due_changes=[datetime(2026, 10, 2, 9, 0, tzinfo=UTC)],
    )  # fmt: skip


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_date_mismatch_on_card(
    flavor: Flavor,
    harness: US2Harness,
    site: JiraSite,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_jira(session_factory, user, flavor)
    site.serve(FakeJira(flavor, issues=[_refund()]))
    await _promise(
        session_factory, user, due=date(2026, 10, 15), at=datetime(2026, 10, 12, 9, tzinfo=UTC)
    )
    fake_llm.set_response("compose.draft", lambda call: draft_output("Будет к четвергу"))
    draft = await _draft(harness, user, chat, contact, "Можно рассчитывать на фикс к пятнице?")

    (task,) = block(fake_llm.calls_for("compose.draft")[0], "tasks")
    assert task["due"] == "2026-10-15"  # the user's newer promise, not the due field
    assert task["deadline_note"].startswith("from your own promise (your Jira comment on PAY-142)")
    warnings = draft.output_json["system_warnings"]
    (mismatch,) = [p for w in warnings if (p := mismatch_parts(w)) is not None]  # model-independent
    assert mismatch[0] == "PAY-142"
    (card,) = _cards(harness)
    assert (
        "📅 Dates differ: PAY-142: Thu 15 Oct (your Jira comment, 12 Oct) · Fri 16 Oct (due date)"
        in card
    )


@pytest.mark.parametrize("flavor", FLAVORS)
async def test_conflict_names_issue(
    flavor: Flavor,
    harness: US2Harness,
    site: JiraSite,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_jira(session_factory, user, flavor)
    site.serve(FakeJira(flavor, issues=[_refund()]))
    fake_llm.set_response("compose.draft", lambda call: draft_output("Посмотрю"))
    draft = await _draft(harness, user, chat, contact, "Сможешь сделать фикс к 15.10?")
    assert [(c.with_kind, c.with_id) for c in _output(draft).conflicts] == [
        ("task", "task:PAY-142")
    ]
    (card,) = _cards(harness)
    assert "PAY-142" in card.split("⚑", 1)[-1]


async def test_telegram_promise_mismatch(
    harness: US2Harness,
    site: JiraSite,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """A promise made only in Telegram about PAY-142 is compared with the issue's due date."""
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_jira(session_factory, user, "cloud")
    site.serve(FakeJira("cloud", issues=[_refund()]))
    await _promise(
        session_factory, user, due=date(2026, 10, 14), at=datetime(2026, 10, 12, 9, tzinfo=UTC),
        contact=contact, comment=False,
    )  # fmt: skip
    fake_llm.set_response("compose.draft", lambda call: draft_output("Будет в среду"))
    draft = await _draft(harness, user, chat, contact, "Как там PAY-142?")
    (mismatch,) = [
        p for w in draft.output_json["system_warnings"] if (p := mismatch_parts(w)) is not None
    ]
    assert mismatch[0] == "PAY-142"
    assert {part.kind for part in mismatch[1]} == {"promise_telegram", "due"}


async def test_jira_outage_uses_stored_commitments(
    harness: US2Harness,
    site: JiraSite,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(make_user, make_chat, make_contact)
    await connect_jira(session_factory, user, "self_hosted")
    site.serve(FakeJira("self_hosted", search_status=503))
    await _promise(
        session_factory, user, due=date(2026, 10, 15), at=datetime(2026, 10, 12, 9, tzinfo=UTC),
        contact=contact,
    )  # fmt: skip
    fake_llm.set_response("compose.draft", lambda call: draft_output("Будет в четверг"))
    draft = await _draft(harness, user, chat, contact, "Когда фикс PAY-142?")
    assert "tasks_unavailable" in draft.output_json["system_warnings"]
    commitments = block(fake_llm.calls_for("compose.draft")[0], "commitments")
    assert [c["what"] for c in commitments] == ["fix on staging"]
    assert "issue_key" not in commitments[0]  # system-side fields never reach the prompt
