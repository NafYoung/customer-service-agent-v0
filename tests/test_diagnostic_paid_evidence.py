from __future__ import annotations

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
STARTED = datetime(2026, 7, 29, 12, tzinfo=UTC)
COMPLETED = STARTED + timedelta(minutes=5)
USAGE = {
    "prompt_tokens": 8,
    "completion_tokens": 2,
    "total_tokens": 10,
}


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
        started_at="2026-07-29T12:00:00+00:00",
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
) -> ModelCallEvidence:
    return ModelCallEvidence(
        sequence=sequence,
        status="error",
        started_at="2026-07-29T12:00:00+00:00",
        latency_ms=1,
        message_count=2,
        tool_contract_count=6,
        phase="agent",
        error_code="MODEL_TRANSPORT_ERROR",
        http_status=None,
        provider_request_id=None,
        provider_attempts=provider_attempts,
    )


def _result(
    *,
    case_id: str,
    model_calls: tuple[ModelCallEvidence, ...] | None = None,
    failed: bool = False,
    error_code: str | None = None,
) -> ReadonlyEvalResult:
    checks = _score_checks(failed=failed)
    return ReadonlyEvalResult(
        case_id=case_id,
        trial=1,
        case_run_id=f"eval-run-{case_id}",
        input_sha256="0" * 64,
        passed=not failed,
        started_at="2026-07-29T12:00:00+00:00",
        completed_at="2026-07-29T12:00:01+00:00",
        duration_ms=1,
        checks=[check.message for check in checks if check.passed],
        failures=[
            check.message for check in checks if not check.passed
        ],
        score_checks=checks,
        final_text="" if error_code else "safe answer",
        model_calls=(
            model_calls
            if model_calls is not None
            else (_success_call(),)
        ),
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
    return cases, [
        _result(case_id=case.case_id)
        for case in cases
    ]


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
    settled_buckets: dict[tuple[str, str], int] = {}
    settled_units = 0
    provider_attempts = 0
    uncertain_count = 0
    for result in results:
        for call in result.model_calls:
            attempts = call.provider_attempts or 0
            provider_attempts += attempts
            if call.status == "success" and call.usage is not None:
                cost = calculate_usage_cost_from_rates(
                    rates_cny=price.rates_cny.model_dump(),
                    tokens_per_price_unit=price.tokens_per_price_unit,
                    usage=call.usage,
                )
                settled_units += cost.units
                key = (cost.mode, format_cny(cost.units))
                settled_buckets[key] = settled_buckets.get(key, 0) + 1
                uncertain_count += max(0, attempts - 1)
            else:
                uncertain_count += attempts
    reservation_units = cny_to_units(Decimal(reservation))
    committed_units = (
        settled_units + reservation_units * uncertain_count
    )
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
    buckets = [
        {
            "status": (
                "settled_exact"
                if mode == "exact"
                else "settled_upper_bound"
            ),
            "settlement_mode": mode,
            "reserved_cny": reservation,
            "known_cost_cny": cost_cny,
            "count": count,
        }
        for (mode, cost_cny), count in sorted(settled_buckets.items())
    ]
    if uncertain_count:
        buckets.append(
            {
                "status": "uncertain",
                "settlement_mode": None,
                "reserved_cny": reservation,
                "known_cost_cny": None,
                "count": uncertain_count,
            }
        )
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
            "run": deepcopy(buckets),
            "cumulative": deepcopy(buckets),
        },
    }


def _build_manifest(
    *,
    results: list[ReadonlyEvalResult],
    budget: dict,
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
        started_at=STARTED,
        completed_at=COMPLETED,
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
) -> dict:
    records = [
        result_to_record(result, split="dev")
        for result in results
    ]
    return {
        "manifest": _build_manifest(results=results, budget=budget),
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
        "reservation_cny_per_attempt": (
            budget["reservation_cny_per_attempt"]
        ),
        "price_snapshot_sha256": (
            price["snapshot_sha256"] if price is not None else None
        ),
        "price_source_url": (
            price["source_url"] if price is not None else None
        ),
        "usage_source_url": (
            price["usage_source_url"] if price is not None else None
        ),
        "price_captured_at": (
            price["captured_at"] if price is not None else None
        ),
        "price_valid_until": (
            price["valid_until"] if price is not None else None
        ),
    }


def _attack_success_without_settled(
    results: list[ReadonlyEvalResult],
    budget: dict,
) -> None:
    reservation = budget["reservation_cny_per_attempt"]
    count = budget["run"]["attempt_count"]
    committed = Decimal(reservation) * count
    bucket = {
        "status": "uncertain",
        "settlement_mode": None,
        "reserved_cny": reservation,
        "known_cost_cny": None,
        "count": count,
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
                "reserved_count": 0,
                "uncertain_count": count,
            }
        )
        budget["attempt_evidence"][scope] = [deepcopy(bucket)]


def _attack_retry_without_uncertain(
    results: list[ReadonlyEvalResult],
    budget: dict,
) -> None:
    results[0].model_calls = (
        replace(results[0].model_calls[0], provider_attempts=2),
    )


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
        "status": "uncertain",
        "settlement_mode": None,
        "reserved_cny": reservation,
        "known_cost_cny": None,
        "count": 1,
    }
    for scope in ("run", "cumulative"):
        budget[scope]["committed_cny"] = format(
            Decimal(budget[scope]["committed_cny"])
            + reservation_amount,
            "f",
        )
        budget[scope]["remaining_execution_cny"] = format(
            Decimal(budget[scope]["remaining_execution_cny"])
            - reservation_amount,
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
    results[0].model_calls = (
        replace(results[0].model_calls[0], provider_attempts=2),
    )
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
