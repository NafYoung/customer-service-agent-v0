from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.evidence import verify_eval_bundle
from evals.readonly_eval import SCORE_CATEGORIES

Sha256 = str
MoneyCny = Annotated[
    str,
    Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$"),
]


class StrictEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactPaths(StrictEvidenceModel):
    cases: Literal["cases.jsonl"]
    summary: Literal["summary.json"]
    trajectories: Literal["trajectories/"]
    integrity: Literal["integrity.json"]


class SourceSnapshot(StrictEvidenceModel):
    git_commit: str | None
    git_dirty: bool | None
    source_tree_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    python_version: str
    platform: str
    package_versions: dict[str, str]


class EvalSnapshot(StrictEvidenceModel):
    suite_name: Literal["readonly-agent"]
    suite_version: str
    split: Literal["dev", "holdout"]
    case_set_name: str
    case_count: int = Field(ge=1)
    case_set_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_version: str
    scorer_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    case_ids: list[str] | None = None

    @model_validator(mode="after")
    def keep_holdout_ids_withheld(self) -> EvalSnapshot:
        if self.split == "holdout" and self.case_ids is not None:
            raise ValueError("Holdout manifest must not expose case_ids")
        if self.split == "dev" and self.case_ids is None:
            raise ValueError("Development manifest must include case_ids")
        return self


class HarnessSnapshot(StrictEvidenceModel):
    prompt_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    tool_contracts_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    policies_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    seed_data_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    agent_loop_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    max_tool_rounds: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)


class GenerationConfig(StrictEvidenceModel):
    stream: Literal[False]
    thinking: Literal["disabled"]
    temperature: float | Literal["provider_default"]
    seed: int | None
    max_tokens: int = Field(ge=1)


class RetryPolicy(StrictEvidenceModel):
    max_retries: int = Field(ge=0)
    backoff: str


class ModelSnapshot(StrictEvidenceModel):
    provider: str
    requested_model: str
    observed_models: list[str]
    base_url_host: str
    generation_config: GenerationConfig
    timeout_seconds: float = Field(gt=0)
    retry_policy: RetryPolicy


class ExecutionSnapshot(StrictEvidenceModel):
    planned_trials: int = Field(ge=1)
    completed_trials: int = Field(ge=0)
    seed_policy: str
    concurrency: int = Field(ge=1)
    case_order: list[str] | Literal["withheld"]


class BudgetManifest(StrictEvidenceModel):
    schema_version: Literal["1.0"]
    enforcement_mode: Literal[
        "persistent_sqlite",
        "offline_no_paid_provider",
    ]
    currency: Literal["CNY"]
    hard_limit_cny: MoneyCny
    execution_limit_cny: MoneyCny
    reservation_cny_per_attempt: MoneyCny
    price_snapshot_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    price_source_url: str | None
    usage_source_url: str | None
    price_captured_at: datetime | None
    price_valid_until: datetime | None

    @model_validator(mode="after")
    def validate_budget_contract(self) -> BudgetManifest:
        hard = Decimal(self.hard_limit_cny)
        execution = Decimal(self.execution_limit_cny)
        if hard > Decimal("20"):
            raise ValueError("Artifact hard budget exceeds CNY 20")
        if execution > hard:
            raise ValueError("Execution budget exceeds hard budget")
        priced_fields = (
            self.price_snapshot_sha256,
            self.price_source_url,
            self.usage_source_url,
            self.price_captured_at,
            self.price_valid_until,
        )
        if self.enforcement_mode == "persistent_sqlite":
            if any(value is None for value in priced_fields):
                raise ValueError(
                    "Persistent paid mode requires a complete price snapshot"
                )
        elif any(value is not None for value in priced_fields):
            raise ValueError("Offline mode cannot claim a paid price snapshot")
        return self


