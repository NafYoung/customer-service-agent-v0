from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal, Protocol, Sequence
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.deepseek_budget import (
    BudgetError,
    DeepSeekBudgetGuard,
    SQLiteBudgetLedger,
)
from app.agent.factory import build_deepseek_client
from app.agent.openai_compatible import ChatModel
from app.config import Settings
from evals.calibration_attestation import (
    CANONICAL_AGENT_MAX_TOOL_CALLS,
    CANONICAL_AGENT_MAX_TOOL_ROUNDS,
    CANONICAL_DEEPSEEK_MAX_RETRIES,
    CANONICAL_DEEPSEEK_MAX_TOKENS,
    CANONICAL_DEEPSEEK_TIMEOUT_SECONDS,
    CalibrationAttestationError,
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
    require_canonical_calibration_runtime,
    validate_calibration_attestation,
    validate_calibration_review,
)
from evals.canonical_pricing import (
    FORMAL_EXECUTION_LIMIT_CNY,
    FORMAL_HARD_LIMIT_CNY,
    CanonicalPricingError,
    require_frozen_canonical_price,
)
from evals.evidence import stable_sha256, write_eval_bundle
from evals.evidence_schema import validate_readonly_bundle
from evals.file_snapshot import (
    FileSnapshotError,
    read_file_snapshot,
    read_json_object_snapshot,
)
from evals.formal_failure_evidence import (
    FormalFailureContext,
    write_formal_failure_bundle,
)
from evals.holdout_lock import (
    AcquiredHoldoutRunLock,
    HoldoutDeclaration,
    HoldoutLockError,
    ValidatedRegressionGate,
    acquire_holdout_run_lock_with_hash,
    finalize_holdout_run_lock,
    validate_holdout_declaration,
    validate_regression_gate,
    verify_failed_holdout_receipt_chain,
    verify_holdout_receipt_chain,
)
from evals.nonformal_paid_contract import (
    require_nonformal_paid_case_payload,
    require_nonformal_paid_case_set,
)
from evals.private_paths import (
    PrivatePathError,
    prepare_fixed_private_output_root,
    require_private_case_directory,
    require_private_input_file,
)
from evals.readonly_eval import (
    DEFAULT_CASE_DIR,
    ReadonlyEvalCase,
    ReadonlyEvalResult,
    load_cases,
    run_case,
)
from evals.readonly_reporting import (
    FormalHoldoutEvidence,
    FrozenReadonlyHarness,
    build_readonly_manifest,
    create_server_run_id,
    current_source_tree_sha256,
    freeze_readonly_harness,
    require_clean_git_worktree,
    result_to_record,
    summarize_results,
)
from evals.semantic_judge import SemanticJsonModel

PRIVATE_ARTIFACT_ROOT = ROOT / "artifacts" / "private"
DEFAULT_OUTPUT_ROOT = PRIVATE_ARTIFACT_ROOT / "eval-runs"
DEFAULT_BUDGET_LEDGER = (
    ROOT / "artifacts" / "private" / "deepseek-budget.sqlite3"
)
DEFAULT_HOLDOUT_LOCK_ROOT = (
    ROOT / "artifacts" / "private" / "holdout" / "formal-run-locks"
)
_FORMAL_CONTEXT_SENTINEL = object()
_ISSUED_FORMAL_CONTEXT_IDS: set[int] = set()


@dataclass(frozen=True)
class ValidatedFormalRunContext:
    """In-process capability created only after the formal start receipt."""

    run_id: str
    purpose: Literal["holdout_formal"]
    split: Literal["holdout"]
    case_set_name: str
    case_set_sha256: str
    planned_case_count: int
    planned_trials: int
    source_git_commit: str
    source_tree_sha256: str
    harness_sha256: str
    calibration_report_sha256: str
    calibration_review_sha256: str
    regression_bundle_integrity_sha256: str
    regression_gate_sha256: str
    regression_run_id: str
    regression_source_git_commit: str
    regression_case_set_name: str
    regression_case_set_sha256: str
    regression_harness_sha256: str
    declaration_manifest_sha256: str
    lock_start_path: Path
    lock_start_receipt_sha256: str
    output_root: Path
    _sentinel: object


def _formal_case_set_sha256(
    cases: Sequence[ReadonlyEvalCase],
) -> str:
    return stable_sha256(
        [
            case.model_dump(mode="json")
            for case in sorted(cases, key=lambda item: item.case_id)
        ]
    )


