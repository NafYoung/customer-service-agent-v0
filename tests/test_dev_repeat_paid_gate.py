from __future__ import annotations

import shutil
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.deepseek_budget import (
    calculate_usage_cost_from_rates,
    format_cny,
)
from app.agent.openai_compatible import AssistantTurn
from app.config import Settings
from evals import holdout_lock as holdout_protocol
from evals import run_readonly_agent_evals as runner
from evals.calibration_attestation import (
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
)
from evals.canonical_pricing import (
    canonical_budget_price_payload,
    canonical_worst_case_attempt_reservation_cny,
    load_canonical_price_snapshot,
)
from evals.evidence import (
    BusinessStateDelta,
    ModelCallEvidence,
    stable_sha256,
    write_eval_bundle,
)
from evals.evidence_schema import (
    BudgetSummary,
    ModelCallRecord,
    validate_readonly_payload,
)
from evals.readonly_eval import (
    DEFAULT_CASE_DIR,
    ReadonlyEvalCase,
    ReadonlyEvalResult,
    ScoreCheck,
    load_cases,
)
from evals.readonly_reporting import (
    build_readonly_manifest,
    result_to_record,
    summarize_results,
)

ROOT = Path(__file__).resolve().parents[1]
REGRESSION_CASE_DIR = ROOT / "evals" / "readonly_regression_cases"
USAGE = {
    "prompt_tokens": 8,
    "completion_tokens": 2,
    "total_tokens": 10,
}


def _settings() -> Settings:
    return Settings(
        deepseek_api_key=None,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-flash",
        deepseek_max_tokens=1024,
        deepseek_temperature=0.0,
    )


def _model_call(
    *,
    case_id: str,
    trial: int,
    phase: str = "agent",
    tool_contract_count: int = 6,
    usage: dict[str, int] | None = USAGE,
    provider_attempts: int = 1,
) -> ModelCallEvidence:
    return ModelCallEvidence(
        sequence=1,
        status="success",
        started_at="2026-07-29T12:00:00+00:00",
        latency_ms=1,
        message_count=2,
        tool_contract_count=tool_contract_count,
        phase=phase,
        finish_reason="stop",
        response_id=f"response-{phase}-{case_id}-{trial}",
        observed_model="deepseek-v4-flash",
        usage=usage,
        provider_attempts=provider_attempts,
    )


def _result(*, case_id: str, trial: int) -> ReadonlyEvalResult:
    score_checks = [
        ScoreCheck(category, f"{category} passed", True)
        for category in (
            "task_success",
            "tool_selection",
            "security",
            "communication",
            "efficiency",
        )
    ]
    return ReadonlyEvalResult(
        case_id=case_id,
        trial=trial,
        case_run_id=f"eval-run-{case_id}-{trial}",
        input_sha256="0" * 64,
        passed=True,
        started_at="2026-07-29T12:00:00+00:00",
        completed_at="2026-07-29T12:00:01+00:00",
        duration_ms=1,
        checks=[check.message for check in score_checks],
        score_checks=score_checks,
        final_text="safe answer",
        model_calls=(
            _model_call(case_id=case_id, trial=trial),
            _model_call(
                case_id=case_id,
                trial=trial,
                phase="semantic_judge",
                tool_contract_count=0,
            ),
        ),
        business_state_delta=BusinessStateDelta(
            changed=False,
            changed_tables=(),
            before_sha256="a" * 64,
            after_sha256="a" * 64,
        ),
    )


def _dev_repeat_inputs() -> tuple[list, list[ReadonlyEvalResult]]:
    cases = load_cases(REGRESSION_CASE_DIR)
    results = [
        _result(case_id=case.case_id, trial=trial)
        for trial in range(1, 5)
        for case in cases
    ]
    return cases, results


def _attempt_count(results: list[ReadonlyEvalResult]) -> int:
    return sum(len(result.model_calls) for result in results)


class _CountingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def complete(self, **_: object) -> AssistantTurn:
        self.calls += 1
        return AssistantTurn(
            content="safe answer",
            tool_calls=(),
            finish_reason="stop",
            usage=dict(USAGE),
            response_id=f"response-{self.calls}",
            model="deepseek-v4-flash",
            provider_attempts=1,
        )

    def close(self) -> None:
        self.closed = True


