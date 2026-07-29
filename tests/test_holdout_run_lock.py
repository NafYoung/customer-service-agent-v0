from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from evals import holdout_lock as holdout_protocol
from evals.calibration_attestation import (
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
)
from evals.evidence import stable_sha256, write_eval_bundle
from evals.formal_failure_evidence import (
    FormalFailureContext,
    write_formal_failure_bundle,
)
from evals.holdout_lock import (
    HoldoutLockError,
    acquire_holdout_run_lock,
    finalize_holdout_run_lock,
    holdout_lock_receipt_sha256,
    validate_holdout_declaration,
    verify_failed_holdout_receipt_chain,
)
from evals.readonly_eval import ReadonlyEvalCase
from evals.readonly_reporting import current_readonly_harness_fingerprints
from evals.run_readonly_agent_evals import _build_parser, _validate_args


def _attestation() -> ValidatedCalibrationAttestation:
    return ValidatedCalibrationAttestation(
        report_sha256="a" * 64,
        run_id="eval-20260729-calibration-v2",
        source_git_commit="1" * 40,
        fixture_sha256="c" * 64,
        contract_set_sha256="d" * 64,
        harness_sha256="e" * 64,
        result_count=49,
        fixture_ids=tuple(f"canonical-fixture-{index:02d}" for index in range(49)),
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


def _regression_gate(**overrides: object) -> SimpleNamespace:
    source_snapshot = {
        "git_commit": "2" * 40,
        "git_dirty": False,
        "source_tree_sha256": "8" * 64,
        "python_version": "3.11-test",
        "platform": "test-platform",
        "package_versions": {
            "fastapi": "test",
            "httpx": "test",
        },
    }
    values: dict[str, object] = {
        "bundle_path": Path("/private/regression/eval-public-regression"),
        "bundle_integrity_sha256": "6" * 64,
        "gate_sha256": "7" * 64,
        "run_id": "eval-20260729-dev-repeat-public-binding",
        "source_git_commit": "2" * 40,
        "case_set_name": "readonly-regression-v1",
        "case_set_sha256": (
            "6340394c8edd5d95c2756f3f4753d4e224682b7f84a445c76b3abb675bad2edb"
        ),
        "harness_sha256": stable_sha256(current_readonly_harness_fingerprints()),
        "source_tree_sha256": source_snapshot["source_tree_sha256"],
        "source_identity_sha256": stable_sha256(source_snapshot),
        "source_snapshot": source_snapshot,
        "runtime_identity_sha256": "5" * 64,
        "passed_trials": 28,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _manifest_with_regression(
    path: Path,
    *,
    gate: SimpleNamespace | None = None,
) -> Path:
    regression = gate or _regression_gate()
    manifest_path = _manifest(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "public_regression_bundle_integrity_sha256": (
                regression.bundle_integrity_sha256
            ),
            "public_regression_gate_sha256": regression.gate_sha256,
            "public_regression_run_id": regression.run_id,
            "public_regression_source_git_commit": (regression.source_git_commit),
            "public_regression_case_set_name": (regression.case_set_name),
            "public_regression_case_set_sha256": (regression.case_set_sha256),
            "public_regression_harness_sha256": (regression.harness_sha256),
        }
    )
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    return manifest_path


def _cases() -> list[ReadonlyEvalCase]:
    return _semantic_cases()


def _semantic_cases(count: int = 20) -> list[ReadonlyEvalCase]:
    return [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"sealed-semantic-case-{index:02d}",
                "user_message": f"请检查第 {index} 个订单请求。",
                "expected": {
                    "semantic_contract": {
                        "required_claims": [
                            {
                                "id": f"safe_answer_{index:02d}",
                                "category": "task_success",
                                "proposition": "回答准确说明当前请求的结果",
                            }
                        ],
                        "forbidden_claims": [],
                    }
                },
            }
        )
        for index in range(1, count + 1)
    ]


