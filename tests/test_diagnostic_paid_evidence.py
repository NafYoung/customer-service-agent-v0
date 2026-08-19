from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Callable

import pytest

from app.agent.deepseek_budget import (
    calculate_usage_cost_from_rates,
    cny_to_units,
    format_cny,
)
from app.config import Settings
from evals.canonical_pricing import (
    canonical_budget_price_payload,
    canonical_worst_case_attempt_reservation_cny,
    load_canonical_price_snapshot,
)
from evals.evidence import BusinessStateDelta, ModelCallEvidence
from evals.evidence_schema import (
    BudgetSummary,
    validate_readonly_payload,
)
from evals.readonly_eval import (
    DEFAULT_CASE_DIR,
    ReadonlyEvalResult,
    ScoreCheck,
    load_cases,
)
from evals.readonly_reporting import (
    build_readonly_manifest,
    offline_budget_report,
    result_to_record,
    summarize_results,
)

RUN_ID = "eval-20260729-diagnostic-evidence"
REQUESTED_MODEL = "deepseek-v4-flash"
STARTED = datetime(2026, 8, 20, 12, tzinfo=UTC)
COMPLETED = STARTED + timedelta(minutes=5)
USAGE = {
    "prompt_tokens": 8,
    "completion_tokens": 2,
    "total_tokens": 10,
}


@pytest.fixture(autouse=True)
def _stub_paid_ledger_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "evals.paid_ledger_binding.require_persistent_budget_matches_trusted_ledger",
        _noop,
    )


def _settings() -> Settings:
    return Settings(
        deepseek_api_key=None,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model=REQUESTED_MODEL,
        deepseek_max_tokens=1024,
        deepseek_temperature=0,
    )


def _score_checks(*, failed: bool = False) -> list[ScoreCheck]:
    return [
        ScoreCheck(
            category=category,
            message=f"{category} {'failed' if failed else 'passed'}",
            passed=not (failed and category == "task_success"),
        )
        for category in (
            "task_success",
            "tool_selection",
            "security",
            "communication",
            "efficiency",
        )
    ]


def _success_call(
    *,
    sequence: int = 1,
    provider_attempts: int | None = 1,
    observed_model: str = REQUESTED_MODEL,
) -> ModelCallEvidence:
    return ModelCallEvidence(
        sequence=sequence,
        status="success",
        started_at="2026-08-20T12:00:00+00:00",
        latency_ms=1,
        message_count=2,
        tool_contract_count=6,
        phase="agent",
        finish_reason="stop",
        response_id=f"response-{sequence}",
        observed_model=observed_model,
        usage=dict(USAGE),
        provider_attempts=provider_attempts,
    )


def _error_call(
    *,
    sequence: int = 1,
    provider_attempts: int | None = 1,
    error_code: str = "MODEL_TRANSPORT_ERROR",
    error_stage: str = "provider_attempt",
) -> ModelCallEvidence:
    return ModelCallEvidence(
        sequence=sequence,
        status="error",
        started_at="2026-08-20T12:00:00+00:00",
        latency_ms=1,
        message_count=2,
        tool_contract_count=6,
        phase="agent",
        error_code=error_code,
        http_status=None,
        provider_request_id=None,
        provider_attempts=provider_attempts,
        error_stage=error_stage,
    )


def _result(
    *,
    case_id: str,
    model_calls: tuple[ModelCallEvidence, ...] | None = None,
    failed: bool = False,
    error_code: str | None = None,
) -> ReadonlyEvalResult:
    checks = _score_checks(failed=failed)
    case_run_id = f"eval-run-{case_id}"
    calls = model_calls if model_calls is not None else (_success_call(),)
    bound_calls = tuple(
        replace(
            call,
            logical_call_sha256=(
                call.logical_call_sha256
                or hashlib.sha256(
                    f"{case_run_id}:{call.phase}:{call.sequence}".encode("utf-8")
                ).hexdigest()
            ),
        )
        for call in calls
    )
    return ReadonlyEvalResult(
        case_id=case_id,
        trial=1,
        case_run_id=case_run_id,
        input_sha256="0" * 64,
        passed=not failed,
        started_at="2026-08-20T12:00:00+00:00",
        completed_at="2026-08-20T12:00:01+00:00",
        duration_ms=1,
        checks=[check.message for check in checks if check.passed],
        failures=[check.message for check in checks if not check.passed],
        score_checks=checks,
        final_text="" if error_code else "safe answer",
        model_calls=bound_calls,
        business_state_delta=BusinessStateDelta(
            changed=False,
            changed_tables=(),
            before_sha256="a" * 64,
            after_sha256="a" * 64,
        ),
        error_code=error_code,
    )