def _paid_budget(
    *,
    run_id: str,
    purpose: str,
    attempt_count: int,
) -> dict:
    settings = _settings()
    price = load_canonical_price_snapshot()
    usage_cost = calculate_usage_cost_from_rates(
        rates_cny=price.rates_cny.model_dump(),
        tokens_per_price_unit=price.tokens_per_price_unit,
        usage=USAGE,
    )
    per_attempt = usage_cost.units
    settled = format_cny(per_attempt * attempt_count)
    remaining = format(
        Decimal("18") - Decimal(settled),
        "f",
    )
    amount = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": settled,
        "settled_cny": settled,
        "remaining_execution_cny": remaining,
        "attempt_count": attempt_count,
        "reserved_count": 0,
        "uncertain_count": 0,
    }
    reservation = canonical_worst_case_attempt_reservation_cny(
        canonical_price=price,
        max_output_tokens=settings.deepseek_max_tokens,
    )
    bucket = {
        "status": (
            "settled_exact"
            if usage_cost.mode == "exact"
            else "settled_upper_bound"
        ),
        "settlement_mode": usage_cost.mode,
        "reserved_cny": reservation,
        "known_cost_cny": format_cny(per_attempt),
        "count": attempt_count,
    }
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
        "run_status": "completed",
        "run_identity": {
            "run_id": run_id,
            "purpose": purpose,
            "model": settings.deepseek_model,
            "price_sha256": price.sha256,
            "status": "completed",
            "started_at": "2026-07-29T12:00:00+00:00",
            "completed_at": "2026-07-29T12:05:00+00:00",
        },
        "price": canonical_budget_price_payload(price),
        "reservation_cny_per_attempt": reservation,
        "run": dict(amount),
        "cumulative": dict(amount),
        "attempt_evidence": {
            "run": [dict(bucket)],
            "cumulative": [dict(bucket)],
        },
    }


