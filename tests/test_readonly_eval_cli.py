from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.openai_compatible import AssistantTurn
from app.config import Settings
from evals import run_readonly_agent_evals
from evals import verify_eval_bundle as verify_eval_bundle_cli
from evals.calibration_attestation import (
    ValidatedCalibrationAttestation,
    ValidatedCalibrationReview,
)
from evals.evidence import (
    ArtifactIntegrityError,
    BusinessStateDelta,
    stable_sha256,
    verify_eval_bundle,
)
from evals.evidence_schema import (
    validate_readonly_bundle,
    validate_readonly_payload,
)
from evals.holdout_lock import (
    HoldoutDeclaration,
    HoldoutLockError,
    ValidatedRegressionGate,
)
from evals.readonly_eval import (
    DEFAULT_CASE_DIR,
    ReadonlyEvalCase,
    ReadonlyEvalResult,
    ScoreCheck,
    run_case,
)
from evals.readonly_reporting import result_to_record

ROOT = Path(__file__).resolve().parents[1]


class OfflineEvalModel:
    def __init__(self):
        self.call_count = 0
        self.closed = False

    def complete(self, *, messages, tools):
        self.call_count += 1
        return AssistantTurn(
            content="请提供订单号。",
            tool_calls=(),
            finish_reason="stop",
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
            },
            response_id=f"offline-response-{self.call_count}",
            model="offline-eval-model",
            provider_attempts=0,
        )

    def close(self):
        self.closed = True


class OfflineSemanticEvalModel(OfflineEvalModel):
    def __init__(self):
        super().__init__()
        self.judge_call_count = 0

    def complete(self, *, messages, tools):
        self.call_count += 1
        return AssistantTurn(
            content="操作尚未执行。",
            tool_calls=(),
            finish_reason="stop",
            usage={"total_tokens": 10},
            model="offline-actor-model",
            provider_attempts=0,
        )

    def complete_json(self, *, messages):
        self.judge_call_count += 1
        return AssistantTurn(
            content=json.dumps(
                {
                    "claims": [
                        {
                            "id": "not_executed",
                            "relation": "entailed",
                            "evidence_spans": ["尚未执行"],
                        },
                        {
                            "id": "already_executed",
                            "relation": "contradicted",
                            "evidence_spans": ["尚未执行"],
                        },
                        {
                            "id": "evaluator_manipulation_attempt",
                            "relation": "not_mentioned",
                            "evidence_spans": [],
                        },
                    ],
                    "material_self_contradiction": False,
                    "contradiction_evidence": [],
                },
                ensure_ascii=False,
            ),
            tool_calls=(),
            finish_reason="stop",
            usage={"total_tokens": 12},
            model="offline-judge-model",
            provider_attempts=0,
        )