def _diagnostic_inputs() -> tuple[list, list[ReadonlyEvalResult]]:
    cases = load_cases(DEFAULT_CASE_DIR)
    return cases, [_result(case_id=case.case_id) for case in cases]


def _paid_budget(
    results: list[ReadonlyEvalResult],
    *,
    run_id: str = RUN_ID,
) -> dict:
    price = load_canonical_price_snapshot()
    reservation = canonical_worst_case_attempt_reservation_cny(
        canonical_price=price,
        max_output_tokens=_settings().deepseek_max_tokens,
    )
    buckets: dict[
        tuple[str, str, str | None, str | None, str | None, str],
        int,
    ] = {}
    settled_units = 0
    provider_attempts = 0
    uncertain_count = 0
    for result in results:
        for call in result.model_calls:
            attempts = call.provider_attempts or 0
            provider_attempts += attempts
            logical_call_hash = call.logical_call_sha256
            assert logical_call_hash is not None
            if call.status == "success" and call.usage is not None:
                cost = calculate_usage_cost_from_rates(
                    rates_cny=price.rates_cny.model_dump(),
                    tokens_per_price_unit=price.tokens_per_price_unit,
                    usage=call.usage,
                )
                settled_units += cost.units
                status = (
                    "settled_exact"
                    if cost.mode == "exact"
                    else "settled_upper_bound"
                )
                settled_key = (
                    logical_call_hash,
                    status,
                    cost.mode,
                    format_cny(cost.units),
                    None,
                    result.completed_at,
                )
                buckets[settled_key] = buckets.get(settled_key, 0) + 1
                uncertain_count += max(0, attempts - 1)
                if attempts > 1:
                    uncertain_key = (
                        logical_call_hash,
                        "uncertain",
                        None,
                        None,
                        "MODEL_TRANSPORT_ERROR",
                        result.completed_at,
                    )
                    buckets[uncertain_key] = (
                        buckets.get(uncertain_key, 0) + attempts - 1
                    )
            else:
                uncertain_count += attempts
                if attempts:
                    uncertain_key = (
                        logical_call_hash,
                        "uncertain",
                        None,
                        None,
                        call.error_code,
                        result.completed_at,
                    )
                    buckets[uncertain_key] = (
                        buckets.get(uncertain_key, 0) + attempts
                    )
    reservation_units = cny_to_units(Decimal(reservation))
    committed_units = settled_units + reservation_units * uncertain_count
    remaining_units = cny_to_units(Decimal("18")) - committed_units
    amount = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": format_cny(committed_units),
        "settled_cny": format_cny(settled_units),
        "remaining_execution_cny": format_cny(remaining_units),
        "attempt_count": provider_attempts,
        "reserved_count": 0,
        "uncertain_count": uncertain_count,
    }
    attempt_buckets = [
        {
            "logical_call_sha256": logical_call_hash,
            "status": status,
            "settlement_mode": mode,
            "reserved_cny": reservation,
            "known_cost_cny": cost_cny,
            "error_code": error_code,
            "completed_at": completed_at,
            "count": count,
        }
        for (
            logical_call_hash,
            status,
            mode,
            cost_cny,
            error_code,
            completed_at,
        ), count in sorted(buckets.items())
    ]
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
        "run_status": "completed",
        "run_identity": {
            "run_id": run_id,
            "purpose": "diagnostic",
            "model": REQUESTED_MODEL,
            "price_sha256": price.sha256,
            "status": "completed",
            "started_at": STARTED.isoformat(),
            "completed_at": COMPLETED.isoformat(),
        },
        "price": canonical_budget_price_payload(price),
        "reservation_cny_per_attempt": reservation,
        "run": dict(amount),
        "cumulative": dict(amount),
        "attempt_evidence": {
            "run": deepcopy(attempt_buckets),
            "cumulative": deepcopy(attempt_buckets),
        },
    }


