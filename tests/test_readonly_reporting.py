from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from evals import readonly_reporting
from evals.calibration_attestation import (
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
)
from evals.evidence import (
    BusinessStateDelta,
    ModelCallEvidence,
    stable_sha256,
)
from evals.evidence_schema import validate_readonly_payload
from evals.readonly_eval import (
    ReadonlyEvalCase,
    ReadonlyEvalResult,
    ScoreCheck,
    load_cases,
)
from evals.readonly_reporting import (
    FormalHoldoutEvidence,
    build_readonly_manifest,
    create_server_run_id,
    current_readonly_harness_fingerprints,
    result_to_record,
    summarize_results,
)


def _budget_report(
    attempt_count: int = 4,
    *,
    run_id: str = "eval-20260729-abcdef12",
    purpose: str = "holdout_formal",
) -> dict:
    settled = Decimal(attempt_count) * Decimal("0.000012")
    settled_cny = format(settled, "f")
    amount = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": settled_cny,
        "settled_cny": settled_cny,
        "remaining_execution_cny": format(
            Decimal("18") - settled,
            "f",
        ),
        "attempt_count": attempt_count,
        "reserved_count": 0,
        "uncertain_count": 0,
    }
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
        "run_status": "completed",
        "run_identity": {
            "run_id": run_id,
            "purpose": purpose,
            "model": "deepseek-v4-flash",
            "price_sha256": "f" * 64,
            "status": "completed",
            "started_at": "2026-07-29T12:00:00+00:00",
            "completed_at": "2026-07-29T12:05:00+00:00",
        },
        "price": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "currency": "CNY",
            "snapshot_sha256": "f" * 64,
            "source_url": (
                "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
            ),
            "usage_source_url": (
                "https://api-docs.deepseek.com/api/create-chat-completion/"
            ),
            "captured_at": "2026-07-29T08:58:58+00:00",
            "valid_until": "2026-07-30T08:58:58+00:00",
            "rates_cny": {
                "prompt_cache_hit": "0.02",
                "prompt_cache_miss": "1",
                "completion": "2",
            },
            "tokens_per_price_unit": 1_000_000,
        },
        "reservation_cny_per_attempt": "1.002048",
        "run": dict(amount),
        "cumulative": dict(amount),
    }


def _attestation() -> ValidatedCalibrationAttestation:
    return ValidatedCalibrationAttestation(
        report_sha256="a" * 64,
        run_id="eval-20260729-calibration-v2",
        source_git_commit="1" * 40,
        fixture_sha256="c" * 64,
        contract_set_sha256="d" * 64,
        harness_sha256="e" * 64,
        result_count=49,
        fixture_ids=tuple(
            f"canonical-fixture-{index:02d}"
            for index in range(49)
        ),
        fixture_kinds=tuple(
            (
                f"canonical-fixture-{index:02d}",
                "safe_canonical",
            )
            for index in range(49)
        ),
        completed_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )


def _review() -> ValidatedCalibrationReview:
    return ValidatedCalibrationReview(
        review_sha256="b" * 64,
        reviewer_id="independent-reviewer-v1",
        reviewed_count=5,
    )


def _result(
    *,
    case_id: str,
    trial: int,
    passed: bool,
    efficiency_passed: bool = True,
) -> ReadonlyEvalResult:
    return ReadonlyEvalResult(
        case_id=case_id,
        trial=trial,
        case_run_id=f"eval-run-t{trial}-{case_id}",
        input_sha256="0" * 64,
        passed=passed,
        started_at="2026-07-29T10:00:00+00:00",
        completed_at="2026-07-29T10:00:01+00:00",
        duration_ms=1000 + trial,
        score_checks=[
            ScoreCheck("task_success", "task result", True),
            ScoreCheck("tool_selection", "tool result", True),
            ScoreCheck("security", "security result", True),
            ScoreCheck("communication", "communication result", True),
            ScoreCheck(
                "efficiency",
                "efficiency result",
                efficiency_passed,
            ),
        ],
        final_text="safe answer",
        model_calls=(
            ModelCallEvidence(
                sequence=1,
                status="success",
                started_at="2026-07-29T10:00:00+00:00",
                latency_ms=100,
                message_count=2,
                tool_contract_count=6,
                finish_reason="stop",
                response_id=f"response-{case_id}-{trial}",
                observed_model="deepseek-v4-flash",
                usage={
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                },
                provider_attempts=1,
            ),
        ),
        business_state_delta=BusinessStateDelta(
            changed=False,
            changed_tables=(),
            before_sha256="a" * 64,
            after_sha256="a" * 64,
        ),
    )


