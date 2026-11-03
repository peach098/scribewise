"""US4 account and isolation (T062): two users never see each other's data in any repo query or
bot view, /delete_my_data cascades and logs out, /my_data counts and exports only the caller's
data, a lost Telegram connection stops processing until it is back, per-chat language and
formality overrides, out of office, and /history without content. Fake LLM, fake gateway,
offline Bot API; synthetic data only."""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import AnswerCallbackQuery, SendDocument
from aiogram.types import BufferedInputFile
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import applications
from scribe.access import repo as access_repo
from scribe.config import Settings
from scribe.db import repos
from scribe.db.base import Base
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    ActivityKind,
    ActivityRecord,
    BetaApplication,
    BetaEntry,
    ChatKind,
    Commitment,
    ConversationTurn,
    Draft,
    DroppedReason,
    ProfileState,
    ProposalState,
    RefusedContact,
    SessionStatus,
    Subscription,
    TelegramSession,
    TurnState,
    User,
    UserState,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.stored_output import (
    EARLIER_KEY,
    StoredEarlier,
    load_stored_output,
    pack_output,
)
from scribe.i18n import format_day_month_year, t
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.retention.user_data import row_dict
from scribe.telegram.control_bot.account_command import log_out_telegram
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import GatewayNotStarted
from tests.conftest import FROZEN_NOW, FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import _block, grounded_reply
from tests.integration.us2_support import US2Harness, callback_datas
from tests.integration.us3_support import connect_trello
from tests.integration.us4_support import add_profile, make_harness, style_output

SECRET_TEXT = "Synthetic secret question about the Q3 numbers"
REPLY_TEXT = "Synthetic reply about Friday"


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("compose.draft", grounded_reply(REPLY_TEXT))
    fake_llm.set_response("profile.build", style_output())
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _seed(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    name: str,
) -> tuple[User, Any, Any]:
    """A user with a chat, a contact, a message turn drafted and sent, a waiting turn, a global
    and a contact profile, a commitment and activity."""
    user = await make_user(state=UserState.active, display_name=f"{name} user")
    contact = await make_contact(user, display_name=f"{name} Contact")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id, title=f"{name} chat")
    await add_profile(session_factory, user, summary=f"{name} global summary.")
    await add_profile(session_factory, user, contact=contact, summary=f"{name} contact summary.")
    async with session_factory() as session:
        await commitments_repo.add_commitment(
            session, user.id, contact_id=contact.id, what=f"{name} promise",
            due=None, source_message_text=f"{name} quote", source_sent_at=clock(),
            confidence=0.9,
        )  # fmt: skip
        await session.commit()
    await harness.incoming(user, chat, contact, f"{SECRET_TEXT} ({name})")
    await harness.tick()
    draft = (await harness.drafts(user))[-1]
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    async with session_factory() as session:
        await users_repo.update_user(session, user.id, delay_min_s=3600, delay_max_s=3600)
        await session.commit()
    await harness.incoming(user, chat, contact, f"Waiting question ({name})")
    return user, chat, contact


# --- isolation ----------------------------------------------------------------------------------

# Cross-user by design: account lookup by the control chat / Telegram account, the boot-time
# client list, the operator CLI and the global job table.
CROSS_USER = {
    ("users", "create_user"),
    ("users", "get_user"),
    ("users", "get_by_control_chat"),
    ("users", "find_user_by_telegram_user_id"),
    ("users", "count_by_state"),
    ("users", "list_user_ids_with_session_status"),
    ("users", "list_users"),
    ("users", "delete_user"),
    ("users", "update_user"),
    ("users", "set_state"),
    # specs/003: the Jira jobs claim due connections across users, list them for the daily
    # check, and the retention purge drops old comment markers for everyone.
    ("tasks_conn", "claim_due_jira"),
    ("tasks_conn", "list_jira_user_ids"),
    ("jira_cursor", "delete_older_than"),
    # Issue #67: the spend entry carries its user id; the operator's cost report spans users.
    ("llm_spend", "add"),
    ("llm_spend", "daily"),
    # Issue #74: the operators' daily spend alert spans users.
    ("llm_spend", "totals_on"),
}


def _repo_modules() -> list[ModuleType]:
    import importlib
    import pkgutil

    return [
        importlib.import_module(f"scribe.db.repos.{m.name}")
        for m in pkgutil.iter_modules(repos.__path__)
        if m.name != "jobs"  # scheduled_jobs is global, not per user
    ]


def test_every_repo_function_is_scoped_by_user() -> None:
    unscoped = [
        f"{module.__name__}.{name}"
        for module in _repo_modules()
        for name, fn in inspect.getmembers(module, inspect.iscoroutinefunction)
        if fn.__module__ == module.__name__
        and "user_id" not in inspect.signature(fn).parameters
        and (module.__name__.rsplit(".", 1)[-1], name) not in CROSS_USER
    ]
    assert unscoped == []


