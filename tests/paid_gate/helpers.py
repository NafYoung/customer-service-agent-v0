from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.deepseek_budget import (
    DeepSeekBudgetGuard,
    SQLiteBudgetLedger,
    calculate_usage_cost_from_rates,
    format_cny,
)
from app.agent.openai_compatible import AssistantTurn
from app.agent.readonly import ToolTrace
from app.config import Settings
from evals import holdout_lock as holdout_protocol
from evals import paid_ledger_binding as paid_ledger_binding_module
from evals import run_readonly_agent_evals as runner
from evals.calibration_attestation import (
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
)
from evals.canonical_pricing import (
    canonical_budget_price_payload,
    canonical_worst_case_attempt_reservation_cny,
    load_canonical_price_snapshot,
)
from evals.evidence import (
    BusinessStateDelta,
    ModelCallEvidence,
    stable_sha256,
    write_eval_bundle,
)
from evals.readonly_eval import (
    ReadonlyEvalCase,
    ReadonlyEvalResult,
    load_cases,
    rescore_readonly_case_evidence,
)
from evals.readonly_reporting import (
    build_readonly_manifest,
    result_to_record,
    summarize_results,
)
from evals.semantic_judge import (
    SemanticClaimVerdict,
    SemanticJudgeVerdict,
    effective_semantic_contract,
)

ROOT = Path(__file__).resolve().parents[2]
_REAL_PAID_LEDGER_REQUIRE = (
    paid_ledger_binding_module.require_persistent_budget_matches_trusted_ledger
)
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
        deepseek_temperature=0.0,
    )


def _model_call(
    *,
    case_id: str,
    trial: int,
    phase: str = "agent",
    tool_contract_count: int = 6,
    usage: dict[str, int] | None = USAGE,
    provider_attempts: int = 1,
) -> ModelCallEvidence:
    logical_call_sha256 = hashlib.sha256(
        f"{case_id}:{trial}:{phase}".encode("utf-8")
    ).hexdigest()
    return ModelCallEvidence(
        sequence=1,
        status="success",
        started_at="2026-08-20T12:00:00+00:00",
        latency_ms=1,
        message_count=2,
        tool_contract_count=tool_contract_count,
        phase=phase,
        finish_reason="stop",
        response_id=None,
        observed_model="deepseek-v4-flash",
        usage=usage,
        provider_attempts=provider_attempts,
        logical_call_sha256=logical_call_sha256,
    )


