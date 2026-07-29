from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.openai_compatible import AssistantTurn
from evals import run_readonly_agent_evals
from evals import verify_eval_bundle as verify_eval_bundle_cli
from evals.evidence import verify_eval_bundle
from evals.evidence_schema import (
    validate_readonly_bundle,
    validate_readonly_payload,
)

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
        )

    def close(self):
        self.closed = True


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


def test_cli_writes_verified_machine_readable_bundle_without_paid_api(
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
                "expected": {
                    "forbidden_tools": [
                        "prepare_cancel_order",
                        "prepare_return",
                        "prepare_exchange",
                        "create_handoff_ticket",
                        "execute_prepared_action",
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
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
            str(case_dir),
            "--output-root",
            str(output_root),
            "--run-id",
            "eval-20260729-offline12",
            "--purpose",
            "diagnostic",
            "--split",
            "dev",
            "--case-set-name",
            "offline-test-v1",
            "--trials",
            "2",
        ]
    )

    assert exit_code == 0
    assert model.closed is True
    assert budget_guard.closed is True
    bundle_path = output_root / "eval-20260729-offline12"
    verified = verify_eval_bundle(bundle_path)
    validated = validate_readonly_bundle(bundle_path)
    assert verified["manifest"]["status"] == "completed"
    assert validated.manifest.status == "completed"
    assert verified["manifest"]["execution"]["planned_trials"] == 2
    assert verified["manifest"]["execution"]["completed_trials"] == 2
    assert verified["manifest"]["model"]["observed_models"] == [
        "offline-eval-model"
    ]
    assert len(verified["cases"]) == 2
    assert len(verified["trajectories"]) == 2
    assert verified["summary"]["strict"]["passed"] == 2
    assert verified["summary"]["reliability"]["pass_power_k"] == 1.0
    assert verified["summary"]["usage"]["total_tokens"] == 32
    assert verified["summary"]["business_state"]["all_trials_unchanged"] is True
    assert (
        verified["manifest"]["budget"]["enforcement_mode"]
        == "offline_no_paid_provider"
    )
    assert verified["summary"]["budget"]["run"]["committed_cny"] == "0"

    output = capsys.readouterr().out
    assert "2/2 read-only Agent trials passed" in output
    assert str(bundle_path) in output

    invalid_payload = deepcopy(verified)
    invalid_payload["manifest"].pop("model")
    with pytest.raises(ValidationError):
        validate_readonly_payload(invalid_payload)

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
