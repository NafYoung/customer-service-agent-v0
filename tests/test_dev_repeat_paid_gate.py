from __future__ import annotations

import shutil
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.deepseek_budget import (
    calculate_usage_cost_from_rates,
    format_cny,
)
from app.config import Settings
from evals import run_readonly_agent_evals as runner
from evals.canonical_pricing import (
    canonical_budget_price_payload,
    canonical_worst_case_attempt_reservation_cny,
    load_canonical_price_snapshot,
)
from evals.evidence import (
    BusinessStateDelta,
    ModelCallEvidence,
)
from evals.evidence_schema import BudgetSummary
from evals.readonly_eval import (
    DEFAULT_CASE_DIR,
    ReadonlyEvalResult,
    ScoreCheck,
    load_cases,
)
from evals.readonly_reporting import build_readonly_manifest

ROOT = Path(__file__).resolve().parents[1]
REGRESSION_CASE_DIR = ROOT / "evals" / "readonly_regression_cases"
USAGE = {
    "prompt_tokens": 8,
    "completion_tokens": 2,
    "total_tokens": 10,
}


def _settings() -> Settings:
    return Settings(
        deepseek_api_key=None,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-flash",
        deepseek_max_tokens=1024,
        deepseek_temperature=0,
    )


def _model_call(
    *,
    case_id: str,
    trial: int,
    usage: dict[str, int] | None = USAGE,
    provider_attempts: int = 1,
) -> ModelCallEvidence:
    return ModelCallEvidence(
        sequence=1,
        status="success",
        started_at="2026-07-29T12:00:00+00:00",
        latency_ms=1,
        message_count=2,
        tool_contract_count=6,
        finish_reason="stop",
        response_id=f"response-{case_id}-{trial}",
        observed_model="deepseek-v4-flash",
        usage=usage,
        provider_attempts=provider_attempts,
    )


def _result(*, case_id: str, trial: int) -> ReadonlyEvalResult:
    score_checks = [
        ScoreCheck(category, f"{category} passed", True)
        for category in (
            "task_success",
            "tool_selection",
            "security",
            "communication",
            "efficiency",
        )
    ]
    return ReadonlyEvalResult(
        case_id=case_id,
        trial=trial,
        case_run_id=f"eval-run-{case_id}-{trial}",
        input_sha256="0" * 64,
        passed=True,
        started_at="2026-07-29T12:00:00+00:00",
        completed_at="2026-07-29T12:00:01+00:00",
        duration_ms=1,
        checks=[check.message for check in score_checks],
        score_checks=score_checks,
        final_text="safe answer",
        model_calls=(_model_call(case_id=case_id, trial=trial),),
        business_state_delta=BusinessStateDelta(
            changed=False,
            changed_tables=(),
            before_sha256="a" * 64,
            after_sha256="a" * 64,
        ),
    )


def _dev_repeat_inputs() -> tuple[list, list[ReadonlyEvalResult]]:
    cases = load_cases(REGRESSION_CASE_DIR)
    results = [
        _result(case_id=case.case_id, trial=trial)
        for trial in range(1, 5)
        for case in cases
    ]
    return cases, results


def _paid_budget(
    *,
    run_id: str,
    purpose: str,
    attempt_count: int,
) -> dict:
    settings = _settings()
    price = load_canonical_price_snapshot()
    per_attempt = calculate_usage_cost_from_rates(
        rates_cny=price.rates_cny.model_dump(),
        tokens_per_price_unit=price.tokens_per_price_unit,
        usage=USAGE,
    ).units
    settled = format_cny(per_attempt * attempt_count)
    remaining = format(
        Decimal("18") - Decimal(settled),
        "f",
    )
    amount = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": settled,
        "settled_cny": settled,
        "remaining_execution_cny": remaining,
        "attempt_count": attempt_count,
        "reserved_count": 0,
        "uncertain_count": 0,
    }
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
        "run_status": "completed",
        "run_identity": {
            "run_id": run_id,
            "purpose": purpose,
            "model": settings.deepseek_model,
            "price_sha256": price.sha256,
            "status": "completed",
            "started_at": "2026-07-29T12:00:00+00:00",
            "completed_at": "2026-07-29T12:05:00+00:00",
        },
        "price": canonical_budget_price_payload(price),
        "reservation_cny_per_attempt": (
            canonical_worst_case_attempt_reservation_cny(
                canonical_price=price,
                max_output_tokens=settings.deepseek_max_tokens,
            )
        ),
        "run": dict(amount),
        "cumulative": dict(amount),
    }


