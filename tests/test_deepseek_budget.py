from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.deepseek_budget import (
    BudgetExceededError,
    BudgetInvariantError,
    BudgetUsageError,
    DeepSeekPriceSnapshot,
    SQLiteBudgetLedger,
    calculate_usage_cost,
    cny_to_units,
    worst_case_attempt_cost,
)


def _price_snapshot() -> DeepSeekPriceSnapshot:
    return DeepSeekPriceSnapshot.model_validate(
        {
            "schema_version": "1.0",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "currency": "CNY",
            "tokens_per_price_unit": 1_000_000,
            "rates_cny": {
                "prompt_cache_hit": "0.02",
                "prompt_cache_miss": "1",
                "completion": "2",
            },
            "limits": {
                "context_tokens": 1_000_000,
                "max_output_tokens": 384_000,
            },
            "source_url": (
                "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
            ),
            "usage_source_url": (
                "https://api-docs.deepseek.com/api/create-chat-completion/"
            ),
            "captured_at": "2026-07-29T08:58:58Z",
            "valid_until": "2026-07-30T08:58:58Z",
        }
    )


def _ledger(
    tmp_path: Path,
    *,
    execution_limit_cny: str = "20",
) -> SQLiteBudgetLedger:
    return SQLiteBudgetLedger(
        path=tmp_path / "private" / "budget.sqlite3",
        hard_limit_cny=Decimal("20"),
        execution_limit_cny=Decimal(execution_limit_cny),
    )


def test_price_snapshot_is_current_only_inside_declared_window():
    snapshot = _price_snapshot()

    snapshot.require_current(
        now=datetime(2026, 7, 29, 12, tzinfo=UTC),
        expected_model="deepseek-v4-flash",
    )

    with pytest.raises(BudgetInvariantError, match="expired"):
        snapshot.require_current(
            now=datetime(2026, 7, 30, 9, tzinfo=UTC),
            expected_model="deepseek-v4-flash",
        )
    with pytest.raises(BudgetInvariantError, match="model"):
        snapshot.require_current(
            now=datetime(2026, 7, 29, 12, tzinfo=UTC),
            expected_model="different-model",
        )


def test_exact_usage_cost_uses_cache_hit_miss_and_decimal_arithmetic():
    cost = calculate_usage_cost(
        _price_snapshot(),
        {
            "prompt_tokens": 1_000_000,
            "prompt_cache_hit_tokens": 400_000,
            "prompt_cache_miss_tokens": 600_000,
            "completion_tokens": 2_000,
            "total_tokens": 1_002_000,
        },
    )

    assert cost.mode == "exact"
    assert cost.cny == Decimal("0.612")
    assert cost.units == cny_to_units(Decimal("0.612"))


def test_missing_cache_breakdown_uses_all_prompt_as_upper_bound():
    cost = calculate_usage_cost(
        _price_snapshot(),
        {
            "prompt_tokens": 1_000,
            "completion_tokens": 100,
            "total_tokens": 1_100,
        },
    )

    assert cost.mode == "upper_bound"
    assert cost.cny == Decimal("0.0012")


@pytest.mark.parametrize(
    "usage",
    [
        {
            "prompt_tokens": -1,
            "completion_tokens": 1,
            "total_tokens": 0,
        },
        {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 99,
        },
        {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 12,
        },
        {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 4,
            "prompt_cache_miss_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 12,
        },
    ],
)
def test_invalid_usage_is_rejected_instead_of_undercharged(usage):
    with pytest.raises(BudgetUsageError):
        calculate_usage_cost(_price_snapshot(), usage)


def test_worst_case_reservation_uses_independent_input_and_output_limits():
    reservation = worst_case_attempt_cost(
        _price_snapshot(),
        max_output_tokens=1024,
    )

    assert reservation.mode == "reservation"
    assert reservation.cny == Decimal("1.002048")


def test_ledger_allows_exact_limit_and_rejects_one_more_unit(tmp_path):
    ledger = _ledger(tmp_path, execution_limit_cny="1.002048")
    snapshot = _price_snapshot()
    ledger.start_run(
        run_id="eval-budget-limit-0001",
        purpose="diagnostic",
        price_snapshot=snapshot,
    )
    reservation = worst_case_attempt_cost(snapshot, max_output_tokens=1024)

    first = ledger.reserve_attempt(
        run_id="eval-budget-limit-0001",
        logical_call_id="logical-call-1",
        attempt_number=1,
        model=snapshot.model,
        reserved_units=reservation.units,
    )
    assert first.reserved_units == cny_to_units(Decimal("1.002048"))

    with pytest.raises(BudgetExceededError):
        ledger.reserve_attempt(
            run_id="eval-budget-limit-0001",
            logical_call_id="logical-call-2",
            attempt_number=1,
            model=snapshot.model,
            reserved_units=1,
        )


