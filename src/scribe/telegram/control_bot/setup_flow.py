"""Setup flow (contracts/control-chat.md "Setup FSM states"), US1 minimum (T040):

``/start`` -> connect the Telegram account (``connect_flow``, ``purpose="setup"``) -> pick work
chats -> time zone (typed like in /settings, quick buttons ``setuptz:<zone>`` picked by the
Telegram language, or ``setuptz:keep``) -> other defaults from ``Settings`` -> import own
messages + build the global profile -> user ``active`` -> the grammatical gender for drafts
(issue #46, ``grammatical_gender.after_setup``): after "All set" the user is asked once with two
buttons, the one suggested by the most recent imported messages (in memory only) first; the
claim is stored with the switch to ``active`` and a failure leaves it to the job. ``/cancel``
aborts and keeps the account in ``setting_up``.

A new user's UI language (``users.language``, issue #8) is the default for their Telegram
``language_code`` (ru, uk, be, kk -> Russian); every text of the flow is in that language.

The chat picker lives in ``chat_picker.py`` (purpose ``setup``, finisher :func:`finish_pick`);
➕ Add work chats in /chats reuses it (specs/007).

Work account mode (specs/008): the setup picker has the 💼 switch (``pickwa:<on|off>``). With it
on, every private chat is a work chat (tapping one excludes it) and Done is allowed with
nothing ticked; Done saves the mode (``work_account.switch``), the ticked groups as picked work
chats and the excluded private chats as ``auto`` excluded rows. The import then also reads the
user's own messages in up to ``work_account_import_max_chats`` recent private chats.

Seams: the task tracker steps (``tasks_connect``/``tasks_boards``, T056) and the hours and
delay steps (T073) go between ``pick_chats`` and ``importing``, next to ``timezone``.
"""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import ProfileScope, SessionStatus, User, UserState
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, t
from scribe.llm.registry import LLMRouter
from scribe.logging import get_logger
from scribe.style.grammatical_gender import recent_texts
from scribe.style.importer import ImportResult, import_own_messages
from scribe.style.profiles import BuildSummary, build_global_profile, build_profiles_from_import
from scribe.telegram.control_bot import chat_picker, connect_flow, grammatical_gender
from scribe.telegram.control_bot.limits import chat_slots
from scribe.telegram.control_bot.menu import set_chat_commands
from scribe.telegram.control_bot.render import (
    button,
    format_callback,
    parse_callback,
    reconnect_keyboard,
)
from scribe.telegram.control_bot.timezones import (
    AmbiguousTimeZone,
    ResolvedTimeZone,
    UnknownTimeZone,
    confirmation_text,
    quick_zones,
    resolve_time_zone,
    time_zone_keyboard,
    zone_from_callback,
)
from scribe.telegram.gateway import Dialog, MessagingGateway
from scribe.telegram.login import LoginManager

log = get_logger("scribe.control_bot")


class SetupStates(StatesGroup):
    pick_chats = State()
    timezone = State()
    importing = State()


# --- /start and /cancel -------------------------------------------------------------------------


async def start_command(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    telegram_login: LoginManager | None = None,
) -> None:
    if user is not None:
        if await connect_flow.in_connect_state(state):
            # A second attempt cancels the first (spec edge case): same purpose, method choice.
            purpose = await connect_flow.get_purpose(state)
            if telegram_login is not None:
                await telegram_login.end(message.chat.id, "cancelled", drain=True)
            await connect_flow.begin_connect(bot, message.chat.id, user, state, purpose)
        elif user.state != UserState.setting_up:
            async with session_factory() as session:
                row = await users_repo.get_telegram_session(session, user.id)
            if row is not None and row.status != SessionStatus.active:
                # Setup is done (settings, chats, profiles kept): reconnect, don't re-run it.
                await message.answer(
                    t("telegram.disconnected_start", lang), reply_markup=reconnect_keyboard(lang)
                )
            else:
                await message.answer(t("start.already_set_up", lang))
        elif await state.get_state() is not None:
            await message.answer(t("start.in_progress", lang))
        else:
            await connect_flow.begin_connect(bot, message.chat.id, user, state, "setup")
        return
    sender = message.from_user
    display_name = sender.full_name if sender is not None else ""
    language_code = sender.language_code if sender is not None else None
    try:
        async with session_factory() as session:
            created = await users_repo.create_user(
                session,
                control_chat_id=message.chat.id,
                settings=settings,
                display_name=display_name,
                language=lang,
            )
            await session.commit()
    except IntegrityError:  # a concurrent /start for the same chat already created it
        await message.answer(t("start.in_progress", lang))
        return
    log.info("user_created", user_id=str(created.id), language=lang)
    if lang == "ru" and not (language_code or "").casefold().startswith("ru"):
        # uk / be / kk: Telegram would show them the default (English) menu.
        await set_chat_commands(bot, message.chat.id, lang)
    await message.answer(t("start.welcome", lang))
    await connect_flow.begin_connect(bot, message.chat.id, created, state, "setup")


