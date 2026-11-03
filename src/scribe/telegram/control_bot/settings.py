"""Control-chat settings and timing commands (US2; contracts/control-chat.md).

- ``/settings`` (FR-016, FR-017): global reading-delay range, working hours, working days,
  time zone and the bot's language. Values are typed as a reply (delay, hours, time zone) or
  toggled (days, language) and validated before they are stored: ``0 <= min <= max <= 86400``
  (min = max gives a fixed delay), ``start < end``, at least one working day, an IANA time zone
  known to ``zoneinfo``. The time zone accepts human input (city, Russian alias, offset;
  ``timezones.py``) and offers quick buttons picked by the Telegram language; ambiguous input
  offers the matching zones as buttons. Changing hours, days or time zone re-plans the user's
  waiting turns. The language (issue #8) applies at once: the confirmation and every later
  message are in the new language, and the chat's ``/`` menu is switched too.
  🔎 Search earlier conversations (specs/004 S9): ``settings:search_earlier`` flips
  ``users.search_earlier_messages`` on each press and re-renders the /settings message.
  🔵 Mark chats unread after the agent (specs/009): ``settings:mark_unread`` flips
  ``users.mark_unread`` the same way (no popup); nothing is marked or unmarked by the press.
  👤 Gender in drafts (issue #46): ``settings:gender`` turns the /settings message into the two
  forms (``setgender:<masculine|feminine>:settings``, ``grammatical_gender.py``) with the current
  one ticked; a press stores ``users.grammatical_gender`` and renders /settings again in place.
  A press on the one-off question (``setgender:<masculine|feminine>``) stores it, ticks it and
  confirms.
- ``/chats`` (FR-024): the user's work chats (enabled ⚙️, disabled ⏸), each with a settings
  screen: reading-delay override (set or clear), language override (BCP-47, e.g. ``en``,
  ``pt-BR``), formality 1–5 and enable / disable. Group chats state the fixed rule (drafts only
  when the user is tagged or replied to; specs/006-group-mention-only) and have no mode to
  pick. Overrides win over the selected style profile (``drafting/composer.py``). Disabling a
  chat drops its waiting turns and its pending drafts with their turns (``chat_disabled``; the
  draft cards say so); a turn being read and drafted at that moment is ended by the composer
  without a draft. Nothing more is stored from the chat. Under the list, ➕ Add work chats
  (``chatadd:0``, set-up users only; ``add_chats.py``, specs/007) opens the setup chat picker
  with the chats that are not work chats yet; the ones picked are added enabled and learned
  for the style profiles like 🔄 Rebuild. ``/history`` shows it as "➕ Work chats added".
- ``/waiting`` (FR-018): waiting messages with their planned reading time in the user's time
  zone and a 👀 Read now button (``readnow:<turn_id>``, handled in ``review.py``).
- ``/pause`` / ``/resume`` (FR-025): toggle ``users.state``. While paused, messages are still
  received and stored but stay unread and are not drafted (the scheduler skips paused users);
  /resume re-plans waiting turns so overdue ones are read at the next tick inside working hours.

The commands answer when no other flow is active (no FSM state, a settings question, or a draft
edit); during setup or Telegram login those flows handle commands themselves.

- ``/ooo [text]`` (FR-025): out of office on with an optional suggested reply; ``/ooo`` alone
  turns it off. ``users.ooo_message`` is non-NULL exactly while out of office (also during a
  /pause from it, so /resume restores it); drafts become out-of-office replies meanwhile.
- ``/history`` (FR-027): the last 20 activity records (what happened, when, for which contact),
  never message content. Skipped messages (``skipped``, with the reason) and "Draft anyway"
  (``draft_anyway``) appear there too (specs/004-skip-closing-messages).

004: ``/skipped`` (``skipped.py``) lists the messages the reply gate skipped; each chat screen
in ``/chats`` also carries the chat's "Skip thanks/bye" value (``chatskip:<chat_id>:<value>``,
the same ``work_chats.skip_closing`` as ``skipchat:`` in ``/skipped``).

US3: ``/tasks`` (``tasks_command.py``) and ``/commitments`` (``commitments_command.py``), US4:
``/profile`` (``profile_command.py``), ``/my_data`` and ``/delete_my_data``
(``account_command.py``) are included in this router with the same "no other flow is active"
rule.

Work account mode (specs/008, ``scribe.chats.work_account``): ``/settings`` shows the mode (with
the number of excluded chats) and ``settings:work_account`` asks to turn it on or off
(``wacct:<on|off|cancel>``; turning off pauses the chats it picked up and drops their waiting
messages and pending drafts, the cards say so). ``/chats`` then starts with the mode's line,
marks chats picked up by the mode ``· 💼`` and excluded ones 🚫, pages the list
(``chatspage:<n>``, 8 per page) and offers 🚫 Exclude chats… (``wxopen:0``, ``exclusions.py``);
while the mode is off its chats are hidden and summarised in one line. On a private chat's
screen the enable row reads 🚫 Exclude / ↩️ Include again while the mode is on (and for every
chat it picked up). ``/history`` names the chat of a pick-up, exclusion or inclusion.

Callbacks:
``settings:<delay|hours|days|tz|lang|search_earlier|mark_unread|work_account|gender>``,
``setgender:<masculine|feminine>[:settings]``, ``setday:<0-6|done>``, ``settz:<zone>``,
``wacct:<on|off|cancel>``, ``chatspage:<n>``, ``wxopen:0``,
``setlang:<en|ru>``, ``chatcfg:<chat_id>``, ``chatdelay:<chat_id>[:clear]``,
``chatlang:<chat_id>[:clear]``, ``chatformal:<chat_id>:<1-5|clear>``,
``chatenable:<chat_id>:<on|off>``, ``chatskip:<chat_id>:<inherit|off|on>``, ``chatadd:0`` (with
the picker's ``pickchat:`` / ``pickpage:`` / ``pickdone:``, ``chat_picker.py``); the legacy
``chatgroup:<chat_id>:<mentions_only|always>`` of old settings cards only answers "no longer
available" (006). Callbacks act
only on the caller's own rows (``ownership.OwnershipMiddleware`` on this router and the routers
it includes, plus scoped loads). Messages are plain text (no parse mode), in the user's language
(``scribe.i18n``). No message content is logged.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import ApplyStates
from scribe.chats import work_account
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.context.history import turn_messages
from scribe.db.models import (
    MAX_DELAY_S,
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Contact,
    ConversationTurn,
    Draft,
    DroppedReason,
    TurnState,
    User,
    UserState,
    WorkChat,
    WorkChatOrigin,
)
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import (
    LANGUAGES,
    Lang,
    LocalizedError,
    Msg,
    format_day_month,
    format_when,
    has_text,
    language_name,
    normalize_lang,
    t,
    weekday_abbr,
)
from scribe.intake.waiting import replan_waiting_turns
from scribe.logging import get_logger
from scribe.style.grammatical_gender import GRAMMATICAL_GENDERS
from scribe.telegram.control_bot import (
    account_command,
    add_chats,
    chat_picker,
    commitments_command,
    exclusions,
    grammatical_gender,
    jira_connect,
    profile_command,
    skipped,
    tasks_command,
)
from scribe.telegram.control_bot.grammatical_gender import (
    FROM_SETTINGS,
    GENDER_ACTION,
    gender_keyboard,
    gender_label,
)
from scribe.telegram.control_bot.menu import set_chat_commands
from scribe.telegram.control_bot.ownership import OwnershipMiddleware
from scribe.telegram.control_bot.render import (
    InvalidCallbackData,
    button,
    chat_skip_buttons,
    chat_skip_choice,
    format_callback,
    keyboard,
    parse_callback,
    skip_reason,
)
from scribe.telegram.control_bot.review import DraftPresenter, ReviewStates
from scribe.telegram.control_bot.timezones import (
    AmbiguousTimeZone,
    ResolvedTimeZone,
    UnknownTimeZone,
    confirmation_text,
    quick_zones,
    resolve_time_zone,
    time_zone_keyboard,
    zone_from_callback,
    zone_label,
)

log = get_logger("scribe.control_bot")

WAITING_LIST_MAX = 10
PREVIEW_CHARS = 120
SEARCH_EARLIER = "search_earlier"  # settings:search_earlier (specs/004 contracts/control-chat.md)
WORK_ACCOUNT = "work_account"  # settings:work_account (specs/008 contracts/control-chat.md)
CHAT_EXCLUDED_STATUS = Msg("status.chat_excluded")
WORK_ACCOUNT_OFF_STATUS = Msg("status.work_account_off")
MARK_UNREAD = "mark_unread"  # settings:mark_unread (specs/009 contracts/control-chat.md)
GENDER = "gender"  # settings:gender (issue #46): the grammatical gender buttons


OOO_MAX_CHARS = 500
HISTORY_LIMIT = 20
CHAT_DISABLED_STATUS = Msg("status.chat_disabled")
CHAT_SKIP_VALUES: dict[str, bool | None] = {"inherit": None, "off": False, "on": True}


class SettingsStates(StatesGroup):
    delay = State()
    hours = State()
    timezone = State()
    chat_delay = State()
    chat_language = State()


class InvalidSetting(LocalizedError):
    """User input that does not pass validation; the message is shown to the user."""


# --- parsing and validation (pure) --------------------------------------------------------------

# English and Russian units ("2-10 min", "2-10 мин", "90 с", "1-2 ч").
_UNITS = {
    "s": 1,
    "sec": 1,
    "m": 60,
    "min": 60,
    "h": 3600,
    "hr": 3600,
    "с": 1,
    "сек": 1,
    "м": 60,
    "мин": 60,
    "ч": 3600,
    "час": 3600,
    "часа": 3600,
    "часов": 3600,
}
_PART = re.compile(r"^\s*(\d+)\s*([a-zа-яё]*)\.?\s*$")
_HHMM = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$")


def _split_unit(part: str) -> tuple[int, str | None]:
    match = _PART.match(part.lower())
    if match is None:
        raise InvalidSetting("err.units")
    unit = match.group(2) or None
    if unit is not None and unit not in _UNITS:
        raise InvalidSetting("err.unknown_unit", unit=unit)
    return int(match.group(1)), unit


def parse_delay_range(text: str) -> tuple[int, int]:
    """``"2-10 min"`` -> (120, 600); ``"90-300 s"``; ``"5 min"`` -> (300, 300) fixed.

    A unit given only on the upper bound applies to both; numbers without any unit are minutes.
    Validates ``0 <= min <= max <= 86400``.
    """
    parts = re.split(r"\s*[-–—]\s*", text.strip())
    if not 1 <= len(parts) <= 2 or not all(parts):
        raise InvalidSetting("err.range_format")
    values = [_split_unit(part) for part in parts]
    default_unit = values[-1][1] or "min"
    seconds = [number * _UNITS[unit or default_unit] for number, unit in values]
    low, high = seconds[0], seconds[-1]
    if low > high:
        raise InvalidSetting("err.min_gt_max")
    if high > MAX_DELAY_S:
        raise InvalidSetting("err.max_delay")
    return low, high


def _parse_time(part: str) -> time:
    match = _HHMM.match(part)
    if match is None:
        raise InvalidSetting("err.hhmm")
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        raise InvalidSetting("err.hours_range")
    return time(hour, minute)


def parse_working_hours(text: str) -> tuple[time, time]:
    """``"09:00-19:00"`` or ``"9-19"``; the start must be earlier than the end."""
    parts = re.split(r"\s*[-–—]\s*", text.strip())
    if len(parts) != 2:
        raise InvalidSetting("err.start_end")
    start, end = _parse_time(parts[0]), _parse_time(parts[1])
    if start >= end:
        raise InvalidSetting("err.start_before_end")
    return start, end


def parse_time_zone(text: str, now: datetime | None = None) -> str:
    """Human time zone input -> an IANA name ``zoneinfo`` can load (``timezones.py``)."""
    try:
        return resolve_time_zone(text, now or utcnow()).zone
    except AmbiguousTimeZone as exc:
        choices = ", ".join(exc.choices)
        raise InvalidSetting("err.tz_pick", problem=exc.msg, choices=choices) from exc
    except UnknownTimeZone as exc:
        raise InvalidSetting(exc.msg.key, **exc.msg.params) from exc


LANGUAGE_NAMES = {
    "english": "en",
    "английский": "en",
    "russian": "ru",
    "русский": "ru",
    "german": "de",
    "немецкий": "de",
    "french": "fr",
    "французский": "fr",
    "spanish": "es",
    "испанский": "es",
    "ukrainian": "uk",
    "украинский": "uk",
    "kazakh": "kk",
    "казахский": "kk",
    "italian": "it",
    "portuguese": "pt",
    "chinese": "zh",
    "turkish": "tr",
}
_BCP47 = re.compile(r"^([a-zA-Z]{2,3})(?:-([a-zA-Z]{4}))?(?:-([a-zA-Z]{2}|\d{3}))?$")


def parse_language(text: str) -> str:
    """A BCP-47 language tag (``en``, ``pt-BR``, ``zh-Hant``, ``es-419``) or a known name,
    normalised (language lower case, script title case, region upper case)."""
    value = text.strip()
    named = LANGUAGE_NAMES.get(value.casefold())
    if named is not None:
        return named
    match = _BCP47.match(value.replace("_", "-"))
    if match is None:
        raise InvalidSetting("err.chat_language")
    language, script, region = match.groups()
    parts = [language.lower()]
    if script:
        parts.append(script.title())
    if region:
        parts.append(region.upper())
    return "-".join(parts)


def toggle_day(days: list[int], day: int) -> list[int]:
    """Toggle ``day`` (0 = Monday); at least one working day must stay."""
    if not 0 <= day <= 6:
        raise InvalidSetting("err.unknown_day")
    toggled = sorted(set(days) ^ {day})
    if not toggled:
        raise InvalidSetting("err.keep_one_day")
    return toggled


# --- rendering ----------------------------------------------------------------------------------


def delay_label(min_s: int, max_s: int, lang: str) -> str:
    def fmt(seconds: int) -> str:
        if seconds and seconds % 3600 == 0:
            return t("unit.h", lang, n=seconds // 3600)
        if seconds and seconds % 60 == 0:
            return t("unit.min", lang, n=seconds // 60)
        return t("unit.s", lang, n=seconds)

    if min_s == max_s:
        return t("settings.delay_fixed", lang, value=fmt(min_s))
    return t("settings.delay_random", lang, low=fmt(min_s), high=fmt(max_s))


def days_label(days: list[int], lang: str) -> str:
    return ", ".join(weekday_abbr(d, lang) for d in sorted(days))


def _zone_text(zone: str) -> str:
    # Etc/GMT-3 is UTC+3 (POSIX sign): spell the offset out so the inverted sign isn't confusing.
    return zone_label(zone, utcnow()) if zone.startswith("Etc/") else zone


def work_account_line(user: User, excluded: int | None, lang: str) -> str:
    """The /settings line of work account mode (specs/008); ``excluded`` None: not counted."""
    if not user.work_account:
        return t("settings.work_account.off", lang)
    if excluded is None:
        return t("settings.work_account.on_plain", lang)
    return t("settings.work_account.on", lang, n=excluded)


def settings_text(user: User, excluded: int | None = None) -> str:
    """``excluded``: the number of excluded chats for the work account line (specs/008)."""
    lang = user.language
    state = t(f"settings.state.{user.state.value}", lang)
    delay = delay_label(user.delay_min_s, user.delay_max_s, lang)
    start, end = f"{user.work_start:%H:%M}", f"{user.work_end:%H:%M}"
    search = "on" if user.search_earlier_messages else "off"
    mark = "on" if user.mark_unread else "off"
    return "\n".join(
        [
            t("settings.title", lang),
            t("settings.delay", lang, value=delay),
            t("settings.hours", lang, start=start, end=end),
            t("settings.days", lang, days=days_label(user.working_days, lang)),
            t("settings.tz", lang, zone=_zone_text(user.timezone)),
            t("settings.language", lang, language=language_name(lang)),
            t("settings.drafting", lang, state=state),
            t(
                "settings.search_earlier",
                lang,
                state=t(f"settings.search_earlier_state.{search}", lang),
            ),
            t("settings.mark_unread", lang, state=t(f"settings.mark_unread_state.{mark}", lang)),
            work_account_line(user, excluded, lang),
            t("settings.gender", lang, value=gender_label(user.grammatical_gender, lang)),
            "",
            t("settings.per_chat", lang),
        ]
    )


def settings_keyboard(
    lang: str,
    search_earlier: bool = True,
    work_account: bool = False,
    mark_unread: bool = True,
) -> InlineKeyboardMarkup:
    """``search_earlier``: the current ``users.search_earlier_messages`` (specs/004);
    ``work_account``: the current ``users.work_account`` (specs/008); ``mark_unread``: the
    current ``users.mark_unread`` (specs/009)."""
    search = "btn.search_earlier_off" if search_earlier else "btn.search_earlier_on"
    mode = "btn.work_account_off" if work_account else "btn.work_account_on"
    mark = "btn.mark_unread_off" if mark_unread else "btn.mark_unread_on"
    return keyboard(
        [
            [
                button(t("btn.delay", lang), format_callback("settings", "delay")),
                button(t("btn.hours", lang), format_callback("settings", "hours")),
            ],
            [
                button(t("btn.days", lang), format_callback("settings", "days")),
                button(t("btn.tz", lang), format_callback("settings", "tz")),
            ],
            [button(t("btn.language", lang), format_callback("settings", "lang"))],
            [button(t(search, lang), format_callback("settings", SEARCH_EARLIER))],
            [button(t(mark, lang), format_callback("settings", MARK_UNREAD))],
            [button(t(mode, lang), format_callback("settings", WORK_ACCOUNT))],
            [button(t("btn.gender", lang), format_callback("settings", GENDER))],
        ]
    )


def days_keyboard(days: list[int], lang: str) -> InlineKeyboardMarkup:
    row = [
        button(
            ("✅ " if d in days else "⬜ ") + weekday_abbr(d, lang), format_callback("setday", d)
        )
        for d in range(7)
    ]
    done = button(t("btn.done", lang), format_callback("setday", "done"))
    return keyboard([row[:4], row[4:], [done]])


def language_keyboard(current: str) -> InlineKeyboardMarkup:
    """One button per language, named in that language; the current one is ticked."""
    return keyboard(
        [
            [
                button(
                    ("✅ " if code == current else "") + language_name(code),
                    format_callback("setlang", code),
                )
                for code in LANGUAGES
            ]
        ]
    )


def chat_settings_text(chat: WorkChat, user: User) -> str:
    lang = user.language
    if chat.override_delay_min_s is not None and chat.override_delay_max_s is not None:
        delay = delay_label(chat.override_delay_min_s, chat.override_delay_max_s, lang)
    else:
        global_delay = delay_label(user.delay_min_s, user.delay_max_s, lang)
        delay = t("settings.chat_global", lang, value=global_delay)
    lines = [
        t("settings.chat", lang, title=chat.title or t("common.chat", lang), delay=delay),
        t(
            "settings.chat_language",
            lang,
            language=chat.override_language or t("settings.chat_language_auto", lang),
        ),
        t(
            "settings.chat_formality",
            lang,
            value=(
                t("settings.chat_formality_value", lang, n=chat.override_formality)
                if chat.override_formality is not None
                else t("settings.chat_formality_profile", lang)
            ),
        ),
    ]
    if chat.kind != ChatKind.private:
        lines.append(t("settings.chat_group", lang))  # 006: one rule, nothing to choose
    skip = chat_skip_choice(chat.skip_closing, user.skip_closing, lang)
    lines.append(t("settings.chat_skip", lang, value=skip))  # 004 FR-014
    if _work_account_screen(chat, user.work_account):
        lines.append(t(f"settings.chat_origin.{_origin_state(chat)}", lang))
    else:
        lines.append(t("settings.chat_enabled" if chat.enabled else "settings.chat_disabled", lang))
    return "\n".join(lines)


def _work_account_screen(chat: WorkChat, work_account: bool) -> bool:
    """specs/008: the chat screen speaks of exclusion (not enable / disable) for a private
    chat while the mode is on, and for every chat the mode picked up."""
    return chat.kind == ChatKind.private and (work_account or chat.origin == WorkChatOrigin.auto)


def _origin_state(chat: WorkChat) -> str:
    if chats_repo.is_excluded(chat):
        return "excluded"
    if chat.origin == WorkChatOrigin.auto:
        return "auto" if chat.enabled else "paused"
    return "picked"


def chat_settings_keyboard(
    chat: WorkChat, lang: str, *, global_skip: bool = True, work_account: bool = False
) -> InlineKeyboardMarkup:
    """``work_account``: the user's mode (specs/008): Exclude / Include again on private chats."""
    row = [button(t("btn.set_delay", lang), format_callback("chatdelay", chat.id))]
    if chat.override_delay_min_s is not None:
        use_global = format_callback("chatdelay", chat.id, "clear")
        row.append(button(t("btn.use_global", lang), use_global))
    language = [button(t("btn.chat_language", lang), format_callback("chatlang", chat.id))]
    if chat.override_language is not None:
        auto = format_callback("chatlang", chat.id, "clear")
        language.append(button(t("btn.chat_language_auto", lang), auto))
    formality = [
        button(("• " if chat.override_formality == n else "") + f"🎩{n}",
               format_callback("chatformal", chat.id, n))
        for n in range(1, 6)
    ]  # fmt: skip
    if chat.override_formality is not None:
        formality.append(button("↩️", format_callback("chatformal", chat.id, "clear")))
    rows = [row, language, formality]
    # 004: Skip thanks/bye — Global / Always draft / Skip.
    rows.append(chat_skip_buttons("chatskip", chat.id, chat.skip_closing, global_skip, lang))
    if _work_account_screen(chat, work_account):
        if chats_repo.is_excluded(chat):
            label, value = "btn.chat_include", "on"
        else:
            label, value = "btn.chat_exclude", "off"
        rows.append([button(t(label, lang), format_callback("chatenable", chat.id, value))])
        return keyboard(rows)
    rows.append(
        [button(t("btn.chat_disable", lang), format_callback("chatenable", chat.id, "off"))]
        if chat.enabled
        else [button(t("btn.chat_enable", lang), format_callback("chatenable", chat.id, "on"))]
    )
    return keyboard(rows)