async def test_two_users_are_isolated(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    alice, alice_chat, alice_contact = await _seed(
        harness, session_factory, clock, make_user, make_chat, make_contact, "Alice"
    )
    bob, bob_chat, bob_contact = await _seed(
        harness, session_factory, clock, make_user, make_chat, make_contact, "Bob"
    )
    harness.api.clear()

    # Every view of Alice shows Alice's data only.
    control = alice.telegram_control_chat_id
    for command in ("/chats", "/waiting", "/history", "/profile", "/commitments", "/my_data"):
        await harness.feed(text_update(control, command))
    await harness.feed(callback_update(control, "profcontacts:0"))
    await harness.feed(callback_update(control, f"chatcfg:{alice_chat.id}"))
    await harness.feed(callback_update(control, "mydata:export"))
    texts = "\n".join(m.text or "" for m in harness.messages())
    assert "Alice" in texts
    assert "Bob" not in texts
    (document,) = harness.api.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    assert int(document.chat_id) == control
    exported = document.document.data.decode()
    assert "Alice" in exported and "Bob" not in exported and str(bob.id) not in exported

    # Every button naming one of Bob's rows does nothing for Alice.
    async with session_factory() as session:
        bob_draft = (await harness.drafts(bob))[-1]
        bob_turn = (await harness.turns(bob))[-1]
        bob_profile = await session.scalar(
            text("SELECT id FROM style_profiles WHERE user_id = :u AND scope = 'contact'"),
            {"u": bob.id},
        )
        (bob_commitment,) = await commitments_repo.list_open(session, bob.id)
        bob_record = (await activity_repo.list_recent(session, bob.id, limit=1))[0]
        bob_proposal = await profiles_repo.create_proposal(
            session, bob.id, style_profile_id=bob_profile,
            rule_json={"key": "emoji_removed", "text": "no emojis"}, evidence_draft_ids=[],
        )  # fmt: skip
        await session.commit()
    foreign = [
        f"approve:{bob_draft.id}",
        f"edit:{bob_draft.id}",
        f"regen:{bob_draft.id}",
        f"reject:{bob_draft.id}",
        f"sources:{bob_draft.id}",
        f"readnow:{bob_turn.id}",
        f"pin:{bob_profile}",
        f"unpin:{bob_profile}",
        f"profview:{bob_profile}",
        f"removeprofile:{bob_profile}",
        f"profrules:{bob_profile}",
        f"undo_refresh:{bob_record.id}",
        f"rule_accept:{uuid.uuid4()}",
        f"rule_accept:{bob_proposal.id}",
        f"rule_dismiss:{bob_proposal.id}",
        f"cmtres:{bob_commitment.id}",
        f"cmtedit:{bob_commitment.id}",
        f"cmtdel:{bob_commitment.id}",
        f"chatcfg:{bob_chat.id}",
        f"chatdelay:{bob_chat.id}",
        f"chatdelay:{bob_chat.id}:clear",
        f"chatlang:{bob_chat.id}:clear",
        f"chatformal:{bob_chat.id}:5",
        f"chatgroup:{bob_chat.id}:always",
        f"chatenable:{bob_chat.id}:off",
    ]
    harness.api.clear()
    for data in foreign:
        await harness.feed(callback_update(control, data))
    assert all("Bob" not in t for t in harness.bot_texts())
    # The ownership middleware stopped every one of them before its handler.
    refused = log_capture.events("callback_refused_not_owner")
    assert len(refused) == len(foreign)
    assert {e["user_id"] for e in refused} == {str(alice.id)}
    async with session_factory() as session:
        stored_chat = await chats_repo.get_work_chat(session, bob.id, bob_chat.id)
        stored_contact = await chats_repo.get_contact(session, bob.id, bob_contact.id)
        (still_open,) = await commitments_repo.list_open(session, bob.id)
    assert (
        stored_chat is not None and stored_chat.enabled and stored_chat.override_formality is None
    )
    assert stored_contact is not None and not stored_contact.pinned_profile
    assert stored_chat.override_delay_min_s is None
    assert still_open.id == bob_commitment.id
    assert still_open.what == "Bob promise"
    assert (await harness.drafts(bob))[-1].state == bob_draft.state
    async with session_factory() as session:
        proposal = await profiles_repo.get_proposal(session, bob.id, bob_proposal.id)
        bob_current = await profiles_repo.get_profile(session, bob.id, bob_profile)
    assert proposal is not None and proposal.state == ProposalState.proposed
    assert bob_current is not None and bob_current.state == ProfileState.current
    assert (await harness.turns(bob))[-1].due_at == bob_turn.due_at
    assert alice_contact.id != bob_contact.id


# --- /delete_my_data ----------------------------------------------------------------------------


async def _counts(session_factory: async_sessionmaker[AsyncSession], user: User) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with session_factory() as session:
        for table in Base.metadata.sorted_tables:
            if "user_id" in table.c:
                result = await session.execute(
                    select(func.count()).select_from(table).where(table.c.user_id == user.id)
                )
                counts[table.name] = int(result.scalar_one())
        counts["users"] = int(
            (
                await session.execute(
                    select(func.count()).select_from(User).where(User.id == user.id)
                )
            ).scalar_one()
        )
        counts["bot_fsm_state"] = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM bot_fsm_state WHERE chat_id = :c"),
                    {"c": user.telegram_control_chat_id},
                )
            ).scalar_one()
        )
    return counts


