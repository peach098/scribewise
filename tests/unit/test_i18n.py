"""The message catalogs (issue #8): key and placeholder parity, plurals, dates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scribe.i18n import (
    CATALOGS,
    PLURAL_FORMS,
    LocalizedError,
    Msg,
    format_date_time,
    format_day_month,
    format_when,
    language_for_code,
    normalize_lang,
    placeholders,
    plural_form,
    t,
)

NOW = datetime(2026, 11, 5, 11, 5, tzinfo=UTC)


def test_every_key_is_in_both_catalogs() -> None:
    en, ru = set(CATALOGS["en"]), set(CATALOGS["ru"])
    assert sorted(en - ru) == [], "keys missing from ru.py"
    assert sorted(ru - en) == [], "keys missing from en.py"


@pytest.mark.parametrize("key", sorted(CATALOGS["en"]))
def test_placeholders_match_between_languages(key: str) -> None:
    assert placeholders(CATALOGS["en"][key]) == placeholders(CATALOGS["ru"][key])


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_plural_entries_have_exactly_the_language_forms(lang: str) -> None:
    for key, entry in CATALOGS[lang].items():  # type: ignore[index]
        if not isinstance(entry, str):
            assert set(entry) == PLURAL_FORMS[lang], key  # type: ignore[index]
            assert "n" in placeholders(entry), key
        else:
            assert isinstance(entry, str)


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_no_empty_texts(lang: str) -> None:
    for key, entry in CATALOGS[lang].items():  # type: ignore[index]
        values = [entry] if isinstance(entry, str) else list(entry.values())
        assert all(v.strip() for v in values), key


@pytest.mark.parametrize(
    ("n", "form"),
    [
        (0, "many"),
        (1, "one"),
        (2, "few"),
        (4, "few"),
        (5, "many"),
        (11, "many"),
        (12, "many"),
        (14, "many"),
        (21, "one"),
        (22, "few"),
        (25, "many"),
        (101, "one"),
        (111, "many"),
        (112, "many"),
        (1004, "few"),
    ],
)
def test_russian_plural_rules(n: int, form: str) -> None:
    assert plural_form(n, "ru") == form


@pytest.mark.parametrize(("n", "form"), [(0, "other"), (1, "one"), (2, "other"), (21, "other")])
def test_english_plural_rules(n: int, form: str) -> None:
    assert plural_form(n, "en") == form


@pytest.mark.parametrize(
    ("n", "word"),
    [(1, "сообщение"), (2, "сообщения"), (5, "сообщений"), (11, "сообщений"), (21, "сообщение")],
)
def test_russian_plural_text(n: int, word: str) -> None:
    assert f"пришло {n} {word} —" in t("connect.backlog", "ru", n=n)


def test_english_plural_text() -> None:
    assert "1 message arrived" in t("connect.backlog", "en", n=1)
    assert "3 messages arrived" in t("connect.backlog", "en", n=3)


def test_missing_parameter_fails_loudly() -> None:
    with pytest.raises(KeyError):
        t("connect.connected", "en")
    with pytest.raises(KeyError):
        t("no.such.key", "ru")


@pytest.mark.parametrize(
    ("code", "lang"),
    [
        ("ru", "ru"),
        ("ru-RU", "ru"),
        ("uk", "ru"),
        ("be", "ru"),
        ("kk", "ru"),
        ("en", "en"),
        ("en-GB", "en"),
        ("de", "en"),
        (None, "en"),
        ("", "en"),
    ],
)
def test_language_for_telegram_code(code: str | None, lang: str) -> None:
    assert language_for_code(code) == lang


def test_unknown_language_falls_back_to_english() -> None:
    assert normalize_lang(None) == "en"
    assert normalize_lang("de") == "en"
    assert t("btn.done", "de") == "Done"


def test_msg_parameters_render_in_the_same_language() -> None:
    msg = Msg("notice.media_only", name=Msg("common.someone"))
    assert msg.render("en").startswith("📎 Someone sent")
    assert msg.render("ru").startswith("📎 Кто-то:")
    assert msg == Msg("notice.media_only", name=Msg("common.someone"))


def test_localized_error_is_english_as_a_string() -> None:
    exc = LocalizedError("err.unknown_unit", unit="km")
    assert str(exc) == "Unknown unit 'km': use s, min or h."
    assert exc.text("ru") == "Неизвестная единица «km»: используйте с, мин или ч."


def test_dates_use_catalog_names_not_the_system_locale() -> None:
    assert format_day_month(NOW, "en") == "5 Nov"
    assert format_day_month(NOW, "ru") == "5 ноя"
    assert format_day_month(NOW.replace(month=5), "ru") == "5 мая"
    assert format_date_time(NOW, "ru") == "5 ноя 2026, 11:05"


@pytest.mark.parametrize(
    ("delta", "en", "ru"),
    [
        (timedelta(hours=3), "14:05", "сегодня в 14:05"),
        (timedelta(days=1), "11:05 tomorrow", "завтра в 11:05"),
        (timedelta(days=-1), "11:05 yesterday", "вчера в 11:05"),
        (timedelta(days=3), "11:05 8 Nov", "8 ноя в 11:05"),
    ],
)
def test_format_when(delta: timedelta, en: str, ru: str) -> None:
    assert format_when(NOW + delta, NOW, "UTC", "en") == en
    assert format_when(NOW + delta, NOW, "UTC", "ru") == ru


def test_format_when_uses_the_time_zone() -> None:
    late = datetime(2026, 11, 5, 22, 30, tzinfo=UTC)  # 01:30 next day in Moscow
    assert format_when(late, NOW, "Europe/Moscow", "ru") == "завтра в 01:30"
