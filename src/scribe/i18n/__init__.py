"""Control-bot UI texts in the user's language (issue #8): a small dict-based message catalog.

``t(key, lang, **params)`` looks ``key`` up in the catalog of ``lang`` (``en`` or ``ru``,
:mod:`scribe.i18n.en` / :mod:`scribe.i18n.ru`) and fills its named ``{placeholders}``. A
missing placeholder raises ``KeyError``, so a template never reaches the user half-filled.

Plural entries are dicts of CLDR forms (``one`` / ``other`` in English; ``one`` / ``few`` /
``many`` in Russian) and pick the form by the ``n`` parameter (:func:`plural_form`).

Background code that has no incoming update (scheduler, client pool, intake) passes a
:class:`Msg` (a key plus parameters) instead of text; whoever sends it renders it with the
stored ``users.language``. Parameters may themselves be :class:`Msg` values and are rendered
in the same language. :class:`LocalizedError` carries a ``Msg`` for validation errors that
are shown to the user; ``str(exc)`` is the English text (logs and tests).

Why not aiogram's ``I18n`` middleware: it is gettext-based (Babel, compiled ``.mo`` files) and
binds the locale to the current update through a context variable, which does not exist for
messages sent from background jobs. A plain dict is typed, needs no build step, and lets a test
check that both catalogs have the same keys and placeholders.

Dates and times never use the system locale: month and weekday names live in the catalogs
(:func:`format_when`, :func:`format_day_month`, :func:`format_weekday_date`,
:func:`format_date_time`).
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from scribe.i18n import en, ru

Lang = Literal["en", "ru"]
LANGUAGES: tuple[Lang, ...] = ("en", "ru")
DEFAULT_LANG: Lang = "en"
# Telegram language codes whose speakers get the Russian UI by default.
RUSSIAN_DEFAULT_CODES = frozenset({"ru", "uk", "be", "kk"})

Entry = str | Mapping[str, str]
CATALOGS: dict[Lang, Mapping[str, Entry]] = {"en": en.CATALOG, "ru": ru.CATALOG}
PLURAL_FORMS: dict[Lang, frozenset[str]] = {
    "en": frozenset({"one", "other"}),
    "ru": frozenset({"one", "few", "many"}),
}

_FORMATTER = string.Formatter()


def normalize_lang(lang: str | None) -> Lang:
    """``ru`` or ``en``; anything else (``None``, an unknown code) is the default, English."""
    return "ru" if lang == "ru" else DEFAULT_LANG


def language_for_code(language_code: str | None) -> Lang:
    """Default UI language from Telegram's ``language_code``: ru, uk, be, kk -> Russian."""
    base = (language_code or "").split("-", 1)[0].casefold()
    return "ru" if base in RUSSIAN_DEFAULT_CODES else "en"


def plural_form(n: int, lang: str | None) -> str:
    """The CLDR plural category of ``n`` (integers only)."""
    n = abs(int(n))
    if normalize_lang(lang) == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    return "one" if n == 1 else "other"


@dataclass(frozen=True)
class Msg:
    """A catalog key and its parameters, rendered later in the recipient's language."""

    key: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __init__(self, key: str, /, **params: Any) -> None:
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "params", params)

    def render(self, lang: str | None) -> str:
        return t(self.key, lang, **self.params)

    def __hash__(self) -> int:
        return hash((self.key, tuple(sorted((k, repr(v)) for k, v in self.params.items()))))


class LocalizedError(ValueError):
    """A validation error shown to the user; ``str(exc)`` is the English text."""

    def __init__(self, key: str, /, **params: Any) -> None:
        self.msg = Msg(key, **params)
        super().__init__(self.msg.render(DEFAULT_LANG))

    def text(self, lang: str | None) -> str:
        return self.msg.render(lang)


def _template(key: str, lang: Lang, params: Mapping[str, Any]) -> str:
    entry = CATALOGS[lang].get(key)
    if entry is None:
        raise KeyError(f"no {lang} text for {key!r}")
    if isinstance(entry, str):
        return entry
    if "n" not in params:
        raise KeyError(f"plural text {key!r} needs the parameter 'n'")
    return entry[plural_form(int(params["n"]), lang)]