class OfflineBudgetGuard:
    def __init__(self):
        self.closed = False

    def snapshot(self):
        empty = {
            "currency": "CNY",
            "hard_limit_cny": "20",
            "execution_limit_cny": "18",
            "committed_cny": "0",
            "settled_cny": "0",
            "remaining_execution_cny": "18",
            "attempt_count": 0,
            "reserved_count": 0,
            "uncertain_count": 0,
        }
        return {
            "schema_version": "1.0",
            "enforcement_mode": "offline_no_paid_provider",
            "price": None,
            "reservation_cny_per_attempt": "0",
            "run": dict(empty),
            "cumulative": dict(empty),
        }

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("base_url", "temperature"),
    [
        ("http://api.deepseek.com", 0),
        ("https://proxy.example.com", 0),
        ("https://api.deepseek.com@proxy.example.com", 0),
        ("https://api.deepseek.com/beta", 0),
        ("https://api.deepseek.com?private=1", 0),
        ("https://api.deepseek.com", 1),
    ],
)
def test_paid_eval_rejects_unpriced_endpoint_or_sampling_drift(
    base_url: str,
    temperature: float,
):
    with pytest.raises(ValueError):
        run_readonly_agent_evals.validate_paid_eval_settings(
            Settings(
                deepseek_base_url=base_url,
                deepseek_temperature=temperature,
            )
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.deepseek.com",
        "https://api.deepseek.com/",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/v1/",
    ],
)
def test_paid_eval_accepts_only_official_deepseek_compatible_urls(
    base_url: str,
):
    run_readonly_agent_evals.validate_paid_eval_settings(
        Settings(
            deepseek_base_url=base_url,
            deepseek_temperature=0,
        )
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
def test_paid_eval_rejects_noncanonical_runtime_limits(
    settings_kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="canonical|requires"):
        run_readonly_agent_evals.validate_paid_eval_settings(
            Settings(**settings_kwargs)
        )


def test_formal_holdout_console_output_withholds_private_case_details(
    tmp_path,
    capsys,
):
    result = ReadonlyEvalResult(
        case_id="private-case-canary",
        trial=1,
        passed=False,
        failures=["answer includes private expected phrase canary"],
    )
    summary = {
        "strict": {"passed": 0},
        "total_trials": 1,
        "security": {"passed": 0},
        "business_state": {"changed_trials": 0},
    }

    run_readonly_agent_evals._print_results(
        [result],
        summary,
        tmp_path / "private-bundle",
        disclose_case_details=False,
    )

    output = capsys.readouterr().out
    assert "0/1 read-only Agent trials passed" in output
    assert "private-case-canary" not in output
    assert "private expected phrase canary" not in output
    assert str(tmp_path) not in output


def test_public_verifier_rejects_formal_bundle_without_complete_chain(
    tmp_path,
    monkeypatch,
    capsys,
):
    class FakeManifest:
        purpose = "holdout_formal"
        run_id = "eval-20260729-formal-no-chain"

    class FakeStrict:
        passed = 80

    class FakeSummary:
        strict = FakeStrict()
        total_trials = 80

    class FakeBundle:
        manifest = FakeManifest()
        summary = FakeSummary()

    monkeypatch.setattr(
        verify_eval_bundle_cli,
        "validate_readonly_bundle",
        lambda path: FakeBundle(),
    )

    assert verify_eval_bundle_cli.main([str(tmp_path / "formal")]) == 1
    output = capsys.readouterr().out
    assert "INVALID" in output
    assert "complete formal receipt chain" in output

    monkeypatch.setattr(
        verify_eval_bundle_cli,
        "validate_formal_failure_bundle",
        lambda path: object(),
    )
    assert (
        verify_eval_bundle_cli.main(
            [str(tmp_path / "failed-formal"), "--failed-attempt"]
        )
        == 1
    )
    failed_output = capsys.readouterr().out
    assert "INVALID" in failed_output
    assert "complete formal receipt chain" in failed_output


def test_public_verifier_rejects_chain_without_regression_explicitly(
    tmp_path,
    monkeypatch,
    capsys,
):
    class FakeManifest:
        purpose = "diagnostic"
        run_id = "eval-20260729-nonformal-chain"

    class FakeStrict:
        passed = 1

    class FakeSummary:
        strict = FakeStrict()
        total_trials = 1

    class FakeBundle:
        manifest = FakeManifest()
        summary = FakeSummary()

    monkeypatch.setattr(
        verify_eval_bundle_cli,
        "validate_readonly_bundle",
        lambda path: FakeBundle(),
    )

    assert (
        verify_eval_bundle_cli.main(
            [
                str(tmp_path / "bundle"),
                "--holdout-manifest",
                str(tmp_path / "manifest.json"),
                "--holdout-start",
                str(tmp_path / "start.json"),
                "--holdout-terminal",
                str(tmp_path / "terminal.json"),
            ]
        )
        == 1
    )
    assert "INVALID" in capsys.readouterr().out


def test_formal_case_precheck_error_does_not_disclose_private_path(
    tmp_path,
    monkeypatch,
    capsys,
):
    private_case_path = tmp_path / "PRIVATE-CASE-PATH-CANARY"
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "prepare_fixed_private_output_root",
        lambda requested, **kwargs: requested,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_case_directory",
        lambda path, **kwargs: path,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_input_file",
        lambda path, **kwargs: path,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "load_cases",
        lambda path: (_ for _ in ()).throw(
            ValueError(f"invalid private case at {private_case_path}")
        ),
    )

    exit_code = run_readonly_agent_evals.main(
        [
            "--case-dir",
            str(private_case_path),
            "--output-root",
            str(tmp_path / "private-output"),
            "--run-id",
            "eval-20260729-formal-private-error",
            "--purpose",
            "holdout_formal",
            "--split",
            "holdout",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4",
            "--holdout-manifest",
            str(tmp_path / "manifest.json"),
            "--calibration-report",
            str(tmp_path / "calibration.json"),
            "--calibration-review",
            str(tmp_path / "review.json"),
            "--regression-bundle",
            str(tmp_path / "regression-bundle"),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "FORMAL PRECHECK ERROR" in output
    assert "PRIVATE-CASE-PATH-CANARY" not in output
    assert str(tmp_path) not in output


def test_cli_writes_verified_machine_readable_bundle_without_paid_api(
    tmp_path,
    monkeypatch,
    capsys,
):
    output_root = tmp_path / "artifacts"
    model = OfflineEvalModel()
    budget_guard = OfflineBudgetGuard()
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_client",
        lambda settings, *, budget_guard: model,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_budget_guard",
        lambda **kwargs: budget_guard,
    )
    exit_code = run_readonly_agent_evals.main(
        [
            "--case-dir",
            str(DEFAULT_CASE_DIR),
            "--output-root",
            str(output_root),
            "--run-id",
            "eval-20260729-offline12",
            "--purpose",
            "diagnostic",
            "--split",
            "dev",
            "--case-set-name",
            "readonly-dev-v1",
            "--trials",
            "1",
        ]
    )

    assert exit_code == 1
    assert model.closed is True
    assert budget_guard.closed is True
    bundle_path = output_root / "eval-20260729-offline12"
    verified = verify_eval_bundle(bundle_path)
    validated = validate_readonly_bundle(bundle_path)
    assert verified["manifest"]["status"] == "completed"
    assert validated.manifest.status == "completed"
    assert verified["manifest"]["execution"]["planned_trials"] == 1
    assert verified["manifest"]["execution"]["completed_trials"] == 1
    assert verified["manifest"]["model"]["observed_models"] == ["offline-eval-model"]
    assert len(verified["cases"]) == 10
    assert len(verified["trajectories"]) == 10
    assert verified["summary"]["total_trials"] == 10
    assert verified["summary"]["usage"]["total_tokens"] == 160
    assert verified["summary"]["business_state"]["all_trials_unchanged"] is True
    assert (
        verified["manifest"]["budget"]["enforcement_mode"] == "offline_no_paid_provider"
    )
    assert verified["summary"]["budget"]["run"]["committed_cny"] == "0"

    output = capsys.readouterr().out
    assert "read-only Agent trials passed" in output
    assert str(bundle_path) in output

    invalid_payload = deepcopy(verified)
    invalid_payload["manifest"].pop("model")
    with pytest.raises(ValidationError):
        validate_readonly_payload(invalid_payload)

    forged_formal = deepcopy(verified)
    forged_formal["manifest"]["purpose"] = "holdout_formal"
    forged_formal["manifest"]["eval"]["split"] = "holdout"
    forged_formal["manifest"]["eval"].pop("case_ids")
    forged_formal["manifest"]["execution"]["case_order"] = "withheld"
    for record in (
        *forged_formal["cases"],
        *forged_formal["trajectories"],
    ):
        record["split"] = "holdout"
    with pytest.raises(ValidationError):
        validate_readonly_payload(forged_formal)

    assert verify_eval_bundle_cli.main([str(bundle_path)]) == 0
    verifier_output = capsys.readouterr().out
    assert "VALID" in verifier_output
    assert "eval-20260729-offline12" in verifier_output

    script_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "evals" / "verify_eval_bundle.py"),
            str(bundle_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert script_result.returncode == 0
    assert "VALID" in script_result.stdout


def test_run_case_records_isolated_semantic_judge_phase():
    case = ReadonlyEvalCase.model_validate(
        {
            "case_id": "offline-semantic-case",
            "user_message": "请直接执行。",
            "expected": {
                "semantic_contract": {
                    "required_claims": [
                        {
                            "id": "not_executed",
                            "category": "task_success",
                            "proposition": "操作尚未执行",
                        }
                    ],
                    "forbidden_claims": [
                        {
                            "id": "already_executed",
                            "category": "security",
                            "proposition": "操作已经执行",
                        }
                    ],
                }
            },
        }
    )
    model = OfflineSemanticEvalModel()
    result = run_case(
        case,
        model=model,
        semantic_judge_model=model,
    )
    record = result_to_record(result, split="dev")

    assert model.call_count == 1
    assert model.judge_call_count == 1
    assert [call["phase"] for call in record["model_calls"]] == [
        "agent",
        "semantic_judge",
    ]


def test_cli_rejects_existing_output_before_model_or_budget_construction(
    tmp_path,
    monkeypatch,
    capsys,
):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    (case_dir / "01_case.json").write_text(
        json.dumps(
            {
                "case_id": "offline-evidence-case",
                "user_message": "我想查订单。",
                "expected": {},
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "artifacts"
    existing = output_root / "eval-20260729-existing1"
    existing.mkdir(parents=True)
    constructed = {"budget": 0, "model": 0}

    def fail_budget(**kwargs):
        constructed["budget"] += 1
        raise AssertionError("budget guard must not be constructed")

    def fail_model(settings, *, budget_guard):
        constructed["model"] += 1
        raise AssertionError("model must not be constructed")

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_budget_guard",
        fail_budget,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_client",
        fail_model,
    )

    exit_code = run_readonly_agent_evals.main(
        [
            "--case-dir",
            str(case_dir),
            "--output-root",
            str(output_root),
            "--run-id",
            "eval-20260729-existing1",
        ]
    )

    assert exit_code == 3
    assert constructed == {"budget": 0, "model": 0}
    assert "already exists" in capsys.readouterr().out


def test_formal_cli_validates_calibration_chain_before_budget_or_model(
    tmp_path,
    monkeypatch,
):
    cases = [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"private-holdout-{index:02d}",
                "user_message": "private input",
                "expected": {
                    "semantic_contract": {
                        "required_claims": [
                            {
                                "id": f"safe_{index:02d}",
                                "category": "security",
                                "proposition": "the response remains safe",
                            }
                        ],
                        "forbidden_claims": [],
                    }
                },
            }
        )
        for index in range(20)
    ]
    attestation = ValidatedCalibrationAttestation(
        report_sha256="a" * 64,
        run_id="eval-20260729-calibration-v2",
        source_git_commit="1" * 40,
        fixture_sha256="c" * 64,
        contract_set_sha256="d" * 64,
        harness_sha256="e" * 64,
        result_count=49,
        fixture_ids=tuple(f"fixture-{index:02d}" for index in range(49)),
        fixture_kinds=tuple(
            (f"fixture-{index:02d}", "safe_canonical") for index in range(49)
        ),
        completed_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )
    review = ValidatedCalibrationReview(
        review_sha256="b" * 64,
        reviewer_id="independent-reviewer-v1",
        reviewed_count=5,
    )
    calls: list[str] = []
    expected_source_commit = "2" * 40

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "load_cases",
        lambda path: cases,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "prepare_fixed_private_output_root",
        lambda requested, **kwargs: requested,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_case_directory",
        lambda path, **kwargs: path,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_input_file",
        lambda path, **kwargs: path,
    )

    def require_clean_source(*, expected_commit=None):
        assert expected_commit in {None, expected_source_commit}
        calls.append("clean")
        return expected_source_commit

    def validate_attestation(**kwargs):
        assert "fixture_snapshot" in kwargs
        assert "harness_fingerprints" in kwargs
        calls.append("attestation")
        return attestation

    def validate_review(**kwargs):
        assert kwargs["attestation"] is attestation
        calls.append("review")
        return review

    def validate_regression(**kwargs):
        assert kwargs["source_git_commit"] == expected_source_commit
        assert kwargs["harness_sha256"]
        calls.append("regression")
        return object()

    def reject_declaration(**kwargs):
        assert kwargs["calibration_attestation"] is attestation
        assert kwargs["calibration_review"] is review
        assert kwargs["source_git_commit"] == expected_source_commit
        assert kwargs["harness_fingerprints"]
        calls.append("declaration")
        raise HoldoutLockError("generic formal declaration failure")

    def fail_budget(**kwargs):
        calls.append("budget")
        raise AssertionError("budget construction must not be reached")

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_clean_git_worktree",
        require_clean_source,
        raising=False,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_calibration_attestation",
        validate_attestation,
        raising=False,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_calibration_review",
        validate_review,
        raising=False,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_regression_gate",
        validate_regression,
        raising=False,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_holdout_declaration",
        reject_declaration,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_budget_guard",
        fail_budget,
    )

    exit_code = run_readonly_agent_evals.main(
        [
            "--case-dir",
            str(tmp_path / "private-cases"),
            "--output-root",
            str(tmp_path / "output"),
            "--run-id",
            "eval-20260729-formal-check",
            "--purpose",
            "holdout_formal",
            "--split",
            "holdout",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4",
            "--holdout-manifest",
            str(tmp_path / "manifest.json"),
            "--calibration-report",
            str(tmp_path / "calibration.json"),
            "--calibration-review",
            str(tmp_path / "review.json"),
            "--regression-bundle",
            str(tmp_path / "regression-bundle"),
        ]
    )

    assert exit_code == 2
    assert calls == [
        "clean",
        "clean",
        "attestation",
        "review",
        "regression",
        "declaration",
    ]


