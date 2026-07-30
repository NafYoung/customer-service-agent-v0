"""Helpers that materialize temporary ledgers matching synthetic paid payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from app.agent.deepseek_budget import (
    DeepSeekBudgetGuard,
    SQLiteBudgetLedger,
    load_price_snapshot,
    logical_call_sha256,
)
from evals import paid_ledger_binding as paid_ledger_binding_module
from evals.canonical_pricing import CANONICAL_PRICE_SNAPSHOT_PATH


def install_matching_ledger_for_paid_payload(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: Mapping[str, Any],
    modules: Sequence[object] = (),
) -> Path:
    """Create a private ledger whose run evidence matches a paid bundle budget."""

    budget = payload["summary"]["budget"]
    identity = budget["run_identity"]
    fixed_now = datetime.fromisoformat(identity["started_at"])
    if fixed_now.tzinfo is None:
        fixed_now = fixed_now.replace(tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(
        "app.agent.deepseek_budget.datetime",
        _FixedDateTime,
    )
    ledger_path = tmp_path / "trusted-private-paid" / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(
        path=ledger_path,
        hard_limit_cny=Decimal(str(budget["run"]["hard_limit_cny"])),
        execution_limit_cny=Decimal(
            str(budget["run"]["execution_limit_cny"])
        ),
    )
    price_snapshot = load_price_snapshot(CANONICAL_PRICE_SNAPSHOT_PATH)
    guard = DeepSeekBudgetGuard(
        ledger=ledger,
        run_id=identity["run_id"],
        purpose=identity["purpose"],
        price_snapshot=price_snapshot,
        model=identity["model"],
        max_output_tokens=1024,
        now=fixed_now,
        now_provider=lambda: fixed_now,
    )
    for record in payload["cases"]:
        for call in record["model_calls"]:
            phase = call["phase"]
            logical_call_id = (
                f"{record['case_id']}:{record['trial']}:{phase}"
            )
            assert (
                logical_call_sha256(logical_call_id)
                == call["logical_call_sha256"]
            )
            reservation = guard.reserve_attempt(
                logical_call_id=logical_call_id,
                attempt_number=1,
            )
            digest = call.get("response_content_sha256")
            guard.settle_attempt(
                reservation=reservation,
                usage=call["usage"],
                provider_request_id=None,
                response_content_sha256=digest,
            )
    guard.close()
    snapshot = guard.snapshot()
    # Keep the summary budget identical to the trusted ledger export.
    payload["summary"]["budget"] = snapshot
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "DEFAULT_BUDGET_LEDGER",
        ledger_path,
        raising=False,
    )
    for module in modules:
        if hasattr(module, "DEFAULT_BUDGET_LEDGER"):
            monkeypatch.setattr(
                module,
                "DEFAULT_BUDGET_LEDGER",
                ledger_path,
                raising=False,
            )
    return ledger_path
