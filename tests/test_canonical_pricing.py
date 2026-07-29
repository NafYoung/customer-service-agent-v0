from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from evals import canonical_pricing
from evals.canonical_pricing import (
    CANONICAL_PRICE_SNAPSHOT_PATH,
    EXPECTED_CANONICAL_PRICE_FILE_SHA256,
    CanonicalPricingError,
    canonical_budget_price_payload,
    canonical_price_file_sha256,
    load_canonical_price_snapshot,
    require_canonical_paid_budget,
)


def _require(price: dict, *, hard: str = "20", execution: str = "18"):
    return require_canonical_paid_budget(
        price=price,
        expected_model="deepseek-v4-flash",
        run_hard_limit_cny=hard,
        run_execution_limit_cny=execution,
        cumulative_hard_limit_cny=hard,
        cumulative_execution_limit_cny=execution,
    )


def test_canonical_price_helpers_bind_exact_file_and_snapshot_identity() -> None:
    snapshot = load_canonical_price_snapshot()

    assert canonical_price_file_sha256() == hashlib.sha256(
        CANONICAL_PRICE_SNAPSHOT_PATH.read_bytes()
    ).hexdigest()
    assert (
        canonical_price_file_sha256()
        == EXPECTED_CANONICAL_PRICE_FILE_SHA256
    )
    assert canonical_budget_price_payload()["snapshot_sha256"] == (
        snapshot.sha256
    )
    assert _require(canonical_budget_price_payload()) == snapshot


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("model", "forged-model"),
        ("snapshot_sha256", "0" * 64),
        ("source_url", "https://api-docs.deepseek.com/forged"),
        (
            "usage_source_url",
            "https://api-docs.deepseek.com/forged-usage",
        ),
        ("captured_at", "2026-07-29T08:58:59Z"),
        ("valid_until", "2026-07-30T08:58:59Z"),
        ("tokens_per_price_unit", 1),
    ],
)
def test_canonical_price_helper_rejects_identity_field_drift(
    field: str,
    replacement: object,
) -> None:
    forged = canonical_budget_price_payload()
    forged[field] = replacement

    with pytest.raises(CanonicalPricingError, match="canonical pricing"):
        _require(forged)


def test_canonical_price_helper_rejects_rate_and_limit_drift() -> None:
    forged = deepcopy(canonical_budget_price_payload())
    forged["rates_cny"]["completion"] = "0"

    with pytest.raises(CanonicalPricingError, match="canonical pricing"):
        _require(forged)
    with pytest.raises(CanonicalPricingError, match="20 CNY|18 CNY"):
        _require(
            canonical_budget_price_payload(),
            hard="5",
            execution="5",
        )
    with pytest.raises(CanonicalPricingError, match="finite"):
        require_canonical_paid_budget(
            price=canonical_budget_price_payload(),
            expected_model="deepseek-v4-flash",
            run_hard_limit_cny=Decimal("NaN"),
            run_execution_limit_cny="18",
            cumulative_hard_limit_cny="20",
            cumulative_execution_limit_cny="18",
        )


def test_frozen_canonical_price_rejects_a_valid_low_rate_file_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    forged_payload = json.loads(
        CANONICAL_PRICE_SNAPSHOT_PATH.read_text(encoding="utf-8")
    )
    forged_payload["rates_cny"] = {
        "prompt_cache_hit": "0.01",
        "prompt_cache_miss": "0.01",
        "completion": "0.01",
    }
    forged_path = tmp_path / "valid-but-forged-price.json"
    forged_path.write_text(
        json.dumps(forged_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        canonical_pricing,
        "CANONICAL_PRICE_SNAPSHOT_PATH",
        forged_path,
    )

    with pytest.raises(
        CanonicalPricingError,
        match="identity|inconsistent|canonical",
    ):
        canonical_pricing.freeze_canonical_price_snapshot()
