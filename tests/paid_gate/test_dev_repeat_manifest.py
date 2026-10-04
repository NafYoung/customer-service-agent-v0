from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evals.evidence_schema import (
    validate_readonly_payload,
)
from evals.readonly_reporting import (
    build_readonly_manifest,
    result_to_record,
    summarize_results,
)
from tests.paid_gate.helpers import (
    _attempt_count,
    _dev_repeat_inputs,
    _dev_repeat_payload,
    _logical_call_hashes,
    _paid_budget,
    _settings,
)


def test_dev_repeat_manifest_accepts_only_canonical_7_by_4_case_set() -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-valid"
    started = datetime(2026, 8, 20, 12, tzinfo=UTC)

    manifest = build_readonly_manifest(
        run_id=run_id,
        purpose="dev_repeat",
        split="dev",
        case_set_name="readonly-regression-v1",
        cases=cases,
        results=results,
        settings=_settings(),
        planned_trials=4,
        started_at=started,
        completed_at=started + timedelta(minutes=5),
        budget_report=_paid_budget(
            run_id=run_id,
            purpose="dev_repeat",
            attempt_count=_attempt_count(results),
            logical_call_hashes=_logical_call_hashes(results),
        ),
    )

    assert manifest["eval"]["case_count"] == 7
    assert (
        manifest["eval"]["case_set_sha256"]
        == "e8263f89e8b5fb1a5fb114f2700fe02045c39ebe00917dcea7381f8590e17926"
    )

def test_public_validator_recomputes_dev_repeat_bucket_costs() -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-public"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=_attempt_count(results),
        logical_call_hashes=_logical_call_hashes(results),
    )
    started = datetime(2026, 8, 20, 12, tzinfo=UTC)
    manifest = build_readonly_manifest(
        run_id=run_id,
        purpose="dev_repeat",
        split="dev",
        case_set_name="readonly-regression-v1",
        cases=cases,
        results=results,
        settings=_settings(),
        planned_trials=4,
        started_at=started,
        completed_at=started + timedelta(minutes=5),
        budget_report=budget,
    )
    manifest["artifacts"] = {
        "cases": "cases.jsonl",
        "summary": "summary.json",
        "trajectories": "trajectories/",
        "integrity": "integrity.json",
    }
    records = [result_to_record(result, split="dev") for result in results]
    payload = {
        "manifest": manifest,
        "cases": records,
        "summary": summarize_results(
            run_id=run_id,
            results=results,
            planned_trials=4,
            budget_report=budget,
        ),
        "trajectories": deepcopy(records),
        "integrity": {
            "schema_version": "1.0",
            "algorithm": "sha256",
            "files": {},
        },
    }
    validate_readonly_payload(payload)

    forged = deepcopy(payload)
    forged_cost = Decimal("0.000013")
    forged_total = forged_cost * _attempt_count(results)
    for scope in ("run", "cumulative"):
        forged["summary"]["budget"]["attempt_evidence"][scope][0]["known_cost_cny"] = (
            format(forged_cost, "f")
        )
        forged["summary"]["budget"][scope]["committed_cny"] = format(forged_total, "f")
        forged["summary"]["budget"][scope]["settled_cny"] = format(forged_total, "f")
        forged["summary"]["budget"][scope]["remaining_execution_cny"] = format(
            Decimal("18") - forged_total, "f"
        )

    with pytest.raises(ValueError, match="attempt|bucket|cost|record"):
        validate_readonly_payload(forged)

def test_dev_repeat_payload_cannot_be_relabelled_as_diagnostic() -> None:
    payload = _dev_repeat_payload()
    payload["manifest"]["purpose"] = "diagnostic"
    payload["summary"]["budget"]["run_identity"]["purpose"] = "diagnostic"

    with pytest.raises(ValueError, match="diagnostic|canonical|case"):
        validate_readonly_payload(payload)

@pytest.mark.parametrize(
    "attack",
    [
        "move_calls_between_trials",
        "all_calls_missing_with_forged_manifest",
        "success_has_error",
        "agent_contract_count",
    ],
)
def test_public_validator_binds_paid_calls_to_each_trial(
    attack: str,
) -> None:
    payload = _dev_repeat_payload()
    if attack == "move_calls_between_trials":
        for section in ("cases", "trajectories"):
            moved = payload[section][0]["model_calls"]
            payload[section][0]["model_calls"] = []
            payload[section][1]["model_calls"] = [
                *moved,
                *payload[section][1]["model_calls"],
            ]
    elif attack == "all_calls_missing_with_forged_manifest":
        for section in ("cases", "trajectories"):
            for record in payload[section]:
                record["model_calls"] = []
        payload["summary"]["usage"] = {"model_calls": 0}
        payload["summary"]["latency_ms"]["model_call"] = {
            "p50": None,
            "p95": None,
            "max": None,
            "total": 0,
        }
        budget = payload["summary"]["budget"]
        for scope in ("run", "cumulative"):
            budget[scope].update(
                {
                    "committed_cny": "0",
                    "settled_cny": "0",
                    "remaining_execution_cny": "18",
                    "attempt_count": 0,
                    "reserved_count": 0,
                    "uncertain_count": 0,
                }
            )
            budget["attempt_evidence"][scope] = []
        payload["manifest"]["model"]["observed_models"] = [
            payload["manifest"]["model"]["requested_model"]
        ]
    else:
        for section in ("cases", "trajectories"):
            agent_call = payload[section][0]["model_calls"][0]
            if attack == "success_has_error":
                agent_call["error_code"] = "FORGED_SUCCESS_ERROR"
            else:
                agent_call["tool_contract_count"] = 5

    with pytest.raises(
        ValueError,
        match="call|trial|record|observed|model",
    ):
        validate_readonly_payload(payload)

