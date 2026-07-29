from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ValidationError

from app.agent.deepseek_budget import (
    BudgetInvariantError,
    DeepSeekPriceSnapshot,
    format_cny,
    worst_case_attempt_cost,
)
from evals.file_snapshot import (
    FileSnapshot,
    FileSnapshotError,
    read_file_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PRICE_SNAPSHOT_PATH = (
    ROOT / "pricing" / "deepseek-v4-flash-2026-07-29.json"
)
EXPECTED_CANONICAL_PRICE_FILE_SHA256 = (
    "d65a5a7c107632afa9f48b2dbc77cacc6dc819e34a0343d2bc62bb5f5443fa35"
)
FORMAL_HARD_LIMIT_CNY = Decimal("20")
FORMAL_EXECUTION_LIMIT_CNY = Decimal("18")


class CanonicalPricingError(RuntimeError):
    """Paid formal evidence does not match the repository price contract."""


@dataclass(frozen=True)
class FrozenCanonicalPrice:
    """One safe file read shared by fingerprints and the paid guard."""

    file_snapshot: FileSnapshot
    price_snapshot: DeepSeekPriceSnapshot


def freeze_canonical_price_snapshot() -> FrozenCanonicalPrice:
    """Read and parse canonical pricing from the exact same immutable bytes."""

    try:
        file_snapshot = read_file_snapshot(
            CANONICAL_PRICE_SNAPSHOT_PATH
        )
        price_snapshot = DeepSeekPriceSnapshot.model_validate_json(
            file_snapshot.raw
        )
    except (FileSnapshotError, ValidationError, ValueError) as exc:
        raise CanonicalPricingError(
            "The canonical pricing snapshot is unavailable or invalid."
        ) from exc
    frozen = FrozenCanonicalPrice(
        file_snapshot=file_snapshot,
        price_snapshot=price_snapshot,
    )
    require_frozen_canonical_price(frozen)
    return frozen


def require_frozen_canonical_price(
    frozen: FrozenCanonicalPrice,
    *,
    expected_file_sha256: str | None = None,
) -> DeepSeekPriceSnapshot:
    """Reject any split identity before a paid ledger or request can start."""

    actual_file_sha256 = hashlib.sha256(
        frozen.file_snapshot.raw
    ).hexdigest()
    try:
        reparsed = DeepSeekPriceSnapshot.model_validate_json(
            frozen.file_snapshot.raw
        )
    except (ValidationError, ValueError) as exc:
        raise CanonicalPricingError(
            "The frozen canonical pricing bytes are invalid."
        ) from exc
    if (
        actual_file_sha256
        != EXPECTED_CANONICAL_PRICE_FILE_SHA256
        or frozen.file_snapshot.sha256 != actual_file_sha256
        or (
            expected_file_sha256 is not None
            and actual_file_sha256 != expected_file_sha256
        )
        or reparsed.model_dump(mode="json")
        != frozen.price_snapshot.model_dump(mode="json")
    ):
        raise CanonicalPricingError(
            "The frozen canonical pricing identity is inconsistent."
        )
    return frozen.price_snapshot


def load_canonical_price_snapshot() -> DeepSeekPriceSnapshot:
    """Load the repository-owned price snapshot without trusting an artifact."""

    return freeze_canonical_price_snapshot().price_snapshot


def canonical_price_file_sha256() -> str:
    """Hash the exact repository bytes for frozen-harness binding."""

    return freeze_canonical_price_snapshot().file_snapshot.sha256


def canonical_budget_price_payload(
    snapshot: DeepSeekPriceSnapshot | None = None,
) -> dict[str, Any]:
    """Return the only price summary accepted for paid formal evidence."""

    canonical = snapshot or load_canonical_price_snapshot()
    payload = canonical.model_dump(mode="json")
    return {
        "provider": payload["provider"],
        "model": payload["model"],
        "currency": payload["currency"],
        "snapshot_sha256": canonical.sha256,
        "source_url": payload["source_url"],
        "usage_source_url": payload["usage_source_url"],
        "captured_at": payload["captured_at"],
        "valid_until": payload["valid_until"],
        "rates_cny": payload["rates_cny"],
        "tokens_per_price_unit": payload["tokens_per_price_unit"],
    }


def canonical_worst_case_attempt_reservation_cny(
    *,
    canonical_price: DeepSeekPriceSnapshot,
    max_output_tokens: int,
) -> str:
    """Compute the exact reservation bound from canonical price and tokens."""

    try:
        reservation = worst_case_attempt_cost(
            canonical_price,
            max_output_tokens=max_output_tokens,
        )
    except (BudgetInvariantError, ValueError) as exc:
        raise CanonicalPricingError(
            "The formal max_tokens cannot be priced canonically."
        ) from exc
    return format_cny(reservation.units)


def require_canonical_attempt_reservation(
    *,
    canonical_price: DeepSeekPriceSnapshot,
    max_output_tokens: int,
    reservation_cny_per_attempt: str,
) -> None:
    """Reject evidence that understates the paid guard's exact reservation."""

    expected = canonical_worst_case_attempt_reservation_cny(
        canonical_price=canonical_price,
        max_output_tokens=max_output_tokens,
    )
    if reservation_cny_per_attempt != expected:
        raise CanonicalPricingError(
            "Paid formal evidence reservation does not match canonical "
            "pricing and max_tokens."
        )


def _as_payload(
    price: Mapping[str, Any] | BaseModel,
) -> dict[str, Any]:
    if isinstance(price, BaseModel):
        return price.model_dump(mode="json")
    return dict(price)


def _as_decimal(value: str | Decimal, *, label: str) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CanonicalPricingError(
            f"The formal {label} is not a valid CNY amount."
        ) from exc
    if not amount.is_finite():
        raise CanonicalPricingError(
            f"The formal {label} is not a finite CNY amount."
        )
    return amount


def require_canonical_paid_budget(
    *,
    price: Mapping[str, Any] | BaseModel,
    expected_model: str,
    run_hard_limit_cny: str | Decimal,
    run_execution_limit_cny: str | Decimal,
    cumulative_hard_limit_cny: str | Decimal,
    cumulative_execution_limit_cny: str | Decimal,
) -> DeepSeekPriceSnapshot:
    """Bind formal paid evidence to canonical pricing and exact limits."""

    canonical = load_canonical_price_snapshot()
    if (
        expected_model != canonical.model
        or _as_payload(price)
        != canonical_budget_price_payload(canonical)
    ):
        raise CanonicalPricingError(
            "Paid formal evidence does not match canonical pricing."
        )
    limits = (
        (
            _as_decimal(
                run_hard_limit_cny,
                label="run hard limit",
            ),
            FORMAL_HARD_LIMIT_CNY,
        ),
        (
            _as_decimal(
                run_execution_limit_cny,
                label="run execution limit",
            ),
            FORMAL_EXECUTION_LIMIT_CNY,
        ),
        (
            _as_decimal(
                cumulative_hard_limit_cny,
                label="cumulative hard limit",
            ),
            FORMAL_HARD_LIMIT_CNY,
        ),
        (
            _as_decimal(
                cumulative_execution_limit_cny,
                label="cumulative execution limit",
            ),
            FORMAL_EXECUTION_LIMIT_CNY,
        ),
    )
    if any(actual != required for actual, required in limits):
        raise CanonicalPricingError(
            "Paid formal evidence must use the 20 CNY hard limit "
            "and 18 CNY execution limit."
        )
    return canonical
