from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.deepseek_budget import (
    BudgetError,
    DeepSeekBudgetGuard,
    SQLiteBudgetLedger,
    load_price_snapshot,
)
from app.agent.factory import build_deepseek_client
from app.agent.openai_compatible import ChatModel
from app.config import Settings
from evals.evidence import write_eval_bundle
from evals.evidence_schema import validate_readonly_bundle
from evals.readonly_eval import (
    DEFAULT_CASE_DIR,
    ReadonlyEvalCase,
    ReadonlyEvalResult,
    load_cases,
    run_case,
)
from evals.readonly_reporting import (
    build_readonly_manifest,
    create_server_run_id,
    result_to_record,
    summarize_results,
)

DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "eval-runs"
DEFAULT_BUDGET_LEDGER = (
    ROOT / "artifacts" / "private" / "deepseek-budget.sqlite3"
)
PRICE_SNAPSHOT_PATH = (
    ROOT / "pricing" / "deepseek-v4-flash-2026-07-29.json"
)
HARD_BUDGET_LIMIT_CNY = Decimal("20")
EXECUTION_BUDGET_LIMIT_CNY = Decimal("18")


def build_deepseek_budget_guard(
    *,
    settings: Settings,
    run_id: str,
    purpose: str,
) -> DeepSeekBudgetGuard:
    snapshot = load_price_snapshot(PRICE_SNAPSHOT_PATH)
    return DeepSeekBudgetGuard(
        ledger=SQLiteBudgetLedger(
            path=DEFAULT_BUDGET_LEDGER,
            hard_limit_cny=HARD_BUDGET_LIMIT_CNY,
            execution_limit_cny=EXECUTION_BUDGET_LIMIT_CNY,
        ),
        run_id=run_id,
        purpose=purpose,
        price_snapshot=snapshot,
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
    if not 1 <= args.trials <= 10:
        parser.error("--trials must be between 1 and 10")
    if args.purpose in {"dev_repeat", "holdout_formal"} and args.trials != 4:
        parser.error(f"{args.purpose} requires exactly 4 trials")
    if args.purpose == "holdout_formal" and args.split != "holdout":
        parser.error("holdout_formal requires --split holdout")
    if args.split == "holdout" and args.case_dir.resolve() == DEFAULT_CASE_DIR.resolve():
        parser.error("holdout runs require an explicit non-development --case-dir")
    if not args.case_set_name.strip():
        parser.error("--case-set-name cannot be empty")


def run_eval_suite(
    *,
    model: ChatModel,
    settings: Settings,
    cases: Sequence[ReadonlyEvalCase],
    run_id: str,
    purpose: str,
    split: str,
    case_set_name: str,
    trials: int,
    output_root: Path,
    budget_report_provider: Callable[[], dict] | None = None,
) -> tuple[list[ReadonlyEvalResult], dict, Path]:
    started_at = datetime.now(UTC)
    results = [
        run_case(
            case,
            model=model,
            server_run_id=run_id,
            trial=trial,
        )
        for trial in range(1, trials + 1)
        for case in cases
    ]
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
    )
    records = [
        result_to_record(result, split=split)
        for result in results
    ]
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
) -> None:
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
            print(f"  - {item.case_id} trial {item.trial}: {failure}")

    passed = summary["strict"]["passed"]
    total = summary["total_trials"]
    print(f"\n{passed}/{total} read-only Agent trials passed.")
    print(
        "Safety assertions passed "
        f"{summary['security']['passed']}/{total}; "
        "business-state changes: "
        f"{summary['business_state']['changed_trials']}."
    )
    print(f"Verified evidence bundle: {bundle_path}")
    print(
        "This is a versioned harness result, not a production safety "
        "certification."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    run_id = args.run_id or create_server_run_id()
    settings = Settings()

    try:
        cases = load_cases(args.case_dir)
    except (OSError, ValueError) as exc:
        print(f"CASE ERROR: {exc}")
        return 2
    if not cases:
        print(f"CASE ERROR: no Eval cases found in {args.case_dir}")
        return 2
    bundle_target = args.output_root / run_id
    if bundle_target.exists():
        print(
            "EVIDENCE ERROR: output bundle already exists; "
            "use a fresh server run ID."
        )
        return 3
    budget_guard = None
    try:
        budget_guard = build_deepseek_budget_guard(
            settings=settings,
            run_id=run_id,
            purpose=args.purpose,
        )
    except BudgetError as exc:
        print(f"BUDGET ERROR: {exc}")
        return 2

    try:
        model = build_deepseek_client(
            settings,
            budget_guard=budget_guard,
        )
    except ValueError as exc:
        budget_guard.close()
        print(f"CONFIGURATION ERROR: {exc}")
        print(
            "Load a private DEEPSEEK_API_KEY into the process environment, "
            "then rerun this command."
        )
        return 2

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
        )
    except (OSError, ValueError) as exc:
        print(f"EVIDENCE ERROR: {type(exc).__name__}: {exc}")
        return 3
    finally:
        model.close()
        budget_guard.close()

    _print_results(results, summary, bundle_path)
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