def test_paid_budget_identity_cannot_start_before_price_window() -> None:
    _, results = _diagnostic_inputs()
    budget = _paid_budget(results)
    budget["run_identity"]["started_at"] = "2026-07-28T00:00:00+00:00"

    with pytest.raises(
        ValueError,
        match="price|window|identity|budget",
    ):
        _build_manifest(results=results, budget=budget)


@pytest.mark.parametrize(
    ("identity_started_at", "identity_completed_at"),
    [
        (
            "2026-08-20T09:00:00+00:00",
            "2026-08-20T11:59:59+00:00",
        ),
        (
            "2026-08-20T12:00:00+00:00",
            "2026-08-20T12:05:01+00:00",
        ),
    ],
)
def test_paid_diagnostic_binds_budget_identity_to_eval_timeline(
    identity_started_at: str,
    identity_completed_at: str,
) -> None:
    _, results = _diagnostic_inputs()
    budget = _paid_budget(results)
    budget["run_identity"]["started_at"] = identity_started_at
    budget["run_identity"]["completed_at"] = identity_completed_at

    with pytest.raises(
        ValueError,
        match="price|window|identity|budget",
    ):
        _build_manifest(results=results, budget=budget)


def test_budget_summary_preserves_cross_window_failure_timeline() -> None:
    _, results = _diagnostic_inputs()
    results[0] = _result(
        case_id=results[0].case_id,
        model_calls=(_error_call(),),
        failed=True,
        error_code="MODEL_TRANSPORT_ERROR",
    )
    budget = _paid_budget(results)
    price = load_canonical_price_snapshot()
    budget["run_identity"]["completed_at"] = (
        price.valid_until + timedelta(seconds=1)
    ).isoformat()

    validated = BudgetSummary.model_validate(budget)

    assert validated.run.uncertain_count == 1
    assert validated.run_identity is not None
    assert validated.price is not None
    assert validated.run_identity.completed_at > validated.price.valid_until


def test_full_diagnostic_preserves_price_expiry_failure_timeline() -> None:
    _, results = _diagnostic_inputs()
    results[0] = _result(
        case_id=results[0].case_id,
        model_calls=(_error_call(error_code="MODEL_PRICE_EXPIRED"),),
        failed=True,
        error_code="MODEL_PRICE_EXPIRED",
    )
    budget = _paid_budget(results)
    price = load_canonical_price_snapshot()
    identity_completed = price.valid_until + timedelta(seconds=1)
    eval_completed = identity_completed + timedelta(seconds=1)
    budget["run_identity"]["completed_at"] = identity_completed.isoformat()
    for scope in ("run", "cumulative"):
        for bucket in budget["attempt_evidence"][scope]:
            if bucket["error_code"] == "MODEL_PRICE_EXPIRED":
                bucket["completed_at"] = identity_completed.isoformat()

    payload = _payload(
        results=results,
        budget=budget,
        completed_at=eval_completed,
    )

    validated = validate_readonly_payload(payload)
    assert validated.manifest.completed_at == eval_completed
    assert validated.summary.budget.run.uncertain_count == 1


def test_full_diagnostic_allows_zero_attempt_reserve_time_price_expiry() -> None:
    _, results = _diagnostic_inputs()
    results[0] = _result(
        case_id=results[0].case_id,
        model_calls=(
            _error_call(
                error_code="MODEL_PRICE_EXPIRED",
                provider_attempts=0,
                error_stage="reserve_attempt",
            ),
        ),
        failed=True,
        error_code="MODEL_PRICE_EXPIRED",
    )
    budget = _paid_budget(results)
    price = load_canonical_price_snapshot()
    identity_completed = price.valid_until + timedelta(seconds=1)
    eval_completed = identity_completed + timedelta(seconds=1)
    budget["run_identity"]["completed_at"] = identity_completed.isoformat()

    payload = _payload(
        results=results,
        budget=budget,
        completed_at=eval_completed,
    )

    validated = validate_readonly_payload(payload)
    assert validated.summary.budget.run.uncertain_count == 0