def _write_manifest_for_cases(
    path: Path,
    cases: list[ReadonlyEvalCase],
) -> Path:
    payload = {
        "schema_version": "2.0",
        "case_set_name": "readonly-holdout-v2",
        "case_count": len(cases),
        "case_set_sha256": stable_sha256(
            [
                case.model_dump(mode="json")
                for case in sorted(cases, key=lambda item: item.case_id)
            ]
        ),
        **current_readonly_harness_fingerprints(),
        "formal_runs_allowed": 1,
        "formal_runs_completed": 0,
        "lifecycle_status": "sealed",
        "rerun_policy": "prohibited",
        "sealed_at": "2026-07-29T13:00:00+00:00",
        "sealer_id": "independent-holdout-sealer-v2",
        "source_git_commit": "2" * 40,
        "implementation_independence_declared": True,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _manifest(
    path: Path,
    *,
    formal_runs_allowed: int = 1,
    formal_runs_completed: int = 0,
    scorer_sha256: str | None = None,
    settings: Settings | None = None,
) -> Path:
    path.parent.chmod(0o700)
    cases = _cases()
    harness = current_readonly_harness_fingerprints(settings)
    payload = {
        "schema_version": "2.0",
        "case_set_name": "readonly-holdout-v2",
        "case_count": len(cases),
        "case_set_sha256": stable_sha256(
            [case.model_dump(mode="json") for case in cases]
        ),
        **harness,
        "semantic_calibration_report_sha256": "a" * 64,
        "semantic_calibration_review_sha256": "b" * 64,
        "semantic_calibration_run_id": _attestation().run_id,
        "semantic_calibration_source_git_commit": (_attestation().source_git_commit),
        "semantic_calibration_fixture_sha256": (_attestation().fixture_sha256),
        "semantic_calibration_contract_set_sha256": (
            _attestation().contract_set_sha256
        ),
        "semantic_calibration_harness_sha256": (_attestation().harness_sha256),
        "semantic_calibration_reviewer_id": _review().reviewer_id,
        "semantic_calibration_reviewed_count": _review().reviewed_count,
        "public_regression_bundle_integrity_sha256": (
            _regression_gate().bundle_integrity_sha256
        ),
        "public_regression_gate_sha256": _regression_gate().gate_sha256,
        "public_regression_run_id": _regression_gate().run_id,
        "public_regression_source_git_commit": (_regression_gate().source_git_commit),
        "public_regression_case_set_name": (_regression_gate().case_set_name),
        "public_regression_case_set_sha256": (_regression_gate().case_set_sha256),
        "public_regression_harness_sha256": (_regression_gate().harness_sha256),
        "formal_runs_allowed": formal_runs_allowed,
        "formal_runs_completed": formal_runs_completed,
        "lifecycle_status": "sealed",
        "rerun_policy": "prohibited",
        "sealed_at": "2026-07-29T13:00:00+00:00",
        "sealer_id": "independent-holdout-sealer-v2",
        "source_git_commit": "2" * 40,
        "implementation_independence_declared": True,
    }
    if scorer_sha256 is not None:
        payload["scorer_sha256"] = scorer_sha256
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_holdout_lock_is_exclusive_and_final_status_is_persisted(
    tmp_path: Path,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=_regression_gate(),
    )
    lock_root = tmp_path / "private-locks"
    lock_path = acquire_holdout_run_lock(
        lock_root=lock_root,
        declaration=declaration,
        run_id="eval-20260729-holdout-v2",
        now=datetime(2026, 7, 29, 10, tzinfo=UTC),
    )

    assert stat.S_IMODE(lock_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    with pytest.raises(HoldoutLockError, match="already been consumed"):
        acquire_holdout_run_lock(
            lock_root=lock_root,
            declaration=declaration,
            run_id="eval-20260729-holdout-v2-second",
        )

    start_receipt_sha256 = holdout_lock_receipt_sha256(lock_path)
    terminal_path = finalize_holdout_run_lock(
        lock_path=lock_path,
        status="completed",
        run_id="eval-20260729-holdout-v2",
        expected_start_receipt_sha256=start_receipt_sha256,
        bundle_integrity_sha256="f" * 64,
        now=datetime(2026, 7, 29, 10, 5, tzinfo=UTC),
    )
    lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    terminal_payload = json.loads(terminal_path.read_text(encoding="utf-8"))
    assert holdout_lock_receipt_sha256(lock_path) == start_receipt_sha256
    assert lock_payload["status"] == "started"
    assert lock_payload["run_id"] == "eval-20260729-holdout-v2"
    assert "case_set_sha256" in lock_payload
    assert terminal_payload["status"] == "completed"
    assert terminal_payload["lock_start_receipt_sha256"] == start_receipt_sha256
    assert terminal_payload["bundle_integrity_sha256"] == "f" * 64
    assert terminal_payload["attempt_bundle_integrity_sha256"] is None
    assert terminal_payload["failure_evidence_status"] is None
    assert "user_message" not in json.dumps(
        {"start": lock_payload, "terminal": terminal_payload}
    )


@pytest.mark.parametrize(
    "receipt_name",
    [
        "readonly-holdout-v2.start.json",
        "readonly-holdout-v2.terminal.json",
    ],
)
def test_exclusive_receipt_write_cleans_partial_file_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch,
    receipt_name: str,
) -> None:
    receipt_path = tmp_path / receipt_name
    real_write = os.write
    interrupted = False

    def interrupt_after_partial_write(
        descriptor: int,
        data: bytes | memoryview,
    ) -> int:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            real_write(descriptor, bytes(data[:5]))
            raise KeyboardInterrupt
        return real_write(descriptor, data)

    monkeypatch.setattr(os, "write", interrupt_after_partial_write)

    with pytest.raises(KeyboardInterrupt):
        holdout_protocol._write_exclusive_json(
            receipt_path,
            {"schema_version": "1.0", "status": "started"},
        )

    assert receipt_path.exists() is False


def test_lock_acquisition_returns_exact_receipt_hash_without_reread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=_regression_gate(),
    )
    monkeypatch.setattr(
        holdout_protocol,
        "_read_manifest_with_sha256",
        lambda path: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    acquired = holdout_protocol.acquire_holdout_run_lock_with_hash(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id="eval-20260729-atomic-start-hash",
    )

    assert (
        acquired.receipt_sha256
        == hashlib.sha256(acquired.path.read_bytes()).hexdigest()
    )


def test_holdout_declaration_requires_calibration_from_same_source_commit(
    tmp_path: Path,
) -> None:
    with pytest.raises(HoldoutLockError, match="calibration|source|commit"):
        validate_holdout_declaration(
            manifest_path=_manifest(tmp_path / "manifest.json"),
            case_set_name="readonly-holdout-v2",
            cases=_cases(),
            calibration_attestation=_attestation(),
            calibration_review=_review(),
            regression_gate=_regression_gate(),
            source_git_commit="2" * 40,
        )


def test_formal_v2_parser_requires_public_regression_bundle() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--case-dir",
            "artifacts/private/holdout-v2/cases",
            "--purpose",
            "holdout_formal",
            "--split",
            "holdout",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4",
            "--holdout-manifest",
            "artifacts/private/holdout-v2/manifest.json",
            "--calibration-report",
            "artifacts/private/calibration/report.json",
            "--calibration-review",
            "artifacts/private/calibration/review.json",
        ]
    )

    with pytest.raises(SystemExit):
        _validate_args(parser, args)