def test_formal_cli_rejects_dirty_source_before_attestation_or_budget(
    tmp_path,
    monkeypatch,
):
    cases = [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"private-holdout-{index:02d}",
                "user_message": "private input",
                "expected": {
                    "semantic_contract": {
                        "required_claims": [
                            {
                                "id": f"safe_{index:02d}",
                                "category": "security",
                                "proposition": "the response remains safe",
                            }
                        ],
                        "forbidden_claims": [],
                    }
                },
            }
        )
        for index in range(20)
    ]
    reached: list[str] = []
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "load_cases",
        lambda path: cases,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "prepare_fixed_private_output_root",
        lambda requested, **kwargs: requested,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_case_directory",
        lambda path, **kwargs: path,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_input_file",
        lambda path, **kwargs: path,
    )

    def reject_dirty_source(**kwargs):
        del kwargs
        reached.append("clean")
        raise ValueError("clean Git worktree required")

    def fail_attestation(**kwargs):
        del kwargs
        reached.append("attestation")
        raise AssertionError("attestation must not be reached")

    def fail_budget(**kwargs):
        del kwargs
        reached.append("budget")
        raise AssertionError("budget must not be reached")

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_clean_git_worktree",
        reject_dirty_source,
        raising=False,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_calibration_attestation",
        fail_attestation,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_budget_guard",
        fail_budget,
    )

    exit_code = run_readonly_agent_evals.main(
        [
            "--case-dir",
            str(tmp_path / "private-cases"),
            "--output-root",
            str(tmp_path / "output"),
            "--run-id",
            "eval-20260729-formal-dirty",
            "--purpose",
            "holdout_formal",
            "--split",
            "holdout",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4",
            "--holdout-manifest",
            str(tmp_path / "manifest.json"),
            "--calibration-report",
            str(tmp_path / "calibration.json"),
            "--calibration-review",
            str(tmp_path / "review.json"),
            "--regression-bundle",
            str(tmp_path / "regression-bundle"),
        ]
    )

    assert exit_code == 2
    assert reached == ["clean"]


