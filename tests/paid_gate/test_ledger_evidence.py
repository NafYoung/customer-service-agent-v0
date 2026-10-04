from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from evals import paid_ledger_binding as paid_ledger_binding_module
from evals.evidence_schema import (
    BudgetSummary,
    ModelCallRecord,
)
from tests.paid_gate.helpers import (
    _REAL_PAID_LEDGER_REQUIRE,
    _budget_with_attempt_evidence,
    _dev_repeat_payload,
    _model_call,
)
from tests.paid_ledger_testutil import install_matching_ledger_for_paid_payload


@pytest.mark.parametrize(
    "attack",
    [
        "offline",
        "totals",
        "reservation",
        "run_not_in_cumulative",
        "duplicate_run_not_in_cumulative",
    ],
)
def test_budget_summary_rejects_contradictory_attempt_evidence(
    attack: str,
) -> None:
    budget = _budget_with_attempt_evidence()
    if attack == "offline":
        budget = {
            "schema_version": "1.0",
            "enforcement_mode": "offline_no_paid_provider",
            "run_status": "completed",
            "price": None,
            "reservation_cny_per_attempt": "0",
            "run": {
                "currency": "CNY",
                "hard_limit_cny": "20",
                "execution_limit_cny": "18",
                "committed_cny": "0",
                "settled_cny": "0",
                "remaining_execution_cny": "18",
                "attempt_count": 0,
                "reserved_count": 0,
                "uncertain_count": 0,
            },
            "cumulative": {
                "currency": "CNY",
                "hard_limit_cny": "20",
                "execution_limit_cny": "18",
                "committed_cny": "0",
                "settled_cny": "0",
                "remaining_execution_cny": "18",
                "attempt_count": 0,
                "reserved_count": 0,
                "uncertain_count": 0,
            },
            "attempt_evidence": {"run": [], "cumulative": []},
        }
    elif attack == "totals":
        budget["attempt_evidence"]["run"][0]["count"] = 2
    elif attack == "reservation":
        budget["attempt_evidence"]["run"][0]["reserved_cny"] = "1.5"
    elif attack == "run_not_in_cumulative":
        budget["attempt_evidence"]["cumulative"][0]["known_cost_cny"] = "0.000013"
        budget["cumulative"]["committed_cny"] = "0.000013"
        budget["cumulative"]["settled_cny"] = "0.000013"
        budget["cumulative"]["remaining_execution_cny"] = "17.999987"
        budget["run"]["remaining_execution_cny"] = "17.999987"
    elif attack == "duplicate_run_not_in_cumulative":
        canonical_bucket = deepcopy(budget["attempt_evidence"]["run"][0])
        canonical_bucket["count"] = 1
        historical_bucket = deepcopy(canonical_bucket)
        historical_bucket["reserved_cny"] = "1.5"
        budget["attempt_evidence"]["run"] = [
            deepcopy(canonical_bucket),
            deepcopy(canonical_bucket),
        ]
        budget["attempt_evidence"]["cumulative"] = [
            deepcopy(canonical_bucket),
            historical_bucket,
        ]
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "0.000024"
            budget[scope]["settled_cny"] = "0.000024"
            budget[scope]["remaining_execution_cny"] = "17.999976"
            budget[scope]["attempt_count"] = 2

    with pytest.raises(ValueError, match="attempt|bucket|offline|budget"):
        BudgetSummary.model_validate(deepcopy(budget))

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_attempts", True),
        ("provider_attempts", "1"),
        ("usage.prompt_tokens", "8"),
        ("usage.completion_tokens", False),
    ],
)
def test_model_call_evidence_rejects_coerced_usage_and_attempt_types(
    field: str,
    value: object,
) -> None:
    payload = asdict(
        _model_call(
            case_id="strict-paid-evidence",
            trial=1,
        )
    )
    if field == "provider_attempts":
        payload[field] = value
    else:
        _, usage_field = field.split(".", maxsplit=1)
        payload["usage"][usage_field] = value

    with pytest.raises(ValueError, match="usage|provider|attempt|integer"):
        ModelCallRecord.model_validate(payload)

def test_dev_repeat_live_ledger_accepts_matching_temporary_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    install_matching_ledger_for_paid_payload(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        payload=payload,
    )
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "require_persistent_budget_matches_trusted_ledger",
        _REAL_PAID_LEDGER_REQUIRE,
    )

    _REAL_PAID_LEDGER_REQUIRE(
        budget=BudgetSummary.model_validate(
            payload["summary"]["budget"]
        ),
        label="dev_repeat",
    )

def test_dev_repeat_live_ledger_rejects_missing_trusted_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    missing = tmp_path / "missing-private" / "budget.sqlite3"
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "DEFAULT_BUDGET_LEDGER",
        missing,
        raising=False,
    )
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "require_persistent_budget_matches_trusted_ledger",
        _REAL_PAID_LEDGER_REQUIRE,
    )

    with pytest.raises(ValueError, match="ledger|trusted|persistent"):
        _REAL_PAID_LEDGER_REQUIRE(
            budget=BudgetSummary.model_validate(
                payload["summary"]["budget"]
            ),
            label="dev_repeat",
        )

def test_dev_repeat_live_ledger_rejects_tampered_settled_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    install_matching_ledger_for_paid_payload(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        payload=payload,
    )
    for scope in ("run", "cumulative"):
        payload["summary"]["budget"][scope]["settled_cny"] = "0.000001"
        payload["summary"]["budget"][scope]["committed_cny"] = "0.000001"
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "require_persistent_budget_matches_trusted_ledger",
        _REAL_PAID_LEDGER_REQUIRE,
    )

    with pytest.raises(ValueError, match="ledger|trusted|persistent|budget"):
        _REAL_PAID_LEDGER_REQUIRE(
            budget=BudgetSummary.model_validate(
                payload["summary"]["budget"]
            ),
            label="dev_repeat",
        )