def test_holdout_declaration_binds_verified_public_regression(
    tmp_path: Path,
) -> None:
    gate = _regression_gate()
    declaration = validate_holdout_declaration(
        manifest_path=_manifest_with_regression(
            tmp_path / "manifest.json",
            gate=gate,
        ),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=gate,
    )

    assert (
        declaration.regression_bundle_integrity_sha256 == gate.bundle_integrity_sha256
    )
    assert declaration.regression_gate_sha256 == gate.gate_sha256
    assert declaration.regression_run_id == gate.run_id


def test_holdout_declaration_rejects_self_reported_regression_hash(
    tmp_path: Path,
) -> None:
    gate = _regression_gate()
    manifest_path = _manifest_with_regression(
        tmp_path / "manifest.json",
        gate=gate,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["public_regression_bundle_integrity_sha256"] = "9" * 64
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(HoldoutLockError, match="regression"):
        validate_holdout_declaration(
            manifest_path=manifest_path,
            case_set_name="readonly-holdout-v2",
            cases=_cases(),
            calibration_attestation=_attestation(),
            calibration_review=_review(),
            regression_gate=gate,
        )


def test_holdout_start_receipt_persists_public_regression_identity(
    tmp_path: Path,
) -> None:
    gate = _regression_gate()
    declaration = validate_holdout_declaration(
        manifest_path=_manifest_with_regression(
            tmp_path / "manifest.json",
            gate=gate,
        ),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=gate,
    )
    start_path = acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id="eval-20260729-regression-bound-start",
    )
    start = json.loads(start_path.read_text(encoding="utf-8"))

    assert (
        start["public_regression_bundle_integrity_sha256"]
        == gate.bundle_integrity_sha256
    )
    assert start["public_regression_gate_sha256"] == gate.gate_sha256
    assert start["public_regression_run_id"] == gate.run_id


def test_start_receipt_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=_regression_gate(),
    )
    start_path = acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id="eval-20260729-start-extra-field",
    )
    payload = json.loads(start_path.read_text(encoding="utf-8"))
    payload["unreviewed_escape_hatch"] = True
    start_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    start_path.chmod(0o600)

    with pytest.raises(HoldoutLockError, match="schema|receipt|invalid"):
        holdout_lock_receipt_sha256(start_path)


def test_holdout_finalize_rejects_a_replaced_start_receipt(
    tmp_path: Path,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=_regression_gate(),
    )
    lock_path = acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id="eval-20260729-replaced-start",
    )
    expected_start_sha256 = holdout_lock_receipt_sha256(lock_path)
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload["created_at"] = "2026-07-29T23:59:59+00:00"
    lock_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(HoldoutLockError, match="start|receipt|changed"):
        finalize_holdout_run_lock(
            lock_path=lock_path,
            status="failed",
            run_id="eval-20260729-replaced-start",
            expected_start_receipt_sha256=expected_start_sha256,
        )


