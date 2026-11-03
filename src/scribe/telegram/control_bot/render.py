"""Control-chat rendering helpers: callback data and keyboards (contracts/control-chat.md).

Callback data format: ``<action>:<entity_id>[:<extra>]``, at most 64 bytes (Telegram limit).
Cards are plain text (no parse mode), so message content can never inject markup.
Every user-facing text comes from the ``scribe.i18n`` catalogs, in the user's language.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from scribe.context.task_dates import MismatchPart, mismatch_parts
from scribe.i18n import (
    LANGUAGES,
    format_date_time,
    format_day_month,
    format_weekday_date,
    t,
    weekday_abbr,
)

CALLBACK_DATA_MAX_BYTES = 64
_ACTION = re.compile(r"^[a-z][a-z0-9_]*$")

# Actions defined by contracts/control-chat.md (for reference; the parser only checks format).
KNOWN_ACTIONS = frozenset(
    {
        "approve",
        "edit",
        "regen",
        "reject",
        "sources",
        "readnow",
        "pin",
        "unpin",
        "removeprofile",
        "rule_accept",
        "rule_dismiss",
        "undo_refresh",
        "pickchat",
        "pickpage",
        "pickdone",
        "tglogin",
        "tgreconnect",
        "tgdisconnect",
        "draftany",
        "skipcfg",
        "skipchat",
        "skippage",
        "skipchats",
        "chatskip",
    }
)


class InvalidCallbackData(ValueError):
    pass


@dataclass(frozen=True)
class CallbackData:
    action: str
    entity_id: str
    extra: str | None = None

    def pack(self) -> str:
        return format_callback(self.action, self.entity_id, self.extra)


def format_callback(action: str, entity_id: object, extra: object | None = None) -> str:
    entity = str(entity_id)
    parts = [action, entity] if extra is None else [action, entity, str(extra)]
    if not _ACTION.match(action):
        raise InvalidCallbackData(f"invalid action {action!r}")
    if not entity or any(":" in part for part in parts[1:]) or (extra is not None and not parts[2]):
        raise InvalidCallbackData("callback parts must be non-empty and contain no ':'")
    data = ":".join(parts)
    if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
        raise InvalidCallbackData(f"callback data exceeds {CALLBACK_DATA_MAX_BYTES} bytes")
    return data


def parse_callback(data: str | None) -> CallbackData:
    if not data:
        raise InvalidCallbackData("empty callback data")
    if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
        raise InvalidCallbackData("callback data too long")
    parts = data.split(":")
    if len(parts) not in (2, 3) or not all(parts):
        raise InvalidCallbackData("expected <action>:<entity_id>[:<extra>]")
    if not _ACTION.match(parts[0]):
        raise InvalidCallbackData(f"invalid action {parts[0]!r}")
    return CallbackData(parts[0], parts[1], parts[2] if len(parts) == 3 else None)


def button(text: str, callback: CallbackData | str) -> InlineKeyboardButton:
    data = callback.pack() if isinstance(callback, CallbackData) else callback
    parse_callback(data)  # never emit malformed callback data
    return InlineKeyboardButton(text=text, callback_data=data)


def keyboard(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


# --- texts (scribe.i18n catalogs) --------------------------------------------------------------

COMMAND_NAMES: tuple[str, ...] = (
    "start",
    "chats",
    "settings",
    "profile",
    "commitments",
    "waiting",
    "skipped",
    "pause",
    "resume",
    "ooo",
    "tasks",
    "history",
    "my_data",
    "telegram",
    "delete_my_data",
    "help",
)


def commands(lang: str) -> list[tuple[str, str]]:
    """``(command, description)`` for the ``/`` menu and /help, in ``lang``."""
    return [(name, t(f"cmd.{name}", lang)) for name in COMMAND_NAMES]


def help_text(lang: str) -> str:
    lines = [t("help.intro", lang), ""]
    lines += [f"/{name} — {description}" for name, description in commands(lang)]
    return "\n".join(lines)


# --- draft card (contracts/control-chat.md "Draft card") ---------------------------------------

TELEGRAM_TEXT_MAX = 4096  # UTF-16 code units, as Telegram counts them (:func:`utf16_len`)


def utf16_len(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units (an emoji outside the BMP is two)."""
    return len(text.encode("utf-16-le")) // 2