async def cancel_setup(
    bot: Bot, chat_id: int, state: FSMContext, telegram_login: LoginManager | None, lang: str
) -> None:
    """``/cancel`` and the connect step's Cancel button: end (and drain) any login attempt."""
    if telegram_login is not None:
        await telegram_login.end(chat_id, "cancelled", drain=True)
    purpose = await connect_flow.get_purpose(state)
    await state.clear()
    key = "setup.reconnect_cancelled" if purpose == "reconnect" else "setup.cancelled"
    await bot.send_message(chat_id, t(key, lang))


async def cancel_command(
    message: Message,
    bot: Bot,
    state: FSMContext,
    lang: Lang,
    telegram_login: LoginManager | None = None,
) -> None:
    await cancel_setup(bot, message.chat.id, state, telegram_login, lang)


async def show_chat_picker(
    bot: Bot, chat_id: int, user: User, state: FSMContext, gateway: MessagingGateway
) -> None:
    """After the Telegram account is connected: list the dialogs and show the picker
    (``chat_picker``, purpose ``setup``; every dialog is listed)."""
    await chat_picker.show_chat_picker(
        bot, chat_id, user, state, gateway, purpose="setup", picking_state=SetupStates.pick_chats
    )


# --- chat picker: Done --------------------------------------------------------------------------


async def finish_pick(done: chat_picker.PickResult) -> None:
    """The ``setup`` finisher: at least one chat (or work account mode on), upsert them, then
    the time zone step."""
    callback, bot, state, user, lang = done.callback, done.bot, done.state, done.user, done.lang
    session_factory: async_sessionmaker[AsyncSession] = done.services["session_factory"]
    clock: Clock = done.services.get("clock", utcnow)
    if not done.chosen and not done.work_account:
        await callback.answer(t("setup.pick_none", lang))
        return
    # A usage limit on work chats (docs/plugins.md); the picker already refuses the taps.
    limits, settings = done.services.get("limits"), done.services.get("settings")
    limit, room = await chat_slots(session_factory, user, limits, settings, clock)
    if limit is not None and done.work_account:
        await callback.answer(t("settings.work_account_unavailable", lang), show_alert=True)
        return
    if limit is not None and len(done.chosen) > room:
        await callback.answer(t("chats.limit_reached", lang, n=limit), show_alert=True)
        return
    await callback.answer()
    async with session_factory() as session:
        for chat_id, title, kind in done.chosen:
            await chats_repo.upsert_work_chat(
                session,
                user.id,
                telegram_chat_id=chat_id,
                title=title,
                kind=chat_picker.chat_kind(kind),
                enabled=True,
            )
        await session.commit()
    if done.work_account:
        now = clock()
        for chat_id, title, kind in done.excluded:
            dialog = Dialog(chat_id=chat_id, title=title, kind=kind)
            await work_account.exclude_chat(session_factory, user.id, dialog=dialog, now=now)
        await work_account.switch(session_factory, user.id, on=True, now=now)
        saved = t(
            "setup.chats_saved_work_account",
            lang,
            excluded=len(done.excluded),
            groups=len(done.chosen),
        )
    else:
        saved = t("setup.chats_saved", lang, n=len(done.chosen))
    await state.set_state(SetupStates.timezone)
    await state.set_data({})
    if callback.message is not None:
        await bot.edit_message_text(
            text=saved, chat_id=callback.message.chat.id, message_id=callback.message.message_id
        )
    log.info(
        "setup_step",
        user_id=str(user.id),
        step="chats_picked",
        chats=len(done.chosen),
        work_account=done.work_account,
        excluded=len(done.excluded),
    )
    # Seam: task tracker (T056) and hours / delay (T073) steps go next to the time zone step;
    # until then the defaults from Settings copied into the user row at /start are kept.
    current = user.timezone
    keep = button(t("btn.keep_tz", lang, zone=current), format_callback("setuptz", "keep"))
    zones = tuple(z for z in quick_zones(callback.from_user.language_code) if z != current)
    await bot.send_message(
        user.telegram_control_chat_id,
        t("setup.tz_prompt", lang),
        reply_markup=time_zone_keyboard("setuptz", zones, clock(), lang, extra=[keep]),
    )


