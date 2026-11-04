"""Issue #42: the global profile's user rules apply to every draft, also for a contact with a
profile of its own. They are merged at draft time (never copied into the contact's rows), so a
change to a global rule reaches the next contact draft without a rebuild; contact builds learn
the contact's style without them; ``/profile`` for a contact shows them read-only (within one
Telegram message); a rule the user already set globally is not proposed or added again for a
contact. Fake LLM, fake gateway, offline Bot API; synthetic data only."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    Contact,
    ProfileScope,
    ProfileState,
    ProposalState,
    StyleRuleProposal,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import profiles as profiles_repo
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import StyleSample
from scribe.llm.registry import LLMRouter
from scribe.style.profiles import build_contact_profile, refresh_profiles, rule_texts
from scribe.telegram.control_bot.render import TELEGRAM_TEXT_MAX
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import _block, draft_output, grounded_reply
from tests.integration.us2_support import US2Harness
from tests.integration.us4_support import (
    add_own_messages,
    add_profile,
    by_scope,
    make_harness,
    profile,
    profiles_of,
    scope_of,
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


def _draft_rules(fake_llm: FakeAdapter) -> list[tuple[str, str | None]]:
    """(text, scope) of the user rules in the newest drafting request."""
    rules_block = _block(fake_llm.calls_for("compose.draft")[-1], "user_rules")
    return [(r["text"], r.get("scope")) for r in rules_block["rules"]]


def _build_rules(call: FakeCall) -> list[tuple[str, str | None]]:
    rules = _block(call, "existing_user_rules_json").get("rules", [])
    return [(r["text"], r.get("scope")) for r in rules]


async def _edit_away_emojis(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    user: User,
    chat: WorkChat,
    contact: Contact,
    rounds: range = range(1, 7),
) -> None:
    """Drafts for ``contact`` (one per round) whose emoji the user removes before sending."""
    drafted = "Привет! Отчёт будет готов в пятницу, пришлю сразу 🙂"

    def respond(call: FakeCall) -> Any:
        source = {"kind": "message", "id": _block(call, "incoming_messages")[0]["id"],
                  "used_for": "the question"}  # fmt: skip
        return draft_output(drafted, sources=[source])

    fake_llm.set_response("compose.draft", respond)
    control = user.telegram_control_chat_id
    for round_number in rounds:
        await harness.incoming(user, chat, contact, f"Synthetic question {round_number}")
        await harness.tick()
        draft = (await harness.drafts(user))[-1]
        await harness.feed(callback_update(control, f"edit:{draft.id}"))
        await harness.feed(
            text_update(control, "Привет! Отчёт будет готов в пятницу, пришлю сразу")
        )
        clock.advance(minutes=5)


async def test_global_rules_reach_contact_drafts_and_follow_edits(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    ivan = await make_contact(user, display_name="Ivan")
    chat = await make_chat(user, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    global_profile = await add_profile(session_factory, user, user_rules=["never use emojis"])
    ivan_profile = await add_profile(
        session_factory, user, contact=ivan, user_rules=["reply in English to Ivan"]
    )

    # The draft for Ivan uses his profile and still carries the global rule, global first.
    await harness.incoming(user, chat, ivan, "Synthetic question 1")
    await harness.tick()
    first = (await harness.drafts(user))[-1]
    assert first.style_profile_id == ivan_profile.id
    assert _draft_rules(fake_llm) == [
        ("never use emojis", "global"),
        ("reply in English to Ivan", "contact"),
    ]

    await harness.feed(callback_update(control, f"reject:{first.id}"))

    # /profile for Ivan lists his own rule (numbered) and the global one (read-only).
    await harness.feed(callback_update(control, f"profview:{ivan_profile.id}"))
    view = harness.messages()[-1].text
    assert "1. reply in English to Ivan" in view
    assert "Global rules (apply here too; edit them in your global profile):" in view
    assert "• never use emojis" in view
    # Removing rule 1 in Ivan's rules removes his own rule, never the global one.
    await harness.feed(callback_update(control, f"profrules:{ivan_profile.id}"))
    assert "• never use emojis" in harness.bot_texts()[-1]
    await harness.feed(text_update(control, "-1"))
    await harness.feed(text_update(control, "-2"))
    assert "There is no rule 2." in harness.bot_texts()[-1]
    await harness.feed(text_update(control, "/done"))
    assert rule_texts(await profile(session_factory, user, ivan_profile.id)) == []
    assert rule_texts(await profile(session_factory, user, global_profile.id)) == [
        "never use emojis"
    ]

    # The global rule is replaced: the next draft for Ivan follows at once, no rebuild.
    await harness.feed(callback_update(control, f"profrules:{global_profile.id}"))
    await harness.feed(text_update(control, "-1"))
    await harness.feed(text_update(control, "be brief"))
    await harness.feed(text_update(control, "/done"))
    await harness.incoming(user, chat, ivan, "Synthetic question 2")
    await harness.tick()
    assert len(fake_llm.calls_for("compose.draft")) == 2
    assert _draft_rules(fake_llm) == [("be brief", "global")]
    # Nothing was copied into Ivan's row.
    assert rule_texts(await profile(session_factory, user, ivan_profile.id)) == []
    assert (await profile(session_factory, user, ivan_profile.id)).version == 1


async def test_contact_builds_learn_without_global_rules(
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
    """A contact build sees only the contact's own rules: the global ones are applied at draft
    time, so the learned contact style never bakes them in (removing a global rule must take
    effect without a rebuild)."""
    user = await make_user(state=UserState.active)
    ivan = await make_contact(user, display_name="Ivan")
    anna = await make_contact(user, display_name="Anna")
    chat = await make_chat(user, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    await add_profile(session_factory, user, user_rules=["never use emojis", "no sign-off"])
    await add_profile(session_factory, user, contact=ivan, user_rules=["reply in English"])
    clock.advance(days=2)
    await add_own_messages(session_factory, user, chat, 14, at=clock() - timedelta(days=1))
    fake_llm.set_response("profile.build", by_scope(style_output(), style_output()))

    await refresh_profiles(session_factory, llm_router, user.id, settings=settings, clock=clock)
    builds = {scope_of(call): call for call in fake_llm.calls_for("profile.build")}
    assert _build_rules(builds["global"]) == [("never use emojis", None), ("no sign-off", None)]
    assert _build_rules(builds["contact"]) == [("reply in English", None)]
    current = {
        p.scope: p for p in await profiles_of(session_factory, user, state=ProfileState.current)
    }
    assert current[ProfileScope.contact].version == 2
    assert rule_texts(current[ProfileScope.contact]) == ["reply in English"]

    # A contact's first profile (a new lineage): no rules sent, none stored.
    fake_llm.calls.clear()
    samples = [
        StyleSample(text=f"Synthetic own message {n}", sent_at=clock() - timedelta(hours=n))
        for n in range(12)
    ]
    built = await build_contact_profile(
        session_factory, llm_router, user.id, anna, samples, settings=settings, clock=clock
    )
    assert built is not None and built.user_rules_json == {}
    (call,) = fake_llm.calls_for("profile.build")
    assert _build_rules(call) == []


async def test_accepting_a_rule_the_global_profile_has_adds_nothing(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """The user sets the rule globally after it was proposed for Anna: accepting the proposal
    adds nothing to Anna's profile and writes no "rule added" record. Once the global rule is
    removed, the next edit proposes it for Anna again, although the earlier evidence drafts are
    still in the window."""
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    anna = await make_contact(user, display_name="Anna")
    chat = await make_chat(user, telegram_chat_id=anna.telegram_peer_id, title="Anna")
    global_profile = await add_profile(session_factory, user)
    anna_profile = await add_profile(session_factory, user, contact=anna)
    await _edit_away_emojis(harness, fake_llm, clock, user, chat, anna, rounds=range(1, 6))
    proposed = [t for t in harness.bot_texts() if t.startswith("💡")]
    assert proposed == [
        "💡 I noticed you removed emojis in 5 of your last 5 edits for Anna.\n"
        'Add rule "no emojis" to Anna\'s profile?'
    ]
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
    assert len(proposal.evidence_draft_ids) == 5
    await harness.feed(callback_update(control, f"profrules:{global_profile.id}"))
    await harness.feed(text_update(control, "No emojis"))
    await harness.feed(text_update(control, "/done"))

    await harness.feed(callback_update(control, f"rule_accept:{proposal.id}"))

    assert rule_texts(await profile(session_factory, user, anna_profile.id)) == []
    async with session_factory() as session:
        closed = await profiles_repo.get_proposal(session, user.id, proposal.id)
        kinds = (
            (
                await session.execute(
                    select(ActivityRecord.kind).where(ActivityRecord.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
    assert closed is not None and closed.state != ProposalState.proposed
    assert ActivityKind.rule_accepted not in kinds
    assert harness.api.of_type(EditMessageText)[-1].text == (
        '✅ "no emojis" is already one of your global rules, so it applies to Anna too. '
        "Nothing was added."
    )

    # The global rule is removed; the next edit for Anna (the 5 earlier ones still count)
    # proposes it again.
    await harness.feed(callback_update(control, f"profrules:{global_profile.id}"))
    await harness.feed(text_update(control, "-1"))
    await harness.feed(text_update(control, "/done"))
    await _edit_away_emojis(harness, fake_llm, clock, user, chat, anna, rounds=range(6, 7))
    assert [t for t in harness.bot_texts() if t.startswith("💡")] == [
        *proposed,
        "💡 I noticed you removed emojis in 6 of your last 6 edits for Anna.\n"
        'Add rule "no emojis" to Anna\'s profile?',
    ]


async def test_typing_a_global_rule_for_a_contact_adds_nothing(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    anna = await make_contact(user, display_name="Anna")
    await add_profile(session_factory, user, user_rules=["never use emojis"])
    anna_profile = await add_profile(session_factory, user, contact=anna)

    await harness.feed(callback_update(control, f"profrules:{anna_profile.id}"))
    await harness.feed(text_update(control, "Never  use EMOJIS"))
    assert harness.bot_texts()[-1] == (
        '"Never use EMOJIS" is already one of your global rules, so it applies to Anna too. '
        "Nothing was added."
    )
    await harness.feed(text_update(control, "reply in English"))
    await harness.feed(text_update(control, "/done"))
    assert rule_texts(await profile(session_factory, user, anna_profile.id)) == ["reply in English"]


async def test_typing_the_localized_label_of_a_global_rule_adds_nothing(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    """An accepted proposal is stored in English and shown localized ("без эмодзи"): typing the
    label the user sees is the same rule."""
    user = await make_user(state=UserState.active, language="ru")
    control = user.telegram_control_chat_id
    anna = await make_contact(user, display_name="Anna")
    global_profile = await add_profile(session_factory, user)
    anna_profile = await add_profile(session_factory, user, contact=anna)
    async with session_factory() as session:
        stored = await profiles_repo.get_profile(session, user.id, global_profile.id)
        assert stored is not None
        stored.user_rules_json = {
            "rules": [{"text": "no emojis", "source": "proposal", "key": "emoji_removed"}]
        }
        await session.commit()

    await harness.feed(callback_update(control, f"profrules:{anna_profile.id}"))
    assert "• без эмодзи" in harness.bot_texts()[-1]
    await harness.feed(text_update(control, "Без  эмодзи"))
    assert harness.bot_texts()[-1] == (
        "Правило «Без эмодзи» уже есть в общем профиле, поэтому действует и для контакта Anna. "
        "Ничего не добавлено."
    )
    assert rule_texts(await profile(session_factory, user, anna_profile.id)) == []


async def _set_rules(
    session_factory: async_sessionmaker[AsyncSession], user: User, profile_id: Any, *rules: Any
) -> None:
    async with session_factory() as session:
        stored = await profiles_repo.get_profile(session, user.id, profile_id)
        assert stored is not None
        stored.user_rules_json = {"rules": list(rules)}
        await session.commit()


_KEYED_NO_EMOJIS = {"text": "no emojis", "source": "proposal", "key": "emoji_removed"}


async def test_ru_global_rule_typed_as_the_label_blocks_the_proposal_for_a_contact(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """(a) A ru user typed "без эмодзи" globally: the "no emojis" proposal is the same rule in
    their language, so it is not proposed for Ivan, and accepting an open one adds nothing."""
    user = await make_user(state=UserState.active, language="ru")
    control = user.telegram_control_chat_id
    ivan = await make_contact(user, display_name="Ivan")
    chat = await make_chat(user, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    await add_profile(session_factory, user, user_rules=["без эмодзи"])
    ivan_profile = await add_profile(session_factory, user, contact=ivan)

    await _edit_away_emojis(harness, fake_llm, clock, user, chat, ivan)
    assert not [t for t in harness.bot_texts() if t.startswith("💡")]

    async with session_factory() as session:
        proposal = await profiles_repo.create_proposal(
            session,
            user.id,
            style_profile_id=ivan_profile.id,
            rule_json={"key": "emoji_removed", "text": "no emojis"},
            evidence_draft_ids=[],
        )
        await session.commit()
    await harness.feed(callback_update(control, f"rule_accept:{proposal.id}"))
    assert rule_texts(await profile(session_factory, user, ivan_profile.id)) == []
    assert harness.api.of_type(EditMessageText)[-1].text == (
        "✅ Правило «без эмодзи» уже есть в общем профиле, поэтому действует и для контакта "
        "Ivan. Ничего не добавлено."
    )


async def test_ru_contact_rule_and_global_keyed_rule_reach_the_model_once(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    """(b) Ivan's typed "без эмодзи" and the global accepted "no emojis" are one rule for a ru
    user: listed once, as Ivan's, in the draft request and in /profile."""
    user = await make_user(state=UserState.active, language="ru")
    control = user.telegram_control_chat_id
    ivan = await make_contact(user, display_name="Ivan")
    chat = await make_chat(user, telegram_chat_id=ivan.telegram_peer_id, title="Ivan")
    global_profile = await add_profile(session_factory, user)
    await _set_rules(session_factory, user, global_profile.id, _KEYED_NO_EMOJIS)
    ivan_profile = await add_profile(session_factory, user, contact=ivan, user_rules=["без эмодзи"])

    await harness.incoming(user, chat, ivan, "Synthetic question")
    await harness.tick()
    assert _draft_rules(fake_llm) == [("без эмодзи", "contact")]
    await harness.feed(callback_update(control, f"profview:{ivan_profile.id}"))
    view = harness.messages()[-1].text
    assert "1. без эмодзи" in view and "Общие правила" not in view