INCOMING_MAX_CHARS = 600
SOURCES_ON_CARD = 3

TONES = frozenset({"none", "more_formal", "urgent", "emotional"})
CHAT_KINDS = frozenset({"private", "group", "channel_comments"})
WARNINGS = frozenset(
    {
        "tasks_unavailable",
        "weak_style_profile",
        "history_truncated",
        "filter_unavailable",
        "unsupported_sources",
        "tasks_partial",
        "out_of_office",
        "drafted_by_habit",
        "drafted_on_request",
        "earlier_search_unavailable",  # specs/004 research S8
        "earlier_search_no_match",
        "earlier_search_off",
        "suspected_injection",
        "action_claim",
    }
)
SENSITIVE_CATEGORIES = frozenset({"credentials", "payment", "personal_data", "legal", "hr"})


def _label(prefix: str, code: str, known: frozenset[str], lang: str) -> str:
    """The catalog label of a known code; an unknown code is shown as it is."""
    return t(f"{prefix}.{code}", lang) if code in known else code


def numbered(questions: Sequence[str]) -> str:
    return "\n".join(f"{n}. {question}" for n, question in enumerate(questions, 1))


@dataclass(frozen=True)
class DraftCardView:
    contact_title: str
    chat_kind: str
    read_at: str | None  # local "HH:MM"
    incoming: list[str]
    profile_label: str  # already in the user's language (or a contact's name)
    tone_shift: str
    draft_text: str
    expires: str  # local and already in the user's language, e.g. "10:05 tomorrow"
    commitment: str | None = None
    conflicts: list[str] = field(default_factory=list)
    sensitive: bool = False
    sensitive_categories: list[str] = field(default_factory=list)
    needs_info: list[str] = field(default_factory=list)  # questions of the fillable placeholders
    needs_info_flag: bool = False
    sources: list[str] = field(default_factory=list)  # short labels
    warnings: list[str] = field(default_factory=list)  # warning codes
    status: str | None = None  # final line, already rendered, e.g. "✅ Sent 10:05"
    lang: str = "en"  # the user's UI language (users.language)
    # 004 FR-011: a later message from the contact (contact_title) says it may be resolved.
    possibly_unnecessary: bool = False


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _warning(code: str, lang: str) -> str:
    if code in WARNINGS:
        return t(f"card.warning.{code}", lang)
    mismatch = mismatch_parts(code)
    if mismatch is not None:
        return _mismatch(*mismatch, lang)
    return t("card.warning.other", lang, code=code)


def _mismatch(key: str | None, parts: list[MismatchPart], lang: str) -> str:
    """``📅 Dates differ: PAY-142: Fri 23 Oct (your Jira comment, 21 Oct) · Sun 25 Oct (due
    date)`` (specs/003 J12)."""
    shown = []
    for part in parts:
        where = t(f"card.date_source.{part.kind}", lang)
        if part.at is not None:
            when = format_day_month(part.at, lang)
            where = t("card.date_source_at", lang, source=where, day=when)
        day = format_weekday_date(part.on, lang)
        shown.append(t("card.date_part", lang, day=day, source=where))
    return t(
        "card.dates_differ",
        lang,
        issue=f"{key}: " if key else "",
        dates=" · ".join(shown),
    )