chat_picker.register_purpose(
    chat_picker.PickerPurpose(
        name="setup",
        prompt_key="setup.pick_prompt",
        finish=finish_pick,
        retry_command="/start",
        single_live_message=False,  # unchanged setup behaviour
        work_account_switch=True,  # specs/008
        work_account_prompt_key="setup.pick_prompt_work_account",
    )
)


# --- time zone step -----------------------------------------------------------------------------


async def tz_step_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: MessagingGateway,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        return
    lang = user.language
    now = clock()
    try:
        resolved = resolve_time_zone(message.text or "", now)
    except AmbiguousTimeZone as exc:
        await message.answer(
            t("settings.tz_choose", lang, problem=exc.msg),
            reply_markup=time_zone_keyboard("setuptz", exc.choices, now, lang),
        )
        return
    except UnknownTimeZone as exc:
        await message.answer(t("setup.tz_unknown", lang, problem=exc.msg))
        return
    await _set_time_zone_and_finish(
        bot, state, user, resolved, session_factory, settings, llm_router, gateway, clock
    )


async def tz_step_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: MessagingGateway,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer()
        return
    try:
        entity = parse_callback(callback.data).entity_id
    except ValueError:
        await callback.answer()
        return
    zone = user.timezone if entity == "keep" else zone_from_callback(entity)
    if zone is None:
        await callback.answer(t("alert.unknown_tz", user.language))
        return
    await callback.answer()
    await _set_time_zone_and_finish(
        bot, state, user, ResolvedTimeZone(zone), session_factory, settings, llm_router,
        gateway, clock,
    )  # fmt: skip


async def _set_time_zone_and_finish(
    bot: Bot,
    state: FSMContext,
    user: User,
    resolved: ResolvedTimeZone,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: MessagingGateway,
    clock: Clock,
) -> None:
    # Leave the step first so a second answer (double tap) is not taken as this step's.
    await state.set_state(SetupStates.importing)
    async with session_factory() as session:
        await users_repo.update_user(session, user.id, timezone=resolved.zone)
        await session.commit()
    log.info("setup_step", user_id=str(user.id), step="timezone_set")
    chat_id = user.telegram_control_chat_id
    await bot.send_message(chat_id, confirmation_text(resolved, clock(), user.language))
    await _import_and_finish(
        bot, chat_id, state, user, session_factory, settings, llm_router, gateway, clock
    )


async def _build_profiles_note(
    session_factory: async_sessionmaker[AsyncSession],
    llm_router: LLMRouter,
    user: User,
    imported: ImportResult,
    *,
    settings: Settings,
    clock: Clock,
) -> str:
    """The same profiles as 🔄 Rebuild (FR-034, FR-035): global + the top contacts, built with
    synchronous calls (the user is waiting). Whatever fails, the user ends up with a global
    profile (a neutral one when nothing could be learned) and the note says what was missed."""
    lang = user.language
    summary: BuildSummary | None = None
    try:
        summary = await build_profiles_from_import(
            session_factory, llm_router, user.id, imported, settings=settings, clock=clock
        )
    except Exception as exc:
        log.error("setup_profile_failed", user_id=str(user.id), error=type(exc).__name__)
    profile = summary.global_profile if summary is not None else None
    if profile is None:
        # The global build failed (or the build crashed before storing it): keep one that
        # exists, else store the neutral default (no LLM call).
        async with session_factory() as session:
            existing = await profiles_repo.get_current_profile(
                session, user.id, scope=ProfileScope.global_
            )
        if existing is None:
            await build_global_profile(session_factory, llm_router, user.id, [], settings=settings)
        note = t("setup.learn_failed", lang)
    else:
        note = t("setup.learned", lang, n=profile.sample_size)
    if summary is not None and summary.contact_profiles:
        note += " " + t("setup.contacts", lang, n=summary.contact_profiles)
    if profile is not None and profile.rules_json.get("weak"):
        note += " " + t("setup.weak", lang, n=settings.min_style_samples)
    if summary is not None and summary.contact_failures:
        note += "\n" + t("setup.contacts_failed", lang, n=summary.contact_failures)
    return note