def _dev_repeat_payload() -> dict:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-public-binding"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=_attempt_count(results),
    )
    started = datetime(2026, 7, 29, 12, tzinfo=UTC)
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
    records = [
        result_to_record(result, split="dev")
        for result in results
    ]
    return {
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


def _write_dev_repeat_bundle(
    tmp_path: Path,
    payload: dict,
) -> Path:
    manifest = payload["manifest"]
    output_root = tmp_path / "private-regression"
    output_root.mkdir(mode=0o700)
    return write_eval_bundle(
        output_root=output_root,
        run_id=manifest["run_id"],
        manifest=manifest,
        case_records=payload["cases"],
        summary=payload["summary"],
    )


def _trust_current_test_source(
    monkeypatch: pytest.MonkeyPatch,
    source_git_commit: str,
) -> None:
    monkeypatch.setattr(
        holdout_protocol,
        "require_clean_git_worktree",
        lambda **_: source_git_commit,
    )


def test_programmatic_formal_run_requires_validated_context_before_model_call(
    tmp_path: Path,
) -> None:
    model = _CountingModel()
    case = load_cases(REGRESSION_CASE_DIR)[:1]

    with pytest.raises(ValueError, match="validated formal"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=case,
            run_id="eval-20260729-formal-no-context",
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=tmp_path,
        )

    assert model.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_programmatic_formal_run_rejects_forged_context_before_model_call(
    tmp_path: Path,
) -> None:
    model = _CountingModel()

    with pytest.raises(ValueError, match="validated formal"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=load_cases(REGRESSION_CASE_DIR)[:1],
            run_id="eval-20260729-formal-forged-context",
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=tmp_path,
            formal_run_context=object(),
        )

    assert model.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_issued_formal_context_binds_fixed_output_root_before_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"sealed-formal-case-{index:02d}",
                "user_message": f"formal case {index}",
                "expected": {
                    "semantic_contract": {
                        "required_claims": [
                            {
                                "id": f"claim_{index:02d}",
                                "category": "task_success",
                                "proposition": "回答安全",
                            }
                        ],
                        "forbidden_claims": [],
                    }
                },
            }
        )
        for index in range(20)
    ]
    source_git_commit = "a" * 40
    source_tree_sha256 = "b" * 64
    fingerprints = {"test_harness": "c" * 64}
    harness_sha256 = stable_sha256(fingerprints)
    attestation = ValidatedCalibrationAttestation(
        report_sha256="d" * 64,
        run_id="eval-20260729-calibration-output-binding",
        source_git_commit=source_git_commit,
        fixture_sha256="e" * 64,
        contract_set_sha256="f" * 64,
        harness_sha256="1" * 64,
        result_count=49,
        fixture_ids=tuple(f"fixture-{index:02d}" for index in range(49)),
        fixture_kinds=tuple(
            (f"fixture-{index:02d}", "safe_canonical")
            for index in range(49)
        ),
        completed_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )
    review = ValidatedCalibrationReview(
        review_sha256="2" * 64,
        reviewer_id="independent-reviewer-v1",
        reviewed_count=5,
    )
    regression_gate = holdout_protocol.ValidatedRegressionGate(
        bundle_path=tmp_path / "private" / "regression",
        bundle_integrity_sha256="3" * 64,
        gate_sha256="4" * 64,
        run_id="eval-20260729-dev-repeat-output-binding",
        source_git_commit=source_git_commit,
        case_set_name="readonly-regression-v1",
        case_set_sha256="5" * 64,
        harness_sha256=harness_sha256,
        runtime_identity_sha256="6" * 64,
        passed_trials=28,
    )
    declaration = holdout_protocol.HoldoutDeclaration(
        case_set_name="readonly-holdout-v2",
        case_set_sha256=runner._formal_case_set_sha256(cases),
        manifest_sha256="7" * 64,
        source_git_commit=source_git_commit,
        scorer_version="readonly-agent-v1",
        calibration_report_sha256=attestation.report_sha256,
        calibration_review_sha256=review.review_sha256,
        calibration_run_id=attestation.run_id,
        calibration_source_git_commit=attestation.source_git_commit,
        calibration_fixture_sha256=attestation.fixture_sha256,
        calibration_contract_set_sha256=attestation.contract_set_sha256,
        calibration_harness_sha256=attestation.harness_sha256,
        calibration_reviewer_id=review.reviewer_id,
        calibration_reviewed_count=review.reviewed_count,
        harness_sha256=harness_sha256,
        regression_bundle_integrity_sha256=(
            regression_gate.bundle_integrity_sha256
        ),
        regression_gate_sha256=regression_gate.gate_sha256,
        regression_run_id=regression_gate.run_id,
        regression_source_git_commit=regression_gate.source_git_commit,
        regression_case_set_name=regression_gate.case_set_name,
        regression_case_set_sha256=regression_gate.case_set_sha256,
        regression_harness_sha256=regression_gate.harness_sha256,
    )
    private_root = tmp_path / "private"
    fixed_output_root = private_root / "eval-runs"
    fixed_lock_root = private_root / "holdout" / "formal-run-locks"
    monkeypatch.setattr(runner, "DEFAULT_OUTPUT_ROOT", fixed_output_root)
    monkeypatch.setattr(
        runner,
        "DEFAULT_HOLDOUT_LOCK_ROOT",
        fixed_lock_root,
    )
    acquired_lock = holdout_protocol.acquire_holdout_run_lock_with_hash(
        lock_root=fixed_lock_root,
        declaration=declaration,
        run_id="eval-20260729-formal-output-binding",
    )
    frozen_harness = SimpleNamespace(fingerprints=fingerprints)
    context = runner._create_validated_formal_run_context(
        run_id="eval-20260729-formal-output-binding",
        purpose="holdout_formal",
        split="holdout",
        cases=cases,
        case_set_name="readonly-holdout-v2",
        trials=4,
        source_git_commit=source_git_commit,
        source_tree_sha256=source_tree_sha256,
        frozen_harness=frozen_harness,
        calibration_attestation=attestation,
        calibration_review=review,
        declaration=declaration,
        regression_gate=regression_gate,
        acquired_lock=acquired_lock,
    )
    model = _CountingModel()
    wrong_output_root = tmp_path / "attacker-output"

    assert context.output_root == fixed_output_root
    with pytest.raises(ValueError, match="validated formal"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=cases,
            run_id=context.run_id,
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=wrong_output_root,
            calibration_attestation=attestation,
            calibration_review=review,
            formal_holdout_evidence=runner.FormalHoldoutEvidence(
                declaration_manifest_sha256=(
                    declaration.manifest_sha256
                ),
                lock_start_receipt_sha256=acquired_lock.receipt_sha256,
                declared_harness_sha256=harness_sha256,
                regression_bundle_integrity_sha256=(
                    regression_gate.bundle_integrity_sha256
                ),
                regression_gate_sha256=regression_gate.gate_sha256,
                regression_run_id=regression_gate.run_id,
                regression_source_git_commit=(
                    regression_gate.source_git_commit
                ),
                regression_case_set_name=regression_gate.case_set_name,
                regression_case_set_sha256=(
                    regression_gate.case_set_sha256
                ),
                regression_harness_sha256=(
                    regression_gate.harness_sha256
                ),
            ),
            frozen_harness=frozen_harness,
            source_git_commit=source_git_commit,
            source_tree_sha256=source_tree_sha256,
            formal_run_context=context,
        )

    assert model.calls == 0
    assert not wrong_output_root.exists()


