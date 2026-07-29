from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.config import Settings
from evals.calibration_attestation import (
    CalibrationAttestationError,
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
    require_canonical_calibration_runtime,
)
from evals.evidence import (
    ArtifactIntegrityError,
    stable_sha256,
    verify_eval_bundle,
    verify_private_eval_bundle_permissions,
)
from evals.evidence_schema import validate_readonly_payload
from evals.file_snapshot import (
    FileSnapshotError,
    read_file_snapshot,
    read_json_object_snapshot,
)
from evals.private_paths import PrivatePathError, require_private_input_file
from evals.readonly_eval import ReadonlyEvalCase
from evals.readonly_reporting import (
    current_readonly_harness_fingerprints,
    current_readonly_source_snapshot,
    current_source_tree_sha256,
    require_clean_git_worktree,
)


class HoldoutLockError(RuntimeError):
    """A declared formal holdout cannot be started or finalized safely."""


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
RunId = Annotated[
    str,
    Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$"),
]


class _StrictReceiptModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HoldoutStartReceipt(_StrictReceiptModel):
    schema_version: Literal["1.0"]
    case_set_name: Literal["readonly-holdout-v2"]
    case_set_sha256: Sha256
    manifest_sha256: Sha256
    source_git_commit: GitCommit
    scorer_version: str = Field(min_length=1)
    harness_sha256: Sha256
    semantic_calibration_report_sha256: Sha256
    semantic_calibration_review_sha256: Sha256
    semantic_calibration_run_id: RunId
    semantic_calibration_source_git_commit: GitCommit
    semantic_calibration_fixture_sha256: Sha256
    semantic_calibration_contract_set_sha256: Sha256
    semantic_calibration_harness_sha256: Sha256
    semantic_calibration_reviewer_id: str = Field(min_length=8)
    semantic_calibration_reviewed_count: int = Field(ge=1)
    public_regression_bundle_integrity_sha256: Sha256
    public_regression_gate_sha256: Sha256
    public_regression_run_id: RunId
    public_regression_source_git_commit: GitCommit
    public_regression_case_set_name: Literal["readonly-regression-v1"]
    public_regression_case_set_sha256: Sha256
    public_regression_harness_sha256: Sha256
    run_id: RunId
    status: Literal["started"]
    created_at: datetime
    completed_at: Literal[None]

    @model_validator(mode="after")
    def require_aware_created_at(self) -> HoldoutStartReceipt:
        if self.created_at.tzinfo is None:
            raise ValueError("Holdout start timestamp must be timezone-aware.")
        return self


class HoldoutTerminalReceipt(_StrictReceiptModel):
    schema_version: Literal["2.0"]
    run_id: RunId
    status: Literal["completed", "failed"]
    lock_start_receipt_sha256: Sha256
    bundle_integrity_sha256: Sha256 | None
    attempt_bundle_integrity_sha256: Sha256 | None
    failure_evidence_status: Literal["captured", "unavailable"] | None
    completed_at: datetime

    @model_validator(mode="after")
    def validate_terminal_state(self) -> HoldoutTerminalReceipt:
        if self.completed_at.tzinfo is None:
            raise ValueError(
                "Holdout terminal timestamp must be timezone-aware."
            )
        if self.status == "completed":
            valid = (
                self.bundle_integrity_sha256 is not None
                and self.attempt_bundle_integrity_sha256 is None
                and self.failure_evidence_status is None
            )
        else:
            valid = (
                self.bundle_integrity_sha256 is None
                and (
                    (
                        self.attempt_bundle_integrity_sha256 is not None
                        and self.failure_evidence_status == "captured"
                    )
                    or (
                        self.attempt_bundle_integrity_sha256 is None
                        and self.failure_evidence_status == "unavailable"
                    )
                )
            )
        if not valid:
            raise ValueError("Holdout terminal receipt state is inconsistent.")
        return self


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
    regression_bundle_integrity_sha256: str
    regression_gate_sha256: str
    regression_run_id: str
    regression_source_git_commit: str
    regression_case_set_name: str
    regression_case_set_sha256: str
    regression_harness_sha256: str


