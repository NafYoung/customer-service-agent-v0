from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.evidence import ArtifactIntegrityError
from evals.evidence_schema import validate_readonly_bundle
from evals.formal_failure_evidence import (
    FormalFailureContext,
    FormalFailureEvidenceError,
    validate_formal_failure_bundle,
    write_formal_failure_bundle,
)
from evals.readonly_eval import SCORE_CATEGORIES
from evals.readonly_reporting import offline_budget_report


def _context(*, run_id: str = "formal-failed-20260729-a1") -> FormalFailureContext:
    return FormalFailureContext.model_validate(
        {
            "run_id": run_id,
            "created_at": "2026-07-29T16:00:00+00:00",
            "failed_at": "2026-07-29T16:00:03+00:00",
            "failure_stage": "suite_execution",
            "failure_code": "MODEL_HTTP_ERROR",
            "source": {
                "git_commit": "1" * 40,
                "git_dirty": False,
                "source_tree_sha256": "2" * 64,
            },
            "case_set": {
                "name": "readonly-holdout-v2",
                "sha256": "3" * 64,
                "planned_case_count": 20,
                "planned_trials": 4,
            },
            "formal_holdout": {
                "declaration_manifest_sha256": "4" * 64,
                "lock_start_receipt_sha256": "5" * 64,
                "declared_harness_sha256": "6" * 64,
                "runtime_harness_sha256": "6" * 64,
            },
        }
    )


def _case_record(
    *,
    case_id: str = "formal-case-01",
    trial: int = 1,
    status: str = "passed",
) -> dict[str, object]:
    scores = {category: status == "passed" for category in SCORE_CATEGORIES}
    completed_at = datetime(2026, 7, 29, 16, 0, trial, tzinfo=UTC)
    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "split": "holdout",
        "trial": trial,
        "case_run_id": f"failed-run-{case_id}-t{trial}",
        "input_sha256": "7" * 64,
        "started_at": (completed_at - timedelta(milliseconds=20)).isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": 20,
        "status": status,
        "termination_reason": (
            "completed" if status == "passed" else "MODEL_HTTP_ERROR"
        ),
        "error_code": None if status == "passed" else "MODEL_HTTP_ERROR",
        "final_text": "safe partial answer",
        "model_calls": [],
        "tool_trace": [],
        "business_state": {
            "changed": False,
            "changed_tables": [],
            "before_sha256": "8" * 64,
            "after_sha256": "8" * 64,
        },
        "counted_action_records": 0,
        "scores": scores,
        "score_checks": [
            {
                "category": category,
                "message": f"{category} check",
                "passed": passed,
            }
            for category, passed in scores.items()
        ],
        "checks": [],
        "failures": (
            [] if status == "passed" else ["provider request failed"]
        ),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_integrity_entry(bundle_path: Path, relative_path: str) -> None:
    integrity_path = bundle_path / "integrity.json"
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    changed_path = bundle_path / relative_path
    integrity["files"][relative_path] = {
        "sha256": _sha256(changed_path),
        "bytes": changed_path.stat().st_size,
    }
    integrity_path.write_text(
        json.dumps(integrity, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _assert_private_tree(bundle_path: Path) -> None:
    for path in (bundle_path, *bundle_path.rglob("*")):
        mode = path.lstat().st_mode
        if path.is_dir():
            assert stat.S_ISDIR(mode)
            assert stat.S_IMODE(mode) == 0o700
        else:
            assert stat.S_ISREG(mode)
            assert stat.S_IMODE(mode) == 0o600


def test_zero_record_failed_attempt_is_private_and_not_completed_evidence(
    tmp_path: Path,
) -> None:
    context = _context()

    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=context,
        case_records=[],
        records_captured=True,
        budget_summary=None,
    )

    bundle = validate_formal_failure_bundle(bundle_path)
    assert bundle.manifest.artifact_kind == "formal_holdout_failed_attempt"
    assert bundle.manifest.status == "failed"
    assert bundle.manifest.completed_record_count == 0
    assert bundle.summary.record_capture_status == "captured"
    assert bundle.summary.partial is not None
    assert bundle.summary.partial.completed_record_count == 0
    assert bundle.summary.budget_capture_status == "unavailable"
    assert bundle.summary.budget is None
    assert bundle.cases == []
    assert bundle.trajectories == []
    _assert_private_tree(bundle_path)

    with pytest.raises(ValidationError):
        validate_readonly_bundle(bundle_path)


def test_partial_failed_attempt_cross_validates_records_and_budget(
    tmp_path: Path,
) -> None:
    records = [
        _case_record(),
        _case_record(trial=2, status="failed"),
    ]

    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id="formal-failed-20260729-a2"),
        case_records=records,
        records_captured=True,
        budget_summary=offline_budget_report(),
    )

    bundle = validate_formal_failure_bundle(bundle_path)
    assert bundle.manifest.completed_record_count == 2
    assert bundle.summary.completed_record_count == 2
    assert bundle.summary.partial is not None
    assert bundle.summary.partial.unique_case_count == 1
    assert bundle.summary.partial.passed_record_count == 1
    assert bundle.summary.partial.failed_record_count == 1
    assert bundle.summary.partial.error_counts == {"MODEL_HTTP_ERROR": 1}
    assert bundle.summary.budget_capture_status == "captured"
    assert bundle.summary.budget is not None
    assert [
        item.model_dump(mode="json") for item in bundle.cases
    ] == [
        item.model_dump(mode="json") for item in bundle.trajectories
    ]


@pytest.mark.parametrize(
    "records",
    [
        [_case_record(), _case_record()],
        [{**_case_record(), "split": "dev"}],
        [_case_record(trial=5)],
    ],
)
def test_writer_rejects_invalid_partial_record_sets(
    tmp_path: Path,
    records: list[dict[str, object]],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        write_formal_failure_bundle(
            output_root=tmp_path / "failed-attempts",
            context=_context(run_id="formal-failed-20260729-a3"),
            case_records=records,
            records_captured=True,
            budget_summary=None,
        )


def test_integrity_tampering_is_rejected(tmp_path: Path) -> None:
    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id="formal-failed-20260729-a4"),
        case_records=[_case_record()],
        records_captured=True,
        budget_summary=None,
    )
    summary_path = bundle_path / "summary.json"
    summary_path.write_text(
        summary_path.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactIntegrityError):
        validate_formal_failure_bundle(bundle_path)


def test_logical_content_tampering_is_rejected_after_reindex(
    tmp_path: Path,
) -> None:
    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id="formal-failed-20260729-a5"),
        case_records=[_case_record()],
        records_captured=True,
        budget_summary=None,
    )
    trajectory_path = (
        bundle_path / "trajectories" / "formal-case-01" / "1.json"
    )
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    trajectory["final_text"] = "forged divergent trajectory"
    trajectory_path.write_text(
        json.dumps(
            trajectory,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _refresh_integrity_entry(
        bundle_path,
        "trajectories/formal-case-01/1.json",
    )

    with pytest.raises(ValidationError, match="records and trajectories differ"):
        validate_formal_failure_bundle(bundle_path)


def test_permission_downgrade_is_rejected(tmp_path: Path) -> None:
    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id="formal-failed-20260729-a6"),
        case_records=[],
        records_captured=False,
        budget_summary=None,
    )
    (bundle_path / "summary.json").chmod(0o644)

    with pytest.raises(FormalFailureEvidenceError, match="owner-only"):
        validate_formal_failure_bundle(bundle_path)