def waiting_item_text(
    contact: str,
    arrived_at: datetime,
    due_at: datetime,
    preview: str,
    tz: str,
    now: datetime,
    lang: str,
) -> str:
    flat = " ".join(preview.split())
    if len(flat) > PREVIEW_CHARS:
        flat = flat[: PREVIEW_CHARS - 1].rstrip() + "…"
    when = t("waiting.now", lang) if due_at <= now else format_when(due_at, now, tz, lang)
    return t(
        "waiting.item",
        lang,
        contact=contact or t("common.someone", lang),
        arrived=format_when(arrived_at, now, tz, lang),
        when=when,
        preview=flat,
    )


# --- handlers -----------------------------------------------------------------------------------


async def _fresh_user(session_factory: async_sessionmaker[AsyncSession], user: User) -> User | None:
    async with session_factory() as session:
        return await users_repo.get_user(session, user.id)


async def settings_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    current = await _fresh_user(session_factory, user) or user
    text = settings_text(current, await _excluded_count(session_factory, current))
    await message.answer(text, reply_markup=_settings_keyboard_of(current))


def _settings_keyboard_of(user: User) -> InlineKeyboardMarkup:
    return settings_keyboard(
        user.language, user.search_earlier_messages, user.work_account, user.mark_unread
    )