def test_failed_terminal_binds_only_failed_attempt_evidence(
    tmp_path: Path,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=_regression_gate(),
    )
    lock_path = acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id="eval-20260729-failed-attempt",
    )
    start_sha256 = holdout_lock_receipt_sha256(lock_path)
    terminal_path = finalize_holdout_run_lock(
        lock_path=lock_path,
        status="failed",
        run_id="eval-20260729-failed-attempt",
        expected_start_receipt_sha256=start_sha256,
        attempt_bundle_integrity_sha256="e" * 64,
    )
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))

    assert terminal["schema_version"] == "2.0"
    assert terminal["bundle_integrity_sha256"] is None
    assert terminal["attempt_bundle_integrity_sha256"] == "e" * 64
    assert terminal["failure_evidence_status"] == "captured"
    with pytest.raises(HoldoutLockError, match="invalid|conflicting"):
        other_lock = acquire_holdout_run_lock(
            lock_root=tmp_path / "other-private-locks",
            declaration=declaration,
            run_id="eval-20260729-conflicting-attempt",
        )
        finalize_holdout_run_lock(
            lock_path=other_lock,
            status="failed",
            run_id="eval-20260729-conflicting-attempt",
            expected_start_receipt_sha256=(holdout_lock_receipt_sha256(other_lock)),
            bundle_integrity_sha256="f" * 64,
            attempt_bundle_integrity_sha256="e" * 64,
        )


@pytest.mark.parametrize("source_tree_attack", [False, True])
def test_failed_holdout_chain_binds_private_attempt_bundle(
    tmp_path: Path,
    monkeypatch,
    source_tree_attack: bool,
) -> None:
    regression_gate = _regression_gate()
    manifest_path = _manifest(tmp_path / "manifest.json")
    declaration = validate_holdout_declaration(
        manifest_path=manifest_path,
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=regression_gate,
    )
    run_id = "eval-20260729-failed-chain"
    start_path = acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id=run_id,
    )
    start_sha256 = holdout_lock_receipt_sha256(start_path)
    failure_bundle = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=FormalFailureContext.model_validate(
            {
                "run_id": run_id,
                "created_at": "2026-07-29T16:00:00+00:00",
                "failed_at": "2026-07-29T16:00:01+00:00",
                "failure_stage": "suite_execution",
                "failure_code": "MODEL_HTTP_ERROR",
                "max_output_tokens": 1024,
                "source": {
                    "git_commit": declaration.source_git_commit,
                    "git_dirty": False,
                    "source_tree_sha256": (
                        "9" * 64
                        if source_tree_attack
                        else regression_gate.source_tree_sha256
                    ),
                },
                "case_set": {
                    "name": declaration.case_set_name,
                    "sha256": declaration.case_set_sha256,
                    "planned_case_count": 20,
                    "planned_trials": 4,
                },
                "formal_holdout": {
                    "declaration_manifest_sha256": (declaration.manifest_sha256),
                    "lock_start_receipt_sha256": start_sha256,
                    "declared_harness_sha256": (declaration.harness_sha256),
                    "runtime_harness_sha256": (declaration.harness_sha256),
                    "regression_bundle_integrity_sha256": (
                        declaration.regression_bundle_integrity_sha256
                    ),
                    "regression_gate_sha256": (declaration.regression_gate_sha256),
                    "regression_run_id": declaration.regression_run_id,
                    "regression_source_git_commit": (
                        declaration.regression_source_git_commit
                    ),
                    "regression_case_set_name": (declaration.regression_case_set_name),
                    "regression_case_set_sha256": (
                        declaration.regression_case_set_sha256
                    ),
                    "regression_harness_sha256": (
                        declaration.regression_harness_sha256
                    ),
                },
            }
        ),
        case_records=[],
        records_captured=True,
        budget_summary=None,
    )
    failure_integrity_sha256 = hashlib.sha256(
        (failure_bundle / "integrity.json").read_bytes()
    ).hexdigest()
    terminal_path = finalize_holdout_run_lock(
        lock_path=start_path,
        status="failed",
        run_id=run_id,
        expected_start_receipt_sha256=start_sha256,
        attempt_bundle_integrity_sha256=(failure_integrity_sha256),
    )
    monkeypatch.setattr(
        holdout_protocol,
        "validate_regression_gate",
        lambda **kwargs: _regression_gate(),
    )

    if source_tree_attack:
        with pytest.raises(
            HoldoutLockError,
            match="source|runtime|chain|match",
        ):
            verify_failed_holdout_receipt_chain(
                manifest_path=manifest_path,
                start_path=start_path,
                terminal_path=terminal_path,
                bundle_path=failure_bundle,
                regression_bundle_path=tmp_path / "regression-bundle",
                private_root=tmp_path,
            )
    else:
        verify_failed_holdout_receipt_chain(
            manifest_path=manifest_path,
            start_path=start_path,
            terminal_path=terminal_path,
            bundle_path=failure_bundle,
            regression_bundle_path=tmp_path / "regression-bundle",
            private_root=tmp_path,
        )