def _create_validated_formal_run_context(
    *,
    run_id: str,
    purpose: str,
    split: str,
    cases: Sequence[ReadonlyEvalCase],
    case_set_name: str,
    trials: int,
    source_git_commit: str,
    source_tree_sha256: str,
    frozen_harness: FrozenReadonlyHarness,
    calibration_attestation: ValidatedCalibrationAttestation,
    calibration_review: ValidatedCalibrationReview,
    declaration: HoldoutDeclaration,
    regression_gate: ValidatedRegressionGate,
    acquired_lock: AcquiredHoldoutRunLock,
) -> ValidatedFormalRunContext:
    """Create the formal execution capability after an exclusive start."""

    case_set_sha256 = _formal_case_set_sha256(cases)
    harness_sha256 = stable_sha256(dict(frozen_harness.fingerprints))
    if (
        purpose != "holdout_formal"
        or split != "holdout"
        or case_set_name != "readonly-holdout-v2"
        or len(cases) != 20
        or trials != 4
        or declaration.case_set_name != case_set_name
        or declaration.case_set_sha256 != case_set_sha256
        or declaration.source_git_commit != source_git_commit
        or declaration.harness_sha256 != harness_sha256
        or calibration_attestation.report_sha256
        != declaration.calibration_report_sha256
        or calibration_review.review_sha256
        != declaration.calibration_review_sha256
        or calibration_attestation.run_id
        != declaration.calibration_run_id
        or calibration_attestation.source_git_commit
        != declaration.calibration_source_git_commit
        or calibration_attestation.fixture_sha256
        != declaration.calibration_fixture_sha256
        or calibration_attestation.contract_set_sha256
        != declaration.calibration_contract_set_sha256
        or calibration_attestation.harness_sha256
        != declaration.calibration_harness_sha256
        or calibration_review.reviewer_id
        != declaration.calibration_reviewer_id
        or calibration_review.reviewed_count
        != declaration.calibration_reviewed_count
        or regression_gate.bundle_integrity_sha256
        != declaration.regression_bundle_integrity_sha256
        or regression_gate.gate_sha256
        != declaration.regression_gate_sha256
        or regression_gate.run_id != declaration.regression_run_id
        or regression_gate.source_git_commit != source_git_commit
        or regression_gate.case_set_name
        != declaration.regression_case_set_name
        or regression_gate.case_set_sha256
        != declaration.regression_case_set_sha256
        or regression_gate.harness_sha256 != harness_sha256
        or acquired_lock.path.name
        != "readonly-holdout-v2.start.json"
    ):
        raise ValueError(
            "The validated formal run context inputs do not match."
        )
    context = ValidatedFormalRunContext(
        run_id=run_id,
        purpose="holdout_formal",
        split="holdout",
        case_set_name=case_set_name,
        case_set_sha256=case_set_sha256,
        planned_case_count=20,
        planned_trials=4,
        source_git_commit=source_git_commit,
        source_tree_sha256=source_tree_sha256,
        harness_sha256=harness_sha256,
        calibration_report_sha256=calibration_attestation.report_sha256,
        calibration_review_sha256=calibration_review.review_sha256,
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
        declaration_manifest_sha256=declaration.manifest_sha256,
        lock_start_path=acquired_lock.path,
        lock_start_receipt_sha256=acquired_lock.receipt_sha256,
        output_root=Path(os.path.abspath(DEFAULT_OUTPUT_ROOT)),
        _sentinel=_FORMAL_CONTEXT_SENTINEL,
    )
    _ISSUED_FORMAL_CONTEXT_IDS.add(id(context))
    return context


def _consume_validated_formal_run_context(
    context: ValidatedFormalRunContext,
    *,
    output_root: Path,
) -> None:
    """Validate and consume one issued capability before any model call."""

    context_id = id(context)
    if context_id not in _ISSUED_FORMAL_CONTEXT_IDS:
        raise ValueError(
            "holdout_formal requires a validated formal run context"
        )
    _ISSUED_FORMAL_CONTEXT_IDS.discard(context_id)
    expected_path = DEFAULT_HOLDOUT_LOCK_ROOT / (
        "readonly-holdout-v2.start.json"
    )
    try:
        expected_output_root = prepare_fixed_private_output_root(
            output_root,
            allowed_root=DEFAULT_OUTPUT_ROOT,
            private_root=PRIVATE_ARTIFACT_ROOT,
        )
    except PrivatePathError as exc:
        raise ValueError(
            "holdout_formal requires a validated formal run context"
        ) from exc
    if Path(os.path.abspath(context.lock_start_path)) != Path(
        os.path.abspath(expected_path)
    ) or context.output_root != expected_output_root or Path(
        os.path.abspath(output_root)
    ) != expected_output_root:
        raise ValueError(
            "holdout_formal requires a validated formal run context"
        )
    try:
        file_mode = context.lock_start_path.lstat().st_mode
        parent_mode = context.lock_start_path.parent.lstat().st_mode
        receipt, receipt_sha256 = read_json_object_snapshot(
            context.lock_start_path,
            label="formal holdout start receipt",
        )
    except (FileSnapshotError, OSError) as exc:
        raise ValueError(
            "holdout_formal requires a validated formal run context"
        ) from exc
    expected_receipt_values: dict[str, object] = {
        "schema_version": "1.0",
        "run_id": context.run_id,
        "status": "started",
        "completed_at": None,
        "case_set_name": context.case_set_name,
        "case_set_sha256": context.case_set_sha256,
        "manifest_sha256": context.declaration_manifest_sha256,
        "source_git_commit": context.source_git_commit,
        "harness_sha256": context.harness_sha256,
        "semantic_calibration_report_sha256": (
            context.calibration_report_sha256
        ),
        "semantic_calibration_review_sha256": (
            context.calibration_review_sha256
        ),
        "public_regression_bundle_integrity_sha256": (
            context.regression_bundle_integrity_sha256
        ),
        "public_regression_gate_sha256": (
            context.regression_gate_sha256
        ),
        "public_regression_run_id": context.regression_run_id,
        "public_regression_source_git_commit": (
            context.regression_source_git_commit
        ),
        "public_regression_case_set_name": (
            context.regression_case_set_name
        ),
        "public_regression_case_set_sha256": (
            context.regression_case_set_sha256
        ),
        "public_regression_harness_sha256": (
            context.regression_harness_sha256
        ),
    }
    if (
        stat.S_ISLNK(file_mode)
        or not stat.S_ISREG(file_mode)
        or stat.S_IMODE(file_mode) != 0o600
        or stat.S_ISLNK(parent_mode)
        or not stat.S_ISDIR(parent_mode)
        or stat.S_IMODE(parent_mode) != 0o700
        or receipt_sha256 != context.lock_start_receipt_sha256
        or any(
            receipt.get(field_name) != expected
            for field_name, expected in expected_receipt_values.items()
        )
    ):
        raise ValueError(
            "holdout_formal requires a validated formal run context"
        )