async def _excluded_count(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> int | None:
    if not user.work_account:
        return None
    async with session_factory() as session:
        return await chats_repo.count_excluded(session, user.id)


async def _toggle_search_earlier(
    callback: CallbackQuery,
    bot: Bot,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """``settings:search_earlier`` (specs/004 S9): each press flips
    ``users.search_earlier_messages`` and re-renders the /settings message."""
    current = await _fresh_user(session_factory, user) or user
    stored = await _save_user(
        session_factory,
        current,
        clock,
        replan=False,
        search_earlier_messages=not current.search_earlier_messages,
    )
    if stored is None:
        await callback.answer(t("common.not_set_up", current.language))
        return
    await callback.answer(t("alert.saved", stored.language))
    await _rerender_settings(callback, bot, stored, session_factory)


async def _toggle_mark_unread(
    callback: CallbackQuery,
    bot: Bot,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """``settings:mark_unread`` (specs/009): each press flips ``users.mark_unread`` and
    re-renders the /settings message; no popup, no chat is marked or unmarked by it."""
    current = await _fresh_user(session_factory, user) or user
    stored = await _save_user(
        session_factory, current, clock, replan=False, mark_unread=not current.mark_unread
    )
    if stored is None:
        await callback.answer(t("common.not_set_up", current.language))
        return
    await callback.answer()
    await _rerender_settings(callback, bot, stored, session_factory)


async def _rerender_settings(
    callback: CallbackQuery,
    bot: Bot,
    stored: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Edit the /settings message in place, or send a fresh one when it cannot be edited."""
    chat_id = stored.telegram_control_chat_id
    excluded = await _excluded_count(session_factory, stored)
    text, markup = settings_text(stored, excluded), _settings_keyboard_of(stored)
    if callback.message is not None:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=callback.message.message_id, reply_markup=markup
            )
            return
        except Exception as exc:  # message too old, not a text message, ...
            log.info("settings_edit_failed", error=type(exc).__name__)
    await bot.send_message(chat_id, text, reply_markup=markup)


async def settings_menu(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
    settings: Settings | None = None,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        item = parse_callback(callback.data).entity_id
    except InvalidCallbackData:
        await callback.answer()
        return
    if item == SEARCH_EARLIER:
        await _toggle_search_earlier(callback, bot, user, session_factory, clock)
        return
    if item == WORK_ACCOUNT:
        await _confirm_work_account(callback, bot, user, session_factory, settings)
        return
    if item == MARK_UNREAD:
        await _toggle_mark_unread(callback, bot, user, session_factory, clock)
        return
    await callback.answer()
    chat_id = user.telegram_control_chat_id
    if item == "days":
        await bot.send_message(
            chat_id,
            t("settings.days_prompt", lang),
            reply_markup=days_keyboard(user.working_days, lang),
        )
        return
    if item == "lang":
        await bot.send_message(
            chat_id, t("settings.language_prompt", lang), reply_markup=language_keyboard(lang)
        )
        return
    if item == GENDER:
        current = await _fresh_user(session_factory, user) or user
        text = t("settings.gender_prompt", lang)
        markup = gender_keyboard(current.grammatical_gender, lang, from_settings=True)
        if callback.message is not None:
            try:  # the /settings message becomes the choice; a press renders it back
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=callback.message.message_id,
                    reply_markup=markup,
                )  # fmt: skip
                return
            except Exception as exc:  # message too old, not a text message, ...
                log.info("settings_edit_failed", error=type(exc).__name__)
        await bot.send_message(chat_id, text, reply_markup=markup)
        return
    if item == "tz":
        await state.set_state(SettingsStates.timezone)
        zones = quick_zones(callback.from_user.language_code)
        await bot.send_message(
            chat_id,
            t("settings.tz_prompt", lang),
            reply_markup=time_zone_keyboard("settz", zones, clock(), lang),
        )
        return
    prompts = {
        "delay": (SettingsStates.delay, "settings.delay_prompt"),
        "hours": (SettingsStates.hours, "settings.hours_prompt"),
    }
    if item not in prompts:
        return
    next_state, prompt = prompts[item]
    await state.set_state(next_state)
    await bot.send_message(chat_id, t(prompt, lang))


# --- work account mode (specs/008) ------------------------------------------------------------


async def _confirm_work_account(
    callback: CallbackQuery,
    bot: Bot,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None,
) -> None:
    """``settings:work_account``: the turn-on or turn-off confirmation, with what it means."""
    current = await _fresh_user(session_factory, user) or user
    lang = current.language
    await callback.answer()
    if current.work_account:
        async with session_factory() as session:
            preview = await work_account.preview_off(session, current.id)
        text = t(
            "settings.work_account_confirm_off",
            lang,
            chats=preview.chats,
            drafts=preview.pending_drafts,
            waiting=preview.waiting,
        )
        confirm = button(t("btn.wacct_off", lang), format_callback("wacct", "off"))
    else:
        cap = (
            settings.work_account_import_max_chats
            if settings is not None
            else Settings.model_fields["work_account_import_max_chats"].default
        )
        text = t("settings.work_account_confirm_on", lang, n=cap)
        confirm = button(t("btn.wacct_on", lang), format_callback("wacct", "on"))
    cancel = button(t("btn.wacct_cancel", lang), format_callback("wacct", "cancel"))
    await bot.send_message(
        current.telegram_control_chat_id, text, reply_markup=keyboard([[confirm, cancel]])
    )


async def work_account_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``wacct:<on|off|cancel>``. A button for the state the mode is already in (an old
    confirmation) changes nothing and shows the current /settings."""
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        choice = parse_callback(callback.data).entity_id
    except InvalidCallbackData:
        choice = ""
    if choice not in {"on", "off", "cancel"}:
        await callback.answer()
        return
    await _close_buttons(bot, callback)
    if choice == "cancel":
        await callback.answer(t("settings.work_account_cancelled", lang))
        return
    now = clock()
    result = await work_account.switch(session_factory, user.id, on=choice == "on", now=now)
    current = await _fresh_user(session_factory, user) or user
    chat_id = current.telegram_control_chat_id
    if not result.changed:
        await callback.answer(t("settings.work_account_cancelled", lang))
    else:
        await work_account.reconcile_user(session_factory, current.id, now=now)
        presenter = DraftPresenter(bot, session_factory, clock=clock)
        for draft_id in result.dropped_draft_ids:
            await presenter.show_status(current.id, draft_id, WORK_ACCOUNT_OFF_STATUS)
        if choice == "on":
            note = t("settings.work_account_now_on", lang)
        else:
            note = t("settings.work_account_now_off", lang, n=result.paused_chats)
        await callback.answer()
        await bot.send_message(chat_id, note)
    excluded = await _excluded_count(session_factory, current)
    await bot.send_message(
        chat_id, settings_text(current, excluded), reply_markup=_settings_keyboard_of(current)
    )


async def _close_buttons(bot: Bot, callback: CallbackQuery) -> None:
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
        log.info("control_message_edit_failed", error=type(exc).__name__)


async def _save_user(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    clock: Clock,
    *,
    replan: bool,
    **values: Any,
) -> User | None:
    async with session_factory() as session:
        stored = await users_repo.update_user(session, user.id, **values)
        if stored is not None and replan:
            moved = await replan_waiting_turns(session, stored, clock())
            log.info("waiting_turns_replanned", user_id=str(user.id), moved=moved)
        await session.commit()
    log.info("settings_changed", user_id=str(user.id), fields=sorted(values))
    return stored


def _saved_text(user: User) -> str:
    return t("settings.saved", user.language, settings=settings_text(user))


async def _apply_text_setting(
    message: Message,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    parse: Any,
    fields: tuple[str, ...],
    *,
    replan: bool,
) -> None:
    if user is None:
        await state.clear()
        return
    try:
        parsed = parse(message.text or "")
    except InvalidSetting as exc:
        await message.answer(t("common.try_again", user.language, problem=exc.msg))
        return
    values = dict(zip(fields, parsed if isinstance(parsed, tuple) else (parsed,), strict=True))
    await state.clear()
    stored = await _save_user(session_factory, user, clock, replan=replan, **values)
    if stored is not None:
        await message.answer(_saved_text(stored))


async def delay_input(
    message: Message,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock = utcnow,
) -> None:
    await _apply_text_setting(
        message, state, user, session_factory, clock, parse_delay_range,
        ("delay_min_s", "delay_max_s"), replan=False,
    )  # fmt: skip


async def hours_input(
    message: Message,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock = utcnow,
) -> None:
    await _apply_text_setting(
        message, state, user, session_factory, clock, parse_working_hours,
        ("work_start", "work_end"), replan=True,
    )  # fmt: skip


async def _save_time_zone(
    send: Callable[[str], Awaitable[Any]],
    user: User,
    resolved: ResolvedTimeZone,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    stored = await _save_user(session_factory, user, clock, replan=True, timezone=resolved.zone)
    if stored is not None:
        confirmation = confirmation_text(resolved, clock(), stored.language)
        await send(confirmation + "\n\n" + settings_text(stored))


async def timezone_input(
    message: Message,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await state.clear()
        return
    lang = user.language
    now = clock()
    try:
        resolved = resolve_time_zone(message.text or "", now)
    except AmbiguousTimeZone as exc:
        await message.answer(
            t("settings.tz_choose", lang, problem=exc.msg),
            reply_markup=time_zone_keyboard("settz", exc.choices, now, lang),
        )
        return
    except UnknownTimeZone as exc:
        await message.answer(t("common.try_again", lang, problem=exc.msg))
        return
    await state.clear()
    await _save_time_zone(message.answer, user, resolved, session_factory, clock)


async def time_zone_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """A quick or choice button from the time zone question (``settz:<zone>``)."""
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        zone = zone_from_callback(parse_callback(callback.data).entity_id)
    except InvalidCallbackData:
        zone = None
    if zone is None:
        await callback.answer(t("alert.unknown_tz", lang))
        return
    await callback.answer(t("alert.saved", lang))
    if await state.get_state() == SettingsStates.timezone.state:
        await state.clear()
    chat_id = user.telegram_control_chat_id

    async def send(text: str) -> None:
        await bot.send_message(chat_id, text)

    await _save_time_zone(send, user, ResolvedTimeZone(zone), session_factory, clock)


async def gender_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``setgender:<masculine|feminine>[:settings]`` (issue #46): store the user's choice. From
    /settings: render /settings again in place, like the other switches. From the one-off
    question: tick it and confirm. Users still in setup (stale buttons) are refused."""
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        parsed = parse_callback(callback.data)
    except InvalidCallbackData:
        await callback.answer()
        return
    chosen = next((g for g in GRAMMATICAL_GENDERS if g == parsed.entity_id), None)
    if chosen is None:
        await callback.answer()
        return
    stored = await grammatical_gender.choose(session_factory, user.id, chosen, clock())
    if stored is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    if parsed.extra == FROM_SETTINGS:
        await callback.answer()
        await _rerender_settings(callback, bot, stored, session_factory)
        return
    await callback.answer(t("alert.saved", stored.language))
    chat_id = stored.telegram_control_chat_id
    if callback.message is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=callback.message.message_id,
                reply_markup=gender_keyboard(chosen, stored.language),
            )
        except Exception as exc:  # unchanged markup, message too old, ...
            log.info("gender_keyboard_edit_failed", error=type(exc).__name__)
    await bot.send_message(chat_id, t(f"gender.saved.{chosen}", stored.language))


async def language_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``setlang:<en|ru>`` (issue #8): store ``users.language``; it applies at once, also to the
    chat's ``/`` menu."""

    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        chosen = parse_callback(callback.data).entity_id
    except InvalidCallbackData:
        chosen = ""
    if chosen not in LANGUAGES:
        await callback.answer()
        return
    new_lang = normalize_lang(chosen)
    stored = await _save_user(session_factory, user, clock, replan=False, language=new_lang)
    if stored is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    await callback.answer(t("alert.saved", new_lang))
    chat_id = stored.telegram_control_chat_id
    if callback.message is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=callback.message.message_id,
                reply_markup=language_keyboard(new_lang),
            )
        except Exception as exc:  # unchanged markup, message too old, ...
            log.info("language_keyboard_edit_failed", error=type(exc).__name__)
    await set_chat_commands(bot, chat_id, new_lang)
    await bot.send_message(chat_id, _saved_text(stored), reply_markup=_settings_keyboard_of(stored))


async def toggle_day_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        item = parse_callback(callback.data).entity_id
    except InvalidCallbackData:
        await callback.answer()
        return
    current = await _fresh_user(session_factory, user)
    if current is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    if item == "done":
        await callback.answer(t("alert.saved", lang))
        await bot.send_message(current.telegram_control_chat_id, settings_text(current))
        return
    try:
        days = toggle_day(list(current.working_days), int(item))
    except InvalidSetting as exc:
        await callback.answer(exc.text(lang))
        return
    except ValueError:
        await callback.answer(t("err.unknown_day", lang))
        return
    await _save_user(session_factory, current, clock, replan=True, working_days=days)
    await callback.answer()
    if callback.message is not None:
        await bot.edit_message_reply_markup(
            chat_id=current.telegram_control_chat_id,
            message_id=callback.message.message_id,
            reply_markup=days_keyboard(days, lang),
        )


async def cancel_input(message: Message, state: FSMContext, lang: Lang) -> None:
    await state.clear()
    await message.answer(t("settings.cancelled", lang))


# --- /chats: per-chat delay override ------------------------------------------------------------


async def chats_command(
    message: Message,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    await send_chats_list(bot, message.chat.id, user, session_factory, lang)


async def send_chats_list(
    bot: Bot,
    chat_id: int,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    lang: str,
    *,
    page: int = 0,
    edit_message_id: int | None = None,
) -> None:
    """The ``/chats`` message: one row per work chat (enabled ⚙️ first, disabled ⏸, excluded
    🚫; ``· 💼`` = picked up by work account mode), 8 per page (``chatspage:<n>``), then 🚫 Exclude
    chats… while the mode is on (specs/008) and ➕ Add work chats for a user who finished setup
    (specs/007), also on an empty list. While the mode is off the chats it picked up are hidden
    and counted in one line. ``edit_message_id``: edit that message instead (paging)."""
    async with session_factory() as session:
        current = await users_repo.get_user(session, user.id) or user
        mode = current.work_account
        chats = await chats_repo.list_work_chats(session, user.id, include_auto=mode)
        excluded = await chats_repo.count_excluded(session, user.id) if mode else 0
        paused = 0 if mode else await chats_repo.count_paused(session, user.id)
    chats.sort(key=lambda c: (not c.enabled, c.title))
    pages = max(1, -(-len(chats) // chat_picker.PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    shown = chats[page * chat_picker.PAGE_SIZE : (page + 1) * chat_picker.PAGE_SIZE]
    untitled = t("common.chat", lang)
    rows = [
        [button(_chat_row_label(chat, mode, untitled), format_callback("chatcfg", chat.id))]
        for chat in shown
    ]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(button("◀️", format_callback("chatspage", page - 1)))
        nav.append(button(f"{page + 1}/{pages}", format_callback("chatspage", page)))
        if page < pages - 1:
            nav.append(button("▶️", format_callback("chatspage", page + 1)))
        rows.append(nav)
    if mode:
        rows.append([button(t("btn.exclude_chats", lang), format_callback("wxopen", 0))])
    if current.state != UserState.setting_up:
        rows.append([button(t("btn.chat_add", lang), format_callback("chatadd", 0))])
    lines = [t("chats.header_work_account", lang, n=excluded)] if mode else []
    if chats:
        lines.append(t("chats.list", lang))
    elif not mode:
        lines.append(t("chats.none", lang))
    if paused:
        lines.append(t("chats.paused_footer", lang, n=paused))
    text = "\n".join(lines)
    markup = keyboard(rows) if rows else None
    if edit_message_id is not None:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup
            )
            return
        except Exception as exc:  # unchanged, too old, ...
            log.info("chats_page_edit_failed", error=type(exc).__name__)
            return
    await bot.send_message(chat_id, text, reply_markup=markup)


def _chat_row_label(chat: WorkChat, work_account: bool, untitled: str) -> str:
    if chat.enabled:
        mark = "⚙️"
    elif work_account and chats_repo.is_excluded(chat):
        mark = "🚫"
    else:
        mark = "⏸"
    suffix = " · 💼" if chat.origin == WorkChatOrigin.auto else ""
    title = chat.title or untitled
    return f"{mark} {title}"[: 60 - len(suffix)] + suffix


async def chats_page_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    """``chatspage:<n>``: another page of /chats, in place."""
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        page = int(parse_callback(callback.data).entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer()
        return
    await callback.answer()
    message_id = callback.message.message_id if callback.message is not None else None
    await send_chats_list(
        bot,
        user.telegram_control_chat_id,
        user,
        session_factory,
        lang,
        page=page,
        edit_message_id=message_id,
    )


async def _owned_chat(
    callback: CallbackQuery,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> tuple[str | None, WorkChat, User] | None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return None
    try:
        data = parse_callback(callback.data)
        chat_id = uuid.UUID(data.entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("alert.chat_not_found", lang))
        return None
    async with session_factory() as session:
        chat = await chats_repo.get_work_chat(session, user.id, chat_id)  # ownership check
        current = await users_repo.get_user(session, user.id)
    if chat is None or current is None:
        await callback.answer(t("alert.chat_not_found", lang))
        return None
    return data.extra, chat, current


async def chat_settings_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    owned = await _owned_chat(callback, user, session_factory, lang)
    if owned is None:
        return
    _, chat, current = owned
    await callback.answer()
    await bot.send_message(
        current.telegram_control_chat_id,
        chat_settings_text(chat, current),
        reply_markup=chat_settings_keyboard(
            chat,
            current.language,
            global_skip=current.skip_closing,
            work_account=current.work_account,
        ),
    )


async def chat_delay_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    owned = await _owned_chat(callback, user, session_factory, lang)
    if owned is None:
        return
    extra, chat, current = owned
    if extra == "clear":
        async with session_factory() as session:
            stored = await chats_repo.get_work_chat(session, current.id, chat.id)
            if stored is not None:
                stored.override_delay_min_s = None
                stored.override_delay_max_s = None
            await session.commit()
        await callback.answer(t("alert.global_delay", lang))
        if stored is not None:
            await bot.send_message(
                current.telegram_control_chat_id, chat_settings_text(stored, current)
            )
        return
    await state.set_state(SettingsStates.chat_delay)
    await state.set_data({"work_chat_id": str(chat.id)})
    await callback.answer()
    await bot.send_message(current.telegram_control_chat_id, t("settings.chat_delay_prompt", lang))


async def chat_delay_input(
    message: Message,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    if user is None:
        await state.clear()
        return
    lang = user.language
    try:
        low, high = parse_delay_range(message.text or "")
    except InvalidSetting as exc:
        await message.answer(t("common.try_again", lang, problem=exc.msg))
        return
    await state.clear()
    try:
        chat_id = uuid.UUID(str(data.get("work_chat_id")))
    except ValueError:
        return
    async with session_factory() as session:
        chat = await chats_repo.get_work_chat(session, user.id, chat_id)
        if chat is None:
            await message.answer(t("alert.chat_not_found", lang))
            return
        chat.override_delay_min_s, chat.override_delay_max_s = low, high
        current = await users_repo.get_user(session, user.id) or user
        await session.commit()
    log.info("chat_delay_override_set", user_id=str(user.id))
    await message.answer(
        t("settings.saved", current.language, settings=chat_settings_text(chat, current))
    )


async def _send_chat_settings(
    bot: Bot, session_factory: async_sessionmaker[AsyncSession], user: User, chat_id: uuid.UUID
) -> None:
    async with session_factory() as session:
        chat = await chats_repo.get_work_chat(session, user.id, chat_id)
        current = await users_repo.get_user(session, user.id) or user
    if chat is not None:
        await bot.send_message(
            current.telegram_control_chat_id,
            chat_settings_text(chat, current),
            reply_markup=chat_settings_keyboard(
                chat,
                current.language,
                global_skip=current.skip_closing,
                work_account=current.work_account,
            ),
        )


async def chat_language_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    owned = await _owned_chat(callback, user, session_factory, lang)
    if owned is None:
        return
    extra, chat, current = owned
    if extra == "clear":
        async with session_factory() as session:
            stored = await chats_repo.get_work_chat(session, current.id, chat.id)
            if stored is not None:
                stored.override_language = None
            await session.commit()
        await callback.answer(t("alert.chat_language_auto", lang))
        await _send_chat_settings(bot, session_factory, current, chat.id)
        return
    await state.set_state(SettingsStates.chat_language)
    await state.set_data({"work_chat_id": str(chat.id)})
    await callback.answer()
    await bot.send_message(
        current.telegram_control_chat_id, t("settings.chat_language_prompt", lang)
    )


async def chat_language_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    if user is None:
        await state.clear()
        return
    lang = user.language
    try:
        language = parse_language(message.text or "")
    except InvalidSetting as exc:
        await message.answer(t("common.try_again", lang, problem=exc.msg))
        return
    await state.clear()
    try:
        chat_id = uuid.UUID(str(data.get("work_chat_id")))
    except ValueError:
        return
    async with session_factory() as session:
        chat = await chats_repo.get_work_chat(session, user.id, chat_id)
        if chat is None:
            await message.answer(t("alert.chat_not_found", lang))
            return
        chat.override_language = language
        await session.commit()
    log.info("chat_override_set", user_id=str(user.id), field="language")
    await message.answer(t("settings.saved_short", lang))
    await _send_chat_settings(bot, session_factory, user, chat_id)


async def chat_option_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``chatformal:<id>:<1-5|clear>``, ``chatenable:<id>:<on|off>``,
    ``chatskip:<id>:<inherit|off|on>`` (004). The legacy ``chatgroup:<id>:<mode>`` (buttons on
    settings cards posted before 006) changes nothing and answers "no longer available"."""
    owned = await _owned_chat(callback, user, session_factory, lang)
    if owned is None:
        return
    extra, chat, current = owned
    action = parse_callback(callback.data).action
    if action == "chatgroup":
        await callback.answer(t("alert.option_removed", lang))
        return
    if action == "chatenable" and _work_account_screen(chat, current.work_account):
        await _exclude_or_include(callback, bot, current, chat, extra, session_factory, clock)
        return
    if action == "chatenable" and extra == "off":
        dropped_drafts = await disable_chat(session_factory, current.id, chat.id, clock())
        if dropped_drafts is None:
            await callback.answer(t("alert.chat_not_found", lang))
            return
        log.info("chat_override_set", user_id=str(current.id), field=action)
        presenter = DraftPresenter(bot, session_factory, clock=clock)
        for draft_id in dropped_drafts:
            await presenter.show_status(current.id, draft_id, CHAT_DISABLED_STATUS)
        await callback.answer(t("alert.saved_check", lang))
        await _send_chat_settings(bot, session_factory, current, chat.id)
        return
    async with session_factory() as session:
        stored = await chats_repo.get_work_chat(session, current.id, chat.id)
        if stored is None:
            await callback.answer(t("alert.chat_not_found", lang))
            return
        if action == "chatformal" and extra == "clear":
            stored.override_formality = None
        elif action == "chatformal" and extra in {"1", "2", "3", "4", "5"}:
            stored.override_formality = int(extra)
        elif action == "chatenable" and extra == "on":
            stored.enabled = True
        elif action == "chatskip" and extra in CHAT_SKIP_VALUES:
            stored.skip_closing = CHAT_SKIP_VALUES[extra]
        else:
            await callback.answer()
            return
        await session.commit()
    log.info("chat_override_set", user_id=str(current.id), field=action)
    await callback.answer(t("alert.saved_check", lang))
    await _send_chat_settings(bot, session_factory, current, chat.id)


async def _exclude_or_include(
    callback: CallbackQuery,
    bot: Bot,
    user: User,
    chat: WorkChat,
    extra: str | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """specs/008: ``chatenable:<id>:off`` = 🚫 Exclude, ``:on`` = ↩️ Include again."""
    lang = user.language
    now = clock()
    if extra == "off":
        dropped = await work_account.exclude_chat(
            session_factory, user.id, work_chat_id=chat.id, now=now
        )
        if dropped is None:
            await callback.answer(t("alert.chat_not_found", lang))
            return
        status = (
            CHAT_EXCLUDED_STATUS if chat.origin == WorkChatOrigin.auto else CHAT_DISABLED_STATUS
        )
        presenter = DraftPresenter(bot, session_factory, clock=clock)
        for draft_id in dropped:
            await presenter.show_status(user.id, draft_id, status)
    elif extra == "on":
        if not await work_account.include_chat(session_factory, user.id, chat.id, now=now):
            await callback.answer(t("alert.chat_not_found", lang))
            return
    else:
        await callback.answer()
        return
    log.info("chat_override_set", user_id=str(user.id), field="chatenable")
    await callback.answer(t("alert.saved_check", lang))
    await _send_chat_settings(bot, session_factory, user, chat.id)


async def disable_chat(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    now: datetime,
) -> list[uuid.UUID] | None:
    """Disable a work chat (``chat_disabled``) and return the ids of the pending drafts it
    dropped (their cards get a status line); None: no such chat. The protocol (lock order chat
    -> pending drafts -> turns, retry when a draft appears) is
    ``scribe.chats.work_account.deactivate_chat`` (specs/008), shared with exclusion and the
    work account mode switch."""
    result = await work_account.deactivate_chat(
        session_factory, user_id, work_chat_id, now, reason=DroppedReason.chat_disabled
    )
    return None if result is None else result.dropped_draft_ids


# --- /waiting -----------------------------------------------------------------------------------


async def waiting_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    now = clock()
    items: list[tuple[str, uuid.UUID]] = []
    async with session_factory() as session:
        current = await users_repo.get_user(session, user.id) or user
        turns = await turns_repo.list_turns(session, user.id, state=TurnState.waiting)
        for turn in turns[:WAITING_LIST_MAX]:
            contact = await chats_repo.get_contact(session, user.id, turn.contact_id)
            messages = await turn_messages(session, turn)
            preview = messages[0].text if messages else ""
            text = waiting_item_text(
                contact.display_name if contact is not None else "",
                turn.arrived_at,
                turn.due_at,
                preview,
                current.timezone,
                now,
                lang,
            )
            items.append((text, turn.id))
    if not items:
        await message.answer(t("waiting.nothing", lang))
        return
    if current.state == UserState.paused:
        await message.answer(t("waiting.paused", lang))
    for text, turn_id in items:
        read_now = button(t("btn.read_now", lang), format_callback("readnow", turn_id))
        await message.answer(text, reply_markup=keyboard([[read_now]]))
    if len(turns) > WAITING_LIST_MAX:
        await message.answer(t("waiting.more", lang, n=len(turns) - WAITING_LIST_MAX))


# --- /pause, /resume ----------------------------------------------------------------------------


async def pause_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    async with session_factory() as session:
        current = await users_repo.get_user(session, user.id)
        if current is None or current.state == UserState.setting_up:
            await message.answer(t("settings.finish_setup", lang))
            return
        if current.state == UserState.paused:
            await message.answer(t("settings.already_paused", lang))
            return
        if current.state == UserState.out_of_office and current.ooo_message is None:
            # Remembered without a schema change: ooo_message is non-NULL exactly while the
            # user is out of office (data-model.md), so /resume can restore it.
            current.ooo_message = ""
        current.state = UserState.paused
        await session.commit()
    log.info("user_paused", user_id=str(user.id))
    await message.answer(t("settings.paused", lang))


async def resume_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    async with session_factory() as session:
        current = await users_repo.get_user(session, user.id)
        if current is None or current.state == UserState.setting_up:
            await message.answer(t("settings.finish_setup", lang))
            return
        if current.state != UserState.paused:
            await message.answer(t("settings.not_paused", lang))
            return
        # Back to the state /pause interrupted: out of office if it was (ooo_message kept).
        current.state = (
            UserState.out_of_office if current.ooo_message is not None else UserState.active
        )
        await replan_waiting_turns(session, current, clock())
        waiting = len(await turns_repo.list_turns(session, user.id, state=TurnState.waiting))
        await session.commit()
    log.info("user_resumed", user_id=str(user.id), waiting=waiting)
    await message.answer(t("settings.resumed", lang, count=waiting))


# --- /ooo ---------------------------------------------------------------------------------------


async def ooo_command(
    message: Message,
    command: CommandObject,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    text = " ".join((command.args or "").split())[:OOO_MAX_CHARS]
    async with session_factory() as session:
        current = await users_repo.get_user(session, user.id)
        if current is None or current.state == UserState.setting_up:
            await message.answer(t("settings.finish_setup", lang))
            return
        paused = current.state == UserState.paused
        if text:
            # Invariant (data-model.md): ooo_message is non-NULL exactly while out of office,
            # also while paused from it, so /resume restores out of office.
            current.ooo_message = text
            if not paused:
                current.state = UserState.out_of_office
            reply = t("ooo.on", lang, text=text)
            if paused:
                reply += t("ooo.on_paused", lang)
        elif current.ooo_message is not None or current.state == UserState.out_of_office:
            current.ooo_message = None
            if current.state == UserState.out_of_office:
                current.state = UserState.active
            reply = t("ooo.off", lang) + (t("ooo.off_paused", lang) if paused else "")
        else:
            await message.answer(t("ooo.usage", lang))
            return
        await session.commit()
    log.info("out_of_office_changed", user_id=str(user.id), on=bool(text))
    await message.answer(reply)


# --- /history -----------------------------------------------------------------------------------


def _history_label(record: ActivityRecord, lang: str) -> str:
    meta = record.meta_json or {}
    if record.kind == ActivityKind.profile_refreshed and meta.get("noop") is True:
        return t("history.profile_checked", lang)
    if record.kind == ActivityKind.profile_undo and meta.get("removed") is True:
        return t("history.profile_removed", lang)
    if record.kind == ActivityKind.profile_undo and meta.get("superseded") is True:
        return t("history.profiles_rebuilt", lang)
    if record.kind == ActivityKind.skipped and isinstance(meta.get("category"), str):
        return t("history.skipped", lang, reason=skip_reason(meta["category"], lang))
    if record.kind == ActivityKind.chats_added and meta.get("origin") == WorkChatOrigin.auto:
        return t("history.chat_picked_up", lang)  # specs/008
    key = f"history.kind.{record.kind.value}"
    return t(key, lang) if has_text(key) else str(record.kind)


def history_line(record: ActivityRecord, contact: str | None, tz: str, lang: str) -> str:
    local = record.at.astimezone(ZoneInfo(tz))
    when = t(
        "history.when",
        lang,
        weekday=weekday_abbr(local.weekday(), lang),
        date=format_day_month(local.date(), lang),
        time=f"{local:%H:%M}",
    )
    label = _history_label(record, lang)
    return f"{when} · {label}" + (f" · {contact}" if contact else "")


async def _recent_activity(
    session: AsyncSession, user_id: uuid.UUID, limit: int
) -> list[tuple[ActivityRecord, str | None]]:
    """The latest records with the contact of their turn (directly or through the draft), in
    one query; every joined table is scoped to the user."""
    turn_id = func.coalesce(ActivityRecord.conversation_turn_id, Draft.conversation_turn_id)
    result = await session.execute(
        select(ActivityRecord, Contact.display_name)
        .outerjoin(Draft, and_(Draft.id == ActivityRecord.draft_id, Draft.user_id == user_id))
        .outerjoin(
            ConversationTurn,
            and_(ConversationTurn.id == turn_id, ConversationTurn.user_id == user_id),
        )
        .outerjoin(
            Contact,
            and_(Contact.id == ConversationTurn.contact_id, Contact.user_id == user_id),
        )
        .where(ActivityRecord.user_id == user_id)
        .order_by(ActivityRecord.at.desc())
        .limit(limit)
    )
    return [(record, name or None) for record, name in result.all()]


HISTORY_TITLES = 3  # specs/007: "➕ Work chats added · A, B, C, +2 more"


def added_chats_text(titles: list[str], lang: str) -> str | None:
    """The chats of a ``chats_added`` record: up to 3 titles, then "+N more"."""
    if not titles:
        return None
    shown = titles[:HISTORY_TITLES]
    rest = len(titles) - len(shown)
    return ", ".join(shown + ([t("history.more", lang, n=rest)] if rest else []))


async def _added_chat_titles(
    session: AsyncSession, user_id: uuid.UUID, records: list[ActivityRecord], lang: str
) -> dict[uuid.UUID, str | None]:
    """``chats_added`` (and 008's ``chat_excluded`` / ``chat_included``) record id -> its chats'
    current titles (one query, scoped to the user; chats deleted since are left out). The record
    itself stores ids only."""
    wanted: dict[uuid.UUID, list[uuid.UUID]] = {}
    for record in records:
        meta = record.meta_json or {}
        if record.kind in (ActivityKind.chat_excluded, ActivityKind.chat_included):
            raw_ids = [meta.get("work_chat_id")]
        elif record.kind == ActivityKind.chats_added:
            raw_ids = meta.get("work_chat_ids", [])
        else:
            continue
        ids = []
        for raw in raw_ids:
            try:
                ids.append(uuid.UUID(str(raw)))
            except ValueError:
                continue
        wanted[record.id] = ids
    all_ids = {chat_id for ids in wanted.values() for chat_id in ids}
    if not all_ids:
        return {record_id: None for record_id in wanted}
    result = await session.execute(
        select(WorkChat.id, WorkChat.title).where(
            WorkChat.user_id == user_id, WorkChat.id.in_(all_ids)
        )
    )
    untitled = t("common.chat", lang)
    titles = {chat_id: title or untitled for chat_id, title in result.all()}
    return {
        record_id: added_chats_text([titles[i] for i in ids if i in titles], lang)
        for record_id, ids in wanted.items()
    }


async def history_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> None:
    """FR-027: what happened and when (who approved: you, with the time), never content."""
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    async with session_factory() as session:
        current = await users_repo.get_user(session, user.id) or user
        rows = await _recent_activity(session, user.id, HISTORY_LIMIT)
        added = await _added_chat_titles(session, user.id, [record for record, _ in rows], lang)
    lines = [
        history_line(record, added.get(record.id, contact), current.timezone, lang)
        for record, contact in rows
    ]
    if not lines:
        await message.answer(t("history.empty", lang))
        return
    header = t("history.header", lang, n=len(lines))
    await message.answer("\n".join([header, *lines]))


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


_TEXT_INPUT = F.text & ~F.text.startswith("/")


def create_router() -> Router:
    router = Router(name="settings")
    # Outside other flows only: during setup / Telegram login those flows answer commands.
    idle = StateFilter(
        None,
        SettingsStates,
        ReviewStates.editing,
        tasks_command.TasksStates,
        jira_connect.JiraStates,
        commitments_command.CommitmentStates,
        profile_command.ProfileStates,
        account_command.DeleteStates,
        ApplyStates,  # specs/005 US5: /my_data and /delete_my_data mid-application
        add_chats.AddChatsStates,  # specs/007: commands close the ➕ picker and answer
        exclusions.ChatsStates,  # specs/008: and the exclusion picker
    )
    router.message.register(settings_command, Command("settings"), idle)
    router.message.register(chats_command, Command("chats"), idle)
    router.message.register(waiting_command, Command("waiting"), idle)
    router.message.register(pause_command, Command("pause"), idle)
    router.message.register(resume_command, Command("resume"), idle)
    router.message.register(ooo_command, Command("ooo"), idle)
    router.message.register(history_command, Command("history"), idle)
    router.message.register(cancel_input, Command("cancel"), StateFilter(SettingsStates))
    router.message.register(delay_input, StateFilter(SettingsStates.delay), _TEXT_INPUT)
    router.message.register(hours_input, StateFilter(SettingsStates.hours), _TEXT_INPUT)
    router.message.register(timezone_input, StateFilter(SettingsStates.timezone), _TEXT_INPUT)
    router.message.register(chat_delay_input, StateFilter(SettingsStates.chat_delay), _TEXT_INPUT)
    router.message.register(
        chat_language_input, StateFilter(SettingsStates.chat_language), _TEXT_INPUT
    )
    router.callback_query.register(settings_menu, _action("settings"))
    router.callback_query.register(toggle_day_callback, _action("setday"))
    router.callback_query.register(time_zone_callback, _action("settz"), idle)
    router.callback_query.register(language_callback, _action("setlang"), idle)
    # Issue #46: also the one-off question, which may arrive while another flow is open.
    router.callback_query.register(gender_callback, _action(GENDER_ACTION))
    router.callback_query.register(chat_settings_callback, _action("chatcfg"))
    router.callback_query.register(chat_delay_callback, _action("chatdelay"))
    router.callback_query.register(chat_language_callback, _action("chatlang"), idle)
    router.callback_query.register(chat_option_callback, _action("chatformal"), idle)
    router.callback_query.register(chat_option_callback, _action("chatgroup"), idle)
    router.callback_query.register(chat_option_callback, _action("chatenable"), idle)
    router.callback_query.register(chat_option_callback, _action("chatskip"), idle)
    router.callback_query.register(work_account_callback, _action("wacct"), idle)  # specs/008
    router.callback_query.register(chats_page_callback, _action("chatspage"), idle)
    # T072: a callback naming another user's row stops here (applies to the included routers).
    router.callback_query.middleware(OwnershipMiddleware())
    # Any other command ends an abandoned Trello key/token step (drops the key), DELETE
    # confirmation or rules input.
    router.message.outer_middleware(tasks_command.AbandonedConnectMiddleware())
    router.message.outer_middleware(account_command.AbandonedDeleteMiddleware())
    router.message.outer_middleware(profile_command.AbandonedRulesMiddleware())  # ✏️ Edit rules
    router.include_router(add_chats.create_router(idle))  # specs/007: ➕ Add work chats
    router.include_router(exclusions.create_router(idle))  # specs/008: 🚫 Exclude chats…
    router.include_router(tasks_command.create_router(idle))  # US3: /tasks
    router.include_router(jira_connect.create_router(idle))  # specs/003: Jira in /tasks
    router.include_router(commitments_command.create_router(idle))  # US3: /commitments
    router.include_router(profile_command.create_router(idle))  # US4: /profile
    router.include_router(account_command.create_router(idle))  # US4: /my_data, /delete_my_data
    router.include_router(skipped.create_router(idle))  # 004: /skipped
    return router
