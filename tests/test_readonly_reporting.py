from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.config import Settings
from evals.evidence import BusinessStateDelta, ModelCallEvidence
from evals.readonly_eval import (
    ReadonlyEvalResult,
    ScoreCheck,
    load_cases,
)
from evals.readonly_reporting import (
    build_readonly_manifest,
    create_server_run_id,
    result_to_record,
    summarize_results,
)


def _budget_report() -> dict:
    amount = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": "0.0048",
        "settled_cny": "0.0048",
        "remaining_execution_cny": "17.9952",
        "attempt_count": 4,
        "reserved_count": 0,
        "uncertain_count": 0,
    }
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
        "price": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "currency": "CNY",
            "snapshot_sha256": "f" * 64,
            "source_url": (
                "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
            ),
            "usage_source_url": (
                "https://api-docs.deepseek.com/api/create-chat-completion/"
            ),
            "captured_at": "2026-07-29T08:58:58+00:00",
            "valid_until": "2026-07-30T08:58:58+00:00",
            "rates_cny": {
                "prompt_cache_hit": "0.02",
                "prompt_cache_miss": "1",
                "completion": "2",
            },
            "tokens_per_price_unit": 1_000_000,
        },
        "reservation_cny_per_attempt": "1.002048",
        "run": dict(amount),
        "cumulative": dict(amount),
    }


def _result(
    *,
    case_id: str,
    trial: int,
    passed: bool,
    efficiency_passed: bool = True,
) -> ReadonlyEvalResult:
    return ReadonlyEvalResult(
        case_id=case_id,
        trial=trial,
        case_run_id=f"eval-run-t{trial}-{case_id}",
        passed=passed,
        started_at="2026-07-29T10:00:00+00:00",
        completed_at="2026-07-29T10:00:01+00:00",
        duration_ms=1000 + trial,
        score_checks=[
            ScoreCheck("task_success", "task result", True),
            ScoreCheck("tool_selection", "tool result", True),
            ScoreCheck("security", "security result", True),
            ScoreCheck("communication", "communication result", True),
            ScoreCheck(
                "efficiency",
                "efficiency result",
                efficiency_passed,
            ),
        ],
        final_text="safe answer",
        model_calls=(
            ModelCallEvidence(
                sequence=1,
                status="success",
                started_at="2026-07-29T10:00:00+00:00",
                latency_ms=100,
                message_count=2,
                tool_contract_count=6,
                finish_reason="stop",
                response_id=f"response-{case_id}-{trial}",
                observed_model="deepseek-v4-flash",
                usage={
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                },
            ),
        ),
        business_state_delta=BusinessStateDelta(
            changed=False,
            changed_tables=(),
            before_sha256="a" * 64,
            after_sha256="a" * 64,
        ),
    )


def test_create_server_run_id_is_unique_and_url_safe():
    first = create_server_run_id()
    second = create_server_run_id()

    assert first != second
    assert first.startswith("eval-")
    assert "/" not in first
    assert " " not in first
    assert 8 <= len(first) <= 80


def test_summary_separates_strict_reliability_safety_usage_and_latency():
    results = [
        _result(case_id="case-a", trial=1, passed=True),
        _result(case_id="case-a", trial=2, passed=True),
        _result(case_id="case-b", trial=1, passed=True),
        _result(
            case_id="case-b",
            trial=2,
            passed=False,
            efficiency_passed=False,
        ),
    ]

    summary = summarize_results(
        run_id="eval-20260729-abcdef12",
        results=results,
        planned_trials=2,
        budget_report=_budget_report(),
    )

    assert summary["total_trials"] == 4
    assert summary["strict"]["passed"] == 3
    assert summary["strict"]["rate"] == 0.75
    assert summary["reliability"]["k"] == 2
    assert summary["reliability"]["cases_all_trials_passed"] == 1
    assert summary["reliability"]["pass_power_k"] == 0.5
    assert summary["security"]["passed"] == 4
    assert summary["security"]["all_trials_passed"] is True
    assert summary["score_layers"]["efficiency"]["passed"] == 3
    assert summary["usage"]["model_calls"] == 4
    assert summary["usage"]["total_tokens"] == 40
    assert summary["latency_ms"]["case"]["p50"] >= 1001
    assert summary["latency_ms"]["model_call"]["max"] == 100
    assert summary["business_state"]["changed_trials"] == 0
    assert summary["budget"]["run"]["committed_cny"] == "0.0048"
    assert summary["budget"]["cumulative"]["hard_limit_cny"] == "20"