def _formal_cases() -> list[ReadonlyEvalCase]:
    return [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"formal-reporting-case-{index:02d}",
                "user_message": f"检查第 {index} 个请求。",
                "expected": {
                    "semantic_contract": {
                        "required_claims": [
                            {
                                "id": f"safe_claim_{index:02d}",
                                "category": "security",
                                "proposition": "回答保持安全",
                            }
                        ],
                        "forbidden_claims": [],
                    }
                },
            }
        )
        for index in range(20)
    ]


def _formal_holdout_evidence(
    settings: Settings | None = None,
) -> FormalHoldoutEvidence:
    return FormalHoldoutEvidence(
        declaration_manifest_sha256="6" * 64,
        lock_start_receipt_sha256="7" * 64,
        declared_harness_sha256=stable_sha256(
            current_readonly_harness_fingerprints(settings)
        ),
    )


def test_create_server_run_id_is_unique_and_url_safe():
    first = create_server_run_id()
    second = create_server_run_id()

    assert first != second
    assert first.startswith("eval-")
    assert "/" not in first
    assert " " not in first
    assert 8 <= len(first) <= 80


def test_clean_git_gate_requires_a_resolved_clean_commit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        readonly_reporting,
        "_git_snapshot",
        lambda: ("a" * 40, False),
    )
    assert readonly_reporting.require_clean_git_worktree() == "a" * 40

    monkeypatch.setattr(
        readonly_reporting,
        "_git_snapshot",
        lambda: ("a" * 40, True),
    )
    with pytest.raises(ValueError, match="clean"):
        readonly_reporting.require_clean_git_worktree()

    monkeypatch.setattr(
        readonly_reporting,
        "_git_snapshot",
        lambda: (None, None),
    )
    with pytest.raises(ValueError, match="Git|commit"):
        readonly_reporting.require_clean_git_worktree()


def test_summary_separates_strict_reliability_safety_usage_and_latency():
    results = [
        _result(case_id="case-a", trial=1, passed=True),
        _result(case_id="case-a", trial=2, passed=True),
        _result(case_id="case-b", trial=1, passed=True),
        _result(
            case_id="case-b",
            trial=2,
            passed=False,
            efficiency_passed=False,
        ),
    ]

    summary = summarize_results(
        run_id="eval-20260729-abcdef12",
        results=results,
        planned_trials=2,
        budget_report=_budget_report(),
    )

    assert summary["total_trials"] == 4
    assert summary["strict"]["passed"] == 3
    assert summary["strict"]["rate"] == 0.75
    assert summary["reliability"]["k"] == 2
    assert summary["reliability"]["cases_all_trials_passed"] == 1
    assert summary["reliability"]["pass_power_k"] == 0.5
    assert summary["security"]["passed"] == 4
    assert summary["security"]["all_trials_passed"] is True
    assert summary["score_layers"]["efficiency"]["passed"] == 3
    assert summary["usage"]["model_calls"] == 4
    assert summary["usage"]["total_tokens"] == 40
    assert summary["latency_ms"]["case"]["p50"] >= 1001
    assert summary["latency_ms"]["model_call"]["max"] == 100
    assert summary["business_state"]["changed_trials"] == 0
    assert summary["budget"]["run"]["committed_cny"] == "0.000048"
    assert summary["budget"]["cumulative"]["hard_limit_cny"] == "20"


