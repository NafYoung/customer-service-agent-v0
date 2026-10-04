from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals import holdout_lock as holdout_protocol
from evals import run_readonly_agent_evals as runner
from evals.calibration_attestation import (
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
)
from evals.evidence import stable_sha256
from evals.readonly_eval import ReadonlyEvalCase, load_cases
from tests.paid_gate.helpers import (
    REGRESSION_CASE_DIR,
    _assert_zero_budget_attempts,
    _CountingModel,
    _formal_budget_guard,
    _settings,
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

@pytest.mark.parametrize(
    "output_attack",
    [
        "alternate_root",
        "symlink_escape",
        "source_drift",
        "runtime_drift",
    ],
)
def test_issued_formal_context_binds_output_and_source_before_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_attack: str,
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
    source_snapshot = {
        "git_commit": source_git_commit,
        "git_dirty": False,
        "source_tree_sha256": source_tree_sha256,
        "python_version": "3.11-test",
        "platform": "test-platform",
        "package_versions": {"httpx": "test"},
    }
    source_identity_sha256 = stable_sha256(source_snapshot)
    canonical_settings = replace(
        _settings(),
        deepseek_api_key="test-only-placeholder",
    )
    frozen_harness = runner.freeze_readonly_harness(canonical_settings)
    fingerprints = dict(frozen_harness.fingerprints)
    harness_sha256 = stable_sha256(fingerprints)
    runtime_identity_sha256 = stable_sha256(
        {
            "source": source_snapshot,
            "harness": runner.readonly_harness_snapshot(
                settings=canonical_settings,
                fingerprints=fingerprints,
            ),
            "model": runner.readonly_model_snapshot(
                settings=canonical_settings,
                observed_models=[canonical_settings.deepseek_model],
            ),
        }
    )
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
            (f"fixture-{index:02d}", "safe_canonical") for index in range(49)
        ),
        completed_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
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
        source_tree_sha256=source_tree_sha256,
        source_identity_sha256=source_identity_sha256,
        runtime_identity_sha256=runtime_identity_sha256,
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
        regression_bundle_integrity_sha256=(regression_gate.bundle_integrity_sha256),
        regression_gate_sha256=regression_gate.gate_sha256,
        regression_run_id=regression_gate.run_id,
        regression_source_git_commit=regression_gate.source_git_commit,
        regression_case_set_name=regression_gate.case_set_name,
        regression_case_set_sha256=regression_gate.case_set_sha256,
        regression_harness_sha256=regression_gate.harness_sha256,
        regression_source_tree_sha256=(regression_gate.source_tree_sha256),
        regression_source_identity_sha256=(regression_gate.source_identity_sha256),
        regression_runtime_identity_sha256=(regression_gate.runtime_identity_sha256),
    )
    private_root = tmp_path / "private"
    fixed_output_root = private_root / "eval-runs"
    fixed_lock_root = private_root / "holdout" / "formal-run-locks"
    outside_output_root = tmp_path / "outside-output"
    if output_attack == "symlink_escape":
        private_root.mkdir(mode=0o700)
        outside_output_root.mkdir(mode=0o700)
        fixed_output_root.symlink_to(
            outside_output_root,
            target_is_directory=True,
        )
    monkeypatch.setattr(runner, "DEFAULT_OUTPUT_ROOT", fixed_output_root)
    monkeypatch.setattr(runner, "PRIVATE_ARTIFACT_ROOT", private_root)
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
    budget_guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-output-binding-budget",
        settings=canonical_settings,
    )
    model = runner.build_deepseek_client(
        canonical_settings,
        budget_guard=budget_guard,
    )
    budget_report_provider = budget_guard.snapshot
    capability = runner._create_validated_formal_execution_capability(
        run_id="eval-20260729-formal-output-binding",
        purpose="holdout_formal",
        split="holdout",
        cases=cases,
        case_set_name="readonly-holdout-v2",
        trials=4,
        source_git_commit=source_git_commit,
        source_tree_sha256=source_tree_sha256,
        settings=canonical_settings,
        model=model,
        semantic_judge_model=model,
        budget_guard=budget_guard,
        budget_report_provider=budget_report_provider,
        frozen_harness=frozen_harness,
        calibration_attestation=attestation,
        calibration_review=review,
        declaration=declaration,
        regression_gate=regression_gate,
        acquired_lock=acquired_lock,
    )
    context = capability.context
    requested_output_root = (
        fixed_output_root
        if output_attack in {"symlink_escape", "source_drift", "runtime_drift"}
        else tmp_path / "attacker-output"
    )
    if output_attack == "source_drift":
        monkeypatch.setattr(
            runner,
            "current_readonly_source_snapshot",
            lambda: {
                "git_commit": source_git_commit,
                "git_dirty": False,
                "source_tree_sha256": source_tree_sha256,
                "python_version": "forged",
                "platform": "forged",
                "package_versions": {"httpx": "forged"},
            },
        )
    elif output_attack == "runtime_drift":
        monkeypatch.setattr(
            runner,
            "current_readonly_source_snapshot",
            lambda: deepcopy(source_snapshot),
        )
    runtime_settings = (
        replace(
            canonical_settings,
            deepseek_timeout_seconds=600,
        )
        if output_attack == "runtime_drift"
        else canonical_settings
    )

    assert context.output_root == fixed_output_root
    with pytest.raises(
        ValueError,
        match="validated formal|source identity|runtime identity",
    ):
        runner.run_eval_suite(
            model=model,
            settings=runtime_settings,
            cases=cases,
            run_id=context.run_id,
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=requested_output_root,
            budget_report_provider=budget_report_provider,
            semantic_judge_model=model,
            calibration_attestation=attestation,
            calibration_review=review,
            formal_holdout_evidence=runner.FormalHoldoutEvidence(
                declaration_manifest_sha256=(declaration.manifest_sha256),
                lock_start_receipt_sha256=acquired_lock.receipt_sha256,
                declared_harness_sha256=harness_sha256,
                regression_bundle_integrity_sha256=(
                    regression_gate.bundle_integrity_sha256
                ),
                regression_gate_sha256=regression_gate.gate_sha256,
                regression_run_id=regression_gate.run_id,
                regression_source_git_commit=(regression_gate.source_git_commit),
                regression_case_set_name=regression_gate.case_set_name,
                regression_case_set_sha256=(regression_gate.case_set_sha256),
                regression_harness_sha256=(regression_gate.harness_sha256),
                regression_source_tree_sha256=(regression_gate.source_tree_sha256),
                regression_source_identity_sha256=(
                    regression_gate.source_identity_sha256
                ),
                regression_runtime_identity_sha256=(
                    regression_gate.runtime_identity_sha256
                ),
            ),
            frozen_harness=frozen_harness,
            source_git_commit=source_git_commit,
            source_tree_sha256=source_tree_sha256,
            formal_execution_capability=capability,
        )

    _assert_zero_budget_attempts(budget_guard)
    model.close()
    if output_attack == "symlink_escape":
        assert list(outside_output_root.iterdir()) == []
    elif output_attack in {"source_drift", "runtime_drift"}:
        assert (
            not requested_output_root.exists()
            or list(requested_output_root.iterdir()) == []
        )
    else:
        assert not requested_output_root.exists()

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
            "f" * 64 if attack == "case_hash" else runner._formal_case_set_sha256(cases)
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
        regression_source_tree_sha256="b" * 64,
        regression_source_identity_sha256="6" * 64,
        regression_runtime_identity_sha256="7" * 64,
        declaration_manifest_sha256="4" * 64,
        lock_start_path=tmp_path / "readonly-holdout-v2.start.json",
        lock_start_receipt_sha256="5" * 64,
        output_root=tmp_path,
        _sentinel=(
            object() if attack == "forged_sentinel" else runner._FORMAL_CONTEXT_SENTINEL
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