def t(key: str, lang: str | None = DEFAULT_LANG, /, **params: Any) -> str:
    """The text for ``key`` in ``lang`` with ``params`` filled in (``Msg`` values rendered)."""
    language = normalize_lang(lang)
    template = _template(key, language, params)
    values = {k: v.render(language) if isinstance(v, Msg) else v for k, v in params.items()}
    return _FORMATTER.vformat(template, (), values)


def has_text(key: str) -> bool:
    return key in CATALOGS[DEFAULT_LANG]


def render(text: Msg | str, lang: str | None) -> str:
    """``Msg`` -> text in ``lang``; plain text (user content, already rendered) as it is."""
    return text.render(lang) if isinstance(text, Msg) else text


def placeholders(entry: Entry) -> frozenset[str]:
    """Named fields of a catalog entry (all plural forms together)."""
    templates = [entry] if isinstance(entry, str) else list(entry.values())
    names = set()
    for template in templates:
        for _, name, _, _ in _FORMATTER.parse(template):
            if name is not None:
                names.add(name.split(".", 1)[0].split("[", 1)[0])
    return frozenset(names)


# --- dates and times ----------------------------------------------------------------------------


def month_abbr(month: int, lang: str | None) -> str:
    return t(f"month.{month}", lang)


def weekday_abbr(weekday: int, lang: str | None) -> str:
    """``weekday`` 0 = Monday."""
    return t(f"day.{weekday}", lang)


def format_day_month(moment: date, lang: str | None) -> str:
    """``5 Nov`` / ``5 ноя``."""
    return t(
        "date.day_month",
        lang,
        day=moment.day,
        month=month_abbr(moment.month, lang),
    )


def format_day_month_year(moment: date, lang: str | None) -> str:
    """``5 Nov 2026`` / ``5 ноя 2026``."""
    return t(
        "date.day_month_year",
        lang,
        day=moment.day,
        month=month_abbr(moment.month, lang),
        year=moment.year,
    )


def format_weekday_date(day: date, lang: str | None) -> str:
    """``Thu 5 Nov`` / ``Чт, 5 ноя``."""
    return t(
        "date.weekday_day_month",
        lang,
        weekday=weekday_abbr(day.weekday(), lang),
        day=day.day,
        month=month_abbr(day.month, lang),
    )


def format_date_time(moment: datetime, lang: str | None) -> str:
    """``20 Oct 2026, 14:03`` / ``20 окт 2026, 14:03`` (``moment`` already local)."""
    return t(
        "date.full",
        lang,
        day=moment.day,
        month=month_abbr(moment.month, lang),
        year=moment.year,
        time=f"{moment:%H:%M}",
    )


def format_when(moment: datetime, now: datetime, tz: str | ZoneInfo, lang: str | None) -> str:
    """A moment relative to today in ``tz``: ``14:05`` / ``сегодня в 14:05``, ``... tomorrow`` /
    ``завтра в ...``, ``... yesterday`` / ``вчера в ...``, else ``14:05 5 Nov`` / ``5 ноя в 14:05``.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local, today = moment.astimezone(zone), now.astimezone(zone).date()
    hhmm = f"{local:%H:%M}"
    days = (local.date() - today).days
    if days == 0:
        return t("when.today", lang, time=hhmm)
    if days == 1:
        return t("when.tomorrow", lang, time=hhmm)
    if days == -1:
        return t("when.yesterday", lang, time=hhmm)
    return t("when.other", lang, time=hhmm, date=format_day_month(local.date(), lang))


def language_name(lang: str | None) -> str:
    return t(f"language.{normalize_lang(lang)}", lang)


__all__ = [
    "CATALOGS",
    "DEFAULT_LANG",
    "LANGUAGES",
    "Lang",
    "LocalizedError",
    "Msg",
    "PLURAL_FORMS",
    "format_date_time",
    "format_day_month",
    "format_day_month_year",
    "format_weekday_date",
    "format_when",
    "has_text",
    "language_for_code",
    "language_name",
    "month_abbr",
    "normalize_lang",
    "placeholders",
    "plural_form",
    "render",
    "t",
    "weekday_abbr",
]
