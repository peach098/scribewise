"""Plugin hooks of the control bot against Postgres (docs/plugins.md): routers before the core,
``pre_checkout_query`` handlers, open paths through the access gate, the offer buttons of the
"no access" reply, ``/my_data`` sections, ``/delete_my_data`` hooks in the delete transaction,
and plugin access events in ``access show``. Offline Bot API, fake gateway; synthetic ids only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
)
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import admin
from scribe.access import repo as access_repo
from scribe.config import Settings
from scribe.db.models import AccessEvent, AccessEventKind, RefusedContact, User, UserState
from scribe.i18n import t
from scribe.plugins import PluginHost
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.fsm_storage import PostgresFSMStorage
from scribe.telegram.control_bot.render import button
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeUser
from tests.integration.bot_support import (
    NOW,
    RecordingSession,
    callback_update,
    offline_bot,
    text_update,
)
from tests.integration.us2_support import callback_datas

STRANGER = 900000201
OPERATOR = 900000202
MEMBER = 900000203


@pytest.fixture
def gated(settings: Settings) -> Settings:
    return settings.model_copy(update={"access_gate": True, "operator_telegram_ids": [OPERATOR]})


def _dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    host: PluginHost | None = None,
) -> Dispatcher:
    options = host.dispatcher_options() if host is not None else {}
    return create_dispatcher(
        session_factory=session_factory,
        settings=settings,
        storage=PostgresFSMStorage(session_factory),
        clock=clock,
        gateway=FakeMessagingGateway(),
        **options,
    )


async def _refusals(session_factory: async_sessionmaker[AsyncSession], telegram_id: int) -> int:
    async with session_factory() as session:
        row = await session.get(RefusedContact, telegram_id)
    return 0 if row is None else row.attempts


def _texts(api: RecordingSession) -> list[str]:
    return [r.text for r in api.of_type(SendMessage)]


def _payment_update(chat_id: int) -> Update:
    return Update(
        update_id=990001,
        message=Message(
            message_id=990001,
            date=NOW,
            chat=Chat(id=chat_id, type="private"),
            from_user=TgUser(id=chat_id, is_bot=False, first_name="Synthetic"),
            successful_payment=SuccessfulPayment(
                currency="USD",
                total_amount=100,
                invoice_payload="synthetic",
                telegram_payment_charge_id="synthetic-charge",
                provider_payment_charge_id="synthetic-provider",
            ),
        ),
    )


# --- routers ------------------------------------------------------------------------------------


async def test_a_first_router_answers_a_core_callback_before_the_core(
    session_factory: async_sessionmaker[AsyncSession], gated: Settings, clock: FrozenClock
) -> None:
    router = Router(name="plugin-first")

    async def subinfo(callback: CallbackQuery, bot: Any) -> None:
        await callback.answer()
        await bot.send_message(callback.from_user.id, "Synthetic plugin answer")

    router.callback_query.register(subinfo, F.data == "apply:subinfo")
    host = PluginHost(gated)
    host.add_router(router, first=True)
    bot, api = offline_bot()

    await _dispatcher(session_factory, gated, clock, host).feed_update(
        bot, callback_update(STRANGER, "apply:subinfo")
    )

    assert _texts(api) == ["Synthetic plugin answer"]  # not the core placeholder

    bot, api = offline_bot()  # without the plugin: the core answers
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, callback_update(STRANGER, "apply:subinfo")
    )
    assert _texts(api) == [t("access.subscription_placeholder", "en")]


async def test_a_pre_checkout_handler_is_reached(
    session_factory: async_sessionmaker[AsyncSession], gated: Settings, clock: FrozenClock
) -> None:
    seen: list[str] = []
    router = Router(name="plugin-checkout")

    async def pre_checkout(query: PreCheckoutQuery) -> None:
        seen.append(query.invoice_payload)

    router.pre_checkout_query.register(pre_checkout)
    host = PluginHost(gated)
    host.handle_pre_checkout(router)
    bot, _ = offline_bot()
    update = Update(
        update_id=990002,
        pre_checkout_query=PreCheckoutQuery(
            id="synthetic-query",
            from_user=TgUser(id=STRANGER, is_bot=False, first_name="Synthetic"),
            currency="USD",
            total_amount=100,
            invoice_payload="synthetic-payload",
        ),
    )

    await _dispatcher(session_factory, gated, clock, host).feed_update(bot, update)

    assert seen == ["synthetic-payload"]


# --- the access gate ------------------------------------------------------------------------------


async def test_open_paths_pass_the_gate_without_counting_refusals(
    session_factory: async_sessionmaker[AsyncSession],
    gated: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    reached: list[str] = []
    router = Router(name="plugin-open")

    async def command(message: Message) -> None:
        reached.append("command")

    async def press(callback: CallbackQuery) -> None:
        reached.append("callback")
        await callback.answer()

    async def payment(message: Message) -> None:
        reached.append("payment")

    router.message.register(command, Command("example"))
    router.callback_query.register(press, F.data.startswith("example:"))
    router.message.register(payment, F.successful_payment)
    host = PluginHost(gated)
    host.add_router(router, first=True)
    host.gate_allow(
        commands={"example"},
        callback_prefixes=("example:",),
        message_filter=lambda message: message.successful_payment is not None,
    )
    dispatcher = _dispatcher(session_factory, gated, clock, host)
    await make_user(control_chat_id=MEMBER, state=UserState.active)  # no access: restricted
    for sender in (STRANGER, MEMBER):
        bot, api = offline_bot()
        await dispatcher.feed_update(bot, text_update(sender, "/example"))
        await dispatcher.feed_update(bot, callback_update(sender, "example:go"))
        await dispatcher.feed_update(bot, _payment_update(sender))
        assert _texts(api) == []
        assert await _refusals(session_factory, sender) == 0
    assert reached == ["command", "callback", "payment"] * 2

    bot, api = offline_bot()  # anything else is still refused
    await dispatcher.feed_update(bot, text_update(STRANGER, "/start"))
    assert len(_texts(api)) == 1
    assert await _refusals(session_factory, STRANGER) == 1


async def test_the_offer_provider_replaces_the_buttons(
    session_factory: async_sessionmaker[AsyncSession], gated: Settings, clock: FrozenClock
) -> None:
    asked: list[tuple[int, str]] = []

    async def offer(telegram_id: int, lang: str, session: AsyncSession) -> list[Any]:
        asked.append((telegram_id, lang))
        return [[button("Synthetic offer", "example:offer")]]

    host = PluginHost(gated)
    host.set_no_access_offer(offer)
    bot, api = offline_bot()
    await _dispatcher(session_factory, gated, clock, host).feed_update(
        bot, text_update(STRANGER, "/start", language_code="ru")
    )

    (reply,) = api.of_type(SendMessage)
    assert reply.text == t("access.refused", "ru")
    assert callback_datas(reply.reply_markup) == ["example:offer"]
    assert asked == [(STRANGER, "ru")]

    bot, api = offline_bot()  # without a plugin: the beta buttons, as before
    await _dispatcher(session_factory, gated, clock).feed_update(
        bot, text_update(STRANGER + 10, "/start")
    )
    (reply,) = api.of_type(SendMessage)
    assert callback_datas(reply.reply_markup) == ["apply:start", "apply:subinfo"]


# --- personal data ------------------------------------------------------------------------------


async def test_my_data_shows_plugin_sections(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    async def section(session: AsyncSession, telegram_id: int, lang: str) -> list[str]:
        return [f"Synthetic plugin section for {telegram_id} ({lang})"]

    host = PluginHost(settings)
    host.on_my_data(section)
    user = await make_user(control_chat_id=MEMBER, state=UserState.active)
    bot, api = offline_bot()

    await _dispatcher(session_factory, settings, clock, host).feed_update(
        bot, text_update(user.telegram_control_chat_id, "/my_data")
    )

    (summary,) = api.of_type(SendMessage)
    assert summary.text.endswith(f"\n\nSynthetic plugin section for {MEMBER} (en)")


async def test_delete_my_data_runs_the_plugin_hooks_in_the_delete_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    calls: list[tuple[int, bool]] = []
    fail = {"now": True}

    async def warning(session: AsyncSession, telegram_id: int, lang: str) -> list[str]:
        return ["Synthetic plugin warning."]

    async def delete(session: AsyncSession, telegram_id: int) -> None:
        # The user row is already gone in this (uncommitted) transaction.
        still_there = await session.scalar(
            select(User.id).where(User.telegram_control_chat_id == telegram_id)
        )
        calls.append((telegram_id, still_there is not None))
        if fail["now"]:
            raise RuntimeError("synthetic plugin failure")

    host = PluginHost(settings)
    host.on_delete_warning(warning)
    host.on_delete_user(delete)
    dispatcher = _dispatcher(session_factory, settings, clock, host)
    user = await make_user(control_chat_id=MEMBER, state=UserState.active)
    control = user.telegram_control_chat_id

    bot, api = offline_bot()
    await dispatcher.feed_update(bot, text_update(control, "/delete_my_data"))
    assert _texts(api)[-1].endswith("\n\nSynthetic plugin warning.")
    await dispatcher.feed_update(bot, text_update(control, "DELETE"))
    assert calls == [(MEMBER, False)]
    assert _texts(api)[-1] == t("delete.failed", "en")
    async with session_factory() as session:  # the failure rolled the deletion back
        assert await session.get(User, user.id) is not None

    fail["now"] = False
    await dispatcher.feed_update(bot, text_update(control, "/delete_my_data"))
    await dispatcher.feed_update(bot, text_update(control, "DELETE"))
    assert calls == [(MEMBER, False), (MEMBER, False)]
    assert _texts(api)[-1] == t("delete.done", "en")
    async with session_factory() as session:
        assert await session.get(User, user.id) is None


# --- plugin access events (E9) --------------------------------------------------------------------


async def test_plugin_access_events_show_their_name(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now: datetime = clock()
    async with session_factory() as session:
        await access_repo.record_plugin_event(
            session, STRANGER, "example_started", actor="system", meta={"ref": 1}, now=now
        )
        await session.commit()
        (event,) = (
            await session.scalars(select(AccessEvent).where(AccessEvent.telegram_id == STRANGER))
        ).all()
    assert event.kind == AccessEventKind.plugin
    assert event.meta_json == {"ref": 1, "event": "example_started"}

    code = await admin.run(
        ["access", "show", str(STRANGER)], session_factory, clock=clock, settings=settings
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"{now:%Y-%m-%d %H:%M}  plugin:example_started  system  -" in out