@pytest.mark.parametrize("attack", ["forged_sentinel", "case_hash"])
def test_programmatic_formal_context_rejects_internal_binding_attacks(
    tmp_path: Path,
    attack: str,
) -> None:
    model = _CountingModel()
    cases = load_cases(REGRESSION_CASE_DIR)[:1]
    context = runner.ValidatedFormalRunContext(
        run_id="eval-20260729-formal-context-attack",
        purpose="holdout_formal",
        split="holdout",
        case_set_name="readonly-holdout-v2",
        case_set_sha256=(
            "f" * 64
            if attack == "case_hash"
            else runner._formal_case_set_sha256(cases)
        ),
        planned_case_count=1,
        planned_trials=4,
        source_git_commit="a" * 40,
        source_tree_sha256="b" * 64,
        harness_sha256="c" * 64,
        calibration_report_sha256="d" * 64,
        calibration_review_sha256="e" * 64,
        regression_bundle_integrity_sha256="1" * 64,
        regression_gate_sha256="2" * 64,
        regression_run_id="eval-20260729-dev-repeat-public-binding",
        regression_source_git_commit="a" * 40,
        regression_case_set_name="readonly-regression-v1",
        regression_case_set_sha256="3" * 64,
        regression_harness_sha256="c" * 64,
        declaration_manifest_sha256="4" * 64,
        lock_start_path=tmp_path / "readonly-holdout-v2.start.json",
        lock_start_receipt_sha256="5" * 64,
        _sentinel=(
            object()
            if attack == "forged_sentinel"
            else runner._FORMAL_CONTEXT_SENTINEL
        ),
    )

    with pytest.raises(ValueError, match="validated formal"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=cases,
            run_id="eval-20260729-formal-context-attack",
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=tmp_path,
            formal_run_context=context,
        )

    assert model.calls == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "attack",
    [
        "missing_trial",
        "security_failure",
        "changed_state",
        "stale_source",
        "stale_harness",
    ],
)
def test_formal_regression_gate_rejects_noncanonical_public_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"][
        "runtime_harness_sha256"
    ]
    if attack == "missing_trial":
        payload["cases"].pop()
        payload["trajectories"].pop()
    elif attack == "security_failure":
        payload["summary"]["security"]["passed"] = 27
        payload["summary"]["security"]["failed"] = 1
        payload["summary"]["security"]["rate"] = 27 / 28
        payload["summary"]["security"]["all_trials_passed"] = False
    elif attack == "changed_state":
        payload["summary"]["business_state"]["changed_trials"] = 1
        payload["summary"]["business_state"]["all_trials_unchanged"] = False
    elif attack == "stale_source":
        payload["manifest"]["source"]["git_commit"] = "b" * 40
    elif attack == "stale_harness":
        payload["manifest"]["harness"]["runtime_harness_sha256"] = "b" * 64
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )


def test_formal_regression_gate_accepts_only_verified_28_of_28_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"][
        "runtime_harness_sha256"
    ]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    gate = holdout_protocol.validate_regression_gate(
        bundle_path=bundle_path,
        private_root=tmp_path,
        source_git_commit=source_git_commit,
        harness_sha256=expected_harness,
    )

    assert gate.run_id == payload["manifest"]["run_id"]
    assert gate.source_git_commit == source_git_commit
    assert gate.case_set_name == "readonly-regression-v1"
    assert gate.passed_trials == 28


@pytest.mark.parametrize(
    ("field_path", "forged_value"),
    [
        (("source", "source_tree_sha256"), "f" * 64),
        (("source", "python_version"), "0.0.0-forged"),
        (("source", "platform"), "forged-platform"),
        (("source", "package_versions"), {"forged": "1.0"}),
        (("eval", "scorer_version"), "forged-scorer"),
        (("eval", "scorer_sha256"), "f" * 64),
        (("harness", "prompt_sha256"), "f" * 64),
        (("harness", "tool_contracts_sha256"), "f" * 64),
        (("harness", "policies_sha256"), "f" * 64),
        (("harness", "seed_data_sha256"), "f" * 64),
        (("harness", "agent_loop_sha256"), "f" * 64),
        (("harness", "model_runtime_sha256"), "f" * 64),
        (("harness", "semantic_judge_version"), "forged-judge"),
        (("harness", "semantic_judge_prompt_sha256"), "f" * 64),
        (("harness", "semantic_judge_source_sha256"), "f" * 64),
        (
            ("harness", "semantic_calibration_source_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_validator_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_runner_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_corpus_sha256"),
            "f" * 64,
        ),
        (("harness", "evidence_protocol_sha256"), "f" * 64),
        (
            ("harness", "canonical_price_snapshot_sha256"),
            "f" * 64,
        ),
        (("harness", "max_tool_rounds"), 99),
        (("harness", "max_tool_calls"), 99),
        (("model", "provider"), "forged-provider"),
        (("model", "requested_model"), "forged-model"),
        (("model", "observed_models"), ["forged-model"]),
        (("model", "base_url_host"), "attacker.example"),
        (("model", "generation_config", "temperature"), 0.5),
        (("model", "generation_config", "seed"), 7),
        (("model", "generation_config", "max_tokens"), 2048),
        (("model", "timeout_seconds"), 99),
        (("model", "retry_policy", "max_retries"), 99),
        (("model", "retry_policy", "backoff"), "forged-backoff"),
        (("model", "semantic_judge", "version"), "forged-judge"),
        (("model", "semantic_judge", "temperature"), 0.5),
    ],
)
def test_formal_regression_gate_rejects_self_attested_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_path: tuple[str, ...],
    forged_value: object,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"][
        "runtime_harness_sha256"
    ]
    target = payload["manifest"]
    for field_name in field_path[:-1]:
        nested = target[field_name]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = forged_value
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression|runtime|source|model",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )


def test_formal_regression_gate_rejects_renamed_or_replaced_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"][
        "runtime_harness_sha256"
    ]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)
    renamed_path = bundle_path.with_name("renamed-regression-bundle")
    bundle_path.rename(renamed_path)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=renamed_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )


@pytest.mark.parametrize(
    ("purpose", "source_dir", "case_set_name", "truncate"),
    [
        (
            "diagnostic",
            DEFAULT_CASE_DIR,
            "readonly-dev-v1",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "readonly-regression-v1",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "wrong-regression-name",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "readonly-regression-v1",
            True,
        ),
    ],
)
def test_nonformal_paid_cli_rejects_noncanonical_case_identity_before_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    source_dir: Path,
    case_set_name: str,
    truncate: bool,
) -> None:
    case_dir = source_dir
    if not truncate and case_set_name in {
        "readonly-dev-v1",
        "readonly-regression-v1",
    }:
        case_dir = tmp_path / f"external-{purpose}"
        shutil.copytree(source_dir, case_dir)
    original_load_cases = runner.load_cases
    if truncate:
        monkeypatch.setattr(
            runner,
            "load_cases",
            lambda path: original_load_cases(path)[:-1],
        )
    monkeypatch.setattr(runner, "Settings", _settings)
    monkeypatch.setattr(
        runner,
        "freeze_readonly_harness",
        lambda settings: object(),
    )
    reached = {"budget": 0}

    def reject_budget(**kwargs):
        reached["budget"] += 1
        raise ValueError("budget guard must not be reached")

    monkeypatch.setattr(
        runner,
        "build_deepseek_budget_guard",
        reject_budget,
    )

    status = runner.main(
        [
            "--run-id",
            f"eval-20260729-{purpose}-gate",
            "--purpose",
            purpose,
            "--split",
            "dev",
            "--case-dir",
            str(case_dir),
            "--case-set-name",
            case_set_name,
            "--trials",
            "4" if purpose == "dev_repeat" else "1",
            "--output-root",
            str(tmp_path / "output"),
        ]
    )

    assert status == 2
    assert reached == {"budget": 0}


@pytest.mark.parametrize("attack", ["absolute", "traversal"])
def test_cli_rejects_unsafe_run_id_before_output_path_probe(
    tmp_path: Path,
    attack: str,
) -> None:
    outside = tmp_path / "outside-existing"
    outside.mkdir()
    run_id = (
        str(outside)
        if attack == "absolute"
        else "../outside-existing"
    )

    with pytest.raises(SystemExit):
        runner.main(
            [
                "--run-id",
                run_id,
                "--output-root",
                str(tmp_path / "output"),
            ]
        )


@pytest.mark.parametrize(
    ("purpose", "case_set_name", "trials"),
    [
        ("diagnostic", "arbitrary-diagnostic-v1", 1),
        ("dev_repeat", "arbitrary-repeat-v1", 4),
        ("unknown-purpose", "arbitrary-unknown-v1", 1),
    ],
)
def test_programmatic_runner_rejects_invalid_scope_before_model_calls(
    tmp_path: Path,
    purpose: str,
    case_set_name: str,
    trials: int,
) -> None:
    model = _CountingModel()
    output_root = tmp_path / "output"
    arbitrary_case = ReadonlyEvalCase.model_validate(
        {
            "case_id": "arbitrary-programmatic-case",
            "user_message": "arbitrary",
            "expected": {},
        }
    )

    with pytest.raises(ValueError, match="purpose|canonical|case"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=[arbitrary_case],
            run_id="eval-20260729-programmatic-preflight",
            purpose=purpose,
            split="dev",
            case_set_name=case_set_name,
            trials=trials,
            output_root=output_root,
        )

    assert model.calls == 0
    assert not output_root.exists()


def test_diagnostic_manifest_and_schema_reject_noncanonical_identity() -> None:
    arbitrary_case = ReadonlyEvalCase.model_validate(
        {
            "case_id": "arbitrary-diagnostic-case",
            "user_message": "arbitrary",
            "expected": {},
        }
    )
    result = _result(case_id=arbitrary_case.case_id, trial=1)
    started = datetime(2026, 7, 29, 12, tzinfo=UTC)

    with pytest.raises(ValueError, match="canonical|case"):
        build_readonly_manifest(
            run_id="eval-20260729-diagnostic-builder-forgery",
            purpose="diagnostic",
            split="dev",
            case_set_name="arbitrary-diagnostic-v1",
            cases=[arbitrary_case],
            results=[result],
            settings=_settings(),
            planned_trials=1,
            started_at=started,
            completed_at=started + timedelta(minutes=1),
        )

    canonical_cases = load_cases(DEFAULT_CASE_DIR)
    canonical_results = [
        _result(case_id=case.case_id, trial=1)
        for case in canonical_cases
    ]
    for result in canonical_results:
        result.model_calls = (
            replace(
                result.model_calls[0],
                observed_model="offline-eval-model",
                provider_attempts=0,
            ),
        )
    manifest = build_readonly_manifest(
        run_id="eval-20260729-diagnostic-schema-forgery",
        purpose="diagnostic",
        split="dev",
        case_set_name="readonly-dev-v1",
        cases=canonical_cases,
        results=canonical_results,
        settings=_settings(),
        planned_trials=1,
        started_at=started,
        completed_at=started + timedelta(minutes=1),
    )
    manifest["eval"]["case_set_name"] = "arbitrary-diagnostic-v1"
    manifest["artifacts"] = {
        "cases": "cases.jsonl",
        "summary": "summary.json",
        "trajectories": "trajectories/",
        "integrity": "integrity.json",
    }
    records = [
        result_to_record(item, split="dev")
        for item in canonical_results
    ]
    payload = {
        "manifest": manifest,
        "cases": records,
        "summary": summarize_results(
            run_id=manifest["run_id"],
            results=canonical_results,
            planned_trials=1,
        ),
        "trajectories": deepcopy(records),
        "integrity": {
            "schema_version": "1.0",
            "algorithm": "sha256",
            "files": {},
        },
    }

    with pytest.raises(ValueError, match="diagnostic|canonical|case"):
        validate_readonly_payload(payload)


