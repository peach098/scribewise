"""``/profile`` (FR-023, FR-033, FR-034, FR-037; contracts/control-chat.md "Profile view").

- ``/profile``: the global profile (version, sample size, plain-language summary, the weak-profile
  warning under ``min_style_samples``), "Rules you set", and buttons ✏️ Edit rules
  (``profrules:<profile_id>``), 👥 Contact profiles (N/20) (``profcontacts:0``), ↩️ Undo last
  refresh (``undo_refresh:<batch_id>``, only while it can be undone) and 🔄 Rebuild
  (``profrebuild:0`` → confirm ``profrebuild:1``: re-import the user's own messages of the last
  6 months in the enabled work chats and rebuild the global and contact profiles, synchronously;
  the confirm button is removed on the first tap and a second tap while a rebuild runs is
  answered "Already rebuilding"; refused with an alert at the user's daily LLM hard cap, issue
  #74). A rebuild ends the latest refresh's undo. The global and the
  contact profiles fail independently (:func:`rebuild_result_text` says which stayed as they
  were); only when nothing could be rebuilt is the note "couldn't rebuild, nothing changed".
- Contact profiles: one row per current contact profile with 👁 view (``profview:<id>``),
  📌 pin / unpin (``pin:<id>`` / ``unpin:<id>``) and 🗑 remove (``removeprofile:<id>``). A pinned
  contact keeps its profile; a removed one goes back to the global profile and the removal is
  recorded: it gets a profile again (also on 🔄 Rebuild) only if the messages written after the
  removal alone make it one of the most active contacts again.
- A contact's view and its Edit rules also list the global profile's rules, read-only and marked
  as global (they apply to that contact's drafts too, issue #42); the numbers, and ``-N``, refer
  to the contact's own rules only. A rule typed there that is already a global rule is not
  stored (the user is told it applies already). A view longer than one Telegram message (4096
  UTF-16 code units) cuts the inherited global rules first, then the profile's own ("…and N
  more").
- Edit rules: the numbered rules of that profile; each message adds a rule, ``-N`` removes rule N,
  ``/done`` (or ``/cancel``) ends; any other command ends it too
  (:class:`AbandonedRulesMiddleware`).
  Rules go to the profile's current version (re-read under the user's profile lock) and are
  copied forward by every rebuild and refresh.
- Rule proposals: ``rule_accept:<proposal_id>`` adds the rule, ``rule_dismiss:<proposal_id>``
  closes the proposal (``style/proposals.py``).

Every callback acts only on the caller's rows (``ownership.OwnershipMiddleware`` plus the scoped
loads below). Rules and summaries are shown to their owner only and never logged.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Awaitable, Callable, Collection
from itertools import accumulate
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.chats import work_account
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import (
    Contact,
    ContactKind,
    ProfileScope,
    ProfileState,
    StyleProfile,
    User,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import paused_alert, paused_now
from scribe.i18n import Lang, Msg, t
from scribe.llm.registry import LLMRouter
from scribe.logging import get_logger
from scribe.style import proposals
from scribe.style.importer import import_own_messages
from scribe.style.profiles import (
    NEUTRAL_SUMMARY,
    AppliedRules,
    BuildSummary,
    InvalidRule,
    UndoRefused,
    add_rule,
    build_profiles_from_import,
    effective_user_rules,
    inherited_rules_json,
    is_weak,
    last_undoable_refresh,
    profile_build_lock,
    remove_contact_profile,
    resolve_user_rules,
    undo_refresh,
    with_rule_removed,
)
from scribe.telegram.control_bot.render import (
    TELEGRAM_TEXT_MAX,
    InvalidCallbackData,
    button,
    format_callback,
    keyboard,
    parse_callback,
    utf16_len,
)
from scribe.telegram.gateway import MessagingGateway

log = get_logger("scribe.control_bot")

SUMMARY_MAX = 1500
_REMOVE = re.compile(r"^\s*-\s*(\d+)\s*$")


class ProfileStates(StatesGroup):
    rules = State()


# --- rendering ----------------------------------------------------------------------------------


def _owner_label(contact: Contact | None, lang: str) -> str:
    if contact is None:
        return t("profile.contact", lang)
    name = contact.display_name or t("profile.contact", lang)
    return t("profile.group_name", lang, name=name) if contact.kind == ContactKind.group else name


def rules_lines(rules: AppliedRules, lang: str, *, budget: int = TELEGRAM_TEXT_MAX) -> list[str]:
    """The profile's own rules, numbered (``-N`` removes rule N), then, for a contact profile,
    the global rules it inherits, read-only (issue #42). At most ``budget`` UTF-16 code units
    (joined with newlines, as Telegram counts them): the inherited rules are cut first, then
    the own ones, each with an "…and N more" line (30 + 30 rules of 200 characters exceed one
    Telegram message)."""
    own = [
        f"{i}. {proposals.rule_label(r['text'], r.get('key'), lang)}"
        for i, r in enumerate(rules.own, start=1)
    ]
    inherited = [
        f"• {proposals.rule_label(r['text'], r.get('key'), lang)}" for r in rules.inherited
    ]
    head = t("profile.rules", lang) if own else t("profile.rules_none", lang)
    global_head = t("profile.rules_global", lang)

    def more_own(kept: int) -> str | None:
        return t("profile.rules_more", lang, n=len(own) - kept) if kept < len(own) else None

    def more_global(kept: int) -> str | None:
        left = len(inherited) - kept
        return t("profile.rules_global_more", lang, n=left) if left else None

    # Prefix sums of line sizes (each line plus its newline), so every cut is sized in O(1).
    own_sizes = list(accumulate((utf16_len(line) + 1 for line in own), initial=0))
    inherited_sizes = list(accumulate((utf16_len(line) + 1 for line in inherited), initial=0))

    def size(kept_own: int, kept_inherited: int) -> int:
        extra = [more_own(kept_own), more_global(kept_inherited)]
        total = utf16_len(head) + own_sizes[kept_own] + inherited_sizes[kept_inherited]
        if kept_inherited:
            total += utf16_len(global_head) + 1
        return total + sum(utf16_len(line) + 1 for line in extra if line is not None)

    # Cut the inherited rules first, then the own ones: the first cut that fits.
    cuts = [(len(own), k) for k in range(len(inherited), -1, -1)]
    cuts += [(k, 0) for k in range(len(own) - 1, -1, -1)]
    kept_own, kept_inherited = next(((o, i) for o, i in cuts if size(o, i) <= budget), (0, 0))
    lines = [head, *own[:kept_own]]
    lines += [line for line in [more_own(kept_own)] if line is not None]
    if kept_inherited:
        lines += [global_head, *inherited[:kept_inherited]]
    lines += [line for line in [more_global(kept_inherited)] if line is not None]
    return lines


def _weak_text(settings: Settings, lang: str) -> str:
    return t("profile.weak", lang, n=settings.min_style_samples)


def profile_text(
    profile: StyleProfile,
    rules: AppliedRules,
    *,
    owner: Contact | None,
    settings: Settings,
    pinned: bool = False,
    lang: str = "en",
) -> str:
    if owner is None:
        scope = (
            t("profile.scope_global", lang)
            if profile.scope == ProfileScope.global_
            else _owner_label(owner, lang)
        )
        title = t(
            "profile.title_global",
            lang,
            scope=scope,
            version=profile.version,
            n=profile.sample_size,
        )
    else:
        title = t(
            "profile.title_contact",
            lang,
            owner=_owner_label(owner, lang),
            version=profile.version,
            n=profile.sample_size,
        )
        if pinned:
            title += " 📌"
    summary = profile.summary_text.strip()
    if summary == NEUTRAL_SUMMARY:
        summary = t("profile.neutral_summary", lang)
    if len(summary) > SUMMARY_MAX:
        summary = summary[: SUMMARY_MAX - 1].rstrip() + "…"
    lines = [title, summary]
    if is_weak(profile):
        lines.append(_weak_text(settings, lang))
    lines.append("")
    budget = TELEGRAM_TEXT_MAX - utf16_len("\n".join(lines)) - 1
    lines += rules_lines(rules, lang, budget=budget)
    return "\n".join(lines)


def global_keyboard(
    profile: StyleProfile | None,
    contact_count: int,
    max_profiles: int,
    undo: uuid.UUID | None,
    lang: str,
) -> InlineKeyboardMarkup:
    row1: list[InlineKeyboardButton] = []
    if profile is not None:
        row1.append(button(t("btn.edit_rules", lang), format_callback("profrules", profile.id)))
    row1.append(
        button(
            t("btn.contact_profiles", lang, count=contact_count, max=max_profiles),
            format_callback("profcontacts", 0),
        )
    )
    row2: list[InlineKeyboardButton] = []
    if undo is not None:
        row2.append(button(t("btn.undo_refresh", lang), format_callback("undo_refresh", undo)))
    row2.append(button(t("btn.rebuild", lang), format_callback("profrebuild", 0)))
    return keyboard([row1, row2])


def contact_row(
    profile: StyleProfile, contact: Contact | None, lang: str
) -> list[InlineKeyboardButton]:
    name = _owner_label(contact, lang)[:30]
    pinned = contact is not None and contact.pinned_profile
    pin = (
        button(t("btn.unpin", lang), format_callback("unpin", profile.id))
        if pinned
        else button(t("btn.pin", lang), format_callback("pin", profile.id))
    )
    return [
        button(f"👁 {name}", format_callback("profview", profile.id)),
        pin,
        button("🗑", format_callback("removeprofile", profile.id)),
    ]


# --- helpers ------------------------------------------------------------------------------------


async def _contact_profiles(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[StyleProfile, Contact | None]]:
    rows = []
    for profile in await profiles_repo.list_current_contact_profiles(session, user_id):
        contact = (
            await chats_repo.get_contact(session, user_id, profile.contact_id)
            if profile.contact_id is not None
            else None
        )
        rows.append((profile, contact))
    rows.sort(
        key=lambda pc: (
            not (pc[1] is not None and pc[1].pinned_profile),
            -(pc[1].own_message_count_6m if pc[1] is not None else 0),
            pc[1].display_name if pc[1] is not None else "",
        )
    )
    return rows


async def _owned_profile(
    callback: CallbackQuery,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: str,
) -> StyleProfile | None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return None
    try:
        profile_id = uuid.UUID(parse_callback(callback.data).entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("profile.not_found", lang))
        return None
    async with session_factory() as session:
        profile = await profiles_repo.get_profile(session, user.id, profile_id)
    if profile is None or profile.state == ProfileState.retired:
        await callback.answer(t("profile.not_found", lang))
        return None
    return profile


# --- /profile -----------------------------------------------------------------------------------


async def profile_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    async with session_factory() as session:
        profile = await profiles_repo.get_current_profile(
            session, user.id, scope=ProfileScope.global_
        )
        contacts = await profiles_repo.list_current_contact_profiles(session, user.id)
        undo = await last_undoable_refresh(session, user.id)
        rules = await effective_user_rules(session, user.id, profile, lang=lang)
    markup = global_keyboard(profile, len(contacts), settings.max_contact_profiles, undo, lang)
    if profile is None:
        await message.answer(t("profile.none", lang), reply_markup=markup)
        return
    await message.answer(
        profile_text(profile, rules, owner=None, settings=settings, lang=lang),
        reply_markup=markup,
    )


async def contacts_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    await callback.answer()
    await _send_contacts(bot, user, session_factory, settings, lang)


async def _send_contacts(
    bot: Bot,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: str,
) -> None:
    async with session_factory() as session:
        rows = await _contact_profiles(session, user.id)
    total = f"{len(rows)}/{settings.max_contact_profiles}"
    if not rows:
        await bot.send_message(
            user.telegram_control_chat_id, t("profile.contacts_none", lang, total=total)
        )
        return
    lines = [t("profile.contacts", lang, total=total)]
    for profile, contact in rows:
        pin = " 📌" if contact is not None and contact.pinned_profile else ""
        lines.append(f"• {_owner_label(contact, lang)} · v{profile.version}{pin}")
    await bot.send_message(
        user.telegram_control_chat_id,
        "\n".join(lines),
        reply_markup=keyboard([contact_row(p, c, lang) for p, c in rows]),
    )


async def view_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
) -> None:
    profile = await _owned_profile(callback, user, session_factory, lang)
    if profile is None or user is None:
        return
    await callback.answer()
    async with session_factory() as session:
        contact = (
            await chats_repo.get_contact(session, user.id, profile.contact_id)
            if profile.contact_id is not None
            else None
        )
        rules = await effective_user_rules(session, user.id, profile, lang=lang)
    pinned = contact is not None and contact.pinned_profile
    await bot.send_message(
        user.telegram_control_chat_id,
        profile_text(profile, rules, owner=contact, settings=settings, pinned=pinned, lang=lang),
        reply_markup=keyboard(
            [[button(t("btn.edit_rules", lang), format_callback("profrules", profile.id))]]
        ),
    )


async def pin_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
) -> None:
    profile = await _owned_profile(callback, user, session_factory, lang)
    if profile is None or user is None:
        return
    if profile.contact_id is None:
        await callback.answer(t("alert.global_always_kept", lang))
        return
    pin = parse_callback(callback.data).action == "pin"
    async with session_factory() as session:
        await profiles_repo.lock_profiles(session, user.id)  # slots read the pins
        contact = await chats_repo.get_contact(session, user.id, profile.contact_id)
        if contact is None:
            await callback.answer(t("profile.not_found", lang))
            return
        if pin and not contact.pinned_profile:
            pinned = sum(c.pinned_profile for c in await chats_repo.list_contacts(session, user.id))
            if pinned >= settings.max_contact_profiles:
                await callback.answer(t("alert.pin_limit", lang, n=settings.max_contact_profiles))
                return
        contact.pinned_profile = pin
        await session.commit()
    log.info("contact_profile_pin", user_id=str(user.id), pinned=pin)
    await callback.answer(t("alert.pinned" if pin else "alert.unpinned", lang))
    await _send_contacts(bot, user, session_factory, settings, lang)


async def remove_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    profile = await _owned_profile(callback, user, session_factory, lang)
    if profile is None or user is None:
        return
    if profile.contact_id is None:
        await callback.answer(t("alert.global_not_removable", lang))
        return
    async with session_factory() as session:
        contact = await chats_repo.get_contact(session, user.id, profile.contact_id)
        name = _owner_label(contact, lang)
        await remove_contact_profile(session, user.id, profile.contact_id, at=clock())
        await session.commit()
    log.info("contact_profile_removed", user_id=str(user.id), profile_id=str(profile.id))
    await callback.answer(t("alert.removed", lang))
    await bot.send_message(user.telegram_control_chat_id, t("profile.removed", lang, name=name))


# --- rules --------------------------------------------------------------------------------------


def _rules_text(
    rules: AppliedRules, contact: Contact | None, lang: str, *, reserve: int = 0
) -> str:
    """The rules of a profile being edited (``contact`` None: the global profile); ``reserve``:
    UTF-16 code units the caller adds around it (the whole stays within one Telegram
    message)."""
    whose = t("profile.your_global", lang) if contact is None else _owner_label(contact, lang)
    head = t("profile.rules_for", lang, whose=whose)
    tail = ["", t("profile.rules_prompt", lang)]
    budget = TELEGRAM_TEXT_MAX - reserve - utf16_len("\n".join([head, *tail])) - 1
    return "\n".join([head, *rules_lines(rules, lang, budget=budget), *tail])


async def _rules_message(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    profile_id: uuid.UUID,
    lang: str,
) -> str | None:
    async with session_factory() as session:
        profile = await profiles_repo.get_profile(session, user.id, profile_id)
        if profile is None:
            return None
        contact = (
            await chats_repo.get_contact(session, user.id, profile.contact_id)
            if profile.contact_id is not None
            else None
        )
        rules = await effective_user_rules(session, user.id, profile, lang=lang)
    return _rules_text(rules, contact, lang)


async def rules_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    profile = await _owned_profile(callback, user, session_factory, lang)
    if profile is None or user is None:
        return
    await callback.answer()
    await state.set_state(ProfileStates.rules)
    await state.set_data(
        {
            "scope": profile.scope.value,
            "contact_id": str(profile.contact_id) if profile.contact_id else None,
        }
    )
    text = await _rules_message(session_factory, user, profile.id, lang)
    if text is not None:
        await bot.send_message(user.telegram_control_chat_id, text)


async def _current_target(
    session: AsyncSession, user_id: uuid.UUID, data: dict[str, Any]
) -> StyleProfile | None:
    """The current version of the profile being edited (a refresh may have replaced it)."""
    try:
        scope = ProfileScope(str(data.get("scope")))
        contact_id = uuid.UUID(str(data["contact_id"])) if data.get("contact_id") else None
    except (ValueError, KeyError):
        return None
    profile = await profiles_repo.get_current_profile(
        session, user_id, scope=scope, contact_id=contact_id
    )
    return profile if profile is not None and profile.state == ProfileState.current else None


async def rules_input(
    message: Message,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if user is None:
        await state.clear()
        return
    lang = user.language
    data = await state.get_data()
    text = message.text or ""
    profile_id: uuid.UUID | None = None
    async with session_factory() as session:
        # Under the profile lock: a rebuild or refresh storing a new version meanwhile is
        # waited for, and the rule goes to the version that is current after it. The reply is
        # decided here and sent after the lock is released.
        await profiles_repo.lock_profiles(session, user.id)
        profile = await _current_target(session, user.id, data)
        if profile is None:
            reply = t("profile.not_found", lang)
        else:
            contact = (
                await chats_repo.get_contact(session, user.id, profile.contact_id)
                if profile.contact_id is not None
                else None
            )
            global_rules = await inherited_rules_json(session, user.id, profile)
            try:
                removal = _REMOVE.match(text)
                if removal is not None:
                    profile.user_rules_json = with_rule_removed(
                        profile.user_rules_json, int(removal.group(1))
                    )
                    added = True
                else:
                    addition = add_rule(
                        resolve_user_rules(profile, global_rules, lang=lang),
                        profile.user_rules_json,
                        text,
                    )
                    profile.user_rules_json = addition.user_rules_json
                    added = not addition.already_global
            except InvalidRule as exc:
                reply = t(
                    "profile.rule_invalid",
                    lang,
                    problem=exc.msg,
                    prompt=Msg("profile.rules_prompt"),
                )
            else:
                if added:
                    profile_id = profile.id
                    await session.commit()
                    saved = t("settings.saved", lang, settings="")
                    rules = resolve_user_rules(profile, global_rules, lang=lang)
                    reply = t(
                        "settings.saved",
                        lang,
                        settings=_rules_text(rules, contact, lang, reserve=utf16_len(saved)),
                    )
                else:  # issue #42: it applies here already; a copy would outlive its removal
                    reply = t(
                        "profile.rule_typed_already_global",
                        lang,
                        rule=" ".join(text.split()),
                        owner=_owner_label(contact, lang),
                    )
    if profile is None:
        await state.clear()
    if profile_id is not None:
        log.info("profile_rules_edited", user_id=str(user.id), profile_id=str(profile_id))
    await message.answer(reply)


async def rules_done(message: Message, state: FSMContext, lang: Lang) -> None:
    await state.clear()
    await message.answer(t("profile.rules_done", lang))


class AbandonedRulesMiddleware(BaseMiddleware):
    """Any command other than /done or /cancel during ✏️ Edit rules ends that step first, so
    later free text never becomes a rule. Outer middleware on the settings router (next to the
    Trello key and DELETE confirmation ones)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        text = event.text if isinstance(event, Message) else None
        if (
            isinstance(state, FSMContext)
            and text is not None
            and text.startswith("/")
            and not text.startswith(("/done", "/cancel"))
            and await state.get_state() == ProfileStates.rules.state
        ):
            await state.clear()
        return await handler(event, data)


# --- undo / rebuild -----------------------------------------------------------------------------


async def undo_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        batch_id = uuid.UUID(parse_callback(callback.data).entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("alert.nothing_to_undo", lang))
        return
    try:
        result = await undo_refresh(
            session_factory,
            user.id,
            batch_id,
            clock=clock,
            max_profiles=settings.max_contact_profiles,
        )
    except UndoRefused:
        await callback.answer(t("alert.undo_refused", lang))
        return
    await callback.answer(t("alert.restored", lang))
    await bot.send_message(
        user.telegram_control_chat_id, t("profile.restored", lang, n=result.restored)
    )


async def rebuild_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: MessagingGateway,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        step = parse_callback(callback.data).entity_id
    except InvalidCallbackData:
        await callback.answer()
        return
    now = clock()
    if await paused_now(session_factory, settings, user.id, now):  # issue #74
        await callback.answer(paused_alert(user.timezone, now).render(lang), show_alert=True)
        return
    if step != "1":
        await callback.answer()
        await bot.send_message(
            user.telegram_control_chat_id,
            t("profile.rebuild_confirm", lang),
            reply_markup=keyboard(
                [[button(t("btn.rebuild_now", lang), format_callback("profrebuild", 1))]]
            ),
        )
        return
    outcome = await rebuild_profiles(
        bot, user, session_factory, settings, llm_router, gateway, lang, clock,
        wait=False, on_start=lambda: _rebuild_started(bot, callback),
    )  # fmt: skip
    if outcome == RebuildOutcome.busy:
        await callback.answer(t("profile.rebuild_busy", lang))


async def _rebuild_started(bot: Bot, callback: CallbackQuery) -> None:
    await callback.answer()
    await _remove_buttons(bot, callback)


class RebuildOutcome(enum.StrEnum):
    busy = "busy"  # another build holds the lock (``wait=False``): nothing done, nothing sent
    built = "built"  # the result note (rebuild_result_text) was sent
    nothing_new = "nothing_new"  # ``only_if_new_in`` chats had no own messages: nothing built
    failed = "failed"  # the import or the build raised: the failure note was sent
    abandoned = "abandoned"  # the user was deleted meanwhile: no message


async def rebuild_profiles(
    bot: Bot,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: MessagingGateway,
    lang: Lang,
    clock: Clock = utcnow,
    *,
    wait: bool,
    only_if_new_in: Collection[int] | None = None,
    announce: bool = True,
    failure_key: str = "profile.rebuild_failed",
    on_start: Callable[[], Awaitable[None]] | None = None,
) -> RebuildOutcome:
    """The 🔄 Rebuild work: re-import the user's own messages in the enabled work chats and
    rebuild the global and contact profiles (``build_profiles_from_import``, interactive), then
    send one note to the control chat.

    - ``wait``: when another build holds the user's ``profile_build_lock``, ``False`` returns
      ``busy`` at once (the 🔄 button answers "Already rebuilding"); ``True`` waits for it
      (adding chats in /chats, specs/007 FR-015).
    - ``on_start`` runs once the lock is held, before anything is sent.
    - ``announce`` sends "⏳ Reading your own messages…" first.
    - ``only_if_new_in``: Telegram chat ids; when the import has no own message in any of them,
      nothing is built and ``chats.add_nothing_to_learn`` is sent.
    - ``failure_key``: the note when nothing could be rebuilt.
    """
    lock = profile_build_lock(user.id)
    if not wait and lock.locked():
        return RebuildOutcome.busy
    chat_id = user.telegram_control_chat_id
    async with lock:
        if on_start is not None:
            await on_start()
        if wait and not await _user_exists(session_factory, user):  # deleted while waiting
            log.info("profile_rebuild_abandoned", user_id=str(user.id))
            return RebuildOutcome.abandoned
        if announce:
            await bot.send_message(chat_id, t("profile.rebuilding", lang))
        try:
            async with session_factory() as session:  # specs/008: + recent private chats
                targets = await work_account.import_targets(
                    session, gateway, user.id, max_auto=settings.work_account_import_max_chats
                )
            imported = await import_own_messages(
                gateway, session_factory, user.id, settings=settings, clock=clock, targets=targets
            )
            async with session_factory() as session:  # so the new chats rank for profiles
                await work_account.materialise_imported(
                    session, user.id, imported, targets, now=clock()
                )
                await session.commit()
            if only_if_new_in is not None and not any(
                imported.per_chat_counts.get(c, 0) for c in only_if_new_in
            ):
                if not await _user_exists(session_factory, user):
                    return RebuildOutcome.abandoned
                log.info("profile_rebuild_skipped", user_id=str(user.id), reason="nothing_new")
                months = settings.style_import_months
                await bot.send_message(
                    chat_id, t("chats.add_nothing_to_learn", lang, months=months)
                )
                return RebuildOutcome.nothing_new
            summary = await build_profiles_from_import(
                session_factory,
                llm_router,
                user.id,
                imported,
                settings=settings,
                clock=clock,
                interactive=True,
            )
        except Exception as exc:
            if not await _user_exists(session_factory, user):  # /delete_my_data meanwhile
                log.info("profile_rebuild_abandoned", user_id=str(user.id))
                return RebuildOutcome.abandoned
            log.error("profile_rebuild_failed", user_id=str(user.id), error=type(exc).__name__)
            await bot.send_message(chat_id, t(failure_key, lang))
            return RebuildOutcome.failed
    text = rebuild_result_text(summary, settings=settings, lang=lang, failure_key=failure_key)
    await bot.send_message(chat_id, text)
    return RebuildOutcome.built


def rebuild_result_text(
    summary: BuildSummary,
    *,
    settings: Settings,
    lang: Lang,
    failure_key: str = "profile.rebuild_failed",
) -> str:
    """The note after a Rebuild. The global and the contact profiles fail independently: a
    failed global build keeps the old global profile (said so), failed contact builds are
    counted; nothing rebuilt at all is ``failure_key`` ("couldn't rebuild, nothing changed")."""
    contacts = t("profile.rebuilt_contacts", lang, n=summary.contact_profiles)
    if summary.global_profile is None:
        if not summary.contact_profiles:
            return t(failure_key, lang)
        text = t("profile.rebuilt_global_failed", lang, contacts=contacts)
    else:
        text = t("profile.rebuilt", lang, n=summary.global_profile.sample_size, contacts=contacts)
    if summary.contact_failures:
        text += "\n" + t("profile.rebuild_contacts_failed", lang, n=summary.contact_failures)
    if summary.global_profile is not None and is_weak(summary.global_profile):
        text += "\n" + _weak_text(settings, lang)
    return text


async def _user_exists(session_factory: async_sessionmaker[AsyncSession], user: User) -> bool:
    async with session_factory() as session:
        return await users_repo.get_user(session, user.id) is not None


async def _remove_buttons(bot: Bot, callback: CallbackQuery) -> None:
    """Remove the tapped message's buttons (best effort: it may be too old to edit)."""
    if callback.message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=None,
        )
    except Exception as exc:
        log.warning("control_message_edit_failed", error=type(exc).__name__)