def render_draft_card(view: DraftCardView) -> str:
    lang = view.lang
    header = [
        view.contact_title or t("card.unknown_contact", lang),
        _label("card.kind", view.chat_kind, CHAT_KINDS, lang),
    ]
    if view.read_at:
        header.append(t("card.read", lang, time=view.read_at))
    quoted = _truncate("\n".join(view.incoming), INCOMING_MAX_CHARS)
    tone = _label("card.tone", view.tone_shift, TONES, lang)

    flags: list[str] = []
    if view.commitment:
        flags.append(t("card.commits", lang, what=view.commitment))
    flags += [t("card.conflict", lang, what=c) for c in view.conflicts]
    if view.sensitive:
        cats = ", ".join(
            _label("card.sensitive", c, SENSITIVE_CATEGORIES, lang)
            for c in view.sensitive_categories
        )
        flags.append(
            t("card.sensitive_categories", lang, categories=cats)
            if cats
            else t("card.sensitive", lang)
        )

    tail: list[str] = []
    if flags:
        tail.append(t("card.flags", lang, flags="  ·  ".join(flags)))
    if view.needs_info:
        # Issue #2: the user can reply with just the values, in this order.
        tail.append(t("card.needs_info", lang, questions=numbered(view.needs_info)))
        if not view.status:
            hint = "card.fill_hint_one" if len(view.needs_info) == 1 else "card.fill_hint_many"
            tail.append(t(hint, lang))
    elif view.needs_info_flag:
        tail.append(t("card.needs_info_see_draft", lang))
    if view.sources:
        shown = view.sources[:SOURCES_ON_CARD]
        more = len(view.sources) - len(shown)
        if more:
            shown = [*shown, t("card.sources_more", lang, n=more)]
        tail.append(t("card.sources", lang, sources=" · ".join(shown)))
    tail += [_warning(code, lang) for code in view.warnings]
    tail.append(t("card.expires", lang, when=view.expires))

    def assemble(draft_text: str) -> str:
        parts = [
            "   ·   ".join(["💬 " + header[0], *header[1:]]),
            "› " + quoted.replace("\n", "\n› "),
            "",
            t("card.draft", lang, profile=view.profile_label, tone=tone),
            draft_text,
            "",
            *tail,
        ]
        if view.status:
            parts += ["", view.status]
        elif view.possibly_unnecessary:  # a banner for a pending card only (004 FR-011)
            sender = view.contact_title or t("card.unknown_contact", lang)
            parts = [t("card.possibly_unnecessary", lang, sender=sender), "", *parts]
        return "\n".join(parts)

    card = assemble(view.draft_text)
    if len(card) > TELEGRAM_TEXT_MAX:
        overflow = len(card) - TELEGRAM_TEXT_MAX + 40
        cut = view.draft_text[: max(0, len(view.draft_text) - overflow)]
        card = assemble(cut + t("card.truncated", lang))
    return card


def draft_keyboard(draft_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button(t("btn.send", lang), format_callback("approve", draft_id)),
                button(t("btn.edit", lang), format_callback("edit", draft_id)),
            ],
            [
                button(t("btn.regen", lang), format_callback("regen", draft_id)),
                button(t("btn.reject", lang), format_callback("reject", draft_id)),
                button(t("btn.sources", lang), format_callback("sources", draft_id)),
            ],
        ]
    )


def confirm_send_keyboard(action: str, draft_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    """Second step for flagged drafts: ``approve|edit:<id>:confirm``."""
    send_anyway = t("btn.send_anyway", lang)
    return keyboard([[button(send_anyway, format_callback(action, draft_id, "confirm"))]])


# --- Telegram connection (002 contracts/control-chat.md) ---------------------------------------


def account_label(
    display_name: str | None, username: str | None, masked_phone: str | None, lang: str
) -> str:
    """``Name (@username)``, else ``Name (<masked phone>)``, else the name alone.

    ``masked_phone`` must already be masked (``scribe.telegram.login.mask_phone``).
    """
    name = (display_name or "").strip()
    handle = (username or "").strip().lstrip("@")
    detail = f"@{handle}" if handle else (masked_phone or "").strip()
    if name and detail:
        return f"{name} ({detail})"
    return name or detail or t("connect.unknown_account", lang)


def qr_caption(
    code_no: int, total: int, expires_at: datetime, tz: str | ZoneInfo, lang: str
) -> str:
    """Caption of the QR photo; ``expires_at`` is aware, ``tz`` an IANA name or ``ZoneInfo``."""
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("qr_caption needs a timezone-aware datetime")
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    until = expires_at.astimezone(zone).strftime("%H:%M:%S")
    return t("connect.qr_caption", lang, code_no=code_no, total=total, until=until)


def _use_phone_button(lang: str) -> InlineKeyboardButton:
    return button(t("btn.use_phone_instead", lang), format_callback("tglogin", 0, "phone"))


def use_phone_keyboard(lang: str) -> InlineKeyboardMarkup:
    return keyboard([[_use_phone_button(lang)]])


def method_choice_keyboard(lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button(t("btn.qr_recommended", lang), format_callback("tglogin", 0, "qr"))],
            [button(t("btn.phone_and_code", lang), format_callback("tglogin", 0, "phone"))],
        ]
    )