def test_result_record_contains_trial_trajectory_without_eval_expectations():
    result = _result(case_id="case-a", trial=1, passed=True)

    record = result_to_record(result, split="dev")

    assert record["case_id"] == "case-a"
    assert record["trial"] == 1
    assert record["split"] == "dev"
    assert record["model_calls"][0]["usage"]["total_tokens"] == 10
    assert record["business_state"]["changed"] is False
    assert record["scores"]["security"] is True
    assert "expected" not in json.dumps(record)
    assert "user_message" not in json.dumps(record)


def test_manifest_fingerprints_harness_and_never_serializes_secret_or_holdout_ids():
    secret = "PRIVATE-MANIFEST-KEY-CANARY"
    settings = Settings(
        deepseek_api_key=secret,
        deepseek_base_url="https://api.deepseek.com/v1?private=query",
        deepseek_model="deepseek-v4-flash",
    )
    cases = _formal_cases()
    results = [
        _result(case_id=case.case_id, trial=trial, passed=True)
        for trial in range(1, 5)
        for case in cases
    ]
    started = datetime.now(UTC)
    completed = started + timedelta(seconds=2)

    manifest = build_readonly_manifest(
        run_id="eval-20260729-abcdef12",
        purpose="holdout_formal",
        split="holdout",
        case_set_name="readonly-holdout-v1",
        cases=cases,
        results=results,
        settings=settings,
        planned_trials=4,
        started_at=started,
        completed_at=completed,
        budget_report=_budget_report(len(results)),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        formal_holdout_evidence=_formal_holdout_evidence(settings),
    )
    serialized = json.dumps(manifest, ensure_ascii=False)

    assert secret not in serialized
    assert "private=query" not in serialized
    assert manifest["model"]["base_url_host"] == "api.deepseek.com"
    assert manifest["schema_version"] == "2.0"
    assert manifest["eval"]["case_count"] == 20
    assert manifest["eval"]["case_set_sha256"]
    assert "case_ids" not in manifest["eval"]
    assert manifest["harness"]["prompt_sha256"]
    assert manifest["harness"]["tool_contracts_sha256"]
    assert manifest["harness"]["policies_sha256"]
    assert manifest["harness"]["agent_loop_sha256"]
    assert manifest["harness"]["model_runtime_sha256"]
    assert manifest["harness"]["semantic_judge_prompt_sha256"]
    assert manifest["harness"]["semantic_judge_source_sha256"]
    assert manifest["harness"]["semantic_calibration_source_sha256"]
    assert manifest["harness"]["semantic_calibration_validator_sha256"]
    assert manifest["harness"]["semantic_calibration_runner_sha256"]
    assert manifest["harness"]["semantic_calibration_corpus_sha256"]
    assert manifest["eval"]["semantic_calibration"] == {
        "report_sha256": "a" * 64,
        "review_sha256": "b" * 64,
        "run_id": "eval-20260729-calibration-v2",
        "source_git_commit": "1" * 40,
        "fixture_sha256": "c" * 64,
        "contract_set_sha256": "d" * 64,
        "harness_sha256": "e" * 64,
        "reviewer_id": "independent-reviewer-v1",
        "reviewed_count": 5,
    }
    assert manifest["eval"]["formal_holdout"] == {
        "declaration_manifest_sha256": "6" * 64,
        "lock_start_receipt_sha256": "7" * 64,
        "declared_harness_sha256": stable_sha256(
            current_readonly_harness_fingerprints(settings)
        ),
    }
    assert (
        manifest["harness"]["runtime_harness_sha256"]
        == manifest["eval"]["formal_holdout"][
            "declared_harness_sha256"
        ]
    )
    assert manifest["source"]["source_tree_sha256"]
    assert manifest["execution"]["planned_trials"] == 4
    assert manifest["execution"]["completed_trials"] == 4
    assert manifest["model"]["observed_models"] == ["deepseek-v4-flash"]
    assert manifest["model"]["generation_config"]["temperature"] == 0.0
    assert manifest["model"]["semantic_judge"] == {
        "version": "atomic-claims-v1",
        "response_format": "json_object",
        "tools_enabled": False,
        "temperature": 0.0,
        "thinking": "disabled",
    }
    assert manifest["eval"]["scorer_version"] == "readonly-scorer-v6"
    assert manifest["budget"]["price_snapshot_sha256"] == "f" * 64
    assert manifest["budget"]["hard_limit_cny"] == "20"

    dev_cases = load_cases()[:2]
    dev_results = [
        _result(case_id=case.case_id, trial=1, passed=True)
        for case in dev_cases
    ]
    dev_manifest = build_readonly_manifest(
        run_id="eval-20260729-abcdef13",
        purpose="dev_repeat",
        split="dev",
        case_set_name="readonly-dev-v1",
        cases=dev_cases,
        results=dev_results,
        settings=settings,
        planned_trials=1,
        started_at=started,
        completed_at=completed,
        budget_report=_budget_report(
            len(dev_results),
            run_id="eval-20260729-abcdef13",
            purpose="dev_repeat",
        ),
    )
    assert dev_manifest["eval"]["case_ids"] == [
        case.case_id for case in dev_cases
    ]
    assert "semantic_calibration" not in dev_manifest["eval"]


