from __future__ import annotations

import argparse
import hashlib
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
from evals.readonly_eval import load_cases
from evals.readonly_reporting import (
    create_server_run_id,
    current_readonly_harness_fingerprints,
)
from evals.run_readonly_agent_evals import build_deepseek_budget_guard
from evals.semantic_calibration import (
    load_calibration_fixtures,
    run_calibration_fixture,
    summarize_calibration,
    validate_calibration_coverage,
)

DEFAULT_FIXTURE_PATH = (
    ROOT / "evals" / "semantic_judge_calibration_cases.jsonl"
)
DEFAULT_CASE_DIR = ROOT / "evals" / "readonly_regression_cases"
DEFAULT_OUTPUT_ROOT = (
    ROOT / "artifacts" / "private" / "semantic-judge-calibration"
)
_RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{7,79}$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_id = args.run_id or create_server_run_id()
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        print("CALIBRATION ERROR: invalid run id.")
        return 2
    report_path = args.output_root / f"{run_id}.json"
    if report_path.exists():
        print("CALIBRATION ERROR: report already exists.")
        return 3

    try:
        fixtures = load_calibration_fixtures(args.fixture_path)
        cases = load_cases(args.case_dir)
        validate_calibration_coverage(fixtures=fixtures, cases=cases)
    except (OSError, ValueError) as exc:
        print(f"CALIBRATION INPUT ERROR: {exc}")
        return 2
    case_by_id = {case.case_id: case for case in cases}
    settings = Settings()
    if settings.deepseek_temperature != 0:
        print("CONFIGURATION ERROR: semantic judge requires temperature 0.")
        return 2

    try:
        budget_guard = build_deepseek_budget_guard(
            settings=settings,
            run_id=run_id,
            purpose="semantic_judge_calibration",
        )
        model = build_deepseek_client(
            settings,
            budget_guard=budget_guard,
        )
    except (BudgetError, ValueError) as exc:
        print(f"CONFIGURATION ERROR: {exc}")
        return 2

    started_at = datetime.now(UTC)
    try:
        results = [
            run_calibration_fixture(
                fixture=fixture,
                case=case_by_id[fixture.case_id],
                model=model,
            )
            for fixture in fixtures
        ]
        summary = summarize_calibration(results)
        budget_report = budget_guard.snapshot()
        completed_at = datetime.now(UTC)
        report: dict[str, object] = {
            "schema_version": "1.0",
            "run_id": run_id,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "fixture_sha256": _sha256(args.fixture_path),
            "harness": current_readonly_harness_fingerprints(settings),
            "summary": asdict(summary),
            "budget": budget_report,
            "results": [asdict(result) for result in results],
        }
        _write_private_report(report_path, report)
    finally:
        model.close()

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{result.fixture_id}: {status}")
    print(
        f"{summary.passed}/{summary.total} calibration fixtures matched; "
        f"adversarial={summary.adversarial_rate:.3f}, "
        f"positive={summary.positive_rate:.3f}."
    )
    print(f"Private calibration report: {report_path}")
    return 0 if summary.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
