from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.budget.errors import (
    BudgetInvariantError,
    BudgetPriceWindowError,
    BudgetUsageError,
)

CNY_UNITS_PER_CNY = 100_000_000
PRICE_WINDOW_SAFETY_MARGIN_SECONDS = 2.0

class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceRates(_StrictModel):
    prompt_cache_hit: str
    prompt_cache_miss: str
    completion: str

    @field_validator("*")
    @classmethod
    def validate_decimal_rate(cls, value: str) -> str:
        amount = _parse_decimal(value, field_name="price rate")
        if amount < 0:
            raise ValueError("Price rates cannot be negative")
        cny_to_units(amount / Decimal(1_000_000))
        return value


class ModelLimits(_StrictModel):
    context_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)


class DeepSeekPriceSnapshot(_StrictModel):
    schema_version: Literal["1.0"]
    provider: Literal["deepseek"]
    model: str
    currency: Literal["CNY"]
    tokens_per_price_unit: Literal[1_000_000]
    rates_cny: PriceRates
    limits: ModelLimits
    source_url: str
    usage_source_url: str
    captured_at: datetime
    valid_until: datetime

    @field_validator("source_url", "usage_source_url")
    @classmethod
    def require_official_https_url(cls, value: str) -> str:
        if not value.startswith("https://api-docs.deepseek.com/"):
            raise ValueError("Pricing sources must be official DeepSeek HTTPS URLs")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> DeepSeekPriceSnapshot:
        if self.captured_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("Pricing timestamps must be timezone-aware")
        if self.valid_until <= self.captured_at:
            raise ValueError("Pricing valid_until must follow captured_at")
        return self

    @property
    def sha256(self) -> str:
        payload = self.model_dump(mode="json")
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def require_current(
        self,
        *,
        expected_model: str,
        now: datetime | None = None,
        minimum_remaining: timedelta = timedelta(0),
    ) -> None:
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            raise BudgetInvariantError("Pricing check time must be timezone-aware")
        if minimum_remaining < timedelta(0):
            raise BudgetInvariantError(
                "Minimum pricing validity window cannot be negative."
            )
        if self.model != expected_model:
            raise BudgetInvariantError(
                "Pricing snapshot model does not match the configured model."
            )
        if checked_at < self.captured_at:
            raise BudgetInvariantError(
                "Pricing snapshot is not active yet."
            )
        if checked_at >= self.valid_until:
            raise BudgetPriceWindowError(
                "Pricing snapshot has expired; refresh it before paid calls."
            )
        if checked_at + minimum_remaining >= self.valid_until:
            raise BudgetPriceWindowError(
                "Pricing snapshot validity is too short for a paid request."
            )


@dataclass(frozen=True)
class UsageCost:
    units: int
    cny: Decimal
    mode: Literal["exact", "upper_bound", "reservation"]



def _parse_decimal(value: str | Decimal, *, field_name: str) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not amount.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return amount


def cny_to_units(amount: Decimal) -> int:
    if amount < 0:
        raise ValueError("CNY amount cannot be negative")
    scaled = amount * CNY_UNITS_PER_CNY
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError(
            "CNY amount is more precise than the budget ledger supports"
        )
    return int(integral)


def units_to_cny(units: int) -> Decimal:
    if units < 0:
        raise ValueError("Budget units cannot be negative")
    return Decimal(units) / CNY_UNITS_PER_CNY


def format_cny(units: int) -> str:
    amount = units_to_cny(units)
    normalized = amount.normalize()
    return format(normalized, "f")



def _usage_token(
    usage: Mapping[str, Any],
    field: str,
    *,
    required: bool = True,
) -> int | None:
    value = usage.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BudgetUsageError(
            f"Provider usage field {field} must be a non-negative integer."
        )
    return value


def _rate(snapshot: DeepSeekPriceSnapshot, field: str) -> Decimal:
    value = getattr(snapshot.rates_cny, field)
    return _parse_decimal(value, field_name=field)


