"""US2 settings (T045): parsing and validation of delay, hours, days and time zone; re-planning
of waiting turns; reminder/expiry durations (FR-016, FR-017, FR-032)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from scribe.config import Settings
from scribe.intake.waiting import replanned_due_at
from scribe.telegram.control_bot.settings import (
    InvalidSetting,
    delay_label,
    parse_delay_range,
    parse_time_zone,
    parse_working_hours,
    toggle_day,
    waiting_item_text,
)
from tests.conftest import TEST_MASTER_KEY_B64


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2-10 min", (120, 600)),
        ("2 - 10", (120, 600)),  # no unit: minutes
        ("90-300 s", (90, 300)),
        ("90s-5min", (90, 300)),
        ("1-2 h", (3600, 7200)),
        ("5 min", (300, 300)),  # min = max: a fixed delay
        ("0", (0, 0)),
        ("0-24h", (0, 86400)),
        ("3–5 min", (180, 300)),  # en dash
    ],
)
def test_parse_delay_range(text: str, expected: tuple[int, int]) -> None:
    assert parse_delay_range(text) == expected


@pytest.mark.parametrize(
    "text", ["10-2 min", "25 h", "0-86401 s", "", "abc", "-5", "5 days", "1-2-3"]
)
def test_parse_delay_range_rejects(text: str) -> None:
    with pytest.raises(InvalidSetting):
        parse_delay_range(text)


def test_parse_working_hours() -> None:
    assert parse_working_hours("09:00-19:00") == (time(9), time(19))
    assert parse_working_hours("9-17:30") == (time(9), time(17, 30))
    for bad in ("19:00-09:00", "09:00-09:00", "25:00-26:00", "9", "nine-five"):
        with pytest.raises(InvalidSetting):
            parse_working_hours(bad)


def test_parse_time_zone() -> None:
    now = datetime(2026, 10, 21, 11, 0, tzinfo=UTC)
    assert parse_time_zone(" Europe/Berlin ", now) == "Europe/Berlin"
    assert parse_time_zone("europe/berlin", now) == "Europe/Berlin"  # issue #5: any case
    assert parse_time_zone("utc", now) == "UTC"
    assert parse_time_zone("UTC+3", now) == "Etc/GMT-3"
    for bad in ("Mars/Olympus", "../etc/passwd", "", "west"):  # "west" is ambiguous
        with pytest.raises(InvalidSetting):
            parse_time_zone(bad, now)


def test_toggle_day_keeps_at_least_one() -> None:
    assert toggle_day([0, 1, 2, 3, 4], 5) == [0, 1, 2, 3, 4, 5]
    assert toggle_day([0, 1], 1) == [0]
    with pytest.raises(InvalidSetting):
        toggle_day([3], 3)
    with pytest.raises(InvalidSetting):
        toggle_day([3], 7)


def test_delay_label() -> None:
    assert delay_label(120, 600, "en") == "2 min–10 min, random for every message"
    assert delay_label(300, 300, "en") == "5 min (fixed)"
    assert delay_label(0, 90, "en") == "0 s–90 s, random for every message"


def _user(**values: Any) -> Any:
    defaults = {
        "timezone": "UTC",
        "working_days": [0, 1, 2, 3, 4],
        "work_start": time(9),
        "work_end": time(19),
    }
    return SimpleNamespace(**{**defaults, **values})


def test_replanned_due_at() -> None:
    wednesday_10 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    arrived = wednesday_10
    # Planned in the future inside hours: unchanged.
    due = arrived + timedelta(seconds=300)
    assert replanned_due_at(arrived, 300, due, _user(), wednesday_10) == due
    # Hours now end at 10:03: the read moves to Thursday 09:00.
    late = _user(work_end=time(10, 3))
    assert replanned_due_at(arrived, 300, due, late, wednesday_10) == datetime(
        2026, 10, 15, 9, 0, tzinfo=UTC
    )
    # Overdue (e.g. after /resume): due now when now is inside hours, else the next window.
    now = wednesday_10 + timedelta(hours=2)
    assert replanned_due_at(arrived, 300, due, _user(), now) == now
    evening = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)
    assert replanned_due_at(arrived, 300, due, _user(), evening) == datetime(
        2026, 10, 15, 9, 0, tzinfo=UTC
    )
    # A wait cut short on purpose (read now / read elsewhere) is kept.
    assert replanned_due_at(arrived, 300, arrived, late, evening) == arrived


def test_waiting_item_text() -> None:
    now = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    text = waiting_item_text(
        "Anna",
        now - timedelta(minutes=5),
        now + timedelta(hours=1),
        "x" * 200,
        "Europe/Berlin",
        now,
        "en",
    )
    first, second = text.split("\n")
    assert first == "⏳ Anna · arrived 11:55 · will be read at 13:00"
    assert second.startswith("› xxx") and len(second) == 2 + 120
    tomorrow = waiting_item_text("Anna", now, now + timedelta(days=1), "hi", "UTC", now, "en")
    assert "will be read at 10:00 tomorrow" in tomorrow
    later = waiting_item_text("Anna", now, now + timedelta(days=3), "hi", "UTC", now, "en")
    assert "will be read at 10:00 17 Oct" in later


def test_waiting_item_text_in_russian() -> None:
    now = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    text = waiting_item_text(
        "", now - timedelta(days=1), now + timedelta(hours=1), "hi", "Europe/Moscow", now, "ru"
    )
    assert text.split("\n")[0] == "⏳ Кто-то · пришло вчера в 13:00 · прочитаю сегодня в 14:00"
    due = waiting_item_text("Анна", now, now, "hi", "UTC", now, "ru")
    assert "прочитаю сейчас" in due
    later = waiting_item_text("Анна", now, now + timedelta(days=22), "hi", "UTC", now, "ru")
    assert "прочитаю 5 ноя в 10:00" in later


def test_delay_label_and_units_in_russian() -> None:
    assert delay_label(120, 600, "ru") == "2 мин–10 мин, случайно для каждого сообщения"
    assert delay_label(3600, 3600, "ru") == "1 ч (фиксированная)"
    assert parse_delay_range("2-10 мин") == (120, 600)
    assert parse_delay_range("90-300 с") == (90, 300)
    assert parse_delay_range("1-2 ч") == (3600, 7200)


def test_invalid_setting_is_localized() -> None:
    with pytest.raises(InvalidSetting) as raised:
        parse_delay_range("5 km")
    assert str(raised.value) == "Unknown unit 'km': use s, min or h."
    assert raised.value.text("ru") == "Неизвестная единица «km»: используйте с, мин или ч."
    with pytest.raises(InvalidSetting) as raised:
        toggle_day([3], 3)
    assert raised.value.text("ru") == "Оставьте хотя бы один рабочий день."


def _settings(**values: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        master_key=TEST_MASTER_KEY_B64,
        bot_token="123456:TEST-TOKEN-NOT-REAL",
        tg_api_id=1,
        tg_api_hash="test-api-hash",
        **values,
    )


def test_review_deadlines_and_fast_clock() -> None:
    assert _settings().review_deadlines_s == (7200, 86400)  # 2 h reminder, 24 h expiry
    assert _settings(test_fast_clock=True).review_deadlines_s == (60, 120)
    assert _settings(draft_remind_after_s=600, draft_expire_after_s=1200).review_deadlines_s == (
        600,
        1200,
    )
