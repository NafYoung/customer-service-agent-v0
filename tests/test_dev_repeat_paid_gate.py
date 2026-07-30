from __future__ import annotations

import hashlib
import shutil
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.agent.deepseek_budget import (
    DeepSeekBudgetGuard,
    SQLiteBudgetLedger,
    calculate_usage_cost_from_rates,
    format_cny,
)
from app.agent.factory import build_deepseek_client
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
from evals.evidence_schema import (
    BudgetSummary,
    ModelCallRecord,
    validate_readonly_payload,
)
from evals.readonly_eval import (
    DEFAULT_CASE_DIR,
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
from tests.paid_ledger_testutil import install_matching_ledger_for_paid_payload

ROOT = Path(__file__).resolve().parents[1]
_REAL_PAID_LEDGER_REQUIRE = (
    paid_ledger_binding_module.require_persistent_budget_matches_trusted_ledger
)
REGRESSION_CASE_DIR = ROOT / "evals" / "readonly_regression_cases"
USAGE = {
    "prompt_tokens": 8,
    "completion_tokens": 2,
    "total_tokens": 10,
}


@pytest.fixture(autouse=True)
def _stub_paid_ledger_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "evals.paid_ledger_binding.require_persistent_budget_matches_trusted_ledger",
        _noop,
    )


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
        started_at="2026-08-01T12:00:00+00:00",
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
        started_at="2026-08-01T12:00:00+00:00",
        completed_at="2026-08-01T12:00:01+00:00",
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
        completed_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
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
    return DeepSeekBudgetGuard(
        ledger=SQLiteBudgetLedger(
            path=tmp_path / f"{run_id}.sqlite3",
            hard_limit_cny=Decimal("20"),
            execution_limit_cny=Decimal("18"),
        ),
        run_id=run_id,
        purpose="holdout_formal",
        price_snapshot=load_canonical_price_snapshot(),
        model=settings.deepseek_model,
        max_output_tokens=settings.deepseek_max_tokens,
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
            "completed_at": "2026-08-01T12:04:59+00:00",
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
            "started_at": "2026-08-01T12:00:00+00:00",
            "completed_at": "2026-08-01T12:05:00+00:00",
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
    started = datetime(2026, 8, 1, 12, tzinfo=UTC)
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


def test_programmatic_formal_run_requires_validated_context_before_model_call(
    tmp_path: Path,
) -> None:
    model = _CountingModel()
    case = load_cases(REGRESSION_CASE_DIR)[:1]

    with pytest.raises(ValueError, match="validated formal"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=case,
            run_id="eval-20260729-formal-no-context",
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=tmp_path,
        )

    assert model.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_programmatic_formal_run_rejects_forged_context_before_model_call(
    tmp_path: Path,
) -> None:
    model = _CountingModel()

    with pytest.raises(ValueError, match="validated formal"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=load_cases(REGRESSION_CASE_DIR)[:1],
            run_id="eval-20260729-formal-forged-context",
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=tmp_path,
            formal_run_context=object(),
        )

    assert model.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_formal_execution_capability_rejects_actor_model_replacement_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-bound-actor",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    replacement_model = _CountingModel()
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=replacement_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert replacement_model.calls == 0
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()


def test_formal_model_public_runtime_config_is_canonical_and_credential_free(
    tmp_path: Path,
) -> None:
    synthetic_key = "test-only-secret-canary"
    settings = replace(
        _settings(),
        deepseek_api_key=synthetic_key,
    )
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-public-config",
        settings=settings,
    )
    model = runner.build_deepseek_client(
        settings,
        budget_guard=guard,
    )
    try:
        public_config = model.public_runtime_config()
        assert public_config == runner.deepseek_public_runtime_config(settings)
        assert synthetic_key not in repr(public_config)
        assert all("key" not in field.casefold() for field in public_config)
        _assert_zero_budget_attempts(guard)
    finally:
        model.close()


def test_formal_execution_capability_rejects_instance_method_override_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-instance-method-override",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    forged_calls = 0

    def forged_complete(*, messages, tools):
        del messages, tools
        nonlocal forged_calls
        forged_calls += 1
        raise AssertionError("instance override reached a model-call boundary")

    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        bound_model.__dict__["complete"] = forged_complete
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert forged_calls == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.__dict__.pop("complete", None)
        bound_model.close()


def test_formal_execution_capability_rejects_class_method_override_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-class-method-override",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    capability = _issue_formal_execution_capability(
        runtime=runtime,
        model=bound_model,
        semantic_judge_model=bound_model,
        budget_guard=guard,
        budget_report_provider=report_provider,
    )
    forged_calls = 0

    def forged_complete(self, *, messages, tools):
        del self, messages, tools
        nonlocal forged_calls
        forged_calls += 1
        raise AssertionError("class override reached a model-call boundary")

    monkeypatch.setattr(
        type(bound_model),
        "complete",
        forged_complete,
    )
    try:
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert forged_calls == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()


def test_formal_execution_capability_rejects_guard_method_override_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-guard-method-override",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    capability = _issue_formal_execution_capability(
        runtime=runtime,
        model=bound_model,
        semantic_judge_model=bound_model,
        budget_guard=guard,
        budget_report_provider=report_provider,
    )
    forged_calls = 0

    def forged_reserve_attempt(*, logical_call_id, attempt_number):
        del logical_call_id, attempt_number
        nonlocal forged_calls
        forged_calls += 1
        raise AssertionError("guard override reached a paid-call boundary")

    guard.__dict__["reserve_attempt"] = forged_reserve_attempt
    try:
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert forged_calls == 0
        _assert_zero_budget_attempts(guard)
    finally:
        guard.__dict__.pop("reserve_attempt", None)
        bound_model.close()


