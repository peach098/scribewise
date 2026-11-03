"""Per-model prices (``config/llm_prices.yml``) and ``cost_usd`` from token usage.

- Prompt-cache writes: 5-minute writes at ``cache_write_input_per_m`` (default 1.25 x input),
  1-hour writes at ``cache_write_1h_input_per_m`` (default 2 x input; Anthropic).
- Message Batches are billed at 50%.
- ``off_peak`` (DeepSeek): the whole call is billed at ``multiplier`` when it starts outside the
  peak windows (``peak_weekdays`` x ``peak_hours_utc``, UTC, end hour exclusive). Without a
  call time (``at=None``) the list (peak) price applies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scribe.llm.provider import Usage

BATCH_DISCOUNT = 0.5
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache writes
CACHE_WRITE_1H_MULTIPLIER = 2.0  # 1-hour cache writes


class PricingError(ValueError):
    pass


class OffPeak(BaseModel):
    """A time-of-day discount: calls starting outside every peak window cost ``multiplier``."""

    model_config = ConfigDict(extra="forbid")

    multiplier: float = Field(gt=0, le=1)
    peak_weekdays: list[int] = Field(min_length=1)  # 0 = Monday ... 6 = Sunday
    peak_hours_utc: list[tuple[int, int]] = Field(min_length=1)  # [start, end) whole hours

    @model_validator(mode="after")
    def _check(self) -> OffPeak:
        if any(not 0 <= day <= 6 for day in self.peak_weekdays):
            raise ValueError("peak_weekdays must be 0 (Monday) .. 6 (Sunday)")
        if any(not 0 <= start < end <= 24 for start, end in self.peak_hours_utc):
            raise ValueError("peak_hours_utc must be [start, end) with 0 <= start < end <= 24")
        return self

    def is_peak(self, at: datetime) -> bool:
        """``at`` in UTC (a naive time is taken as UTC)."""
        moment = at.astimezone(UTC) if at.tzinfo is not None else at
        if moment.weekday() not in self.peak_weekdays:
            return False
        return any(start <= moment.hour < end for start, end in self.peak_hours_utc)


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_m: float = Field(ge=0)
    output_per_m: float = Field(ge=0)
    cached_input_per_m: float = Field(ge=0)
    cache_write_input_per_m: float | None = Field(default=None, ge=0)
    cache_write_1h_input_per_m: float | None = Field(default=None, ge=0)
    off_peak: OffPeak | None = None

    def multiplier_at(self, at: datetime | None) -> float:
        if self.off_peak is None or at is None or self.off_peak.is_peak(at):
            return 1.0
        return self.off_peak.multiplier


class PriceTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: dict[str, ModelPrice]

    def has(self, model: str) -> bool:
        return model in self.models

    def cost_usd(
        self, model: str, usage: Usage, *, batch: bool = False, at: datetime | None = None
    ) -> float:
        """``at``: when the call started, for time-of-day pricing (``None``: list price)."""
        price = self.models.get(model)
        if price is None:
            raise PricingError(f"no price for model {model!r}")
        cache_write = (
            price.cache_write_input_per_m
            if price.cache_write_input_per_m is not None
            else price.input_per_m * CACHE_WRITE_MULTIPLIER
        )
        cache_write_1h = (
            price.cache_write_1h_input_per_m
            if price.cache_write_1h_input_per_m is not None
            else price.input_per_m * CACHE_WRITE_1H_MULTIPLIER
        )
        total = (
            usage.input_tokens * price.input_per_m
            + usage.output_tokens * price.output_per_m
            + usage.cached_input_tokens * price.cached_input_per_m
            + usage.cache_write_input_tokens * cache_write
            + usage.cache_write_1h_input_tokens * cache_write_1h
        ) / 1_000_000
        if batch:
            total *= BATCH_DISCOUNT
        total *= price.multiplier_at(at)
        return round(total, 6)


def cost_usd(
    prices: PriceTable,
    model: str,
    usage: Usage,
    *,
    batch: bool = False,
    at: datetime | None = None,
) -> float:
    return prices.cost_usd(model, usage, batch=batch, at=at)


def load_price_table(path: Path) -> PriceTable:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return PriceTable.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PricingError(f"invalid price table {path}: {exc}") from exc