@pytest.mark.parametrize("source_runtime_attack", [False, True])
def test_completed_holdout_chain_links_manifest_start_bundle_and_terminal(
    tmp_path: Path,
    monkeypatch,
    source_runtime_attack: bool,
) -> None:
    regression_gate = _regression_gate()
    manifest_path = _manifest(tmp_path / "manifest.json")
    declaration = validate_holdout_declaration(
        manifest_path=manifest_path,
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=regression_gate,
    )
    run_id = "eval-20260729-complete-chain"
    start_path = acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id=run_id,
    )
    start_sha256 = holdout_lock_receipt_sha256(start_path)
    source_snapshot = dict(regression_gate.source_snapshot)
    source_snapshot["package_versions"] = dict(source_snapshot["package_versions"])
    if source_runtime_attack:
        source_snapshot["package_versions"]["httpx"] = "forged"
    bundle_manifest = {
        "run_id": run_id,
        "source": source_snapshot,
        "harness": {
            "runtime_harness_sha256": declaration.harness_sha256,
        },
        "eval": {
            "formal_holdout": {
                "declaration_manifest_sha256": (declaration.manifest_sha256),
                "lock_start_receipt_sha256": start_sha256,
                "declared_harness_sha256": declaration.harness_sha256,
                "regression_bundle_integrity_sha256": (
                    declaration.regression_bundle_integrity_sha256
                ),
                "regression_gate_sha256": (declaration.regression_gate_sha256),
                "regression_run_id": declaration.regression_run_id,
                "regression_source_git_commit": (
                    declaration.regression_source_git_commit
                ),
                "regression_case_set_name": (declaration.regression_case_set_name),
                "regression_case_set_sha256": (declaration.regression_case_set_sha256),
                "regression_harness_sha256": (declaration.regression_harness_sha256),
            },
            "semantic_calibration": {
                "report_sha256": declaration.calibration_report_sha256,
                "review_sha256": declaration.calibration_review_sha256,
                "run_id": declaration.calibration_run_id,
                "source_git_commit": (declaration.calibration_source_git_commit),
                "fixture_sha256": (declaration.calibration_fixture_sha256),
                "contract_set_sha256": (declaration.calibration_contract_set_sha256),
                "harness_sha256": (declaration.calibration_harness_sha256),
                "reviewer_id": declaration.calibration_reviewer_id,
                "reviewed_count": (declaration.calibration_reviewed_count),
            },
            "case_set_name": declaration.case_set_name,
            "case_set_sha256": declaration.case_set_sha256,
            "scorer_version": declaration.scorer_version,
        },
    }
    bundle_path = write_eval_bundle(
        output_root=tmp_path / "bundles",
        run_id=run_id,
        manifest=bundle_manifest,
        case_records=[{"case_id": "chain-case", "trial": 1}],
        summary={},
    )
    bundle_manifest_path = bundle_path / "manifest.json"
    integrity_path = bundle_path / "integrity.json"
    integrity_sha256 = hashlib.sha256(integrity_path.read_bytes()).hexdigest()
    validation_calls = 0

    def accept_minimal_schema(payload):
        nonlocal validation_calls
        validation_calls += 1
        assert payload["manifest"]["run_id"] == run_id

    monkeypatch.setattr(
        holdout_protocol,
        "validate_readonly_payload",
        accept_minimal_schema,
        raising=False,
    )
    monkeypatch.setattr(
        holdout_protocol,
        "validate_regression_gate",
        lambda **kwargs: _regression_gate(),
    )
    terminal_path = finalize_holdout_run_lock(
        lock_path=start_path,
        status="completed",
        run_id=run_id,
        expected_start_receipt_sha256=start_sha256,
        bundle_integrity_sha256=integrity_sha256,
    )

    if source_runtime_attack:
        with pytest.raises(
            HoldoutLockError,
            match="source|runtime|chain|match",
        ):
            holdout_protocol.verify_holdout_receipt_chain(
                manifest_path=manifest_path,
                start_path=start_path,
                terminal_path=terminal_path,
                bundle_path=bundle_path,
                regression_bundle_path=tmp_path / "regression-bundle",
                private_root=tmp_path,
            )
        return
    holdout_protocol.verify_holdout_receipt_chain(
        manifest_path=manifest_path,
        start_path=start_path,
        terminal_path=terminal_path,
        bundle_path=bundle_path,
        regression_bundle_path=tmp_path / "regression-bundle",
        private_root=tmp_path,
    )
    assert validation_calls == 1

    outside_private_root = tmp_path / "different-private-root"
    outside_private_root.mkdir(mode=0o700)
    with pytest.raises(HoldoutLockError, match="private|root|outside"):
        holdout_protocol.verify_holdout_receipt_chain(
            manifest_path=manifest_path,
            start_path=start_path,
            terminal_path=terminal_path,
            bundle_path=bundle_path,
            regression_bundle_path=tmp_path / "regression-bundle",
            private_root=outside_private_root,
        )

    terminal_payload = json.loads(terminal_path.read_text(encoding="utf-8"))
    terminal_payload["unreviewed_escape_hatch"] = True
    terminal_path.write_text(
        json.dumps(terminal_payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    terminal_path.chmod(0o600)
    with pytest.raises(HoldoutLockError, match="schema|receipt|terminal"):
        holdout_protocol.verify_holdout_receipt_chain(
            manifest_path=manifest_path,
            start_path=start_path,
            terminal_path=terminal_path,
            bundle_path=bundle_path,
            regression_bundle_path=tmp_path / "regression-bundle",
            private_root=tmp_path,
        )
    terminal_payload.pop("unreviewed_escape_hatch")
    terminal_path.write_text(
        json.dumps(terminal_payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    terminal_path.chmod(0o600)

    for private_path in (
        manifest_path,
        start_path,
        terminal_path,
    ):
        private_path.chmod(0o644)
        with pytest.raises(
            HoldoutLockError,
            match="private|permission|owner|regular",
        ):
            holdout_protocol.verify_holdout_receipt_chain(
                manifest_path=manifest_path,
                start_path=start_path,
                terminal_path=terminal_path,
                bundle_path=bundle_path,
                regression_bundle_path=tmp_path / "regression-bundle",
                private_root=tmp_path,
            )
        private_path.chmod(0o600)

    bundle_path.chmod(0o755)
    with pytest.raises(
        HoldoutLockError,
        match="private|permission|owner|directory",
    ):
        holdout_protocol.verify_holdout_receipt_chain(
            manifest_path=manifest_path,
            start_path=start_path,
            terminal_path=terminal_path,
            bundle_path=bundle_path,
            regression_bundle_path=tmp_path / "regression-bundle",
            private_root=tmp_path,
        )
    bundle_path.chmod(0o700)

    summary_path = bundle_path / "summary.json"
    real_summary_path = bundle_path.parent / "summary-real.json"
    summary_path.rename(real_summary_path)
    summary_path.symlink_to(real_summary_path)
    with pytest.raises(
        HoldoutLockError,
        match="private|permission|symlink|regular",
    ):
        holdout_protocol.verify_holdout_receipt_chain(
            manifest_path=manifest_path,
            start_path=start_path,
            terminal_path=terminal_path,
            bundle_path=bundle_path,
            regression_bundle_path=tmp_path / "regression-bundle",
            private_root=tmp_path,
        )
    summary_path.unlink()
    real_summary_path.rename(summary_path)

    bundle_manifest["eval"]["formal_holdout"]["declared_harness_sha256"] = "0" * 64
    bundle_manifest_path.write_text(
        json.dumps(bundle_manifest, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(
        HoldoutLockError,
        match="chain|harness|integrity|schema",
    ):
        holdout_protocol.verify_holdout_receipt_chain(
            manifest_path=manifest_path,
            start_path=start_path,
            terminal_path=terminal_path,
            bundle_path=bundle_path,
            regression_bundle_path=tmp_path / "regression-bundle",
            private_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("fixture_sha256", "0" * 64),
        ("contract_set_sha256", "0" * 64),
        ("harness_sha256", "0" * 64),
        ("reviewer_id", "forged-reviewer"),
        ("reviewed_count", 6),
        ("case_set_name", "forged-holdout-v2"),
        ("case_set_sha256", "0" * 64),
        ("scorer_version", "forged-scorer"),
    ],
)
def test_completed_chain_cross_checks_every_declared_evidence_field(
    tmp_path: Path,
    monkeypatch,
    field_name: str,
    forged_value: object,
) -> None:
    manifest_path = _manifest(tmp_path / "manifest.json")
    declaration = validate_holdout_declaration(
        manifest_path=manifest_path,
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=_regression_gate(),
    )
    run_id = "eval-20260729-forged-chain"
    start_path = acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id=run_id,
    )
    start_sha256 = holdout_lock_receipt_sha256(start_path)
    calibration = {
        "report_sha256": declaration.calibration_report_sha256,
        "review_sha256": declaration.calibration_review_sha256,
        "run_id": declaration.calibration_run_id,
        "source_git_commit": declaration.calibration_source_git_commit,
        "fixture_sha256": declaration.calibration_fixture_sha256,
        "contract_set_sha256": (declaration.calibration_contract_set_sha256),
        "harness_sha256": declaration.calibration_harness_sha256,
        "reviewer_id": declaration.calibration_reviewer_id,
        "reviewed_count": declaration.calibration_reviewed_count,
    }
    bundle_eval = {
        "formal_holdout": {
            "declaration_manifest_sha256": (declaration.manifest_sha256),
            "lock_start_receipt_sha256": start_sha256,
            "declared_harness_sha256": declaration.harness_sha256,
            "regression_bundle_integrity_sha256": (
                declaration.regression_bundle_integrity_sha256
            ),
            "regression_gate_sha256": declaration.regression_gate_sha256,
            "regression_run_id": declaration.regression_run_id,
            "regression_source_git_commit": (declaration.regression_source_git_commit),
            "regression_case_set_name": (declaration.regression_case_set_name),
            "regression_case_set_sha256": (declaration.regression_case_set_sha256),
            "regression_harness_sha256": (declaration.regression_harness_sha256),
        },
        "semantic_calibration": calibration,
        "case_set_name": declaration.case_set_name,
        "case_set_sha256": declaration.case_set_sha256,
        "scorer_version": declaration.scorer_version,
    }
    if field_name in calibration:
        calibration[field_name] = forged_value
    else:
        bundle_eval[field_name] = forged_value
    bundle_path = write_eval_bundle(
        output_root=tmp_path / "bundles",
        run_id=run_id,
        manifest={
            "run_id": run_id,
            "source": {
                "git_commit": declaration.source_git_commit,
            },
            "harness": {
                "runtime_harness_sha256": (declaration.harness_sha256),
            },
            "eval": bundle_eval,
        },
        case_records=[{"case_id": "chain-case", "trial": 1}],
        summary={},
    )
    monkeypatch.setattr(
        holdout_protocol,
        "validate_readonly_payload",
        lambda payload: payload,
    )
    monkeypatch.setattr(
        holdout_protocol,
        "validate_regression_gate",
        lambda **kwargs: _regression_gate(),
    )
    terminal_path = finalize_holdout_run_lock(
        lock_path=start_path,
        status="completed",
        run_id=run_id,
        expected_start_receipt_sha256=start_sha256,
        bundle_integrity_sha256=hashlib.sha256(
            (bundle_path / "integrity.json").read_bytes()
        ).hexdigest(),
    )

    with pytest.raises(HoldoutLockError, match="chain|harness|match"):
        holdout_protocol.verify_holdout_receipt_chain(
            manifest_path=manifest_path,
            start_path=start_path,
            terminal_path=terminal_path,
            bundle_path=bundle_path,
            regression_bundle_path=tmp_path / "regression-bundle",
            private_root=tmp_path,
        )


def test_same_case_hash_cannot_get_a_second_lock_by_renaming(
    tmp_path: Path,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=_regression_gate(),
    )
    acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id="eval-20260729-holdout-v2",
    )

    renamed = replace(
        declaration,
        case_set_name="renamed-holdout-v2",
        manifest_sha256="f" * 64,
    )
    with pytest.raises(HoldoutLockError, match="already been consumed"):
        acquire_holdout_run_lock(
            lock_root=tmp_path / "private-locks",
            declaration=renamed,
            run_id="eval-20260729-renamed-holdout-v2",
        )


def test_formal_v2_global_lock_rejects_a_different_case_set(
    tmp_path: Path,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
        calibration_attestation=_attestation(),
        calibration_review=_review(),
        regression_gate=_regression_gate(),
    )
    lock_root = tmp_path / "private-locks"
    acquire_holdout_run_lock(
        lock_root=lock_root,
        declaration=declaration,
        run_id="eval-20260729-holdout-v2",
    )
    different = replace(
        declaration,
        case_set_sha256="0" * 64,
        manifest_sha256="1" * 64,
    )

    with pytest.raises(HoldoutLockError, match="already been consumed"):
        acquire_holdout_run_lock(
            lock_root=lock_root,
            declaration=different,
            run_id="eval-20260729-different-v2",
        )


@pytest.mark.parametrize(
    (
        "formal_runs_allowed",
        "formal_runs_completed",
        "scorer_sha256",
        "message",
    ),
    [
        (1, 1, None, "formal run is no longer available"),
        (True, 0, None, "formal run is no longer available"),
        (1, False, None, "formal run is no longer available"),
        (1, 0, "0" * 64, "frozen harness"),
    ],
)
def test_holdout_declaration_fails_closed_before_model_use(
    tmp_path: Path,
    formal_runs_allowed: int,
    formal_runs_completed: int,
    scorer_sha256: str | None,
    message: str,
) -> None:
    with pytest.raises(HoldoutLockError, match=message):
        validate_holdout_declaration(
            manifest_path=_manifest(
                tmp_path / "manifest.json",
                formal_runs_allowed=formal_runs_allowed,
                formal_runs_completed=formal_runs_completed,
                scorer_sha256=scorer_sha256,
            ),
            case_set_name="readonly-holdout-v2",
            cases=_cases(),
            calibration_attestation=_attestation(),
            calibration_review=_review(),
            regression_gate=_regression_gate(),
        )


def test_holdout_manifest_v2_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    path = _manifest(tmp_path / "manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["unreviewed_escape_hatch"] = True
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(HoldoutLockError, match="schema"):
        validate_holdout_declaration(
            manifest_path=path,
            case_set_name="readonly-holdout-v2",
            cases=_cases(),
            calibration_attestation=_attestation(),
            calibration_review=_review(),
            regression_gate=_regression_gate(),
        )


def test_holdout_declaration_freezes_paid_model_runtime(
    tmp_path: Path,
) -> None:
    sealed_settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=0,
        deepseek_max_tokens=1024,
        deepseek_max_retries=2,
        deepseek_timeout_seconds=30,
        agent_max_tool_rounds=4,
        agent_max_tool_calls=12,
    )
    manifest_path = _manifest(
        tmp_path / "manifest.json",
        settings=sealed_settings,
    )
    changed_settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=1,
        deepseek_max_tokens=1,
        deepseek_max_retries=0,
        deepseek_timeout_seconds=5,
        agent_max_tool_rounds=1,
        agent_max_tool_calls=1,
    )

    assert current_readonly_harness_fingerprints(
        sealed_settings
    ) != current_readonly_harness_fingerprints(changed_settings)
    with pytest.raises(HoldoutLockError, match="frozen harness"):
        validate_holdout_declaration(
            manifest_path=manifest_path,
            case_set_name="readonly-holdout-v2",
            cases=_cases(),
            settings=changed_settings,
            calibration_attestation=_attestation(),
            calibration_review=_review(),
            regression_gate=_regression_gate(
                harness_sha256=stable_sha256(
                    current_readonly_harness_fingerprints(changed_settings)
                )
            ),
        )


def test_formal_holdout_requires_twenty_semantically_scored_cases(
    tmp_path: Path,
) -> None:
    with pytest.raises(HoldoutLockError, match="20 semantic"):
        validate_holdout_declaration(
            manifest_path=_write_manifest_for_cases(
                tmp_path / "undersized.json",
                _semantic_cases(19),
            ),
            case_set_name="readonly-holdout-v2",
            cases=_semantic_cases(19),
        )

    cases = _semantic_cases()
    cases[-1].expected.semantic_contract = None
    with pytest.raises(HoldoutLockError, match="20 semantic"):
        validate_holdout_declaration(
            manifest_path=_write_manifest_for_cases(
                tmp_path / "missing-contract.json",
                cases,
            ),
            case_set_name="readonly-holdout-v2",
            cases=cases,
        )


def test_formal_holdout_requires_bound_calibration_and_review_hashes(
    tmp_path: Path,
) -> None:
    cases = _semantic_cases()
    with pytest.raises(HoldoutLockError, match="calibration"):
        validate_holdout_declaration(
            manifest_path=_write_manifest_for_cases(
                tmp_path / "missing-attestations.json",
                cases,
            ),
            case_set_name="readonly-holdout-v2",
            cases=cases,
        )


def test_holdout_cli_requires_a_declared_manifest() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--purpose",
            "holdout_formal",
            "--split",
            "holdout",
            "--case-dir",
            "private-holdout-cases",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4",
        ]
    )

    with pytest.raises(SystemExit):
        _validate_args(parser, args)


def test_holdout_cli_requires_calibration_and_review_attestations() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--purpose",
            "holdout_formal",
            "--split",
            "holdout",
            "--case-dir",
            "private-holdout-cases",
            "--holdout-manifest",
            "private-holdout-manifest.json",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4",
        ]
    )

    with pytest.raises(SystemExit):
        _validate_args(parser, args)


@pytest.mark.parametrize("purpose", ["diagnostic", "dev_repeat"])
def test_holdout_split_cannot_bypass_the_formal_runner(
    purpose: str,
) -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--purpose",
            purpose,
            "--split",
            "holdout",
            "--case-dir",
            "private-holdout-cases",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4" if purpose == "dev_repeat" else "1",
        ]
    )

    with pytest.raises(SystemExit):
        _validate_args(parser, args)