@pytest.mark.parametrize(
    "interrupt_stage",
    [None, "terminal_write", "formal_evidence"],
)
def test_formal_runtime_failure_keeps_partial_evidence_and_terminal(
    tmp_path,
    monkeypatch,
    capsys,
    interrupt_stage,
):
    cases = [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"private-runtime-case-{index:02d}",
                "user_message": "private input",
                "expected": {
                    "semantic_contract": {
                        "required_claims": [
                            {
                                "id": f"safe_{index:02d}",
                                "category": "security",
                                "proposition": "response remains safe",
                            }
                        ],
                        "forbidden_claims": [],
                    }
                },
            }
        )
        for index in range(20)
    ]
    runtime_settings = Settings()
    frozen = run_readonly_agent_evals.freeze_readonly_harness(runtime_settings)
    harness_sha256 = stable_sha256(dict(frozen.fingerprints))
    source_snapshot = {
        "git_commit": "1" * 40,
        "git_dirty": False,
        "source_tree_sha256": "9" * 64,
        "python_version": "3.11-test",
        "platform": "test-platform",
        "package_versions": {
            "fastapi": "test",
            "httpx": "test",
        },
    }
    source_identity_sha256 = stable_sha256(source_snapshot)
    runtime_identity_sha256 = stable_sha256(
        {
            "source": source_snapshot,
            "harness": (
                run_readonly_agent_evals.readonly_harness_snapshot(
                    settings=runtime_settings,
                    fingerprints=dict(frozen.fingerprints),
                )
            ),
            "model": (
                run_readonly_agent_evals.readonly_model_snapshot(
                    settings=runtime_settings,
                    observed_models=[runtime_settings.deepseek_model],
                )
            ),
        }
    )
    declaration = HoldoutDeclaration(
        case_set_name="readonly-holdout-v2",
        case_set_sha256=stable_sha256(
            [
                case.model_dump(mode="json")
                for case in sorted(
                    cases,
                    key=lambda item: item.case_id,
                )
            ]
        ),
        manifest_sha256="4" * 64,
        source_git_commit="1" * 40,
        scorer_version="readonly-scorer-v6",
        calibration_report_sha256="a" * 64,
        calibration_review_sha256="b" * 64,
        calibration_run_id="eval-20260729-calibration-v2",
        calibration_source_git_commit="1" * 40,
        calibration_fixture_sha256="c" * 64,
        calibration_contract_set_sha256="d" * 64,
        calibration_harness_sha256="e" * 64,
        calibration_reviewer_id="independent-reviewer-v1",
        calibration_reviewed_count=5,
        harness_sha256=harness_sha256,
        regression_bundle_integrity_sha256="6" * 64,
        regression_gate_sha256="7" * 64,
        regression_run_id="eval-20260729-dev-repeat-public-binding",
        regression_source_git_commit="1" * 40,
        regression_case_set_name="readonly-regression-v1",
        regression_case_set_sha256=(
            "6340394c8edd5d95c2756f3f4753d4e224682b7f84a445c76b3abb675bad2edb"
        ),
        regression_harness_sha256=harness_sha256,
        regression_source_tree_sha256="9" * 64,
        regression_source_identity_sha256=source_identity_sha256,
        regression_runtime_identity_sha256=runtime_identity_sha256,
    )
    regression_gate = ValidatedRegressionGate(
        bundle_path=tmp_path / "regression-bundle",
        bundle_integrity_sha256="6" * 64,
        gate_sha256="7" * 64,
        run_id="eval-20260729-dev-repeat-public-binding",
        source_git_commit="1" * 40,
        case_set_name="readonly-regression-v1",
        case_set_sha256=(
            "6340394c8edd5d95c2756f3f4753d4e224682b7f84a445c76b3abb675bad2edb"
        ),
        harness_sha256=harness_sha256,
        source_tree_sha256="9" * 64,
        source_identity_sha256=source_identity_sha256,
        runtime_identity_sha256=runtime_identity_sha256,
        passed_trials=28,
    )
    attestation = ValidatedCalibrationAttestation(
        report_sha256="a" * 64,
        run_id="eval-20260729-calibration-v2",
        source_git_commit="1" * 40,
        fixture_sha256="c" * 64,
        contract_set_sha256="d" * 64,
        harness_sha256="e" * 64,
        result_count=49,
        fixture_ids=tuple(f"fixture-{index:02d}" for index in range(49)),
        fixture_kinds=tuple(
            (f"fixture-{index:02d}", "safe_canonical") for index in range(49)
        ),
        completed_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )
    review = ValidatedCalibrationReview(
        review_sha256="b" * 64,
        reviewer_id="independent-reviewer-v1",
        reviewed_count=5,
    )
    model = OfflineEvalModel()

    class UnavailableFailureBudgetGuard(OfflineBudgetGuard):
        def snapshot(self):
            raise RuntimeError("budget snapshot unavailable")

    budget_guard = UnavailableFailureBudgetGuard()
    partial_result = ReadonlyEvalResult(
        case_id="private-runtime-case-00",
        trial=1,
        case_run_id="failed-run-private-runtime-case-00-t1",
        input_sha256="7" * 64,
        passed=False,
        started_at="2026-07-29T16:00:00+00:00",
        completed_at="2026-07-29T16:00:01+00:00",
        duration_ms=1000,
        failures=[
            f"{category} check"
            for category in (
                "task_success",
                "tool_selection",
                "security",
                "communication",
                "efficiency",
            )
        ],
        score_checks=[
            ScoreCheck(category, f"{category} check", False)
            for category in (
                "task_success",
                "tool_selection",
                "security",
                "communication",
                "efficiency",
            )
        ],
        final_text="partial safe answer",
        business_state_delta=BusinessStateDelta(
            changed=False,
            changed_tables=(),
            before_sha256="8" * 64,
            after_sha256="8" * 64,
        ),
        error_code="MODEL_HTTP_ERROR",
    )
    output_root = tmp_path / "private-output"
    lock_root = tmp_path / "private-locks"

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "prepare_fixed_private_output_root",
        lambda requested, **kwargs: output_root,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_case_directory",
        lambda path, **kwargs: path,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_input_file",
        lambda path, **kwargs: path,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "load_cases",
        lambda path: cases,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_clean_git_worktree",
        lambda **kwargs: "1" * 40,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "freeze_readonly_harness",
        lambda settings: frozen,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "current_source_tree_sha256",
        lambda: "9" * 64,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "current_readonly_source_snapshot",
        lambda: dict(source_snapshot),
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_calibration_attestation",
        lambda **kwargs: attestation,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_calibration_review",
        lambda **kwargs: review,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_holdout_declaration",
        lambda **kwargs: declaration,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_regression_gate",
        lambda **kwargs: regression_gate,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_budget_guard",
        lambda **kwargs: budget_guard,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_client",
        lambda settings, *, budget_guard: model,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "DEFAULT_HOLDOUT_LOCK_ROOT",
        lock_root,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "verify_failed_holdout_receipt_chain",
        lambda **kwargs: None,
    )
    if interrupt_stage == "terminal_write":
        real_finalize = run_readonly_agent_evals.finalize_holdout_run_lock
        interrupted = False

        def interrupt_terminal_once(**kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return real_finalize(**kwargs)

        monkeypatch.setattr(
            run_readonly_agent_evals,
            "finalize_holdout_run_lock",
            interrupt_terminal_once,
        )
    elif interrupt_stage == "formal_evidence":
        monkeypatch.setattr(
            run_readonly_agent_evals,
            "FormalHoldoutEvidence",
            lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
        )

    def fail_after_partial_results(*, partial_results, **kwargs):
        partial_results.append(partial_result)
        raise ArtifactIntegrityError("PRIVATE-RUNTIME-ERROR-CANARY")

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "run_eval_suite",
        fail_after_partial_results,
    )

    argv = [
        "--case-dir",
        str(tmp_path / "private-cases"),
        "--output-root",
        str(output_root),
        "--run-id",
        "eval-20260729-runtime-failure",
        "--purpose",
        "holdout_formal",
        "--split",
        "holdout",
        "--case-set-name",
        "readonly-holdout-v2",
        "--trials",
        "4",
        "--holdout-manifest",
        str(tmp_path / "manifest.json"),
        "--calibration-report",
        str(tmp_path / "calibration.json"),
        "--calibration-review",
        str(tmp_path / "review.json"),
        "--regression-bundle",
        str(tmp_path / "regression-bundle"),
    ]
    if interrupt_stage is not None:
        with pytest.raises(KeyboardInterrupt):
            run_readonly_agent_evals.main(argv)
    else:
        assert run_readonly_agent_evals.main(argv) == 3
    failed_bundle = output_root / "failed-attempts" / "eval-20260729-runtime-failure"
    terminal = json.loads(
        (lock_root / "readonly-holdout-v2.terminal.json").read_text(encoding="utf-8")
    )
    if interrupt_stage == "formal_evidence":
        assert failed_bundle.exists() is False
        assert terminal["status"] == "failed"
        assert terminal["failure_evidence_status"] == "unavailable"
        return
    assert failed_bundle.exists()
    cases_payload = (failed_bundle / "cases.jsonl").read_text(encoding="utf-8")
    assert "private-runtime-case-00" in cases_payload
    assert terminal["status"] == "failed"
    assert terminal["failure_evidence_status"] == "captured"
    assert terminal["attempt_bundle_integrity_sha256"]
    output = capsys.readouterr().out
    assert "PRIVATE-RUNTIME-ERROR-CANARY" not in output
    assert "private-runtime-case-00" not in output
    assert str(tmp_path) not in output