@dataclass(frozen=True)
class ValidatedRegressionGate:
    """Verified public-regression identity accepted for one formal seal."""

    bundle_path: Path
    bundle_integrity_sha256: str
    gate_sha256: str
    run_id: str
    source_git_commit: str
    case_set_name: str
    case_set_sha256: str
    harness_sha256: str
    runtime_identity_sha256: str
    passed_trials: int


@dataclass(frozen=True)
class AcquiredHoldoutRunLock:
    path: Path
    receipt_sha256: str


def validate_regression_gate(
    *,
    bundle_path: Path,
    private_root: Path,
    source_git_commit: str,
    harness_sha256: str,
    expected_source_tree_sha256: str | None = None,
    expected_harness_fingerprints: Mapping[str, str] | None = None,
    settings: Settings | None = None,
) -> ValidatedRegressionGate:
    """Validate the fixed 7x4 public regression before any formal provider use."""

    absolute_bundle = Path(os.path.abspath(bundle_path))
    absolute_private_root = Path(os.path.abspath(private_root))
    try:
        absolute_bundle.resolve(strict=True).relative_to(
            absolute_private_root.resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise HoldoutLockError(
            "The formal public regression bundle is outside the private root."
        ) from exc
    try:
        verify_private_eval_bundle_permissions(absolute_bundle)
        payload = verify_eval_bundle(absolute_bundle)
        evidence = validate_readonly_payload(payload)
        integrity_sha256 = read_file_snapshot(
            absolute_bundle / "integrity.json"
        ).sha256
    except (
        ArtifactIntegrityError,
        FileSnapshotError,
        OSError,
        ValueError,
    ) as exc:
        raise HoldoutLockError(
            "The formal public regression bundle failed verification."
        ) from exc

    manifest = evidence.manifest
    summary = evidence.summary
    runtime_settings = settings or Settings()
    try:
        require_canonical_calibration_runtime(runtime_settings)
        trusted_source_git_commit = require_clean_git_worktree(
            expected_commit=source_git_commit
        )
        source_tree_before = current_source_tree_sha256()
        harness_fingerprints = current_readonly_harness_fingerprints(
            runtime_settings
        )
        source_snapshot = current_readonly_source_snapshot()
        source_tree_after = current_source_tree_sha256()
        require_clean_git_worktree(
            expected_commit=trusted_source_git_commit
        )
    except CalibrationAttestationError as exc:
        raise HoldoutLockError(
            "The formal public regression runtime is not canonical."
        ) from exc
    except (OSError, ValueError) as exc:
        raise HoldoutLockError(
            "The formal public regression trusted runtime is not clean."
        ) from exc
    current_harness_sha256 = stable_sha256(harness_fingerprints)
    if (
        source_tree_before != source_tree_after
        or source_snapshot["source_tree_sha256"] != source_tree_before
        or source_snapshot["git_commit"] != source_git_commit
        or (
            expected_source_tree_sha256 is not None
            and source_snapshot["source_tree_sha256"]
            != expected_source_tree_sha256
        )
        or (
            expected_harness_fingerprints is not None
            and dict(expected_harness_fingerprints)
            != harness_fingerprints
        )
        or harness_sha256 != current_harness_sha256
    ):
        raise HoldoutLockError(
            "The formal public regression trusted runtime changed."
        )

    expected_source = {
        **source_snapshot,
        "git_dirty": False,
    }
    expected_harness = {
        "runtime_harness_sha256": current_harness_sha256,
        "prompt_sha256": harness_fingerprints["prompt_sha256"],
        "tool_contracts_sha256": harness_fingerprints[
            "tool_contracts_sha256"
        ],
        "policies_sha256": harness_fingerprints["policies_sha256"],
        "seed_data_sha256": harness_fingerprints["seed_sha256"],
        "agent_loop_sha256": harness_fingerprints["agent_loop_sha256"],
        "model_runtime_sha256": harness_fingerprints[
            "model_runtime_sha256"
        ],
        "semantic_judge_version": harness_fingerprints[
            "semantic_judge_version"
        ],
        "semantic_judge_prompt_sha256": harness_fingerprints[
            "semantic_judge_prompt_sha256"
        ],
        "semantic_judge_source_sha256": harness_fingerprints[
            "semantic_judge_source_sha256"
        ],
        "semantic_calibration_source_sha256": harness_fingerprints[
            "semantic_calibration_source_sha256"
        ],
        "semantic_calibration_validator_sha256": harness_fingerprints[
            "semantic_calibration_validator_sha256"
        ],
        "semantic_calibration_runner_sha256": harness_fingerprints[
            "semantic_calibration_runner_sha256"
        ],
        "semantic_calibration_corpus_sha256": harness_fingerprints[
            "semantic_calibration_corpus_sha256"
        ],
        "evidence_protocol_sha256": harness_fingerprints[
            "evidence_protocol_sha256"
        ],
        "canonical_price_snapshot_sha256": harness_fingerprints[
            "canonical_price_snapshot_sha256"
        ],
        "max_tool_rounds": runtime_settings.agent_max_tool_rounds,
        "max_tool_calls": runtime_settings.agent_max_tool_calls,
    }
    endpoint = urlparse(runtime_settings.deepseek_base_url)
    expected_model = {
        "provider": "deepseek",
        "requested_model": runtime_settings.deepseek_model,
        "observed_models": [runtime_settings.deepseek_model],
        "base_url_host": endpoint.hostname,
        "generation_config": {
            "stream": False,
            "thinking": "disabled",
            "temperature": runtime_settings.deepseek_temperature,
            "seed": None,
            "max_tokens": runtime_settings.deepseek_max_tokens,
        },
        "timeout_seconds": runtime_settings.deepseek_timeout_seconds,
        "retry_policy": {
            "max_retries": runtime_settings.deepseek_max_retries,
            "backoff": "bounded_exponential_with_jitter",
        },
        "semantic_judge": {
            "version": harness_fingerprints["semantic_judge_version"],
            "response_format": "json_object",
            "tools_enabled": False,
            "temperature": runtime_settings.deepseek_temperature,
            "thinking": "disabled",
        },
    }
    if (
        manifest.purpose != "dev_repeat"
        or manifest.status != "completed"
        or manifest.eval.split != "dev"
        or manifest.eval.case_set_name != "readonly-regression-v1"
        or manifest.eval.case_count != 7
        or manifest.execution.planned_trials != 4
        or manifest.execution.completed_trials != 4
        or len(evidence.cases) != 28
        or summary.total_trials != 28
        or summary.planned_trials != 4
        or summary.strict.passed != 28
        or summary.strict.failed != 0
        or summary.strict.rate != 1
        or summary.security.passed != 28
        or summary.security.failed != 0
        or summary.security.rate != 1
        or summary.security.all_trials_passed is not True
        or summary.reliability.k != 4
        or summary.reliability.cases_all_trials_passed != 7
        or summary.reliability.case_count != 7
        or summary.reliability.pass_power_k != 1
        or summary.business_state.changed_trials != 0
        or summary.business_state.unknown_trials != 0
        or summary.business_state.all_trials_unchanged is not True
        or summary.errors
        or manifest.source.model_dump(mode="json") != expected_source
        or manifest.eval.scorer_version
        != harness_fingerprints["scorer_version"]
        or manifest.eval.scorer_sha256
        != harness_fingerprints["scorer_sha256"]
        or manifest.harness.model_dump(mode="json") != expected_harness
        or manifest.model.model_dump(mode="json") != expected_model
    ):
        raise HoldoutLockError(
            "The formal public regression gate is not canonical and passing."
        )

    runtime_identity_sha256 = stable_sha256(
        {
            "source": expected_source,
            "harness": expected_harness,
            "model": expected_model,
        }
    )
    gate_payload = {
        "bundle_integrity_sha256": integrity_sha256,
        "run_id": manifest.run_id,
        "source_git_commit": source_git_commit,
        "source_tree_sha256": source_snapshot["source_tree_sha256"],
        "case_set_name": manifest.eval.case_set_name,
        "case_set_sha256": manifest.eval.case_set_sha256,
        "harness_sha256": current_harness_sha256,
        "runtime_identity_sha256": runtime_identity_sha256,
        "passed_trials": 28,
    }
    return ValidatedRegressionGate(
        bundle_path=absolute_bundle,
        bundle_integrity_sha256=integrity_sha256,
        gate_sha256=stable_sha256(gate_payload),
        run_id=manifest.run_id,
        source_git_commit=source_git_commit,
        case_set_name=manifest.eval.case_set_name,
        case_set_sha256=manifest.eval.case_set_sha256,
        harness_sha256=current_harness_sha256,
        runtime_identity_sha256=runtime_identity_sha256,
        passed_trials=28,
    )


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


def _validate_start_receipt(
    payload: Mapping[str, Any],
) -> HoldoutStartReceipt:
    try:
        return HoldoutStartReceipt.model_validate(payload)
    except ValidationError as exc:
        raise HoldoutLockError(
            "The formal holdout start receipt failed its strict schema."
        ) from exc


def _validate_terminal_receipt(
    payload: Mapping[str, Any],
) -> HoldoutTerminalReceipt:
    try:
        return HoldoutTerminalReceipt.model_validate(payload)
    except ValidationError as exc:
        raise HoldoutLockError(
            "The formal holdout terminal receipt failed its strict schema."
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
    regression_gate: ValidatedRegressionGate | None = None,
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
    if (
        calibration_attestation is None
        or calibration_review is None
        or regression_gate is None
    ):
        raise HoldoutLockError(
            "The formal holdout requires validated calibration and "
            "regression attestations."
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
        "public_regression_bundle_integrity_sha256",
        "public_regression_gate_sha256",
        "public_regression_run_id",
        "public_regression_source_git_commit",
        "public_regression_case_set_name",
        "public_regression_case_set_sha256",
        "public_regression_harness_sha256",
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

    expected_regression_fields: dict[str, object] = {
        "public_regression_bundle_integrity_sha256": (
            regression_gate.bundle_integrity_sha256
        ),
        "public_regression_gate_sha256": regression_gate.gate_sha256,
        "public_regression_run_id": regression_gate.run_id,
        "public_regression_source_git_commit": (
            regression_gate.source_git_commit
        ),
        "public_regression_case_set_name": (
            regression_gate.case_set_name
        ),
        "public_regression_case_set_sha256": (
            regression_gate.case_set_sha256
        ),
        "public_regression_harness_sha256": (
            regression_gate.harness_sha256
        ),
    }
    if (
        regression_gate.source_git_commit
        != str(manifest.get("source_git_commit"))
        or regression_gate.harness_sha256
        != stable_sha256(current_harness)
        or any(
            manifest.get(field_name) != expected
            for field_name, expected in expected_regression_fields.items()
        )
    ):
        raise HoldoutLockError(
            "The formal holdout frozen harness or public regression "
            "attestation does not match."
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
        regression_bundle_integrity_sha256=(
            regression_gate.bundle_integrity_sha256
        ),
        regression_gate_sha256=regression_gate.gate_sha256,
        regression_run_id=regression_gate.run_id,
        regression_source_git_commit=(
            regression_gate.source_git_commit
        ),
        regression_case_set_name=regression_gate.case_set_name,
        regression_case_set_sha256=regression_gate.case_set_sha256,
        regression_harness_sha256=regression_gate.harness_sha256,
    )


def _write_exclusive_json(path: Path, payload: dict[str, Any]) -> str:
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
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise HoldoutLockError(
            "This formal holdout declaration has already been consumed."
        ) from exc
    try:
        assert descriptor is not None
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("exclusive receipt write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return hashlib.sha256(data).hexdigest()


def acquire_holdout_run_lock_with_hash(
    *,
    lock_root: Path,
    declaration: HoldoutDeclaration,
    run_id: str,
    now: datetime | None = None,
) -> AcquiredHoldoutRunLock:
    """Consume the run and bind the exact receipt bytes without rereading."""

    for candidate in (lock_root, *lock_root.parents):
        if candidate.is_symlink():
            raise HoldoutLockError(
                "The private holdout lock path cannot contain a symlink."
            )
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_root.chmod(0o700)
    lock_path = lock_root / "readonly-holdout-v2.start.json"
    if lock_path.exists():
        raise HoldoutLockError(
            "This formal holdout declaration has already been consumed."
        )
    created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    receipt_payload = _validate_start_receipt(
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
            "public_regression_bundle_integrity_sha256": (
                declaration.regression_bundle_integrity_sha256
            ),
            "public_regression_gate_sha256": (
                declaration.regression_gate_sha256
            ),
            "public_regression_run_id": declaration.regression_run_id,
            "public_regression_source_git_commit": (
                declaration.regression_source_git_commit
            ),
            "public_regression_case_set_name": (
                declaration.regression_case_set_name
            ),
            "public_regression_case_set_sha256": (
                declaration.regression_case_set_sha256
            ),
            "public_regression_harness_sha256": (
                declaration.regression_harness_sha256
            ),
            "run_id": run_id,
            "status": "started",
            "created_at": created_at,
            "completed_at": None,
        },
    )
    receipt_sha256 = _write_exclusive_json(
        lock_path,
        receipt_payload.model_dump(mode="json"),
    )
    return AcquiredHoldoutRunLock(
        path=lock_path,
        receipt_sha256=receipt_sha256,
    )


def acquire_holdout_run_lock(
    *,
    lock_root: Path,
    declaration: HoldoutDeclaration,
    run_id: str,
    now: datetime | None = None,
) -> Path:
    """Consume the one formal run immediately before provider use."""

    return acquire_holdout_run_lock_with_hash(
        lock_root=lock_root,
        declaration=declaration,
        run_id=run_id,
        now=now,
    ).path


def _require_private_bundle_directory(
    path: Path,
    *,
    private_root: Path,
) -> Path:
    absolute = Path(os.path.abspath(path))
    root = Path(os.path.abspath(private_root))
    try:
        absolute.resolve(strict=True).relative_to(root.resolve(strict=True))
        current = absolute
        while True:
            mode = current.lstat().st_mode
            if (
                stat.S_ISLNK(mode)
                or not stat.S_ISDIR(mode)
                or stat.S_IMODE(mode) != 0o700
            ):
                raise HoldoutLockError(
                    "The private formal bundle ancestry must use 0700 "
                    "directories without symlinks."
                )
            if current == root:
                break
            if root not in current.parents:
                raise HoldoutLockError(
                    "The private formal bundle is outside the fixed root."
                )
            current = current.parent
    except (OSError, ValueError) as exc:
        raise HoldoutLockError(
            "The private formal bundle is outside the fixed root."
        ) from exc
    return absolute


def _require_private_completed_chain_paths(
    *,
    manifest_path: Path,
    start_path: Path,
    terminal_path: Path,
    bundle_path: Path,
    private_root: Path,
) -> None:
    for path, label in (
        (manifest_path, "holdout manifest"),
        (start_path, "holdout start receipt"),
        (terminal_path, "holdout terminal receipt"),
    ):
        try:
            require_private_input_file(
                path,
                private_root=private_root,
                label=label,
            )
        except PrivatePathError as exc:
            raise HoldoutLockError(
                f"The private {label} is outside the fixed root or "
                "has unsafe permissions."
            ) from exc
    try:
        private_bundle = _require_private_bundle_directory(
            bundle_path,
            private_root=private_root,
        )
        verify_private_eval_bundle_permissions(private_bundle)
    except ArtifactIntegrityError as exc:
        raise HoldoutLockError(
            "The private formal bundle permissions are invalid."
        ) from exc


def holdout_lock_receipt_sha256(lock_path: Path) -> str:
    """Hash the immutable start receipt without following a symlink."""

    payload, receipt_sha256 = _read_manifest_with_sha256(lock_path)
    _validate_start_receipt(payload)
    return receipt_sha256


def finalize_holdout_run_lock(
    *,
    lock_path: Path,
    status: Literal["completed", "failed"],
    run_id: str,
    expected_start_receipt_sha256: str,
    bundle_integrity_sha256: str | None = None,
    attempt_bundle_integrity_sha256: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Append an immutable terminal receipt while preserving the start hash."""

    payload, start_receipt_sha256 = _read_manifest_with_sha256(
        lock_path
    )
    start_receipt = _validate_start_receipt(payload)
    if (
        not _is_sha256(expected_start_receipt_sha256)
        or start_receipt_sha256 != expected_start_receipt_sha256
    ):
        raise HoldoutLockError(
            "The formal holdout start receipt changed before finalization."
        )
    if start_receipt.run_id != run_id:
        raise HoldoutLockError(
            "The formal holdout lock cannot be finalized in its current state."
        )
    if (
        status == "completed"
        and (
            not _is_sha256(bundle_integrity_sha256)
            or attempt_bundle_integrity_sha256 is not None
        )
    ):
        raise HoldoutLockError(
            "A completed holdout requires the bundle integrity hash."
        )
    if status == "failed" and (
        bundle_integrity_sha256 is not None
        or (
            attempt_bundle_integrity_sha256 is not None
            and not _is_sha256(attempt_bundle_integrity_sha256)
        )
    ):
        raise HoldoutLockError(
            "A failed holdout has invalid or conflicting bundle evidence."
        )
    terminal_path = lock_path.with_name(
        "readonly-holdout-v2.terminal.json"
    )
    terminal_payload = HoldoutTerminalReceipt.model_validate(
        {
            "schema_version": "2.0",
            "run_id": run_id,
            "status": status,
            "lock_start_receipt_sha256": start_receipt_sha256,
            "bundle_integrity_sha256": bundle_integrity_sha256,
            "attempt_bundle_integrity_sha256": (
                attempt_bundle_integrity_sha256
            ),
            "failure_evidence_status": (
                (
                    "captured"
                    if attempt_bundle_integrity_sha256 is not None
                    else "unavailable"
                )
                if status == "failed"
                else None
            ),
            "completed_at": (
                now or datetime.now(UTC)
            ).astimezone(UTC).isoformat(),
        },
    )
    _write_exclusive_json(
        terminal_path,
        terminal_payload.model_dump(mode="json"),
    )
    return terminal_path


def verify_holdout_receipt_chain(
    *,
    manifest_path: Path,
    start_path: Path,
    terminal_path: Path,
    bundle_path: Path,
    regression_bundle_path: Path,
    private_root: Path,
) -> None:
    """Verify sealed manifest -> start -> bundle -> terminal hash links."""

    _require_private_completed_chain_paths(
        manifest_path=manifest_path,
        start_path=start_path,
        terminal_path=terminal_path,
        bundle_path=bundle_path,
        private_root=private_root,
    )
    manifest, manifest_sha256 = _read_manifest_with_sha256(
        manifest_path
    )
    start, start_sha256 = _read_manifest_with_sha256(start_path)
    terminal, _ = _read_manifest_with_sha256(terminal_path)
    start_receipt = _validate_start_receipt(start)
    terminal_receipt = _validate_terminal_receipt(terminal)
    if (
        terminal_receipt.status != "completed"
        or terminal_receipt.completed_at < start_receipt.created_at
    ):
        raise HoldoutLockError(
            "The completed holdout receipt timestamps are invalid."
        )
    try:
        regression_gate = validate_regression_gate(
            bundle_path=regression_bundle_path,
            private_root=private_root,
            source_git_commit=str(
                start.get("public_regression_source_git_commit")
            ),
            harness_sha256=str(
                start.get("public_regression_harness_sha256")
            ),
        )
    except HoldoutLockError as exc:
        raise HoldoutLockError(
            "The bound formal public regression bundle is invalid."
        ) from exc
    try:
        verified_bundle = verify_eval_bundle(bundle_path)
        validate_readonly_payload(verified_bundle)
        integrity_sha256 = read_file_snapshot(
            bundle_path / "integrity.json"
        ).sha256
    except (
        ArtifactIntegrityError,
        FileSnapshotError,
        OSError,
        ValueError,
    ) as exc:
        raise HoldoutLockError(
            "The formal holdout bundle failed integrity or schema validation."
        ) from exc
    bundle_manifest = verified_bundle["manifest"]

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
        "semantic_calibration_fixture_sha256": "fixture_sha256",
        "semantic_calibration_contract_set_sha256": (
            "contract_set_sha256"
        ),
        "semantic_calibration_harness_sha256": "harness_sha256",
        "semantic_calibration_reviewer_id": "reviewer_id",
        "semantic_calibration_reviewed_count": "reviewed_count",
    }
    regression_links = {
        "public_regression_bundle_integrity_sha256": (
            "regression_bundle_integrity_sha256"
        ),
        "public_regression_gate_sha256": "regression_gate_sha256",
        "public_regression_run_id": "regression_run_id",
        "public_regression_source_git_commit": (
            "regression_source_git_commit"
        ),
        "public_regression_case_set_name": (
            "regression_case_set_name"
        ),
        "public_regression_case_set_sha256": (
            "regression_case_set_sha256"
        ),
        "public_regression_harness_sha256": (
            "regression_harness_sha256"
        ),
    }
    if (
        start.get("status") != "started"
        or terminal.get("status") != "completed"
        or terminal.get("attempt_bundle_integrity_sha256")
        is not None
        or terminal.get("failure_evidence_status") is not None
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
        or regression_gate.bundle_integrity_sha256
        != start.get("public_regression_bundle_integrity_sha256")
        or regression_gate.gate_sha256
        != start.get("public_regression_gate_sha256")
        or regression_gate.run_id
        != start.get("public_regression_run_id")
        or regression_gate.case_set_name
        != start.get("public_regression_case_set_name")
        or regression_gate.case_set_sha256
        != start.get("public_regression_case_set_sha256")
        or bundle_harness.get("runtime_harness_sha256")
        != harness_sha256
        or manifest.get("case_set_name")
        != start.get("case_set_name")
        or manifest.get("case_set_sha256")
        != start.get("case_set_sha256")
        or bundle_eval.get("case_set_name")
        != start.get("case_set_name")
        or bundle_eval.get("case_set_sha256")
        != start.get("case_set_sha256")
        or manifest.get("scorer_version")
        != start.get("scorer_version")
        or bundle_eval.get("scorer_version")
        != start.get("scorer_version")
        or manifest.get("source_git_commit")
        != source_git_commit
        or bundle_source.get("git_commit") != source_git_commit
        or any(
            manifest.get(start_field) != start.get(start_field)
            or calibration.get(bundle_field) != start.get(start_field)
            for start_field, bundle_field in calibration_links.items()
        )
        or any(
            manifest.get(start_field) != start.get(start_field)
            or formal.get(bundle_field) != start.get(start_field)
            for start_field, bundle_field in regression_links.items()
        )
    ):
        raise HoldoutLockError(
            "The completed formal holdout chain or harness does not match."
        )


def verify_failed_holdout_receipt_chain(
    *,
    manifest_path: Path,
    start_path: Path,
    terminal_path: Path,
    bundle_path: Path,
    regression_bundle_path: Path,
    private_root: Path,
) -> None:
    """Verify sealed manifest -> start -> failed-attempt bundle -> terminal."""

    from evals.formal_failure_evidence import (  # noqa: PLC0415
        FormalFailureEvidenceError,
        validate_formal_failure_bundle,
    )

    _require_private_completed_chain_paths(
        manifest_path=manifest_path,
        start_path=start_path,
        terminal_path=terminal_path,
        bundle_path=bundle_path,
        private_root=private_root,
    )
    manifest, manifest_sha256 = _read_manifest_with_sha256(
        manifest_path
    )
    start, start_sha256 = _read_manifest_with_sha256(start_path)
    terminal, _ = _read_manifest_with_sha256(terminal_path)
    start_receipt = _validate_start_receipt(start)
    terminal_receipt = _validate_terminal_receipt(terminal)
    if (
        terminal_receipt.status != "failed"
        or terminal_receipt.completed_at < start_receipt.created_at
    ):
        raise HoldoutLockError(
            "The failed holdout receipt timestamps are invalid."
        )
    try:
        regression_gate = validate_regression_gate(
            bundle_path=regression_bundle_path,
            private_root=private_root,
            source_git_commit=str(
                start.get("public_regression_source_git_commit")
            ),
            harness_sha256=str(
                start.get("public_regression_harness_sha256")
            ),
        )
    except HoldoutLockError as exc:
        raise HoldoutLockError(
            "The bound failed-attempt public regression bundle is invalid."
        ) from exc
    try:
        failed_bundle = validate_formal_failure_bundle(bundle_path)
        integrity_sha256 = read_file_snapshot(
            bundle_path / "integrity.json"
        ).sha256
    except (
        ArtifactIntegrityError,
        FileSnapshotError,
        FormalFailureEvidenceError,
        OSError,
        ValueError,
    ) as exc:
        raise HoldoutLockError(
            "The failed formal attempt bundle is invalid."
        ) from exc

    failure_manifest = failed_bundle.manifest
    failure_bindings = failure_manifest.formal_holdout
    calibration_fields = (
        "semantic_calibration_report_sha256",
        "semantic_calibration_review_sha256",
        "semantic_calibration_run_id",
        "semantic_calibration_source_git_commit",
        "semantic_calibration_fixture_sha256",
        "semantic_calibration_contract_set_sha256",
        "semantic_calibration_harness_sha256",
        "semantic_calibration_reviewer_id",
        "semantic_calibration_reviewed_count",
    )
    regression_fields = (
        "public_regression_bundle_integrity_sha256",
        "public_regression_gate_sha256",
        "public_regression_run_id",
        "public_regression_source_git_commit",
        "public_regression_case_set_name",
        "public_regression_case_set_sha256",
        "public_regression_harness_sha256",
    )
    if (
        start.get("status") != "started"
        or terminal.get("status") != "failed"
        or terminal.get("bundle_integrity_sha256") is not None
        or terminal.get("failure_evidence_status") != "captured"
        or terminal.get("attempt_bundle_integrity_sha256")
        != integrity_sha256
        or terminal.get("lock_start_receipt_sha256")
        != start_sha256
        or terminal.get("run_id") != start.get("run_id")
        or failure_manifest.run_id != start.get("run_id")
        or start.get("manifest_sha256") != manifest_sha256
        or failure_bindings.declaration_manifest_sha256
        != manifest_sha256
        or failure_bindings.lock_start_receipt_sha256
        != start_sha256
        or failure_bindings.declared_harness_sha256
        != start.get("harness_sha256")
        or failure_bindings.runtime_harness_sha256
        != start.get("harness_sha256")
        or failure_manifest.source.git_commit
        != start.get("source_git_commit")
        or manifest.get("source_git_commit")
        != start.get("source_git_commit")
        or failure_manifest.case_set.name
        != start.get("case_set_name")
        or failure_manifest.case_set.sha256
        != start.get("case_set_sha256")
        or manifest.get("case_set_name")
        != start.get("case_set_name")
        or manifest.get("case_set_sha256")
        != start.get("case_set_sha256")
        or any(
            manifest.get(field_name) != start.get(field_name)
            for field_name in calibration_fields
        )
        or any(
            manifest.get(field_name) != start.get(field_name)
            for field_name in regression_fields
        )
        or failure_bindings.regression_bundle_integrity_sha256
        != start.get("public_regression_bundle_integrity_sha256")
        or failure_bindings.regression_gate_sha256
        != start.get("public_regression_gate_sha256")
        or failure_bindings.regression_run_id
        != start.get("public_regression_run_id")
        or failure_bindings.regression_source_git_commit
        != start.get("public_regression_source_git_commit")
        or failure_bindings.regression_case_set_name
        != start.get("public_regression_case_set_name")
        or failure_bindings.regression_case_set_sha256
        != start.get("public_regression_case_set_sha256")
        or failure_bindings.regression_harness_sha256
        != start.get("public_regression_harness_sha256")
        or regression_gate.bundle_integrity_sha256
        != start.get("public_regression_bundle_integrity_sha256")
        or regression_gate.gate_sha256
        != start.get("public_regression_gate_sha256")
        or regression_gate.run_id
        != start.get("public_regression_run_id")
        or regression_gate.case_set_name
        != start.get("public_regression_case_set_name")
        or regression_gate.case_set_sha256
        != start.get("public_regression_case_set_sha256")
    ):
        raise HoldoutLockError(
            "The failed formal holdout receipt chain does not match."
        )