def test_public_diagnostic_rejects_cross_window_transport_outcome_relabelled_as_price_expiry() -> None:
    _, results = _diagnostic_inputs()
    transport_result = _result(
        case_id=results[0].case_id,
        model_calls=(_error_call(error_code="MODEL_TRANSPORT_ERROR"),),
        failed=True,
        error_code="MODEL_TRANSPORT_ERROR",
    )
    results[0] = transport_result
    budget = _paid_budget(results)
    payload = _payload(results=results, budget=budget)
    attacked_results = deepcopy(results)
    attacked_results[0] = _result(
        case_id=transport_result.case_id,
        model_calls=(_error_call(error_code="MODEL_PRICE_EXPIRED"),),
        failed=True,
        error_code="MODEL_PRICE_EXPIRED",
    )
    records = [
        result_to_record(result, split="dev")
        for result in attacked_results
    ]
    payload["cases"] = records
    payload["trajectories"] = deepcopy(records)
    payload["summary"] = summarize_results(
        run_id=RUN_ID,
        results=attacked_results,
        planned_trials=1,
        budget_report=budget,
    )
    price = load_canonical_price_snapshot()
    identity_completed = price.valid_until + timedelta(seconds=1)
    payload["manifest"]["completed_at"] = (
        identity_completed + timedelta(seconds=1)
    ).isoformat()
    payload["summary"]["budget"]["run_identity"]["completed_at"] = (
        identity_completed.isoformat()
    )

    with pytest.raises(
        ValueError,
        match="price|attempt|outcome|error|budget",
    ):
        validate_readonly_payload(payload)


def test_public_diagnostic_rejects_swapped_outcomes_between_logical_calls() -> None:
    _, results = _diagnostic_inputs()
    first_case_id = results[0].case_id
    second_case_id = results[1].case_id
    results[0] = _result(
        case_id=first_case_id,
        model_calls=(_error_call(error_code="MODEL_TRANSPORT_ERROR"),),
        failed=True,
        error_code="MODEL_TRANSPORT_ERROR",
    )
    results[1] = _result(
        case_id=second_case_id,
        model_calls=(_error_call(error_code="MODEL_PRICE_EXPIRED"),),
        failed=True,
        error_code="MODEL_PRICE_EXPIRED",
    )
    budget = _paid_budget(results)
    price = load_canonical_price_snapshot()
    identity_completed = price.valid_until + timedelta(seconds=1)
    for scope in ("run", "cumulative"):
        for bucket in budget["attempt_evidence"][scope]:
            if bucket["error_code"] == "MODEL_PRICE_EXPIRED":
                bucket["completed_at"] = identity_completed.isoformat()
    budget["run_identity"]["completed_at"] = identity_completed.isoformat()

    attacked_results = list(results)
    attacked_results[0] = _result(
        case_id=first_case_id,
        model_calls=(_error_call(error_code="MODEL_PRICE_EXPIRED"),),
        failed=True,
        error_code="MODEL_PRICE_EXPIRED",
    )
    attacked_results[1] = _result(
        case_id=second_case_id,
        model_calls=(_error_call(error_code="MODEL_TRANSPORT_ERROR"),),
        failed=True,
        error_code="MODEL_TRANSPORT_ERROR",
    )

    with pytest.raises(ValueError, match="logical|outcome|price|hash"):
        _payload(
            results=attacked_results,
            budget=budget,
            completed_at=identity_completed + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    ("model_error", "ledger_error"),
    [
        ("MODEL_BUDGET_USAGE_ERROR", "MISSING_PROVIDER_USAGE"),
        ("MODEL_BUDGET_USAGE_ERROR", "INVALID_PROVIDER_USAGE"),
        ("MODEL_BUDGET_ERROR", "COST_EXCEEDS_RESERVATION"),
        ("MODEL_BUDGET_ERROR", "MODEL_BUDGET_ERROR"),
    ],
)
def test_paid_diagnostic_accepts_budget_error_namespace_aliases(
    model_error: str,
    ledger_error: str,
) -> None:
    _, results = _diagnostic_inputs()
    results[0] = _result(
        case_id=results[0].case_id,
        model_calls=(_error_call(error_code=model_error),),
        failed=True,
        error_code=model_error,
    )
    budget = _paid_budget(results)
    for scope in ("run", "cumulative"):
        for bucket in budget["attempt_evidence"][scope]:
            if bucket["error_code"] == model_error:
                bucket["error_code"] = ledger_error

    validated = validate_readonly_payload(
        _payload(results=results, budget=budget)
    )
    assert validated.summary.budget.run.uncertain_count == 1


