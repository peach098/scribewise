"""Time zone input for ``/settings`` and setup (issue #5): human input -> an IANA zone name.

The zone is stored as text in ``users.timezone`` and loaded with ``zoneinfo.ZoneInfo`` wherever
it is used, so the result is always a name ``zoneinfo`` knows. :func:`resolve_time_zone` tries,
in order:

1. A UTC offset: ``UTC+3``, ``GMT+3``, ``+3``, ``+03:00``, ``UTC-5:30``, ``МСК+1`` (Moscow + 1).
   Whole hours become ``Etc/GMT-3`` style zones. Note the POSIX sign inversion: ``Etc/GMT-3``
   *is* UTC+3. ``zoneinfo`` has no fixed zones for non-whole offsets, so ``UTC+5:30`` becomes
   the city zone that is at that offset now (``Asia/Kolkata``; a short table picks the usual
   one when several match). Offsets carry a note that a city is better: it follows DST.
2. An alias: Russian city names (``Москва``, ``МСК``, ``Санкт-Петербург``, …) and common
   abbreviations (``CET``, ``PST``, ``EST``, …) mapped to city zones.
3. An IANA name, case-insensitive: ``europe/moscow`` -> ``Europe/Moscow``.
4. A city, i.e. the last segment of an IANA name: ``moscow``, ``new york``, ``new_york``.
   Links to the same zone (``Asia/Nicosia`` / ``Europe/Nicosia``) count as one match; truly
   different zones raise :class:`AmbiguousTimeZone` with the choices.
5. A top-level legacy name, case-insensitive (``japan`` -> ``Japan``).

Anything else raises :class:`UnknownTimeZone` with examples. Pure apart from reading the
time zone database (the system one, plus the ``tzdata`` package so legacy names such as
``Brazil/West`` exist even where the OS ships without them); no I/O beyond that. Errors and
notes are ``scribe.i18n`` messages (``LocalizedError`` / ``Msg``), rendered in the user's
language by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from scribe.i18n import LocalizedError, Msg, has_text, t
from scribe.telegram.control_bot.render import button, format_callback, keyboard

MAX_CHOICES = 8
QUICK_ZONES = 6
OFFSET_NOTE = Msg("tz.offset_note")
# Areas that only hold backward-compatible links (``US/Eastern``); real cities win over them.
_LEGACY_AREAS = frozenset({"US", "Canada", "Brazil", "Chile", "Mexico"})
# Usual zone for a non-whole offset when several zones are at it.
_NON_WHOLE_PREFERRED = {
    (3, 30): "Asia/Tehran",
    (4, 30): "Asia/Kabul",
    (5, 30): "Asia/Kolkata",
    (5, 45): "Asia/Kathmandu",
    (6, 30): "Asia/Yangon",
    (8, 45): "Australia/Eucla",
    (9, 30): "Australia/Darwin",
    (10, 30): "Australia/Adelaide",
    (12, 45): "Pacific/Chatham",
    (-3, -30): "America/St_Johns",
    (-2, -30): "America/St_Johns",
    (-9, -30): "Pacific/Marquesas",
}
_OFFSET = re.compile(r"^(utc|gmt|msk|мск)?([+-]?)(\d{1,2})(?:[:.]?(\d{2}))?$")


class UnknownTimeZone(LocalizedError):
    """Input that names no time zone; the message (with examples) is shown to the user."""


class AmbiguousTimeZone(LocalizedError):
    """Input that matches several different zones; the user picks one of ``choices``."""

    def __init__(self, text: str, choices: tuple[str, ...]) -> None:
        super().__init__("tz.ambiguous", text=text)
        self.choices = choices


@dataclass(frozen=True)
class ResolvedTimeZone:
    zone: str  # an IANA name ``zoneinfo`` can load
    note: Msg | None = None  # shown under the confirmation (e.g. "a city follows DST")


# --- indexes ------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def known_time_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def _first_known(*names: str) -> str:
    known = known_time_zones()
    return next((name for name in names if name in known), names[-1])


def _key(text: str) -> str:
    """Normalised lookup key: case-folded, ``ё`` -> ``е``, spaces/hyphens -> ``_``, no dots."""
    folded = " ".join(text.strip().casefold().replace("ё", "е").replace(".", " ").split())
    return re.sub(r"[ \-]", "_", folded)


@lru_cache(maxsize=1)
def _full_name_index() -> dict[str, str]:
    return {_key(name): name for name in known_time_zones()}


@lru_cache(maxsize=1)
def _city_index() -> dict[str, tuple[str, ...]]:
    index: dict[str, list[str]] = {}
    for name in known_time_zones():
        if "/" not in name or name.startswith(("Etc/", "SystemV/")):
            continue
        index.setdefault(_key(name.rsplit("/", 1)[1]), []).append(name)
    return {key: tuple(sorted(names)) for key, names in index.items()}


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    moscow = "Europe/Moscow"
    kyiv = _first_known("Europe/Kyiv", "Europe/Kiev")
    table = {
        # Russian (and nearby) cities, as users type them.
        "мск": moscow,
        "msk": moscow,
        "москва": moscow,
        "moskva": moscow,
        "санкт-петербург": moscow,
        "петербург": moscow,
        "питер": moscow,
        "спб": moscow,
        "spb": moscow,
        "saint petersburg": moscow,
        "st petersburg": moscow,
        "sankt-peterburg": moscow,
        "казань": moscow,
        "нижний новгород": moscow,
        "ростов-на-дону": moscow,
        "краснодар": moscow,
        "сочи": moscow,
        "воронеж": moscow,
        "калининград": "Europe/Kaliningrad",
        "самара": "Europe/Samara",
        "екатеринбург": "Asia/Yekaterinburg",
        "ekaterinburg": "Asia/Yekaterinburg",
        "екб": "Asia/Yekaterinburg",
        "пермь": "Asia/Yekaterinburg",
        "челябинск": "Asia/Yekaterinburg",
        "уфа": "Asia/Yekaterinburg",
        "омск": "Asia/Omsk",
        "новосибирск": "Asia/Novosibirsk",
        "нск": "Asia/Novosibirsk",
        "красноярск": "Asia/Krasnoyarsk",
        "иркутск": "Asia/Irkutsk",
        "якутск": "Asia/Yakutsk",
        "владивосток": "Asia/Vladivostok",
        "хабаровск": "Asia/Vladivostok",
        "магадан": "Asia/Magadan",
        "камчатка": "Asia/Kamchatka",
        "минск": "Europe/Minsk",
        "киев": kyiv,
        "київ": kyiv,
        "kiev": kyiv,
        "kyiv": kyiv,
        "алматы": "Asia/Almaty",
        "алма-ата": "Asia/Almaty",
        "астана": "Asia/Almaty",
        "astana": "Asia/Almaty",
        "тбилиси": "Asia/Tbilisi",
        "ереван": "Asia/Yerevan",
        "баку": "Asia/Baku",
        "ташкент": "Asia/Tashkent",
        "бишкек": "Asia/Bishkek",
        "рига": "Europe/Riga",
        "вильнюс": "Europe/Vilnius",
        "таллин": "Europe/Tallinn",
        "кишинев": "Europe/Chisinau",
        "стамбул": "Europe/Istanbul",
        "istanbul": "Europe/Istanbul",
        "берлин": "Europe/Berlin",
        "лондон": "Europe/London",
        "нью-йорк": "America/New_York",
        "дубай": "Asia/Dubai",
        "nyc": "America/New_York",
        "la": "America/Los_Angeles",
        "sf": "America/Los_Angeles",
        "san francisco": "America/Los_Angeles",
        # Abbreviations -> a city that follows the same daylight saving rules.
        "utc": "UTC",
        "gmt": "UTC",
        "z": "UTC",
        "zulu": "UTC",
        "cet": "Europe/Berlin",
        "cest": "Europe/Berlin",
        "eet": "Europe/Athens",
        "eest": "Europe/Athens",
        "wet": "Europe/Lisbon",
        "bst": "Europe/London",
        "pst": "America/Los_Angeles",
        "pdt": "America/Los_Angeles",
        "pt": "America/Los_Angeles",
        "mst": "America/Denver",
        "mdt": "America/Denver",
        "mt": "America/Denver",
        "cst": "America/Chicago",
        "cdt": "America/Chicago",
        "ct": "America/Chicago",
        "est": "America/New_York",
        "edt": "America/New_York",
        "et": "America/New_York",
        "ist": "Asia/Kolkata",
        "jst": "Asia/Tokyo",
        "kst": "Asia/Seoul",
        "hkt": "Asia/Hong_Kong",
        "aest": "Australia/Sydney",
        "aedt": "Australia/Sydney",
    }
    return {_key(alias): zone for alias, zone in table.items()}


# --- offsets ------------------------------------------------------------------------------------


def format_offset(offset: timedelta | None) -> str:
    """``UTC+3``, ``UTC-3:30``, ``UTC+5:45``; ``UTC`` for zero."""
    minutes = round((offset or timedelta()).total_seconds() / 60)
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def _parse_offset(text: str) -> timedelta | None:
    """The offset ``text`` names, or None when it is not offset-shaped at all."""
    compact = re.sub(r"\s+", "", text.casefold()).replace("−", "-").replace("–", "-")
    match = _OFFSET.match(compact)
    if match is None:
        return None
    prefix, sign, hours_s, minutes_s = match.groups()
    if not prefix and not sign:
        return None  # a bare number is not an offset
    hours, minutes = int(hours_s), int(minutes_s or 0)
    if minutes not in (0, 30, 45):
        raise UnknownTimeZone("tz.offset_minutes")
    offset = timedelta(hours=hours, minutes=minutes) * (-1 if sign == "-" else 1)
    if prefix in ("msk", "мск"):
        offset += timedelta(hours=3)
    if not timedelta(hours=-12) <= offset <= timedelta(hours=14):
        raise UnknownTimeZone("tz.offset_range")
    return offset


def _zone_for_offset(offset: timedelta, now: datetime) -> ResolvedTimeZone:
    minutes = round(offset.total_seconds() / 60)
    if minutes == 0:
        return ResolvedTimeZone("UTC")
    if minutes % 60 == 0:
        # POSIX sign convention: Etc/GMT-3 is three hours *ahead* of UTC.
        return ResolvedTimeZone(f"Etc/GMT{-minutes // 60:+d}", OFFSET_NOTE)
    candidates = tuple(zone for zone in _city_zones() if ZoneInfo(zone).utcoffset(now) == offset)
    hours, rest = divmod(abs(minutes), 60)
    key = (hours, rest) if minutes > 0 else (-hours, -rest)
    preferred = _NON_WHOLE_PREFERRED.get(key)
    if preferred in candidates:
        zone = preferred
    elif candidates:
        zone = _distinct(candidates, now)[0]
    else:
        raise UnknownTimeZone("tz.no_zone_at", offset=format_offset(offset))
    note = Msg("tz.non_whole_note", zone=zone, offset=format_offset(offset))
    return ResolvedTimeZone(zone, note)


@lru_cache(maxsize=1)
def _city_zones() -> tuple[str, ...]:
    return tuple(sorted(zone for names in _city_index().values() for zone in names))


# --- choosing between matches -------------------------------------------------------------------


def _preference(zone: str) -> tuple[bool, int, str]:
    area = zone.split("/", 1)[0]
    return (area in _LEGACY_AREAS, -zone.count("/"), zone)


def _behaviour(zone: str, now: datetime) -> tuple[timedelta | None, ...]:
    tz = ZoneInfo(zone)
    return tuple(tz.utcoffset(now + timedelta(days=15 * step)) for step in range(25))


def _distinct(zones: tuple[str, ...], now: datetime) -> tuple[str, ...]:
    """One zone per behaviour over the next year (links to the same zone collapse)."""
    groups: dict[tuple[timedelta | None, ...], list[str]] = {}
    for zone in zones:
        groups.setdefault(_behaviour(zone, now), []).append(zone)
    best = [min(group, key=_preference) for group in groups.values()]
    return tuple(sorted(best, key=_preference))


# --- resolve ------------------------------------------------------------------------------------


def resolve_time_zone(text: str, now: datetime) -> ResolvedTimeZone:
    """Human input -> an IANA zone ``zoneinfo`` can load (see the module docstring)."""
    raw = text.strip()
    if not raw:
        raise UnknownTimeZone("tz.unknown", text=raw)
    offset = _parse_offset(raw)
    if offset is not None:
        return _zone_for_offset(offset, now)
    key = _key(raw)
    alias = _aliases().get(key)
    if alias is not None:
        return ResolvedTimeZone(alias)
    full = _full_name_index().get(key)
    if full is not None and "/" in full:
        return ResolvedTimeZone(full)
    cities = _city_index().get(key)
    if cities:
        distinct = _distinct(cities, now)
        if len(distinct) == 1:
            return ResolvedTimeZone(distinct[0])
        raise AmbiguousTimeZone(raw, distinct[:MAX_CHOICES])
    if full is not None:
        return ResolvedTimeZone(full)
    raise UnknownTimeZone("tz.unknown", text=raw)


# --- rendering ----------------------------------------------------------------------------------


def zone_label(zone: str, now: datetime) -> str:
    """``Europe/Moscow (UTC+3)``; just ``UTC`` for UTC."""
    if zone == "UTC":
        return "UTC"
    return f"{zone} ({format_offset(ZoneInfo(zone).utcoffset(now))})"


def confirmation_text(resolved: ResolvedTimeZone, now: datetime, lang: str) -> str:
    """``✅ Time zone set to Europe/Moscow (UTC+3, now 14:05 there).``"""
    tz = ZoneInfo(resolved.zone)
    local = now.astimezone(tz)
    offset = format_offset(tz.utcoffset(now))
    text = t("tz.confirm", lang, zone=resolved.zone, offset=offset, time=f"{local:%H:%M}")
    return text + (f"\n{resolved.note.render(lang)}" if resolved.note else "")


_QUICK_DEFAULT = (
    "Europe/London",
    "Europe/Berlin",
    "Europe/Moscow",
    "Asia/Dubai",
    "America/New_York",
    "America/Los_Angeles",
)


@lru_cache(maxsize=1)
def _quick_by_language() -> dict[str, tuple[str, ...]]:
    kyiv = _first_known("Europe/Kyiv", "Europe/Kiev")
    return {
        "ru": (
            "Europe/Moscow",
            "Europe/Kaliningrad",
            "Europe/Samara",
            "Asia/Yekaterinburg",
            "Asia/Novosibirsk",
            "Asia/Vladivostok",
        ),
        "uk": (kyiv, "Europe/Warsaw", "Europe/Berlin"),
        "be": ("Europe/Minsk", "Europe/Moscow", "Europe/Warsaw"),
        "kk": ("Asia/Almaty", "Asia/Aqtobe", "Europe/Moscow"),
        "uz": ("Asia/Tashkent", "Europe/Moscow"),
        "ka": ("Asia/Tbilisi", "Europe/Moscow"),
        "hy": ("Asia/Yerevan", "Europe/Moscow"),
        "az": ("Asia/Baku", "Europe/Moscow"),
        "de": ("Europe/Berlin", "Europe/Vienna", "Europe/Zurich"),
        "fr": ("Europe/Paris",),
        "es": ("Europe/Madrid", "America/Mexico_City"),
        "it": ("Europe/Rome",),
        "pt": ("Europe/Lisbon", "America/Sao_Paulo"),
        "pl": ("Europe/Warsaw",),
        "tr": ("Europe/Istanbul",),
        "en": ("Europe/London", "America/New_York", "America/Chicago", "America/Los_Angeles"),
    }


def quick_zones(language_code: str | None) -> tuple[str, ...]:
    """Likely zones for the quick buttons: the Telegram language's zones first (ru -> Moscow)."""
    language = (language_code or "").split("-", 1)[0].casefold()
    ordered = [*_quick_by_language().get(language, ()), *_QUICK_DEFAULT]
    known = known_time_zones()
    unique = [zone for i, zone in enumerate(ordered) if zone in known and zone not in ordered[:i]]
    return tuple(unique[:QUICK_ZONES])


def city_name(zone: str, lang: str) -> str:
    """The city of a zone for buttons: from the catalog when it has one, else the IANA city."""
    key = f"city.{zone}"
    return t(key, lang) if has_text(key) else zone.rsplit("/", 1)[-1].replace("_", " ")


def _button_label(zone: str, now: datetime, lang: str) -> str:
    if zone == "UTC":
        return "UTC"
    return f"{city_name(zone, lang)} ({format_offset(ZoneInfo(zone).utcoffset(now))})"


def time_zone_keyboard(
    action: str,
    zones: tuple[str, ...],
    now: datetime,
    lang: str,
    *,
    extra: list[InlineKeyboardButton] | None = None,
) -> InlineKeyboardMarkup:
    """Two zone buttons per row (``<action>:<zone>``), then ``extra`` as its own row."""
    buttons = [
        button(_button_label(zone, now, lang), format_callback(action, zone)) for zone in zones
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if extra:
        rows.append(extra)
    return keyboard(rows)


def zone_from_callback(entity_id: str) -> str | None:
    """A zone name from callback data, only if ``zoneinfo`` knows it."""
    return entity_id if entity_id in known_time_zones() else None
