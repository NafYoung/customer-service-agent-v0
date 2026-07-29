from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from app.config import Settings
from evals.calibration_attestation import (
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
)
from evals.evidence import stable_sha256
from evals.file_snapshot import (
    FileSnapshotError,
    read_file_snapshot,
    read_json_object_snapshot,
)
from evals.readonly_eval import ReadonlyEvalCase
from evals.readonly_reporting import current_readonly_harness_fingerprints


class HoldoutLockError(RuntimeError):
    """A declared formal holdout cannot be started or finalized safely."""


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_git_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class HoldoutDeclaration:
    case_set_name: str
    case_set_sha256: str
    manifest_sha256: str
    source_git_commit: str
    scorer_version: str
    calibration_report_sha256: str
    calibration_review_sha256: str
    calibration_run_id: str
    calibration_source_git_commit: str
    calibration_fixture_sha256: str
    calibration_contract_set_sha256: str
    calibration_harness_sha256: str
    calibration_reviewer_id: str
    calibration_reviewed_count: int
    harness_sha256: str


def _read_manifest_with_sha256(
    path: Path,
) -> tuple[dict[str, Any], str]:
    try:
        return read_json_object_snapshot(
            path,
            label="holdout manifest or receipt",
        )
    except FileSnapshotError as exc:
        raise HoldoutLockError(
            "The declared holdout manifest could not be read safely."
        ) from exc


def validate_holdout_declaration(
    *,
    manifest_path: Path,
    case_set_name: str,
    cases: Sequence[ReadonlyEvalCase],
    settings: Settings | None = None,
    calibration_attestation: (
        ValidatedCalibrationAttestation | None
    ) = None,
    calibration_review: ValidatedCalibrationReview | None = None,
    harness_fingerprints: Mapping[str, str] | None = None,
    source_git_commit: str | None = None,
) -> HoldoutDeclaration:
    """Bind a sealed case set to the exact current read-only harness."""

    manifest, manifest_sha256 = _read_manifest_with_sha256(
        manifest_path
    )
    if (
        type(manifest.get("formal_runs_allowed")) is not int
        or manifest.get("formal_runs_allowed") != 1
        or type(manifest.get("formal_runs_completed")) is not int
        or manifest.get("formal_runs_completed") != 0
        or manifest.get("lifecycle_status") != "sealed"
        or manifest.get("rerun_policy") != "prohibited"
    ):
        raise HoldoutLockError(
            "The declared formal run is no longer available."
        )
    if manifest.get("case_set_name") != case_set_name:
        raise HoldoutLockError(
            "The declared holdout name does not match the requested case set."
        )
    if len(cases) != 20 or any(
        case.expected.semantic_contract is None
        for case in cases
    ):
        raise HoldoutLockError(
            "A formal holdout requires exactly 20 semantic-scored cases."
        )
    if calibration_attestation is None or calibration_review is None:
        raise HoldoutLockError(
            "The formal holdout requires validated calibration attestations."
        )
    if (
        source_git_commit is not None
        and calibration_attestation.source_git_commit
        != source_git_commit
    ):
        raise HoldoutLockError(
            "The calibration and holdout source commits must match."
        )
    current_harness = (
        dict(harness_fingerprints)
        if harness_fingerprints is not None
        else current_readonly_harness_fingerprints(settings)
    )
    required_manifest_fields = {
        "schema_version",
        "case_set_name",
        "case_count",
        "case_set_sha256",
        "formal_runs_allowed",
        "formal_runs_completed",
        "lifecycle_status",
        "rerun_policy",
        "sealed_at",
        "sealer_id",
        "source_git_commit",
        "implementation_independence_declared",
        "semantic_calibration_report_sha256",
        "semantic_calibration_review_sha256",
        "semantic_calibration_run_id",
        "semantic_calibration_source_git_commit",
        "semantic_calibration_fixture_sha256",
        "semantic_calibration_contract_set_sha256",
        "semantic_calibration_harness_sha256",
        "semantic_calibration_reviewer_id",
        "semantic_calibration_reviewed_count",
        *current_harness,
    }
    if (
        set(manifest) != required_manifest_fields
        or manifest.get("schema_version") != "2.0"
        or manifest.get("case_set_name") != "readonly-holdout-v2"
        or not isinstance(manifest.get("sealed_at"), str)
        or not isinstance(manifest.get("sealer_id"), str)
        or len(manifest.get("sealer_id", "")) < 8
        or not _is_git_commit(manifest.get("source_git_commit"))
        or manifest.get("implementation_independence_declared") is not True
        or (
            source_git_commit is not None
            and manifest.get("source_git_commit") != source_git_commit
        )
    ):
        raise HoldoutLockError(
            "The formal holdout manifest failed its strict v2 schema."
        )

    expected_calibration_fields: dict[str, object] = {
        "semantic_calibration_report_sha256": (
            calibration_attestation.report_sha256
        ),
        "semantic_calibration_review_sha256": (
            calibration_review.review_sha256
        ),
        "semantic_calibration_run_id": calibration_attestation.run_id,
        "semantic_calibration_source_git_commit": (
            calibration_attestation.source_git_commit
        ),
        "semantic_calibration_fixture_sha256": (
            calibration_attestation.fixture_sha256
        ),
        "semantic_calibration_contract_set_sha256": (
            calibration_attestation.contract_set_sha256
        ),
        "semantic_calibration_harness_sha256": (
            calibration_attestation.harness_sha256
        ),
        "semantic_calibration_reviewer_id": (
            calibration_review.reviewer_id
        ),
        "semantic_calibration_reviewed_count": (
            calibration_review.reviewed_count
        ),
    }
    if any(
        manifest.get(field_name) != expected
        for field_name, expected in expected_calibration_fields.items()
    ):
        raise HoldoutLockError(
            "The formal holdout calibration attestations do not match."
        )

    case_payloads = [
        case.model_dump(mode="json")
        for case in sorted(cases, key=lambda item: item.case_id)
    ]
    case_set_sha256 = stable_sha256(case_payloads)
    if (
        type(manifest.get("case_count")) is not int
        or manifest.get("case_count") != len(cases)
        or manifest.get("case_set_sha256") != case_set_sha256
    ):
        raise HoldoutLockError(
            "The declared holdout cases do not match the sealed case set."
        )

    if any(
        manifest.get(field_name) != expected
        for field_name, expected in current_harness.items()
    ):
        raise HoldoutLockError(
            "The current code does not match the frozen harness declaration."
        )

    return HoldoutDeclaration(
        case_set_name=case_set_name,
        case_set_sha256=case_set_sha256,
        manifest_sha256=manifest_sha256,
        source_git_commit=str(manifest["source_git_commit"]),
        scorer_version=current_harness["scorer_version"],
        calibration_report_sha256=calibration_attestation.report_sha256,
        calibration_review_sha256=calibration_review.review_sha256,
        calibration_run_id=calibration_attestation.run_id,
        calibration_source_git_commit=(
            calibration_attestation.source_git_commit
        ),
        calibration_fixture_sha256=(
            calibration_attestation.fixture_sha256
        ),
        calibration_contract_set_sha256=(
            calibration_attestation.contract_set_sha256
        ),
        calibration_harness_sha256=(
            calibration_attestation.harness_sha256
        ),
        calibration_reviewer_id=calibration_review.reviewer_id,
        calibration_reviewed_count=calibration_review.reviewed_count,
        harness_sha256=stable_sha256(current_harness),
    )


