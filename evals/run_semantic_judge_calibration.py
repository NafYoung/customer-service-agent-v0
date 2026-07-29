from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.deepseek_budget import BudgetError
from app.agent.factory import build_deepseek_client
from app.config import Settings
from evals.calibration_attestation import (
    CalibrationAttestationError,
    canonical_contract_set_sha256,
    validate_calibration_attestation,
)
from evals.private_paths import (
    PrivatePathError,
    prepare_fixed_private_output_root,
)
from evals.readonly_eval import load_cases
from evals.readonly_reporting import (
    create_server_run_id,
    freeze_readonly_harness,
    require_clean_git_worktree,
)
from evals.run_readonly_agent_evals import build_deepseek_budget_guard
from evals.semantic_calibration import (
    load_calibration_fixtures_snapshot,
    parse_calibration_fixtures_snapshot,
    run_calibration_fixture,
    summarize_calibration,
    validate_calibration_coverage,
)

DEFAULT_FIXTURE_PATH = (
    ROOT / "evals" / "semantic_judge_calibration_cases.jsonl"
)
DEFAULT_CASE_DIR = ROOT / "evals" / "readonly_regression_cases"
PRIVATE_ARTIFACT_ROOT = ROOT / "artifacts" / "private"
DEFAULT_OUTPUT_ROOT = (
    PRIVATE_ARTIFACT_ROOT / "semantic-judge-calibration"
)
_RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{7,79}$")


def _write_private_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Calibrate the isolated atomic-claim semantic judge against "
            "public, human-labelled fixtures."
        )
    )
    parser.add_argument("--fixture-path", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--case-dir", type=Path, default=DEFAULT_CASE_DIR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--mode",
        choices=("holdout_eligible", "diagnostic"),
        default="holdout_eligible",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_id = args.run_id or create_server_run_id()
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        print("CALIBRATION ERROR: invalid run id.")
        return 2
    if args.mode == "holdout_eligible":
        try:
            args.output_root = prepare_fixed_private_output_root(
                args.output_root,
                allowed_root=DEFAULT_OUTPUT_ROOT,
                private_root=PRIVATE_ARTIFACT_ROOT,
            )
        except PrivatePathError:
            print(
                "CALIBRATION INPUT ERROR: holdout-eligible reports "
                "must use the fixed private output root."
            )
            return 2
    report_path = args.output_root / f"{run_id}.json"
    if report_path.exists():
        print("CALIBRATION ERROR: report already exists.")
        return 3
    if args.mode == "holdout_eligible" and (
        args.fixture_path.resolve() != DEFAULT_FIXTURE_PATH.resolve()
        or args.case_dir.resolve() != DEFAULT_CASE_DIR.resolve()
    ):
        print(
            "CALIBRATION INPUT ERROR: holdout eligibility requires the "
            "canonical fixture and case paths."
        )
        return 2

    settings = Settings()
    if settings.deepseek_temperature != 0:
        print("CONFIGURATION ERROR: semantic judge requires temperature 0.")
        return 2
    source_git_commit: str | None = None
    if args.mode == "holdout_eligible":
        try:
            source_git_commit = require_clean_git_worktree()
        except ValueError as exc:
            print(f"CALIBRATION PRECHECK ERROR: {exc}")
            return 2
    try:
        frozen_harness = freeze_readonly_harness(settings)
        if source_git_commit is not None:
            require_clean_git_worktree(
                expected_commit=source_git_commit
            )
        if args.mode == "holdout_eligible":
            fixture_snapshot = (
                frozen_harness.calibration_fixture_snapshot
            )
            fixtures = parse_calibration_fixtures_snapshot(
                fixture_snapshot
            )
        else:
            fixtures, fixture_snapshot = (
                load_calibration_fixtures_snapshot(args.fixture_path)
            )
        cases = load_cases(args.case_dir)
        validate_calibration_coverage(
            fixtures=fixtures,
            cases=cases,
            require_canonical=args.mode == "holdout_eligible",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"CALIBRATION INPUT ERROR: {exc}")
        return 2
    case_by_id = {case.case_id: case for case in cases}
    fixture_sha256 = fixture_snapshot.sha256
    contract_set_sha256 = canonical_contract_set_sha256(cases)
    harness = dict(frozen_harness.fingerprints)

    try:
        budget_guard = build_deepseek_budget_guard(
            settings=settings,
            run_id=run_id,
            purpose="semantic_judge_calibration",
        )
    except (BudgetError, ValueError) as exc:
        print(f"CONFIGURATION ERROR: {exc}")
        return 2
    try:
        model = build_deepseek_client(
            settings,
            budget_guard=budget_guard,
        )
    except (BudgetError, ValueError) as exc:
        budget_guard.close()
        print(f"CONFIGURATION ERROR: {exc}")
        return 2

    started_at = datetime.now(UTC)
    try:
        results = [
            run_calibration_fixture(
                fixture=fixture,
                case=case_by_id[fixture.case_id],
                model=model,
                system_prompt=(
                    frozen_harness.semantic_judge_system_prompt
                ),
            )
            for fixture in fixtures
        ]
        summary = summarize_calibration(results)
    finally:
        model.close()
    budget_report = budget_guard.snapshot()
    completed_at = datetime.now(UTC)
    if source_git_commit is not None:
        try:
            require_clean_git_worktree(
                expected_commit=source_git_commit
            )
        except ValueError as exc:
            print(f"CALIBRATION SOURCE ERROR: {exc}")
            return 3
    report: dict[str, object] = {
        "schema_version": "2.0",
        "attestation_kind": (
            "semantic_judge_holdout_eligibility"
            if args.mode == "holdout_eligible"
            else "semantic_judge_diagnostic"
        ),
        "run_id": run_id,
        **(
            {"source_git_commit": source_git_commit}
            if source_git_commit is not None
            else {}
        ),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "fixture_sha256": fixture_sha256,
        "contract_set_sha256": contract_set_sha256,
        "harness": harness,
        "summary": asdict(summary),
        "budget": budget_report,
        "results": [asdict(result) for result in results],
    }
    _write_private_report(report_path, report)
    if args.mode == "holdout_eligible":
        try:
            validate_calibration_attestation(
                report_path=report_path,
                settings=settings,
                fixture_snapshot=fixture_snapshot,
                harness_fingerprints=harness,
            )
        except CalibrationAttestationError as exc:
            print(f"CALIBRATION ATTESTATION ERROR: {exc}")
            return 3

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{result.fixture_id}: {status}")
    print(
        f"{summary.passed}/{summary.total} calibration fixtures matched; "
        f"adversarial={summary.adversarial_rate:.3f}, "
        f"positive={summary.positive_rate:.3f}."
    )
    print(f"Private calibration report: {report_path}")
    if args.mode == "holdout_eligible":
        return 0 if summary.gate_passed else 1
    return 0 if summary.passed == summary.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