@pytest.mark.parametrize(
    ("purpose", "source_dir", "case_set_name", "truncate"),
    [
        (
            "diagnostic",
            DEFAULT_CASE_DIR,
            "readonly-dev-v1",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "readonly-regression-v1",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "wrong-regression-name",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "readonly-regression-v1",
            True,
        ),
    ],
)
def test_nonformal_paid_cli_rejects_noncanonical_case_identity_before_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    source_dir: Path,
    case_set_name: str,
    truncate: bool,
) -> None:
    case_dir = source_dir
    if not truncate and case_set_name in {
        "readonly-dev-v1",
        "readonly-regression-v1",
    }:
        case_dir = tmp_path / f"external-{purpose}"
        shutil.copytree(source_dir, case_dir)
    original_load_cases = runner.load_cases
    if truncate:
        monkeypatch.setattr(
            runner,
            "load_cases",
            lambda path: original_load_cases(path)[:-1],
        )
    monkeypatch.setattr(runner, "Settings", _settings)
    monkeypatch.setattr(
        runner,
        "freeze_readonly_harness",
        lambda settings: object(),
    )
    reached = {"budget": 0}

    def reject_budget(**kwargs):
        reached["budget"] += 1
        raise ValueError("budget guard must not be reached")

    monkeypatch.setattr(
        runner,
        "build_deepseek_budget_guard",
        reject_budget,
    )

    status = runner.main(
        [
            "--run-id",
            f"eval-20260729-{purpose}-gate",
            "--purpose",
            purpose,
            "--split",
            "dev",
            "--case-dir",
            str(case_dir),
            "--case-set-name",
            case_set_name,
            "--trials",
            "4" if purpose == "dev_repeat" else "1",
            "--output-root",
            str(tmp_path / "output"),
        ]
    )

    assert status == 2
    assert reached == {"budget": 0}


def test_dev_repeat_manifest_accepts_only_canonical_7_by_4_case_set() -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-valid"
    started = datetime(2026, 7, 29, 12, tzinfo=UTC)

    manifest = build_readonly_manifest(
        run_id=run_id,
        purpose="dev_repeat",
        split="dev",
        case_set_name="readonly-regression-v1",
        cases=cases,
        results=results,
        settings=_settings(),
        planned_trials=4,
        started_at=started,
        completed_at=started + timedelta(minutes=5),
        budget_report=_paid_budget(
            run_id=run_id,
            purpose="dev_repeat",
            attempt_count=len(results),
        ),
    )

    assert manifest["eval"]["case_count"] == 7
    assert (
        manifest["eval"]["case_set_sha256"]
        == "6340394c8edd5d95c2756f3f4753d4e224682b7f84a445c76b3abb675bad2edb"
    )