def test_formal_manifest_requires_bound_calibration_attestations():
    settings = Settings()
    cases = load_cases()[:1]
    started = datetime.now(UTC)

    try:
        build_readonly_manifest(
            run_id="eval-20260729-missing-calibration",
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            cases=cases,
            results=[
                _result(case_id=cases[0].case_id, trial=1, passed=True)
            ],
            settings=settings,
            planned_trials=1,
            started_at=started,
            completed_at=started + timedelta(seconds=1),
            budget_report=_budget_report(
                run_id="eval-20260729-missing-calibration",
            ),
        )
    except ValueError as exc:
        assert "calibration" in str(exc)
    else:
        raise AssertionError(
            "formal manifest accepted missing calibration attestations"
        )


def test_formal_manifest_rejects_unsettled_budget_and_model_drift():
    settings = Settings(deepseek_model="deepseek-v4-flash")
    cases = _formal_cases()
    results = [
        _result(case_id=case.case_id, trial=trial, passed=True)
        for trial in range(1, 5)
        for case in cases
    ]
    started = datetime.now(UTC)
    common = {
        "run_id": "eval-20260729-formal-gates",
        "purpose": "holdout_formal",
        "split": "holdout",
        "case_set_name": "readonly-holdout-v2",
        "cases": cases,
        "settings": settings,
        "planned_trials": 4,
        "started_at": started,
        "completed_at": started + timedelta(seconds=1),
        "calibration_attestation": _attestation(),
        "calibration_review": _review(),
        "formal_holdout_evidence": _formal_holdout_evidence(),
    }
    unsettled = _budget_report(
        len(results),
        run_id="eval-20260729-formal-gates",
    )
    unsettled["run"]["uncertain_count"] = 1

    try:
        build_readonly_manifest(
            **common,
            results=results,
            budget_report=unsettled,
        )
    except ValueError as exc:
        assert "budget" in str(exc)
    else:
        raise AssertionError("formal manifest accepted unsettled budget")

    drifted = deepcopy(results)
    drifted[0].model_calls = (
        replace(
            drifted[0].model_calls[0],
            observed_model="different-model",
        ),
    )
    try:
        build_readonly_manifest(
            **common,
            results=drifted,
            budget_report=_budget_report(
                len(results),
                run_id="eval-20260729-formal-gates",
            ),
        )
    except ValueError as exc:
        assert "model" in str(exc)
    else:
        raise AssertionError("formal manifest accepted model drift")


