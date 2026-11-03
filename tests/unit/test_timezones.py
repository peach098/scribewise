"""Issue #5: human time zone input (cities, Russian aliases, offsets) -> IANA zones."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scribe.telegram.control_bot.timezones import (
    OFFSET_NOTE,
    AmbiguousTimeZone,
    ResolvedTimeZone,
    UnknownTimeZone,
    confirmation_text,
    format_offset,
    known_time_zones,
    quick_zones,
    resolve_time_zone,
    time_zone_keyboard,
    zone_from_callback,
    zone_label,
)

NOW = datetime(2026, 10, 21, 11, 5, tzinfo=UTC)  # Europe still on summer time (until Oct 25)
WINTER = datetime(2026, 1, 15, 11, 5, tzinfo=UTC)


def _offset(zone: str, now: datetime = NOW) -> timedelta | None:
    return ZoneInfo(zone).utcoffset(now)


@pytest.mark.parametrize(
    "text",
    [
        "Europe/Moscow",
        "europe/moscow",
        " EUROPE/MOSCOW ",
        "moscow",
        "Moscow",
        "Москва",
        "москва",
        "МСК",
        "msk",
        "Санкт-Петербург",
        "St. Petersburg",
        "UTC+3",
        "GMT+3",
        "+3",
        "+03:00",
        "+0300",
        "utc +3",
        "UTC3",
        "МСК+0",
    ],
)
def test_moscow_inputs_are_utc_plus_3(text: str) -> None:
    zone = resolve_time_zone(text, NOW).zone
    assert zone in known_time_zones()
    assert _offset(zone) == timedelta(hours=3)
    assert _offset(zone, WINTER) == timedelta(hours=3)


@pytest.mark.parametrize(
    ("text", "zone"),
    [
        ("Europe/Moscow", "Europe/Moscow"),
        ("europe/moscow", "Europe/Moscow"),
        ("moscow", "Europe/Moscow"),
        ("Москва", "Europe/Moscow"),
        ("МСК", "Europe/Moscow"),
        ("new york", "America/New_York"),
        ("new_york", "America/New_York"),
        ("New-York", "America/New_York"),
        ("america/new_york", "America/New_York"),
        ("buenos aires", "America/Argentina/Buenos_Aires"),  # link and zone collapse
        ("port au prince", "America/Port-au-Prince"),
        ("Екатеринбург", "Asia/Yekaterinburg"),
        ("Новосибирск", "Asia/Novosibirsk"),
        ("Владивосток", "Asia/Vladivostok"),
        ("Минск", "Europe/Minsk"),
        ("Алматы", "Asia/Almaty"),
        ("Тбилиси", "Asia/Tbilisi"),
        ("CET", "Europe/Berlin"),
        ("EET", "Europe/Athens"),
        ("PST", "America/Los_Angeles"),
        ("PT", "America/Los_Angeles"),
        ("utc", "UTC"),
        ("GMT", "UTC"),
        ("UTC+0", "UTC"),
        ("-0", "UTC"),
        ("Etc/GMT-3", "Etc/GMT-3"),
    ],
)
def test_resolves_to_zone(text: str, zone: str) -> None:
    assert resolve_time_zone(text, NOW).zone == zone


@pytest.mark.parametrize("text", ["Киев", "Kyiv", "kiev"])
def test_kyiv(text: str) -> None:
    assert resolve_time_zone(text, NOW).zone in {"Europe/Kyiv", "Europe/Kiev"}


@pytest.mark.parametrize(
    ("text", "zone", "hours"),
    [
        ("UTC+3", "Etc/GMT-3", 3),  # POSIX sign: Etc/GMT-3 is three hours *ahead* of UTC
        ("GMT-5", "Etc/GMT+5", -5),
        ("−5", "Etc/GMT+5", -5),  # Unicode minus
        ("+14", "Etc/GMT-14", 14),
        ("UTC-12", "Etc/GMT+12", -12),
        ("+10", "Etc/GMT-10", 10),
        ("МСК+1", "Etc/GMT-4", 4),  # Moscow + 1 = UTC+4
        ("msk-1", "Etc/GMT-2", 2),
    ],
)
def test_whole_hour_offsets_invert_the_etc_sign(text: str, zone: str, hours: int) -> None:
    resolved = resolve_time_zone(text, NOW)
    assert resolved.zone == zone
    assert _offset(zone) == timedelta(hours=hours)
    assert resolved.note == OFFSET_NOTE  # a city is better: it follows DST


@pytest.mark.parametrize(
    ("text", "zone", "offset"),
    [
        ("UTC+5:30", "Asia/Kolkata", timedelta(hours=5, minutes=30)),
        ("+0545", "Asia/Kathmandu", timedelta(hours=5, minutes=45)),
        ("UTC+3:30", "Asia/Tehran", timedelta(hours=3, minutes=30)),
        ("GMT-2:30", "America/St_Johns", timedelta(hours=-2, minutes=-30)),  # summer time
    ],
)
def test_non_whole_offsets_map_to_a_city_at_that_offset_now(
    text: str, zone: str, offset: timedelta
) -> None:
    resolved = resolve_time_zone(text, NOW)
    assert resolved.zone == zone
    assert _offset(zone) == offset
    assert resolved.note is not None and zone in resolved.note.render("en")


def test_city_names_have_no_note() -> None:
    assert resolve_time_zone("Moscow", NOW).note is None
    assert resolve_time_zone("UTC", NOW).note is None


def test_ambiguous_city_offers_choices() -> None:
    with pytest.raises(AmbiguousTimeZone) as raised:
        resolve_time_zone("west", NOW)
    assert set(raised.value.choices) == {"Australia/West", "Brazil/West"}
    assert "Several time zones match 'west'" in str(raised.value)


@pytest.mark.parametrize(
    "text",
    ["Mars/Olympus", "", "   ", "../etc/passwd", "5", "UTC+15", "UTC-13", "UTC+3:20", "atlantis"],
)
def test_unknown_input_is_refused(text: str) -> None:
    with pytest.raises(UnknownTimeZone):
        resolve_time_zone(text, NOW)


def test_unknown_input_shows_examples() -> None:
    with pytest.raises(UnknownTimeZone) as raised:
        resolve_time_zone("atlantis", NOW)
    message = str(raised.value)
    assert "don't know the time zone 'atlantis'" in message
    assert "Moscow" in message and "Europe/Moscow" in message and "UTC+3" in message


def test_offset_with_no_zone_now_is_refused() -> None:
    with pytest.raises(UnknownTimeZone, match="No time zone is at UTC-5:30"):
        resolve_time_zone("UTC-5:30", NOW)


def test_format_offset_and_label() -> None:
    assert format_offset(timedelta(hours=3)) == "UTC+3"
    assert format_offset(timedelta(hours=-3, minutes=-30)) == "UTC-3:30"
    assert format_offset(timedelta(hours=5, minutes=45)) == "UTC+5:45"
    assert format_offset(timedelta()) == "UTC"
    assert zone_label("Etc/GMT-3", NOW) == "Etc/GMT-3 (UTC+3)"
    assert zone_label("UTC", NOW) == "UTC"


def test_confirmation_shows_zone_and_local_time() -> None:
    text = confirmation_text(ResolvedTimeZone("Europe/Moscow"), NOW, "en")
    assert text == "✅ Time zone set to Europe/Moscow (UTC+3, now 14:05 there)."
    with_note = confirmation_text(resolve_time_zone("+3", NOW), NOW, "en")
    assert with_note.startswith("✅ Time zone set to Etc/GMT-3 (UTC+3, now 14:05 there).\n💡")


def test_quick_zones_follow_the_telegram_language() -> None:
    assert quick_zones("ru")[0] == "Europe/Moscow"
    assert quick_zones("ru-RU")[0] == "Europe/Moscow"
    assert quick_zones("de")[0] == "Europe/Berlin"
    default = quick_zones(None)
    assert default == quick_zones("xx")
    assert "Europe/Moscow" in default
    for language in ("ru", "en", "uk", "kk", None):
        zones = quick_zones(language)
        assert len(zones) == len(set(zones)) <= 6
        assert all(zone in known_time_zones() for zone in zones)


def test_time_zone_keyboard_and_callback() -> None:
    markup = time_zone_keyboard("settz", ("Europe/Moscow", "America/New_York", "UTC"), NOW, "en")
    rows = markup.inline_keyboard
    assert [[b.text for b in row] for row in rows] == [
        ["Moscow (UTC+3)", "New York (UTC-4)"],
        ["UTC"],
    ]
    assert rows[0][0].callback_data == "settz:Europe/Moscow"
    # Every zone name fits Telegram's 64-byte callback data limit.
    longest = max(known_time_zones(), key=len)
    time_zone_keyboard("setuptz", (longest,), NOW, "en")
    assert zone_from_callback("Europe/Moscow") == "Europe/Moscow"
    assert zone_from_callback("Mars/Olympus") is None