@pytest.mark.parametrize(
    "attack",
    [
        "active",
        "reserved",
        "uncertain",
        "unsettled",
        "overrun",
        "forged_price",
        "reservation",
        "attempt_count",
        "missing_usage",
        "retry",
        "cost",
        "missing_attempt_evidence",
        "bucket_call_mismatch",
    ],
)
def test_dev_repeat_manifest_rejects_unsettled_or_unpriced_paid_evidence(
    attack: str,
) -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-attacked"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=_attempt_count(results),
        logical_call_hashes=_logical_call_hashes(results),
    )
    if attack == "active":
        budget["run_status"] = "active"
        budget["run_identity"]["status"] = "active"
        budget["run_identity"]["completed_at"] = None
    elif attack in {"reserved", "uncertain"}:
        for scope in ("run", "cumulative"):
            budget[scope][f"{attack}_count"] = 1
    elif attack == "unsettled":
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "1"
            budget[scope]["remaining_execution_cny"] = "17"
    elif attack == "overrun":
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "18.1"
            budget[scope]["settled_cny"] = "18.1"
            budget[scope]["remaining_execution_cny"] = "0"
    elif attack == "forged_price":
        fake_hash = "0" * 64
        budget["run_identity"]["price_sha256"] = fake_hash
        budget["price"]["snapshot_sha256"] = fake_hash
    elif attack == "reservation":
        budget["reservation_cny_per_attempt"] = "0"
    elif attack == "attempt_count":
        budget["run"]["attempt_count"] += 1
    elif attack == "missing_usage":
        results[0].model_calls = (
            replace(results[0].model_calls[0], usage=None),
            *results[0].model_calls[1:],
        )
    elif attack == "retry":
        results[0].model_calls = (
            replace(results[0].model_calls[0], provider_attempts=2),
            *results[0].model_calls[1:],
        )
        budget["run"]["attempt_count"] += 1
    elif attack == "cost":
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "1"
            budget[scope]["settled_cny"] = "1"
            budget[scope]["remaining_execution_cny"] = "17"
    elif attack == "missing_attempt_evidence":
        budget.pop("attempt_evidence")
    elif attack == "bucket_call_mismatch":
        mismatched_cost = Decimal("0.000013")
        mismatched_total = mismatched_cost * len(results)
        for scope in ("run", "cumulative"):
            budget["attempt_evidence"][scope][0]["known_cost_cny"] = format(
                mismatched_cost, "f"
            )
            budget[scope]["committed_cny"] = format(
                mismatched_total,
                "f",
            )
            budget[scope]["settled_cny"] = format(
                mismatched_total,
                "f",
            )
            budget[scope]["remaining_execution_cny"] = format(
                Decimal("18") - mismatched_total,
                "f",
            )

    started = datetime(2026, 8, 20, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="budget|price|usage|attempt|canonical"):
        build_readonly_manifest(
            run_id=run_id,
            purpose="dev_repeat",
            split="dev",
            case_set_name="readonly-regression-v1",
            cases=cases,
            results=results,
            settings=_settings(),
            planned_trials=4,
            started_at=started,
            completed_at=started + timedelta(minutes=5),
            budget_report=budget,
        )

@pytest.mark.parametrize(
    "attack",
    [
        "move_calls_between_trials",
        "agent_phase_missing",
        "agent_sequence_gap",
        "judge_has_tools",
        "call_outside_trial_window",
        "success_has_error",
        "agent_contract_count",
    ],
)
def test_dev_repeat_manifest_binds_calls_to_each_completed_trial(
    attack: str,
) -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-call-binding"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=_attempt_count(results),
        logical_call_hashes=_logical_call_hashes(results),
    )
    if attack == "move_calls_between_trials":
        moved = results[0].model_calls
        results[0].model_calls = ()
        results[1].model_calls = (*moved, *results[1].model_calls)
    elif attack == "agent_phase_missing":
        results[0].model_calls = tuple(
            replace(call, phase="semantic_judge") for call in results[0].model_calls
        )
    elif attack == "agent_sequence_gap":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            replace(agent, sequence=2),
            judge,
        )
    elif attack == "judge_has_tools":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            agent,
            replace(judge, tool_contract_count=6),
        )
    elif attack == "call_outside_trial_window":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            replace(
                agent,
                started_at="2026-08-20T11:59:59+00:00",
            ),
            judge,
        )
    elif attack == "success_has_error":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            replace(agent, error_code="FORGED_SUCCESS_ERROR"),
            judge,
        )
    elif attack == "agent_contract_count":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            replace(agent, tool_contract_count=5),
            judge,
        )

    started = datetime(2026, 8, 20, 12, tzinfo=UTC)
    with pytest.raises(
        ValueError,
        match="call|trial|phase|sequence|judge|time|record",
    ):
        build_readonly_manifest(
            run_id=run_id,
            purpose="dev_repeat",
            split="dev",
            case_set_name="readonly-regression-v1",
            cases=cases,
            results=results,
            settings=_settings(),
            planned_trials=4,
            started_at=started,
            completed_at=started + timedelta(minutes=5),
            budget_report=budget,
        )