def test_formal_execution_capability_rejects_judge_model_replacement_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-bound-judge",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    replacement_judge = _CountingModel()
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=replacement_judge,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert replacement_judge.calls == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()


@pytest.mark.parametrize("entity", ["prompt", "policy", "tool"])
def test_formal_execution_capability_rejects_harness_entity_replacement_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entity: str,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-bound-{entity}",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    if entity == "prompt":
        replacement_harness = replace(
            runtime.frozen_harness,
            agent_system_prompt=(
                runtime.frozen_harness.agent_system_prompt + "\nforged prompt"
            ),
        )
    elif entity == "policy":
        replacement_policies = dict(runtime.frozen_harness.policy_documents)
        policy_name = next(iter(replacement_policies))
        replacement_policies[policy_name] += "\nforged policy"
        replacement_harness = replace(
            runtime.frozen_harness,
            policy_documents=replacement_policies,
        )
    else:
        replacement_tools = deepcopy(runtime.frozen_harness.tool_contracts)
        replacement_tools[0]["description"] += " forged tool"
        replacement_harness = replace(
            runtime.frozen_harness,
            tool_contracts=tuple(replacement_tools),
        )
    assert replacement_harness.fingerprints == runtime.frozen_harness.fingerprints
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=replacement_harness,
                capability=capability,
            )
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()


def test_formal_execution_capability_refreezes_harness_before_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-refreeze",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    capability = _issue_formal_execution_capability(
        runtime=runtime,
        model=bound_model,
        semantic_judge_model=bound_model,
        budget_guard=guard,
        budget_report_provider=report_provider,
    )
    changed_canonical_harness = replace(
        runtime.frozen_harness,
        agent_system_prompt=(
            runtime.frozen_harness.agent_system_prompt + "\nchanged canonical input"
        ),
    )
    monkeypatch.setattr(
        runner,
        "freeze_readonly_harness",
        lambda settings: changed_canonical_harness,
    )
    try:
        with pytest.raises(ValueError, match="runtime identity"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()


@pytest.mark.parametrize(
    "runtime_object",
    ["budget_guard", "report_provider", "capability", "model_config"],
)
def test_formal_execution_capability_rejects_runtime_object_replacement_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_object: str,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-bound-{runtime_object}",
        settings=runtime.settings,
    )
    replacement_guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-replacement-{runtime_object}",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    capability = None
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        attacked_provider = report_provider
        attacked_capability = capability
        if runtime_object == "budget_guard":
            bound_model._budget_guard = replacement_guard
        elif runtime_object == "report_provider":
            attacked_provider = replacement_guard.snapshot
        elif runtime_object == "model_config":
            bound_model._model = "forged-model"
        else:
            attacked_capability = replace(
                capability,
                budget_guard=replacement_guard,
            )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=attacked_provider,
                frozen_harness=runtime.frozen_harness,
                capability=attacked_capability,
            )
        _assert_zero_budget_attempts(guard)
        _assert_zero_budget_attempts(replacement_guard)
    finally:
        bound_model._model = runtime.settings.deepseek_model
        bound_model._budget_guard = guard
        bound_model.close()
        replacement_guard.close()


def test_formal_execution_capability_rejects_post_issue_httpx_client_swap_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-client-swap",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    mock_hits = 0

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal mock_hits
        mock_hits += 1
        return httpx.Response(200, json={"choices": []})

    sealed_client = bound_model._client
    swapped_client = httpx.Client(
        transport=httpx.MockTransport(_mock_handler),
    )
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        bound_model._client = swapped_client
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert mock_hits == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model._client = sealed_client
        swapped_client.close()
        bound_model.close()


def test_formal_execution_capability_rejects_transport_mode_lie_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-transport-lie",
        settings=runtime.settings,
    )
    mock_hits = 0

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal mock_hits
        mock_hits += 1
        return httpx.Response(200, json={"choices": []})

    lied_model = build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
        transport=httpx.MockTransport(_mock_handler),
    )
    report_provider = guard.snapshot
    try:
        lied_model._transport_mode = "default"
        assert lied_model.live_transport_mode() == "custom"
        with pytest.raises(ValueError, match="formal execution capability"):
            _issue_formal_execution_capability(
                runtime=runtime,
                model=lied_model,
                semantic_judge_model=lied_model,
                budget_guard=guard,
                budget_report_provider=report_provider,
            )
        assert mock_hits == 0
        _assert_zero_budget_attempts(guard)
    finally:
        lied_model.close()


@pytest.mark.parametrize("rebinding", ["ledger", "price_snapshot"])
def test_formal_execution_capability_rejects_budget_graph_rebinding_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rebinding: str,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-budget-graph-{rebinding}",
        settings=runtime.settings,
    )
    replacement_guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-budget-graph-replacement-{rebinding}",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    original_ledger = guard._ledger
    original_price = guard._price_snapshot
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        if rebinding == "ledger":
            guard._ledger = replacement_guard._ledger
        else:
            guard._price_snapshot = replacement_guard._price_snapshot
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        guard._ledger = original_ledger
        guard._price_snapshot = original_price
        _assert_zero_budget_attempts(guard)
        _assert_zero_budget_attempts(replacement_guard)
    finally:
        guard._ledger = original_ledger
        guard._price_snapshot = original_price
        bound_model.close()
        replacement_guard.close()