async def test_delete_my_data_cascades_and_logs_out(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    monkeypatch: pytest.MonkeyPatch,
    log_capture: LogCapture,
) -> None:
    alice, _, _ = await _seed(
        harness, session_factory, clock, make_user, make_chat, make_contact, "Alice"
    )
    bob, _, _ = await _seed(
        harness, session_factory, clock, make_user, make_chat, make_contact, "Bob"
    )
    await connect_trello(session_factory, alice)
    await gateway.start(alice.id)
    logged_out: list[uuid.UUID] = []

    async def log_out(user_id: uuid.UUID) -> None:  # 002's MessagingGateway.log_out
        logged_out.append(user_id)

    monkeypatch.setattr(gateway, "log_out", log_out, raising=False)
    before = await _counts(session_factory, alice)
    assert all(before[t] > 0 for t in ("messages", "drafts", "style_profiles", "commitments"))
    bob_before = await _counts(session_factory, bob)
    control = alice.telegram_control_chat_id

    # Step 1 explains; anything but DELETE cancels.
    await harness.feed(text_update(control, "/delete_my_data"))
    assert "Type DELETE to confirm" in harness.bot_texts()[-1]
    await harness.feed(text_update(control, "delete"))
    assert harness.bot_texts()[-1] == "Not deleted. Nothing was changed."
    # Another command in between cancels too.
    await harness.feed(text_update(control, "/delete_my_data"))
    await harness.feed(text_update(control, "/waiting"))
    await harness.feed(text_update(control, "DELETE"))
    assert logged_out == [] and (await _counts(session_factory, alice)) == before

    await harness.feed(text_update(control, "/delete_my_data"))
    await harness.feed(text_update(control, "DELETE"))

    assert harness.bot_texts()[-1].startswith("🗑 Done. All your data was deleted")
    assert logged_out == [alice.id]
    assert alice.id not in gateway.started  # the client is stopped
    after = await _counts(session_factory, alice)
    assert set(after.values()) == {0}, after
    assert await _counts(session_factory, bob) == bob_before  # nobody else is touched
    deleted = log_capture.events("data_deleted")
    assert len(deleted) == 1 and deleted[0]["user_id"] == str(alice.id)
    assert SECRET_TEXT not in log_capture.raw


async def test_delete_my_data_log_out_branches(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    log_capture: LogCapture,
) -> None:
    user = await make_user(state=UserState.active)

    class NoLogOut:  # a gateway without 002's log_out (before the merge)
        async def stop(self, user_id: uuid.UUID) -> None:
            return None

    await log_out_telegram(NoLogOut(), user)  # type: ignore[arg-type]
    assert [e["user_id"] for e in log_capture.events("telegram_log_out_unavailable")] == [
        str(user.id)
    ]

    # Telegram unreachable: nothing is deleted, the user is told to try again.
    async def failing(user_id: uuid.UUID) -> None:
        raise ConnectionError("synthetic")

    monkeypatch.setattr(gateway, "log_out", failing, raising=False)
    await harness.feed(text_update(user.telegram_control_chat_id, "/delete_my_data"))
    await harness.feed(text_update(user.telegram_control_chat_id, "DELETE"))
    assert "nothing was deleted yet" in harness.bot_texts()[-1]
    async with session_factory() as session:
        assert await users_repo.get_user(session, user.id) is not None


# --- /delete_my_data and the access gate (specs/005 T017) ----------------------------------------

ACCESS_ID = 900000011


async def _access_rows(session_factory: async_sessionmaker[AsyncSession], telegram_id: int) -> int:
    async with session_factory() as session:
        total = 0
        for model in (BetaEntry, Subscription, AccessEvent, RefusedContact):
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.telegram_id == telegram_id)
            )
            total += int(count or 0)
        return total


async def _grant(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    *,
    beta: bool = False,
    sub: tuple[datetime, datetime] | None = None,
) -> None:
    async with session_factory() as session:
        if beta:
            session.add(
                BetaEntry(telegram_id=telegram_id, added_by="operator", added_at=FROZEN_NOW)
            )
            await access_repo.record_event(
                session, telegram_id, AccessEventKind.beta_added, actor="operator", now=FROZEN_NOW
            )
        if sub is not None:
            session.add(
                Subscription(
                    telegram_id=telegram_id, starts_at=sub[0], ends_at=sub[1], granted_by="operator"
                )
            )
        await session.commit()