class ReadonlyManifest(StrictEvidenceModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$")
    purpose: Literal["diagnostic", "dev_repeat", "holdout_formal"]
    status: Literal["completed", "partial"]
    created_at: datetime
    completed_at: datetime
    source: SourceSnapshot
    eval: EvalSnapshot
    harness: HarnessSnapshot
    model: ModelSnapshot
    execution: ExecutionSnapshot
    budget: BudgetManifest
    artifacts: ArtifactPaths


class CountRate(StrictEvidenceModel):
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    rate: float = Field(ge=0, le=1)


class ReliabilitySummary(StrictEvidenceModel):
    k: int = Field(ge=1)
    cases_all_trials_passed: int = Field(ge=0)
    case_count: int = Field(ge=0)
    pass_power_k: float = Field(ge=0, le=1)


class SecuritySummary(CountRate):
    all_trials_passed: bool


class Distribution(StrictEvidenceModel):
    p50: int | float | None
    p95: int | float | None
    max: int | float | None
    total: int | float = Field(ge=0)


class LatencySummary(StrictEvidenceModel):
    case: Distribution
    model_call: Distribution


class BusinessStateSummary(StrictEvidenceModel):
    changed_trials: int = Field(ge=0)
    unknown_trials: int = Field(ge=0)
    all_trials_unchanged: bool


class BudgetAmountSummary(StrictEvidenceModel):
    currency: Literal["CNY"]
    hard_limit_cny: MoneyCny
    execution_limit_cny: MoneyCny
    committed_cny: MoneyCny
    settled_cny: MoneyCny
    remaining_execution_cny: MoneyCny
    attempt_count: int = Field(ge=0)
    reserved_count: int = Field(ge=0)
    uncertain_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_amounts(self) -> BudgetAmountSummary:
        hard = Decimal(self.hard_limit_cny)
        execution = Decimal(self.execution_limit_cny)
        committed = Decimal(self.committed_cny)
        settled = Decimal(self.settled_cny)
        remaining = Decimal(self.remaining_execution_cny)
        if hard > Decimal("20") or execution > hard:
            raise ValueError("Budget limits exceed the artifact contract")
        if committed > execution or settled > committed:
            raise ValueError("Budget commitments are inconsistent")
        if remaining > execution:
            raise ValueError("Remaining budget exceeds execution limit")
        return self


class BudgetPriceSummary(StrictEvidenceModel):
    provider: Literal["deepseek"]
    model: str
    currency: Literal["CNY"]
    snapshot_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str
    usage_source_url: str
    captured_at: datetime
    valid_until: datetime
    rates_cny: dict[str, MoneyCny]
    tokens_per_price_unit: Literal[1_000_000]


class BudgetSummary(StrictEvidenceModel):
    schema_version: Literal["1.0"]
    enforcement_mode: Literal[
        "persistent_sqlite",
        "offline_no_paid_provider",
    ]
    price: BudgetPriceSummary | None
    reservation_cny_per_attempt: MoneyCny
    run: BudgetAmountSummary
    cumulative: BudgetAmountSummary

    @model_validator(mode="after")
    def validate_mode(self) -> BudgetSummary:
        if self.enforcement_mode == "persistent_sqlite":
            if self.price is None:
                raise ValueError("Persistent paid mode requires pricing")
        elif self.price is not None:
            raise ValueError("Offline mode cannot contain paid pricing")
        if self.run.hard_limit_cny != self.cumulative.hard_limit_cny:
            raise ValueError("Run and cumulative hard limits differ")
        return self


class ReadonlySummary(StrictEvidenceModel):
    schema_version: Literal["1.0"]
    run_id: str
    total_cases: int = Field(ge=0)
    planned_trials: int = Field(ge=1)
    total_trials: int = Field(ge=0)
    strict: CountRate
    reliability: ReliabilitySummary
    security: SecuritySummary
    score_layers: dict[str, CountRate]
    usage: dict[str, int]
    latency_ms: LatencySummary
    business_state: BusinessStateSummary
    errors: dict[str, int]
    budget: BudgetSummary

    @model_validator(mode="after")
    def validate_score_layers(self) -> ReadonlySummary:
        if set(self.score_layers) != set(SCORE_CATEGORIES):
            raise ValueError("Summary score layers do not match scorer categories")
        return self


class ModelToolRequest(StrictEvidenceModel):
    tool_call_id: str
    tool_name: str
    arguments: str


class ModelCallRecord(StrictEvidenceModel):
    sequence: int = Field(ge=1)
    status: Literal["success", "error"]
    started_at: datetime
    latency_ms: int = Field(ge=0)
    message_count: int = Field(ge=0)
    tool_contract_count: int = Field(ge=0)
    tool_calls: list[ModelToolRequest]
    finish_reason: str | None
    response_id: str | None
    observed_model: str | None
    usage: dict[str, int] | None
    error_code: str | None
    http_status: int | None
    provider_request_id: str | None
    provider_attempts: int | None


class ToolTraceRecord(StrictEvidenceModel):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] | None
    success: bool
    result: Any | None
    error_code: str | None
    latency_ms: int | None


