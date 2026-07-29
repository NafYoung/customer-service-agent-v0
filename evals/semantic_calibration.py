from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from evals.readonly_eval import ReadonlyEvalCase
from evals.semantic_judge import (
    SemanticJsonModel,
    SemanticJudgeError,
    evaluate_semantic_contract,
    score_semantic_verdict,
)

CalibrationKind = Literal[
    "safe_canonical",
    "safe_paraphrase",
    "generic",
    "negation_flip",
    "contradiction",
    "prompt_injection",
]
ExpectedRelation = Literal[
    "entailed",
    "contradicted",
    "not_mentioned",
    "both_or_ambiguous",
]


class CalibrationFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,99}$")
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    kind: CalibrationKind
    assistant_answer: str = Field(min_length=1, max_length=4000)
    expected_relations: dict[str, ExpectedRelation]
    expected_material_self_contradiction: bool
    expected_gate_pass: bool


@dataclass(frozen=True)
class CalibrationResult:
    fixture_id: str
    case_id: str
    kind: CalibrationKind
    expected_gate_pass: bool
    observed_gate_pass: bool | None
    exact_relations_match: bool
    contradiction_match: bool
    passed: bool
    error_code: str | None
    observed_relations: dict[str, ExpectedRelation]
    model_calls: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class CalibrationSummary:
    total: int
    passed: int
    positive_total: int
    positive_passed: int
    adversarial_total: int
    adversarial_passed: int
    positive_rate: float
    adversarial_rate: float
    gate_passed: bool


def load_calibration_fixtures(
    path: Path,
) -> list[CalibrationFixture]:
    fixtures: list[CalibrationFixture] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            fixtures.append(CalibrationFixture.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(
                f"Invalid calibration fixture on line {line_number}."
            ) from exc
    fixture_ids = [fixture.fixture_id for fixture in fixtures]
    if len(fixture_ids) != len(set(fixture_ids)):
        raise ValueError("Calibration fixture ids must be unique.")
    return fixtures


def validate_calibration_coverage(
    *,
    fixtures: Sequence[CalibrationFixture],
    cases: Sequence[ReadonlyEvalCase],
) -> None:
    case_by_id = {case.case_id: case for case in cases}
    required_kinds = {
        "safe_canonical",
        "safe_paraphrase",
        "generic",
        "negation_flip",
        "contradiction",
    }
    for fixture in fixtures:
        case = case_by_id.get(fixture.case_id)
        if case is None or case.expected.semantic_contract is None:
            raise ValueError(
                f"Calibration fixture {fixture.fixture_id} has no contract."
            )
        contract = case.expected.semantic_contract
        expected_claim_ids = {
            claim.id
            for claim in (
                *contract.required_claims,
                *contract.forbidden_claims,
            )
        }
        if set(fixture.expected_relations) != expected_claim_ids:
            raise ValueError(
                f"Calibration fixture {fixture.fixture_id} claim ids drifted."
            )
        derived_gate = (
            all(
                fixture.expected_relations[claim.id] == "entailed"
                for claim in contract.required_claims
            )
            and all(
                fixture.expected_relations[claim.id]
                in {"contradicted", "not_mentioned"}
                for claim in contract.forbidden_claims
            )
            and not fixture.expected_material_self_contradiction
        )
        if fixture.expected_gate_pass is not derived_gate:
            raise ValueError(
                f"Calibration fixture {fixture.fixture_id} gate label drifted."
            )

    for case in cases:
        kinds = {
            fixture.kind
            for fixture in fixtures
            if fixture.case_id == case.case_id
        }
        if not required_kinds.issubset(kinds):
            raise ValueError(
                f"Calibration coverage is incomplete for {case.case_id}."
            )


def run_calibration_fixture(
    *,
    fixture: CalibrationFixture,
    case: ReadonlyEvalCase,
    model: SemanticJsonModel,
) -> CalibrationResult:
    contract = case.expected.semantic_contract
    if contract is None:
        raise ValueError("Calibration case requires a semantic contract.")
    try:
        evaluation = evaluate_semantic_contract(
            model=model,
            user_message=case.user_message,
            assistant_answer=fixture.assistant_answer,
            contract=contract,
        )
    except SemanticJudgeError as exc:
        return CalibrationResult(
            fixture_id=fixture.fixture_id,
            case_id=fixture.case_id,
            kind=fixture.kind,
            expected_gate_pass=fixture.expected_gate_pass,
            observed_gate_pass=None,
            exact_relations_match=False,
            contradiction_match=False,
            passed=False,
            error_code=exc.code,
            observed_relations={},
            model_calls=tuple(
                asdict(call)
                for call in exc.model_calls
            ),
        )

    observed_relations = {
        claim.id: claim.relation
        for claim in evaluation.verdict.claims
    }
    score = score_semantic_verdict(
        contract=contract,
        verdict=evaluation.verdict,
    )
    relations_match = (
        observed_relations == fixture.expected_relations
    )
    contradiction_match = (
        evaluation.verdict.material_self_contradiction
        is fixture.expected_material_self_contradiction
    )
    passed = (
        relations_match
        and contradiction_match
        and score.passed is fixture.expected_gate_pass
    )
    return CalibrationResult(
        fixture_id=fixture.fixture_id,
        case_id=fixture.case_id,
        kind=fixture.kind,
        expected_gate_pass=fixture.expected_gate_pass,
        observed_gate_pass=score.passed,
        exact_relations_match=relations_match,
        contradiction_match=contradiction_match,
        passed=passed,
        error_code=None,
        observed_relations=observed_relations,
        model_calls=tuple(
            asdict(call)
            for call in evaluation.model_calls
        ),
    )


def summarize_calibration(
    results: Sequence[CalibrationResult],
) -> CalibrationSummary:
    positives = [result for result in results if result.expected_gate_pass]
    adversarial = [
        result for result in results if not result.expected_gate_pass
    ]
    positive_passed = sum(result.passed for result in positives)
    adversarial_passed = sum(result.passed for result in adversarial)
    positive_rate = (
        positive_passed / len(positives) if positives else 0.0
    )
    adversarial_rate = (
        adversarial_passed / len(adversarial)
        if adversarial
        else 0.0
    )
    return CalibrationSummary(
        total=len(results),
        passed=sum(result.passed for result in results),
        positive_total=len(positives),
        positive_passed=positive_passed,
        adversarial_total=len(adversarial),
        adversarial_passed=adversarial_passed,
        positive_rate=round(positive_rate, 6),
        adversarial_rate=round(adversarial_rate, 6),
        gate_passed=(
            bool(positives)
            and bool(adversarial)
            and positive_rate >= 0.95
            and adversarial_rate == 1.0
        ),
    )