class ClosableChatModel(ChatModel, Protocol):
    def close(self) -> None: ...


def _stable_failure_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(
        r"[A-Z][A-Z0-9_]{2,95}",
        code,
    ):
        return code
    class_name = type(error).__name__
    normalized = re.sub(
        r"(?<!^)(?=[A-Z])",
        "_",
        class_name,
    ).upper()
    normalized = re.sub(r"[^A-Z0-9_]", "_", normalized)
    return normalized[:96] if len(normalized) >= 3 else "EVAL_ERROR"


def _finalize_terminal_with_retry(
    *,
    lock_path: Path,
    status: Literal["completed", "failed"],
    run_id: str,
    start_receipt_sha256: str,
    bundle_integrity_sha256: str | None = None,
    attempt_bundle_integrity_sha256: str | None = None,
) -> tuple[Path, BaseException | None]:
    """Persist a terminal receipt after one asynchronous interruption."""

    def write_terminal() -> Path:
        return finalize_holdout_run_lock(
            lock_path=lock_path,
            status=status,
            run_id=run_id,
            expected_start_receipt_sha256=start_receipt_sha256,
            bundle_integrity_sha256=bundle_integrity_sha256,
            attempt_bundle_integrity_sha256=(
                attempt_bundle_integrity_sha256
            ),
        )

    try:
        return write_terminal(), None
    except BaseException as exc:
        terminal_path = lock_path.with_name(
            "readonly-holdout-v2.terminal.json"
        )
        if not terminal_path.exists():
            terminal_path = write_terminal()
        return terminal_path, exc


def _recover_owned_start_receipt_sha256(
    *,
    lock_path: Path,
    run_id: str,
) -> str | None:
    """Recover an already-fsynced start after an interrupted return boundary."""

    for _ in range(2):
        try:
            payload, receipt_sha256 = read_json_object_snapshot(
                lock_path,
                label="holdout start receipt",
            )
            if (
                payload.get("run_id") == run_id
                and payload.get("status") == "started"
            ):
                return receipt_sha256
            return None
        except BaseException:
            continue
    return None


def _quarantine_unverified_formal_bundle(
    bundle_target: Path,
) -> None:
    if not bundle_target.exists():
        return
    quarantine_root = bundle_target.parent / "failed-quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_root.chmod(0o700)
    quarantine_target = quarantine_root / (
        f"quarantined-{bundle_target.name}"
    )
    if quarantine_target.exists():
        raise FileExistsError(
            "A quarantine target already exists for this formal run."
        )
    bundle_target.rename(quarantine_target)
    quarantine_target.chmod(0o700)


def validate_paid_eval_settings(settings: Settings) -> None:
    endpoint = urlparse(settings.deepseek_base_url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "api.deepseek.com"
        or endpoint.port not in {None, 443}
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or endpoint.path.rstrip("/") not in {"", "/v1"}
    ):
        raise ValueError(
            "Paid Eval requires the official DeepSeek HTTPS endpoint."
        )
    if settings.deepseek_temperature != 0:
        raise ValueError("Paid Eval requires DEEPSEEK_TEMPERATURE=0.")
    if (
        settings.deepseek_timeout_seconds
        != CANONICAL_DEEPSEEK_TIMEOUT_SECONDS
        or settings.deepseek_max_tokens != CANONICAL_DEEPSEEK_MAX_TOKENS
        or settings.deepseek_max_retries
        != CANONICAL_DEEPSEEK_MAX_RETRIES
        or settings.agent_max_tool_rounds
        != CANONICAL_AGENT_MAX_TOOL_ROUNDS
        or settings.agent_max_tool_calls
        != CANONICAL_AGENT_MAX_TOOL_CALLS
    ):
        raise ValueError(
            "Paid Eval requires the canonical read-only Eval runtime."
        )


def build_deepseek_budget_guard(
    *,
    settings: Settings,
    run_id: str,
    purpose: str,
    frozen_harness: FrozenReadonlyHarness,
) -> DeepSeekBudgetGuard:
    validate_paid_eval_settings(settings)
    if purpose in {"dev_repeat", "holdout_formal"}:
        try:
            require_canonical_calibration_runtime(settings)
        except CalibrationAttestationError as exc:
            raise ValueError(
                "Formal-eligible Eval requires the canonical runtime."
            ) from exc
    price_snapshot = require_frozen_canonical_price(
        frozen_harness.canonical_price,
        expected_file_sha256=frozen_harness.fingerprints[
            "canonical_price_snapshot_sha256"
        ],
    )
    return DeepSeekBudgetGuard(
        ledger=SQLiteBudgetLedger(
            path=DEFAULT_BUDGET_LEDGER,
            hard_limit_cny=FORMAL_HARD_LIMIT_CNY,
            execution_limit_cny=FORMAL_EXECUTION_LIMIT_CNY,
        ),
        run_id=run_id,
        purpose=purpose,
        price_snapshot=price_snapshot,
        model=settings.deepseek_model,
        max_output_tokens=settings.deepseek_max_tokens,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the real read-only Agent Eval and persist an integrity-checked "
            "machine-readable evidence bundle."
        )
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=DEFAULT_CASE_DIR,
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
    )
    parser.add_argument(
        "--holdout-manifest",
        type=Path,
        default=None,
        help=(
            "Required for holdout_formal; binds the sealed cases and frozen "
            "harness to a single process-safe formal run."
        ),
    )
    parser.add_argument(
        "--calibration-report",
        type=Path,
        default=None,
        help=(
            "Required for holdout_formal; the canonical semantic-judge "
            "calibration attestation bound by the holdout manifest."
        ),
    )
    parser.add_argument(
        "--calibration-review",
        type=Path,
        default=None,
        help=(
            "Required for holdout_formal; an independent review receipt "
            "bound to the calibration attestation."
        ),
    )
    parser.add_argument(
        "--regression-bundle",
        type=Path,
        default=None,
        help=(
            "Required for holdout_formal; the private verified 7x4 public "
            "regression evidence bundle bound by the holdout manifest."
        ),
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--purpose",
        choices=("diagnostic", "dev_repeat", "holdout_formal"),
        default="diagnostic",
    )
    parser.add_argument(
        "--split",
        choices=("dev", "holdout"),
        default="dev",
    )
    parser.add_argument(
        "--case-set-name",
        default="readonly-dev-v1",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
    )
    return parser


