from __future__ import annotations

import json
from pathlib import Path

from app.agent.openai_compatible import AssistantTurn
from evals.calibration_attestation import validate_calibration_attestation
from evals.semantic_calibration import load_calibration_fixtures
from evals import run_semantic_judge_calibration as calibration_cli


class _ClosedBudgetGuard:
    def __init__(self, attempt_count: int):
        self.attempt_count = attempt_count
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def snapshot(self) -> dict[str, object]:
        assert self.closed is True
        amounts = {
            "currency": "CNY",
            "hard_limit_cny": "20",
            "execution_limit_cny": "18",
            "committed_cny": "0.01",
            "settled_cny": "0.01",
            "remaining_execution_cny": "17.99",
            "attempt_count": self.attempt_count,
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
                "snapshot_sha256": "d" * 64,
                "source_url": (
                    "https://api-docs.deepseek.com/quick_start/pricing"
                ),
                "usage_source_url": (
                    "https://api-docs.deepseek.com/api/"
                    "create-chat-completion/"
                ),
                "captured_at": "2026-07-29T08:58:58+00:00",
                "valid_until": "2026-07-30T08:58:58+00:00",
                "rates_cny": {},
                "tokens_per_price_unit": 1_000_000,
            },
            "reservation_cny_per_attempt": "0.01",
            "run": dict(amounts),
            "cumulative": dict(amounts),
        }


class _CanonicalCalibrationModel:
    def __init__(self, budget_guard: _ClosedBudgetGuard):
        fixtures = load_calibration_fixtures(
            Path("evals/semantic_judge_calibration_cases.jsonl")
        )
        self.fixture_by_answer = {
            fixture.assistant_answer: fixture
            for fixture in fixtures
        }
        self.budget_guard = budget_guard
        self.closed = False

    def complete_json(self, *, messages):
        request = json.loads(messages[1]["content"])
        answer = request["assistant_answer"]
        fixture = self.fixture_by_answer[answer]
        grounded_span = answer[: min(300, len(answer))]
        contradiction_evidence = []
        if fixture.expected_material_self_contradiction:
            contradiction_evidence = [answer[0], answer[-1]]
        return AssistantTurn(
            content=json.dumps(
                {
                    "claims": [
                        {
                            "id": claim_id,
                            "relation": relation,
                            "evidence_spans": (
                                []
                                if relation == "not_mentioned"
                                else [grounded_span]
                            ),
                        }
                        for claim_id, relation
                        in fixture.expected_relations.items()
                    ],
                    "material_self_contradiction": (
                        fixture.expected_material_self_contradiction
                    ),
                    "contradiction_evidence": contradiction_evidence,
                },
                ensure_ascii=False,
            ),
            tool_calls=(),
            finish_reason="stop",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            model="deepseek-v4-flash",
            provider_attempts=1,
        )

    def close(self) -> None:
        self.closed = True
        self.budget_guard.close()


def test_holdout_eligible_calibration_writes_a_validated_closed_report(
    tmp_path,
    monkeypatch,
):
    fixtures = load_calibration_fixtures(
        Path("evals/semantic_judge_calibration_cases.jsonl")
    )
    budget_guard = _ClosedBudgetGuard(len(fixtures))
    model = _CanonicalCalibrationModel(budget_guard)
    monkeypatch.setattr(
        calibration_cli,
        "build_deepseek_budget_guard",
        lambda **kwargs: budget_guard,
    )
    monkeypatch.setattr(
        calibration_cli,
        "build_deepseek_client",
        lambda settings, *, budget_guard: model,
    )

    exit_code = calibration_cli.main(
        [
            "--output-root",
            str(tmp_path),
            "--run-id",
            "eval-20260729-calibration-cli",
        ]
    )

    assert exit_code == 0
    assert model.closed is True
    report_path = tmp_path / "eval-20260729-calibration-cli.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "2.0"
    assert (
        report["attestation_kind"]
        == "semantic_judge_holdout_eligibility"
    )
    validate_calibration_attestation(
        report_path=report_path,
        settings=calibration_cli.Settings(),
    )


def test_holdout_eligible_calibration_rejects_custom_corpus_before_model(
    tmp_path,
    monkeypatch,
):
    called = False

    def fail_if_called(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("budget must not be created")

    monkeypatch.setattr(
        calibration_cli,
        "build_deepseek_budget_guard",
        fail_if_called,
    )

    exit_code = calibration_cli.main(
        [
            "--mode",
            "holdout_eligible",
            "--fixture-path",
            str(tmp_path / "custom.jsonl"),
            "--output-root",
            str(tmp_path),
            "--run-id",
            "eval-20260729-custom-corpus",
        ]
    )

    assert exit_code == 2
    assert called is False