def test_formal_manifest_recomputes_exact_cost_from_every_model_call():
    settings = Settings(deepseek_model="deepseek-v4-flash")
    cases = _formal_cases()
    results = [
        _result(case_id=case.case_id, trial=trial, passed=True)
        for trial in range(1, 5)
        for case in cases
    ]
    started = datetime.now(UTC)
    common = {
        "run_id": "eval-20260729-formal-cost",
        "purpose": "holdout_formal",
        "split": "holdout",
        "case_set_name": "readonly-holdout-v2",
        "cases": cases,
        "results": results,
        "settings": settings,
        "planned_trials": 4,
        "started_at": started,
        "completed_at": started + timedelta(seconds=1),
        "calibration_attestation": _attestation(),
        "calibration_review": _review(),
        "formal_holdout_evidence": _formal_holdout_evidence(),
    }
    overstated = _budget_report(
        len(results),
        run_id="eval-20260729-formal-cost",
    )
    for scope in ("run", "cumulative"):
        overstated[scope]["committed_cny"] = "17"
        overstated[scope]["settled_cny"] = "17"
        overstated[scope]["remaining_execution_cny"] = "1"

    with pytest.raises(ValueError, match="cost|usage|budget"):
        build_readonly_manifest(
            **common,
            budget_report=overstated,
        )

    retried = deepcopy(results)
    retried[0].model_calls = (
        replace(
            retried[0].model_calls[0],
            provider_attempts=2,
        ),
    )
    retry_budget = _budget_report(
        len(results) + 1,
        run_id="eval-20260729-formal-cost",
    )
    with pytest.raises(ValueError, match="attempt|cost|usage"):
        build_readonly_manifest(
            **{**common, "results": retried},
            budget_report=retry_budget,
        )


def test_formal_bundle_schema_recomputes_cost_instead_of_trusting_summary():
    settings = Settings(deepseek_model="deepseek-v4-flash")
    cases = _formal_cases()
    results = [
        _result(case_id=case.case_id, trial=trial, passed=True)
        for trial in range(1, 5)
        for case in cases
    ]
    budget = _budget_report(
        len(results),
        run_id="eval-20260729-formal-schema-cost",
    )
    started = datetime.now(UTC)
    manifest = build_readonly_manifest(
        run_id="eval-20260729-formal-schema-cost",
        purpose="holdout_formal",
        split="holdout",
        case_set_name="readonly-holdout-v2",
        cases=cases,
        results=results,
        settings=settings,
        planned_trials=4,
        started_at=started,
        completed_at=started + timedelta(seconds=1),
        budget_report=budget,
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        formal_holdout_evidence=_formal_holdout_evidence(),
    )
    manifest["source"]["git_commit"] = "a" * 40
    manifest["source"]["git_dirty"] = False
    manifest["artifacts"] = {
        "cases": "cases.jsonl",
        "summary": "summary.json",
        "trajectories": "trajectories/",
        "integrity": "integrity.json",
    }
    records = [
        result_to_record(result, split="holdout")
        for result in results
    ]
    payload = {
        "manifest": manifest,
        "cases": records,
        "summary": summarize_results(
            run_id="eval-20260729-formal-schema-cost",
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
    mismatched_trajectory = deepcopy(payload)
    mismatched_trajectory["trajectories"][0]["final_text"] = (
        "forged trajectory content"
    )
    with pytest.raises(ValueError, match="Case|trajectory|differ"):
        validate_readonly_payload(mismatched_trajectory)

    mismatched_execution_limit = deepcopy(payload)
    mismatched_execution_limit["manifest"]["budget"][
        "execution_limit_cny"
    ] = "17"
    with pytest.raises(ValueError, match="budget|limit|differ"):
        validate_readonly_payload(mismatched_execution_limit)

    mismatched_reservation = deepcopy(payload)
    mismatched_reservation["manifest"]["budget"][
        "reservation_cny_per_attempt"
    ] = "0.5"
    with pytest.raises(ValueError, match="budget|reservation|differ"):
        validate_readonly_payload(mismatched_reservation)

    for scope in ("run", "cumulative"):
        payload["summary"]["budget"][scope]["committed_cny"] = "17"
        payload["summary"]["budget"][scope]["settled_cny"] = "17"
        payload["summary"]["budget"][scope][
            "remaining_execution_cny"
        ] = "1"

    with pytest.raises(ValueError, match="cost|usage|[Ff]ormal"):
        validate_readonly_payload(payload)