async def test_delete_my_data_removes_access_records_and_gate_refuses_after(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    log_capture: LogCapture,
) -> None:
    gated = settings.model_copy(update={"access_gate": True})
    harness = make_harness(session_factory, gated, llm_router, fake_llm, gateway, clock)
    user = await make_user(control_chat_id=ACCESS_ID, state=UserState.active)
    ends_at = FROZEN_NOW + timedelta(days=20)
    await _grant(
        session_factory, ACCESS_ID, beta=True, sub=(FROZEN_NOW - timedelta(days=10), ends_at)
    )
    async with session_factory() as session:
        await access_repo.touch_refused(session, ACCESS_ID, now=FROZEN_NOW, cooldown_s=3600)
        await session.commit()
    other_rows = await _access_rows(session_factory, ACCESS_ID + 1)
    assert await _access_rows(session_factory, ACCESS_ID) == 4

    await harness.feed(text_update(ACCESS_ID, "/delete_my_data"))
    day = format_day_month_year(ends_at.astimezone(ZoneInfo(user.timezone)), "en")
    warning = t("access.delete_warning", what=t("access.delete_what_both", date=day))
    assert warning in harness.bot_texts()[-1]
    await harness.feed(text_update(ACCESS_ID, "DELETE"))
    assert harness.bot_texts()[-1] == t("delete.done")

    assert await _access_rows(session_factory, ACCESS_ID) == 0
    assert await _access_rows(session_factory, ACCESS_ID + 1) == other_rows
    (logged,) = log_capture.events("access_records_deleted")
    assert (logged["beta_entries"], logged["subscriptions"]) == (1, 1)
    assert (logged["access_events"], logged["refused_contacts"]) == (1, 1)
    assert str(ACCESS_ID) not in json.dumps(logged)

    # A stranger to the gate again.
    await harness.feed(text_update(ACCESS_ID, "/start"))
    assert harness.bot_texts()[-1] == t("access.refused")
    async with session_factory() as session:
        assert await users_repo.get_by_control_chat(session, ACCESS_ID) is None