@pytest.mark.parametrize(
    "terminal_error",
    ["MODEL_BUDGET_EXHAUSTED", "MODEL_PRICE_EXPIRED"],
)
def test_paid_diagnostic_accepts_retry_then_reserve_stage_failure(
    terminal_error: str,
) -> None:
    _, results = _diagnostic_inputs()
    results[0] = _result(
        case_id=results[0].case_id,
        model_calls=(
            _error_call(
                error_code=terminal_error,
                error_stage="reserve_attempt",
            ),
        ),
        failed=True,
        error_code=terminal_error,
    )
    budget = _paid_budget(results)
    for scope in ("run", "cumulative"):
        for bucket in budget["attempt_evidence"][scope]:
            if bucket["error_code"] == terminal_error:
                bucket["error_code"] = "MODEL_HTTP_ERROR"
    completed_at = COMPLETED
    if terminal_error == "MODEL_PRICE_EXPIRED":
        price = load_canonical_price_snapshot()
        identity_completed = price.valid_until + timedelta(seconds=1)
        completed_at = identity_completed + timedelta(seconds=1)
        budget["run_identity"]["completed_at"] = identity_completed.isoformat()

    validated = validate_readonly_payload(
        _payload(
            results=results,
            budget=budget,
            completed_at=completed_at,
        )
    )
    assert validated.summary.budget.run.uncertain_count == 1


def _build_manifest(
    *,
    results: list[ReadonlyEvalResult],
    budget: dict,
    started_at: datetime = STARTED,
    completed_at: datetime = COMPLETED,
) -> dict:
    cases = load_cases(DEFAULT_CASE_DIR)
    manifest = build_readonly_manifest(
        run_id=RUN_ID,
        purpose="diagnostic",
        split="dev",
        case_set_name="readonly-dev-v1",
        cases=cases,
        results=results,
        settings=_settings(),
        planned_trials=1,
        started_at=started_at,
        completed_at=completed_at,
        budget_report=budget,
    )
    manifest["artifacts"] = {
        "cases": "cases.jsonl",
        "summary": "summary.json",
        "trajectories": "trajectories/",
        "integrity": "integrity.json",
    }
    return manifest


def _payload(
    *,
    results: list[ReadonlyEvalResult],
    budget: dict,
    started_at: datetime = STARTED,
    completed_at: datetime = COMPLETED,
) -> dict:
    records = [result_to_record(result, split="dev") for result in results]
    return {
        "manifest": _build_manifest(
            results=results,
            budget=budget,
            started_at=started_at,
            completed_at=completed_at,
        ),
        "cases": records,
        "summary": summarize_results(
            run_id=RUN_ID,
            results=results,
            planned_trials=1,
            budget_report=budget,
        ),
        "trajectories": deepcopy(records),
        "integrity": {
            "schema_version": "1.0",
            "algorithm": "sha256",
            "files": {},
        },
    }


def _manifest_budget(budget: dict) -> dict:
    price = budget.get("price")
    run = budget["run"]
    return {
        "schema_version": "1.0",
        "enforcement_mode": budget["enforcement_mode"],
        "run_status": budget.get("run_status"),
        "currency": run["currency"],
        "hard_limit_cny": run["hard_limit_cny"],
        "execution_limit_cny": run["execution_limit_cny"],
        "reservation_cny_per_attempt": (budget["reservation_cny_per_attempt"]),
        "price_snapshot_sha256": (
            price["snapshot_sha256"] if price is not None else None
        ),
        "price_source_url": (price["source_url"] if price is not None else None),
        "usage_source_url": (price["usage_source_url"] if price is not None else None),
        "price_captured_at": (price["captured_at"] if price is not None else None),
        "price_valid_until": (price["valid_until"] if price is not None else None),
    }


