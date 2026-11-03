"""Issue #74: daily LLM budget decisions (caps, UTC day rollover, tier), its settings, and the
operators' spend alert (percentile rule, sections, rendering). Synthetic ids and amounts only."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from scribe.config import Settings
from scribe.drafting.budget import (
    BudgetCaps,
    drafting_tier,
    next_reset,
    paused_alert,
    resume_time,
    utc_day,
)
from scribe.observability.spend_alert import (
    PERCENTILE_MIN_USERS,
    build_alert,
    nearest_rank,
    render_alert,
)

KEY_B64 = base64.b64encode(b"\x00" * 32).decode()
CAPS = BudgetCaps(soft_usd=1.0, hard_usd=3.0)
DAY = date(2026, 10, 30)


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        master_key=KEY_B64,
        bot_token="placeholder",
        tg_api_id=1,
        tg_api_hash="placeholder",
        **overrides,
    )


# --- cap decisions --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spent", "level"),
    [
        (0, "ok"),
        (0.999999, "ok"),
        (1.0, "soft"),  # "at or above"
        (Decimal("2.999999"), "soft"),
        (3.0, "hard"),
        (Decimal("250"), "hard"),
    ],
)
def test_level_by_spend(spent: float | Decimal, level: str) -> None:
    assert CAPS.level(spent) == level


def test_a_cap_of_zero_is_off() -> None:
    assert BudgetCaps(0, 3.0).level(2.5) == "ok"
    assert BudgetCaps(0, 3.0).level(3.0) == "hard"
    assert BudgetCaps(1.0, 0).level(1000) == "soft"
    off = BudgetCaps(0, 0)
    assert not off.enabled and off.level(1000) == "ok"
    assert BudgetCaps(1.0, 0).enabled and BudgetCaps(0, 3.0).enabled


def test_float_caps_compare_exactly_with_decimal_spend() -> None:
    # 0.1 + 0.2 is not 0.3 in binary floats; the numeric column sums exactly.
    caps = BudgetCaps(soft_usd=0.3, hard_usd=0.7)
    assert caps.level(Decimal("0.1") + Decimal("0.2")) == "soft"
    assert caps.level(Decimal("0.7")) == "hard"


def test_soft_and_hard_draft_on_economy_without_touching_the_stored_tier() -> None:
    assert drafting_tier("quality", "ok") == "quality"
    assert drafting_tier("economy", "ok") == "economy"
    assert drafting_tier("quality", "soft") == "economy"
    assert drafting_tier("quality", "hard") == "economy"


# --- day rollover ---------------------------------------------------------------------------------


def test_the_budget_day_is_the_utc_date() -> None:
    moscow = timezone(timedelta(hours=3))
    # 01:30 in Moscow on the 31st is still the 30th in UTC: the same budget day.
    assert utc_day(datetime(2026, 10, 31, 1, 30, tzinfo=moscow)) == DAY
    assert utc_day(datetime(2026, 10, 30, 23, 59, 59, tzinfo=UTC)) == DAY
    assert utc_day(datetime(2026, 10, 31, 0, 0, tzinfo=UTC)) == DAY + timedelta(days=1)


def test_next_reset_is_the_next_midnight_utc() -> None:
    reset = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    assert next_reset(datetime(2026, 10, 30, 0, 0, tzinfo=UTC)) == reset
    assert next_reset(datetime(2026, 10, 30, 23, 59, 59, tzinfo=UTC)) == reset
    assert next_reset(reset) == reset + timedelta(days=1)  # midnight starts a new day


def test_resume_time_in_the_users_zone() -> None:
    now = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    assert resume_time(now, "UTC") == "00:00"
    assert resume_time(now, "Europe/Moscow") == "03:00"
    assert resume_time(now, "America/New_York") == "20:00"  # EDT, the evening before
    alert = paused_alert("Europe/Moscow", now)
    assert "03:00" in alert.render("en") and "03:00" in alert.render("ru")


# --- settings -------------------------------------------------------------------------------------


def test_budget_defaults_and_zero() -> None:
    s = _settings()
    assert (s.llm_daily_soft_cap_usd, s.llm_daily_hard_cap_usd) == (1.0, 3.0)
    assert BudgetCaps.from_settings(s) == CAPS
    off = _settings(llm_daily_soft_cap_usd=0, llm_daily_hard_cap_usd=0)
    assert not BudgetCaps.from_settings(off).enabled
    # One cap off, the other set: any order is fine.
    assert _settings(llm_daily_soft_cap_usd=5, llm_daily_hard_cap_usd=0).llm_daily_soft_cap_usd == 5


def test_budget_settings_are_validated() -> None:
    with pytest.raises(ValidationError, match="llm_daily_soft_cap_usd must be <="):
        _settings(llm_daily_soft_cap_usd=4, llm_daily_hard_cap_usd=3)
    with pytest.raises(ValidationError):
        _settings(llm_daily_hard_cap_usd=-1)


def test_budget_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBE_LLM_DAILY_SOFT_CAP_USD", "2.5")
    monkeypatch.setenv("SCRIBE_LLM_DAILY_HARD_CAP_USD", "0")
    s = _settings()
    assert (s.llm_daily_soft_cap_usd, s.llm_daily_hard_cap_usd) == (2.5, 0)


# --- operator alert -------------------------------------------------------------------------------


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=i + 1) for i in range(n)]


def test_nearest_rank() -> None:
    values = [Decimal(v) for v in range(1, 21)]  # 1..20
    assert nearest_rank(values, 0.95) == 19
    assert nearest_rank(values, 0.5) == 10
    assert nearest_rank([Decimal(7)], 0.95) == 7


def test_percentile_needs_enough_users() -> None:
    ids = _ids(PERCENTILE_MIN_USERS - 1)
    totals = {u: Decimal("0.10") for u in ids} | {ids[-1]: Decimal("0.90")}
    alert = build_alert(DAY, totals, CAPS)
    assert alert.p95 is None and alert.above_p95 == [] and alert.empty
    assert "fewer than 20 users" in render_alert(alert, CAPS, "en")


def test_users_above_p95_with_enough_users() -> None:
    ids = _ids(PERCENTILE_MIN_USERS)
    totals = {u: Decimal("0.20") + Decimal(n) / 100 for n, u in enumerate(ids)}  # 0.20..0.39
    totals[uuid.UUID(int=999)] = Decimal(0)  # no spend: not counted
    alert = build_alert(DAY, totals, CAPS)
    assert alert.users == 20 and alert.p95 == Decimal("0.38")
    assert alert.above_p95 == [(ids[-1], Decimal("0.39"))]
    assert (alert.soft, alert.hard) == ([], [])


def test_equal_spend_puts_nobody_above_the_percentile() -> None:
    alert = build_alert(DAY, dict.fromkeys(_ids(30), Decimal("0.3")), CAPS)
    assert alert.p95 == Decimal("0.3") and alert.above_p95 == [] and alert.empty


def test_cap_sections_split_soft_from_hard() -> None:
    soft, hard, harder, calm = _ids(4)
    totals = {soft: Decimal("1.0"), hard: Decimal("3.0"), harder: Decimal("7"), calm: Decimal(0.5)}
    alert = build_alert(DAY, totals, CAPS)
    assert alert.soft == [(soft, Decimal("1.0"))]
    assert alert.hard == [(harder, Decimal("7")), (hard, Decimal("3.0"))]  # biggest first
    text = render_alert(alert, CAPS, "en")
    assert text.splitlines()[0] == "📊 LLM spend on 2026-10-30 (UTC): 4 users, $11.50 in total."
    assert f"Reached the hard cap ($3.00, drafting paused):\n• {harder} — $7.00" in text
    assert f"• {soft} — $1.00" in text and str(calm) not in text
    russian = render_alert(alert, CAPS, "ru")
    assert "4 пользователя" in russian and "жёсткого лимита ($3.00" in russian


def test_caps_off_report_no_cap_hits() -> None:
    alert = build_alert(DAY, {uuid.UUID(int=1): Decimal(100)}, BudgetCaps(0, 0))
    assert alert.empty