# --- proposals ----------------------------------------------------------------------------------


async def rule_accept_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    proposal_id = await _proposal_id(callback, user, lang)
    if proposal_id is None or user is None:
        return
    try:
        rule, owner, already_global = await proposals.accept_proposal(
            session_factory, user.id, proposal_id, lang=lang, clock=clock
        )
    except proposals.ProposalClosed:
        await callback.answer(t("alert.proposal_closed", lang))
        return
    except InvalidRule as exc:  # e.g. the profile has the maximum number of rules
        await callback.answer(exc.text(lang), show_alert=True)  # the proposal stays open
        return
    key = next((k for k, spec in proposals.FEATURES.items() if spec.rule == rule), None)
    label = proposals.rule_label(rule, key, lang)
    if already_global:
        await callback.answer()
        await _close_message(
            bot, callback, t("profile.rule_already_global", lang, rule=label, owner=owner or "")
        )
        return
    await callback.answer(t("alert.rule_added", lang))
    if owner:
        text = t("profile.rule_added_contact", lang, rule=label, owner=owner)
    else:
        text = t("profile.rule_added_global", lang, rule=label)
    await _close_message(bot, callback, text)


async def rule_dismiss_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    proposal_id = await _proposal_id(callback, user, lang)
    if proposal_id is None or user is None:
        return
    try:
        await proposals.dismiss_proposal(session_factory, user.id, proposal_id)
    except proposals.ProposalClosed:
        await callback.answer(t("alert.proposal_closed", lang))
        return
    await callback.answer(t("alert.dismissed", lang))
    await _close_message(bot, callback, t("profile.proposal_dismissed", lang))


