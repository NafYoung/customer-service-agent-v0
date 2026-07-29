from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.agent.openai_compatible import AssistantTurn
from evals import run_semantic_judge_calibration as calibration_cli
from evals.calibration_attestation import validate_calibration_attestation
from evals.canonical_pricing import canonical_budget_price_payload
from evals.semantic_calibration import load_calibration_fixtures


class _ClosedBudgetGuard:
    def __init__(self, attempt_count: int):
        self.attempt_count = attempt_count
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def snapshot(self) -> dict[str, object]:
        assert self.closed is True
        price = canonical_budget_price_payload()
        settled = Decimal(self.attempt_count) * Decimal("0.00002")
        settled_cny = format(settled, "f")
        amounts = {
            "currency": "CNY",
            "hard_limit_cny": "20",
            "execution_limit_cny": "18",
            "committed_cny": settled_cny,
            "settled_cny": settled_cny,
            "remaining_execution_cny": format(
                Decimal("18") - settled,
                "f",
            ),
            "attempt_count": self.attempt_count,
            "reserved_count": 0,
            "uncertain_count": 0,
        }
        return {
            "schema_version": "1.0",
            "enforcement_mode": "persistent_sqlite",
            "run_status": "completed",
            "run_identity": {
                "run_id": "eval-20260729-calibration-cli",
                "purpose": "semantic_judge_calibration",
                "model": "deepseek-v4-flash",
                "price_sha256": price["snapshot_sha256"],
                "status": "completed",
                "started_at": "2026-07-29T12:00:00+00:00",
                "completed_at": "2026-07-29T12:05:00+00:00",
            },
            "price": price,
            "reservation_cny_per_attempt": "1.002048",
            "run": dict(amounts),
            "cumulative": dict(amounts),
            "attempt_evidence": {
                "run": [
                    {
                        "status": "settled_upper_bound",
                        "settlement_mode": "upper_bound",
                        "reserved_cny": "1.002048",
                        "known_cost_cny": "0.00002",
                        "count": self.attempt_count,
                    }
                ],
                "cumulative": [
                    {
                        "status": "settled_upper_bound",
                        "settlement_mode": "upper_bound",
                        "reserved_cny": "1.002048",
                        "known_cost_cny": "0.00002",
                        "count": self.attempt_count,
                    }
                ],
            },
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
        contradiction_evidence = []
        if fixture.expected_material_self_contradiction:
            contradiction_evidence = [
                fixture.contradiction_evidence_sides[0][0],
                fixture.contradiction_evidence_sides[1][0],
            ]

        def evidence_spans(
            claim_id: str,
            relation: str,
        ) -> list[str]:
            if relation == "not_mentioned":
                return []
            regions = fixture.acceptable_evidence_regions[claim_id]
            if relation == "both_or_ambiguous":
                return [
                    next(
                        region
                        for region in regions
                        if region in side
                    )
                    for side in fixture.contradiction_evidence_sides
                ]
            return [regions[0]]

        return AssistantTurn(
            content=json.dumps(
                {
                    "claims": [
                        {
                            "id": claim_id,
                            "relation": relation,
                            "evidence_spans": evidence_spans(
                                claim_id,
                                relation,
                            ),
                        }
                        for claim_id, relation
                        in fixture.effective_expected_relations.items()
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
    clean_checks: list[str | None] = []
    monkeypatch.setattr(
        calibration_cli,
        "DEFAULT_OUTPUT_ROOT",
        tmp_path,
    )
    monkeypatch.setattr(
        calibration_cli,
        "PRIVATE_ARTIFACT_ROOT",
        tmp_path,
    )

    def require_clean_source(*, expected_commit=None):
        clean_checks.append(expected_commit)
        return "1" * 40

    monkeypatch.setattr(
        calibration_cli,
        "require_clean_git_worktree",
        require_clean_source,
        raising=False,
    )
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
    assert report["source_git_commit"] == "1" * 40
    assert clean_checks == [None, "1" * 40, "1" * 40]
    validate_calibration_attestation(
        report_path=report_path,
        settings=calibration_cli.Settings(),
        fixture_snapshot=(
            calibration_cli.freeze_readonly_harness(
                calibration_cli.Settings()
            ).calibration_fixture_snapshot
        ),
        harness_fingerprints=report["harness"],
    )


def test_holdout_eligible_calibration_rejects_custom_corpus_before_model(
    tmp_path,
    monkeypatch,
):
    called = False
    monkeypatch.setattr(
        calibration_cli,
        "DEFAULT_OUTPUT_ROOT",
        tmp_path,
    )
    monkeypatch.setattr(
        calibration_cli,
        "PRIVATE_ARTIFACT_ROOT",
        tmp_path,
    )

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


def test_holdout_eligible_calibration_checks_clean_git_before_budget(
    tmp_path,
    monkeypatch,
):
    budget_called = False
    monkeypatch.setattr(
        calibration_cli,
        "DEFAULT_OUTPUT_ROOT",
        tmp_path,
    )
    monkeypatch.setattr(
        calibration_cli,
        "PRIVATE_ARTIFACT_ROOT",
        tmp_path,
    )

    def reject_dirty_source(**kwargs):
        del kwargs
        raise ValueError("clean Git worktree required")

    def fail_budget(**kwargs):
        nonlocal budget_called
        del kwargs
        budget_called = True
        raise AssertionError("budget must not be created")

    monkeypatch.setattr(
        calibration_cli,
        "require_clean_git_worktree",
        reject_dirty_source,
        raising=False,
    )
    monkeypatch.setattr(
        calibration_cli,
        "build_deepseek_budget_guard",
        fail_budget,
    )

    exit_code = calibration_cli.main(
        [
            "--output-root",
            str(tmp_path),
            "--run-id",
            "eval-20260729-dirty-calibration",
        ]
    )

    assert exit_code == 2
    assert budget_called is False
