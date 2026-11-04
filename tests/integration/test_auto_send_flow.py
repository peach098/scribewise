"""specs/014 US1: arming after the card is posted, the cancel window, every way to stop it, and
the race with a tap (SC-001, SC-002). Real database, fake gateway, frozen clock; synthetic texts
only."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    AutoSendStop,
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
from scribe.scheduling.scheduler import Scheduler
from scribe.sending import auto_send
from scribe.sending.auto_send import AutoSender
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FROZEN_NOW, FrozenClock, LogCapture, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import draft_output, grounded_reply, select_all_history
from tests.integration.us2_support import (
    US2Harness,
    arm_chat,
    callback_datas,
    decision_rows,
)
from tests.integration.us4_support import add_profile

REPLY = "Synthetic reply about the report"
WINDOW = timedelta(seconds=120)
ARMED_LINE = "⏱ Will be sent automatically at"
MANUAL_LINE = "✋ Needs your approval:"
STOPPED_LINE = "⏹ Automatic sending cancelled"
SENT_AUTO = "✅ Sent automatically at"


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
    armed: bool = True,
    weak_style: bool = False,
    **user_values: Any,
) -> tuple[User, WorkChat, Contact]:
    user_values.setdefault("delay_min_s", 0)
    user_values.setdefault("delay_max_s", 0)
    user = await make_user(state=UserState.active, **user_values)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    # A built style profile: without one every draft carries weak_style_profile and waits.
    await add_profile(harness.session_factory, user, weak=weak_style)
    if armed:
        await arm_chat(harness, chat)
    return user, chat, contact


async def _drafted(
    harness: US2Harness,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
    *,
    armed: bool = True,
    weak_style: bool = False,
    **user_values: Any,
) -> tuple[User, WorkChat, Contact, Draft]:
    """A user with a pending draft in a chat, whose card was just posted."""
    user, chat, contact = await _setup(
        harness,
        make_user,
        make_chat,
        make_contact,
        armed=armed,
        weak_style=weak_style,
        **user_values,
    )
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    return user, chat, contact, draft


async def _draft(harness: US2Harness, user: User, version: int = 1) -> Draft:
    return next(d for d in await harness.drafts(user) if d.version == version)


def _local(moment: datetime, user: User) -> str:
    return f"{moment.astimezone(ZoneInfo(user.timezone)):%H:%M}"


def _card(harness: US2Harness) -> SendMessage:
    return harness.messages()[-1]


def _last_edit(harness: US2Harness) -> EditMessageText:
    return harness.edits()[-1]


def _edit_text(harness: US2Harness) -> str:
    return _last_edit(harness).text or ""


def _popups(harness: US2Harness) -> list[str]:
    return [a.text or "" for a in harness.api.of_type(AnswerCallbackQuery)]


async def _activities(
    harness: US2Harness, user: User, *kinds: ActivityKind
) -> list[ActivityRecord]:
    async with harness.session_factory() as session:
        result = await session.execute(
            select(ActivityRecord)
            .where(ActivityRecord.user_id == user.id, ActivityRecord.kind.in_(kinds))
            .order_by(ActivityRecord.at, ActivityRecord.created_at)
        )
        return list(result.scalars().all())


async def _press(harness: US2Harness, user: User, data: str) -> None:
    await harness.feed(callback_update(user.telegram_control_chat_id, data))


async def _set(harness: US2Harness, statement: str, **params: Any) -> None:
    async with harness.session_factory() as session:
        await session.execute(text(statement), params)
        await session.commit()


async def _run_window(harness: US2Harness, clock: FrozenClock) -> None:
    clock.advance(seconds=WINDOW.total_seconds())
    await harness.tick()


def _ordinary_keyboard(draft_id: Any) -> list[str]:
    return [
        f"approve:{draft_id}",
        f"edit:{draft_id}",
        f"regen:{draft_id}",
        f"reject:{draft_id}",
        f"sources:{draft_id}",
    ]


# --- arming ---------------------------------------------------------------------------------------


async def test_chat_without_auto_send_is_never_armed(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact, draft = await _drafted(
        harness, make_user, make_chat, make_contact, armed=False
    )
    assert draft.auto_send_due_at is None
    card = _card(harness)
    assert ARMED_LINE not in card.text and MANUAL_LINE not in card.text
    assert callback_datas(card.reply_markup) == _ordinary_keyboard(draft.id)
    async with harness.session_factory() as session:
        assert await auto_send.plan_arm(session, draft_id=draft.id, now=FROZEN_NOW) is None

    harness.clock.advance(hours=1)
    await harness.tick()
    assert harness.gateway.send_text_calls == []


async def test_eligible_draft_is_armed_with_the_card(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    assert draft.auto_send_due_at == FROZEN_NOW + WINDOW
    assert draft.control_message_id is not None
    card = _card(harness)
    assert f"{ARMED_LINE} {_local(FROZEN_NOW + WINDOW, user)}" in card.text
    assert callback_datas(card.reply_markup) == [
        f"approve:{draft.id}",
        f"edit:{draft.id}",
        f"autostop:{draft.id}",
        f"regen:{draft.id}",
        f"reject:{draft.id}",
        f"sources:{draft.id}",
    ]
    assert [a.kind for a in await _activities(harness, user, ActivityKind.auto_approved)] == []


async def test_ineligible_draft_in_an_auto_send_chat_waits_for_the_user(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    promise = {
        "creates_commitment": {"value": True, "what": "send the file", "due": None},
        "sensitive": {"value": False, "categories": []},
    }
    fake_llm.set_response("compose.draft", grounded_reply(REPLY, flags=promise))
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    assert draft.auto_send_due_at is None
    card = _card(harness)
    assert f"{MANUAL_LINE} new promise" in card.text
    assert ARMED_LINE not in card.text
    assert callback_datas(card.reply_markup) == _ordinary_keyboard(draft.id)

    harness.clock.advance(hours=1)
    await harness.tick()
    assert harness.gateway.send_text_calls == []


async def test_draft_in_a_weak_style_profile_waits_for_the_user(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact, draft = await _drafted(
        harness, make_user, make_chat, make_contact, weak_style=True
    )
    assert draft.auto_send_due_at is None
    assert f"{MANUAL_LINE} style not learned yet" in _card(harness).text


# --- firing ---------------------------------------------------------------------------------------


async def test_sent_when_the_window_ends_and_not_before(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    clock.advance(seconds=119)
    await harness.tick()
    assert gateway.send_text_calls == []

    clock.advance(seconds=1)
    await harness.tick()
    (call,) = gateway.send_text_calls
    assert (call.chat_id, call.text) == (chat.telegram_chat_id, REPLY)

    sent = await _draft(harness, user)
    assert sent.state == DraftState.sent
    assert sent.auto_approved_at == FROZEN_NOW + WINDOW
    assert sent.approved_by_user_at is None  # the user's approval is never faked
    activities = await _activities(
        harness, user, ActivityKind.auto_approved, ActivityKind.approved, ActivityKind.sent
    )
    assert [a.kind for a in activities] == [ActivityKind.auto_approved, ActivityKind.sent]
    assert activities[1].meta_json["auto"] is True
    (decision,) = await decision_rows(harness, chat)
    assert (decision.decision, decision.during_auto_send, decision.draft_id) == (
        DraftDecisionKind.auto_sent,
        True,
        draft.id,
    )
    final = _last_edit(harness)
    assert f"{SENT_AUTO} {_local(FROZEN_NOW + WINDOW, user)}" in (final.text or "")
    assert ARMED_LINE not in (final.text or "")

    clock.advance(minutes=5)
    await harness.tick()
    assert len(gateway.send_text_calls) == 1


async def test_restart_before_the_window_end_still_sends_once(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    clock.advance(seconds=60)
    restarted = Scheduler(session_factory, tick_s=5, clock=clock)
    restarted.on_auto_sends(
        AutoSender(
            session_factory,
            gateway,
            harness.presenter,
            clock=clock,
            marker=None,
            commitments=None,
        )
    )
    await restarted.tick(drain=True)
    assert gateway.send_text_calls == []

    clock.advance(seconds=60)
    await restarted.tick(drain=True)
    await harness.tick()  # the first scheduler finds nothing left to send
    assert len(gateway.send_text_calls) == 1


async def test_window_missed_by_a_long_outage_is_not_sent_late(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    clock.advance(minutes=45)  # the service was down; a stop tap may be waiting in the queue
    await harness.tick()
    assert gateway.send_text_calls == []
    stopped = await _draft(harness, user)
    assert stopped.auto_send_stop == AutoSendStop.ineligible and stopped.state == DraftState.pending


# --- the card must exist first (research A4a, FR-006, SC-001) ----------------------------------


async def test_failed_card_post_never_arms_and_a_later_post_starts_the_countdown(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    harness.api.outcomes = [RuntimeError("telegram unreachable")]
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    draft = await _draft(harness, user)
    assert draft.control_message_id is None and draft.auto_send_due_at is None

    for minutes in (3, 5, 30):  # past any window: still nothing goes out
        clock.advance(minutes=minutes)
        await harness.tick()
    assert gateway.send_text_calls == []

    clock.set(FROZEN_NOW + timedelta(minutes=10))  # stand-in for the card retry (issue #15)
    await harness.presenter.post_card(user.id, draft.id)
    posted_at = clock()
    armed = await _draft(harness, user)
    assert armed.control_message_id is not None
    assert armed.auto_send_due_at == posted_at + WINDOW
    await harness.tick()
    assert gateway.send_text_calls == []

    clock.advance(seconds=120)
    await harness.tick()
    assert len(gateway.send_text_calls) == 1


async def test_slow_post_never_sends_before_the_time_on_the_card(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    original = harness.api.make_request

    async def slow(
        bot: Bot,
        method: Any,
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram BaseSession signature
    ) -> Any:
        if isinstance(method, SendMessage):
            clock.advance(seconds=30)  # the card takes 30 s to post
        return await original(bot, method, timeout)

    harness.api.make_request = slow  # type: ignore[method-assign]
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    draft = await _draft(harness, user)
    card = _card(harness)
    assert _local(FROZEN_NOW + WINDOW, user) in card.text  # promised from before the post
    assert draft.auto_send_due_at == FROZEN_NOW + timedelta(seconds=30) + WINDOW
    assert draft.auto_send_due_at >= FROZEN_NOW + WINDOW


async def test_draft_made_ineligible_while_the_card_is_posted_is_not_armed(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact = await _setup(harness, make_user, make_chat, make_contact)
    original = harness.api.make_request

    async def outdated_meanwhile(
        bot: Bot,
        method: Any,
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram BaseSession signature
    ) -> Any:
        if isinstance(method, SendMessage):  # a new message flags the draft while it is posted
            await _set(harness, "UPDATE drafts SET outdated = true")
        return await original(bot, method, timeout)

    harness.api.make_request = outdated_meanwhile  # type: ignore[method-assign]
    await harness.incoming(user, chat, contact, "Synthetic question: when is the report due?")
    await harness.tick()
    draft = await _draft(harness, user)
    assert draft.control_message_id is not None and draft.auto_send_due_at is None
    assert f"{MANUAL_LINE} new messages" in _edit_text(harness)
    assert ARMED_LINE not in _edit_text(harness)

    harness.clock.advance(hours=1)
    await harness.tick()
    assert gateway.send_text_calls == []


async def test_failing_commit_arm_sends_nothing_and_is_logged(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    monkeypatch: pytest.MonkeyPatch,
    log_capture: LogCapture,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database trouble")

    monkeypatch.setattr(auto_send, "commit_arm", broken)
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    assert draft.control_message_id is not None  # the card itself is kept
    assert draft.auto_send_due_at is None
    (event,) = log_capture.events("auto_send_arm_failed")
    assert event["draft_id"] == str(draft.id) and event["error"] == "RuntimeError"
    assert MANUAL_LINE in _edit_text(harness)

    harness.clock.advance(hours=1)
    await harness.tick()
    assert gateway.send_text_calls == []


async def test_reposting_a_card_does_not_rearm(
    harness: US2Harness, clock: FrozenClock, make_user: MakeUser, make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:  # fmt: skip
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _press(harness, user, f"autostop:{draft.id}")
    stopped = await _draft(harness, user)
    assert stopped.auto_send_stop == AutoSendStop.user_action

    clock.advance(minutes=1)
    await harness.presenter.post_card(user.id, draft.id)  # a second card for the same draft
    again = await _draft(harness, user)
    assert again.auto_send_due_at == stopped.auto_send_due_at
    assert again.auto_send_stop == AutoSendStop.user_action
    assert ARMED_LINE not in _card(harness).text


# --- stop paths (FR-008 to FR-012): none of them sends after the window ------------------------


async def _quiet_after_window(harness: US2Harness, clock: FrozenClock) -> None:
    for _ in range(3):
        clock.advance(seconds=WINDOW.total_seconds())
        await harness.tick()
    assert harness.gateway.send_text_calls == []


async def test_autostop_cancels_the_countdown_and_keeps_the_draft_approvable(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _press(harness, user, f"autostop:{draft.id}")
    stopped = await _draft(harness, user)
    assert stopped.auto_send_stop == AutoSendStop.user_action
    assert stopped.state == DraftState.pending
    assert [d.decision for d in await decision_rows(harness, chat)] == [
        DraftDecisionKind.auto_cancelled
    ]
    edit = _last_edit(harness)
    assert STOPPED_LINE in (edit.text or "")
    assert ARMED_LINE not in (edit.text or "")
    assert callback_datas(edit.reply_markup) == _ordinary_keyboard(draft.id)
    await _quiet_after_window(harness, clock)

    await _press(harness, user, f"approve:{draft.id}")  # still approvable by hand
    assert len(gateway.send_text_calls) == 1
    sent = await _draft(harness, user)
    assert sent.state == DraftState.sent and sent.approved_by_user_at is not None
    assert sent.auto_approved_at is None


async def test_edit_stops_the_countdown_before_asking_for_the_text(
    harness: US2Harness, clock: FrozenClock, make_user: MakeUser, make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:  # fmt: skip
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _press(harness, user, f"edit:{draft.id}")
    assert (await _draft(harness, user)).auto_send_stop == AutoSendStop.user_action
    await _quiet_after_window(harness, clock)


async def test_reject_stops_the_countdown(
    harness: US2Harness, clock: FrozenClock, make_user: MakeUser, make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:  # fmt: skip
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _press(harness, user, f"reject:{draft.id}")
    rejected = await _draft(harness, user)
    assert rejected.state == DraftState.rejected
    assert rejected.auto_send_stop == AutoSendStop.user_action
    await _quiet_after_window(harness, clock)


async def test_regenerated_version_is_not_armed(
    harness: US2Harness, clock: FrozenClock, make_user: MakeUser, make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:  # fmt: skip
    user, chat, contact, v1 = await _drafted(harness, make_user, make_chat, make_contact)
    await _press(harness, user, f"regen:{v1.id}")
    old, new = await harness.drafts(user)
    assert old.state == DraftState.superseded and old.auto_send_stop == AutoSendStop.user_action
    assert new.version == 2 and new.auto_send_due_at is None
    card = _card(harness)
    assert f"{MANUAL_LINE} you're already working on this draft" in card.text
    await _quiet_after_window(harness, clock)


async def test_filled_version_is_not_armed(
    harness: US2Harness,
    fake_llm: FakeAdapter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    def with_details(call: Any) -> Any:
        reply = grounded_reply("Synthetic reply, due [[date]]", needs_info=True)(call)
        return draft_output(
            reply.text,
            sources=[s.model_dump() for s in reply.sources],
            needs_info=True,
            placeholders=[{"marker": "[[date]]", "question": "Which date?"}],
        )

    fake_llm.set_response("compose.draft", with_details)
    user, chat, contact, v1 = await _drafted(harness, make_user, make_chat, make_contact)
    assert v1.auto_send_due_at is None  # missing details: waits for the user from the start
    assert f"{MANUAL_LINE} needs details" in _card(harness).text

    await harness.feed(text_update(user.telegram_control_chat_id, "Friday"))
    old, filled = await harness.drafts(user)
    assert old.state == DraftState.superseded
    assert filled.version == 2 and filled.auto_send_due_at is None
    assert f"{MANUAL_LINE} you're already working on this draft" in _card(harness).text
    await _quiet_after_window(harness, clock)


async def test_send_now_during_the_window_is_the_users_own_approval(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    clock.advance(seconds=30)
    await _press(harness, user, f"approve:{draft.id}")
    assert len(gateway.send_text_calls) == 1
    sent = await _draft(harness, user)
    assert sent.state == DraftState.sent
    assert sent.approved_by_user_at == FROZEN_NOW + timedelta(seconds=30)
    assert sent.auto_approved_at is None
    (decision,) = await decision_rows(harness, chat)
    assert (decision.decision, decision.during_auto_send) == (
        DraftDecisionKind.sent_unchanged,
        True,
    )
    clock.advance(minutes=5)
    await harness.tick()
    assert len(gateway.send_text_calls) == 1


async def test_new_incoming_message_stops_the_countdown(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await harness.incoming(user, chat, contact, "Also, in English please")
    stopped = await _draft(harness, user)
    assert stopped.outdated and stopped.auto_send_stop == AutoSendStop.outdated
    assert "⚠️ New messages arrived" in _edit_text(harness)
    assert ARMED_LINE not in _edit_text(harness)
    await _quiet_after_window(harness, clock)


async def test_manual_reply_in_the_chat_drops_the_draft(
    harness: US2Harness, clock: FrozenClock, make_user: MakeUser, make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:  # fmt: skip
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await harness.manual_reply(user, chat)
    assert (await _draft(harness, user)).state == DraftState.dropped
    await _quiet_after_window(harness, clock)


@pytest.mark.parametrize(
    ("change", "stop", "line"),
    [
        ("UPDATE users SET state = 'paused'", AutoSendStop.paused, "paused"),
        ("UPDATE users SET state = 'out_of_office'", AutoSendStop.ineligible, "out of office"),
        (
            "UPDATE drafts SET possibly_unnecessary = true",
            AutoSendStop.ineligible,
            "reply may not be needed",
        ),
        (
            "UPDATE users SET access_suspended_at = now()",
            AutoSendStop.ineligible,
            "access ended",
        ),
    ],
    ids=["pause", "ooo", "unnecessary", "suspended"],
)
async def test_state_that_changed_during_the_window_is_rechecked_when_it_ends(
    harness: US2Harness,
    clock: FrozenClock,
    change: str,
    stop: AutoSendStop,
    line: str,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _set(harness, change)
    clock.advance(seconds=120)
    await harness.tick()
    assert harness.gateway.send_text_calls == []
    stopped = await _draft(harness, user)
    assert stopped.state == DraftState.pending and stopped.auto_approved_at is None
    assert stopped.auto_send_stop == stop
    assert f"{MANUAL_LINE} {line}" in _edit_text(harness)
    assert [
        a.meta_json["reason"]
        for a in await _activities(harness, user, ActivityKind.auto_send_stopped)
    ] == [stop.value]
    await _quiet_after_window(harness, clock)


async def test_window_ending_outside_working_hours_waits_for_the_user(
    harness: US2Harness,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    clock.set(FROZEN_NOW.replace(hour=17, minute=59))  # work ends at 18:00
    user, chat, contact, draft = await _drafted(
        harness, make_user, make_chat, make_contact, work_end=time(18, 0)
    )
    assert draft.auto_send_due_at == clock() + WINDOW
    clock.advance(seconds=120)
    await harness.tick()
    stopped = await _draft(harness, user)
    assert stopped.auto_send_stop == AutoSendStop.outside_hours
    assert f"{MANUAL_LINE} outside your working hours" in _edit_text(harness)
    await _quiet_after_window(harness, clock)


async def test_turning_the_chat_off_stops_its_countdown(
    harness: US2Harness, clock: FrozenClock, make_user: MakeUser, make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:  # fmt: skip
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _press(harness, user, f"chatauto:{chat.id}:off")
    stopped = await _draft(harness, user)
    assert stopped.auto_send_stop == AutoSendStop.disabled
    assert ARMED_LINE not in _edit_text(harness)
    assert callback_datas(_last_edit(harness).reply_markup) == _ordinary_keyboard(draft.id)
    await _quiet_after_window(harness, clock)


async def test_failed_send_stops_the_countdown_without_a_retry(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    gateway.fail_send_with = RuntimeError("telegram unreachable")
    clock.advance(seconds=120)
    await harness.tick()
    failed = await _draft(harness, user)
    assert failed.state == DraftState.pending
    assert failed.auto_send_stop == AutoSendStop.send_failed
    assert failed.send_random_id is not None
    assert f"{MANUAL_LINE} sending failed" in _edit_text(harness)
    attempts = list(gateway.send_attempt_random_ids)
    assert attempts == [failed.send_random_id]

    gateway.fail_send_with = None
    for _ in range(3):
        clock.advance(minutes=1)
        await harness.tick()
    assert gateway.send_attempt_random_ids == attempts  # no automatic retry
    assert gateway.send_text_calls == []

    await _press(harness, user, f"approve:{draft.id}")  # the user's own Send reuses the key
    (call,) = gateway.send_text_calls
    assert call.random_id == failed.send_random_id
    assert (await _draft(harness, user)).state == DraftState.sent


# --- the race between the tap and the countdown (research A3) -----------------------------------


async def _fire_with_tap_in_between(
    harness: US2Harness, user: User, tap: Callable[[Draft], str]
) -> None:
    """The step has claimed the draft (``auto_approved_at`` set) when the tap arrives."""
    inner = harness.auto_sender

    async def claim_then_tap(session: AsyncSession, drafts: Any) -> Any:
        after_commit = await inner(session, drafts)
        await session.flush()
        for draft in drafts:
            await _press(harness, user, tap(draft))
        return after_commit

    harness.scheduler.on_auto_sends(claim_then_tap)
    harness.clock.advance(seconds=120)
    await harness.tick()


@pytest.mark.parametrize("action", ["autostop", "edit", "reject", "approve"])
async def test_a_tap_after_the_step_claimed_the_draft_is_too_late(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    action: str,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    harness.api.clear()
    await _fire_with_tap_in_between(harness, user, lambda d: f"{action}:{d.id}")
    assert "Already being sent automatically" in _popups(harness)
    assert len(gateway.send_text_calls) == 1  # the countdown won, once
    sent = await _draft(harness, user)
    assert sent.state == DraftState.sent
    assert sent.auto_approved_at is not None and sent.approved_by_user_at is None
    assert sent.auto_send_stop is None
    assert [d.decision for d in await decision_rows(harness, chat)] == [DraftDecisionKind.auto_sent]


@pytest.mark.parametrize("action", ["autostop", "edit", "reject"])
async def test_a_tap_that_committed_first_leaves_nothing_for_the_step(
    harness: US2Harness,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    action: str,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    await _press(harness, user, f"{action}:{draft.id}")
    await _quiet_after_window(harness, clock)
    assert (await _draft(harness, user)).auto_approved_at is None


async def test_database_refuses_a_sent_draft_without_any_approval(
    harness: US2Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user, chat, contact, draft = await _drafted(harness, make_user, make_chat, make_contact)
    async with harness.session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(Draft).where(Draft.id == draft.id).values(state=DraftState.sent)
            )
            await session.flush()
        await session.rollback()
    async with harness.session_factory() as session:
        with pytest.raises(IntegrityError):  # an armed draft without a card cannot exist either
            await session.execute(
                update(Draft).where(Draft.id == draft.id).values(control_message_id=None)
            )
            await session.flush()
        await session.rollback()