async def _proposal_id(callback: CallbackQuery, user: User | None, lang: str) -> uuid.UUID | None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return None
    try:
        return uuid.UUID(parse_callback(callback.data).entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("alert.proposal_closed", lang))
        return None


async def _close_message(bot: Bot, callback: CallbackQuery, text: str) -> None:
    if callback.message is None:
        return
    await bot.edit_message_text(
        text=text, chat_id=callback.message.chat.id, message_id=callback.message.message_id
    )


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


def create_router(idle: StateFilter) -> Router:
    """``idle``: the states in which control-chat commands answer (see ``settings.py``)."""
    router = Router(name="profile")
    router.message.register(profile_command, Command("profile"), idle)
    router.message.register(rules_done, Command("done", "cancel"), StateFilter(ProfileStates))
    router.message.register(
        rules_input, StateFilter(ProfileStates.rules), F.text & ~F.text.startswith("/")
    )
    router.callback_query.register(contacts_callback, _action("profcontacts"), idle)
    router.callback_query.register(view_callback, _action("profview"), idle)
    router.callback_query.register(pin_callback, _action("pin"), idle)
    router.callback_query.register(pin_callback, _action("unpin"), idle)
    router.callback_query.register(remove_callback, _action("removeprofile"), idle)
    router.callback_query.register(rules_callback, _action("profrules"), idle)
    router.callback_query.register(undo_callback, _action("undo_refresh"), idle)
    router.callback_query.register(rebuild_callback, _action("profrebuild"), idle)
    router.callback_query.register(rule_accept_callback, _action("rule_accept"), idle)
    router.callback_query.register(rule_dismiss_callback, _action("rule_dismiss"), idle)
    return router


__all__ = [
    "AbandonedRulesMiddleware",
    "ProfileStates",
    "RebuildOutcome",
    "create_router",
    "profile_text",
    "rebuild_profiles",
    "rebuild_result_text",
]