@pytest.mark.parametrize(
    "attack",
    [
        "active",
        "reserved",
        "uncertain",
        "unsettled",
        "overrun",
        "forged_price",
        "reservation",
        "attempt_count",
        "missing_usage",
        "retry",
        "cost",
    ],
)
def test_dev_repeat_manifest_rejects_unsettled_or_unpriced_paid_evidence(
    attack: str,
) -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-attacked"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=len(results),
    )
    if attack == "active":
        budget["run_status"] = "active"
        budget["run_identity"]["status"] = "active"
        budget["run_identity"]["completed_at"] = None
    elif attack in {"reserved", "uncertain"}:
        for scope in ("run", "cumulative"):
            budget[scope][f"{attack}_count"] = 1
    elif attack == "unsettled":
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "1"
            budget[scope]["remaining_execution_cny"] = "17"
    elif attack == "overrun":
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "18.1"
            budget[scope]["settled_cny"] = "18.1"
            budget[scope]["remaining_execution_cny"] = "0"
    elif attack == "forged_price":
        fake_hash = "0" * 64
        budget["run_identity"]["price_sha256"] = fake_hash
        budget["price"]["snapshot_sha256"] = fake_hash
    elif attack == "reservation":
        budget["reservation_cny_per_attempt"] = "0"
    elif attack == "attempt_count":
        budget["run"]["attempt_count"] += 1
    elif attack == "missing_usage":
        results[0].model_calls = (
            replace(results[0].model_calls[0], usage=None),
        )
    elif attack == "retry":
        results[0].model_calls = (
            replace(results[0].model_calls[0], provider_attempts=2),
        )
        budget["run"]["attempt_count"] += 1
    elif attack == "cost":
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "1"
            budget[scope]["settled_cny"] = "1"
            budget[scope]["remaining_execution_cny"] = "17"

    started = datetime(2026, 7, 29, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="budget|price|usage|attempt|canonical"):
        build_readonly_manifest(
            run_id=run_id,
            purpose="dev_repeat",
            split="dev",
            case_set_name="readonly-regression-v1",
            cases=cases,
            results=results,
            settings=_settings(),
            planned_trials=4,
            started_at=started,
            completed_at=started + timedelta(minutes=5),
            budget_report=budget,
        )


def _budget_with_attempt_evidence() -> dict:
    budget = _paid_budget(
        run_id="eval-20260729-attempt-evidence",
        purpose="dev_repeat",
        attempt_count=1,
    )
    bucket = {
        "status": "settled_exact",
        "settlement_mode": "exact",
        "reserved_cny": budget["reservation_cny_per_attempt"],
        "known_cost_cny": budget["run"]["committed_cny"],
        "count": 1,
    }
    budget["attempt_evidence"] = {
        "run": [dict(bucket)],
        "cumulative": [dict(bucket)],
    }
    return budget


@pytest.mark.parametrize(
    "attack",
    [
        "offline",
        "totals",
        "reservation",
        "run_not_in_cumulative",
    ],
)
def test_budget_summary_rejects_contradictory_attempt_evidence(
    attack: str,
) -> None:
    budget = _budget_with_attempt_evidence()
    if attack == "offline":
        budget = {
            "schema_version": "1.0",
            "enforcement_mode": "offline_no_paid_provider",
            "run_status": "completed",
            "price": None,
            "reservation_cny_per_attempt": "0",
            "run": {
                "currency": "CNY",
                "hard_limit_cny": "20",
                "execution_limit_cny": "18",
                "committed_cny": "0",
                "settled_cny": "0",
                "remaining_execution_cny": "18",
                "attempt_count": 0,
                "reserved_count": 0,
                "uncertain_count": 0,
            },
            "cumulative": {
                "currency": "CNY",
                "hard_limit_cny": "20",
                "execution_limit_cny": "18",
                "committed_cny": "0",
                "settled_cny": "0",
                "remaining_execution_cny": "18",
                "attempt_count": 0,
                "reserved_count": 0,
                "uncertain_count": 0,
            },
            "attempt_evidence": {"run": [], "cumulative": []},
        }
    elif attack == "totals":
        budget["attempt_evidence"]["run"][0]["count"] = 2
    elif attack == "reservation":
        budget["attempt_evidence"]["run"][0][
            "reserved_cny"
        ] = "1.5"
    elif attack == "run_not_in_cumulative":
        budget["attempt_evidence"]["cumulative"][0][
            "known_cost_cny"
        ] = "0.000013"
        budget["cumulative"]["committed_cny"] = "0.000013"
        budget["cumulative"]["settled_cny"] = "0.000013"
        budget["cumulative"]["remaining_execution_cny"] = "17.999987"
        budget["run"]["remaining_execution_cny"] = "17.999987"

    with pytest.raises(ValueError, match="attempt|bucket|offline|budget"):
        BudgetSummary.model_validate(deepcopy(budget))