def test_settlement_releases_unused_reservation_and_is_idempotent(tmp_path):
    ledger = _ledger(tmp_path, execution_limit_cny="1.002048")
    snapshot = _price_snapshot()
    ledger.start_run(
        run_id="eval-budget-settle-0001",
        purpose="diagnostic",
        price_snapshot=snapshot,
    )
    reservation_cost = worst_case_attempt_cost(
        snapshot,
        max_output_tokens=1024,
    )
    reservation = ledger.reserve_attempt(
        run_id="eval-budget-settle-0001",
        logical_call_id="logical-call-1",
        attempt_number=1,
        model=snapshot.model,
        reserved_units=reservation_cost.units,
    )
    usage = {
        "prompt_tokens": 1_000,
        "completion_tokens": 100,
        "total_tokens": 1_100,
    }

    first = ledger.settle_attempt(
        reservation=reservation,
        price_snapshot=snapshot,
        usage=usage,
        provider_request_id="request-1",
    )
    second = ledger.settle_attempt(
        reservation=reservation,
        price_snapshot=snapshot,
        usage=usage,
        provider_request_id="request-1",
    )

    assert first == second
    assert first.mode == "upper_bound"
    assert ledger.snapshot()["committed_cny"] == "0.0012"

    ledger.reserve_attempt(
        run_id="eval-budget-settle-0001",
        logical_call_id="logical-call-2",
        attempt_number=1,
        model=snapshot.model,
        reserved_units=reservation_cost.units - first.units,
    )


def test_uncertain_attempt_remains_committed_across_connections(tmp_path):
    ledger_path = tmp_path / "private" / "budget.sqlite3"
    first = SQLiteBudgetLedger(
        path=ledger_path,
        hard_limit_cny=Decimal("20"),
        execution_limit_cny=Decimal("2"),
    )
    snapshot = _price_snapshot()
    first.start_run(
        run_id="eval-budget-uncertain-1",
        purpose="diagnostic",
        price_snapshot=snapshot,
    )
    reservation = first.reserve_attempt(
        run_id="eval-budget-uncertain-1",
        logical_call_id="logical-call-1",
        attempt_number=1,
        model=snapshot.model,
        reserved_units=cny_to_units(Decimal("1.5")),
    )
    first.mark_uncertain(
        reservation=reservation,
        error_code="MODEL_TRANSPORT_ERROR",
    )

    reopened = SQLiteBudgetLedger(
        path=ledger_path,
        hard_limit_cny=Decimal("20"),
        execution_limit_cny=Decimal("2"),
    )
    assert reopened.snapshot()["committed_cny"] == "1.5"
    with pytest.raises(BudgetExceededError):
        reopened.reserve_attempt(
            run_id="eval-budget-uncertain-1",
            logical_call_id="logical-call-2",
            attempt_number=1,
            model=snapshot.model,
            reserved_units=cny_to_units(Decimal("0.50000001")),
        )


def test_duplicate_reservation_is_idempotent_but_mismatch_fails(tmp_path):
    ledger = _ledger(tmp_path)
    snapshot = _price_snapshot()
    ledger.start_run(
        run_id="eval-budget-idempotent-1",
        purpose="diagnostic",
        price_snapshot=snapshot,
    )
    kwargs = {
        "run_id": "eval-budget-idempotent-1",
        "logical_call_id": "logical-call-1",
        "attempt_number": 1,
        "model": snapshot.model,
        "reserved_units": 123,
    }

    first = ledger.reserve_attempt(**kwargs)
    second = ledger.reserve_attempt(**kwargs)

    assert first == second
    with pytest.raises(BudgetInvariantError):
        ledger.reserve_attempt(**{**kwargs, "reserved_units": 124})


def test_concurrent_connections_cannot_both_take_last_budget(tmp_path):
    ledger = _ledger(tmp_path, execution_limit_cny="1")
    snapshot = _price_snapshot()
    ledger.start_run(
        run_id="eval-budget-concurrent-1",
        purpose="diagnostic",
        price_snapshot=snapshot,
    )

    def reserve(index: int) -> str:
        worker = _ledger(tmp_path, execution_limit_cny="1")
        try:
            worker.reserve_attempt(
                run_id="eval-budget-concurrent-1",
                logical_call_id=f"logical-call-{index}",
                attempt_number=1,
                model=snapshot.model,
                reserved_units=cny_to_units(Decimal("0.75")),
            )
        except BudgetExceededError:
            return "blocked"
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, (1, 2)))

    assert sorted(outcomes) == ["blocked", "reserved"]


def test_ledger_and_parent_directory_are_private(tmp_path):
    _ledger(tmp_path)
    path = tmp_path / "private" / "budget.sqlite3"

    assert path.exists()
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_snapshot_serializes_amounts_as_strings_without_float(tmp_path):
    ledger = _ledger(tmp_path)
    serialized = json.dumps(ledger.snapshot())

    assert "20" in serialized
    assert not any(
        isinstance(value, float)
        for value in ledger.snapshot().values()
    )