async def test_delete_warning_only_when_there_is_access_to_lose(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    async def ask(telegram_id: int) -> str:
        await harness.feed(text_update(telegram_id, "/delete_my_data"))
        text_ = harness.bot_texts()[-1]
        await harness.feed(text_update(telegram_id, "/cancel"))
        return text_

    nothing = await make_user(control_chat_id=ACCESS_ID, state=UserState.active)
    assert await ask(nothing.telegram_control_chat_id) == t("delete.ask", word="DELETE")

    beta_only = await make_user(control_chat_id=ACCESS_ID + 1, state=UserState.active)
    await _grant(session_factory, ACCESS_ID + 1, beta=True)
    assert t("access.delete_warning", what=t("access.delete_what_beta")) in await ask(
        beta_only.telegram_control_chat_id
    )

    # A scheduled subscription counts; an ended or cancelled one does not.
    scheduled = await make_user(control_chat_id=ACCESS_ID + 2, state=UserState.active)
    starts, ends = FROZEN_NOW + timedelta(days=5), FROZEN_NOW + timedelta(days=35)
    await _grant(session_factory, ACCESS_ID + 2, sub=(starts, ends))
    day = format_day_month_year(ends.astimezone(ZoneInfo(scheduled.timezone)), "en")
    assert t(
        "access.delete_warning", what=t("access.delete_what_subscription", date=day)
    ) in await ask(scheduled.telegram_control_chat_id)

    ended = await make_user(control_chat_id=ACCESS_ID + 3, state=UserState.active)
    await _grant(
        session_factory,
        ACCESS_ID + 3,
        sub=(FROZEN_NOW - timedelta(days=40), FROZEN_NOW - timedelta(days=10)),
    )
    assert await ask(ended.telegram_control_chat_id) == t("delete.ask", word="DELETE")


# --- /my_data -----------------------------------------------------------------------------------


async def test_my_data_counts_and_export_own_data_only(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    log_capture: LogCapture,
) -> None:
    alice, _, _ = await _seed(
        harness, session_factory, clock, make_user, make_chat, make_contact, "Alice"
    )
    bob, _, _ = await _seed(
        harness, session_factory, clock, make_user, make_chat, make_contact, "Bob"
    )
    await connect_trello(session_factory, alice)
    control = alice.telegram_control_chat_id
    harness.api.clear()

    await harness.feed(text_update(control, "/my_data"))
    summary = harness.messages()[-1]
    assert summary.text.startswith("📦 What I store about you")
    assert "💬 Messages: 3 (oldest 14 Oct 2026; each is deleted 30 days after it was sent" in (
        summary.text
    )
    assert "next removal 13 Nov 2026" in summary.text
    assert "✍️ Drafts: 1" in summary.text
    assert "🧑 Style profiles: 2 (2 current, 0 previous, 0 retired" in summary.text
    assert "🤝 Commitments: 1 open, 0 closed" in summary.text
    assert "🗂 Task source: trello (active)" in summary.text
    assert callback_datas(summary.reply_markup) == ["mydata:export"]

    await harness.feed(callback_update(control, "mydata:export"))
    (document,) = harness.api.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    export = json.loads(document.document.data)
    assert {r["id"] for r in export["users"]} == {str(alice.id)}
    for table, rows in export.items():
        if isinstance(rows, list) and rows and "user_id" in rows[0]:
            assert {r["user_id"] for r in rows} == {str(alice.id)}, table
    texts = [m["text"] for m in export["messages"]]
    assert f"{SECRET_TEXT} (Alice)" in texts  # decrypted
    assert REPLY_TEXT in texts  # the sent reply, stored as the user's message
    (draft,) = export["drafts"]
    assert draft["text"] == REPLY_TEXT and draft["output_json"]["output"]["text"] == REPLY_TEXT
    assert draft["output_json"]["output"]["sources"]  # decrypted content, not the "enc" blob
    assert {p["summary_text"] for p in export["style_profiles"]} == {
        "Alice global summary.",
        "Alice contact summary.",
    }
    assert export["commitments"][0]["what"] == "Alice promise"
    (connection,) = export["task_source_connections"]
    assert "credentials" not in connection and connection["provider"] == "trello"
    raw = document.document.data.decode()
    assert "synthetic-trello-token" not in raw and "Bob" not in raw
    # The Telegram session row is exported without its session string.
    row = row_dict(
        TelegramSession(
            user_id=alice.id, session_string="synthetic-secret", status=SessionStatus.active
        )
    )
    assert "session_string" not in row and "session_token" not in row
    assert row["status"] == "active"
    async with session_factory() as session:
        (record,) = (
            (
                await session.execute(
                    select(ActivityRecord).where(
                        ActivityRecord.user_id == alice.id,
                        ActivityRecord.kind == ActivityKind.data_exported,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert record.meta_json["rows"]["messages"] == 3
    assert SECRET_TEXT not in log_capture.raw and "Alice promise" not in log_capture.raw
    assert bob.id != alice.id


EARLIER_EXCERPT = "Synthetic earlier agreement: invoice 42 is paid on Friday"


async def test_export_and_delete_cover_cited_earlier_messages(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """specs/004 T031: cited earlier messages are exported decrypted inside their draft and are
    deleted with it by /delete_my_data."""
    alice, chat, _ = await _seed(
        harness, session_factory, clock, make_user, make_chat, make_contact, "Alice"
    )
    sent_at = clock() - timedelta(days=90)
    async with session_factory() as session:
        draft = (await session.execute(select(Draft).where(Draft.user_id == alice.id))).scalar_one()
        stored = load_stored_output(draft.output_json, draft.text)
        assert stored.output is not None
        draft.output_json = pack_output(
            stored.output,
            stored.system_warnings,
            stored.reply_to_message_id,
            earlier_messages=[
                StoredEarlier(
                    ref="earlier:1",
                    chat_id=chat.telegram_chat_id,
                    message_id=777,
                    sent_at=sent_at,
                    chat_title="Alice chat",
                    sender="Alice Contact",
                    excerpt=EARLIER_EXCERPT,
                )
            ],
        )
        await session.commit()
    async with session_factory() as session:
        raw = json.dumps(
            await session.scalar(select(Draft.output_json).where(Draft.id == draft.id))
        )
    assert EARLIER_EXCERPT not in raw and "Alice Contact" not in raw  # encrypted at rest

    control = alice.telegram_control_chat_id
    harness.api.clear()
    await harness.feed(callback_update(control, "mydata:export"))
    (document,) = harness.api.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    (exported,) = json.loads(document.document.data)["drafts"]
    assert exported["output_json"]["earlier_messages"] == [
        {
            "ref": "earlier:1",
            "chat_id": chat.telegram_chat_id,
            "message_id": 777,
            "sent_at": sent_at.isoformat(),
            "chat_title": "Alice chat",
            "sender": "Alice Contact",
            "excerpt": EARLIER_EXCERPT,
        }
    ]

    async def log_out(user_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(gateway, "log_out", log_out, raising=False)
    await harness.feed(text_update(control, "/delete_my_data"))
    await harness.feed(text_update(control, "DELETE"))
    assert harness.bot_texts()[-1].startswith("🗑 Done. All your data was deleted")
    async with session_factory() as session:
        left = await session.scalar(
            select(func.count()).select_from(Draft).where(Draft.output_json.has_key(EARLIER_KEY))
        )
    assert left == 0


async def test_my_data_access_section_and_export(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    """specs/005 T032: the Access section and ``access.*`` in the export; ids and dates only,
    no operator reason or note; events of other people and unlinked entries are left out."""
    telegram_id, other_id = ACCESS_ID + 20, ACCESS_ID + 21
    user = await make_user(control_chat_id=telegram_id, state=UserState.active)
    tz = ZoneInfo(user.timezone)
    beta_since = FROZEN_NOW - timedelta(days=40)
    active = (FROZEN_NOW - timedelta(days=10), FROZEN_NOW + timedelta(days=20))
    ended = (FROZEN_NOW - timedelta(days=60), FROZEN_NOW - timedelta(days=30))
    cancelled = (FROZEN_NOW - timedelta(days=5), FROZEN_NOW + timedelta(days=5))
    async with session_factory() as session:
        beta = BetaEntry(
            telegram_id=telegram_id,
            added_by="operator",
            added_at=beta_since,
            note="Synthetic operator note",
        )
        session.add(beta)
        for starts, ends in (ended, active, cancelled):
            session.add(
                Subscription(
                    telegram_id=telegram_id,
                    starts_at=starts,
                    ends_at=ends,
                    granted_by="operator",
                    note="Synthetic payment ref",
                    cancelled_at=FROZEN_NOW - timedelta(days=1) if starts == cancelled[0] else None,
                )
            )
        await session.flush()
        await access_repo.record_event(
            session, telegram_id, AccessEventKind.beta_added, actor="operator",
            meta={"beta_entry_id": beta.id}, now=beta_since,
        )  # fmt: skip
        await access_repo.record_event(
            session, telegram_id, AccessEventKind.subscription_granted, actor="operator",
            reason="Synthetic operator reason", meta={"ends_at": active[1]}, now=active[0],
        )  # fmt: skip
        # Not this person's: an unlinked username-only entry's event and someone else's event.
        await access_repo.record_event(
            session, access_repo.UNLINKED_TELEGRAM_ID, AccessEventKind.beta_added,
            actor="operator", meta={"beta_entry_id": uuid.uuid4()}, now=FROZEN_NOW,
        )  # fmt: skip
        await access_repo.record_event(
            session, other_id, AccessEventKind.beta_added, actor="operator", now=FROZEN_NOW
        )
        await session.commit()

    def day(moment: datetime) -> str:
        return format_day_month_year(moment.astimezone(tz), "en")

    await harness.feed(text_update(telegram_id, "/my_data"))
    summary = harness.messages()[-1].text or ""
    status = t("access.mydata_status_beta_and_subscribed")
    assert t("access.mydata_title", status=status) in summary
    assert t("access.mydata_beta_since", day=day(beta_since)) in summary
    for (starts, ends), state in ((ended, "ended"), (active, "active"), (cancelled, "cancelled")):
        line = t(
            "access.mydata_subscription",
            start=day(starts),
            end=day(ends),
            state=t(f"access.mydata_sub_{state}"),
        )
        assert line in summary
    assert t("access.mydata_events", n=2) in summary
    assert summary.index(t("access.mydata_title", status=status)) < summary.index(
        t("my_data.footer")
    )

    await harness.feed(callback_update(telegram_id, "mydata:export"))
    (document,) = harness.api.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    export = json.loads(document.document.data)
    access = export["access"]
    assert access["beta"] == [
        {
            "id": str(beta.id),
            "telegram_id": telegram_id,
            "added_at": beta_since.isoformat(),
            "linked_at": None,
        }
    ]
    assert [(s["starts_at"], s["state"]) for s in access["subscriptions"]] == [
        (ended[0].isoformat(), "ended"),
        (active[0].isoformat(), "active"),
        (cancelled[0].isoformat(), "cancelled"),
    ]
    assert set(access["subscriptions"][0]) == {
        "id", "starts_at", "ends_at", "cancelled_at", "reminded_at", "created_at", "state",
    }  # fmt: skip
    assert [e["kind"] for e in access["events"]] == ["beta_added", "subscription_granted"]
    assert set(access["events"][1]) == {"id", "kind", "actor", "at", "meta"}
    assert access["events"][1]["meta"] == {"ends_at": active[1].isoformat()}
    raw = document.document.data.decode()
    for secret in ("Synthetic operator reason", "Synthetic operator note", "Synthetic payment ref"):
        assert secret not in raw
    assert str(other_id) not in raw


async def test_my_data_access_section_without_access(
    harness: US2Harness, make_user: MakeUser
) -> None:
    user = await make_user(control_chat_id=ACCESS_ID + 22, state=UserState.active)
    await harness.feed(text_update(user.telegram_control_chat_id, "/my_data"))
    summary = harness.messages()[-1].text or ""
    assert t("access.mydata_title", status=t("access.mydata_status_none")) in summary
    assert t("access.mydata_events", n=0) in summary
    assert "Beta since" not in summary

    await harness.feed(callback_update(user.telegram_control_chat_id, "mydata:export"))
    (document,) = harness.api.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    export = json.loads(document.document.data)
    assert export["access"] == {"beta": [], "subscriptions": [], "events": [], "applications": []}


# --- Telegram connection ------------------------------------------------------------------------


async def test_telegram_disconnect_stops_processing_and_reconnect_resumes(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """Disconnect / reconnect themselves are ``/telegram`` (specs/002-telegram-qr-login); here:
    while the user's client is not running nothing is read or drafted, and the waiting message
    is processed as soon as it runs again."""
    user = await make_user(state=UserState.active)
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)
    await gateway.stop(user.id)
    gateway.fail_mark_read_with = GatewayNotStarted("synthetic: not connected")
    await harness.incoming(user, chat, contact, "Synthetic question")

    await harness.tick()
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.waiting and turn.read_at is None
    assert gateway.mark_read_calls == [] and await harness.drafts(user) == []

    await gateway.start(user.id)
    gateway.fail_mark_read_with = None
    clock.advance(seconds=61)
    await harness.tick()
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.pending_review
    assert len(gateway.mark_read_calls) == 1 and len(await harness.drafts(user)) == 1


# --- per-chat overrides -------------------------------------------------------------------------


async def test_per_chat_override_language_and_formality(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    contact = await make_contact(user, display_name="Synthetic Teammate")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id, title="Teammate")
    await add_profile(session_factory, user, contact=contact, formality=1, languages=["ru"])

    await harness.feed(callback_update(control, f"chatcfg:{chat.id}"))
    screen = harness.messages()[-1]
    assert "🌐 Language: the language of the incoming message" in screen.text
    assert f"chatlang:{chat.id}" in callback_datas(screen.reply_markup)
    assert f"chatformal:{chat.id}:5" in callback_datas(screen.reply_markup)
    await harness.feed(callback_update(control, f"chatlang:{chat.id}"))
    await harness.feed(text_update(control, "not a language!"))
    assert "Send a language code" in harness.bot_texts()[-1]
    await harness.feed(text_update(control, "English"))
    await harness.feed(callback_update(control, f"chatformal:{chat.id}:5"))
    assert "🎩 Formality: 5/5" in harness.messages()[-1].text
    assert "🌐 Language: en" in harness.messages()[-1].text

    await harness.incoming(user, chat, contact, "Привет, как дела с отчётом?")
    await harness.tick()
    call = fake_llm.calls_for("compose.draft")[-1]
    assert _block(call, "chat_overrides") == {"language": "en", "formality": 5}
    assert _block(call, "profile")["rules_json"]["formality"] == 1  # the profile is still sent

    # pt-BR is normalised; ↩️ clears; group mode only on group chats.
    await harness.feed(callback_update(control, f"chatlang:{chat.id}"))
    await harness.feed(text_update(control, "pt_br"))
    async with session_factory() as session:
        stored = await chats_repo.get_work_chat(session, user.id, chat.id)
    assert stored is not None and stored.override_language == "pt-BR"
    await harness.feed(callback_update(control, f"chatformal:{chat.id}:clear"))
    await harness.feed(callback_update(control, f"chatlang:{chat.id}:clear"))
    async with session_factory() as session:
        stored = await chats_repo.get_work_chat(session, user.id, chat.id)
    assert stored is not None and stored.override_language is None
    assert stored.override_formality is None
    assert not any(
        d.startswith("chatgroup") for d in callback_datas(harness.messages()[-1].reply_markup)
    )

    # 006: groups state the fixed rule and have no group-mode buttons; stale buttons only
    # answer "no longer available" and change nothing.
    group = await make_chat(user, kind=ChatKind.group, title="Synthetic Group")
    await harness.feed(callback_update(control, f"chatcfg:{group.id}"))
    screen = harness.messages()[-1]
    assert "👥 Drafts in this group: only when you are tagged or replied to" in screen.text
    assert not any(d.startswith("chatgroup") for d in callback_datas(screen.reply_markup))
    async with session_factory() as session:
        before = await chats_repo.get_work_chat(session, user.id, group.id)
        assert before is not None
        snapshot = (before.enabled, before.override_formality, before.updated_at)
    for mode in ("always", "mentions_only"):
        harness.api.clear()
        await harness.feed(callback_update(control, f"chatgroup:{group.id}:{mode}"))
        assert harness.messages() == [] and harness.edits() == []
        (answer,) = harness.api.of_type(AnswerCallbackQuery)
        assert answer.text == t("alert.option_removed", "en")
    async with session_factory() as session:
        after = await chats_repo.get_work_chat(session, user.id, group.id)
    assert after is not None
    assert (after.enabled, after.override_formality, after.updated_at) == snapshot

    # Disabling a chat drops its waiting messages; /chats still lists it.
    await harness.feed(callback_update(control, f"reject:{(await harness.drafts(user))[-1].id}"))
    async with session_factory() as session:
        await users_repo.update_user(session, user.id, delay_min_s=600, delay_max_s=600)
        await session.commit()
    await harness.incoming(user, chat, contact, "Synthetic waiting")
    await harness.feed(callback_update(control, f"chatenable:{chat.id}:off"))
    waiting = [t for t in await harness.turns(user) if t.dropped_reason is not None]
    assert [t.dropped_reason for t in waiting] == [DroppedReason.chat_disabled]
    await harness.feed(text_update(control, "/chats"))
    assert "⏸ Teammate" in str(harness.messages()[-1].reply_markup)


# --- out of office ------------------------------------------------------------------------------


async def test_ooo_state_produces_out_of_office_draft(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    contact = await make_contact(user)
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id)

    async def stored_user() -> User:
        async with session_factory() as session:
            found = await users_repo.get_user(session, user.id)
            assert found is not None
            return found

    await harness.feed(text_update(control, "/ooo"))
    assert "Out of office is off. To turn it on" in harness.bot_texts()[-1]
    await harness.feed(text_update(control, "/ooo On vacation until 20 Oct"))
    assert harness.bot_texts()[-1].startswith("🏖 Out of office is on.")
    current = await stored_user()
    assert (current.state, current.ooo_message) == (
        UserState.out_of_office,
        "On vacation until 20 Oct",
    )

    await harness.incoming(user, chat, contact, "Synthetic question")
    await harness.tick()  # out-of-office users are still read and drafted
    call = fake_llm.calls_for("compose.draft")[-1]
    assert _block(call, "user_state") == {"ooo": "On vacation until 20 Oct"}
    draft = (await harness.drafts(user))[-1]
    assert "out_of_office" in draft.output_json["system_warnings"]
    assert "out-of-office reply" in harness.messages()[-1].text

    # /pause keeps the out-of-office text, /resume restores out of office (Phase 4 invariant).
    await harness.feed(text_update(control, "/pause"))
    current = await stored_user()
    assert (current.state, current.ooo_message) == (UserState.paused, "On vacation until 20 Oct")
    await harness.feed(text_update(control, "/resume"))
    current = await stored_user()
    assert (current.state, current.ooo_message) == (
        UserState.out_of_office,
        "On vacation until 20 Oct",
    )
    # /ooo while paused changes the text and keeps the pause; /ooo alone turns it off.
    await harness.feed(text_update(control, "/pause"))
    await harness.feed(text_update(control, "/ooo Back on Monday"))
    current = await stored_user()
    assert (current.state, current.ooo_message) == (UserState.paused, "Back on Monday")
    await harness.feed(text_update(control, "/ooo"))
    current = await stored_user()
    assert (current.state, current.ooo_message) == (UserState.paused, None)
    await harness.feed(text_update(control, "/resume"))
    assert (await stored_user()).state == UserState.active

    await harness.feed(callback_update(control, f"reject:{draft.id}"))
    await harness.incoming(user, chat, contact, "Synthetic follow-up")
    await harness.tick()
    assert _block(fake_llm.calls_for("compose.draft")[-1], "user_state") == {"ooo": None}


# --- /history -----------------------------------------------------------------------------------


async def test_history_shows_no_content(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    alice, _, _ = await _seed(
        harness, session_factory, clock, make_user, make_chat, make_contact, "Alice"
    )
    await harness.feed(text_update(alice.telegram_control_chat_id, "/history"))
    history = harness.bot_texts()[-1]
    assert history.startswith("🧾 Recent activity (last 3, newest first; no message content):")
    assert "Wed 14 Oct 10:00 · ✅ Sent · Alice Contact" in history
    assert "👍 Approved by you · Alice Contact" in history
    assert "✍️ Draft prepared · Alice Contact" in history
    for content in (SECRET_TEXT, REPLY_TEXT, "Alice promise", "Alice quote"):
        assert content not in history
    async with session_factory() as session:
        turns = (
            (
                await session.execute(
                    select(ConversationTurn).where(ConversationTurn.user_id == alice.id)
                )
            )
            .scalars()
            .all()
        )
        commitments = (
            (await session.execute(select(Commitment).where(Commitment.user_id == alice.id)))
            .scalars()
            .all()
        )
    assert len(turns) == 2 and len(commitments) == 1


async def test_beta_applications_in_my_data_export_and_deletion(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    """specs/005 FR-029: /my_data lists the person's applications with status and dates, the
    export holds the use case, and /delete_my_data deletes them; other people's stay."""
    telegram_id = ACCESS_ID + 30
    user = await make_user(control_chat_id=telegram_id, state=UserState.active)
    tz = ZoneInfo(user.timezone)
    use_case = "Synthetic use case for the beta application"
    submitted_at, decided_at = FROZEN_NOW - timedelta(days=5), FROZEN_NOW - timedelta(days=4)
    async with session_factory() as session:
        rejected = await applications.submit(
            session, telegram_id=telegram_id, username="synthetic_applicant",
            use_case=use_case, language="en", now=submitted_at - timedelta(days=40),
        )  # fmt: skip
        await applications.decide(
            session, rejected.id, approve=False, operator_id=900000099, now=decided_at
        )
        await applications.submit(
            session, telegram_id=telegram_id, username=None, use_case=use_case,
            language="en", now=submitted_at,
        )  # fmt: skip
        await applications.submit(
            session, telegram_id=telegram_id + 1, username=None, use_case="Someone else's",
            language="en", now=submitted_at,
        )  # fmt: skip
        await session.commit()

    def day(moment: datetime) -> str:
        return format_day_month_year(moment.astimezone(tz), "en")

    await harness.feed(text_update(telegram_id, "/my_data"))
    summary = harness.messages()[-1].text or ""
    assert (
        t(
            "access.mydata_application",
            day=day(submitted_at - timedelta(days=40)),
            status=t("access.mydata_app_rejected", day=day(decided_at)),
        )
        in summary
    )
    assert (
        t(
            "access.mydata_application",
            day=day(submitted_at),
            status=t("access.mydata_app_pending"),
        )
        in summary
    )
    assert use_case not in summary

    await harness.feed(callback_update(telegram_id, "mydata:export"))
    (document,) = harness.api.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    exported = json.loads(document.document.data)["access"]["applications"]
    assert [a["status"] for a in exported] == ["rejected", "pending"]
    assert exported[0]["use_case"] == use_case
    assert exported[0]["username"] == "synthetic_applicant"
    assert "decided_by" not in exported[0]
    assert "Someone else's" not in document.document.data.decode()

    await harness.feed(text_update(telegram_id, "/delete_my_data"))
    await harness.feed(text_update(telegram_id, "DELETE"))
    assert harness.bot_texts()[-1] == t("delete.done")
    async with session_factory() as session:
        left = (await session.scalars(select(BetaApplication.telegram_id))).all()
    assert list(left) == [telegram_id + 1]
