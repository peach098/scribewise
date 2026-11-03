"""cost_usd from token usage (T013)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from scribe.llm.pricing import (
    ModelPrice,
    OffPeak,
    PriceTable,
    PricingError,
    cost_usd,
    load_price_table,
)
from scribe.llm.provider import Usage

PRICES = PriceTable(
    models={
        "opus": ModelPrice(
            input_per_m=4.0, output_per_m=20.0, cached_input_per_m=0.2, cache_write_input_per_m=5.0
        ),
        "plain": ModelPrice(input_per_m=2.0, output_per_m=10.0, cached_input_per_m=0.2),
    }
)


def test_cost_combines_all_token_kinds() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cached_input_tokens=500_000,
        cache_write_input_tokens=200_000,
    )
    # 4 + 2 + 0.1 + 1.0
    assert cost_usd(PRICES, "opus", usage) == pytest.approx(7.1)


def test_cache_write_defaults_to_125_percent_of_input() -> None:
    usage = Usage(cache_write_input_tokens=1_000_000)
    assert PRICES.cost_usd("plain", usage) == pytest.approx(2.5)


def test_batch_is_half_price() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert PRICES.cost_usd("plain", usage, batch=True) == pytest.approx(6.0)


def test_typical_draft_cost_is_cents() -> None:
    usage = Usage(input_tokens=2_000, output_tokens=600, cached_input_tokens=1_500)
    assert 0.0 < PRICES.cost_usd("opus", usage) < 0.05


def test_unknown_model_raises() -> None:
    with pytest.raises(PricingError):
        PRICES.cost_usd("unknown", Usage())


def test_usage_addition() -> None:
    total = Usage(input_tokens=1, output_tokens=2, latency_ms=3) + Usage(
        input_tokens=10, cached_input_tokens=5, latency_ms=4
    )
    assert total == Usage(input_tokens=11, output_tokens=2, cached_input_tokens=5, latency_ms=7)


def test_shipped_deepseek_prices() -> None:
    """DeepSeek list prices (peak rates): cache hits bill at the hit rate, no write surcharge."""
    table = load_price_table(Path(__file__).resolve().parents[2] / "config" / "llm_prices.yml")
    usage = Usage(input_tokens=232_000, cached_input_tokens=768_000, output_tokens=50_000)
    # 0.232 * 0.30 + 0.768 * 0.006 + 0.05 * 1.20
    assert table.cost_usd("deepseek-flash", usage) == pytest.approx(0.134208)
    # 0.232 * 1.32 + 0.768 * 0.044 + 0.05 * 3.96
    assert table.cost_usd("deepseek-v4-pro", usage) == pytest.approx(0.538032)


# --- issue #67: 1-hour cache writes, DeepSeek off-peak -------------------------------------------

SHIPPED = Path(__file__).resolve().parents[2] / "config" / "llm_prices.yml"
OFF_PEAK = OffPeak(multiplier=0.5, peak_weekdays=[0, 1, 2, 3, 4], peak_hours_utc=[(1, 4), (6, 10)])
TIMED = PriceTable(
    models={
        "timed": ModelPrice(
            input_per_m=1.0, output_per_m=2.0, cached_input_per_m=0.1, off_peak=OFF_PEAK
        ),
    }
)
MILLION_IN = Usage(input_tokens=1_000_000)
MONDAY = datetime(2026, 10, 26, tzinfo=UTC)  # a Monday


def test_one_hour_cache_write_defaults_to_twice_input() -> None:
    usage = Usage(cache_write_1h_input_tokens=1_000_000)
    assert PRICES.cost_usd("plain", usage) == pytest.approx(4.0)
    assert PRICES.cost_usd("opus", usage) == pytest.approx(8.0)


def test_five_minute_and_one_hour_writes_are_priced_separately() -> None:
    usage = Usage(cache_write_input_tokens=1_000_000, cache_write_1h_input_tokens=1_000_000)
    # opus: 5.0 (explicit 5-minute price) + 2 x 4.0
    assert PRICES.cost_usd("opus", usage) == pytest.approx(13.0)


def test_one_hour_cache_write_price_can_be_set() -> None:
    table = PriceTable(
        models={
            "m": ModelPrice(
                input_per_m=1.0,
                output_per_m=1.0,
                cached_input_per_m=0.1,
                cache_write_1h_input_per_m=3.0,
            )
        }
    )
    assert table.cost_usd("m", Usage(cache_write_1h_input_tokens=1_000_000)) == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("at", "peak"),
    [
        (MONDAY.replace(hour=0, minute=59, second=59), False),
        (MONDAY.replace(hour=1), True),  # window start is inclusive
        (MONDAY.replace(hour=3, minute=59, second=59), True),
        (MONDAY.replace(hour=4), False),  # window end is exclusive
        (MONDAY.replace(hour=5, minute=30), False),  # the gap between the two windows
        (MONDAY.replace(hour=6), True),
        (MONDAY.replace(hour=9, minute=59), True),
        (MONDAY.replace(hour=10), False),
        (MONDAY.replace(hour=15), False),
        (MONDAY.replace(day=30, hour=8), True),  # Friday
        (MONDAY.replace(day=31, hour=8), False),  # Saturday
        (MONDAY.replace(day=25, hour=2), False),  # Sunday
    ],
)
def test_peak_windows(at: datetime, peak: bool) -> None:
    assert OFF_PEAK.is_peak(at) is peak
    expected = 1.0 if peak else 0.5
    assert TIMED.cost_usd("timed", MILLION_IN, at=at) == pytest.approx(expected)


def test_peak_window_is_evaluated_in_utc() -> None:
    moscow = timezone(timedelta(hours=3))
    # 12:30 in Moscow is 09:30 UTC on a Monday: peak.
    assert OFF_PEAK.is_peak(datetime(2026, 10, 26, 12, 30, tzinfo=moscow))
    # 13:30 in Moscow is 10:30 UTC: off-peak.
    assert not OFF_PEAK.is_peak(datetime(2026, 10, 26, 13, 30, tzinfo=moscow))
    # 02:00 Monday in Moscow is 23:00 Sunday UTC: off-peak although Monday locally.
    assert not OFF_PEAK.is_peak(datetime(2026, 10, 26, 2, 0, tzinfo=moscow))


def test_no_call_time_means_list_price() -> None:
    assert TIMED.cost_usd("timed", MILLION_IN) == pytest.approx(1.0)


def test_models_without_off_peak_ignore_the_time() -> None:
    saturday = MONDAY.replace(day=31, hour=12)
    assert PRICES.cost_usd("plain", MILLION_IN, at=saturday) == pytest.approx(2.0)


def test_off_peak_stacks_with_batch() -> None:
    saturday = MONDAY.replace(day=31, hour=12)
    assert TIMED.cost_usd("timed", MILLION_IN, batch=True, at=saturday) == pytest.approx(0.25)


@pytest.mark.parametrize(
    "off_peak",
    [
        {"multiplier": 0, "peak_weekdays": [0], "peak_hours_utc": [[1, 4]]},
        {"multiplier": 0.5, "peak_weekdays": [7], "peak_hours_utc": [[1, 4]]},
        {"multiplier": 0.5, "peak_weekdays": [0], "peak_hours_utc": [[4, 4]]},
        {"multiplier": 0.5, "peak_weekdays": [0], "peak_hours_utc": [[20, 25]]},
        {"multiplier": 0.5, "peak_weekdays": [], "peak_hours_utc": [[1, 4]]},
    ],
)
def test_invalid_off_peak_is_rejected(tmp_path: Path, off_peak: dict[str, object]) -> None:
    path = tmp_path / "prices.yml"
    model = {"input_per_m": 1, "output_per_m": 1, "cached_input_per_m": 0, "off_peak": off_peak}
    path.write_text(yaml.safe_dump({"models": {"m": model}}), encoding="utf-8")
    with pytest.raises(PricingError):
        load_price_table(path)


def test_shipped_deepseek_off_peak_prices() -> None:
    """research.md §3: off-peak is half of every DeepSeek rate (hit, miss, output)."""
    table = load_price_table(SHIPPED)
    usage = Usage(input_tokens=232_000, cached_input_tokens=768_000, output_tokens=50_000)
    peak, off_peak = MONDAY.replace(hour=7), MONDAY.replace(hour=12)
    assert table.cost_usd("deepseek-flash", usage, at=peak) == pytest.approx(0.134208)
    assert table.cost_usd("deepseek-flash", usage, at=off_peak) == pytest.approx(0.067104)
    assert table.cost_usd("deepseek-v4-pro", usage, at=off_peak) == pytest.approx(0.269016)
    # Anthropic has no time-of-day pricing.
    sonnet = Usage(input_tokens=1_000_000)
    assert table.cost_usd("claude-sonnet-5-5", sonnet, at=off_peak) == pytest.approx(2.0)
