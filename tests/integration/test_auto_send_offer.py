"""specs/014 US2 and US3: the decisions the user's actions leave behind, the offer of auto-send for
a chat the user keeps sending unchanged (FR-013..FR-018) and the suggestion to turn it off when the
user keeps stepping in (FR-020). Real database, fake gateway, frozen clock; synthetic data only."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    AutoSendOfferState,
    AutoSendSource,
    Contact,
    Draft,
    DraftDecisionKind,
    DraftState,
    User,
    UserState,
    WorkChat,
)
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.sending.auto_send import AutoSendAdvisor
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import (
    US2Harness,
    arm_chat,
    button_texts,
    callback_datas,
    decision_rows,
    seed_auto_drafts,
    seed_decisions,
)
from tests.integration.us4_support import add_profile

REPLY = "Synthetic reply about the report"
OFFER_MARK = "in the last 30 days. Send drafts in this chat automatically?"
WARN_MARK = "automatic drafts. Turn auto-send off for this chat?"
KIND = DraftDecisionKind


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
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    return US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _setup(
    harness: US2Harness,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    *,
    title: str = "Synthetic Anna",
    armed: bool = False,
    user: User | None = None,
) -> tuple[User, WorkChat, Contact]:
    if user is None:
        user = await make_user(state=UserState.active, delay_min_s=0, delay_max_s=0)
        await add_profile(harness.session_factory, user)  # no weak_style_profile warning
    contact = await make_contact(user, display_name=title)
    chat = await make_chat(user, title=title, telegram_chat_id=contact.telegram_peer_id)
    if armed:
        await arm_chat(harness, chat)
    return user, chat, contact


async def _new_draft(harness: US2Harness, user: User, chat: WorkChat, contact: Contact) -> Draft:
    """A message arrives and its card is posted; returns the new pending draft."""
    before = {d.id for d in await harness.drafts(user)}
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    (draft,) = [d for d in await harness.drafts(user) if d.id not in before]
    return draft


async def _press(harness: US2Harness, user: User, data: str) -> None:
    await harness.feed(callback_update(user.telegram_control_chat_id, data))


async def _approve(harness: US2Harness, user: User, chat: WorkChat, contact: Contact) -> Draft:
    draft = await _new_draft(harness, user, chat, contact)
    await _press(harness, user, f"approve:{draft.id}")
    return draft


def _with(harness: US2Harness, mark: str) -> list[SendMessage]:
    return [m for m in harness.messages() if mark in (m.text or "")]


async def _chat(harness: US2Harness, chat: WorkChat) -> WorkChat:
    async with harness.session_factory() as session:
        row = await session.get(WorkChat, chat.id)
        assert row is not None
        await session.refresh(row)
        return row


async def _user(harness: US2Harness, user: User) -> User:
    async with harness.session_factory() as session:
        row = await session.get(User, user.id)
        assert row is not None
        await session.refresh(row)
        return row


async def _activities(harness: US2Harness, user: User, kind: ActivityKind) -> list[ActivityRecord]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ActivityRecord)
            .where(ActivityRecord.user_id == user.id, ActivityRecord.kind == kind)
            .order_by(ActivityRecord.at, ActivityRecord.created_at)
        )
        return list(result.scalars().all())


def _kinds(rows: list[Any]) -> list[DraftDecisionKind]:
    return [row.decision for row in rows]


# --- the decisions the user's actions leave behind (FR-013) ---------------------------------------


async def test_us2_approving_unchanged_writes_sent_unchanged(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    draft = await _approve(harness, user, chat, contact)
    (row,) = await decision_rows(harness, chat)
    assert (row.decision, row.draft_id, row.during_auto_send) == (
        KIND.sent_unchanged,
        draft.id,
        False,
    )


async def test_us2_edit_and_send_writes_sent_edited(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    draft = await _new_draft(harness, user, chat, contact)
    await _press(harness, user, f"edit:{draft.id}")
    await harness.feed(text_update(user.telegram_control_chat_id, "Synthetic edited reply"))
    assert _kinds(await decision_rows(harness, chat)) == [KIND.sent_edited]


async def test_us2_reject_writes_rejected_and_regen_writes_regenerated_for_the_old_version(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    first = await _new_draft(harness, user, chat, contact)
    await _press(harness, user, f"regen:{first.id}")
    (row,) = await decision_rows(harness, chat)
    assert (row.decision, row.draft_id, row.during_auto_send) == (
        KIND.regenerated,
        first.id,
        False,
    )
    second = next(d for d in await harness.drafts(user) if d.version == 2)
    await _press(harness, user, f"reject:{second.id}")
    rows = await decision_rows(harness, chat)
    assert _kinds(rows) == [KIND.regenerated, KIND.rejected]
    assert rows[1].draft_id == second.id


async def test_us2_expired_and_dropped_drafts_write_no_decision(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    draft = await _new_draft(harness, user, chat, contact)
    clock.advance(days=1)  # Thursday: still a working day
    await harness.tick()
    assert (await harness.drafts(user))[0].state == DraftState.expired
    await harness.manual_reply(user, chat)  # a manual reply drops whatever is waiting
    assert await decision_rows(harness, chat) == []
    assert draft.id is not None


# --- the offer (FR-015..FR-018) -------------------------------------------------------------------


async def test_us2_the_twentieth_decision_posts_exactly_one_offer(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    await seed_decisions(harness, user, chat, unchanged=12, edited=7)
    assert _with(harness, OFFER_MARK) == []  # seeding is not a decision the advisor sees

    await _approve(harness, user, chat, contact)  # the 20th, the 13th unchanged
    (offer,) = _with(harness, OFFER_MARK)
    assert "In “Synthetic Anna” you sent 13 of 20 drafts unchanged" in offer.text
    assert "within 2 min" in offer.text
    assert callback_datas(offer.reply_markup) == [
        f"autooffer:{chat.id}:on",
        f"autooffer:{chat.id}:later",
        f"autooffer:{chat.id}:never",
    ]
    (activity,) = await _activities(harness, user, ActivityKind.auto_send_offered)
    assert activity.meta_json == {"work_chat_id": str(chat.id), "unchanged": 13, "total": 20}
    stored = await _chat(harness, chat)
    assert stored.auto_send_offer == AutoSendOfferState.offered
    assert stored.auto_send_offer_not_before == clock() + timedelta(days=30)
    assert (await _user(harness, user)).auto_send_last_offer_at == clock()

    await _approve(harness, user, chat, contact)  # the 21st
    assert len(_with(harness, OFFER_MARK)) == 1


async def test_us2_sixty_percent_exactly_is_not_offered(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    await seed_decisions(harness, user, chat, unchanged=11, edited=8)
    await _approve(harness, user, chat, contact)  # 12 of 20
    assert _with(harness, OFFER_MARK) == []
    assert await _activities(harness, user, ActivityKind.auto_send_offered) == []


async def test_us2_decisions_older_than_30_days_do_not_count(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    await seed_decisions(
        harness, user, chat, unchanged=19, at=clock() - timedelta(days=30, seconds=1)
    )
    await _approve(harness, user, chat, contact)
    assert _with(harness, OFFER_MARK) == []


async def test_us2_not_now_snoozes_for_a_month(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    await seed_decisions(harness, user, chat, unchanged=19)
    await _approve(harness, user, chat, contact)
    await _press(harness, user, f"autooffer:{chat.id}:later")
    assert harness.edits()[-1].text == "OK, I won't ask about this chat for a month."
    (answered,) = await _activities(harness, user, ActivityKind.auto_send_offer_answered)
    assert answered.meta_json == {"work_chat_id": str(chat.id), "answer": "later"}
    assert (await _chat(harness, chat)).auto_send_offer_not_before == clock() + timedelta(days=30)

    await seed_decisions(harness, user, chat, unchanged=20)
    await _approve(harness, user, chat, contact)
    assert len(_with(harness, OFFER_MARK)) == 1  # the snooze holds

    clock.advance(days=30, seconds=1)
    await seed_decisions(harness, user, chat, unchanged=19)
    await _approve(harness, user, chat, contact)
    assert len(_with(harness, OFFER_MARK)) == 2


async def test_us2_never_stops_offers_for_good(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    await seed_decisions(harness, user, chat, unchanged=19)
    await _approve(harness, user, chat, contact)
    await _press(harness, user, f"autooffer:{chat.id}:never")
    assert (await _chat(harness, chat)).auto_send_offer == AutoSendOfferState.opted_out
    assert "won't offer auto-send for this chat again" in (harness.edits()[-1].text or "")

    clock.advance(days=90)
    await seed_decisions(harness, user, chat, unchanged=25)
    await _approve(harness, user, chat, contact)
    assert len(_with(harness, OFFER_MARK)) == 1


async def test_us2_offers_can_be_switched_back_on_from_the_chat_screen(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    await _press(harness, user, f"chatcfg:{chat.id}")
    assert f"chatauto:{chat.id}:reset" not in callback_datas(harness.messages()[-1].reply_markup)

    await seed_decisions(harness, user, chat, unchanged=19)
    await _approve(harness, user, chat, contact)
    await _press(harness, user, f"autooffer:{chat.id}:never")
    await _press(harness, user, f"chatcfg:{chat.id}")
    screen = harness.messages()[-1]
    assert f"chatauto:{chat.id}:reset" in callback_datas(screen.reply_markup)
    assert "🔔 Offer auto-send again" in button_texts(screen.reply_markup)

    await _press(harness, user, f"chatauto:{chat.id}:reset")
    stored = await _chat(harness, chat)
    assert stored.auto_send_offer == AutoSendOfferState.none
    assert stored.auto_send_offer_not_before is None

    clock.advance(days=2)
    await seed_decisions(harness, user, chat, unchanged=20)
    await _approve(harness, user, chat, contact)
    assert len(_with(harness, OFFER_MARK)) == 2


async def test_us2_turn_on_asks_for_the_confirmation_then_enables_from_the_offer(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    await seed_decisions(harness, user, chat, unchanged=19)
    await _approve(harness, user, chat, contact)
    await _press(harness, user, f"autooffer:{chat.id}:on")
    shown = harness.edits()[-1]
    assert "Auto-send for “Synthetic Anna”" in (shown.text or "")
    assert f"autoconfirm:{chat.id}:offer" in callback_datas(shown.reply_markup)
    assert not (await _chat(harness, chat)).auto_send  # asking changes nothing

    await _press(harness, user, f"autoconfirm:{chat.id}:offer")
    stored = await _chat(harness, chat)
    assert stored.auto_send and stored.auto_send_source == AutoSendSource.offer
    (answered,) = await _activities(harness, user, ActivityKind.auto_send_offer_answered)
    assert answered.meta_json == {"work_chat_id": str(chat.id), "answer": "on"}
    (on,) = await _activities(harness, user, ActivityKind.auto_send_on)
    assert on.meta_json == {"work_chat_id": str(chat.id), "source": "offer"}


async def test_us2_two_chats_qualifying_the_same_day_get_one_offer(
    harness: US2Harness,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, anna, anna_contact = await _setup(harness, make_user, make_chat, make_contact)
    _, boris, boris_contact = await _setup(
        harness, make_user, make_chat, make_contact, title="Synthetic Boris", user=user
    )
    await seed_decisions(harness, user, anna, unchanged=19)
    await seed_decisions(harness, user, boris, unchanged=19)
    await _approve(harness, user, anna, anna_contact)
    await _approve(harness, user, boris, boris_contact)
    (offer,) = _with(harness, OFFER_MARK)
    assert "Synthetic Anna" in offer.text
    assert (await _chat(harness, boris)).auto_send_offer == AutoSendOfferState.none


async def test_us2_an_advisor_failure_is_logged_and_the_action_still_succeeds(
    harness: US2Harness,
    log_capture: LogCapture,
    monkeypatch: pytest.MonkeyPatch,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    async def broken(self: AutoSendAdvisor, user_id: Any, work_chat_id: Any) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(AutoSendAdvisor, "_lease", broken)
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    draft = await _approve(harness, user, chat, contact)
    assert (await harness.drafts(user))[0].state == DraftState.sent
    (event,) = log_capture.events("autosend_advisor_failed")
    assert event["user_id"] == str(user.id) and event["work_chat_id"] == str(chat.id)
    assert event["error"] == "RuntimeError"
    assert draft.id is not None


# --- the suggestion to turn auto-send off (FR-020) ------------------------------------------------

AUTO = (KIND.auto_sent,)
CANCELLED = (KIND.auto_cancelled, KIND.sent_unchanged)
EDITED = (KIND.sent_edited,)
REJECTED = (KIND.rejected,)
EARLY = (KIND.sent_unchanged,)  # Send now without changes: not an intervention


async def _cancel_new(
    harness: US2Harness, user: User, chat: WorkChat, contact: Contact, *, finish: bool = False
) -> Draft:
    """A new armed draft whose countdown the user cancels; ``finish``: and then rejects it, so the
    next message starts a new turn."""
    draft = await _new_draft(harness, user, chat, contact)
    assert draft.auto_send_due_at is not None
    await _press(harness, user, f"autostop:{draft.id}")
    if finish:
        await _press(harness, user, f"reject:{draft.id}")
    return draft


async def test_us3_five_interventions_in_ten_suggest_turning_it_off(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact, armed=True)
    nine = [AUTO, CANCELLED, AUTO, EDITED, AUTO, REJECTED, AUTO, AUTO, CANCELLED]  # 4 interventions
    await seed_auto_drafts(harness, user, chat, contact, nine)
    assert _with(harness, WARN_MARK) == []
    await _cancel_new(harness, user, chat, contact)  # the 10th draft, the 5th intervention
    (warn,) = _with(harness, WARN_MARK)
    assert "you stopped or changed 5 of the last 10 automatic drafts" in warn.text
    assert callback_datas(warn.reply_markup) == [
        f"chatauto:{chat.id}:off",
        f"autowarn:{chat.id}:keep",
    ]
    assert (await _chat(harness, chat)).auto_send_warned_at == clock()


async def test_us3_four_interventions_in_ten_do_not(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact, armed=True)
    nine = [AUTO, AUTO, CANCELLED, AUTO, EDITED, AUTO, AUTO, REJECTED, AUTO]  # 3 interventions
    await seed_auto_drafts(harness, user, chat, contact, nine)
    await _cancel_new(harness, user, chat, contact)
    assert _with(harness, WARN_MARK) == []


async def test_us3_sending_early_unchanged_is_not_an_intervention(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact, armed=True)
    await seed_auto_drafts(
        harness, user, chat, contact, [EARLY] * 6 + [CANCELLED, CANCELLED, CANCELLED]
    )
    await _cancel_new(harness, user, chat, contact)  # 4 cancelled in all, 6 sent early
    assert _with(harness, WARN_MARK) == []


async def test_us3_fewer_than_ten_auto_send_drafts_never_warn(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact, armed=True)
    await seed_auto_drafts(
        harness, user, chat, contact, [CANCELLED, EDITED, REJECTED, CANCELLED, AUTO]
    )
    await _cancel_new(harness, user, chat, contact)  # 5 interventions among 6 drafts
    assert _with(harness, WARN_MARK) == []


async def test_us3_the_suggestion_comes_at_most_once_a_week(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact, armed=True)
    await seed_auto_drafts(harness, user, chat, contact, [CANCELLED] * 5 + [AUTO] * 4)
    await _cancel_new(harness, user, chat, contact, finish=True)
    assert len(_with(harness, WARN_MARK)) == 1

    clock.advance(days=1)  # Thursday: still a working day
    await _cancel_new(harness, user, chat, contact, finish=True)
    assert len(_with(harness, WARN_MARK)) == 1

    clock.advance(days=6)  # a week after the first one
    await _cancel_new(harness, user, chat, contact, finish=True)
    assert len(_with(harness, WARN_MARK)) == 2


async def test_us3_turn_off_from_the_suggestion_and_keep_it_on(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact, armed=True)
    await seed_auto_drafts(harness, user, chat, contact, [CANCELLED] * 5 + [AUTO] * 4)
    await _cancel_new(harness, user, chat, contact)

    await _press(harness, user, f"autowarn:{chat.id}:keep")
    assert harness.edits()[-1].text == "OK, auto-send stays on for this chat."
    assert (await _chat(harness, chat)).auto_send

    await _press(harness, user, f"chatauto:{chat.id}:off")
    assert not (await _chat(harness, chat)).auto_send