def qr_keyboard(lang: str, same_device_url: str | None = None) -> InlineKeyboardMarkup:
    """Buttons under the QR photo; the URL button only when ``tg_qr_same_device_link`` is on."""
    rows: list[list[InlineKeyboardButton]] = []
    if same_device_url:
        approve = t("btn.approve_same_device", lang)
        rows.append([InlineKeyboardButton(text=approve, url=same_device_url)])
    rows.append([_use_phone_button(lang)])
    rows.append([button(t("btn.cancel", lang), format_callback("tglogin", 0, "cancel"))])
    return keyboard(rows)


def qr_expired_keyboard(lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button(t("btn.new_code", lang), format_callback("tglogin", 0, "qr"))],
            [_use_phone_button(lang)],
        ]
    )


def use_qr_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Extra button on the phone and code prompts."""
    return keyboard([[button(t("btn.use_qr_instead", lang), format_callback("tglogin", 0, "qr"))]])


def not_my_account_keyboard(lang: str) -> InlineKeyboardMarkup:
    notme = format_callback("tglogin", 0, "notme")
    return keyboard([[button(t("btn.not_my_account", lang), notme)]])


def reconnect_keyboard(lang: str) -> InlineKeyboardMarkup:
    return keyboard([[button(t("btn.reconnect", lang), format_callback("tgreconnect", 0))]])


# --- /telegram status card (002 contracts/control-chat.md "`/telegram` status card") ----------

SESSION_STATUSES = frozenset({"active", "revoked", "error"})
LOGIN_METHODS = frozenset({"qr", "phone_code"})


def telegram_status_card(
    *,
    account: str,
    status: str,
    logged_out_elsewhere: bool,
    login_method: str,
    connected_at: datetime,
    tz: str | ZoneInfo,
    device_model: str,
    lang: str,
) -> str:
    """The ``/telegram`` card; ``connected_at`` is aware and shown in the user's time zone."""
    if connected_at.tzinfo is None or connected_at.utcoffset() is None:
        raise ValueError("telegram_status_card needs a timezone-aware datetime")
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = connected_at.astimezone(zone)
    status_label = _label("telegram.status", status, SESSION_STATUSES, lang)
    if status == "revoked" and logged_out_elsewhere:
        status_label = t("telegram.logged_out_elsewhere", lang, status=status_label)
    method = _label("telegram.method", login_method, LOGIN_METHODS, lang)
    shown_account = account or t("connect.unknown_account", lang)
    connected = format_date_time(local, lang)
    return "\n".join(
        [
            t("telegram.card.title", lang),
            t("telegram.card.account", lang, account=shown_account),
            t("telegram.card.status", lang, status=status_label),
            t("telegram.card.connected", lang, when=connected, method=method),
            "",
            t("telegram.card.terminate", lang, device=device_model),
        ]
    )


def disconnect_keyboard(lang: str) -> InlineKeyboardMarkup:
    return keyboard([[button(t("btn.disconnect", lang), format_callback("tgdisconnect", 0))]])


