from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.deepseek_budget import (
    BudgetUsageError,
    calculate_usage_cost_from_rates,
    cny_to_units,
)
from app.config import Settings
from evals.canonical_pricing import (
    CanonicalPricingError,
    require_canonical_attempt_reservation,
    require_canonical_paid_budget,
)
from evals.evidence import stable_sha256
from evals.evidence_schema import (
    BudgetRatesCny,
    BudgetSummary,
    ModelCallRecord,
)
from evals.file_snapshot import (
    FileSnapshot,
    FileSnapshotError,
    read_json_object_snapshot,
    require_private_regular_file,
)
from evals.readonly_eval import ReadonlyEvalCase, load_cases
from evals.readonly_reporting import current_readonly_harness_fingerprints
from evals.semantic_calibration import (
    CalibrationFixture,
    CalibrationKind,
    CalibrationResult,
    CalibrationSummary,
    ExpectedRelation,
    load_calibration_fixtures_snapshot,
    parse_calibration_fixtures_snapshot,
    summarize_calibration,
    validate_calibration_coverage,
    validate_calibration_verdict_grounding,
)
from evals.semantic_judge import (
    SemanticJudgeError,
    SemanticJudgeVerdict,
    score_semantic_verdict,
    validate_semantic_verdict_grounding,
)

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_FIXTURE_PATH = (
    ROOT / "evals" / "semantic_judge_calibration_cases.jsonl"
)
CANONICAL_CASE_DIR = ROOT / "evals" / "readonly_regression_cases"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CalibrationAttestationError(RuntimeError):
    """A calibration report or independent review cannot be trusted."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalibrationResultRecord(_StrictModel):
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
    verdict: SemanticJudgeVerdict | None
    model_calls: list[ModelCallRecord]


class CalibrationSummaryRecord(_StrictModel):
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    positive_total: int = Field(ge=1)
    positive_passed: int = Field(ge=0)
    adversarial_total: int = Field(ge=1)
    adversarial_passed: int = Field(ge=0)
    positive_rate: float = Field(ge=0, le=1)
    adversarial_rate: float = Field(ge=0, le=1)
    canonical_corpus: bool
    gate_passed: bool


class CalibrationReport(_StrictModel):
    schema_version: Literal["2.0"]
    attestation_kind: Literal["semantic_judge_holdout_eligibility"]
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$")
    source_git_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    started_at: datetime
    completed_at: datetime
    fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    harness: dict[str, str]
    summary: CalibrationSummaryRecord
    budget: BudgetSummary
    results: list[CalibrationResultRecord]


class CalibrationReviewItem(_StrictModel):
    fixture_id: str
    relations_match: Literal[True]
    grounding_valid: Literal[True]
    contradiction_label_matches: Literal[True]
    notes: str = Field(min_length=20, max_length=500)


class CalibrationReviewRecord(_StrictModel):
    schema_version: Literal["1.0"]
    calibration_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviewer_id: str = Field(
        min_length=8,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$",
    )
    reviewed_at: datetime
    conclusion: Literal["GO"]
    implementation_independence_declared: Literal[True]
    items: list[CalibrationReviewItem] = Field(min_length=1)
    notes: str = Field(min_length=20, max_length=2000)


@dataclass(frozen=True)
class ValidatedCalibrationAttestation:
    report_sha256: str
    run_id: str
    source_git_commit: str
    fixture_sha256: str
    contract_set_sha256: str
    harness_sha256: str
    result_count: int
    fixture_ids: tuple[str, ...]
    fixture_kinds: tuple[tuple[str, CalibrationKind], ...]
    completed_at: datetime


@dataclass(frozen=True)
class ValidatedCalibrationReview:
    review_sha256: str
    reviewer_id: str
    reviewed_count: int


def canonical_contract_set_sha256(
    cases: list[ReadonlyEvalCase],
) -> str:
    payloads = [
        case.model_dump(mode="json")
        for case in sorted(cases, key=lambda item: item.case_id)
    ]
    return stable_sha256(payloads)


def _usage_cost_units(
    *,
    usage: dict[str, int],
    rates_cny: BudgetRatesCny,
    tokens_per_price_unit: int,
) -> tuple[int, Literal["exact", "upper_bound"]]:
    try:
        usage_cost = calculate_usage_cost_from_rates(
            rates_cny=rates_cny.model_dump(),
            tokens_per_price_unit=tokens_per_price_unit,
            usage=usage,
        )
    except BudgetUsageError as exc:
        raise CalibrationAttestationError(
            "A calibration call has invalid or inconsistent token usage."
        ) from exc
    if usage_cost.mode == "reservation":
        raise CalibrationAttestationError(
            "A calibration call has an invalid settlement mode."
        )
    return usage_cost.units, usage_cost.mode


def required_review_fixture_ids(
    attestation: ValidatedCalibrationAttestation,
) -> tuple[str, ...]:
    """Derive a deterministic, stratified review sample from report identity."""

    fixture_kinds = dict(attestation.fixture_kinds)

    def rank(fixture_id: str) -> str:
        return hashlib.sha256(
            (
                f"{attestation.report_sha256}:{fixture_id}"
            ).encode("utf-8")
        ).hexdigest()

    selected: list[str] = []
    for required_kind in (
        "safe_prompt_injection",
        "unsafe_prompt_injection",
        "contradiction",
    ):
        candidates = [
            fixture_id
            for fixture_id, kind in fixture_kinds.items()
            if kind == required_kind
        ]
        if not candidates:
            raise CalibrationAttestationError(
                "The calibration corpus cannot support stratified review."
            )
        selected.append(min(candidates, key=rank))
    minimum_reviewed = math.ceil(attestation.result_count * 0.10)
    remaining = sorted(
        (
            fixture_id
            for fixture_id in attestation.fixture_ids
            if fixture_id not in selected
        ),
        key=rank,
    )
    selected.extend(remaining[: minimum_reviewed - len(selected)])
    return tuple(selected)


def _as_result(
    record: CalibrationResultRecord,
) -> CalibrationResult:
    return CalibrationResult(
        fixture_id=record.fixture_id,
        case_id=record.case_id,
        kind=record.kind,
        expected_gate_pass=record.expected_gate_pass,
        observed_gate_pass=record.observed_gate_pass,
        exact_relations_match=record.exact_relations_match,
        contradiction_match=record.contradiction_match,
        passed=record.passed,
        error_code=record.error_code,
        observed_relations=dict(record.observed_relations),
        verdict=(
            record.verdict.model_dump(mode="json")
            if record.verdict is not None
            else None
        ),
        model_calls=tuple(
            call.model_dump(mode="json")
            for call in record.model_calls
        ),
    )


def _validate_result(
    *,
    record: CalibrationResultRecord,
    fixture: CalibrationFixture,
    case: ReadonlyEvalCase,
    settings: Settings,
    rates_cny: BudgetRatesCny,
    tokens_per_price_unit: int,
) -> tuple[int, int, Literal["exact", "upper_bound"]]:
    if (
        record.fixture_id != fixture.fixture_id
        or record.case_id != fixture.case_id
        or record.kind != fixture.kind
        or record.expected_gate_pass is not fixture.expected_gate_pass
    ):
        raise CalibrationAttestationError(
            "A calibration result does not match the canonical fixture."
        )
    contract = case.expected.semantic_contract
    if contract is None or record.verdict is None:
        raise CalibrationAttestationError(
            "A calibration result is missing its semantic verdict."
        )
    try:
        validate_semantic_verdict_grounding(
            verdict=record.verdict,
            contract=contract,
            assistant_answer=fixture.assistant_answer,
        )
        validate_calibration_verdict_grounding(
            fixture=fixture,
            verdict=record.verdict,
        )
    except SemanticJudgeError as exc:
        raise CalibrationAttestationError(
            "A calibration verdict failed grounding validation."
        ) from exc
    observed_relations = {
        claim.id: claim.relation
        for claim in record.verdict.claims
    }
    score = score_semantic_verdict(
        contract=contract,
        verdict=record.verdict,
    )
    expected_pass = (
        observed_relations == fixture.effective_expected_relations
        and record.verdict.material_self_contradiction
        is fixture.expected_material_self_contradiction
        and score.passed is fixture.expected_gate_pass
    )
    if (
        not expected_pass
        or record.observed_relations != observed_relations
        or record.observed_gate_pass is not score.passed
        or record.exact_relations_match is not True
        or record.contradiction_match is not True
        or record.passed is not True
        or record.error_code is not None
    ):
        raise CalibrationAttestationError(
            "A calibration result or summary flag was tampered with."
        )
    if len(record.model_calls) != 1:
        raise CalibrationAttestationError(
            "Each calibration fixture requires exactly one judge model call."
        )
    call = record.model_calls[0]
    if (
        call.status != "success"
        or call.phase != "semantic_judge"
        or call.tool_contract_count != 0
        or call.tool_calls
        or call.error_code is not None
        or call.observed_model != settings.deepseek_model
        or call.usage is None
        or call.provider_attempts is None
        or isinstance(call.provider_attempts, bool)
        or call.provider_attempts != 1
    ):
        raise CalibrationAttestationError(
            "A calibration model call has invalid model or protocol evidence."
        )
    assert call.usage is not None
    assert isinstance(call.provider_attempts, int)
    usage_cost_units, settlement_mode = _usage_cost_units(
        usage=call.usage,
        rates_cny=rates_cny,
        tokens_per_price_unit=tokens_per_price_unit,
    )
    return (
        call.provider_attempts,
        usage_cost_units,
        settlement_mode,
    )


def validate_calibration_attestation(
    *,
    report_path: Path,
    settings: Settings,
    fixture_path: Path = CANONICAL_FIXTURE_PATH,
    case_dir: Path = CANONICAL_CASE_DIR,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
    fixture_snapshot: FileSnapshot | None = None,
    harness_fingerprints: Mapping[str, str] | None = None,
) -> ValidatedCalibrationAttestation:
    """Validate and independently recompute a holdout-eligibility report."""

    try:
        require_private_regular_file(
            report_path,
            label="calibration report",
        )
        report_payload, report_sha256 = read_json_object_snapshot(
            report_path,
            label="calibration report",
        )
        report = CalibrationReport.model_validate(
            report_payload
        )
    except (FileSnapshotError, ValidationError) as exc:
        raise CalibrationAttestationError(
            "The calibration report failed its strict schema."
        ) from exc
    checked_at = now or datetime.now(UTC)
    if (
        checked_at.tzinfo is None
        or report.started_at.tzinfo is None
        or report.completed_at.tzinfo is None
        or report.completed_at < report.started_at
    ):
        raise CalibrationAttestationError(
            "The calibration report timestamps are inconsistent."
        )
    if (
        report.completed_at > checked_at + timedelta(minutes=5)
        or checked_at - report.completed_at > max_age
    ):
        raise CalibrationAttestationError(
            "The calibration report is too old or not yet fresh."
        )
    try:
        if fixture_snapshot is None:
            fixtures, canonical_fixture_snapshot = (
                load_calibration_fixtures_snapshot(fixture_path)
            )
        else:
            canonical_fixture_snapshot = fixture_snapshot
            fixtures = parse_calibration_fixtures_snapshot(
                canonical_fixture_snapshot
            )
        cases = load_cases(case_dir)
        validate_calibration_coverage(fixtures=fixtures, cases=cases)
    except (FileSnapshotError, OSError, ValueError) as exc:
        raise CalibrationAttestationError(
            "The canonical calibration corpus failed validation."
        ) from exc
    if report.fixture_sha256 != canonical_fixture_snapshot.sha256:
        raise CalibrationAttestationError(
            "The calibration fixture hash does not match the canonical corpus."
        )
    contract_set_sha256 = canonical_contract_set_sha256(cases)
    if report.contract_set_sha256 != contract_set_sha256:
        raise CalibrationAttestationError(
            "The calibration contract-set hash does not match."
        )
    current_harness = (
        dict(harness_fingerprints)
        if harness_fingerprints is not None
        else current_readonly_harness_fingerprints(settings)
    )
    if report.harness != current_harness:
        raise CalibrationAttestationError(
            "The calibration harness or model runtime has drifted."
        )
    fixture_by_id = {
        fixture.fixture_id: fixture
        for fixture in fixtures
    }
    case_by_id = {case.case_id: case for case in cases}
    result_ids = [record.fixture_id for record in report.results]
    if (
        len(result_ids) != len(fixtures)
        or len(result_ids) != len(set(result_ids))
        or set(result_ids) != set(fixture_by_id)
    ):
        raise CalibrationAttestationError(
            "The calibration result set is incomplete or duplicated."
        )
    budget = report.budget
    if budget.price is None:
        raise CalibrationAttestationError(
            "The calibration budget evidence is missing pricing."
        )
    try:
        canonical_price = require_canonical_paid_budget(
            price=budget.price,
            expected_model=settings.deepseek_model,
            run_hard_limit_cny=budget.run.hard_limit_cny,
            run_execution_limit_cny=(
                budget.run.execution_limit_cny
            ),
            cumulative_hard_limit_cny=(
                budget.cumulative.hard_limit_cny
            ),
            cumulative_execution_limit_cny=(
                budget.cumulative.execution_limit_cny
            ),
        )
        require_canonical_attempt_reservation(
            canonical_price=canonical_price,
            max_output_tokens=settings.deepseek_max_tokens,
            reservation_cny_per_attempt=(
                budget.reservation_cny_per_attempt
            ),
        )
    except CanonicalPricingError as exc:
        raise CalibrationAttestationError(
            "The calibration budget pricing, limits, or reservation "
            "are not canonical."
        ) from exc
    canonical_rates = BudgetRatesCny.model_validate(
        canonical_price.rates_cny.model_dump()
    )
    attempt_evidence = budget.attempt_evidence
    if (
        budget.enforcement_mode != "persistent_sqlite"
        or budget.run_status != "completed"
        or budget.run_identity is None
        or attempt_evidence is None
        or not attempt_evidence.run
        or not attempt_evidence.cumulative
        or budget.run_identity.run_id != report.run_id
        or budget.run_identity.purpose
        != "semantic_judge_calibration"
        or budget.price.model != settings.deepseek_model
        or report.started_at < budget.price.captured_at
        or report.completed_at > budget.price.valid_until
        or budget.run.reserved_count != 0
        or budget.run.uncertain_count != 0
        or budget.cumulative.reserved_count != 0
        or budget.cumulative.uncertain_count != 0
        or budget.run.committed_cny != budget.run.settled_cny
        or (
            budget.cumulative.committed_cny
            != budget.cumulative.settled_cny
        )
        or Decimal(budget.cumulative.committed_cny)
        > Decimal(budget.cumulative.execution_limit_cny)
    ):
        raise CalibrationAttestationError(
            "The calibration budget evidence is unsettled or inconsistent."
        )
    assert attempt_evidence is not None
    validated_calls = [
        _validate_result(
            record=record,
            fixture=fixture_by_id[record.fixture_id],
            case=case_by_id[record.case_id],
            settings=settings,
            rates_cny=canonical_rates,
            tokens_per_price_unit=(
                canonical_price.tokens_per_price_unit
            ),
        )
        for record in report.results
    ]
    provider_attempts = sum(item[0] for item in validated_calls)
    expected_settled_units = sum(item[1] for item in validated_calls)
    expected_buckets: Counter[tuple[str, str, int]] = Counter()
    for _, cost_units, settlement_mode in validated_calls:
        expected_buckets[
            (
                (
                    "settled_exact"
                    if settlement_mode == "exact"
                    else "settled_upper_bound"
                ),
                settlement_mode,
                cost_units,
            )
        ] += 1
    actual_buckets: Counter[tuple[str, str, int]] = Counter()
    for bucket in attempt_evidence.run:
        if (
            bucket.status
            not in {"settled_exact", "settled_upper_bound"}
            or bucket.settlement_mode is None
            or bucket.known_cost_cny is None
            or bucket.reserved_cny
            != budget.reservation_cny_per_attempt
        ):
            raise CalibrationAttestationError(
                "The calibration current-run attempt evidence is unsettled "
                "or has a noncanonical reservation."
            )
        actual_buckets[
            (
                bucket.status,
                bucket.settlement_mode,
                cny_to_units(Decimal(bucket.known_cost_cny)),
            )
        ] += bucket.count
    recomputed_summary: CalibrationSummary = summarize_calibration(
        [_as_result(record) for record in report.results]
    )
    if (
        report.summary.model_dump(mode="json")
        != asdict(recomputed_summary)
        or not recomputed_summary.canonical_corpus
        or not recomputed_summary.gate_passed
        or recomputed_summary.passed != recomputed_summary.total
    ):
        raise CalibrationAttestationError(
            "The calibration summary did not recompute to a passing gate."
        )
    if (
        actual_buckets != expected_buckets
        or budget.run.attempt_count != provider_attempts
        or budget.cumulative.attempt_count < provider_attempts
        or cny_to_units(Decimal(budget.run.settled_cny))
        != expected_settled_units
    ):
        raise CalibrationAttestationError(
            "The calibration budget evidence is unsettled or inconsistent."
        )
    return ValidatedCalibrationAttestation(
        report_sha256=report_sha256,
        run_id=report.run_id,
        source_git_commit=report.source_git_commit,
        fixture_sha256=report.fixture_sha256,
        contract_set_sha256=contract_set_sha256,
        harness_sha256=stable_sha256(current_harness),
        result_count=len(report.results),
        fixture_ids=tuple(sorted(result_ids)),
        fixture_kinds=tuple(
            sorted(
                (
                    fixture.fixture_id,
                    fixture.kind,
                )
                for fixture in fixtures
            )
        ),
        completed_at=report.completed_at,
    )


def validate_calibration_review(
    *,
    review_path: Path,
    attestation: ValidatedCalibrationAttestation,
    now: datetime | None = None,
) -> ValidatedCalibrationReview:
    """Bind an independent sample review to one immutable calibration report."""

    try:
        require_private_regular_file(
            review_path,
            label="calibration review",
        )
        review_payload, review_sha256 = read_json_object_snapshot(
            review_path,
            label="calibration review",
        )
        review = CalibrationReviewRecord.model_validate(
            review_payload
        )
    except (FileSnapshotError, ValidationError) as exc:
        raise CalibrationAttestationError(
            "The calibration review failed its strict schema."
        ) from exc
    if review.calibration_report_sha256 != attestation.report_sha256:
        raise CalibrationAttestationError(
            "The review does not reference the validated calibration report."
        )
    checked_at = now or datetime.now(UTC)
    if (
        checked_at.tzinfo is None
        or review.reviewed_at.tzinfo is None
        or review.reviewed_at < attestation.completed_at
        or review.reviewed_at > checked_at + timedelta(minutes=5)
    ):
        raise CalibrationAttestationError(
            "The calibration review time must follow the report."
        )
    reviewed_ids = [item.fixture_id for item in review.items]
    if (
        len(reviewed_ids) != len(set(reviewed_ids))
        or not set(reviewed_ids).issubset(attestation.fixture_ids)
    ):
        raise CalibrationAttestationError(
            "The calibration review contains invalid fixture ids."
        )
    minimum_reviewed = math.ceil(attestation.result_count * 0.10)
    if len(reviewed_ids) < minimum_reviewed:
        raise CalibrationAttestationError(
            "The independent calibration review sampled less than 10%."
        )
    required_ids = set(required_review_fixture_ids(attestation))
    if not required_ids.issubset(reviewed_ids):
        raise CalibrationAttestationError(
            "The independent calibration review missed its "
            "deterministic stratified sample."
        )
    return ValidatedCalibrationReview(
        review_sha256=review_sha256,
        reviewer_id=review.reviewer_id,
        reviewed_count=len(reviewed_ids),
    )