def _result(*, case: ReadonlyEvalCase, trial: int) -> ReadonlyEvalResult:
    answer_parts = [
        group[0]
        for group in (
            *case.expected.answer_must_contain_any,
            *case.expected.security_answer_must_contain_any,
        )
    ]
    final_text = "；".join(answer_parts) or "安全答复"
    trace: list[ToolTrace] = []
    asserted_tools: set[str] = set()
    for index, assertion in enumerate(case.expected.tool_assertions, start=1):
        trace.append(
            ToolTrace(
                tool_call_id=f"tool-{case.case_id}-{trial}-{index}",
                tool_name=assertion.tool_name,
                arguments={},
                success=(
                    assertion.success
                    if assertion.success is not None
                    else True
                ),
                result=deepcopy(assertion.result_contains),
                error_code=assertion.error_code,
                latency_ms=1,
            )
        )
        asserted_tools.add(assertion.tool_name)
    for tool_name in case.expected.required_tools:
        if tool_name in asserted_tools:
            continue
        trace.append(
            ToolTrace(
                tool_call_id=f"tool-{case.case_id}-{trial}-{len(trace) + 1}",
                tool_name=tool_name,
                arguments={},
                success=True,
                result={},
                error_code=None,
                latency_ms=1,
            )
        )
        asserted_tools.add(tool_name)
    for group in case.expected.required_any_tool_groups:
        if any(tool_name in asserted_tools for tool_name in group):
            continue
        tool_name = group[0]
        trace.append(
            ToolTrace(
                tool_call_id=f"tool-{case.case_id}-{trial}-{len(trace) + 1}",
                tool_name=tool_name,
                arguments={},
                success=True,
                result={},
                error_code=None,
                latency_ms=1,
            )
        )
        asserted_tools.add(tool_name)
    semantic_contract = case.expected.semantic_contract
    semantic_verdict: SemanticJudgeVerdict | None = None
    if semantic_contract is not None:
        effective_contract = effective_semantic_contract(semantic_contract)
        evidence_span = answer_parts[0] if answer_parts else final_text
        semantic_verdict = SemanticJudgeVerdict(
            claims=[
                *[
                    SemanticClaimVerdict(
                        id=claim.id,
                        relation="entailed",
                        evidence_spans=[evidence_span],
                    )
                    for claim in effective_contract.required_claims
                ],
                *[
                    SemanticClaimVerdict(
                        id=claim.id,
                        relation="not_mentioned",
                        evidence_spans=[],
                    )
                    for claim in effective_contract.forbidden_claims
                ],
            ],
            material_self_contradiction=False,
            contradiction_evidence=[],
        )
    input_sha256 = hashlib.sha256(
        case.user_message.encode("utf-8")
    ).hexdigest()
    rescored = rescore_readonly_case_evidence(
        case=case,
        input_sha256=input_sha256,
        final_text=final_text,
        tool_trace=trace,
        business_state_changed=False,
        business_write_count=0,
        error_code=None,
        semantic_verdict=semantic_verdict,
    )
    assert rescored.passed is True
    return ReadonlyEvalResult(
        case_id=case.case_id,
        trial=trial,
        case_run_id=f"eval-run-{case.case_id}-{trial}",
        input_sha256=input_sha256,
        passed=rescored.passed,
        started_at="2026-08-20T12:00:00+00:00",
        completed_at="2026-08-20T12:00:01+00:00",
        duration_ms=1,
        checks=list(rescored.checks),
        failures=list(rescored.failures),
        score_checks=list(rescored.score_checks),
        final_text=final_text,
        tool_names=tuple(item.tool_name for item in trace),
        tool_trace=tuple(trace),
        model_calls=(
            _model_call(case_id=case.case_id, trial=trial),
            _model_call(
                case_id=case.case_id,
                trial=trial,
                phase="semantic_judge",
                tool_contract_count=0,
            ),
        ),
        business_state_delta=BusinessStateDelta(
            changed=False,
            changed_tables=(),
            before_sha256="a" * 64,
            after_sha256="a" * 64,
        ),
        semantic_verdict=semantic_verdict,
    )


def _dev_repeat_inputs() -> tuple[list, list[ReadonlyEvalResult]]:
    cases = load_cases(REGRESSION_CASE_DIR)
    results = [
        _result(case=case, trial=trial)
        for trial in range(1, 5)
        for case in cases
    ]
    return cases, results


def _attempt_count(results: list[ReadonlyEvalResult]) -> int:
    return sum(len(result.model_calls) for result in results)


def _logical_call_hashes(
    results: list[ReadonlyEvalResult],
) -> list[str]:
    return [
        call.logical_call_sha256
        for result in results
        for call in result.model_calls
        if call.logical_call_sha256 is not None
    ]


class _CountingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def complete(self, **_: object) -> AssistantTurn:
        self.calls += 1
        return AssistantTurn(
            content="safe answer",
            tool_calls=(),
            finish_reason="stop",
            usage=dict(USAGE),
            response_id=f"response-{self.calls}",
            model="deepseek-v4-flash",
            provider_attempts=1,
        )

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class _FormalRuntimeInputs:
    cases: list[ReadonlyEvalCase]
    settings: Settings
    frozen_harness: runner.FrozenReadonlyHarness
    attestation: ValidatedCalibrationAttestation
    review: ValidatedCalibrationReview
    regression_gate: holdout_protocol.ValidatedRegressionGate
    declaration: holdout_protocol.HoldoutDeclaration
    acquired_lock: holdout_protocol.AcquiredHoldoutRunLock
    formal_evidence: runner.FormalHoldoutEvidence
    source_snapshot: dict[str, object]
    source_git_commit: str
    source_tree_sha256: str
    fixed_output_root: Path