async def test_ru_typing_the_label_of_an_own_keyed_rule_is_a_duplicate(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    """(c) The global profile's own accepted "no emojis" is shown as "без эмодзи"; typing that
    label in its Edit rules does not append a second rule."""
    user = await make_user(state=UserState.active, language="ru")
    control = user.telegram_control_chat_id
    global_profile = await add_profile(session_factory, user)
    await _set_rules(session_factory, user, global_profile.id, _KEYED_NO_EMOJIS)

    await harness.feed(callback_update(control, f"profrules:{global_profile.id}"))
    await harness.feed(text_update(control, "Без  эмодзи"))
    assert rule_texts(await profile(session_factory, user, global_profile.id)) == ["no emojis"]


async def test_accepting_a_proposal_over_the_rule_limit_keeps_it_open(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    anna = await make_contact(user, display_name="Anna")
    await add_profile(session_factory, user)
    full = [f"synthetic rule {n}" for n in range(30)]
    anna_profile = await add_profile(session_factory, user, contact=anna, user_rules=full)
    async with session_factory() as session:
        proposal = await profiles_repo.create_proposal(
            session,
            user.id,
            style_profile_id=anna_profile.id,
            rule_json={"key": "emoji_removed", "text": "no emojis"},
            evidence_draft_ids=[],
        )
        await session.commit()

    await harness.feed(callback_update(control, f"rule_accept:{proposal.id}"))

    (answer,) = harness.api.of_type(AnswerCallbackQuery)
    assert answer.text == "A profile can have at most 30 rules; remove one first."
    assert rule_texts(await profile(session_factory, user, anna_profile.id)) == full
    async with session_factory() as session:
        still = await profiles_repo.get_proposal(session, user.id, proposal.id)
    assert still is not None and still.state == ProposalState.proposed


async def test_long_rule_lists_fit_a_telegram_message(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    control = user.telegram_control_chat_id
    anna = await make_contact(user, display_name="Anna")
    await add_profile(
        session_factory, user, user_rules=[f"global {n:02d} " + "y" * 190 for n in range(30)]
    )
    anna_profile = await add_profile(
        session_factory,
        user,
        contact=anna,
        user_rules=[f"own {n:02d} " + "x" * 193 for n in range(29)],
    )
    await harness.feed(callback_update(control, f"profview:{anna_profile.id}"))
    await harness.feed(callback_update(control, f"profrules:{anna_profile.id}"))
    await harness.feed(text_update(control, "z" * 200))  # rule 30: the "Saved" reply
    texts = harness.bot_texts()
    assert len(texts) >= 3 and all(len(text) <= TELEGRAM_TEXT_MAX for text in texts)
    assert texts[-1].startswith("✅ Saved.") and "…and 30 more global rules" in texts[-1]
    assert len(rule_texts(await profile(session_factory, user, anna_profile.id))) == 30


async def test_rule_set_globally_is_not_proposed_for_a_contact(
    harness: US2Harness,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user = await make_user(state=UserState.active)
    anna = await make_contact(user, display_name="Anna")
    chat = await make_chat(user, telegram_chat_id=anna.telegram_peer_id, title="Anna")
    await add_profile(session_factory, user, user_rules=["No emojis"])
    await add_profile(session_factory, user, contact=anna)
    await _edit_away_emojis(harness, fake_llm, clock, user, chat, anna)

    assert not [t for t in harness.bot_texts() if t.startswith("💡")]
    async with session_factory() as session:
        proposals = (
            await session.execute(
                select(StyleRuleProposal).where(StyleRuleProposal.user_id == user.id)
            )
        ).all()
    assert proposals == []
