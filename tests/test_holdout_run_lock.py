from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from evals.calibration_attestation import (
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
)
from evals.evidence import stable_sha256
from evals.holdout_lock import (
    HoldoutLockError,
    acquire_holdout_run_lock,
    finalize_holdout_run_lock,
    holdout_lock_receipt_sha256,
    validate_holdout_declaration,
)
from evals.readonly_eval import ReadonlyEvalCase
from evals.readonly_reporting import current_readonly_harness_fingerprints
from evals.run_readonly_agent_evals import _build_parser, _validate_args


def _attestation() -> ValidatedCalibrationAttestation:
    return ValidatedCalibrationAttestation(
        report_sha256="a" * 64,
        run_id="eval-20260729-calibration-v2",
        fixture_sha256="c" * 64,
        contract_set_sha256="d" * 64,
        harness_sha256="e" * 64,
        result_count=44,
        fixture_ids=tuple(
            f"canonical-fixture-{index:02d}"
            for index in range(44)
        ),
    )


def _review() -> ValidatedCalibrationReview:
    return ValidatedCalibrationReview(
        review_sha256="b" * 64,
        reviewer_id="independent-reviewer-v1",
        reviewed_count=5,
    )


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
        "implementation_independence_declared": True,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _manifest(
    path: Path,
    *,
    formal_runs_allowed: int = 1,
    formal_runs_completed: int = 0,
    scorer_sha256: str | None = None,
    settings: Settings | None = None,
) -> Path:
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
        "semantic_calibration_fixture_sha256": (
            _attestation().fixture_sha256
        ),
        "semantic_calibration_contract_set_sha256": (
            _attestation().contract_set_sha256
        ),
        "semantic_calibration_harness_sha256": (
            _attestation().harness_sha256
        ),
        "semantic_calibration_reviewer_id": _review().reviewer_id,
        "semantic_calibration_reviewed_count": _review().reviewed_count,
        "formal_runs_allowed": formal_runs_allowed,
        "formal_runs_completed": formal_runs_completed,
        "lifecycle_status": "sealed",
        "rerun_policy": "prohibited",
        "sealed_at": "2026-07-29T13:00:00+00:00",
        "sealer_id": "independent-holdout-sealer-v2",
        "implementation_independence_declared": True,
    }
    if scorer_sha256 is not None:
        payload["scorer_sha256"] = scorer_sha256
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
        bundle_integrity_sha256="f" * 64,
        now=datetime(2026, 7, 29, 10, 5, tzinfo=UTC),
    )
    lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    terminal_payload = json.loads(
        terminal_path.read_text(encoding="utf-8")
    )
    assert holdout_lock_receipt_sha256(lock_path) == start_receipt_sha256
    assert lock_payload["status"] == "started"
    assert lock_payload["run_id"] == "eval-20260729-holdout-v2"
    assert "case_set_sha256" in lock_payload
    assert terminal_payload["status"] == "completed"
    assert (
        terminal_payload["lock_start_receipt_sha256"]
        == start_receipt_sha256
    )
    assert terminal_payload["bundle_integrity_sha256"] == "f" * 64
    assert "user_message" not in json.dumps(
        {"start": lock_payload, "terminal": terminal_payload}
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

    assert (
        current_readonly_harness_fingerprints(sealed_settings)
        != current_readonly_harness_fingerprints(changed_settings)
    )
    with pytest.raises(HoldoutLockError, match="frozen harness"):
        validate_holdout_declaration(
            manifest_path=manifest_path,
            case_set_name="readonly-holdout-v2",
            cases=_cases(),
            settings=changed_settings,
            calibration_attestation=_attestation(),
            calibration_review=_review(),
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