def _formal_runtime_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _FormalRuntimeInputs:
    cases = [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"sealed-runtime-case-{index:02d}",
                "user_message": f"formal case {index}",
                "expected": {
                    "semantic_contract": {
                        "required_claims": [
                            {
                                "id": f"claim_{index:02d}",
                                "category": "task_success",
                                "proposition": "回答安全",
                            }
                        ],
                        "forbidden_claims": [],
                    }
                },
            }
        )
        for index in range(20)
    ]
    source_git_commit = "a" * 40
    source_tree_sha256 = "b" * 64
    source_snapshot: dict[str, object] = {
        "git_commit": source_git_commit,
        "git_dirty": False,
        "source_tree_sha256": source_tree_sha256,
        "python_version": "3.11-test",
        "platform": "test-platform",
        "package_versions": {"httpx": "test"},
    }
    source_identity_sha256 = stable_sha256(source_snapshot)
    settings = replace(
        _settings(),
        deepseek_api_key="test-only-placeholder",
    )
    frozen_harness = runner.freeze_readonly_harness(settings)
    fingerprints = dict(frozen_harness.fingerprints)
    harness_sha256 = stable_sha256(fingerprints)
    runtime_identity_sha256 = stable_sha256(
        {
            "source": source_snapshot,
            "harness": runner.readonly_harness_snapshot(
                settings=settings,
                fingerprints=fingerprints,
            ),
            "model": runner.readonly_model_snapshot(
                settings=settings,
                observed_models=[settings.deepseek_model],
            ),
        }
    )
    attestation = ValidatedCalibrationAttestation(
        report_sha256="d" * 64,
        run_id="eval-20260729-calibration-runtime-binding",
        source_git_commit=source_git_commit,
        fixture_sha256="e" * 64,
        contract_set_sha256="f" * 64,
        harness_sha256="1" * 64,
        result_count=49,
        fixture_ids=tuple(f"fixture-{index:02d}" for index in range(49)),
        fixture_kinds=tuple(
            (f"fixture-{index:02d}", "safe_canonical") for index in range(49)
        ),
        completed_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    review = ValidatedCalibrationReview(
        review_sha256="2" * 64,
        reviewer_id="independent-reviewer-v1",
        reviewed_count=5,
    )
    regression_gate = holdout_protocol.ValidatedRegressionGate(
        bundle_path=tmp_path / "private" / "regression",
        bundle_integrity_sha256="3" * 64,
        gate_sha256="4" * 64,
        run_id="eval-20260729-dev-repeat-runtime-binding",
        source_git_commit=source_git_commit,
        case_set_name="readonly-regression-v1",
        case_set_sha256="5" * 64,
        harness_sha256=harness_sha256,
        source_tree_sha256=source_tree_sha256,
        source_identity_sha256=source_identity_sha256,
        runtime_identity_sha256=runtime_identity_sha256,
        passed_trials=28,
    )
    declaration = holdout_protocol.HoldoutDeclaration(
        case_set_name="readonly-holdout-v2",
        case_set_sha256=runner._formal_case_set_sha256(cases),
        manifest_sha256="7" * 64,
        source_git_commit=source_git_commit,
        scorer_version="readonly-agent-v1",
        calibration_report_sha256=attestation.report_sha256,
        calibration_review_sha256=review.review_sha256,
        calibration_run_id=attestation.run_id,
        calibration_source_git_commit=attestation.source_git_commit,
        calibration_fixture_sha256=attestation.fixture_sha256,
        calibration_contract_set_sha256=attestation.contract_set_sha256,
        calibration_harness_sha256=attestation.harness_sha256,
        calibration_reviewer_id=review.reviewer_id,
        calibration_reviewed_count=review.reviewed_count,
        harness_sha256=harness_sha256,
        regression_bundle_integrity_sha256=(
            regression_gate.bundle_integrity_sha256
        ),
        regression_gate_sha256=regression_gate.gate_sha256,
        regression_run_id=regression_gate.run_id,
        regression_source_git_commit=regression_gate.source_git_commit,
        regression_case_set_name=regression_gate.case_set_name,
        regression_case_set_sha256=regression_gate.case_set_sha256,
        regression_harness_sha256=regression_gate.harness_sha256,
        regression_source_tree_sha256=regression_gate.source_tree_sha256,
        regression_source_identity_sha256=(
            regression_gate.source_identity_sha256
        ),
        regression_runtime_identity_sha256=(
            regression_gate.runtime_identity_sha256
        ),
    )
    private_root = tmp_path / "private"
    fixed_output_root = private_root / "eval-runs"
    fixed_lock_root = private_root / "holdout" / "formal-run-locks"
    monkeypatch.setattr(runner, "DEFAULT_OUTPUT_ROOT", fixed_output_root)
    monkeypatch.setattr(runner, "PRIVATE_ARTIFACT_ROOT", private_root)
    monkeypatch.setattr(runner, "DEFAULT_HOLDOUT_LOCK_ROOT", fixed_lock_root)
    monkeypatch.setattr(
        runner,
        "current_readonly_source_snapshot",
        lambda: deepcopy(source_snapshot),
    )
    acquired_lock = holdout_protocol.acquire_holdout_run_lock_with_hash(
        lock_root=fixed_lock_root,
        declaration=declaration,
        run_id="eval-20260729-formal-runtime-binding",
    )
    formal_evidence = runner.FormalHoldoutEvidence(
        declaration_manifest_sha256=declaration.manifest_sha256,
        lock_start_receipt_sha256=acquired_lock.receipt_sha256,
        declared_harness_sha256=harness_sha256,
        regression_bundle_integrity_sha256=(
            regression_gate.bundle_integrity_sha256
        ),
        regression_gate_sha256=regression_gate.gate_sha256,
        regression_run_id=regression_gate.run_id,
        regression_source_git_commit=regression_gate.source_git_commit,
        regression_case_set_name=regression_gate.case_set_name,
        regression_case_set_sha256=regression_gate.case_set_sha256,
        regression_harness_sha256=regression_gate.harness_sha256,
        regression_source_tree_sha256=regression_gate.source_tree_sha256,
        regression_source_identity_sha256=(
            regression_gate.source_identity_sha256
        ),
        regression_runtime_identity_sha256=(
            regression_gate.runtime_identity_sha256
        ),
    )
    return _FormalRuntimeInputs(
        cases=cases,
        settings=settings,
        frozen_harness=frozen_harness,
        attestation=attestation,
        review=review,
        regression_gate=regression_gate,
        declaration=declaration,
        acquired_lock=acquired_lock,
        formal_evidence=formal_evidence,
        source_snapshot=source_snapshot,
        source_git_commit=source_git_commit,
        source_tree_sha256=source_tree_sha256,
        fixed_output_root=fixed_output_root,
    )


def _formal_budget_guard(
    tmp_path: Path,
    *,
    run_id: str,
    settings: Settings,
) -> DeepSeekBudgetGuard:
    price_snapshot = load_canonical_price_snapshot()
    checked_at = price_snapshot.captured_at
    return DeepSeekBudgetGuard(
        ledger=SQLiteBudgetLedger(
            path=tmp_path / f"{run_id}.sqlite3",
            hard_limit_cny=Decimal("20"),
            execution_limit_cny=Decimal("18"),
        ),
        run_id=run_id,
        purpose="holdout_formal",
        price_snapshot=price_snapshot,
        model=settings.deepseek_model,
        max_output_tokens=settings.deepseek_max_tokens,
        now=checked_at,
    )


def _run_formal_runtime_attack(
    *,
    runtime: _FormalRuntimeInputs,
    model: object,
    semantic_judge_model: object,
    budget_report_provider: object,
    frozen_harness: runner.FrozenReadonlyHarness,
    capability: object,
) -> None:
    runner.run_eval_suite(
        model=model,
        settings=runtime.settings,
        cases=runtime.cases,
        run_id="eval-20260729-formal-runtime-binding",
        purpose="holdout_formal",
        split="holdout",
        case_set_name="readonly-holdout-v2",
        trials=4,
        output_root=runtime.fixed_output_root,
        budget_report_provider=budget_report_provider,
        semantic_judge_model=semantic_judge_model,
        calibration_attestation=runtime.attestation,
        calibration_review=runtime.review,
        formal_holdout_evidence=runtime.formal_evidence,
        frozen_harness=frozen_harness,
        source_git_commit=runtime.source_git_commit,
        source_tree_sha256=runtime.source_tree_sha256,
        formal_execution_capability=capability,
    )


def _issue_formal_execution_capability(
    *,
    runtime: _FormalRuntimeInputs,
    model: object,
    semantic_judge_model: object,
    budget_guard: DeepSeekBudgetGuard,
    budget_report_provider: object,
) -> object:
    return runner._create_validated_formal_execution_capability(
        run_id="eval-20260729-formal-runtime-binding",
        purpose="holdout_formal",
        split="holdout",
        cases=runtime.cases,
        case_set_name="readonly-holdout-v2",
        trials=4,
        source_git_commit=runtime.source_git_commit,
        source_tree_sha256=runtime.source_tree_sha256,
        settings=runtime.settings,
        model=model,
        semantic_judge_model=semantic_judge_model,
        budget_guard=budget_guard,
        budget_report_provider=budget_report_provider,
        frozen_harness=runtime.frozen_harness,
        calibration_attestation=runtime.attestation,
        calibration_review=runtime.review,
        declaration=runtime.declaration,
        regression_gate=runtime.regression_gate,
        acquired_lock=runtime.acquired_lock,
    )


def _assert_zero_budget_attempts(guard: DeepSeekBudgetGuard) -> None:
    assert guard.snapshot()["run"]["attempt_count"] == 0


def _paid_budget(
    *,
    run_id: str,
    purpose: str,
    attempt_count: int,
    settings: Settings | None = None,
    logical_call_hashes: list[str] | None = None,
) -> dict:
    runtime_settings = settings or _settings()
    price = load_canonical_price_snapshot()
    usage_cost = calculate_usage_cost_from_rates(
        rates_cny=price.rates_cny.model_dump(),
        tokens_per_price_unit=price.tokens_per_price_unit,
        usage=USAGE,
    )
    per_attempt = usage_cost.units
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
    reservation = canonical_worst_case_attempt_reservation_cny(
        canonical_price=price,
        max_output_tokens=runtime_settings.deepseek_max_tokens,
    )
    hashes = logical_call_hashes or [
        hashlib.sha256(
            f"{run_id}:paid-attempt:{index}".encode("utf-8")
        ).hexdigest()
        for index in range(attempt_count)
    ]
    if len(hashes) != attempt_count:
        raise ValueError("logical call hash count must equal paid attempt count")
    buckets = [
        {
            "logical_call_sha256": logical_call_sha256,
            "status": (
                "settled_exact"
                if usage_cost.mode == "exact"
                else "settled_upper_bound"
            ),
            "settlement_mode": usage_cost.mode,
            "reserved_cny": reservation,
            "known_cost_cny": format_cny(per_attempt),
            "error_code": None,
            "completed_at": "2026-08-20T12:04:59+00:00",
            "count": 1,
        }
        for logical_call_sha256 in hashes
    ]
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
        "run_status": "completed",
        "run_identity": {
            "run_id": run_id,
            "purpose": purpose,
            "model": runtime_settings.deepseek_model,
            "price_sha256": price.sha256,
            "status": "completed",
            "started_at": "2026-08-20T12:00:00+00:00",
            "completed_at": "2026-08-20T12:05:00+00:00",
        },
        "price": canonical_budget_price_payload(price),
        "reservation_cny_per_attempt": reservation,
        "run": dict(amount),
        "cumulative": dict(amount),
        "attempt_evidence": {
            "run": [dict(bucket) for bucket in buckets],
            "cumulative": [dict(bucket) for bucket in buckets],
        },
    }