def _validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.run_id is not None and not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{7,79}",
        args.run_id,
    ):
        parser.error(
            "--run-id must be 8-80 lowercase letters, digits, dots, "
            "underscores, or hyphens"
        )
    if not 1 <= args.trials <= 10:
        parser.error("--trials must be between 1 and 10")
    if args.purpose in {"dev_repeat", "holdout_formal"} and args.trials != 4:
        parser.error(f"{args.purpose} requires exactly 4 trials")
    if args.purpose == "holdout_formal" and args.split != "holdout":
        parser.error("holdout_formal requires --split holdout")
    if args.split == "holdout" and args.purpose != "holdout_formal":
        parser.error("the holdout split requires --purpose holdout_formal")
    if args.purpose == "holdout_formal" and args.holdout_manifest is None:
        parser.error("holdout_formal requires --holdout-manifest")
    if args.purpose == "holdout_formal" and (
        args.calibration_report is None
        or args.calibration_review is None
        or args.regression_bundle is None
    ):
        parser.error(
            "holdout_formal requires --calibration-report and "
            "--calibration-review and --regression-bundle"
        )
    if args.purpose != "holdout_formal" and args.holdout_manifest is not None:
        parser.error("--holdout-manifest is only valid for holdout_formal")
    if args.purpose != "holdout_formal" and (
        args.calibration_report is not None
        or args.calibration_review is not None
        or args.regression_bundle is not None
    ):
        parser.error(
            "formal attestations are only valid for holdout_formal"
        )
    if args.split == "holdout" and args.case_dir.resolve() == DEFAULT_CASE_DIR.resolve():
        parser.error("holdout runs require an explicit non-development --case-dir")
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{2,79}",
        args.case_set_name,
    ):
        parser.error(
            "--case-set-name must be 3-80 lowercase letters, digits, "
            "underscores, or hyphens"
        )


def run_eval_suite(
    *,
    model: ClosableChatModel,
    settings: Settings,
    cases: Sequence[ReadonlyEvalCase],
    run_id: str,
    purpose: str,
    split: str,
    case_set_name: str,
    trials: int,
    output_root: Path,
    budget_report_provider: Callable[[], dict] | None = None,
    semantic_judge_model: SemanticJsonModel | None = None,
    calibration_attestation: (
        ValidatedCalibrationAttestation | None
    ) = None,
    calibration_review: ValidatedCalibrationReview | None = None,
    formal_holdout_evidence: FormalHoldoutEvidence | None = None,
    frozen_harness: FrozenReadonlyHarness | None = None,
    source_git_commit: str | None = None,
    source_tree_sha256: str | None = None,
    formal_run_context: ValidatedFormalRunContext | object | None = None,
    partial_results: list[ReadonlyEvalResult] | None = None,
    pre_write_check: Callable[[], None] | None = None,
) -> tuple[list[ReadonlyEvalResult], dict, Path]:
    if purpose not in {
        "diagnostic",
        "dev_repeat",
        "holdout_formal",
    }:
        raise ValueError("Unsupported Eval purpose")
    if purpose == "holdout_formal":
        if (
            not isinstance(
                formal_run_context,
                ValidatedFormalRunContext,
            )
            or formal_run_context._sentinel
            is not _FORMAL_CONTEXT_SENTINEL
        ):
            raise ValueError(
                "holdout_formal requires a validated formal run context"
            )
        _consume_validated_formal_run_context(
            formal_run_context,
            output_root=output_root,
        )
        case_set_sha256 = _formal_case_set_sha256(cases)
        if (
            formal_run_context.purpose != purpose
            or formal_run_context.split != split
            or formal_run_context.run_id != run_id
            or formal_run_context.case_set_name != case_set_name
            or formal_run_context.case_set_sha256 != case_set_sha256
            or formal_run_context.planned_case_count != len(cases)
            or formal_run_context.planned_trials != trials
            or frozen_harness is None
            or formal_run_context.harness_sha256
            != stable_sha256(dict(frozen_harness.fingerprints))
            or source_git_commit
            != formal_run_context.source_git_commit
            or source_tree_sha256
            != formal_run_context.source_tree_sha256
            or calibration_attestation is None
            or calibration_attestation.report_sha256
            != formal_run_context.calibration_report_sha256
            or calibration_review is None
            or calibration_review.review_sha256
            != formal_run_context.calibration_review_sha256
            or formal_holdout_evidence is None
            or formal_holdout_evidence.declaration_manifest_sha256
            != formal_run_context.declaration_manifest_sha256
            or formal_holdout_evidence.lock_start_receipt_sha256
            != formal_run_context.lock_start_receipt_sha256
            or formal_holdout_evidence.declared_harness_sha256
            != formal_run_context.harness_sha256
            or formal_holdout_evidence
            .regression_bundle_integrity_sha256
            != formal_run_context.regression_bundle_integrity_sha256
            or formal_holdout_evidence.regression_gate_sha256
            != formal_run_context.regression_gate_sha256
            or formal_holdout_evidence.regression_run_id
            != formal_run_context.regression_run_id
            or formal_holdout_evidence.regression_source_git_commit
            != formal_run_context.regression_source_git_commit
            or formal_holdout_evidence.regression_case_set_name
            != formal_run_context.regression_case_set_name
            or formal_holdout_evidence.regression_case_set_sha256
            != formal_run_context.regression_case_set_sha256
            or formal_holdout_evidence.regression_harness_sha256
            != formal_run_context.regression_harness_sha256
        ):
            raise ValueError(
                "holdout_formal requires a validated formal run context"
            )
    elif formal_run_context is not None:
        raise ValueError(
            "validated formal run context is only valid for holdout_formal"
        )
    if purpose in {"diagnostic", "dev_repeat"}:
        require_nonformal_paid_case_payload(
            purpose=purpose,
            case_set_name=case_set_name,
            cases=cases,
            planned_trials=trials,
        )
    started_at = datetime.now(UTC)
    runtime_harness = frozen_harness or freeze_readonly_harness(
        settings
    )
    results = partial_results if partial_results is not None else []
    if results:
        raise ValueError("partial_results must be empty before a new Eval")
    for trial in range(1, trials + 1):
        for case in cases:
            results.append(
                run_case(
                    case,
                    model=model,
                    server_run_id=run_id,
                    trial=trial,
                    semantic_judge_model=semantic_judge_model,
                    settings=settings,
                    agent_system_prompt=runtime_harness.agent_system_prompt,
                    semantic_judge_system_prompt=(
                        runtime_harness.semantic_judge_system_prompt
                    ),
                    policy_documents=runtime_harness.policy_documents,
                    tool_contracts=runtime_harness.tool_contracts,
                )
            )
    model.close()
    completed_at = datetime.now(UTC)
    budget_report = (
        budget_report_provider()
        if budget_report_provider is not None
        else None
    )
    summary = summarize_results(
        run_id=run_id,
        results=results,
        planned_trials=trials,
        budget_report=budget_report,
    )
    manifest = build_readonly_manifest(
        run_id=run_id,
        purpose=purpose,
        split=split,
        case_set_name=case_set_name,
        cases=cases,
        results=results,
        settings=settings,
        planned_trials=trials,
        started_at=started_at,
        completed_at=completed_at,
        budget_report=budget_report,
        calibration_attestation=calibration_attestation,
        calibration_review=calibration_review,
        formal_holdout_evidence=formal_holdout_evidence,
        harness_fingerprints=dict(runtime_harness.fingerprints),
        source_git_commit=source_git_commit,
    )
    records = [
        result_to_record(result, split=split)
        for result in results
    ]
    if pre_write_check is not None:
        pre_write_check()
    bundle_path = write_eval_bundle(
        output_root=output_root,
        run_id=run_id,
        manifest=manifest,
        case_records=records,
        summary=summary,
        secret_values=tuple(
            value
            for value in (
                settings.deepseek_api_key,
                settings.host_confirmation_token,
                settings.debug_admin_token,
                settings.demo_verification_code,
            )
            if value
        ),
    )
    validate_readonly_bundle(bundle_path)
    return results, summary, bundle_path


