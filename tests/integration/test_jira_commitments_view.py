"""/commitments with Jira sources and Reopen (specs/003-jira-commitments T032), and Telegram
promises that name an issue key. Synthetic data only."""

from __future__ import annotations

from datetime import date

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    CommitmentAutoNote,
    CommitmentClosedBy,
    CommitmentSourceKind,
    CommitmentState,
    User,
    UserState,
)
from scribe.db.repos import commitments as commitments_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import CommitmentExtraction, CommitmentItem
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.jira_support import connect_jira
from tests.integration.us1_support import select_all_history
from tests.integration.us2_support import US2Harness, callback_datas
from tests.integration.us3_support import commitments_of, inline_extractor

URL = "https://acme.atlassian.net/browse/PAY-142?focusedCommentId=10002"


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


async def _jira_row(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    clock: FrozenClock,
    *,
    comment_id: str = "10002",
    restricted: bool = False,
    closed: CommitmentAutoNote | None = None,
    note: CommitmentAutoNote | None = None,
) -> str:
    async with session_factory() as session:
        row = await commitments_repo.add_commitment(
            session, user.id, contact_id=None, what="fix on staging", due=date(2026, 10, 21),
            source_sent_at=clock(), confidence=0.9, to_whom="Anna Testova",
            source_message_text=None if restricted else "I'll have the fix on staging",
            source_kind=CommitmentSourceKind.jira_comment, jira_issue_key="PAY-142",
            jira_comment_id=comment_id, jira_comment_url=URL, restricted=restricted,
        )  # fmt: skip
        row.auto_note = note
        if closed is not None:
            await commitments_repo.close(
                session, user.id, row.id, CommitmentState.resolved, at=clock(),
                closed_by=CommitmentClosedBy.system, auto_note=closed,
            )  # fmt: skip
        await session.commit()
        return str(row.id)


def _texts(harness: US2Harness) -> list[str]:
    return [m.text for m in harness.api.of_type(SendMessage)]


async def test_commitments_view_sources_and_restore(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await _jira_row(session_factory, user, clock)
    await _jira_row(
        session_factory, user, clock, comment_id="2", restricted=True,
        note=CommitmentAutoNote.source_unreachable,
    )  # fmt: skip
    closed_id = await _jira_row(
        session_factory, user, clock, comment_id="3", closed=CommitmentAutoNote.issue_done
    )
    clock.advance(days=1)

    await harness.feed(text_update(chat_id, "/commitments"))
    texts = _texts(harness)
    assert "🤝 Open commitments (2):" in texts
    first = next(t for t in texts if "I'll have the fix" in t)
    assert "fix on staging · Anna Testova · due Wed 21 Oct" in first
    assert f"📎 Jira PAY-142 comment: {URL}" in first
    restricted = next(t for t in texts if "(restricted)" in t)
    assert "I'll have" not in restricted  # never quoted
    assert "⚠️ PAY-142 is no longer reachable" in restricted
    assert "Recently closed by Scribe:" in texts
    closed = harness.api.of_type(SendMessage)[-1]
    assert "Closed by Scribe: issue done" in closed.text
    assert callback_datas(closed.reply_markup) == [f"cmtreopen:{closed_id}"]

    await harness.feed(callback_update(chat_id, f"cmtreopen:{closed_id}"))
    assert "↩️ Reopened." in _texts(harness)[-1]
    rows = {str(c.id): c for c in await commitments_of(session_factory, user)}
    assert rows[closed_id].state == CommitmentState.open and rows[closed_id].user_modified


async def test_cmtreopen_rejects_other_users_commitment(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    owner, other = await make_user(state=UserState.active), await make_user(state=UserState.active)
    closed_id = await _jira_row(
        session_factory, owner, clock, closed=CommitmentAutoNote.source_removed
    )
    await harness.feed(callback_update(other.telegram_control_chat_id, f"cmtreopen:{closed_id}"))
    assert harness.api.of_type(AnswerCallbackQuery)[-1].text == "Not found"
    (row,) = await commitments_of(session_factory, owner)
    assert row.state == CommitmentState.resolved


async def test_reopen_window_is_30_days(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    await _jira_row(session_factory, user, clock, closed=CommitmentAutoNote.issue_done)
    clock.advance(days=31)
    await harness.feed(text_update(user.telegram_control_chat_id, "/commitments"))
    assert "Recently closed by Scribe:" not in _texts(harness)


async def test_telegram_message_with_issue_key_sets_issue_key(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Anna Testova")
    chat = await make_chat(user, title="Anna Testova", telegram_chat_id=contact.telegram_peer_id)
    await connect_jira(session_factory, user, "cloud", project_keys=["PAY"])
    text = "PAY-142 fix will be ready by Thursday, and OTHER-1 too"
    fake_llm.set_response(
        "extract.commitments",
        CommitmentExtraction(
            items=[
                CommitmentItem(
                    what="have the PAY-142 fix ready",
                    to_whom="Anna Testova",
                    due=date(2026, 10, 15),
                    confidence=0.9,
                    evidence="fix will be ready by Thursday",
                )  # fmt: skip
            ]
        ),
    )
    await harness.incoming(user, chat, contact, "Когда фикс?")
    await harness.manual_reply(user, chat, text)
    await harness.composer.commitments.drain()
    (row,) = await commitments_of(session_factory, user)
    assert row.jira_issue_key == "PAY-142"
    assert row.source_kind == CommitmentSourceKind.telegram_message
