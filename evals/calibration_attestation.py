from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.deepseek_budget import (
    BudgetError,
    BudgetUsageError,
    SQLiteBudgetLedger,
    calculate_usage_cost_from_rates,
    cny_to_units,
)
from app.config import Settings
from evals.canonical_pricing import (
    FORMAL_EXECUTION_LIMIT_CNY,
    FORMAL_HARD_LIMIT_CNY,
    CanonicalPricingError,
    require_canonical_attempt_reservation,
    require_canonical_paid_budget,
)
from evals.evidence import stable_sha256
from evals.evidence_schema import (
    BudgetAmountSummary,
    BudgetAttemptBucket,
    BudgetAttemptEvidence,
    BudgetRatesCny,
    BudgetRunIdentity,
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
from evals.readonly_reporting import (
    current_source_tree_sha256,
    freeze_readonly_harness,
    require_clean_git_worktree,
)
from evals.semantic_calibration import (
    CalibrationFixture,
    CalibrationKind,
    CalibrationResult,
    CalibrationSummary,
    ExpectedRelation,
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
DEFAULT_BUDGET_LEDGER = (
    ROOT / "artifacts" / "private" / "deepseek-budget.sqlite3"
)
CANONICAL_DEEPSEEK_MODEL = "deepseek-v4-flash"
CANONICAL_DEEPSEEK_TIMEOUT_SECONDS = 30.0
CANONICAL_DEEPSEEK_MAX_TOKENS = 1024
CANONICAL_DEEPSEEK_MAX_RETRIES = 2
CANONICAL_AGENT_MAX_TOOL_ROUNDS = 4
CANONICAL_AGENT_MAX_TOOL_CALLS = 12
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CalibrationAttestationError(RuntimeError):
    """A calibration report or independent review cannot be trusted."""


@dataclass(frozen=True)
class _TrustedCalibrationValidationContext:
    source_git_commit: str
    source_tree_sha256: str
    fixture_snapshot: FileSnapshot
    harness_fingerprints: Mapping[str, str]


def _freeze_trusted_validation_context(
    settings: Settings,
) -> _TrustedCalibrationValidationContext:
    """Independently bind validation to one clean local source snapshot."""

    try:
        source_git_commit = require_clean_git_worktree()
        source_tree_before = current_source_tree_sha256()
        frozen_harness = freeze_readonly_harness(settings)
        source_tree_after = current_source_tree_sha256()
        require_clean_git_worktree(
            expected_commit=source_git_commit
        )
    except (
        CanonicalPricingError,
        FileSnapshotError,
        OSError,
        ValueError,
    ) as exc:
        raise CalibrationAttestationError(
            "The trusted calibration source context is not clean or stable."
        ) from exc
    if source_tree_before != source_tree_after:
        raise CalibrationAttestationError(
            "The trusted calibration source context changed while freezing."
        )
    return _TrustedCalibrationValidationContext(
        source_git_commit=source_git_commit,
        source_tree_sha256=source_tree_after,
        fixture_snapshot=frozen_harness.calibration_fixture_snapshot,
        harness_fingerprints=frozen_harness.fingerprints,
    )


def _require_trusted_context_still_current(
    context: _TrustedCalibrationValidationContext,
) -> None:
    """Close the source TOCTOU window immediately before acceptance."""

    try:
        if (
            current_source_tree_sha256()
            != context.source_tree_sha256
        ):
            raise ValueError(
                "The trusted source tree changed during validation."
            )
        require_clean_git_worktree(
            expected_commit=context.source_git_commit
        )
    except (OSError, ValueError) as exc:
        raise CalibrationAttestationError(
            "The trusted calibration source changed before acceptance."
        ) from exc


def require_canonical_calibration_runtime(settings: Settings) -> None:
    """Fail closed unless calibration uses the priced deterministic runtime."""

    try:
        endpoint = urlparse(settings.deepseek_base_url)
        endpoint_port = endpoint.port
    except (TypeError, ValueError) as exc:
        raise CalibrationAttestationError(
            "The calibration runtime endpoint is invalid."
        ) from exc
    if (
        type(settings.deepseek_temperature) not in {int, float}
        or settings.deepseek_temperature != 0
        or settings.deepseek_model != CANONICAL_DEEPSEEK_MODEL
        or endpoint.scheme != "https"
        or endpoint.hostname != "api.deepseek.com"
        or endpoint_port not in {None, 443}
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or endpoint.path not in {"", "/"}
        or settings.deepseek_timeout_seconds
        != CANONICAL_DEEPSEEK_TIMEOUT_SECONDS
        or settings.deepseek_max_tokens != CANONICAL_DEEPSEEK_MAX_TOKENS
        or settings.deepseek_max_retries
        != CANONICAL_DEEPSEEK_MAX_RETRIES
        or settings.agent_max_tool_rounds
        != CANONICAL_AGENT_MAX_TOOL_ROUNDS
        or settings.agent_max_tool_calls
        != CANONICAL_AGENT_MAX_TOOL_CALLS
    ):
        raise CalibrationAttestationError(
            "The calibration runtime must use temperature 0, the canonical "
            "model, official DeepSeek HTTPS endpoint, 30-second timeout, "
            "1024 output-token limit, 2 retries, 4 tool rounds, and 12 "
            "tool calls."
        )


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


class _TrustedLedgerEvidence(_StrictModel):
    run_identity: BudgetRunIdentity
    run: BudgetAmountSummary
    cumulative: BudgetAmountSummary
    attempt_evidence: BudgetAttemptEvidence


def _read_trusted_budget_evidence(
    *,
    run_id: str,
) -> dict[str, object]:
    """Read the fixed private ledger without accepting caller evidence."""

    try:
        return SQLiteBudgetLedger.read_existing_evidence_snapshot(
            path=DEFAULT_BUDGET_LEDGER,
            hard_limit_cny=FORMAL_HARD_LIMIT_CNY,
            execution_limit_cny=FORMAL_EXECUTION_LIMIT_CNY,
            run_id=run_id,
        )
    except BudgetError as exc:
        raise CalibrationAttestationError(
            "The trusted persistent calibration budget ledger is unavailable "
            "or invalid."
        ) from exc


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


@dataclass(frozen=True)
class _ValidatedCalibrationCall:
    logical_call_sha256: str
    started_at: datetime
    provider_attempts: int
    usage_cost_units: int
    settlement_mode: Literal["exact", "upper_bound"]


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
    report_started_at: datetime,
    report_completed_at: datetime,
    identity_started_at: datetime,
    identity_completed_at: datetime,
    price_captured_at: datetime,
    price_valid_until: datetime,
) -> _ValidatedCalibrationCall:
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
        or call.finish_reason != "stop"
        or call.message_count != 2
        or call.tool_contract_count != 0
        or call.tool_calls
        or call.error_code is not None
        or call.http_status is not None
        or call.observed_model != settings.deepseek_model
        or call.usage is None
        or call.provider_attempts is None
        or isinstance(call.provider_attempts, bool)
        or call.provider_attempts != 1
        or call.logical_call_sha256 is None
    ):
        raise CalibrationAttestationError(
            "A calibration model call has invalid model or protocol evidence."
        )
    if (
        call.started_at.tzinfo is None
        or not (
            identity_started_at
            <= report_started_at
            <= call.started_at
            <= identity_completed_at
            <= report_completed_at
        )
        or not (
            price_captured_at
            <= call.started_at
            <= price_valid_until
        )
    ):
        raise CalibrationAttestationError(
            "A calibration model call has an invalid timestamp."
        )
    assert call.usage is not None
    assert isinstance(call.provider_attempts, int)
    usage_cost_units, settlement_mode = _usage_cost_units(
        usage=call.usage,
        rates_cny=rates_cny,
        tokens_per_price_unit=tokens_per_price_unit,
    )
    return _ValidatedCalibrationCall(
        logical_call_sha256=call.logical_call_sha256,
        started_at=call.started_at,
        provider_attempts=call.provider_attempts,
        usage_cost_units=usage_cost_units,
        settlement_mode=settlement_mode,
    )