@pytest.mark.parametrize(
    "output_attack",
    [
        "alternate_root",
        "symlink_escape",
        "source_drift",
        "runtime_drift",
    ],
)
def test_issued_formal_context_binds_output_and_source_before_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_attack: str,
) -> None:
    cases = [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"sealed-formal-case-{index:02d}",
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
    source_snapshot = {
        "git_commit": source_git_commit,
        "git_dirty": False,
        "source_tree_sha256": source_tree_sha256,
        "python_version": "3.11-test",
        "platform": "test-platform",
        "package_versions": {"httpx": "test"},
    }
    source_identity_sha256 = stable_sha256(source_snapshot)
    canonical_settings = replace(
        _settings(),
        deepseek_api_key="test-only-placeholder",
    )
    frozen_harness = runner.freeze_readonly_harness(canonical_settings)
    fingerprints = dict(frozen_harness.fingerprints)
    harness_sha256 = stable_sha256(fingerprints)
    runtime_identity_sha256 = stable_sha256(
        {
            "source": source_snapshot,
            "harness": runner.readonly_harness_snapshot(
                settings=canonical_settings,
                fingerprints=fingerprints,
            ),
            "model": runner.readonly_model_snapshot(
                settings=canonical_settings,
                observed_models=[canonical_settings.deepseek_model],
            ),
        }
    )
    attestation = ValidatedCalibrationAttestation(
        report_sha256="d" * 64,
        run_id="eval-20260729-calibration-output-binding",
        source_git_commit=source_git_commit,
        fixture_sha256="e" * 64,
        contract_set_sha256="f" * 64,
        harness_sha256="1" * 64,
        result_count=49,
        fixture_ids=tuple(f"fixture-{index:02d}" for index in range(49)),
        fixture_kinds=tuple(
            (f"fixture-{index:02d}", "safe_canonical") for index in range(49)
        ),
        completed_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
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
        run_id="eval-20260729-dev-repeat-output-binding",
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
        regression_bundle_integrity_sha256=(regression_gate.bundle_integrity_sha256),
        regression_gate_sha256=regression_gate.gate_sha256,
        regression_run_id=regression_gate.run_id,
        regression_source_git_commit=regression_gate.source_git_commit,
        regression_case_set_name=regression_gate.case_set_name,
        regression_case_set_sha256=regression_gate.case_set_sha256,
        regression_harness_sha256=regression_gate.harness_sha256,
        regression_source_tree_sha256=(regression_gate.source_tree_sha256),
        regression_source_identity_sha256=(regression_gate.source_identity_sha256),
        regression_runtime_identity_sha256=(regression_gate.runtime_identity_sha256),
    )
    private_root = tmp_path / "private"
    fixed_output_root = private_root / "eval-runs"
    fixed_lock_root = private_root / "holdout" / "formal-run-locks"
    outside_output_root = tmp_path / "outside-output"
    if output_attack == "symlink_escape":
        private_root.mkdir(mode=0o700)
        outside_output_root.mkdir(mode=0o700)
        fixed_output_root.symlink_to(
            outside_output_root,
            target_is_directory=True,
        )
    monkeypatch.setattr(runner, "DEFAULT_OUTPUT_ROOT", fixed_output_root)
    monkeypatch.setattr(runner, "PRIVATE_ARTIFACT_ROOT", private_root)
    monkeypatch.setattr(
        runner,
        "DEFAULT_HOLDOUT_LOCK_ROOT",
        fixed_lock_root,
    )
    acquired_lock = holdout_protocol.acquire_holdout_run_lock_with_hash(
        lock_root=fixed_lock_root,
        declaration=declaration,
        run_id="eval-20260729-formal-output-binding",
    )
    budget_guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-output-binding-budget",
        settings=canonical_settings,
    )
    model = runner.build_deepseek_client(
        canonical_settings,
        budget_guard=budget_guard,
    )
    budget_report_provider = budget_guard.snapshot
    capability = runner._create_validated_formal_execution_capability(
        run_id="eval-20260729-formal-output-binding",
        purpose="holdout_formal",
        split="holdout",
        cases=cases,
        case_set_name="readonly-holdout-v2",
        trials=4,
        source_git_commit=source_git_commit,
        source_tree_sha256=source_tree_sha256,
        settings=canonical_settings,
        model=model,
        semantic_judge_model=model,
        budget_guard=budget_guard,
        budget_report_provider=budget_report_provider,
        frozen_harness=frozen_harness,
        calibration_attestation=attestation,
        calibration_review=review,
        declaration=declaration,
        regression_gate=regression_gate,
        acquired_lock=acquired_lock,
    )
    context = capability.context
    requested_output_root = (
        fixed_output_root
        if output_attack in {"symlink_escape", "source_drift", "runtime_drift"}
        else tmp_path / "attacker-output"
    )
    if output_attack == "source_drift":
        monkeypatch.setattr(
            runner,
            "current_readonly_source_snapshot",
            lambda: {
                "git_commit": source_git_commit,
                "git_dirty": False,
                "source_tree_sha256": source_tree_sha256,
                "python_version": "forged",
                "platform": "forged",
                "package_versions": {"httpx": "forged"},
            },
        )
    elif output_attack == "runtime_drift":
        monkeypatch.setattr(
            runner,
            "current_readonly_source_snapshot",
            lambda: deepcopy(source_snapshot),
        )
    runtime_settings = (
        replace(
            canonical_settings,
            deepseek_timeout_seconds=600,
        )
        if output_attack == "runtime_drift"
        else canonical_settings
    )

    assert context.output_root == fixed_output_root
    with pytest.raises(
        ValueError,
        match="validated formal|source identity|runtime identity",
    ):
        runner.run_eval_suite(
            model=model,
            settings=runtime_settings,
            cases=cases,
            run_id=context.run_id,
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=requested_output_root,
            budget_report_provider=budget_report_provider,
            semantic_judge_model=model,
            calibration_attestation=attestation,
            calibration_review=review,
            formal_holdout_evidence=runner.FormalHoldoutEvidence(
                declaration_manifest_sha256=(declaration.manifest_sha256),
                lock_start_receipt_sha256=acquired_lock.receipt_sha256,
                declared_harness_sha256=harness_sha256,
                regression_bundle_integrity_sha256=(
                    regression_gate.bundle_integrity_sha256
                ),
                regression_gate_sha256=regression_gate.gate_sha256,
                regression_run_id=regression_gate.run_id,
                regression_source_git_commit=(regression_gate.source_git_commit),
                regression_case_set_name=regression_gate.case_set_name,
                regression_case_set_sha256=(regression_gate.case_set_sha256),
                regression_harness_sha256=(regression_gate.harness_sha256),
                regression_source_tree_sha256=(regression_gate.source_tree_sha256),
                regression_source_identity_sha256=(
                    regression_gate.source_identity_sha256
                ),
                regression_runtime_identity_sha256=(
                    regression_gate.runtime_identity_sha256
                ),
            ),
            frozen_harness=frozen_harness,
            source_git_commit=source_git_commit,
            source_tree_sha256=source_tree_sha256,
            formal_execution_capability=capability,
        )

    _assert_zero_budget_attempts(budget_guard)
    model.close()
    if output_attack == "symlink_escape":
        assert list(outside_output_root.iterdir()) == []
    elif output_attack in {"source_drift", "runtime_drift"}:
        assert (
            not requested_output_root.exists()
            or list(requested_output_root.iterdir()) == []
        )
    else:
        assert not requested_output_root.exists()