def test_result_record_contains_trial_trajectory_without_eval_expectations():
    result = _result(case_id="case-a", trial=1, passed=True)

    record = result_to_record(result, split="dev")

    assert record["case_id"] == "case-a"
    assert record["trial"] == 1
    assert record["split"] == "dev"
    assert record["model_calls"][0]["usage"]["total_tokens"] == 10
    assert record["business_state"]["changed"] is False
    assert record["scores"]["security"] is True
    assert "expected" not in json.dumps(record)
    assert "user_message" not in json.dumps(record)


def test_manifest_fingerprints_harness_and_never_serializes_secret_or_holdout_ids():
    secret = "PRIVATE-MANIFEST-KEY-CANARY"
    settings = Settings(
        deepseek_api_key=secret,
        deepseek_base_url="https://api.deepseek.com/v1?private=query",
        deepseek_model="deepseek-v4-flash",
    )
    cases = load_cases()[:2]
    results = [
        _result(case_id=case.case_id, trial=1, passed=True)
        for case in cases
    ]
    started = datetime.now(UTC)
    completed = started + timedelta(seconds=2)

    manifest = build_readonly_manifest(
        run_id="eval-20260729-abcdef12",
        purpose="holdout_formal",
        split="holdout",
        case_set_name="readonly-holdout-v1",
        cases=cases,
        results=results,
        settings=settings,
        planned_trials=1,
        started_at=started,
        completed_at=completed,
        budget_report=_budget_report(),
    )
    serialized = json.dumps(manifest, ensure_ascii=False)

    assert secret not in serialized
    assert "private=query" not in serialized
    assert manifest["model"]["base_url_host"] == "api.deepseek.com"
    assert manifest["eval"]["case_count"] == 2
    assert manifest["eval"]["case_set_sha256"]
    assert "case_ids" not in manifest["eval"]
    assert manifest["harness"]["prompt_sha256"]
    assert manifest["harness"]["tool_contracts_sha256"]
    assert manifest["harness"]["policies_sha256"]
    assert manifest["harness"]["agent_loop_sha256"]
    assert manifest["harness"]["model_runtime_sha256"]
    assert manifest["harness"]["semantic_judge_prompt_sha256"]
    assert manifest["harness"]["semantic_judge_source_sha256"]
    assert manifest["source"]["source_tree_sha256"]
    assert manifest["execution"]["planned_trials"] == 1
    assert manifest["execution"]["completed_trials"] == 1
    assert manifest["model"]["observed_models"] == ["deepseek-v4-flash"]
    assert manifest["model"]["generation_config"]["temperature"] == 0.0
    assert manifest["model"]["semantic_judge"] == {
        "version": "atomic-claims-v1",
        "response_format": "json_object",
        "tools_enabled": False,
        "temperature": 0.0,
        "thinking": "disabled",
    }
    assert manifest["eval"]["scorer_version"] == "readonly-scorer-v6"
    assert manifest["budget"]["price_snapshot_sha256"] == "f" * 64
    assert manifest["budget"]["hard_limit_cny"] == "20"

    dev_manifest = build_readonly_manifest(
        run_id="eval-20260729-abcdef13",
        purpose="dev_repeat",
        split="dev",
        case_set_name="readonly-dev-v1",
        cases=cases,
        results=results,
        settings=settings,
        planned_trials=1,
        started_at=started,
        completed_at=completed,
        budget_report=_budget_report(),
    )
    assert dev_manifest["eval"]["case_ids"] == [
        case.case_id for case in cases
    ]
