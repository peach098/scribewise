"""US4 style profiles (T061): per-contact profiles with a 20-slot cap and pinning, selection over
the global profile, group chats as one contact, the weekly refresh with undo that keeps the
user's rules, rule proposals from repeated edits, and the weak-profile warning. Fake LLM, fake
gateway, offline Bot API; synthetic data only."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Contact,
    ContactKind,
    DraftState,
    ProfileScope,
    ProfileState,
    ProposalState,
    StyleRuleProposal,
    UserState,
)
from scribe.db.repos import chats as chats_repo
from scribe.jobs import refresh_user
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.style.importer import import_own_messages
from scribe.style.profiles import (
    assign_profile_slots,
    build_profiles_from_import,
    refresh_profiles,
    rule_texts,
)
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import IncomingMessage
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser, next_telegram_id
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import _block, draft_output, grounded_reply, next_message_id
from tests.integration.us2_support import US2Harness, callback_datas
from tests.integration.us4_support import (
    add_own_messages,
    add_profile,
    by_scope,
    make_harness,
    profile,
    profiles_of,
    style_output,
)


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


def _compose_profile(fake_llm: FakeAdapter) -> dict[str, object]:
    return dict(_block(fake_llm.calls_for("compose.draft")[-1], "profile"))


async def _contact(session_factory: async_sessionmaker[AsyncSession], contact: Contact) -> Contact:
    async with session_factory() as session:
        found = await chats_repo.get_contact(session, contact.user_id, contact.id)
        assert found is not None
        return found


# --- slots --------------------------------------------------------------------------------------


async def test_profile_cap_and_pinning(
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
    user = await make_user(state=UserState.active)
    await add_profile(session_factory, user)
    # 25 active contacts (60 … 36 own messages); the 3 least active ones are pinned.
    contacts = []
    for i in range(25):
        contact = await make_contact(user, display_name=f"Contact {i:02d}")
        contacts.append(contact)
    async with session_factory() as session:
        for i, contact in enumerate(contacts):
            stored = await chats_repo.get_contact(session, user.id, contact.id)
            assert stored is not None
            stored.own_message_count_6m = 60 - i
            stored.pinned_profile = i >= 22
        await session.commit()
    for contact in contacts:
        await add_profile(session_factory, user, contact=contact)

    async with session_factory() as session:
        slots = await assign_profile_slots(
            session, user.id, max_profiles=settings.max_contact_profiles
        )
        await session.commit()
    kept = {c.display_name for c in slots.kept}
    assert len(kept) == 20 and len(slots.retired_profile_ids) == 5
    assert {f"Contact {i:02d}" for i in (22, 23, 24)} <= kept  # pinned, though least active
    assert {f"Contact {i:02d}" for i in range(17, 22)}.isdisjoint(kept)
    current = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    assert len(current) == 20

    # A newcomer becomes more active than the least active unpinned contact (44 messages).
    newcomer = await make_contact(user, display_name="Newcomer")
    chat = await make_chat(user, telegram_chat_id=newcomer.telegram_peer_id, title="Newcomer")
    async with session_factory() as session:
        stored = await chats_repo.get_contact(session, user.id, newcomer.id)
        assert stored is not None
        stored.own_message_count_6m = 40  # below the cut: no profile yet
        await session.commit()
    clock.advance(days=1)
    await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(hours=1))
    fake_llm.set_response("profile.build", style_output())
    result = await refresh_profiles(
        session_factory, llm_router, user.id, settings=settings, clock=clock
    )

    assert result.changed and result.retired == 1
    current = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    by_contact = {p.contact_id for p in current}
    assert len(current) == 20
    assert newcomer.id in by_contact
    assert contacts[16].id not in by_contact  # least active unpinned: retired
    assert all(contacts[i].id in by_contact for i in (22, 23, 24))  # pinned: never retired
    assert (await _contact(session_factory, newcomer)).own_message_count_6m == 52
    # The cap holds in the view as well.
    await harness.feed(text_update(user.telegram_control_chat_id, "/profile"))
    assert "Contact profiles (20/20)" in str(harness.messages()[-1].reply_markup)


async def test_per_contact_profile_selected_over_global(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    manager = await make_contact(user, display_name="Synthetic Manager")
    teammate = await make_contact(user, display_name="Synthetic Teammate")
    other = await make_contact(user, display_name="Synthetic Stranger")
    chats = {
        c.id: await make_chat(user, telegram_chat_id=c.telegram_peer_id, title=c.display_name)
        for c in (manager, teammate, other)
    }
    global_profile = await add_profile(session_factory, user, formality=3)
    manager_profile = await add_profile(
        session_factory, user, contact=manager, formality=5, summary="Formal."
    )
    teammate_profile = await add_profile(
        session_factory, user, contact=teammate, formality=1, summary="Casual."
    )

    expected = [
        (manager, manager_profile.id, 5, "Synthetic Manager"),
        (teammate, teammate_profile.id, 1, "Synthetic Teammate"),
        (other, global_profile.id, 3, "global"),
    ]
    seen: set[uuid.UUID] = set()
    for contact, profile_id, formality, label in expected:
        await harness.incoming(user, chats[contact.id], contact, "Synthetic question")
        await harness.tick()
        # The new draft: drafts of one test share a timestamp and a version, so the list order
        # alone does not say which is the newest.
        (draft,) = [d for d in await harness.drafts(user) if d.id not in seen]
        seen.add(draft.id)
        assert draft.style_profile_id == profile_id
        assert _compose_profile(fake_llm)["rules_json"]["formality"] == formality  # type: ignore[index]
        card = harness.messages()[-1].text
        assert f"✍️ Draft ({label}, tone shift: none)" in card
        await harness.feed(callback_update(user.telegram_control_chat_id, f"reject:{draft.id}"))


async def test_group_chat_is_one_contact(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.active)
    group = await make_chat(user, kind=ChatKind.group, title="Synthetic Finance")
    person_id = next_telegram_id()
    await make_chat(user, telegram_chat_id=person_id, title="Synthetic Ivan")

    def own(chat_id: int, n: int) -> IncomingMessage:
        return IncomingMessage(
            user_id=user.id, chat_id=chat_id, message_id=next_message_id(), sender_peer_id=0,
            sender_display_name="me", text=f"Synthetic own text {n}", has_media=False,
            sent_at=clock() - timedelta(days=10), is_outgoing=True,
        )  # fmt: skip

    gateway.own_history[(user.id, group.telegram_chat_id)] = [
        own(group.telegram_chat_id, n) for n in range(15)
    ]
    gateway.own_history[(user.id, person_id)] = [own(person_id, n) for n in range(12)]
    fake_llm.set_response(
        "profile.build", by_scope(style_output("Global."), style_output("Contact."))
    )
    imported = await import_own_messages(
        gateway, session_factory, user.id, settings=settings, clock=clock
    )
    summary = await build_profiles_from_import(
        session_factory, llm_router, user.id, imported, settings=settings
    )

    assert summary.contact_profiles == 2
    async with session_factory() as session:
        contacts = {c.telegram_peer_id: c for c in await chats_repo.list_contacts(session, user.id)}
    group_contact = contacts[group.telegram_chat_id]
    assert group_contact.kind == ContactKind.group
    assert group_contact.own_message_count_6m == 15
    assert contacts[person_id].kind == ContactKind.person
    contact_profiles = await profiles_of(
        session_factory, user, state=ProfileState.current, scope=ProfileScope.contact
    )
    assert {p.contact_id for p in contact_profiles} == {group_contact.id, contacts[person_id].id}
    # The raw import is not kept: only the chosen examples (≤ 20 per contact).
    assert all(len(p.examples) <= settings.profile_examples_contact for p in contact_profiles)

    # Two people write in the group: both drafts use the one group profile.
    for sender in ("Synthetic Alice", "Synthetic Bob"):
        alice = IncomingMessage(
            user_id=user.id, chat_id=group.telegram_chat_id, message_id=next_message_id(),
            sender_peer_id=next_telegram_id(), sender_display_name=sender,
            text="@me synthetic question", has_media=False, sent_at=clock(), mentions_me=True,
            chat_kind="group", chat_title=group.title,
        )  # fmt: skip
        await gateway.emit_new_message(alice)
        await harness.tick()
        draft = (await harness.drafts(user))[-1]
        group_profile = next(p for p in contact_profiles if p.contact_id == group_contact.id)
        assert draft.style_profile_id == group_profile.id
        assert "✍️ Draft (Synthetic Finance," in harness.messages()[-1].text
        await harness.feed(callback_update(user.telegram_control_chat_id, f"reject:{draft.id}"))
    async with session_factory() as session:
        people = [c for c in await chats_repo.list_contacts(session, user.id)]
    assert sum(c.kind == ContactKind.group for c in people) == 1
    assert len(contact_profiles) == 2  # the senders in the group got no profile of their own


# --- weekly refresh -----------------------------------------------------------------------------


async def test_refresh_keeps_user_rules_and_undo(
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
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    ivan = await make_contact(user, display_name="Ivan")
    chat = await make_chat(user, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    global_v1 = await add_profile(
        session_factory, user, summary="Old global summary.", user_rules=["never use emojis"],
        typical_length_words=40, formality=4,
    )  # fmt: skip
    ivan_v1 = await add_profile(
        session_factory, user, contact=ivan, summary="Old Ivan summary.",
        user_rules=["reply in English to Ivan"], typical_length_words=40, formality=4,
    )  # fmt: skip
    clock.advance(days=2)
    await add_own_messages(session_factory, user, chat, 14, at=clock() - timedelta(days=1))
    fake_llm.set_response(
        "profile.build",
        by_scope(
            style_output("New global summary.", typical_length_words=10, formality=2),
            style_output("New Ivan summary.", typical_length_words=10, formality=4),
        ),
    )

    result = await refresh_user(
        session_factory, llm_router, harness.bot, user.id, settings=settings, clock=clock
    )

    assert result.global_refreshed and result.contacts_refreshed == 1
    # One batch for both profiles (the fake adapter can batch; the route prefers it).
    builds = fake_llm.calls_for("profile.build")
    assert len(builds) == 2 and all(call.batch for call in builds)
    note = harness.messages()[-1]
    assert note.text.startswith("🔄 Profiles refreshed (global + 1 contact). Changes:")
    assert "shorter replies with Ivan" in note.text
    assert "more casual overall" in note.text
    assert callback_datas(note.reply_markup) == [f"undo_refresh:{result.batch_id}"]
    current = await profiles_of(session_factory, user, state=ProfileState.current)
    assert {p.version for p in current} == {2}
    assert {p.summary_text for p in current} == {"New global summary.", "New Ivan summary."}
    # The user's own rules are copied forward unchanged.
    by_scope_current = {p.scope: p for p in current}
    assert rule_texts(by_scope_current[ProfileScope.global_]) == ["never use emojis"]
    assert rule_texts(by_scope_current[ProfileScope.contact]) == ["reply in English to Ivan"]
    assert (await profile(session_factory, user, global_v1.id)).state == ProfileState.previous

    # A rule added after the refresh survives the undo.
    await harness.feed(text_update(control, "/profile"))
    assert "New global summary." in harness.messages()[-1].text
    assert f"undo_refresh:{result.batch_id}" in callback_datas(harness.messages()[-1].reply_markup)
    await harness.feed(
        callback_update(control, f"profrules:{by_scope_current[ProfileScope.global_].id}")
    )
    await harness.feed(text_update(control, "no exclamation marks"))
    await harness.feed(text_update(control, "/done"))

    await harness.feed(callback_update(control, f"undo_refresh:{result.batch_id}"))
    assert harness.messages()[-1].text.startswith("↩️ Restored your previous profiles (2 restored)")
    restored_global = await profile(session_factory, user, global_v1.id)
    restored_ivan = await profile(session_factory, user, ivan_v1.id)
    assert restored_global.state == restored_ivan.state == ProfileState.current
    assert restored_global.summary_text == "Old global summary."
    assert rule_texts(restored_global) == ["never use emojis", "no exclamation marks"]
    assert rule_texts(restored_ivan) == ["reply in English to Ivan"]
    assert {
        p.version for p in await profiles_of(session_factory, user, state=ProfileState.current)
    } == {1}
    await harness.feed(text_update(control, "/profile"))
    assert "Old global summary." in harness.messages()[-1].text
    assert "undo_refresh" not in str(harness.messages()[-1].reply_markup)

    # One undo only.
    harness.api.clear()
    await harness.feed(callback_update(control, f"undo_refresh:{result.batch_id}"))
    assert {
        p.version for p in await profiles_of(session_factory, user, state=ProfileState.current)
    } == {1}
    async with session_factory() as session:
        kinds = (
            (
                await session.execute(
                    select(ActivityRecord.kind).where(ActivityRecord.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
    assert sorted(kinds) == [ActivityKind.profile_refreshed, ActivityKind.profile_undo]


# --- rule proposals -----------------------------------------------------------------------------


async def test_rule_proposal_after_repeated_edits(
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
    other = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    anna = await make_contact(user, display_name="Anna")
    chat = await make_chat(user, telegram_chat_id=anna.telegram_peer_id, title="Anna")
    global_profile = await add_profile(session_factory, user)
    drafted = "Привет! Отчёт будет готов в пятницу, пришлю сразу 🙂"
    fake_llm.set_response("compose.draft", lambda call: draft_output(drafted, sources=[
        {"kind": "message", "id": _block(call, "incoming_messages")[0]["id"],
         "used_for": "the question"}]))  # fmt: skip
    edits = [
        "Привет! Отчёт будет готов в пятницу, пришлю сразу, спасибо 🙂",  # emoji kept
        *["Привет! Отчёт будет готов в пятницу, пришлю сразу"] * 5,  # emoji removed
    ]
    for round_number, edited in enumerate(edits, start=1):
        await harness.incoming(user, chat, anna, f"Synthetic question {round_number}")
        await harness.tick()
        # The pending one: drafts of different turns all have version 1 (heap order).
        (draft,) = [d for d in await harness.drafts(user) if d.state == DraftState.pending]
        await harness.feed(callback_update(control, f"edit:{draft.id}"))
        await harness.feed(text_update(control, edited))
        assert gateway.send_text_calls[-1].text == edited
        proposals = [t for t in harness.bot_texts() if t.startswith("💡")]
        assert bool(proposals) is (round_number == 6)  # 5 of the last 6, not before
        clock.advance(minutes=5)

    (proposal_message,) = [m for m in harness.messages() if m.text.startswith("💡")]
    assert proposal_message.text == (
        "💡 I noticed you removed emojis in 5 of your last 6 edits with your global profile.\n"
        'Add rule "no emojis" to your global profile?'
    )
    async with session_factory() as session:
        (proposal,) = (
            (
                await session.execute(
                    select(StyleRuleProposal).where(StyleRuleProposal.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
    assert proposal.state == ProposalState.proposed and len(proposal.evidence_draft_ids) == 5
    assert callback_datas(proposal_message.reply_markup) == [
        f"rule_accept:{proposal.id}",
        f"rule_dismiss:{proposal.id}",
    ]
    # Nothing changed silently; another user cannot accept it.
    assert rule_texts(await profile(session_factory, user, global_profile.id)) == []
    await harness.feed(
        callback_update(other.telegram_control_chat_id, f"rule_accept:{proposal.id}")
    )
    assert rule_texts(await profile(session_factory, user, global_profile.id)) == []

    await harness.feed(callback_update(control, f"rule_accept:{proposal.id}"))
    stored = await profile(session_factory, user, global_profile.id)
    assert stored.user_rules_json["rules"] == [
        {"text": "no emojis", "source": "proposal", "key": "emoji_removed"}
    ]
    edited_card = harness.api.of_type(EditMessageText)[-1]
    assert (
        edited_card.text == '✅ Added rule "no emojis" to your global profile. /profile shows it.'
    )
    await harness.feed(text_update(control, "/profile"))
    assert "1. no emojis" in harness.messages()[-1].text
    async with session_factory() as session:
        accepted = (
            (
                await session.execute(
                    select(ActivityRecord).where(
                        ActivityRecord.user_id == user.id,
                        ActivityRecord.kind == ActivityKind.rule_accepted,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(accepted) == 1 and "no emojis" not in json.dumps(accepted[0].meta_json)
    # Accepting twice does nothing more; the next draft sees the rule.
    await harness.feed(callback_update(control, f"rule_accept:{proposal.id}"))
    assert len(rule_texts(await profile(session_factory, user, global_profile.id))) == 1
    await harness.incoming(user, chat, anna, "Synthetic question 7")
    await harness.tick()
    assert _compose_profile(fake_llm)["user_rules_json"] == stored.user_rules_json


# --- weak profile -------------------------------------------------------------------------------


async def test_weak_profile_warning_under_50_samples(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user = await make_user(state=UserState.active)
    person_id = next_telegram_id()
    chat = await make_chat(user, telegram_chat_id=person_id, title="Synthetic Petr")
    gateway.own_history[(user.id, person_id)] = [
        IncomingMessage(
            user_id=user.id, chat_id=person_id, message_id=next_message_id(), sender_peer_id=0,
            sender_display_name="me", text=f"Synthetic own text {n}", has_media=False,
            sent_at=clock() - timedelta(days=3), is_outgoing=True,
        )
        for n in range(8)
    ]  # fmt: skip
    fake_llm.set_response("profile.build", style_output("Few samples."))
    await harness.feed(callback_update(user.telegram_control_chat_id, "profrebuild:0"))
    assert "Rebuild re-reads your own messages" in harness.messages()[-1].text
    await harness.feed(callback_update(user.telegram_control_chat_id, "profrebuild:1"))
    done = harness.messages()[-1].text
    assert done.startswith("✅ Rebuilt: global profile from 8 of your messages, 0 contact")
    assert "fewer than 50 of your messages" in done

    (global_profile,) = await profiles_of(session_factory, user, state=ProfileState.current)
    assert global_profile.rules_json["weak"] is True
    await harness.feed(text_update(user.telegram_control_chat_id, "/profile"))
    view = harness.messages()[-1].text
    assert "built from 8 messages" in view and "style matching is weak" in view

    # 8 messages are too few for a contact profile: the global (weak) profile applies.
    async with session_factory() as session:
        petr = await chats_repo.get_contact_by_peer(session, user.id, person_id)
    assert petr is not None
    await harness.incoming(user, chat, petr, "Synthetic question")
    await harness.tick()
    draft = (await harness.drafts(user))[-1]
    assert draft.style_profile_id == global_profile.id
    assert draft.output_json["system_warnings"] == ["weak_style_profile"]
    card = [m for m in harness.api.of_type(SendMessage) if "✍️ Draft" in m.text][-1]
    assert "⚠️ weak style profile" in card.text


async def test_profile_buttons_pin_remove_and_rules(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    anna = await make_contact(user, display_name="Anna")
    await add_profile(session_factory, user, summary="Global summary.")
    anna_profile = await add_profile(session_factory, user, contact=anna, summary="Anna summary.")

    await harness.feed(text_update(control, "/profile"))
    view = harness.messages()[-1]
    assert view.text.startswith("🧑 Your style (global, v1, built from 120 messages)")
    assert "Rules you set: none yet" in view.text
    assert "profcontacts:0" in callback_datas(view.reply_markup)

    await harness.feed(callback_update(control, "profcontacts:0"))
    listing = harness.messages()[-1]
    assert "👥 Contact profiles (1/20)" in listing.text and "• Anna · v1" in listing.text
    assert callback_datas(listing.reply_markup) == [
        f"profview:{anna_profile.id}",
        f"pin:{anna_profile.id}",
        f"removeprofile:{anna_profile.id}",
    ]
    await harness.feed(callback_update(control, f"pin:{anna_profile.id}"))
    assert (await _contact(session_factory, anna)).pinned_profile
    assert f"unpin:{anna_profile.id}" in callback_datas(harness.messages()[-1].reply_markup)
    await harness.feed(callback_update(control, f"unpin:{anna_profile.id}"))
    assert not (await _contact(session_factory, anna)).pinned_profile

    # Rules for Anna's profile: add two, remove one, a bad number is explained.
    await harness.feed(callback_update(control, f"profview:{anna_profile.id}"))
    assert "Anna summary." in harness.messages()[-1].text
    await harness.feed(callback_update(control, f"profrules:{anna_profile.id}"))
    assert harness.bot_texts()[-1].startswith("✏️ Rules for Anna")
    await harness.feed(text_update(control, "never use emojis"))
    await harness.feed(text_update(control, "always reply in English"))
    await harness.feed(text_update(control, "-5"))
    assert "There is no rule 5." in harness.bot_texts()[-1]
    await harness.feed(text_update(control, "-1"))
    await harness.feed(text_update(control, "/done"))
    assert rule_texts(await profile(session_factory, user, anna_profile.id)) == [
        "always reply in English"
    ]

    await harness.feed(callback_update(control, f"removeprofile:{anna_profile.id}"))
    assert harness.bot_texts()[-1].startswith("🗑 Removed the profile for Anna")
    assert (await profile(session_factory, user, anna_profile.id)).state == ProfileState.retired
    assert (await _contact(session_factory, anna)).own_message_count_6m == 0
    harness.api.clear()
    await harness.feed(callback_update(control, f"pin:{anna_profile.id}"))  # retired: refused
    assert not (await _contact(session_factory, anna)).pinned_profile