@pytest.mark.parametrize("attack", ["forged_sentinel", "case_hash"])
def test_programmatic_formal_context_rejects_internal_binding_attacks(
    tmp_path: Path,
    attack: str,
) -> None:
    model = _CountingModel()
    cases = load_cases(REGRESSION_CASE_DIR)[:1]
    context = runner.ValidatedFormalRunContext(
        run_id="eval-20260729-formal-context-attack",
        purpose="holdout_formal",
        split="holdout",
        case_set_name="readonly-holdout-v2",
        case_set_sha256=(
            "f" * 64 if attack == "case_hash" else runner._formal_case_set_sha256(cases)
        ),
        planned_case_count=1,
        planned_trials=4,
        source_git_commit="a" * 40,
        source_tree_sha256="b" * 64,
        harness_sha256="c" * 64,
        calibration_report_sha256="d" * 64,
        calibration_review_sha256="e" * 64,
        regression_bundle_integrity_sha256="1" * 64,
        regression_gate_sha256="2" * 64,
        regression_run_id="eval-20260729-dev-repeat-public-binding",
        regression_source_git_commit="a" * 40,
        regression_case_set_name="readonly-regression-v1",
        regression_case_set_sha256="3" * 64,
        regression_harness_sha256="c" * 64,
        regression_source_tree_sha256="b" * 64,
        regression_source_identity_sha256="6" * 64,
        regression_runtime_identity_sha256="7" * 64,
        declaration_manifest_sha256="4" * 64,
        lock_start_path=tmp_path / "readonly-holdout-v2.start.json",
        lock_start_receipt_sha256="5" * 64,
        output_root=tmp_path,
        _sentinel=(
            object() if attack == "forged_sentinel" else runner._FORMAL_CONTEXT_SENTINEL
        ),
    )

    with pytest.raises(ValueError, match="validated formal"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=cases,
            run_id="eval-20260729-formal-context-attack",
            purpose="holdout_formal",
            split="holdout",
            case_set_name="readonly-holdout-v2",
            trials=4,
            output_root=tmp_path,
            formal_run_context=context,
        )

    assert model.calls == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "attack",
    [
        "missing_trial",
        "security_failure",
        "changed_state",
        "stale_source",
        "stale_harness",
    ],
)
def test_formal_regression_gate_rejects_noncanonical_public_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    if attack == "missing_trial":
        payload["cases"].pop()
        payload["trajectories"].pop()
    elif attack == "security_failure":
        payload["summary"]["security"]["passed"] = 27
        payload["summary"]["security"]["failed"] = 1
        payload["summary"]["security"]["rate"] = 27 / 28
        payload["summary"]["security"]["all_trials_passed"] = False
    elif attack == "changed_state":
        payload["summary"]["business_state"]["changed_trials"] = 1
        payload["summary"]["business_state"]["all_trials_unchanged"] = False
    elif attack == "stale_source":
        payload["manifest"]["source"]["git_commit"] = "b" * 40
    elif attack == "stale_harness":
        payload["manifest"]["harness"]["runtime_harness_sha256"] = "b" * 64
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )


def test_formal_regression_gate_accepts_only_verified_28_of_28_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    gate = holdout_protocol.validate_regression_gate(
        bundle_path=bundle_path,
        private_root=tmp_path,
        source_git_commit=source_git_commit,
        harness_sha256=expected_harness,
    )

    assert gate.run_id == payload["manifest"]["run_id"]
    assert gate.source_git_commit == source_git_commit
    assert gate.case_set_name == "readonly-regression-v1"
    assert gate.passed_trials == 28


def test_formal_regression_gate_rejects_raw_write_evidence_hidden_by_forged_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    attacked = payload["cases"][0]
    attacked["final_text"] = "已执行取消，订单已经取消。"
    attacked["tool_trace"] = [
        {
            "tool_call_id": "forged-write-call-1",
            "tool_name": "execute_prepared_action",
            "arguments": {"action_id": "forged-action-1"},
            "success": True,
            "result": {"status": "executed"},
            "error_code": None,
            "latency_ms": 1,
        }
    ]
    attacked["business_state"] = {
        "changed": False,
        "changed_tables": [],
        "before_sha256": "a" * 64,
        "after_sha256": "a" * 64,
    }
    attacked["counted_action_records"] = 1
    attacked["scores"] = {
        category: True
        for category in (
            "task_success",
            "tool_selection",
            "security",
            "communication",
            "efficiency",
        )
    }
    attacked["score_checks"] = [
        {
            "category": category,
            "message": f"{category} passed",
            "passed": True,
        }
        for category in attacked["scores"]
    ]
    attacked["checks"] = [
        check["message"]
        for check in attacked["score_checks"]
    ]
    attacked["failures"] = []
    attacked["status"] = "passed"
    payload["trajectories"][0] = deepcopy(attacked)
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression|score|write|tool|state",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )


@pytest.mark.parametrize(
    "settings_kwargs",
    [
        {"deepseek_timeout_seconds": 600},
        {"deepseek_max_tokens": 4096},
        {"deepseek_max_retries": 99},
        {"agent_max_tool_rounds": 99},
        {"agent_max_tool_calls": 999},
    ],
)
def test_formal_regression_gate_rejects_coordinated_noncanonical_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings_kwargs: dict[str, object],
) -> None:
    settings = replace(_settings(), **settings_kwargs)
    payload = _dev_repeat_payload(settings=settings)
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="canonical|runtime|regression",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
            settings=settings,
        )


def test_formal_regression_gate_rejects_mixed_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    source_snapshot = deepcopy(payload["manifest"]["source"])
    source_snapshot["git_dirty"] = False
    source_snapshot["source_tree_sha256"] = "b" * 64
    payload["manifest"]["source"] = deepcopy(source_snapshot)
    monkeypatch.setattr(
        holdout_protocol,
        "current_source_tree_sha256",
        lambda: "a" * 64,
    )
    monkeypatch.setattr(
        holdout_protocol,
        "current_readonly_source_snapshot",
        lambda: deepcopy(source_snapshot),
    )
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="trusted runtime changed",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )


def test_public_dev_repeat_rejects_budget_identity_before_price_window() -> None:
    payload = _dev_repeat_payload()
    payload["summary"]["budget"]["run_identity"]["started_at"] = (
        "2026-07-28T00:00:00+00:00"
    )

    with pytest.raises(
        ValueError,
        match="price|window|identity|budget",
    ):
        validate_readonly_payload(payload)


@pytest.mark.parametrize(
    ("field_path", "forged_value"),
    [
        (("source", "source_tree_sha256"), "f" * 64),
        (("source", "python_version"), "0.0.0-forged"),
        (("source", "platform"), "forged-platform"),
        (("source", "package_versions"), {"forged": "1.0"}),
        (("eval", "scorer_version"), "forged-scorer"),
        (("eval", "scorer_sha256"), "f" * 64),
        (("harness", "prompt_sha256"), "f" * 64),
        (("harness", "tool_contracts_sha256"), "f" * 64),
        (("harness", "policies_sha256"), "f" * 64),
        (("harness", "seed_data_sha256"), "f" * 64),
        (("harness", "agent_loop_sha256"), "f" * 64),
        (("harness", "model_runtime_sha256"), "f" * 64),
        (("harness", "semantic_judge_version"), "forged-judge"),
        (("harness", "semantic_judge_prompt_sha256"), "f" * 64),
        (("harness", "semantic_judge_source_sha256"), "f" * 64),
        (
            ("harness", "semantic_calibration_source_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_validator_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_runner_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_corpus_sha256"),
            "f" * 64,
        ),
        (("harness", "evidence_protocol_sha256"), "f" * 64),
        (
            ("harness", "canonical_price_snapshot_sha256"),
            "f" * 64,
        ),
        (("harness", "max_tool_rounds"), 99),
        (("harness", "max_tool_calls"), 99),
        (("model", "provider"), "forged-provider"),
        (("model", "requested_model"), "forged-model"),
        (("model", "observed_models"), ["forged-model"]),
        (("model", "base_url_host"), "attacker.example"),
        (("model", "generation_config", "temperature"), 0.5),
        (("model", "generation_config", "seed"), 7),
        (("model", "generation_config", "max_tokens"), 2048),
        (("model", "timeout_seconds"), 99),
        (("model", "retry_policy", "max_retries"), 99),
        (("model", "retry_policy", "backoff"), "forged-backoff"),
        (("model", "semantic_judge", "version"), "forged-judge"),
        (("model", "semantic_judge", "temperature"), 0.5),
    ],
)
def test_formal_regression_gate_rejects_self_attested_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_path: tuple[str, ...],
    forged_value: object,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    target = payload["manifest"]
    for field_name in field_path[:-1]:
        nested = target[field_name]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = forged_value
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression|runtime|source|model",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )


def test_formal_regression_gate_rejects_renamed_or_replaced_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)
    renamed_path = bundle_path.with_name("renamed-regression-bundle")
    bundle_path.rename(renamed_path)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=renamed_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )


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


@pytest.mark.parametrize("attack", ["absolute", "traversal"])
def test_cli_rejects_unsafe_run_id_before_output_path_probe(
    tmp_path: Path,
    attack: str,
) -> None:
    outside = tmp_path / "outside-existing"
    outside.mkdir()
    run_id = str(outside) if attack == "absolute" else "../outside-existing"

    with pytest.raises(SystemExit):
        runner.main(
            [
                "--run-id",
                run_id,
                "--output-root",
                str(tmp_path / "output"),
            ]
        )


@pytest.mark.parametrize(
    ("purpose", "case_set_name", "trials"),
    [
        ("diagnostic", "arbitrary-diagnostic-v1", 1),
        ("dev_repeat", "arbitrary-repeat-v1", 4),
        ("unknown-purpose", "arbitrary-unknown-v1", 1),
    ],
)
def test_programmatic_runner_rejects_invalid_scope_before_model_calls(
    tmp_path: Path,
    purpose: str,
    case_set_name: str,
    trials: int,
) -> None:
    model = _CountingModel()
    output_root = tmp_path / "output"
    arbitrary_case = ReadonlyEvalCase.model_validate(
        {
            "case_id": "arbitrary-programmatic-case",
            "user_message": "arbitrary",
            "expected": {},
        }
    )

    with pytest.raises(ValueError, match="purpose|canonical|case"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=[arbitrary_case],
            run_id="eval-20260729-programmatic-preflight",
            purpose=purpose,
            split="dev",
            case_set_name=case_set_name,
            trials=trials,
            output_root=output_root,
        )

    assert model.calls == 0
    assert not output_root.exists()


def test_diagnostic_manifest_and_schema_reject_noncanonical_identity() -> None:
    arbitrary_case = ReadonlyEvalCase.model_validate(
        {
            "case_id": "arbitrary-diagnostic-case",
            "user_message": "arbitrary",
            "expected": {},
        }
    )
    result = _result(case=arbitrary_case, trial=1)
    started = datetime(2026, 8, 1, 12, tzinfo=UTC)

    with pytest.raises(ValueError, match="canonical|case"):
        build_readonly_manifest(
            run_id="eval-20260729-diagnostic-builder-forgery",
            purpose="diagnostic",
            split="dev",
            case_set_name="arbitrary-diagnostic-v1",
            cases=[arbitrary_case],
            results=[result],
            settings=_settings(),
            planned_trials=1,
            started_at=started,
            completed_at=started + timedelta(minutes=1),
        )

    canonical_cases = load_cases(DEFAULT_CASE_DIR)
    canonical_results = [
        _result(case=case, trial=1) for case in canonical_cases
    ]
    for result in canonical_results:
        result.model_calls = (
            replace(
                result.model_calls[0],
                observed_model="offline-eval-model",
                provider_attempts=0,
            ),
        )
    manifest = build_readonly_manifest(
        run_id="eval-20260729-diagnostic-schema-forgery",
        purpose="diagnostic",
        split="dev",
        case_set_name="readonly-dev-v1",
        cases=canonical_cases,
        results=canonical_results,
        settings=_settings(),
        planned_trials=1,
        started_at=started,
        completed_at=started + timedelta(minutes=1),
    )
    manifest["eval"]["case_set_name"] = "arbitrary-diagnostic-v1"
    manifest["artifacts"] = {
        "cases": "cases.jsonl",
        "summary": "summary.json",
        "trajectories": "trajectories/",
        "integrity": "integrity.json",
    }
    records = [result_to_record(item, split="dev") for item in canonical_results]
    payload = {
        "manifest": manifest,
        "cases": records,
        "summary": summarize_results(
            run_id=manifest["run_id"],
            results=canonical_results,
            planned_trials=1,
        ),
        "trajectories": deepcopy(records),
        "integrity": {
            "schema_version": "1.0",
            "algorithm": "sha256",
            "files": {},
        },
    }

    with pytest.raises(ValueError, match="diagnostic|canonical|case"):
        validate_readonly_payload(payload)


def test_dev_repeat_manifest_accepts_only_canonical_7_by_4_case_set() -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-valid"
    started = datetime(2026, 8, 1, 12, tzinfo=UTC)

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
            attempt_count=_attempt_count(results),
            logical_call_hashes=_logical_call_hashes(results),
        ),
    )

    assert manifest["eval"]["case_count"] == 7
    assert (
        manifest["eval"]["case_set_sha256"]
        == "6340394c8edd5d95c2756f3f4753d4e224682b7f84a445c76b3abb675bad2edb"
    )