def _write_exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise HoldoutLockError(
            "This formal holdout declaration has already been consumed."
        ) from exc
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("exclusive receipt write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def acquire_holdout_run_lock(
    *,
    lock_root: Path,
    declaration: HoldoutDeclaration,
    run_id: str,
    now: datetime | None = None,
) -> Path:
    """Consume the one formal run immediately before provider use."""

    if lock_root.exists() and lock_root.is_symlink():
        raise HoldoutLockError("The private holdout lock root cannot be a symlink.")
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_root.chmod(0o700)
    lock_path = lock_root / "readonly-holdout-v2.start.json"
    created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    _write_exclusive_json(
        lock_path,
        {
            "schema_version": "1.0",
            "case_set_name": declaration.case_set_name,
            "case_set_sha256": declaration.case_set_sha256,
            "manifest_sha256": declaration.manifest_sha256,
            "source_git_commit": declaration.source_git_commit,
            "scorer_version": declaration.scorer_version,
            "harness_sha256": declaration.harness_sha256,
            "semantic_calibration_report_sha256": (
                declaration.calibration_report_sha256
            ),
            "semantic_calibration_review_sha256": (
                declaration.calibration_review_sha256
            ),
            "semantic_calibration_run_id": declaration.calibration_run_id,
            "semantic_calibration_source_git_commit": (
                declaration.calibration_source_git_commit
            ),
            "semantic_calibration_fixture_sha256": (
                declaration.calibration_fixture_sha256
            ),
            "semantic_calibration_contract_set_sha256": (
                declaration.calibration_contract_set_sha256
            ),
            "semantic_calibration_harness_sha256": (
                declaration.calibration_harness_sha256
            ),
            "semantic_calibration_reviewer_id": (
                declaration.calibration_reviewer_id
            ),
            "semantic_calibration_reviewed_count": (
                declaration.calibration_reviewed_count
            ),
            "run_id": run_id,
            "status": "started",
            "created_at": created_at,
            "completed_at": None,
        },
    )
    return lock_path


def holdout_lock_receipt_sha256(lock_path: Path) -> str:
    """Hash the immutable start receipt without following a symlink."""

    payload, receipt_sha256 = _read_manifest_with_sha256(lock_path)
    if payload.get("status") != "started":
        raise HoldoutLockError(
            "The formal holdout start receipt is invalid."
        )
    return receipt_sha256


