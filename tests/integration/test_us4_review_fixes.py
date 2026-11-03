"""Phase 6 review fixes (US4): refresh counting, serialized profile builds, 🗑 Remove surviving a
rebuild, undo after a rebuild, legacy plaintext re-encryption, disabling a chat while it is being
read, retention of ``previous`` profiles, the rules input ending on other commands, and quiet
handling of a deleted account. Fake LLM, fake gateway, offline Bot API; synthetic data only."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import timedelta

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Contact,
    ContactKind,
    ConversationTurn,
    DraftState,
    DroppedReason,
    ProfileScope,
    ProfileState,
    StyleProfile,
    TurnState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.composer import ComposeError
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.retention.purge import purge_messages
from scribe.style.profiles import (
    build_global_profile,
    refresh_profiles,
    rule_texts,
    undo_refresh,
)
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage
from tests.conftest import (
    FROZEN_NOW,
    FrozenClock,
    LogCapture,
    MakeChat,
    MakeContact,
    MakeUser,
    next_telegram_id,
)
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, next_message_id, select_all_history
from tests.integration.us2_support import US2Harness
from tests.integration.us4_support import (
    add_own_messages,
    add_profile,
    by_scope,
    make_harness,
    profile,
    profiles_of,
    style_output,
)

CIPHERTEXT = re.compile(r"^e2:[^:]+:[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$")


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
    fake_llm.set_response("profile.build", style_output())
    return make_harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _contact(session_factory: async_sessionmaker[AsyncSession], contact: Contact) -> Contact:
    async with session_factory() as session:
        found = await chats_repo.get_contact(session, contact.user_id, contact.id)
        assert found is not None
        return found


def _own(user: User, chat_id: int, n: int, sent_at: object) -> IncomingMessage:
    return IncomingMessage(
        user_id=user.id, chat_id=chat_id, message_id=next_message_id(), sender_peer_id=0,
        sender_display_name="me", text=f"Synthetic own text {n}", has_media=False,
        sent_at=sent_at, is_outgoing=True,  # type: ignore[arg-type]
    )  # fmt: skip


def _answers(harness: US2Harness) -> list[str]:
    return [a.text or "" for a in harness.api.of_type(AnswerCallbackQuery)]


# --- finding 1: no-op refreshes never count the same messages twice ----------------------------


async def test_noop_refreshes_do_not_double_count(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    ivan = await make_contact(user, display_name="Ivan")
    chat = await make_chat(user, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    await add_profile(session_factory, user)
    fake_llm.set_response("profile.build", style_output())
    clock.advance(days=1)
    await add_own_messages(session_factory, user, chat, 4, at=clock() - timedelta(hours=1))

    counts = []
    for _ in range(3):
        result = await refresh_profiles(
            session_factory, llm_router, user.id, settings=settings, clock=clock
        )
        assert not result.changed  # 4 messages: nothing to rebuild or retire
        counts.append((await _contact(session_factory, ivan)).own_message_count_6m)
        clock.advance(days=7)
    assert counts == [4, 4, 4]
    assert fake_llm.calls_for("profile.build") == []
    # Every run is recorded (the "since" mark), none of them is undoable.
    async with session_factory() as session:
        records = (
            (
                await session.execute(
                    select(ActivityRecord).where(
                        ActivityRecord.user_id == user.id,
                        ActivityRecord.kind == ActivityKind.profile_refreshed,
                    )
                )
            )
            .scalars()
            .all()
        )
        from scribe.style.profiles import last_undoable_refresh

        assert await last_undoable_refresh(session, user.id) is None
    assert len(records) == 3 and all(r.meta_json["noop"] is True for r in records)


# --- finding 2: profile builds are serialized per user ------------------------------------------


async def test_concurrent_builds_leave_one_current_version(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
) -> None:
    session_factory = committed_session_factory
    async with session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings
        )
        user.state = UserState.active
        await session.commit()
    fake_llm.set_response("profile.build", style_output())
    samples = [
        _own(user, 1, n, clock() - timedelta(days=1)) for n in range(12)
    ]  # only their text/sent_at matter
    from scribe.llm.provider import StyleSample

    style_samples = [
        StyleSample(text=m.text, sent_at=m.sent_at, contact_display_name="x") for m in samples
    ]
    for _ in range(2):  # with and without an existing current version
        await asyncio.gather(
            *(
                build_global_profile(
                    session_factory,
                    llm_router,
                    user.id,
                    style_samples,
                    settings=settings,
                    clock=clock,
                )  # fmt: skip
                for _ in range(4)
            )
        )
        async with session_factory() as session:
            states = (
                await session.execute(
                    select(StyleProfile.state, StyleProfile.is_current).where(
                        StyleProfile.user_id == user.id
                    )
                )
            ).all()
        assert sum(state == ProfileState.current for state, _ in states) == 1
        assert sum(is_current for _, is_current in states) == 1
        assert sum(state == ProfileState.previous for state, _ in states) <= 1


async def test_rebuild_double_tap_and_confirm_button(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    from scribe.style.profiles import profile_build_lock

    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    person = next_telegram_id()
    await make_chat(user, telegram_chat_id=person, title="Synthetic Petr")
    gateway.own_history[(user.id, person)] = [
        _own(user, person, n, clock() - timedelta(days=3)) for n in range(3)
    ]
    async with profile_build_lock(user.id):  # a rebuild is already running
        await harness.feed(callback_update(control, "profrebuild:1", message_id=77))
    assert "Already rebuilding" in _answers(harness)[-1]
    assert not any(t.startswith("✅ Rebuilt") for t in harness.bot_texts())

    await harness.feed(callback_update(control, "profrebuild:1", message_id=78))
    removed = harness.api.of_type(EditMessageReplyMarkup)
    assert [int(m.message_id or 0) for m in removed] == [78]  # the confirm button goes at once
    assert removed[0].reply_markup is None
    assert harness.bot_texts()[-1].startswith("✅ Rebuilt")


# --- finding 3: a rebuild does not bring back a removed profile ---------------------------------


async def test_rebuild_keeps_removed_contact_profile_removed(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    anna = await make_contact(user, display_name="Anna")
    await make_chat(user, telegram_chat_id=anna.telegram_peer_id, title="Anna")
    await add_profile(session_factory, user)
    anna_profile = await add_profile(session_factory, user, contact=anna)
    fake_llm.set_response("profile.build", by_scope(style_output("G."), style_output("Anna.")))
    peer = anna.telegram_peer_id
    gateway.own_history[(user.id, peer)] = [
        _own(user, peer, n, clock() - timedelta(days=10)) for n in range(15)
    ]

    await harness.feed(callback_update(control, f"removeprofile:{anna_profile.id}"))
    assert "comes back only if" in harness.bot_texts()[-1]
    clock.advance(hours=1)
    await harness.feed(callback_update(control, "profrebuild:1"))
    assert harness.bot_texts()[-1].startswith("✅ Rebuilt")
    contact_current = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    assert contact_current == []  # 15 messages, all written before the removal
    assert (await _contact(session_factory, anna)).own_message_count_6m == 0

    # Messages written after the removal alone earn the slot back.
    clock.advance(days=1)
    gateway.own_history[(user.id, peer)] += [
        _own(user, peer, n, clock() - timedelta(hours=2)) for n in range(12)
    ]
    await harness.feed(callback_update(control, "profrebuild:1"))
    contact_current = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    assert [p.contact_id for p in contact_current] == [anna.id]
    assert (await _contact(session_factory, anna)).own_message_count_6m == 12


async def test_refresh_does_not_count_messages_written_before_removal(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    anna = await make_contact(user, display_name="Anna")
    chat = await make_chat(user, telegram_chat_id=anna.telegram_peer_id, title="Anna")
    await add_profile(session_factory, user)
    anna_profile = await add_profile(session_factory, user, contact=anna)
    clock.advance(days=1)
    await add_own_messages(session_factory, user, chat, 14, at=clock() - timedelta(hours=1))
    await harness.feed(
        callback_update(user.telegram_control_chat_id, f"removeprofile:{anna_profile.id}")
    )
    clock.advance(days=1)
    await refresh_profiles(session_factory, llm_router, user.id, settings=settings, clock=clock)
    assert (await _contact(session_factory, anna)).own_message_count_6m == 0
    assert (
        await profiles_of(
            session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
        )
        == []
    )


# --- finding 4: undo after a rebuild ------------------------------------------------------------


async def test_undo_refused_after_rebuild(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    ivan = await make_contact(user, display_name="Ivan")
    chat = await make_chat(user, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    await add_profile(session_factory, user)
    await add_profile(session_factory, user, contact=ivan)
    fake_llm.set_response("profile.build", by_scope(style_output("G."), style_output("Ivan.")))
    clock.advance(days=2)
    await add_own_messages(session_factory, user, chat, 14, at=clock() - timedelta(days=1))
    refreshed = await refresh_profiles(
        session_factory, llm_router, user.id, settings=settings, clock=clock
    )
    assert refreshed.batch_id is not None and refreshed.global_refreshed

    peer = ivan.telegram_peer_id
    gateway.own_history[(user.id, peer)] = [
        _own(user, peer, n, clock() - timedelta(days=3)) for n in range(14)
    ]
    clock.advance(hours=1)
    await harness.feed(callback_update(control, "profrebuild:1"))
    before = {(p.id, p.state) for p in await profiles_of(session_factory, user)}

    harness.api.clear()
    await harness.feed(callback_update(control, f"undo_refresh:{refreshed.batch_id}"))
    assert any("Nothing to undo" in a for a in _answers(harness))
    assert not any(t.startswith("↩️ Restored") for t in harness.bot_texts())
    assert {(p.id, p.state) for p in await profiles_of(session_factory, user)} == before
    current = await profiles_of(session_factory, user, state=ProfileState.current)
    assert {p.version for p in current} == {3} and len(current) == 2
    await harness.feed(text_update(control, "/profile"))
    assert "undo_refresh" not in str(harness.messages()[-1].reply_markup)


async def test_undo_never_exceeds_the_slot_cap(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    from scribe.db.repos import activity as activity_repo

    user = await make_user(state=UserState.active)
    await add_profile(session_factory, user)
    contacts = [await make_contact(user, display_name=f"C{i:02d}") for i in range(21)]
    rows = [await add_profile(session_factory, user, contact=c) for c in contacts]
    async with session_factory() as session:
        retired = await profiles_repo.get_profile(session, user.id, rows[20].id)
        assert retired is not None
        retired.state, retired.is_current = ProfileState.retired, False
        record = await activity_repo.record(
            session, user.id, ActivityKind.profile_refreshed, at=clock(),
            meta={"global": False, "contacts": 0, "replaced": [], "retired": [str(retired.id)],
                  "samples": 0, "noop": False},
        )  # fmt: skip
        await session.commit()
    result = await undo_refresh(
        session_factory, user.id, record.id, clock=clock, max_profiles=settings.max_contact_profiles
    )
    assert result.restored == 0
    current = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    assert len(current) == 20
    assert (await profile(session_factory, user, rows[20].id)).state == ProfileState.retired


# --- finding 9: interactive rebuilds are synchronous --------------------------------------------


async def test_rebuild_uses_synchronous_llm_calls(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.active)
    person = next_telegram_id()
    await make_chat(user, telegram_chat_id=person, title="Synthetic Petr")
    gateway.own_history[(user.id, person)] = [
        _own(user, person, n, clock() - timedelta(days=3)) for n in range(12)
    ]
    await harness.feed(callback_update(user.telegram_control_chat_id, "profrebuild:1"))
    builds = fake_llm.calls_for("profile.build")
    assert len(builds) == 2 and not any(call.batch for call in builds)


# --- finding 10: a rule saved while a new version is being stored is not lost -----------------


async def test_rule_saved_during_a_build_lands_on_the_new_version(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
) -> None:
    from scribe.style import profiles as profiles_module
    from scribe.style.proposals import accept_proposal

    session_factory = committed_session_factory
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    harness = make_harness(
        session_factory, settings, llm_router, fake_llm, FakeMessagingGateway(), clock
    )
    async with session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings
        )
        user.state = UserState.active
        await session.commit()
    v1 = await add_profile(session_factory, user, user_rules=["existing rule"])
    async with session_factory() as session:
        proposal = await profiles_repo.create_proposal(
            session, user.id, style_profile_id=v1.id,
            rule_json={"key": "emoji_removed", "text": "no emojis"}, evidence_draft_ids=[],
        )  # fmt: skip
        await session.commit()
    control = user.telegram_control_chat_id
    await harness.feed(callback_update(control, f"profrules:{v1.id}"))

    async with session_factory() as build:  # a rebuild storing v2, not committed yet
        built = profiles_module._Built(dict(v1.rules_json), "New summary.", [], 0)
        v2, _ = await profiles_module._store_version(
            build, user.id, scope=ProfileScope.global_, contact_id=None, built=built, at=clock()
        )
        typed = asyncio.create_task(harness.feed(text_update(control, "typed rule")))
        accepted = asyncio.create_task(
            accept_proposal(session_factory, user.id, proposal.id, lang="en")
        )
        await asyncio.sleep(0.3)
        assert not typed.done() and not accepted.done()  # both wait for the profile lock
        await build.commit()
    await asyncio.gather(typed, accepted)

    current = await profile(session_factory, user, v2.id)
    assert current.state == ProfileState.current
    assert sorted(rule_texts(current)) == ["existing rule", "no emojis", "typed rule"]


# --- finding 11: the rules input ends on other commands -----------------------------------------


async def test_other_command_ends_rules_input(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    stored = await add_profile(session_factory, user)
    await harness.feed(callback_update(control, f"profrules:{stored.id}"))
    await harness.feed(text_update(control, "/waiting"))
    await harness.feed(text_update(control, "this is not meant as a rule"))
    assert rule_texts(await profile(session_factory, user, stored.id)) == []
    # /done and /cancel still end it normally; a rule typed inside the step is kept.
    await harness.feed(callback_update(control, f"profrules:{stored.id}"))
    await harness.feed(text_update(control, "never use emojis"))
    await harness.feed(text_update(control, "/done"))
    assert rule_texts(await profile(session_factory, user, stored.id)) == ["never use emojis"]


# --- finding 12: a rebuild for a deleted account ends quietly -------------------------------


async def test_rebuild_for_deleted_user_ends_quietly(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    log_capture: LogCapture,
) -> None:
    from scribe.telegram.control_bot import profile_command

    user = await make_user(state=UserState.active)

    async def import_then_deleted(*args: object, **kwargs: object) -> object:
        async with session_factory() as session:
            await users_repo.delete_user(session, user.id)
            await session.commit()
        raise LookupError("synthetic: the account is gone")

    monkeypatch.setattr(profile_command, "import_own_messages", import_then_deleted)
    harness.api.clear()
    await harness.feed(callback_update(user.telegram_control_chat_id, "profrebuild:1"))
    assert not any("couldn't rebuild" in t for t in harness.bot_texts())
    assert log_capture.events("profile_rebuild_failed") == []


# --- finding 6: disabling a chat ---------------------------------------------------------------


async def test_disable_chat_drops_pending_draft_and_updates_card(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    anna = await make_contact(user, display_name="Anna")
    chat = await make_chat(user, telegram_chat_id=anna.telegram_peer_id, title="Anna")
    await add_profile(session_factory, user)
    await harness.incoming(user, chat, anna, "Synthetic question")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.pending
    harness.api.clear()

    await harness.feed(callback_update(user.telegram_control_chat_id, f"chatenable:{chat.id}:off"))

    (draft,) = await harness.drafts(user)
    assert draft.state == DraftState.dropped and draft.finalized_at is not None
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.dropped and turn.dropped_reason == DroppedReason.chat_disabled
    assert any("Chat disabled" in (e.text or "") for e in harness.edits())
    await harness.feed(callback_update(user.telegram_control_chat_id, f"approve:{draft.id}"))
    assert gateway.send_text_calls == []


async def test_disable_chat_does_not_overwrite_a_turn_being_read(
    committed_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
) -> None:
    """The scheduler claims the turn (waiting -> reading) while the chat is being disabled: the
    disable never writes over the claim, and the composer then ends the turn without a draft."""
    session_factory = committed_session_factory
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply("Synthetic reply"))
    gateway = FakeMessagingGateway()
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    async with session_factory() as session:
        user = await users_repo.create_user(
            session, control_chat_id=next_telegram_id(), settings=settings
        )
        user.state = UserState.active
        user.delay_min_s = user.delay_max_s = 0
        peer = next_telegram_id()
        contact = await chats_repo.upsert_contact(
            session, user.id, telegram_peer_id=peer, kind=ContactKind.person, display_name="Anna"
        )
        chat = await chats_repo.upsert_work_chat(
            session, user.id, telegram_chat_id=peer, title="Anna", kind=ChatKind.private,
            enabled=True,
        )  # fmt: skip
        await session.commit()
    await harness.incoming(user, chat, contact, "Synthetic question")
    (turn,) = await harness.turns(user)
    assert turn.state == TurnState.waiting

    async with session_factory() as claim:  # the scheduler's claim, still open
        locked = (
            await claim.execute(
                select(ConversationTurn).where(ConversationTurn.id == turn.id).with_for_update()
            )
        ).scalar_one()
        locked.state = TurnState.reading
        await claim.flush()
        disable = asyncio.create_task(
            harness.feed(
                callback_update(user.telegram_control_chat_id, f"chatenable:{chat.id}:off")
            )
        )
        await asyncio.sleep(0.3)
        assert not disable.done()  # it waits for the claim instead of reading stale rows
        await claim.commit()
    await disable

    (after,) = await harness.turns(user)
    assert after.state == TurnState.reading  # the claim was not overwritten
    with pytest.raises(ComposeError):
        await harness.composer.compose(user.id, turn.id)
    (after,) = await harness.turns(user)
    assert after.state == TurnState.dropped
    assert after.dropped_reason == DroppedReason.chat_disabled
    assert await harness.drafts(user) == []
    assert gateway.send_text_calls == []
    async with session_factory() as session:
        stored = await session.get(WorkChat, chat.id)
        assert stored is not None and not stored.enabled


# --- finding 5: everything content-bearing is ciphertext in the raw columns ---------------------


def _is_packed(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"enc"}
        and isinstance(value["enc"], str)
        and bool(CIPHERTEXT.match(value["enc"]))
    )


async def _raw_profile_columns(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> list[tuple[object, object, str]]:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT rules_json::text, user_rules_json::text, summary_text "
                "FROM style_profiles WHERE user_id = :u"
            ),
            {"u": user.id},
        )
        return [(json.loads(r), json.loads(u), s) for r, u, s in result.all()]


async def test_profiles_proposals_commitments_are_ciphertext_at_rest(
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
    clock: FrozenClock,
) -> None:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    stored = await add_profile(
        session_factory, user, summary="Synthetic plaintext summary", user_rules=["no emojis"],
        greetings=["Synthetic greeting"],
    )  # fmt: skip
    async with session_factory() as session:
        await profiles_repo.create_proposal(
            session, user.id, style_profile_id=stored.id,
            rule_json={"key": "emoji_removed", "text": "no emojis"}, evidence_draft_ids=[],
        )  # fmt: skip
        await commitments_repo.add_commitment(
            session, user.id, contact_id=contact.id, what="Synthetic promise text", due=None,
            source_message_text="Synthetic quote", source_sent_at=clock(), confidence=0.9,
        )  # fmt: skip
        await session.commit()

    for rules_json, user_rules_json, summary in await _raw_profile_columns(session_factory, user):
        assert _is_packed(rules_json) and _is_packed(user_rules_json)
        assert CIPHERTEXT.match(summary)
    async with session_factory() as session:
        (proposal_raw,) = (
            await session.execute(
                text("SELECT rule_json::text FROM style_rule_proposals WHERE user_id = :u"),
                {"u": user.id},
            )
        ).scalars()
        (what_raw,) = (
            await session.execute(
                text("SELECT what FROM commitments WHERE user_id = :u"), {"u": user.id}
            )
        ).scalars()
        dump = await session.scalar(
            text(
                "SELECT string_agg(t::text, ' ') FROM ("
                "SELECT rules_json::text || user_rules_json::text || summary_text AS t "
                "FROM style_profiles WHERE user_id = :u UNION ALL "
                "SELECT rule_json::text FROM style_rule_proposals WHERE user_id = :u UNION ALL "
                "SELECT what FROM commitments WHERE user_id = :u) AS x"
            ),
            {"u": user.id},
        )
    assert _is_packed(json.loads(proposal_raw))
    assert CIPHERTEXT.match(what_raw)
    for plaintext in ("Synthetic plaintext summary", "no emojis", "Synthetic greeting",
                      "Synthetic promise text", "formality"):  # fmt: skip
        assert plaintext not in dump


async def test_reencrypt_legacy_plaintext_rows(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    clock: FrozenClock,
    log_capture: LogCapture,
) -> None:
    from scribe.drafting.stored_output import load_stored_output
    from scribe.retention.reencrypt import reencrypt_legacy_rows

    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id, title="Anna")
    stored = await add_profile(session_factory, user, user_rules=["no emojis"])
    await harness.incoming(user, chat, contact, "Synthetic question")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    expected_output = load_stored_output(draft.output_json, draft.text).output
    assert expected_output is not None
    async with session_factory() as session:
        proposal = await profiles_repo.create_proposal(
            session, user.id, style_profile_id=stored.id,
            rule_json={"key": "emoji_removed", "text": "no emojis"}, evidence_draft_ids=[],
        )  # fmt: skip
        commitment = await commitments_repo.add_commitment(
            session, user.id, contact_id=contact.id, what="Synthetic legacy promise", due=None,
            source_message_text=None, source_sent_at=clock(), confidence=0.9,
        )  # fmt: skip
        await session.commit()
        # Rows written before the columns were encrypted (plaintext, same column types).
        legacy_output = {
            **{k: v for k, v in draft.output_json.items() if k != "enc"},
            **{
                k: expected_output.model_dump(mode="json")[k]
                for k in ("placeholders", "sources", "flags", "conflicts")
            },
        }
        await session.execute(
            text(
                "UPDATE style_profiles SET rules_json = CAST(:r AS jsonb), "
                "user_rules_json = CAST(:u AS jsonb), summary_text = :s WHERE id = :id"
            ),
            {
                "r": json.dumps({"formality": 2, "greetings": ["Synthetic legacy hi"]}),
                "u": json.dumps({"rules": [{"text": "legacy rule", "source": "user"}]}),
                "s": "Synthetic legacy summary",
                "id": stored.id,
            },
        )
        await session.execute(
            text("UPDATE style_rule_proposals SET rule_json = CAST(:r AS jsonb) WHERE id = :id"),
            {"r": json.dumps({"key": "emoji_removed", "text": "no emojis"}), "id": proposal.id},
        )
        await session.execute(
            text("UPDATE commitments SET what = :w WHERE id = :id"),
            {"w": "Synthetic legacy promise", "id": commitment.id},
        )
        await session.execute(
            text("UPDATE drafts SET output_json = CAST(:o AS jsonb) WHERE id = :id"),
            {"o": json.dumps(legacy_output), "id": draft.id},
        )
        await session.commit()

    first = await reencrypt_legacy_rows(session_factory, batch_size=1)
    assert first.profiles == 1 and first.proposals == 1
    assert first.commitments == 1 and first.drafts == 1
    second = await reencrypt_legacy_rows(session_factory, batch_size=1)  # idempotent
    assert (second.profiles, second.proposals, second.commitments, second.drafts) == (0, 0, 0, 0)

    ((rules_json, user_rules_json, summary),) = await _raw_profile_columns(session_factory, user)
    assert _is_packed(rules_json) and _is_packed(user_rules_json) and CIPHERTEXT.match(summary)
    async with session_factory() as session:
        proposal_raw = await session.scalar(
            text("SELECT rule_json::text FROM style_rule_proposals WHERE id = :id"),
            {"id": proposal.id},
        )
        what_raw = await session.scalar(
            text("SELECT what FROM commitments WHERE id = :id"), {"id": commitment.id}
        )
        output_raw = json.loads(
            await session.scalar(
                text("SELECT output_json::text FROM drafts WHERE id = :id"), {"id": draft.id}
            )
        )
    assert _is_packed(json.loads(proposal_raw)) and CIPHERTEXT.match(what_raw)
    assert "enc" in output_raw and not {"placeholders", "sources", "flags"} & set(output_raw)
    # The values read back unchanged.
    reread = await profile(session_factory, user, stored.id)
    assert reread.rules_json == {"formality": 2, "greetings": ["Synthetic legacy hi"]}
    assert rule_texts(reread) == ["legacy rule"]
    assert reread.summary_text == "Synthetic legacy summary"
    (fresh_draft,) = await harness.drafts(user)
    assert load_stored_output(fresh_draft.output_json, fresh_draft.text).output == expected_output
    async with session_factory() as session:
        (open_commitment,) = await commitments_repo.list_open(session, user.id)
    assert open_commitment.what == "Synthetic legacy promise"
    assert "Synthetic legacy" not in log_capture.raw


# --- finding 7: previous versions are not kept forever ------------------------------------------


async def test_purge_removes_old_previous_versions_except_the_undo_target(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    fake_llm.set_response("profile.build", style_output())
    # A user whose previous global version is the target of the undoable refresh ...
    keeper = await make_user(state=UserState.active)
    ivan = await make_contact(keeper, display_name="Ivan")
    chat = await make_chat(keeper, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    old_keeper = await add_profile(session_factory, keeper)
    clock.advance(days=1)
    await add_own_messages(session_factory, keeper, chat, 12, at=clock() - timedelta(hours=1))
    refreshed = await refresh_profiles(
        session_factory, llm_router, keeper.id, settings=settings, clock=clock
    )
    assert refreshed.global_refreshed
    # ... and one whose previous version came from a rebuild (no refresh to undo).
    other = await make_user(state=UserState.active)
    old_other = await add_profile(session_factory, other)
    await build_global_profile(session_factory, llm_router, other.id, [], settings=settings)
    assert (await profile(session_factory, other, old_other.id)).state == ProfileState.previous
    async with session_factory() as session:
        await session.execute(
            text("UPDATE style_profiles SET updated_at = :t WHERE state = 'previous'"),
            {"t": FROZEN_NOW - timedelta(days=40)},
        )
        await session.commit()

    stats = await purge_messages(session_factory, settings, clock())

    assert stats.profiles_deleted >= 1
    async with session_factory() as session:
        assert await profiles_repo.get_profile(session, other.id, old_other.id) is None
        kept = await profiles_repo.get_profile(session, keeper.id, old_keeper.id)
    assert kept is not None and kept.state == ProfileState.previous


# --- finding 12: a failed deletion after the log-out is explained -------------------------------


async def test_delete_failure_after_log_out_is_explained(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: FakeMessagingGateway,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scribe.telegram.control_bot import account_command

    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id

    async def log_out(user_id: uuid.UUID) -> None:
        return None

    async def failing_delete(session: AsyncSession, user_id: uuid.UUID) -> None:
        raise ConnectionError("synthetic")

    monkeypatch.setattr(gateway, "log_out", log_out, raising=False)
    monkeypatch.setattr(account_command, "delete_user_data", failing_delete)
    await harness.feed(text_update(control, "/delete_my_data"))
    await harness.feed(text_update(control, "DELETE"))
    last = harness.bot_texts()[-1]
    assert "logged out" in last and "couldn't delete" in last
    async with session_factory() as session:
        assert await users_repo.get_user(session, user.id) is not None


# --- finding 9: the weekly job resumes after a restart ----------------------------------------


async def test_weekly_refresh_resumes_after_restart(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    from scribe.jobs import refresh_all_users

    users = []
    for name in ("Done", "Interrupted"):
        user = await make_user(state=UserState.active, display_name=name)
        contact = await make_contact(user, display_name=f"{name} contact")
        chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id, title=name)
        await add_profile(session_factory, user)
        await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(days=1))
        users.append(user)
    fake_llm.set_response("profile.build", style_output())
    # The run started this week: the first user was refreshed before the restart.
    clock.advance(hours=1)
    await refresh_profiles(session_factory, llm_router, users[0].id, settings=settings, clock=clock)
    builds_before = len(fake_llm.calls_for("profile.build"))

    clock.advance(minutes=30)
    await refresh_all_users(
        session_factory, llm_router, harness.bot, settings=settings, clock=clock
    )

    async with session_factory() as session:
        counts = dict(
            (
                await session.execute(
                    select(ActivityRecord.user_id, func.count())
                    .where(ActivityRecord.kind == ActivityKind.profile_refreshed)
                    .group_by(ActivityRecord.user_id)
                )
            ).all()
        )
    assert counts == {users[0].id: 1, users[1].id: 1}
    assert len(fake_llm.calls_for("profile.build")) == builds_before + 1  # only the second user


async def test_interrupted_weekly_job_is_requeued_at_startup(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
) -> None:
    from datetime import time

    from scribe.db.models import ScheduledJob
    from scribe.scheduling.scheduler import Scheduler, Weekly

    async def job() -> None:
        return None

    schedule = Weekly(weekday=FROZEN_NOW.weekday(), at=time(2, 0))
    occurrence = schedule.next_after(FROZEN_NOW - timedelta(days=7))
    async with session_factory() as session:
        # Claimed at the occurrence (next_run_at moved a week on), never finished.
        session.add(
            ScheduledJob(name="synthetic_weekly", next_run_at=occurrence + timedelta(days=7))
        )
        session.add(
            ScheduledJob(
                name="synthetic_done", next_run_at=occurrence + timedelta(days=7),
                last_run_at=occurrence, last_status="ok",
            )
        )  # fmt: skip
        await session.flush()
        await session.execute(
            text("UPDATE scheduled_jobs SET created_at = :t WHERE name LIKE 'synthetic_%'"),
            {"t": occurrence - timedelta(days=30)},
        )
        await session.commit()
    clock.set(occurrence + timedelta(hours=1))
    scheduler = Scheduler(session_factory, tick_s=5, clock=clock)
    scheduler.register_job("synthetic_weekly", schedule, job)
    scheduler.register_job("synthetic_done", schedule, job)
    await scheduler.ensure_jobs()
    async with session_factory() as session:
        rows = {
            r.name: r
            for r in (
                await session.execute(
                    select(ScheduledJob).where(ScheduledJob.name.like("synthetic_%"))
                )
            ).scalars()
        }
    assert rows["synthetic_weekly"].next_run_at <= clock()  # runs again on the next tick
    assert rows["synthetic_done"].next_run_at == occurrence + timedelta(days=7)