def test_public_validator_recomputes_dev_repeat_bucket_costs() -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-public"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=_attempt_count(results),
        logical_call_hashes=_logical_call_hashes(results),
    )
    started = datetime(2026, 8, 1, 12, tzinfo=UTC)
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
        budget_report=budget,
    )
    manifest["artifacts"] = {
        "cases": "cases.jsonl",
        "summary": "summary.json",
        "trajectories": "trajectories/",
        "integrity": "integrity.json",
    }
    records = [result_to_record(result, split="dev") for result in results]
    payload = {
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
    validate_readonly_payload(payload)

    forged = deepcopy(payload)
    forged_cost = Decimal("0.000013")
    forged_total = forged_cost * _attempt_count(results)
    for scope in ("run", "cumulative"):
        forged["summary"]["budget"]["attempt_evidence"][scope][0]["known_cost_cny"] = (
            format(forged_cost, "f")
        )
        forged["summary"]["budget"][scope]["committed_cny"] = format(forged_total, "f")
        forged["summary"]["budget"][scope]["settled_cny"] = format(forged_total, "f")
        forged["summary"]["budget"][scope]["remaining_execution_cny"] = format(
            Decimal("18") - forged_total, "f"
        )

    with pytest.raises(ValueError, match="attempt|bucket|cost|record"):
        validate_readonly_payload(forged)


def test_dev_repeat_payload_cannot_be_relabelled_as_diagnostic() -> None:
    payload = _dev_repeat_payload()
    payload["manifest"]["purpose"] = "diagnostic"
    payload["summary"]["budget"]["run_identity"]["purpose"] = "diagnostic"

    with pytest.raises(ValueError, match="diagnostic|canonical|case"):
        validate_readonly_payload(payload)


@pytest.mark.parametrize(
    "attack",
    [
        "move_calls_between_trials",
        "all_calls_missing_with_forged_manifest",
        "success_has_error",
        "agent_contract_count",
    ],
)
def test_public_validator_binds_paid_calls_to_each_trial(
    attack: str,
) -> None:
    payload = _dev_repeat_payload()
    if attack == "move_calls_between_trials":
        for section in ("cases", "trajectories"):
            moved = payload[section][0]["model_calls"]
            payload[section][0]["model_calls"] = []
            payload[section][1]["model_calls"] = [
                *moved,
                *payload[section][1]["model_calls"],
            ]
    elif attack == "all_calls_missing_with_forged_manifest":
        for section in ("cases", "trajectories"):
            for record in payload[section]:
                record["model_calls"] = []
        payload["summary"]["usage"] = {"model_calls": 0}
        payload["summary"]["latency_ms"]["model_call"] = {
            "p50": None,
            "p95": None,
            "max": None,
            "total": 0,
        }
        budget = payload["summary"]["budget"]
        for scope in ("run", "cumulative"):
            budget[scope].update(
                {
                    "committed_cny": "0",
                    "settled_cny": "0",
                    "remaining_execution_cny": "18",
                    "attempt_count": 0,
                    "reserved_count": 0,
                    "uncertain_count": 0,
                }
            )
            budget["attempt_evidence"][scope] = []
        payload["manifest"]["model"]["observed_models"] = [
            payload["manifest"]["model"]["requested_model"]
        ]
    else:
        for section in ("cases", "trajectories"):
            agent_call = payload[section][0]["model_calls"][0]
            if attack == "success_has_error":
                agent_call["error_code"] = "FORGED_SUCCESS_ERROR"
            else:
                agent_call["tool_contract_count"] = 5

    with pytest.raises(
        ValueError,
        match="call|trial|record|observed|model",
    ):
        validate_readonly_payload(payload)


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
        "missing_attempt_evidence",
        "bucket_call_mismatch",
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
        attempt_count=_attempt_count(results),
        logical_call_hashes=_logical_call_hashes(results),
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
            *results[0].model_calls[1:],
        )
    elif attack == "retry":
        results[0].model_calls = (
            replace(results[0].model_calls[0], provider_attempts=2),
            *results[0].model_calls[1:],
        )
        budget["run"]["attempt_count"] += 1
    elif attack == "cost":
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "1"
            budget[scope]["settled_cny"] = "1"
            budget[scope]["remaining_execution_cny"] = "17"
    elif attack == "missing_attempt_evidence":
        budget.pop("attempt_evidence")
    elif attack == "bucket_call_mismatch":
        mismatched_cost = Decimal("0.000013")
        mismatched_total = mismatched_cost * len(results)
        for scope in ("run", "cumulative"):
            budget["attempt_evidence"][scope][0]["known_cost_cny"] = format(
                mismatched_cost, "f"
            )
            budget[scope]["committed_cny"] = format(
                mismatched_total,
                "f",
            )
            budget[scope]["settled_cny"] = format(
                mismatched_total,
                "f",
            )
            budget[scope]["remaining_execution_cny"] = format(
                Decimal("18") - mismatched_total,
                "f",
            )

    started = datetime(2026, 8, 1, 12, tzinfo=UTC)
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


