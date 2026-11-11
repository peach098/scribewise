"""Usage limits per user from a plugin's resolver (docs/plugins.md) against Postgres: the daily
draft limit, the work chat limit (/chats, ➕, the setup picker, work account mode and its
pick-up), the profile refresh being unavailable or off by default, and the routes excluded from
the cap check. Fake LLM, fake gateway, offline Bot API; synthetic data only."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    LoginMethod,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import (
    BudgetCaps,
    SettingsLimitsResolver,
    UsageLimits,
    user_budget_level,
)
from scribe.i18n import t
from scribe.jobs import refresh_all_users
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import Usage
from scribe.llm.registry import LLMRouter
from scribe.llm.spend import SpendEntry
from scribe.plugins import PluginHost
from scribe.telegram.control_bot.render import button, format_callback
from scribe.telegram.control_bot.setup_flow import SetupStates
from scribe.telegram.fake_gateway import FakeMessagingGateway
from scribe.telegram.gateway import Dialog
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import grounded_reply, select_all_history
from tests.integration.us2_support import US2Harness, button_texts, callback_datas
from tests.integration.us4_support import add_own_messages, add_profile, style_output

REPLY = "Synthetic reply."
OFFER_DATA = "example:offer"


class Fixed:
    """A plugin-style resolver: the same limits for every user."""

    def __init__(self, limits: UsageLimits) -> None:
        self.limits = limits

    async def limits_for(self, session: AsyncSession, user: User, now: datetime) -> UsageLimits:
        return self.limits


async def offer(telegram_id: int, lang: str, session: AsyncSession) -> list[Any]:
    return [[button(f"Synthetic offer ({lang})", OFFER_DATA)]]


def _limits(settings: Settings, **values: Any) -> Fixed:
    return Fixed(UsageLimits(caps=BudgetCaps.from_settings(settings), **values))


def _harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    limits: Fixed,
) -> US2Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    fake_llm.set_response("profile.build", style_output())
    host = PluginHost(settings)
    host.set_limits_resolver(limits)
    return US2Harness(
        session_factory,
        settings,
        llm_router,
        fake_llm,
        gateway,
        clock,
        limits=host.hooks.limits_resolver(settings),
        offer_provider=offer,
    )


@pytest.fixture
def make(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> Any:
    def build(**values: Any) -> US2Harness:
        return _harness(
            session_factory,
            settings,
            llm_router,
            fake_llm,
            gateway,
            clock,
            _limits(settings, **values),
        )

    return build


def _offer_buttons(message: SendMessage) -> list[str]:
    return callback_datas(message.reply_markup)


def _alerts(harness: US2Harness) -> list[str | None]:
    return [a.text for a in harness.api.of_type(AnswerCallbackQuery) if a.show_alert]


async def _connect(
    session_factory: async_sessionmaker[AsyncSession], user: User, clock: FrozenClock
) -> None:
    async with session_factory() as session:
        await users_repo.upsert_telegram_session(
            session, user.id, "synthetic-session", method=LoginMethod.qr, connected_at=clock()
        )
        await session.commit()


async def _chats(session_factory: async_sessionmaker[AsyncSession], user: User) -> list[WorkChat]:
    async with session_factory() as session:
        return await chats_repo.list_work_chats(session, user.id)


# --- drafts per day -------------------------------------------------------------------------------


async def test_draft_limit_skips_the_next_turn_before_the_reply_gate(
    make: Any,
    fake_llm: FakeAdapter,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    harness: US2Harness = make(max_drafts_per_day=3)
    user = await make_user(state=UserState.active, timezone="UTC")
    for n in range(4):
        contact = await make_contact(user, display_name=f"Synthetic contact {n}")
        chat = await make_chat(
            user, title=f"Synthetic {n}", telegram_chat_id=contact.telegram_peer_id
        )
        await harness.incoming(user, chat, contact, f"Could you check synthetic item {n}?")
        await harness.tick()

    assert len(await harness.drafts(user)) == 3
    assert len(fake_llm.calls_for("compose.draft")) == 3
    gate_calls = len(fake_llm.calls_for("classify.reply_needed"))
    turns = await harness.turns(user)
    assert [t.skip_category for t in turns] == [None, None, None, "budget"]
    ((notified, notice),) = harness.notifications
    assert notified == user.id
    assert notice == t("budget.drafts_notice", "en", time="00:00")
    async with harness.session_factory() as session:
        (record,) = (
            await session.scalars(
                select(ActivityRecord).where(
                    ActivityRecord.user_id == user.id,
                    ActivityRecord.kind == ActivityKind.budget_paused,
                )
            )
        ).all()
    assert record.meta_json["reason"] == "drafts"

    # Another turn the same day: skipped again without a model call or a second notice.
    contact = await make_contact(user, display_name="Synthetic contact 5")
    chat = await make_chat(user, title="Synthetic 5", telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "One more synthetic question?")
    await harness.tick()
    assert len(fake_llm.calls_for("classify.reply_needed")) == gate_calls  # gate not asked
    assert len(harness.notifications) == 1


async def test_draft_notice_goes_through_the_plugin_decorator(
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
    from scribe.i18n import Msg

    async def decorate(user_id: Any, message: Msg) -> tuple[Msg, Any]:
        return Msg("example.decorated", base=message), lambda lang: None

    host = PluginHost(settings)
    host.add_messages("en", {"example.decorated": "{base} (decorated)"})
    host.wrap_budget_notifier(decorate)
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    harness = US2Harness(
        session_factory,
        settings,
        llm_router,
        fake_llm,
        gateway,
        clock,
        limits=_limits(settings, max_drafts_per_day=0),
        decorate=host.hooks.notice_decorator(),
    )
    user = await make_user(state=UserState.active, timezone="UTC")
    contact = await make_contact(user, display_name="Synthetic contact")
    chat = await make_chat(user, title="Synthetic", telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Could you check the synthetic item?")
    await harness.tick()

    ((_, notice),) = harness.notifications
    assert notice.endswith("(decorated)")
    assert harness.notice_markups[0] is not None


async def test_without_a_plugin_drafts_are_not_counted(
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
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(REPLY))
    harness = US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)
    assert isinstance(harness.limits, SettingsLimitsResolver)
    user = await make_user(state=UserState.active)
    for n in range(4):
        contact = await make_contact(user, display_name=f"Synthetic contact {n}")
        chat = await make_chat(
            user, title=f"Synthetic {n}", telegram_chat_id=contact.telegram_peer_id
        )
        await harness.incoming(user, chat, contact, f"Could you check synthetic item {n}?")
        await harness.tick()
    assert len(await harness.drafts(user)) == 4
    assert harness.notifications == []


# --- work chats -----------------------------------------------------------------------------------


async def _three_enabled(
    make_user: MakeUser, make_chat: MakeChat, **values: Any
) -> tuple[User, list[WorkChat]]:
    user = await make_user(state=UserState.active, **values)
    chats = [
        await make_chat(user, title=f"Synthetic {n}", telegram_chat_id=900000100 + n)
        for n in range(3)
    ]
    return user, chats


async def test_chats_toggle_refuses_a_fourth_enabled_chat(
    make: Any, make_user: MakeUser, make_chat: MakeChat
) -> None:
    harness: US2Harness = make(max_work_chats=3)
    user, _ = await _three_enabled(make_user, make_chat)
    off = await make_chat(user, title="Synthetic off", telegram_chat_id=900000199, enabled=False)

    await harness.feed(
        callback_update(user.telegram_control_chat_id, format_callback("chatenable", off.id, "on"))
    )

    refusal = harness.messages()[-1]
    assert refusal.text == t("chats.limit_reached", "en", n=3)
    assert _offer_buttons(refusal) == [OFFER_DATA]
    stored = {c.id: c for c in await _chats(harness.session_factory, user)}
    assert not stored[off.id].enabled

    # Below the limit the same toggle works.
    first = next(c for c in stored.values() if c.enabled)
    async with harness.session_factory() as session:
        await chats_repo.set_enabled(session, user.id, first.id, False)
        await session.commit()
    await harness.feed(
        callback_update(user.telegram_control_chat_id, format_callback("chatenable", off.id, "on"))
    )
    stored = {c.id: c for c in await _chats(harness.session_factory, user)}
    assert stored[off.id].enabled


async def test_add_chats_picker_is_refused_at_the_limit(
    make: Any,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    harness: US2Harness = make(max_work_chats=3)
    user, _ = await _three_enabled(make_user, make_chat)
    await _connect(harness.session_factory, user, clock)
    gateway.dialogs[user.id] = [
        Dialog(chat_id=-100900000300, title="Synthetic group", kind="group")
    ]

    await harness.feed(callback_update(user.telegram_control_chat_id, "chatadd:0"))

    refusal = harness.messages()[-1]
    assert refusal.text == t("chats.limit_reached", "en", n=3)
    assert _offer_buttons(refusal) == [OFFER_DATA]
    chat = user.telegram_control_chat_id
    assert await harness.dispatcher.fsm.get_context(harness.bot, chat, chat).get_state() is None


async def test_add_chats_picker_ticks_only_the_chats_left(
    make: Any,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    harness: US2Harness = make(max_work_chats=3)
    user = await make_user(state=UserState.active)
    await make_chat(user, title="Synthetic 0", telegram_chat_id=900000100)
    await make_chat(user, title="Synthetic 1", telegram_chat_id=900000101)
    await _connect(harness.session_factory, user, clock)
    gateway.dialogs[user.id] = [
        Dialog(chat_id=-100900000301, title="Synthetic group A", kind="group"),
        Dialog(chat_id=-100900000302, title="Synthetic group B", kind="group"),
    ]
    control = user.telegram_control_chat_id
    await harness.feed(callback_update(control, "chatadd:0"))
    context = harness.dispatcher.fsm.get_context(harness.bot, control, control)
    picker_id = (await context.get_data())["message_id"]

    await harness.feed(callback_update(control, "pickchat:-100900000301", message_id=picker_id))
    await harness.feed(callback_update(control, "pickchat:-100900000302", message_id=picker_id))

    assert (await context.get_data())["selected"] == [-100900000301]
    assert _alerts(harness) == [t("chats.limit_reached", "en", n=3)]
    await harness.feed(callback_update(control, "pickdone:0", message_id=picker_id))
    enabled = [c for c in await _chats(harness.session_factory, user) if c.enabled]
    assert len(enabled) == 3


async def test_setup_picker_refuses_ticks_beyond_the_limit_and_work_account_mode(
    make: Any, make_user: MakeUser
) -> None:
    harness: US2Harness = make(max_work_chats=1)
    user = await make_user(state=UserState.setting_up)
    control = user.telegram_control_chat_id
    context = harness.dispatcher.fsm.get_context(harness.bot, control, control)
    await context.set_state(SetupStates.pick_chats)
    dialogs = [[-1003, "Synthetic A", "private"], [-1004, "Synthetic B", "group"]]
    await context.set_data(
        {"purpose": "setup", "dialogs": dialogs, "selected": [], "page": 0, "work_account": False}
    )

    await harness.feed(callback_update(control, "pickchat:-1003"))
    await harness.feed(callback_update(control, "pickchat:-1004"))
    await harness.feed(callback_update(control, "pickwa:on"))

    data = await context.get_data()
    assert data["selected"] == [-1003]
    assert data["work_account"] is False
    assert _alerts(harness) == [
        t("chats.limit_reached", "en", n=1),
        t("settings.work_account_unavailable", "en"),
    ]
    await harness.feed(callback_update(control, "pickdone:0"))
    assert await context.get_state() == SetupStates.timezone.state
    assert [c.telegram_chat_id for c in await _chats(harness.session_factory, user)] == [-1003]


async def test_work_account_mode_cannot_be_turned_on_under_a_limit(
    make: Any, make_user: MakeUser, make_chat: MakeChat
) -> None:
    harness: US2Harness = make(max_work_chats=3)
    user, _ = await _three_enabled(make_user, make_chat)
    control = user.telegram_control_chat_id

    await harness.feed(callback_update(control, format_callback("settings", "work_account")))
    refusal = harness.messages()[-1]
    assert refusal.text == t("settings.work_account_unavailable", "en")
    assert _offer_buttons(refusal) == [OFFER_DATA]
    await harness.feed(callback_update(control, format_callback("wacct", "on")))
    assert harness.messages()[-1].text == t("settings.work_account_unavailable", "en")

    async with harness.session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
    assert stored is not None and not stored.work_account


async def test_work_account_pick_up_adds_no_chat_at_the_limit(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
) -> None:
    user, _ = await _three_enabled(make_user, make_chat, work_account=True)
    limited = _limits(settings, max_work_chats=3)
    async with session_factory() as session:
        picked = await work_account.pick_up(
            session,
            user.id,
            telegram_chat_id=900000150,
            title="Synthetic new",
            chat_kind="private",
            peer_kind="user",
            direction="in",
            now=clock(),
            limits=limited,
        )
        await session.commit()
    assert picked is None
    assert len(await _chats(session_factory, user)) == 3

    async with session_factory() as session:  # without the limit the chat is picked up
        picked = await work_account.pick_up(
            session,
            user.id,
            telegram_chat_id=900000150,
            title="Synthetic new",
            chat_kind="private",
            peer_kind="user",
            direction="in",
            now=clock(),
        )
        await session.commit()
    assert picked is not None and picked.kind == ChatKind.private


# --- profile refresh ------------------------------------------------------------------------------


async def _refresh_candidate(
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    choice: bool | None = None,
) -> User:
    user = await make_user(state=UserState.active, profile_auto_refresh=choice)
    contact = await make_contact(user, display_name="Synthetic contact")
    chat = await make_chat(user, telegram_chat_id=contact.telegram_peer_id, title="Synthetic")
    await add_profile(session_factory, user, at=clock() - timedelta(days=40))
    await add_own_messages(session_factory, user, chat, 12, at=clock() - timedelta(days=1))
    return user


async def _refreshed(session_factory: async_sessionmaker[AsyncSession], user: User) -> bool:
    async with session_factory() as session:
        found = await session.scalar(
            select(ActivityRecord.id).where(
                ActivityRecord.user_id == user.id,
                ActivityRecord.kind == ActivityKind.profile_refreshed,
            )
        )
    return found is not None


async def test_refresh_job_skips_users_without_the_refresh(
    make: Any,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
) -> None:
    make()  # the profile build answer
    user = await _refresh_candidate(
        make_user, make_contact, make_chat, session_factory, clock, True
    )

    await refresh_all_users(
        session_factory,
        llm_router,
        None,
        settings=settings,
        clock=clock,
        limits=_limits(settings, profile_refresh_allowed=False),
    )
    assert not await _refreshed(session_factory, user)

    await refresh_all_users(
        session_factory, llm_router, None, settings=settings, clock=clock, limits=_limits(settings)
    )
    assert await _refreshed(session_factory, user)


async def test_refresh_default_from_the_limits_and_the_users_choice_wins(
    make: Any,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    clock: FrozenClock,
    make_user: MakeUser,
    make_contact: MakeContact,
    make_chat: MakeChat,
) -> None:
    make()
    unset = await _refresh_candidate(make_user, make_contact, make_chat, session_factory, clock)
    chosen = await _refresh_candidate(
        make_user, make_contact, make_chat, session_factory, clock, True
    )

    await refresh_all_users(
        session_factory,
        llm_router,
        None,
        settings=settings,
        clock=clock,
        limits=_limits(settings, profile_refresh_default=False),
    )

    assert not await _refreshed(session_factory, unset)
    assert await _refreshed(session_factory, chosen)


async def test_rebuild_and_the_settings_switch_are_unavailable(
    make: Any, fake_llm: FakeAdapter, make_user: MakeUser, make_chat: MakeChat
) -> None:
    harness: US2Harness = make(profile_refresh_allowed=False)
    user = await make_user(state=UserState.active)
    await make_chat(user, title="Synthetic")
    control = user.telegram_control_chat_id

    await harness.feed(callback_update(control, "profrebuild:1"))
    refusal = harness.messages()[-1]
    assert refusal.text == t("profile.rebuild_unavailable", "en")
    assert _offer_buttons(refusal) == [OFFER_DATA]
    assert fake_llm.calls_for("profile.build") == []

    await harness.feed(text_update(control, "/settings"))
    shown = harness.messages()[-1]
    assert t("settings.profile_refresh", "en", state="not available") in (shown.text or "")
    assert t("btn.profile_refresh_unavailable", "en") in button_texts(shown.reply_markup)

    await harness.feed(callback_update(control, format_callback("settings", "profile_refresh")))
    assert harness.messages()[-1].text == t("settings.profile_refresh_unavailable", "en")
    async with harness.session_factory() as session:
        stored = await users_repo.get_user(session, user.id)
    assert stored is not None and stored.profile_auto_refresh is None


async def test_settings_switch_shows_the_limits_default(
    make: Any, make_user: MakeUser, make_chat: MakeChat
) -> None:
    harness: US2Harness = make(profile_refresh_default=False)
    user = await make_user(state=UserState.active)
    await harness.feed(text_update(user.telegram_control_chat_id, "/settings"))
    shown = harness.messages()[-1]
    assert t("settings.profile_refresh", "en", state="off") in (shown.text or "")
    assert t("btn.profile_refresh_on", "en") in button_texts(shown.reply_markup)


# --- routes excluded from the cap check -----------------------------------------------------------


async def test_excluded_routes_do_not_raise_the_cap_level(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    user = await make_user(state=UserState.active)
    record = llm_spend_repo.SpendRecorder(session_factory)
    await record(
        SpendEntry(
            user_id=user.id, route="profile.build", at=clock(), usage=Usage(), cost_usd=0.125
        )
    )
    capped = settings.model_copy(
        update={"llm_daily_soft_cap_usd": 0.1, "llm_daily_hard_cap_usd": 0.1}
    )
    excluding = capped.model_copy(update={"llm_budget_excluded_routes": ["profile.build"]})

    async with session_factory() as session:
        counted = SettingsLimitsResolver.from_settings(capped)
        assert await user_budget_level(session, user.id, counted, clock()) == "hard"
        skipped = SettingsLimitsResolver.from_settings(excluding)
        assert await user_budget_level(session, user.id, skipped, clock()) == "ok"
        # A plugin's resolver gets the deployment's excluded routes too.
        host = PluginHost(excluding)
        host.set_limits_resolver(_limits(capped))
        plugin = host.hooks.limits_resolver(excluding)
        assert await user_budget_level(session, user.id, plugin, clock()) == "ok"
        # The totals are unchanged.
        day = clock().date()
        assert await llm_spend_repo.totals_on(session, day) == {user.id: pytest.approx(0.125)}
