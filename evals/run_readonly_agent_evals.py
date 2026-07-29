from __future__ import annotations

import argparse
import re
import sys
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
    CalibrationAttestationError,
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
    validate_calibration_attestation,
    validate_calibration_review,
)
from evals.canonical_pricing import (
    FORMAL_EXECUTION_LIMIT_CNY,
    FORMAL_HARD_LIMIT_CNY,
    CanonicalPricingError,
    require_frozen_canonical_price,
)
from evals.evidence import write_eval_bundle
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
    acquire_holdout_run_lock_with_hash,
    finalize_holdout_run_lock,
    validate_holdout_declaration,
    verify_failed_holdout_receipt_chain,
    verify_holdout_receipt_chain,
)
from evals.nonformal_paid_contract import (
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


def build_deepseek_budget_guard(
    *,
    settings: Settings,
    run_id: str,
    purpose: str,
    frozen_harness: FrozenReadonlyHarness,
) -> DeepSeekBudgetGuard:
    validate_paid_eval_settings(settings)
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
    ):
        parser.error(
            "holdout_formal requires --calibration-report and "
            "--calibration-review"
        )
    if args.purpose != "holdout_formal" and args.holdout_manifest is not None:
        parser.error("--holdout-manifest is only valid for holdout_formal")
    if args.purpose != "holdout_formal" and (
        args.calibration_report is not None
        or args.calibration_review is not None
    ):
        parser.error(
            "calibration attestations are only valid for holdout_formal"
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
    partial_results: list[ReadonlyEvalResult] | None = None,
    pre_write_check: Callable[[], None] | None = None,
) -> tuple[list[ReadonlyEvalResult], dict, Path]:
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
            declaration = validate_holdout_declaration(
                manifest_path=args.holdout_manifest,
                case_set_name=args.case_set_name,
                cases=cases,
                settings=settings,
                calibration_attestation=calibration_attestation,
                calibration_review=calibration_review,
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
    except ValueError as exc:
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
    if declaration is not None:
        holdout_lock_path = DEFAULT_HOLDOUT_LOCK_ROOT / (
            "readonly-holdout-v2.start.json"
        )
        try:
            assert source_git_commit is not None
            assert frozen_harness is not None
            assert formal_source_tree_sha256 is not None
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
                verify_holdout_receipt_chain(
                    manifest_path=args.holdout_manifest,
                    start_path=holdout_lock_path,
                    terminal_path=terminal_path,
                    bundle_path=bundle_path,
                )
            elif (
                lock_status == "failed"
                and failed_attempt_bundle is not None
            ):
                assert args.holdout_manifest is not None
                verify_failed_holdout_receipt_chain(
                    manifest_path=args.holdout_manifest,
                    start_path=holdout_lock_path,
                    terminal_path=terminal_path,
                    bundle_path=failed_attempt_bundle,
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