def calculate_usage_cost(
    snapshot: DeepSeekPriceSnapshot,
    usage: Mapping[str, Any],
) -> UsageCost:
    return calculate_usage_cost_from_rates(
        rates_cny=snapshot.rates_cny.model_dump(),
        tokens_per_price_unit=snapshot.tokens_per_price_unit,
        usage=usage,
    )


def calculate_usage_cost_from_rates(
    *,
    rates_cny: Mapping[str, Any],
    tokens_per_price_unit: int,
    usage: Mapping[str, Any],
) -> UsageCost:
    """Recompute one recorded provider call without trusting a ledger total."""

    if (
        isinstance(tokens_per_price_unit, bool)
        or not isinstance(tokens_per_price_unit, int)
        or tokens_per_price_unit < 1
    ):
        raise BudgetUsageError(
            "Pricing token unit must be a positive integer."
        )
    required_rates = (
        "prompt_cache_hit",
        "prompt_cache_miss",
        "completion",
    )
    if set(rates_cny) != set(required_rates):
        raise BudgetUsageError(
            "Pricing rates must contain the exact required fields."
        )
    try:
        parsed_rates = {
            field: _parse_decimal(
                rates_cny[field],
                field_name=field,
            )
            for field in required_rates
        }
    except ValueError as exc:
        raise BudgetUsageError(
            "Pricing rates contain an invalid decimal."
        ) from exc
    if any(rate < 0 for rate in parsed_rates.values()):
        raise BudgetUsageError("Pricing rates cannot be negative.")

    prompt_tokens = _usage_token(usage, "prompt_tokens")
    completion_tokens = _usage_token(usage, "completion_tokens")
    total_tokens = _usage_token(usage, "total_tokens")
    assert prompt_tokens is not None
    assert completion_tokens is not None
    assert total_tokens is not None
    if total_tokens != prompt_tokens + completion_tokens:
        raise BudgetUsageError(
            "Provider usage total does not equal prompt plus completion tokens."
        )

    cache_hit = _usage_token(
        usage,
        "prompt_cache_hit_tokens",
        required=False,
    )
    cache_miss = _usage_token(
        usage,
        "prompt_cache_miss_tokens",
        required=False,
    )
    if (cache_hit is None) != (cache_miss is None):
        raise BudgetUsageError(
            "Provider usage must include both cache token fields or neither."
        )

    if cache_hit is None:
        cache_hit = 0
        cache_miss = prompt_tokens
        mode: Literal["exact", "upper_bound"] = "upper_bound"
    else:
        assert cache_miss is not None
        if cache_hit + cache_miss != prompt_tokens:
            raise BudgetUsageError(
                "Provider cache token fields do not equal prompt tokens."
            )
        mode = "exact"

    cost_cny = (
        Decimal(cache_hit) * parsed_rates["prompt_cache_hit"]
        + Decimal(cache_miss) * parsed_rates["prompt_cache_miss"]
        + Decimal(completion_tokens) * parsed_rates["completion"]
    ) / tokens_per_price_unit
    return UsageCost(
        units=cny_to_units(cost_cny),
        cny=cost_cny,
        mode=mode,
    )


def worst_case_attempt_cost(
    snapshot: DeepSeekPriceSnapshot,
    *,
    max_output_tokens: int,
) -> UsageCost:
    if not 1 <= max_output_tokens <= snapshot.limits.max_output_tokens:
        raise BudgetInvariantError(
            "Configured max output tokens exceed the pricing snapshot limit."
        )
    cost_cny = (
        Decimal(snapshot.limits.context_tokens)
        * _rate(snapshot, "prompt_cache_miss")
        + Decimal(max_output_tokens) * _rate(snapshot, "completion")
    ) / snapshot.tokens_per_price_unit
    return UsageCost(
        units=cny_to_units(cost_cny),
        cny=cost_cny,
        mode="reservation",
    )


def load_price_snapshot(path: Path) -> DeepSeekPriceSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BudgetInvariantError(
            "Unable to load the versioned DeepSeek pricing snapshot."
        ) from exc
    try:
        return DeepSeekPriceSnapshot.model_validate(payload)
    except ValueError as exc:
        raise BudgetInvariantError(
            "The versioned DeepSeek pricing snapshot is invalid."
        ) from exc