def _dev_repeat_payload(
    *,
    settings: Settings | None = None,
) -> dict:
    runtime_settings = settings or _settings()
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-public-binding"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=_attempt_count(results),
        settings=runtime_settings,
        logical_call_hashes=_logical_call_hashes(results),
    )
    started = datetime(2026, 8, 20, 12, tzinfo=UTC)
    manifest = build_readonly_manifest(
        run_id=run_id,
        purpose="dev_repeat",
        split="dev",
        case_set_name="readonly-regression-v1",
        cases=cases,
        results=results,
        settings=runtime_settings,
        planned_trials=4,
        started_at=started,
        completed_at=started + timedelta(minutes=5),
        budget_report=budget,
    )
    manifest["artifacts"] = {
        "cases": "cases.jsonl",
        "summary": "summary.json",
        "trajectories": "trajectories/",
        "integrity": "integrity.json",
    }
    records = [result_to_record(result, split="dev") for result in results]
    return {
        "manifest": manifest,
        "cases": records,
        "summary": summarize_results(
            run_id=run_id,
            results=results,
            planned_trials=4,
            budget_report=budget,
        ),
        "trajectories": deepcopy(records),
        "integrity": {
            "schema_version": "1.0",
            "algorithm": "sha256",
            "files": {},
        },
    }


def _write_dev_repeat_bundle(
    tmp_path: Path,
    payload: dict,
) -> Path:
    manifest = payload["manifest"]
    output_root = tmp_path / "private-regression"
    output_root.mkdir(mode=0o700)
    return write_eval_bundle(
        output_root=output_root,
        run_id=manifest["run_id"],
        manifest=manifest,
        case_records=payload["cases"],
        summary=payload["summary"],
    )


def _trust_current_test_source(
    monkeypatch: pytest.MonkeyPatch,
    source_git_commit: str,
) -> None:
    monkeypatch.setattr(
        holdout_protocol,
        "require_clean_git_worktree",
        lambda **_: source_git_commit,
    )


def _budget_with_attempt_evidence() -> dict:
    return _paid_budget(
        run_id="eval-20260729-attempt-evidence",
        purpose="dev_repeat",
        attempt_count=1,
    )
