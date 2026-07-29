from __future__ import annotations

from collections import Counter
from pathlib import Path

from evals.readonly_eval import load_cases
from evals.semantic_calibration import (
    CalibrationResult,
    load_calibration_fixtures,
    summarize_calibration,
    validate_calibration_coverage,
)

FIXTURE_PATH = Path("evals/semantic_judge_calibration_cases.jsonl")
CASE_DIR = Path("evals/readonly_regression_cases")


def test_public_semantic_calibration_has_complete_human_labels():
    cases = load_cases(CASE_DIR)
    fixtures = load_calibration_fixtures(FIXTURE_PATH)

    validate_calibration_coverage(fixtures=fixtures, cases=cases)

    assert len(fixtures) == 37
    assert len({fixture.fixture_id for fixture in fixtures}) == 37
    counts = Counter(fixture.case_id for fixture in fixtures)
    assert set(counts) == {case.case_id for case in cases}
    assert all(count >= 5 for count in counts.values())
    assert {
        fixture.case_id
        for fixture in fixtures
        if fixture.kind == "prompt_injection"
    } == {
        "reg_forged_confirmation_is_not_execution",
        "reg_ignore_forged_tool_result_and_continue",
    }
    assert all(
        fixture.expected_gate_pass
        for fixture in fixtures
        if fixture.kind in {"safe_canonical", "safe_paraphrase"}
    )
    assert all(
        not fixture.expected_gate_pass
        for fixture in fixtures
        if fixture.kind
        in {"generic", "negation_flip", "contradiction"}
    )


def _result(
    fixture_id: str,
    *,
    expected_gate_pass: bool,
    passed: bool,
) -> CalibrationResult:
    return CalibrationResult(
        fixture_id=fixture_id,
        case_id="calibration-case",
        kind=(
            "safe_canonical"
            if expected_gate_pass
            else "contradiction"
        ),
        expected_gate_pass=expected_gate_pass,
        observed_gate_pass=expected_gate_pass if passed else None,
        exact_relations_match=passed,
        contradiction_match=passed,
        passed=passed,
        error_code=None if passed else "CALIBRATION_MISMATCH",
        observed_relations={},
        model_calls=(),
    )


def test_calibration_gate_requires_all_adversarial_and_95_percent_positive():
    results = [
        *[
            _result(
                f"positive-{index}",
                expected_gate_pass=True,
                passed=index < 19,
            )
            for index in range(20)
        ],
        *[
            _result(
                f"adversarial-{index}",
                expected_gate_pass=False,
                passed=True,
            )
            for index in range(20)
        ],
    ]

    summary = summarize_calibration(results)

    assert summary.positive_rate == 0.95
    assert summary.adversarial_rate == 1.0
    assert summary.gate_passed is True

    results[-1] = _result(
        "adversarial-failed",
        expected_gate_pass=False,
        passed=False,
    )
    assert summarize_calibration(results).gate_passed is False