@pytest.mark.parametrize(
    "attack",
    [
        "move_calls_between_trials",
        "agent_phase_missing",
        "agent_sequence_gap",
        "judge_has_tools",
        "call_outside_trial_window",
        "success_has_error",
        "agent_contract_count",
    ],
)
def test_dev_repeat_manifest_binds_calls_to_each_completed_trial(
    attack: str,
) -> None:
    cases, results = _dev_repeat_inputs()
    run_id = "eval-20260729-dev-repeat-call-binding"
    budget = _paid_budget(
        run_id=run_id,
        purpose="dev_repeat",
        attempt_count=_attempt_count(results),
        logical_call_hashes=_logical_call_hashes(results),
    )
    if attack == "move_calls_between_trials":
        moved = results[0].model_calls
        results[0].model_calls = ()
        results[1].model_calls = (*moved, *results[1].model_calls)
    elif attack == "agent_phase_missing":
        results[0].model_calls = tuple(
            replace(call, phase="semantic_judge") for call in results[0].model_calls
        )
    elif attack == "agent_sequence_gap":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            replace(agent, sequence=2),
            judge,
        )
    elif attack == "judge_has_tools":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            agent,
            replace(judge, tool_contract_count=6),
        )
    elif attack == "call_outside_trial_window":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            replace(
                agent,
                started_at="2026-08-01T11:59:59+00:00",
            ),
            judge,
        )
    elif attack == "success_has_error":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            replace(agent, error_code="FORGED_SUCCESS_ERROR"),
            judge,
        )
    elif attack == "agent_contract_count":
        agent, judge = results[0].model_calls
        results[0].model_calls = (
            replace(agent, tool_contract_count=5),
            judge,
        )

    started = datetime(2026, 8, 1, 12, tzinfo=UTC)
    with pytest.raises(
        ValueError,
        match="call|trial|phase|sequence|judge|time|record",
    ):
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
    return _paid_budget(
        run_id="eval-20260729-attempt-evidence",
        purpose="dev_repeat",
        attempt_count=1,
    )


@pytest.mark.parametrize(
    "attack",
    [
        "offline",
        "totals",
        "reservation",
        "run_not_in_cumulative",
        "duplicate_run_not_in_cumulative",
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
        budget["attempt_evidence"]["run"][0]["reserved_cny"] = "1.5"
    elif attack == "run_not_in_cumulative":
        budget["attempt_evidence"]["cumulative"][0]["known_cost_cny"] = "0.000013"
        budget["cumulative"]["committed_cny"] = "0.000013"
        budget["cumulative"]["settled_cny"] = "0.000013"
        budget["cumulative"]["remaining_execution_cny"] = "17.999987"
        budget["run"]["remaining_execution_cny"] = "17.999987"
    elif attack == "duplicate_run_not_in_cumulative":
        canonical_bucket = deepcopy(budget["attempt_evidence"]["run"][0])
        canonical_bucket["count"] = 1
        historical_bucket = deepcopy(canonical_bucket)
        historical_bucket["reserved_cny"] = "1.5"
        budget["attempt_evidence"]["run"] = [
            deepcopy(canonical_bucket),
            deepcopy(canonical_bucket),
        ]
        budget["attempt_evidence"]["cumulative"] = [
            deepcopy(canonical_bucket),
            historical_bucket,
        ]
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "0.000024"
            budget[scope]["settled_cny"] = "0.000024"
            budget[scope]["remaining_execution_cny"] = "17.999976"
            budget[scope]["attempt_count"] = 2

    with pytest.raises(ValueError, match="attempt|bucket|offline|budget"):
        BudgetSummary.model_validate(deepcopy(budget))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_attempts", True),
        ("provider_attempts", "1"),
        ("usage.prompt_tokens", "8"),
        ("usage.completion_tokens", False),
    ],
)
def test_model_call_evidence_rejects_coerced_usage_and_attempt_types(
    field: str,
    value: object,
) -> None:
    payload = asdict(
        _model_call(
            case_id="strict-paid-evidence",
            trial=1,
        )
    )
    if field == "provider_attempts":
        payload[field] = value
    else:
        _, usage_field = field.split(".", maxsplit=1)
        payload["usage"][usage_field] = value

    with pytest.raises(ValueError, match="usage|provider|attempt|integer"):
        ModelCallRecord.model_validate(payload)


def test_dev_repeat_live_ledger_accepts_matching_temporary_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    install_matching_ledger_for_paid_payload(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        payload=payload,
    )
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "require_persistent_budget_matches_trusted_ledger",
        _REAL_PAID_LEDGER_REQUIRE,
    )

    _REAL_PAID_LEDGER_REQUIRE(
        budget=BudgetSummary.model_validate(
            payload["summary"]["budget"]
        ),
        label="dev_repeat",
    )


def test_dev_repeat_live_ledger_rejects_missing_trusted_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    missing = tmp_path / "missing-private" / "budget.sqlite3"
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "DEFAULT_BUDGET_LEDGER",
        missing,
        raising=False,
    )
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "require_persistent_budget_matches_trusted_ledger",
        _REAL_PAID_LEDGER_REQUIRE,
    )

    with pytest.raises(ValueError, match="ledger|trusted|persistent"):
        _REAL_PAID_LEDGER_REQUIRE(
            budget=BudgetSummary.model_validate(
                payload["summary"]["budget"]
            ),
            label="dev_repeat",
        )


def test_dev_repeat_live_ledger_rejects_tampered_settled_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    install_matching_ledger_for_paid_payload(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        payload=payload,
    )
    for scope in ("run", "cumulative"):
        payload["summary"]["budget"][scope]["settled_cny"] = "0.000001"
        payload["summary"]["budget"][scope]["committed_cny"] = "0.000001"
    monkeypatch.setattr(
        paid_ledger_binding_module,
        "require_persistent_budget_matches_trusted_ledger",
        _REAL_PAID_LEDGER_REQUIRE,
    )

    with pytest.raises(ValueError, match="ledger|trusted|persistent|budget"):
        _REAL_PAID_LEDGER_REQUIRE(
            budget=BudgetSummary.model_validate(
                payload["summary"]["budget"]
            ),
            label="dev_repeat",
        )