def disconnect_confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    yes = t("btn.disconnect_yes", lang)
    return keyboard([[button(yes, format_callback("tgdisconnect", 1))]])


# --- skipped messages (004 contracts/control-chat.md "Skipped list", "Per-chat skip settings") --

SKIPPED_PAGE_SIZE = 10
SKIPPED_PREVIEW_CHARS = 80
SKIP_CATEGORIES = frozenset({"thanks", "farewell", "acknowledgment", "forwarded", "budget"})
# Skipped whatever the chat's skip setting: no per-chat switch on their rows.
SETTING_INDEPENDENT_SKIPS = frozenset({"forwarded", "budget"})


def on_off(value: bool, lang: str) -> str:
    return t("skipped.on" if value else "skipped.off", lang)


def skip_reason(code: str, lang: str) -> str:
    return _label("skipped.reason", code, SKIP_CATEGORIES, lang)


def skipped_time(moment: datetime, tz: str | ZoneInfo, lang: str) -> str:
    """``Tue 14:02`` / ``Вт 14:02`` in the user's time zone."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = moment.astimezone(zone)
    return f"{weekday_abbr(local.weekday(), lang)} {local:%H:%M}"


@dataclass(frozen=True)
class SkippedItemView:
    turn_id: uuid.UUID
    work_chat_id: uuid.UUID
    sender: str
    chat_title: str
    local_time: str  # e.g. "Tue 14:02", in the user's time zone and language
    preview: str  # decrypted turn text: rendered for the owner only, never logged
    reason: str  # skip category code
    available: bool  # "Draft anyway" still possible (FR-013)
    chat_override: bool | None  # work_chats.skip_closing
    drafting: bool = False  # "Draft anyway" was just tapped


@dataclass(frozen=True)
class ChatSkipView:
    work_chat_id: uuid.UUID
    title: str
    value: bool | None  # work_chats.skip_closing; None = global


def render_skipped_list(
    items: Sequence[SkippedItemView],
    *,
    total: int,
    page: int,
    global_on: bool,
    days: int,
    lang: str,
) -> str:
    state = on_off(global_on, lang)
    note = t("skipped.forwarded_note", lang)  # #47: the switch does not cover forwards
    if total == 0:
        return t("skipped.empty", lang, n=days, state=state) + "\n" + note
    lines = [t("skipped.header", lang, n=days, total=total, state=state), note]
    for number, item in enumerate(items, page * SKIPPED_PAGE_SIZE + 1):
        preview = _truncate(" ".join(item.preview.split()), SKIPPED_PREVIEW_CHARS)
        sender = item.sender or t("card.unknown_contact", lang)
        chat = item.chat_title or t("common.chat", lang)
        lines += [
            "",
            f"{number}. {sender} · {chat} · {item.local_time}",
            f"   “{preview}”",
            "   " + t("skipped.reason", lang, reason=skip_reason(item.reason, lang)),
        ]
        if item.drafting:
            lines.append("   " + t("skipped.drafting", lang))
        elif not item.available:
            lines.append("   " + t("skipped.replied", lang))
    return "\n".join(lines)


def _global_toggle(global_on: bool, lang: str) -> InlineKeyboardButton:
    if global_on:
        return button(t("btn.skip_turn_off", lang), format_callback("skipcfg", "global", "off"))
    return button(t("btn.skip_turn_on", lang), format_callback("skipcfg", "global", "on"))


def skipped_keyboard(
    items: Sequence[SkippedItemView], *, page: int, pages: int, global_on: bool, lang: str
) -> InlineKeyboardMarkup:
    rows = [
        [_global_toggle(global_on, lang)],
        [button(t("btn.skip_per_chat", lang), format_callback("skipchats", 0))],
    ]
    for number, item in enumerate(items, page * SKIPPED_PAGE_SIZE + 1):
        if item.drafting:
            continue
        row = []
        if item.available:
            row.append(
                button(
                    t("btn.draft_anyway", lang, number=number),
                    format_callback("draftany", item.turn_id),
                )
            )
        if item.reason in SETTING_INDEPENDENT_SKIPS:
            pass  # #47 forwards only, #74 daily LLM limit: not the chat's skip setting
        elif item.chat_override is False:
            row.append(
                button(
                    t("btn.skip_use_global_chat", lang, number=number),
                    format_callback("skipchat", item.work_chat_id, "inherit"),
                )
            )
        else:
            row.append(
                button(
                    t("btn.skip_always_draft_chat", lang, number=number),
                    format_callback("skipchat", item.work_chat_id, "off"),
                )
            )
        rows.append(row)
    pager = []
    if page > 0:
        pager.append(button("◀", format_callback("skippage", page - 1, SKIPPED_PAGE_SIZE)))
    if page < pages - 1:
        pager.append(button("▶", format_callback("skippage", page + 1, SKIPPED_PAGE_SIZE)))
    if pager:
        rows.append(pager)
    return keyboard(rows)


def chat_skip_choice(value: bool | None, global_on: bool, lang: str) -> str:
    """``Global (ON)``, ``Always draft`` or ``Skip`` for ``work_chats.skip_closing``."""
    if value is None:
        return t("skipchats.choice.global", lang, state=on_off(global_on, lang))
    return t("skipchats.choice.on" if value else "skipchats.choice.off", lang)


def is_chat_skip_settings(text: str | None) -> bool:
    """Whether a control-chat message is the per-chat skip settings view (in any language)."""
    return bool(text) and any(
        (text or "").startswith(t("skipchats.title", lang)) for lang in LANGUAGES
    )


def render_chat_skip_settings(
    chats: Sequence[ChatSkipView], *, page: int, pages: int, global_on: bool, lang: str
) -> str:
    state = on_off(global_on, lang)
    title = t("skipchats.title", lang)
    lines = [f"{title}   ·   " + t("skipchats.global_state", lang, state=state), ""]
    if not chats:
        return "\n".join([*lines, t("skipchats.none", lang)])
    untitled = t("common.chat", lang)
    lines += [
        f"{number}. {chat.title or untitled} — ✓ {chat_skip_choice(chat.value, global_on, lang)}"
        for number, chat in enumerate(chats, page * SKIPPED_PAGE_SIZE + 1)
    ]
    return "\n".join(lines)


def _mark(label: str, selected: bool) -> str:
    return f"✓ {label}" if selected else label


def chat_skip_buttons(
    action: str, work_chat_id: uuid.UUID, value: bool | None, global_on: bool, lang: str
) -> list[InlineKeyboardButton]:
    """Global / Always draft / Skip for one chat (``<action>:<chat_id>:inherit|off|on``)."""
    return [
        button(
            _mark(chat_skip_choice(choice, global_on, lang), value is choice),
            format_callback(action, work_chat_id, extra),
        )
        for choice, extra in ((None, "inherit"), (False, "off"), (True, "on"))
    ]


def chat_skip_settings_keyboard(
    chats: Sequence[ChatSkipView], *, page: int, pages: int, global_on: bool, lang: str
) -> InlineKeyboardMarkup:
    rows = [[_global_toggle(global_on, lang)]]
    for number, chat in enumerate(chats, page * SKIPPED_PAGE_SIZE + 1):
        buttons = chat_skip_buttons("skipchat", chat.work_chat_id, chat.value, global_on, lang)
        first = buttons[0]
        buttons[0] = button(f"{number} · {first.text}", first.callback_data or "")
        rows.append(buttons)
    pager = []
    if page > 0:
        pager.append(button("◀", format_callback("skipchats", page - 1)))
    if page < pages - 1:
        pager.append(button("▶", format_callback("skipchats", page + 1)))
    if pager:
        rows.append(pager)
    back = format_callback("skippage", 0, SKIPPED_PAGE_SIZE)
    rows.append([button(t("btn.skip_back", lang), back)])
    return keyboard(rows)