class BusinessStateRecord(StrictEvidenceModel):
    changed: bool | None
    changed_tables: list[str]
    before_sha256: Sha256 | None
    after_sha256: Sha256 | None

    @model_validator(mode="after")
    def validate_hashes(self) -> BusinessStateRecord:
        for value in (self.before_sha256, self.after_sha256):
            if value is not None and not re_full_sha256(value):
                raise ValueError("Business-state hash must be SHA-256")
        return self


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


class ScoreCheckRecord(StrictEvidenceModel):
    category: str
    message: str
    passed: bool


class ReadonlyCaseRecord(StrictEvidenceModel):
    schema_version: Literal["1.0"]
    case_id: str
    split: Literal["dev", "holdout"]
    trial: int = Field(ge=1)
    case_run_id: str
    input_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    status: Literal["passed", "failed"]
    termination_reason: str
    error_code: str | None
    final_text: str
    model_calls: list[ModelCallRecord]
    tool_trace: list[ToolTraceRecord]
    business_state: BusinessStateRecord
    counted_action_records: int = Field(ge=0)
    scores: dict[str, bool]
    score_checks: list[ScoreCheckRecord]
    checks: list[str]
    failures: list[str]

    @model_validator(mode="after")
    def validate_scores(self) -> ReadonlyCaseRecord:
        if set(self.scores) != set(SCORE_CATEGORIES):
            raise ValueError("Case scores do not match scorer categories")
        return self


class FileIntegrity(StrictEvidenceModel):
    sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class IntegrityIndex(StrictEvidenceModel):
    schema_version: Literal["1.0"]
    algorithm: Literal["sha256"]
    files: dict[str, FileIntegrity]


class ReadonlyEvidenceBundle(StrictEvidenceModel):
    manifest: ReadonlyManifest
    cases: list[ReadonlyCaseRecord]
    summary: ReadonlySummary
    trajectories: list[ReadonlyCaseRecord]
    integrity: IntegrityIndex

    @model_validator(mode="after")
    def cross_validate(self) -> ReadonlyEvidenceBundle:
        if self.manifest.run_id != self.summary.run_id:
            raise ValueError("Manifest and summary run_id differ")
        if (
            self.manifest.budget.enforcement_mode
            != self.summary.budget.enforcement_mode
        ):
            raise ValueError("Manifest and summary budget modes differ")
        if (
            self.manifest.budget.hard_limit_cny
            != self.summary.budget.cumulative.hard_limit_cny
        ):
            raise ValueError("Manifest and summary hard budgets differ")
        case_keys = [(item.case_id, item.trial) for item in self.cases]
        trajectory_keys = [
            (item.case_id, item.trial)
            for item in self.trajectories
        ]
        if len(case_keys) != len(set(case_keys)):
            raise ValueError("Duplicate case/trial records")
        if sorted(case_keys) != sorted(trajectory_keys):
            raise ValueError("Case index and trajectories differ")
        if self.summary.total_trials != len(self.cases):
            raise ValueError("Summary trial count differs from cases")
        if self.manifest.eval.case_count != len({item.case_id for item in self.cases}):
            raise ValueError("Manifest case count differs from cases")
        return self


def validate_readonly_payload(
    payload: Mapping[str, Any],
) -> ReadonlyEvidenceBundle:
    return ReadonlyEvidenceBundle.model_validate(payload)


def validate_readonly_bundle(bundle_path: Path) -> ReadonlyEvidenceBundle:
    return validate_readonly_payload(verify_eval_bundle(bundle_path))