def _budget_amount_identity(
    amount: BudgetAmountSummary,
) -> dict[str, object]:
    payload = amount.model_dump(mode="json")
    # The ledger deliberately reports the current cumulative remaining amount
    # in both scopes. Later paid runs can change it without changing this run.
    payload.pop("remaining_execution_cny")
    return payload


def _attempt_bucket_identity(
    bucket: BudgetAttemptBucket,
) -> tuple[object, ...]:
    return (
        bucket.logical_call_sha256,
        bucket.status,
        bucket.settlement_mode,
        bucket.reserved_cny,
        bucket.known_cost_cny,
        bucket.error_code,
        (
            bucket.completed_at.isoformat()
            if bucket.completed_at is not None
            else None
        ),
        bucket.count,
    )


def _require_trusted_ledger_matches_report(
    *,
    report_budget: BudgetSummary,
    trusted_budget: _TrustedLedgerEvidence,
    validated_calls: list[_ValidatedCalibrationCall],
) -> None:
    report_identity = report_budget.run_identity
    report_attempts = report_budget.attempt_evidence
    report_price = report_budget.price
    assert report_identity is not None
    assert report_identity.completed_at is not None
    assert report_attempts is not None
    assert report_price is not None
    if (
        trusted_budget.run_identity.model_dump(mode="json")
        != report_identity.model_dump(mode="json")
        or _budget_amount_identity(trusted_budget.run)
        != _budget_amount_identity(report_budget.run)
        or Counter(
            _attempt_bucket_identity(bucket)
            for bucket in trusted_budget.attempt_evidence.run
        )
        != Counter(
            _attempt_bucket_identity(bucket)
            for bucket in report_attempts.run
        )
    ):
        raise CalibrationAttestationError(
            "The calibration report does not match the trusted persistent "
            "ledger run."
        )

    call_hashes = [
        call.logical_call_sha256
        for call in validated_calls
    ]
    report_bucket_by_hash: dict[str, BudgetAttemptBucket] = {}
    for bucket in report_attempts.run:
        if (
            bucket.count != 1
            or bucket.logical_call_sha256 in report_bucket_by_hash
        ):
            raise CalibrationAttestationError(
                "Calibration calls require unique single-attempt ledger "
                "identities."
            )
        report_bucket_by_hash[bucket.logical_call_sha256] = bucket
    if (
        len(call_hashes) != 49
        or len(call_hashes) != len(set(call_hashes))
        or set(call_hashes) != set(report_bucket_by_hash)
    ):
        raise CalibrationAttestationError(
            "The 49 calibration calls do not match unique trusted ledger "
            "identities."
        )

    for call in validated_calls:
        bucket = report_bucket_by_hash[call.logical_call_sha256]
        expected_status = (
            "settled_exact"
            if call.settlement_mode == "exact"
            else "settled_upper_bound"
        )
        if (
            bucket.status != expected_status
            or bucket.settlement_mode != call.settlement_mode
            or bucket.known_cost_cny is None
            or cny_to_units(Decimal(bucket.known_cost_cny))
            != call.usage_cost_units
            or bucket.error_code is not None
            or bucket.completed_at is None
            or bucket.completed_at < call.started_at
            or bucket.completed_at > report_identity.completed_at
            or bucket.completed_at > report_price.valid_until
        ):
            raise CalibrationAttestationError(
                "A calibration call does not match its settled trusted "
                "ledger attempt."
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

    require_canonical_calibration_runtime(settings)
    if (
        fixture_path.resolve() != CANONICAL_FIXTURE_PATH.resolve()
        or case_dir.resolve() != CANONICAL_CASE_DIR.resolve()
    ):
        raise CalibrationAttestationError(
            "The trusted calibration context requires canonical paths."
        )
    trusted_context = _freeze_trusted_validation_context(settings)
    if (
        fixture_snapshot is not None
        and fixture_snapshot.sha256
        != trusted_context.fixture_snapshot.sha256
    ):
        raise CalibrationAttestationError(
            "The caller fixture snapshot does not match the trusted context."
        )
    if (
        harness_fingerprints is not None
        and dict(harness_fingerprints)
        != dict(trusted_context.harness_fingerprints)
    ):
        raise CalibrationAttestationError(
            "The caller harness does not match the trusted context."
        )
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
            "The calibration report failed its strict schema or budget "
            "lifecycle validation."
        ) from exc
    try:
        trusted_budget = _TrustedLedgerEvidence.model_validate(
            _read_trusted_budget_evidence(run_id=report.run_id)
        )
    except ValidationError as exc:
        raise CalibrationAttestationError(
            "The trusted persistent calibration budget ledger has an "
            "invalid evidence schema."
        ) from exc
    if report.source_git_commit != trusted_context.source_git_commit:
        raise CalibrationAttestationError(
            "The calibration report source commit is not the trusted HEAD."
        )
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
        canonical_fixture_snapshot = trusted_context.fixture_snapshot
        fixtures = parse_calibration_fixtures_snapshot(
            canonical_fixture_snapshot
        )
        cases = load_cases(CANONICAL_CASE_DIR)
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
    current_harness = dict(trusted_context.harness_fingerprints)
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
    run_identity = budget.run_identity
    assert run_identity is not None
    assert run_identity.completed_at is not None
    if (
        budget.enforcement_mode != "persistent_sqlite"
        or budget.run_status != "completed"
        or attempt_evidence is None
        or not attempt_evidence.run
        or not attempt_evidence.cumulative
        or run_identity.run_id != report.run_id
        or run_identity.purpose
        != "semantic_judge_calibration"
        or run_identity.model != settings.deepseek_model
        or run_identity.price_sha256 != budget.price.snapshot_sha256
        or budget.price.model != settings.deepseek_model
        or not (
            run_identity.started_at
            <= report.started_at
            <= run_identity.completed_at
            <= report.completed_at
        )
        or run_identity.started_at < budget.price.captured_at
        or run_identity.completed_at > budget.price.valid_until
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
            report_started_at=report.started_at,
            report_completed_at=report.completed_at,
            identity_started_at=run_identity.started_at,
            identity_completed_at=run_identity.completed_at,
            price_captured_at=budget.price.captured_at,
            price_valid_until=budget.price.valid_until,
        )
        for record in report.results
    ]
    provider_attempts = sum(
        item.provider_attempts
        for item in validated_calls
    )
    expected_settled_units = sum(
        item.usage_cost_units
        for item in validated_calls
    )
    expected_buckets: Counter[tuple[str, str, int]] = Counter()
    for validated_call in validated_calls:
        expected_buckets[
            (
                (
                    "settled_exact"
                    if validated_call.settlement_mode == "exact"
                    else "settled_upper_bound"
                ),
                validated_call.settlement_mode,
                validated_call.usage_cost_units,
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
    _require_trusted_ledger_matches_report(
        report_budget=budget,
        trusted_budget=trusted_budget,
        validated_calls=validated_calls,
    )
    _require_trusted_context_still_current(trusted_context)
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