def _print_results(
    results: Sequence[ReadonlyEvalResult],
    summary: dict,
    bundle_path: Path,
    *,
    disclose_case_details: bool = True,
    disclose_bundle_path: bool | None = None,
) -> None:
    if disclose_bundle_path is None:
        disclose_bundle_path = disclose_case_details
    if disclose_case_details:
        print("| case | trial | result | tools | business state changed |")
        print("|---|---:|---:|---|---:|")
        for item in results:
            status = "PASS" if item.passed else "FAIL"
            tools = ", ".join(item.tool_names) or "-"
            state_changed = bool(
                item.business_state_delta
                and item.business_state_delta.changed
            )
            print(
                f"| {item.case_id} | {item.trial} | {status} | "
                f"{tools} | {state_changed} |"
            )
            for failure in item.failures:
                print(
                    f"  - {item.case_id} trial {item.trial}: {failure}"
                )
    else:
        print("Formal holdout completed; private case details withheld.")

    passed = summary["strict"]["passed"]
    total = summary["total_trials"]
    print(f"\n{passed}/{total} read-only Agent trials passed.")
    print(
        "Safety assertions passed "
        f"{summary['security']['passed']}/{total}; "
        "business-state changes: "
        f"{summary['business_state']['changed_trials']}."
    )
    if disclose_bundle_path:
        print(f"Verified evidence bundle: {bundle_path}")
    else:
        print("Verified private evidence bundle.")
    print(
        "This is a versioned harness result, not a production safety "
        "certification."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    run_id = args.run_id or create_server_run_id()
    if (args.output_root / run_id).exists():
        print(
            "EVIDENCE ERROR: output bundle already exists; "
            "use a fresh server run ID."
        )
        return 3

    if args.purpose == "holdout_formal":
        try:
            args.output_root = prepare_fixed_private_output_root(
                args.output_root,
                allowed_root=DEFAULT_OUTPUT_ROOT,
                private_root=PRIVATE_ARTIFACT_ROOT,
            )
            args.case_dir = require_private_case_directory(
                args.case_dir,
                private_root=PRIVATE_ARTIFACT_ROOT,
            )
            assert args.holdout_manifest is not None
            assert args.calibration_report is not None
            assert args.calibration_review is not None
            assert args.regression_bundle is not None
            args.holdout_manifest = require_private_input_file(
                args.holdout_manifest,
                private_root=PRIVATE_ARTIFACT_ROOT,
                label="holdout declaration",
            )
            args.calibration_report = require_private_input_file(
                args.calibration_report,
                private_root=PRIVATE_ARTIFACT_ROOT,
                label="calibration report",
            )
            args.calibration_review = require_private_input_file(
                args.calibration_review,
                private_root=PRIVATE_ARTIFACT_ROOT,
                label="calibration review",
            )
        except PrivatePathError:
            print(
                "FORMAL PRECHECK ERROR: private artifact paths or "
                "permissions are invalid."
            )
            return 2

    try:
        cases = load_cases(args.case_dir)
    except (OSError, ValueError) as exc:
        if args.purpose == "holdout_formal":
            print("FORMAL PRECHECK ERROR: private holdout cases are invalid.")
        else:
            print(f"CASE ERROR: {exc}")
        return 2
    if not cases:
        if args.purpose == "holdout_formal":
            print("FORMAL PRECHECK ERROR: private holdout cases are invalid.")
        else:
            print(f"CASE ERROR: no Eval cases found in {args.case_dir}")
        return 2
    if args.purpose != "holdout_formal":
        try:
            require_nonformal_paid_case_set(
                purpose=args.purpose,
                case_dir=args.case_dir,
                case_set_name=args.case_set_name,
                cases=cases,
                planned_trials=args.trials,
            )
        except ValueError:
            print(
                "CASE ERROR: non-formal paid case identity is invalid."
            )
            return 2
    settings = Settings()
    bundle_target = args.output_root / run_id
    declaration: HoldoutDeclaration | None = None
    calibration_attestation: ValidatedCalibrationAttestation | None = None
    calibration_review: ValidatedCalibrationReview | None = None
    frozen_harness: FrozenReadonlyHarness | None = None
    source_git_commit: str | None = None
    formal_source_tree_sha256: str | None = None
    regression_gate: ValidatedRegressionGate | None = None
    if args.purpose == "holdout_formal":
        try:
            source_git_commit = require_clean_git_worktree()
            frozen_harness = freeze_readonly_harness(settings)
            formal_source_tree_sha256 = current_source_tree_sha256()
            require_clean_git_worktree(
                expected_commit=source_git_commit
            )
            calibration_attestation = validate_calibration_attestation(
                report_path=args.calibration_report,
                settings=settings,
                fixture_snapshot=(
                    frozen_harness.calibration_fixture_snapshot
                ),
                harness_fingerprints=dict(
                    frozen_harness.fingerprints
                ),
            )
            calibration_review = validate_calibration_review(
                review_path=args.calibration_review,
                attestation=calibration_attestation,
            )
            regression_gate = validate_regression_gate(
                bundle_path=args.regression_bundle,
                private_root=PRIVATE_ARTIFACT_ROOT,
                source_git_commit=source_git_commit,
                harness_sha256=stable_sha256(
                    dict(frozen_harness.fingerprints)
                ),
                expected_source_tree_sha256=(
                    formal_source_tree_sha256
                ),
                expected_harness_fingerprints=dict(
                    frozen_harness.fingerprints
                ),
                settings=settings,
            )
            declaration = validate_holdout_declaration(
                manifest_path=args.holdout_manifest,
                case_set_name=args.case_set_name,
                cases=cases,
                settings=settings,
                calibration_attestation=calibration_attestation,
                calibration_review=calibration_review,
                regression_gate=regression_gate,
                harness_fingerprints=dict(
                    frozen_harness.fingerprints
                ),
                source_git_commit=source_git_commit,
            )
        except (
            CalibrationAttestationError,
            FileSnapshotError,
            HoldoutLockError,
            ValueError,
        ):
            print(
                "FORMAL PRECHECK ERROR: calibration or holdout "
                "declaration is invalid."
            )
            return 2
    if frozen_harness is None:
        try:
            frozen_harness = freeze_readonly_harness(settings)
        except (FileSnapshotError, CanonicalPricingError, ValueError):
            print(
                "CONFIGURATION ERROR: runtime inputs are invalid."
            )
            return 2
    budget_guard = None
    assert frozen_harness is not None
    try:
        budget_guard = build_deepseek_budget_guard(
            settings=settings,
            run_id=run_id,
            purpose=args.purpose,
            frozen_harness=frozen_harness,
        )
    except (BudgetError, CanonicalPricingError, ValueError) as exc:
        if args.purpose == "holdout_formal":
            print(
                "FORMAL PRECHECK ERROR: budget or model configuration "
                "is invalid."
            )
        else:
            print(f"BUDGET OR CONFIGURATION ERROR: {exc}")
        return 2

    try:
        model = build_deepseek_client(
            settings,
            budget_guard=budget_guard,
        )
    except (BudgetError, ValueError) as exc:
        budget_guard.close()
        if args.purpose == "holdout_formal":
            print(
                "FORMAL PRECHECK ERROR: budget or model configuration "
                "is invalid."
            )
        else:
            print(f"CONFIGURATION ERROR: {exc}")
            print(
                "Load a private DEEPSEEK_API_KEY into the process "
                "environment, then rerun this command."
            )
        return 2

    holdout_lock_path: Path | None = None
    holdout_start_receipt_sha256: str | None = None
    formal_holdout_evidence: FormalHoldoutEvidence | None = None
    formal_attempt_started_at: datetime | None = None
    acquired_lock: AcquiredHoldoutRunLock | None = None
    formal_run_context: ValidatedFormalRunContext | None = None
    if declaration is not None:
        holdout_lock_path = DEFAULT_HOLDOUT_LOCK_ROOT / (
            "readonly-holdout-v2.start.json"
        )
        try:
            assert source_git_commit is not None
            assert frozen_harness is not None
            assert formal_source_tree_sha256 is not None
            assert calibration_attestation is not None
            assert calibration_review is not None
            assert regression_gate is not None
            require_clean_git_worktree(
                expected_commit=source_git_commit
            )
            formal_attempt_started_at = datetime.now(UTC)
            acquired_lock = acquire_holdout_run_lock_with_hash(
                lock_root=DEFAULT_HOLDOUT_LOCK_ROOT,
                declaration=declaration,
                run_id=run_id,
            )
            holdout_lock_path = acquired_lock.path
            holdout_start_receipt_sha256 = acquired_lock.receipt_sha256
            formal_holdout_evidence = FormalHoldoutEvidence(
                declaration_manifest_sha256=(
                    declaration.manifest_sha256
                ),
                lock_start_receipt_sha256=holdout_start_receipt_sha256,
                declared_harness_sha256=(
                    declaration.harness_sha256
                ),
                regression_bundle_integrity_sha256=(
                    declaration.regression_bundle_integrity_sha256
                ),
                regression_gate_sha256=(
                    declaration.regression_gate_sha256
                ),
                regression_run_id=declaration.regression_run_id,
                regression_source_git_commit=(
                    declaration.regression_source_git_commit
                ),
                regression_case_set_name=(
                    declaration.regression_case_set_name
                ),
                regression_case_set_sha256=(
                    declaration.regression_case_set_sha256
                ),
                regression_harness_sha256=(
                    declaration.regression_harness_sha256
                ),
            )
            formal_run_context = _create_validated_formal_run_context(
                run_id=run_id,
                purpose=args.purpose,
                split=args.split,
                cases=cases,
                case_set_name=args.case_set_name,
                trials=args.trials,
                source_git_commit=source_git_commit,
                source_tree_sha256=formal_source_tree_sha256,
                frozen_harness=frozen_harness,
                calibration_attestation=calibration_attestation,
                calibration_review=calibration_review,
                declaration=declaration,
                regression_gate=regression_gate,
                acquired_lock=acquired_lock,
            )
        except BaseException as exc:
            for close_resource in (model.close, budget_guard.close):
                try:
                    close_resource()
                except BaseException:
                    pass
            if acquired_lock is not None:
                holdout_lock_path = acquired_lock.path
                holdout_start_receipt_sha256 = (
                    acquired_lock.receipt_sha256
                )
            elif (
                holdout_start_receipt_sha256 is None
                and holdout_lock_path.exists()
            ):
                holdout_start_receipt_sha256 = (
                    _recover_owned_start_receipt_sha256(
                        lock_path=holdout_lock_path,
                        run_id=run_id,
                    )
                )
            if holdout_start_receipt_sha256 is not None:
                try:
                    _, interrupted_terminal = (
                        _finalize_terminal_with_retry(
                            lock_path=holdout_lock_path,
                            status="failed",
                            run_id=run_id,
                            start_receipt_sha256=(
                                holdout_start_receipt_sha256
                            ),
                        )
                    )
                    if isinstance(
                        interrupted_terminal,
                        (KeyboardInterrupt, SystemExit),
                    ):
                        raise interrupted_terminal
                except BaseException as terminal_exc:
                    print(
                        "HOLDOUT LOCK ERROR: terminal evidence is invalid."
                    )
                    if isinstance(
                        terminal_exc,
                        (KeyboardInterrupt, SystemExit),
                    ):
                        raise terminal_exc from None
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise exc from None
                    return 3
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise exc
            if args.purpose == "holdout_formal":
                print("HOLDOUT LOCK ERROR: formal start receipt is invalid.")
            else:
                print(f"HOLDOUT LOCK ERROR: {exc}")
            return 2

    run_error: BaseException | None = None
    failure_stage = "suite_execution"
    lock_status: Literal["completed", "failed"] = "failed"
    bundle_path: Path | None = None
    partial_results: list[ReadonlyEvalResult] = []
    pre_write_check: Callable[[], None] | None = None
    if source_git_commit is not None:
        def check_source_before_write() -> None:
            require_clean_git_worktree(
                expected_commit=source_git_commit
            )

        pre_write_check = check_source_before_write
    try:
        results, summary, bundle_path = run_eval_suite(
            model=model,
            settings=settings,
            cases=cases,
            run_id=run_id,
            purpose=args.purpose,
            split=args.split,
            case_set_name=args.case_set_name,
            trials=args.trials,
            output_root=args.output_root,
            budget_report_provider=budget_guard.snapshot,
            semantic_judge_model=model,
            calibration_attestation=calibration_attestation,
            calibration_review=calibration_review,
            formal_holdout_evidence=formal_holdout_evidence,
            frozen_harness=frozen_harness,
            source_git_commit=source_git_commit,
            source_tree_sha256=formal_source_tree_sha256,
            formal_run_context=formal_run_context,
            partial_results=partial_results,
            pre_write_check=pre_write_check,
        )
        lock_status = "completed"
    except BaseException as exc:
        run_error = exc
    finally:
        for close_resource in (model.close, budget_guard.close):
            try:
                close_resource()
            except BaseException as exc:
                if run_error is None:
                    run_error = exc
                    lock_status = "failed"
                    failure_stage = "cleanup"

    failed_attempt_bundle: Path | None = None
    if holdout_lock_path is not None and lock_status == "failed":
        assert declaration is not None
        assert formal_holdout_evidence is not None
        assert frozen_harness is not None
        assert source_git_commit is not None
        assert formal_source_tree_sha256 is not None
        assert formal_attempt_started_at is not None
        assert holdout_start_receipt_sha256 is not None
        failure_root = args.output_root / "failed-attempts"
        try:
            _quarantine_unverified_formal_bundle(bundle_target)
            failure_root.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
            failure_root.chmod(0o700)
            try:
                failed_budget_report = budget_guard.snapshot()
            except BaseException:
                failed_budget_report = None
            failure_records = [
                result_to_record(result, split="holdout")
                for result in partial_results
            ]
            failed_attempt_bundle = write_formal_failure_bundle(
                output_root=failure_root,
                context=FormalFailureContext.model_validate(
                    {
                        "run_id": run_id,
                        "created_at": formal_attempt_started_at,
                        "failed_at": datetime.now(UTC),
                        "failure_stage": failure_stage,
                        "failure_code": _stable_failure_code(
                            run_error
                            or RuntimeError("formal attempt failed")
                        ),
                        "max_output_tokens": (
                            settings.deepseek_max_tokens
                        ),
                        "source": {
                            "git_commit": source_git_commit,
                            "git_dirty": False,
                            "source_tree_sha256": (
                                formal_source_tree_sha256
                            ),
                        },
                        "case_set": {
                            "name": declaration.case_set_name,
                            "sha256": declaration.case_set_sha256,
                            "planned_case_count": 20,
                            "planned_trials": 4,
                        },
                        "formal_holdout": {
                            "declaration_manifest_sha256": (
                                formal_holdout_evidence
                                .declaration_manifest_sha256
                            ),
                            "lock_start_receipt_sha256": (
                                holdout_start_receipt_sha256
                            ),
                            "declared_harness_sha256": (
                                formal_holdout_evidence
                                .declared_harness_sha256
                            ),
                            "runtime_harness_sha256": (
                                formal_holdout_evidence
                                .declared_harness_sha256
                            ),
                            "regression_bundle_integrity_sha256": (
                                formal_holdout_evidence
                                .regression_bundle_integrity_sha256
                            ),
                            "regression_gate_sha256": (
                                formal_holdout_evidence
                                .regression_gate_sha256
                            ),
                            "regression_run_id": (
                                formal_holdout_evidence.regression_run_id
                            ),
                            "regression_source_git_commit": (
                                formal_holdout_evidence
                                .regression_source_git_commit
                            ),
                            "regression_case_set_name": (
                                formal_holdout_evidence
                                .regression_case_set_name
                            ),
                            "regression_case_set_sha256": (
                                formal_holdout_evidence
                                .regression_case_set_sha256
                            ),
                            "regression_harness_sha256": (
                                formal_holdout_evidence
                                .regression_harness_sha256
                            ),
                        },
                    }
                ),
                case_records=failure_records,
                records_captured=True,
                budget_summary=failed_budget_report,
                secret_values=tuple(
                    value
                    for value in (
                        settings.deepseek_api_key,
                        settings.host_confirmation_token,
                        settings.debug_admin_token,
                        settings.demo_verification_code,
                    )
                    if value
                ),
            )
        except BaseException:
            failed_attempt_bundle = None

    if holdout_lock_path is not None:
        assert holdout_start_receipt_sha256 is not None
        terminal_write_error: BaseException | None = None
        try:
            completed_integrity_sha256 = (
                read_file_snapshot(
                    bundle_path / "integrity.json"
                ).sha256
                if lock_status == "completed"
                and bundle_path is not None
                else None
            )
            failed_integrity_sha256 = (
                read_file_snapshot(
                    failed_attempt_bundle / "integrity.json"
                ).sha256
                if lock_status == "failed"
                and failed_attempt_bundle is not None
                else None
            )
            terminal_path, terminal_write_error = (
                _finalize_terminal_with_retry(
                    lock_path=holdout_lock_path,
                    status=lock_status,
                    run_id=run_id,
                    start_receipt_sha256=(
                        holdout_start_receipt_sha256
                    ),
                    bundle_integrity_sha256=(
                        completed_integrity_sha256
                    ),
                    attempt_bundle_integrity_sha256=(
                        failed_integrity_sha256
                    ),
                )
            )
            if lock_status == "completed" and bundle_path is not None:
                assert args.holdout_manifest is not None
                assert args.regression_bundle is not None
                verify_holdout_receipt_chain(
                    manifest_path=args.holdout_manifest,
                    start_path=holdout_lock_path,
                    terminal_path=terminal_path,
                    bundle_path=bundle_path,
                    regression_bundle_path=args.regression_bundle,
                    private_root=PRIVATE_ARTIFACT_ROOT,
                )
            elif (
                lock_status == "failed"
                and failed_attempt_bundle is not None
            ):
                assert args.holdout_manifest is not None
                assert args.regression_bundle is not None
                verify_failed_holdout_receipt_chain(
                    manifest_path=args.holdout_manifest,
                    start_path=holdout_lock_path,
                    terminal_path=terminal_path,
                    bundle_path=failed_attempt_bundle,
                    regression_bundle_path=args.regression_bundle,
                    private_root=PRIVATE_ARTIFACT_ROOT,
                )
            if isinstance(
                terminal_write_error,
                (KeyboardInterrupt, SystemExit),
            ):
                raise terminal_write_error
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if args.purpose == "holdout_formal":
                print("HOLDOUT LOCK ERROR: terminal evidence is invalid.")
            else:
                print(f"HOLDOUT LOCK ERROR: {exc}")
            return 3
    if run_error is not None:
        if isinstance(run_error, (KeyboardInterrupt, SystemExit)):
            raise run_error
        if args.purpose == "holdout_formal":
            print(
                "EVIDENCE ERROR: formal attempt failed; private failure "
                "evidence and terminal status were recorded when possible."
            )
        else:
            print(
                f"EVIDENCE ERROR: {type(run_error).__name__}: "
                f"{run_error}"
            )
        return 3
    if bundle_path is None:
        print("EVIDENCE ERROR: completed run has no verified bundle.")
        return 3

    _print_results(
        results,
        summary,
        bundle_path,
        disclose_case_details=args.purpose != "holdout_formal",
        disclose_bundle_path=args.purpose != "holdout_formal",
    )
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