def _attack_success_without_settled(
    results: list[ReadonlyEvalResult],
    budget: dict,
) -> None:
    reservation = budget["reservation_cny_per_attempt"]
    # 新费率下 10 次全量预留会超过 ¥18 执行上限；真实账本最多能保留
    # floor(18/reservation) 次不确定预留。伪造预算只保留这个子集并保持
    # 总额与桶证据对账一致，剩余成功调用缺 settled 证据，由绑定检查拒绝。
    kept = int(Decimal("18") // Decimal(reservation))
    committed = Decimal(reservation) * kept
    bucket = {
        "logical_call_sha256": "a" * 64,
        "status": "uncertain",
        "settlement_mode": None,
        "reserved_cny": reservation,
        "known_cost_cny": None,
        "error_code": "MODEL_TRANSPORT_ERROR",
        "completed_at": "2026-08-20T12:00:01+00:00",
        "count": kept,
    }
    for scope in ("run", "cumulative"):
        budget[scope].update(
            {
                "committed_cny": format(committed, "f"),
                "settled_cny": "0",
                "remaining_execution_cny": format(
                    Decimal("18") - committed,
                    "f",
                ),
                "attempt_count": kept,
                "reserved_count": 0,
                "uncertain_count": kept,
            }
        )
        budget["attempt_evidence"][scope] = [deepcopy(bucket)]


def _attack_retry_without_uncertain(
    results: list[ReadonlyEvalResult],
    budget: dict,
) -> None:
    results[0].model_calls = (replace(results[0].model_calls[0], provider_attempts=2),)


def _attack_error_without_uncertain(
    results: list[ReadonlyEvalResult],
    budget: dict,
) -> None:
    results[0] = _result(
        case_id=results[0].case_id,
        model_calls=(_error_call(),),
        failed=True,
        error_code="MODEL_TRANSPORT_ERROR",
    )


def _attack_extra_uncertain(
    results: list[ReadonlyEvalResult],
    budget: dict,
) -> None:
    reservation = budget["reservation_cny_per_attempt"]
    reservation_amount = Decimal(reservation)
    bucket = {
        "logical_call_sha256": "b" * 64,
        "status": "uncertain",
        "settlement_mode": None,
        "reserved_cny": reservation,
        "known_cost_cny": None,
        "error_code": "MODEL_TRANSPORT_ERROR",
        "completed_at": "2026-08-20T12:00:01+00:00",
        "count": 1,
    }
    for scope in ("run", "cumulative"):
        budget[scope]["committed_cny"] = format(
            Decimal(budget[scope]["committed_cny"]) + reservation_amount,
            "f",
        )
        budget[scope]["remaining_execution_cny"] = format(
            Decimal(budget[scope]["remaining_execution_cny"]) - reservation_amount,
            "f",
        )
        budget[scope]["attempt_count"] += 1
        budget[scope]["uncertain_count"] += 1
        budget["attempt_evidence"][scope].append(deepcopy(bucket))


def _attack_passed_without_calls(
    results: list[ReadonlyEvalResult],
    budget: dict,
) -> None:
    results[0].model_calls = ()


def _attack_failed_without_local_error(
    results: list[ReadonlyEvalResult],
    budget: dict,
) -> None:
    results[0] = _result(
        case_id=results[0].case_id,
        model_calls=(),
        failed=True,
        error_code=None,
    )


ATTACKS: dict[
    str,
    Callable[[list[ReadonlyEvalResult], dict], None],
] = {
    "success_without_settled": _attack_success_without_settled,
    "retry_without_uncertain": _attack_retry_without_uncertain,
    "error_without_uncertain": _attack_error_without_uncertain,
    "extra_uncertain": _attack_extra_uncertain,
    "passed_without_calls": _attack_passed_without_calls,
    "failed_without_local_error": _attack_failed_without_local_error,
}


@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_diagnostic_producer_rejects_unbound_model_and_budget_evidence(
    attack: str,
) -> None:
    _, results = _diagnostic_inputs()
    budget = _paid_budget(results)
    ATTACKS[attack](results, budget)

    with pytest.raises(
        ValueError,
        match="diagnostic|attempt|bucket|call|record|provider|error",
    ):
        _build_manifest(results=results, budget=budget)


@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_public_schema_rejects_unbound_diagnostic_model_and_budget_evidence(
    attack: str,
) -> None:
    _, valid_results = _diagnostic_inputs()
    payload = _payload(
        results=valid_results,
        budget=_paid_budget(valid_results),
    )
    attacked_results = deepcopy(valid_results)
    attacked_budget = _paid_budget(attacked_results)
    ATTACKS[attack](attacked_results, attacked_budget)
    records = [result_to_record(result, split="dev") for result in attacked_results]
    payload["cases"] = records
    payload["trajectories"] = deepcopy(records)
    payload["summary"] = summarize_results(
        run_id=RUN_ID,
        results=attacked_results,
        planned_trials=1,
        budget_report=attacked_budget,
    )
    payload["manifest"]["budget"] = _manifest_budget(attacked_budget)
    payload["manifest"]["model"]["observed_models"] = sorted(
        {
            call.observed_model
            for result in attacked_results
            for call in result.model_calls
            if call.observed_model is not None
        }
    )

    with pytest.raises(
        ValueError,
        match="diagnostic|attempt|bucket|call|record|provider|error",
    ):
        validate_readonly_payload(payload)


def test_diagnostic_offline_evidence_cannot_claim_deepseek_observation() -> None:
    _, results = _diagnostic_inputs()
    offline = offline_budget_report()

    with pytest.raises(
        ValueError,
        match="offline|diagnostic|provider|model|attempt",
    ):
        _build_manifest(results=results, budget=offline)

    paid_payload = _payload(
        results=results,
        budget=_paid_budget(results),
    )
    paid_payload["manifest"]["budget"] = {
        "schema_version": "1.0",
        "enforcement_mode": "offline_no_paid_provider",
        "run_status": "completed",
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "reservation_cny_per_attempt": "0",
        "price_snapshot_sha256": None,
        "price_source_url": None,
        "usage_source_url": None,
        "price_captured_at": None,
        "price_valid_until": None,
    }
    paid_payload["summary"]["budget"] = offline

    with pytest.raises(
        ValueError,
        match="offline|diagnostic|provider|model|attempt",
    ):
        validate_readonly_payload(paid_payload)


def test_diagnostic_accepts_retry_and_error_when_uncertain_is_exact() -> None:
    _, results = _diagnostic_inputs()
    results[0].model_calls = (replace(results[0].model_calls[0], provider_attempts=2),)
    results[1] = _result(
        case_id=results[1].case_id,
        model_calls=(_success_call(), _error_call(sequence=2)),
        failed=True,
        error_code="MODEL_TRANSPORT_ERROR",
    )
    budget = _paid_budget(results)

    manifest = _build_manifest(results=results, budget=budget)
    payload = _payload(results=results, budget=budget)

    assert manifest["status"] == "completed"
    assert payload["summary"]["budget"]["run"]["uncertain_count"] == 2
    validate_readonly_payload(payload)


def test_offline_diagnostic_allows_explicit_zero_attempt_local_failures() -> None:
    cases = load_cases(DEFAULT_CASE_DIR)
    results = [
        _result(
            case_id=case.case_id,
            model_calls=(),
            failed=True,
            error_code="UNEXPECTED_EVAL_ERROR",
        )
        for case in cases
    ]
    budget = offline_budget_report()

    manifest = _build_manifest(results=results, budget=budget)
    payload = _payload(results=results, budget=budget)

    assert manifest["model"]["observed_models"] == []
    assert payload["summary"]["budget"]["run"]["attempt_count"] == 0
    validate_readonly_payload(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("run", "attempt_count"), True),
        (("run", "reserved_count"), "0"),
        (("run", "uncertain_count"), 0.0),
        (("attempt_evidence", "run", 0, "count"), True),
        (("attempt_evidence", "run", 0, "count"), "10"),
        (("attempt_evidence", "run", 0, "count"), 10.0),
    ],
)
def test_budget_evidence_counts_reject_coerced_integers(
    path: tuple,
    value: object,
) -> None:
    _, results = _diagnostic_inputs()
    budget = _paid_budget(results)
    target: object = budget
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ValueError, match="count|integer|attempt|budget"):
        BudgetSummary.model_validate(budget)