async def _import_and_finish(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: MessagingGateway,
    clock: Clock,
) -> None:
    lang = user.language
    progress = await bot.send_message(chat_id, t("setup.importing", lang))
    recent: list[str] = []
    try:
        async with session_factory() as session:  # specs/008: + recent private chats
            targets = await work_account.import_targets(
                session, gateway, user.id, max_auto=settings.work_account_import_max_chats
            )
        imported = await import_own_messages(
            gateway, session_factory, user.id, settings=settings, clock=clock, targets=targets
        )
        async with session_factory() as session:
            await work_account.materialise_imported(
                session, user.id, imported, targets, now=clock()
            )
            await session.commit()
        recent = recent_texts(imported.samples)  # issue #46, in memory only
    except Exception as exc:
        log.error("setup_profile_failed", user_id=str(user.id), error=type(exc).__name__)
        await build_global_profile(session_factory, llm_router, user.id, [], settings=settings)
        note = t("setup.learn_failed", lang)
    else:
        note = await _build_profiles_note(
            session_factory, llm_router, user, imported, settings=settings, clock=clock
        )
    await bot.edit_message_text(text=note, chat_id=chat_id, message_id=progress.message_id)

    # Issue #46: the claim for the grammatical gender question (a conditional UPDATE, None when
    # the user is not due) commits together with the switch to active: the job only sees the
    # user once both are visible, already claimed.
    gender_token: datetime | None = None
    async with session_factory() as session:
        await users_repo.set_state(session, user.id, UserState.active)
        gender_token = await grammatical_gender.claim_in(session, user.id, clock())
        await session.commit()
    await state.clear()
    log.info("setup_step", user_id=str(user.id), step="done")
    await bot.send_message(
        chat_id,
        t(
            "setup.done",
            lang,
            delay=_delay_label(settings.delay_min_s, settings.delay_max_s, lang),
            start=settings.work_start.strftime("%H:%M"),
            end=settings.work_end.strftime("%H:%M"),
        ),
    )
    # Issue #46: asked the grammatical gender once, with a suggestion from the messages just
    # imported; a failure leaves it to the grammatical_gender_prompt job.
    if gender_token is not None:
        await grammatical_gender.after_setup(
            session_factory, bot, user.id, recent, gender_token, clock=clock
        )


def _delay_label(min_s: int, max_s: int, lang: str) -> str:
    def fmt(seconds: int) -> str:
        if seconds % 60 == 0 and seconds:
            return t("unit.min", lang, n=seconds // 60)
        return t("unit.s", lang, n=seconds)

    return fmt(min_s) if min_s == max_s else f"{fmt(min_s)}–{fmt(max_s)}"


def create_router() -> Router:
    router = Router(name="setup")
    router.message.register(start_command, CommandStart())
    router.message.register(
        cancel_command, Command("cancel"), StateFilter(SetupStates, connect_flow.ConnectStates)
    )
    chat_picker.register_picker_handlers(router, StateFilter(SetupStates.pick_chats))
    tz_step = StateFilter(SetupStates.timezone)
    router.message.register(tz_step_input, tz_step, F.text & ~F.text.startswith("/"))
    router.callback_query.register(tz_step_callback, tz_step, F.data.startswith("setuptz:"))
    # Last: picker buttons when no picker is open (the /chats add picker is handled earlier,
    # in the settings router).
    chat_picker.register_closed_handler(router)
    return router


__all__ = [
    "SetupStates",
    "cancel_setup",
    "create_router",
    "show_chat_picker",
]