def finalize_holdout_run_lock(
    *,
    lock_path: Path,
    status: Literal["completed", "failed"],
    run_id: str,
    expected_start_receipt_sha256: str,
    bundle_integrity_sha256: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Append an immutable terminal receipt while preserving the start hash."""

    payload, start_receipt_sha256 = _read_manifest_with_sha256(
        lock_path
    )
    if (
        not _is_sha256(expected_start_receipt_sha256)
        or start_receipt_sha256 != expected_start_receipt_sha256
    ):
        raise HoldoutLockError(
            "The formal holdout start receipt changed before finalization."
        )
    if payload.get("run_id") != run_id or payload.get("status") != "started":
        raise HoldoutLockError(
            "The formal holdout lock cannot be finalized in its current state."
        )
    if (
        status == "completed"
        and (
            not _is_sha256(bundle_integrity_sha256)
        )
    ):
        raise HoldoutLockError(
            "A completed holdout requires the bundle integrity hash."
        )
    if status == "failed" and bundle_integrity_sha256 is not None:
        raise HoldoutLockError(
            "A failed holdout cannot claim a completed bundle."
        )
    terminal_path = lock_path.with_name(
        "readonly-holdout-v2.terminal.json"
    )
    _write_exclusive_json(
        terminal_path,
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "status": status,
            "lock_start_receipt_sha256": start_receipt_sha256,
            "bundle_integrity_sha256": bundle_integrity_sha256,
            "completed_at": (
                now or datetime.now(UTC)
            ).astimezone(UTC).isoformat(),
        },
    )
    return terminal_path


def verify_holdout_receipt_chain(
    *,
    manifest_path: Path,
    start_path: Path,
    terminal_path: Path,
    bundle_path: Path,
) -> None:
    """Verify sealed manifest -> start -> bundle -> terminal hash links."""

    manifest, manifest_sha256 = _read_manifest_with_sha256(
        manifest_path
    )
    start, start_sha256 = _read_manifest_with_sha256(start_path)
    terminal, _ = _read_manifest_with_sha256(terminal_path)
    bundle_manifest, _ = _read_manifest_with_sha256(
        bundle_path / "manifest.json"
    )
    try:
        integrity_sha256 = read_file_snapshot(
            bundle_path / "integrity.json"
        ).sha256
    except FileSnapshotError as exc:
        raise HoldoutLockError(
            "The formal holdout bundle integrity file is unreadable."
        ) from exc

    bundle_eval = bundle_manifest.get("eval")
    bundle_source = bundle_manifest.get("source")
    bundle_harness = bundle_manifest.get("harness")
    if not all(
        isinstance(value, dict)
        for value in (
            bundle_eval,
            bundle_source,
            bundle_harness,
        )
    ):
        raise HoldoutLockError(
            "The formal holdout bundle chain is incomplete."
        )
    assert isinstance(bundle_eval, dict)
    assert isinstance(bundle_source, dict)
    assert isinstance(bundle_harness, dict)
    formal = bundle_eval.get("formal_holdout")
    calibration = bundle_eval.get("semantic_calibration")
    if not isinstance(formal, dict) or not isinstance(
        calibration,
        dict,
    ):
        raise HoldoutLockError(
            "The formal holdout bundle chain is incomplete."
        )

    run_id = start.get("run_id")
    harness_sha256 = start.get("harness_sha256")
    source_git_commit = start.get("source_git_commit")
    calibration_links = {
        "semantic_calibration_report_sha256": "report_sha256",
        "semantic_calibration_review_sha256": "review_sha256",
        "semantic_calibration_run_id": "run_id",
        "semantic_calibration_source_git_commit": (
            "source_git_commit"
        ),
    }
    if (
        start.get("status") != "started"
        or terminal.get("status") != "completed"
        or not _is_sha256(harness_sha256)
        or start.get("manifest_sha256") != manifest_sha256
        or terminal.get("run_id") != run_id
        or bundle_manifest.get("run_id") != run_id
        or terminal.get("lock_start_receipt_sha256")
        != start_sha256
        or terminal.get("bundle_integrity_sha256")
        != integrity_sha256
        or formal.get("declaration_manifest_sha256")
        != manifest_sha256
        or formal.get("lock_start_receipt_sha256")
        != start_sha256
        or formal.get("declared_harness_sha256")
        != harness_sha256
        or bundle_harness.get("runtime_harness_sha256")
        != harness_sha256
        or manifest.get("source_git_commit")
        != source_git_commit
        or bundle_source.get("git_commit") != source_git_commit
        or any(
            manifest.get(start_field) != start.get(start_field)
            or calibration.get(bundle_field) != start.get(start_field)
            for start_field, bundle_field in calibration_links.items()
        )
    ):
        raise HoldoutLockError(
            "The completed formal holdout chain or harness does not match."
        )