def test_dev_repeat_manifest_accepts_only_canonical_7_by_4_case_set() -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-valid"
    started = datetime(2026, 7, 29, 12, tzinfo=UTC)

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
        ),
    )

    assert manifest["eval"]["case_count"] == 7
    assert (
        manifest["eval"]["case_set_sha256"]
        == "6340394c8edd5d95c2756f3f4753d4e224682b7f84a445c76b3abb675bad2edb"
    )


def test_public_validator_recomputes_dev_repeat_bucket_costs() -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-public"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=_attempt_count(results),
    )
    started = datetime(2026, 7, 29, 12, tzinfo=UTC)
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
    records = [
        result_to_record(result, split="dev")
        for result in results
    ]
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
        forged["summary"]["budget"]["attempt_evidence"][scope][0][
            "known_cost_cny"
        ] = format(forged_cost, "f")
        forged["summary"]["budget"][scope][
            "committed_cny"
        ] = format(forged_total, "f")
        forged["summary"]["budget"][scope][
            "settled_cny"
        ] = format(forged_total, "f")
        forged["summary"]["budget"][scope][
            "remaining_execution_cny"
        ] = format(Decimal("18") - forged_total, "f")

    with pytest.raises(ValueError, match="attempt|bucket|cost|record"):
        validate_readonly_payload(forged)


def test_dev_repeat_payload_cannot_be_relabelled_as_diagnostic() -> None:
    payload = _dev_repeat_payload()
    payload["manifest"]["purpose"] = "diagnostic"
    payload["summary"]["budget"]["run_identity"][
        "purpose"
    ] = "diagnostic"

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
            budget["attempt_evidence"][scope][0][
                "known_cost_cny"
            ] = format(mismatched_cost, "f")
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

    started = datetime(2026, 7, 29, 12, tzinfo=UTC)
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
    )
    if attack == "move_calls_between_trials":
        moved = results[0].model_calls
        results[0].model_calls = ()
        results[1].model_calls = (*moved, *results[1].model_calls)
    elif attack == "agent_phase_missing":
        results[0].model_calls = tuple(
            replace(call, phase="semantic_judge")
            for call in results[0].model_calls
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
                started_at="2026-07-29T11:59:59+00:00",
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

    started = datetime(2026, 7, 29, 12, tzinfo=UTC)
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


def _budget_with_attempt_evidence() -> dict:
    return _paid_budget(
        run_id="eval-20260729-attempt-evidence",
        purpose="dev_repeat",
        attempt_count=1,
    )


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
        budget["attempt_evidence"]["run"][0][
            "reserved_cny"
        ] = "1.5"
    elif attack == "run_not_in_cumulative":
        budget["attempt_evidence"]["cumulative"][0][
            "known_cost_cny"
        ] = "0.000013"
        budget["cumulative"]["committed_cny"] = "0.000013"
        budget["cumulative"]["settled_cny"] = "0.000013"
        budget["cumulative"]["remaining_execution_cny"] = "17.999987"
        budget["run"]["remaining_execution_cny"] = "17.999987"
    elif attack == "duplicate_run_not_in_cumulative":
        canonical_bucket = deepcopy(
            budget["attempt_evidence"]["run"][0]
        )
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
