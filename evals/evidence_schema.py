from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.agent.deepseek_budget import (
    BudgetUsageError,
    calculate_usage_cost_from_rates,
    cny_to_units,
    units_to_cny,
)
from app.tools.contracts import get_read_only_tool_contracts
from evals.canonical_pricing import (
    CanonicalPricingError,
    canonical_price_file_sha256,
    require_canonical_attempt_reservation,
    require_canonical_paid_budget,
)
from evals.diagnostic_evidence import (
    require_completed_diagnostic_evidence,
)
from evals.evidence import (
    verify_eval_bundle,
    verify_private_eval_bundle_permissions,
)
from evals.nonformal_paid_contract import nonformal_paid_contract
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


class SemanticCalibrationSnapshot(StrictEvidenceModel):
    report_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    review_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$")
    source_git_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    fixture_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    contract_set_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    harness_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str = Field(min_length=8, max_length=120)
    reviewed_count: int = Field(ge=1)


class FormalHoldoutSnapshot(StrictEvidenceModel):
    declaration_manifest_sha256: Sha256 = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    lock_start_receipt_sha256: Sha256 = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    declared_harness_sha256: Sha256 = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    regression_bundle_integrity_sha256: Sha256 = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    regression_gate_sha256: Sha256 = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    regression_run_id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$"
    )
    regression_source_git_commit: str = Field(
        pattern=r"^[0-9a-f]{40,64}$"
    )
    regression_case_set_name: Literal["readonly-regression-v1"]
    regression_case_set_sha256: Sha256 = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    regression_harness_sha256: Sha256 = Field(
        pattern=r"^[0-9a-f]{64}$"
    )


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
    semantic_calibration: SemanticCalibrationSnapshot | None = None
    formal_holdout: FormalHoldoutSnapshot | None = None

    @model_validator(mode="after")
    def keep_holdout_ids_withheld(self) -> EvalSnapshot:
        if self.split == "holdout" and self.case_ids is not None:
            raise ValueError("Holdout manifest must not expose case_ids")
        if self.split == "dev" and self.case_ids is None:
            raise ValueError("Development manifest must include case_ids")
        if self.split == "dev" and self.semantic_calibration is not None:
            raise ValueError(
                "Development manifest cannot claim formal calibration"
            )
        if self.split == "dev" and self.formal_holdout is not None:
            raise ValueError(
                "Development manifest cannot claim a formal holdout"
            )
        return self


class HarnessSnapshot(StrictEvidenceModel):
    runtime_harness_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    prompt_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    tool_contracts_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    policies_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    seed_data_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    agent_loop_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    model_runtime_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    semantic_judge_version: str | None = None
    semantic_judge_prompt_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    semantic_judge_source_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    semantic_calibration_source_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    semantic_calibration_validator_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    semantic_calibration_runner_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    semantic_calibration_corpus_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    evidence_protocol_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    canonical_price_snapshot_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
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


class SemanticJudgeConfig(StrictEvidenceModel):
    version: str
    response_format: Literal["json_object"]
    tools_enabled: Literal[False]
    temperature: float
    thinking: Literal["disabled"]


class ModelSnapshot(StrictEvidenceModel):
    provider: str
    requested_model: str
    observed_models: list[str]
    base_url_host: str
    generation_config: GenerationConfig
    timeout_seconds: float = Field(gt=0)
    retry_policy: RetryPolicy
    semantic_judge: SemanticJudgeConfig | None = None


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
    run_status: Literal["active", "completed"] | None = None
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
    schema_version: Literal["1.0", "2.0"]
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

    @model_validator(mode="after")
    def validate_formal_contract(self) -> ReadonlyManifest:
        if (
            self.created_at.tzinfo is None
            or self.completed_at.tzinfo is None
            or self.completed_at < self.created_at
        ):
            raise ValueError("Eval manifest timestamps are invalid")
        if self.purpose != "holdout_formal":
            if self.eval.split != "dev":
                raise ValueError(
                    "Only holdout_formal can use the holdout split"
                )
            if (
                self.eval.semantic_calibration is not None
                or self.eval.formal_holdout is not None
            ):
                raise ValueError(
                    "Non-formal evidence cannot claim formal attestations"
                )
            if self.purpose in {"diagnostic", "dev_repeat"}:
                contract = nonformal_paid_contract(self.purpose)
                if (
                    self.eval.case_set_name != contract.case_set_name
                    or self.eval.case_count != contract.case_count
                    or self.eval.case_set_sha256
                    != contract.case_set_sha256
                    or self.eval.case_ids != list(contract.case_ids)
                    or self.execution.planned_trials
                    != contract.planned_trials
                    or self.execution.case_order
                    != list(contract.case_ids)
                    or self.status != "completed"
                    or self.execution.completed_trials
                    != contract.planned_trials
                ):
                    raise ValueError(
                        f"{self.purpose} evidence is not canonical "
                        "and completed"
                    )
            if self.purpose == "dev_repeat":
                if (
                    self.budget.enforcement_mode
                    != "persistent_sqlite"
                    or self.budget.run_status != "completed"
                    or self.model.observed_models
                    != [self.model.requested_model]
                ):
                    raise ValueError(
                        "dev_repeat paid evidence is not canonical "
                        "and completed"
                    )
            return self
        if self.eval.split != "holdout":
            raise ValueError("holdout_formal requires the holdout split")
        if self.schema_version == "1.0":
            if (
                self.eval.case_set_name != "readonly-holdout-v1"
                or self.eval.case_count != 20
                or self.eval.scorer_version != "readonly-scorer-v2"
                or self.execution.planned_trials != 4
                or self.execution.completed_trials != 4
                or self.budget.enforcement_mode
                != "persistent_sqlite"
            ):
                raise ValueError(
                    "Legacy formal evidence is restricted to retired v1"
                )
            return self
        required_harness_hashes = (
            self.harness.runtime_harness_sha256,
            self.harness.model_runtime_sha256,
            self.harness.semantic_judge_prompt_sha256,
            self.harness.semantic_judge_source_sha256,
            self.harness.semantic_calibration_source_sha256,
            self.harness.semantic_calibration_validator_sha256,
            self.harness.semantic_calibration_runner_sha256,
            self.harness.semantic_calibration_corpus_sha256,
            self.harness.evidence_protocol_sha256,
            self.harness.canonical_price_snapshot_sha256,
        )
        if (
            self.status != "completed"
            or self.eval.case_set_name != "readonly-holdout-v2"
            or self.eval.case_count != 20
            or self.eval.semantic_calibration is None
            or self.eval.formal_holdout is None
            or self.execution.planned_trials != 4
            or self.execution.completed_trials != 4
            or self.execution.case_order != "withheld"
            or self.budget.enforcement_mode != "persistent_sqlite"
            or self.budget.run_status != "completed"
            or any(value is None for value in required_harness_hashes)
            or self.source.git_commit is None
            or self.source.git_dirty is not False
            or self.model.observed_models
            != [self.model.requested_model]
            or (
                self.eval.formal_holdout is not None
                and self.eval.formal_holdout.declared_harness_sha256
                != self.harness.runtime_harness_sha256
            )
            or (
                self.eval.formal_holdout is not None
                and (
                    self.eval.formal_holdout.regression_source_git_commit
                    != self.source.git_commit
                    or self.eval.formal_holdout.regression_harness_sha256
                    != self.harness.runtime_harness_sha256
                )
            )
        ):
            raise ValueError(
                "Formal v2 evidence is missing mandatory attestations"
            )
        return self


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

    @field_validator(
        "attempt_count",
        "reserved_count",
        "uncertain_count",
        mode="before",
    )
    @classmethod
    def validate_count_types(cls, value: Any) -> Any:
        if type(value) is not int or value < 0:
            raise ValueError(
                "budget counts must be non-negative integers"
            )
        return value

    @model_validator(mode="after")
    def validate_amounts(self) -> BudgetAmountSummary:
        hard = Decimal(self.hard_limit_cny)
        execution = Decimal(self.execution_limit_cny)
        committed = Decimal(self.committed_cny)
        settled = Decimal(self.settled_cny)
        remaining = Decimal(self.remaining_execution_cny)
        if hard > Decimal("20") or execution > hard:
            raise ValueError("Budget limits exceed the artifact contract")
        if settled > committed:
            raise ValueError("Budget commitments are inconsistent")
        if remaining > execution:
            raise ValueError("Remaining budget exceeds execution limit")
        return self


class BudgetRatesCny(StrictEvidenceModel):
    prompt_cache_hit: MoneyCny
    prompt_cache_miss: MoneyCny
    completion: MoneyCny


class BudgetPriceSummary(StrictEvidenceModel):
    provider: Literal["deepseek"]
    model: str
    currency: Literal["CNY"]
    snapshot_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str
    usage_source_url: str
    captured_at: datetime
    valid_until: datetime
    rates_cny: BudgetRatesCny
    tokens_per_price_unit: Literal[1_000_000]

    @model_validator(mode="after")
    def validate_price_window(self) -> BudgetPriceSummary:
        if (
            self.captured_at.tzinfo is None
            or self.valid_until.tzinfo is None
            or self.valid_until <= self.captured_at
        ):
            raise ValueError("Budget price window is invalid")
        return self


class BudgetRunIdentity(StrictEvidenceModel):
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$")
    purpose: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=160)
    price_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["active", "completed"]
    started_at: datetime
    completed_at: datetime | None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> BudgetRunIdentity:
        if self.started_at.tzinfo is None:
            raise ValueError("Budget run start time must be timezone-aware")
        if self.status == "active" and self.completed_at is not None:
            raise ValueError("Active budget run cannot be completed")
        if self.status == "completed" and (
            self.completed_at is None
            or self.completed_at.tzinfo is None
            or self.completed_at < self.started_at
        ):
            raise ValueError("Completed budget run has invalid timestamps")
        return self


class BudgetAttemptBucket(StrictEvidenceModel):
    status: Literal[
        "reserved",
        "uncertain",
        "settled_exact",
        "settled_upper_bound",
    ]
    settlement_mode: Literal["exact", "upper_bound"] | None
    reserved_cny: MoneyCny
    known_cost_cny: MoneyCny | None
    count: int = Field(ge=1)

    @field_validator("count", mode="before")
    @classmethod
    def validate_count_type(cls, value: Any) -> Any:
        if type(value) is not int or value < 1:
            raise ValueError(
                "budget attempt bucket count must be a positive integer"
            )
        return value

    @model_validator(mode="after")
    def validate_attempt_state(self) -> BudgetAttemptBucket:
        reserved = Decimal(self.reserved_cny)
        known = (
            Decimal(self.known_cost_cny)
            if self.known_cost_cny is not None
            else None
        )
        if reserved <= 0:
            raise ValueError(
                "Budget attempt reservation must be positive"
            )
        if self.status == "reserved":
            if self.settlement_mode is not None or known is not None:
                raise ValueError(
                    "Reserved budget attempts cannot claim settlement"
                )
        elif self.status == "uncertain":
            if (self.settlement_mode is None) != (known is None):
                raise ValueError(
                    "Uncertain budget attempt settlement is incomplete"
                )
        else:
            required_mode = (
                "exact"
                if self.status == "settled_exact"
                else "upper_bound"
            )
            if (
                self.settlement_mode != required_mode
                or known is None
                or known > reserved
            ):
                raise ValueError(
                    "Settled budget attempt evidence is inconsistent"
                )
        return self


class BudgetAttemptEvidence(StrictEvidenceModel):
    run: list[BudgetAttemptBucket]
    cumulative: list[BudgetAttemptBucket]


def _attempt_bucket_key(
    bucket: BudgetAttemptBucket,
) -> tuple[str, str | None, str, str | None]:
    return (
        bucket.status,
        bucket.settlement_mode,
        bucket.reserved_cny,
        bucket.known_cost_cny,
    )


def _recompute_attempt_amounts(
    buckets: list[BudgetAttemptBucket],
) -> dict[str, int]:
    committed_units = 0
    settled_units = 0
    attempt_count = 0
    reserved_count = 0
    uncertain_count = 0
    for bucket in buckets:
        reserved_units = cny_to_units(
            Decimal(bucket.reserved_cny)
        )
        known_units = (
            cny_to_units(Decimal(bucket.known_cost_cny))
            if bucket.known_cost_cny is not None
            else None
        )
        attempt_count += bucket.count
        if bucket.status == "reserved":
            reserved_count += bucket.count
        if bucket.status == "uncertain":
            uncertain_count += bucket.count
        if bucket.status in {
            "settled_exact",
            "settled_upper_bound",
        }:
            assert known_units is not None
            committed_units += known_units * bucket.count
            settled_units += known_units * bucket.count
        else:
            committed_units += max(
                reserved_units,
                known_units or reserved_units,
            ) * bucket.count
    return {
        "committed_units": committed_units,
        "settled_units": settled_units,
        "attempt_count": attempt_count,
        "reserved_count": reserved_count,
        "uncertain_count": uncertain_count,
    }


def _require_attempt_amounts(
    *,
    buckets: list[BudgetAttemptBucket],
    amount: BudgetAmountSummary,
    cumulative: bool,
) -> None:
    recomputed = _recompute_attempt_amounts(buckets)
    if (
        cny_to_units(Decimal(amount.committed_cny))
        != recomputed["committed_units"]
        or cny_to_units(Decimal(amount.settled_cny))
        != recomputed["settled_units"]
        or amount.attempt_count != recomputed["attempt_count"]
        or amount.reserved_count != recomputed["reserved_count"]
        or amount.uncertain_count != recomputed["uncertain_count"]
    ):
        raise ValueError(
            "budget totals differ from attempt bucket evidence"
        )
    if cumulative:
        expected_remaining = max(
            0,
            cny_to_units(Decimal(amount.execution_limit_cny))
            - recomputed["committed_units"],
        )
        if cny_to_units(
            Decimal(amount.remaining_execution_cny)
        ) != expected_remaining:
            raise ValueError(
                "budget remaining amount differs from attempt evidence"
            )


class BudgetSummary(StrictEvidenceModel):
    schema_version: Literal["1.0"]
    enforcement_mode: Literal[
        "persistent_sqlite",
        "offline_no_paid_provider",
    ]
    run_status: Literal["active", "completed"] | None = None
    run_identity: BudgetRunIdentity | None = None
    price: BudgetPriceSummary | None
    reservation_cny_per_attempt: MoneyCny
    run: BudgetAmountSummary
    cumulative: BudgetAmountSummary
    attempt_evidence: BudgetAttemptEvidence | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> BudgetSummary:
        if self.enforcement_mode == "persistent_sqlite":
            if self.price is None or self.run_identity is None:
                raise ValueError(
                    "Persistent paid mode requires pricing and run identity"
                )
            if (
                self.run_status != self.run_identity.status
                or self.run_identity.model != self.price.model
                or self.run_identity.price_sha256
                != self.price.snapshot_sha256
            ):
                raise ValueError(
                    "Persistent budget run identity does not match pricing"
                )
        else:
            if (
                self.price is not None
                or self.run_identity is not None
                or self.attempt_evidence is not None
            ):
                raise ValueError(
                    "Offline mode cannot contain paid pricing, identity, "
                    "or attempt evidence"
                )
        if (
            self.run.hard_limit_cny
            != self.cumulative.hard_limit_cny
            or self.run.execution_limit_cny
            != self.cumulative.execution_limit_cny
            or self.run.remaining_execution_cny
            != self.cumulative.remaining_execution_cny
        ):
            raise ValueError("Run and cumulative budget limits differ")
        run_committed = Decimal(self.run.committed_cny)
        run_settled = Decimal(self.run.settled_cny)
        cumulative_committed = Decimal(
            self.cumulative.committed_cny
        )
        cumulative_settled = Decimal(
            self.cumulative.settled_cny
        )
        expected_remaining = max(
            Decimal("0"),
            Decimal(self.cumulative.execution_limit_cny)
            - cumulative_committed,
        )
        if (
            cumulative_committed < run_committed
            or cumulative_settled < run_settled
            or self.cumulative.attempt_count
            < self.run.attempt_count
            or self.cumulative.reserved_count
            < self.run.reserved_count
            or self.cumulative.uncertain_count
            < self.run.uncertain_count
            or Decimal(
                self.cumulative.remaining_execution_cny
            )
            != expected_remaining
        ):
            raise ValueError(
                "Cumulative budget evidence is inconsistent"
            )
        if self.attempt_evidence is not None:
            if any(
                bucket.reserved_cny
                != self.reservation_cny_per_attempt
                for bucket in self.attempt_evidence.run
            ):
                raise ValueError(
                    "Current-run attempt bucket reservation differs "
                    "from the paid guard"
                )
            _require_attempt_amounts(
                buckets=self.attempt_evidence.run,
                amount=self.run,
                cumulative=False,
            )
            _require_attempt_amounts(
                buckets=self.attempt_evidence.cumulative,
                amount=self.cumulative,
                cumulative=True,
            )
            run_buckets: Counter[
                tuple[str, str | None, str, str | None]
            ] = Counter()
            cumulative_buckets: Counter[
                tuple[str, str | None, str, str | None]
            ] = Counter()
            for bucket in self.attempt_evidence.run:
                run_buckets[_attempt_bucket_key(bucket)] += bucket.count
            for bucket in self.attempt_evidence.cumulative:
                cumulative_buckets[
                    _attempt_bucket_key(bucket)
                ] += bucket.count
            if any(
                count > cumulative_buckets[key]
                for key, count in run_buckets.items()
            ):
                raise ValueError(
                    "Current-run attempt buckets are absent from "
                    "cumulative evidence"
                )
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
    phase: Literal["agent", "semantic_judge"] = "agent"
    tool_calls: list[ModelToolRequest]
    finish_reason: str | None
    response_id: str | None
    observed_model: str | None
    usage: dict[str, int] | None
    error_code: str | None
    http_status: int | None
    provider_request_id: str | None
    provider_attempts: int | None

    @field_validator("provider_attempts", mode="before")
    @classmethod
    def validate_provider_attempts(
        cls,
        value: Any,
    ) -> Any:
        if value is None:
            return value
        if type(value) is not int or value < 0:
            raise ValueError(
                "provider_attempts must be a non-negative integer"
            )
        return value

    @field_validator("usage", mode="before")
    @classmethod
    def validate_usage_token_types(
        cls,
        value: Any,
    ) -> Any:
        if value is None:
            return value
        if not isinstance(value, dict) or any(
            not isinstance(key, str)
            or type(token_count) is not int
            or token_count < 0
            for key, token_count in value.items()
        ):
            raise ValueError(
                "usage must map strings to non-negative integers"
            )
        return value


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
        if self.changed is None:
            if (
                self.changed_tables
                or self.before_sha256 is not None
                or self.after_sha256 is not None
            ):
                raise ValueError(
                    "Unknown business state cannot claim hashes or changes"
                )
        elif (
            self.before_sha256 is None
            or self.after_sha256 is None
            or (self.changed and not self.changed_tables)
            or (not self.changed and self.changed_tables)
            or (
                self.changed
                and self.before_sha256 == self.after_sha256
            )
            or (
                not self.changed
                and self.before_sha256 != self.after_sha256
            )
        ):
            raise ValueError("Business-state evidence is inconsistent")
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
        if (
            self.started_at.tzinfo is None
            or self.completed_at.tzinfo is None
            or self.completed_at < self.started_at
        ):
            raise ValueError("Case record timestamps are invalid")
        expected_scores = {
            category: True for category in SCORE_CATEGORIES
        }
        for check in self.score_checks:
            if check.category not in expected_scores:
                raise ValueError("Score check uses an unknown category")
            expected_scores[check.category] = (
                expected_scores[check.category] and check.passed
            )
        expected_checks = [
            check.message for check in self.score_checks if check.passed
        ]
        expected_failures = [
            check.message
            for check in self.score_checks
            if not check.passed
        ]
        if self.scores != expected_scores:
            raise ValueError(
                "Case scores differ from their score checks"
            )
        if (
            self.checks != expected_checks
            or self.failures != expected_failures
        ):
            raise ValueError(
                "Case check and failure lists differ from score checks"
            )
        if (self.status == "passed") != (not self.failures):
            raise ValueError("Case status differs from score failures")
        if self.termination_reason != (
            self.error_code or "completed"
        ):
            raise ValueError(
                "Case termination reason differs from its error"
            )
        return self


def _summary_rate(passed: int, total: int) -> float:
    return round(passed / total, 6) if total else 0.0


def _summary_distribution(
    values: list[int],
) -> dict[str, int | float | None]:
    if not values:
        return {
            "p50": None,
            "p95": None,
            "max": None,
            "total": 0,
        }
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    p50: int | float
    if len(ordered) % 2:
        p50 = ordered[midpoint]
    else:
        p50 = (
            ordered[midpoint - 1] + ordered[midpoint]
        ) / 2
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "p50": p50,
        "p95": ordered[p95_index],
        "max": ordered[-1],
        "total": sum(ordered),
    }


def _recompute_summary_without_budget(
    *,
    run_id: str,
    records: list[ReadonlyCaseRecord],
    planned_trials: int,
) -> dict[str, Any]:
    total = len(records)
    strict_passed = sum(
        record.status == "passed" for record in records
    )
    grouped: dict[str, list[ReadonlyCaseRecord]] = defaultdict(
        list
    )
    for record in records:
        grouped[record.case_id].append(record)
    cases_all_trials_passed = sum(
        len(case_records) == planned_trials
        and all(
            record.status == "passed"
            for record in case_records
        )
        for case_records in grouped.values()
    )
    score_layers: dict[str, dict[str, int | float]] = {}
    for category in SCORE_CATEGORIES:
        passed = sum(
            record.scores[category] for record in records
        )
        score_layers[category] = {
            "passed": passed,
            "failed": total - passed,
            "rate": _summary_rate(passed, total),
        }

    usage: Counter[str] = Counter()
    model_call_latencies: list[int] = []
    model_call_count = 0
    for record in records:
        for call in record.model_calls:
            model_call_count += 1
            model_call_latencies.append(call.latency_ms)
            if call.usage is not None:
                usage.update(call.usage)
    security_passed = sum(
        record.scores["security"] for record in records
    )
    changed_trials = sum(
        record.business_state.changed is True
        for record in records
    )
    unknown_trials = sum(
        record.business_state.changed is None
        for record in records
    )
    errors = Counter(
        record.error_code
        for record in records
        if record.error_code is not None
    )
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "total_cases": len(grouped),
        "planned_trials": planned_trials,
        "total_trials": total,
        "strict": {
            "passed": strict_passed,
            "failed": total - strict_passed,
            "rate": _summary_rate(strict_passed, total),
        },
        "reliability": {
            "k": planned_trials,
            "cases_all_trials_passed": cases_all_trials_passed,
            "case_count": len(grouped),
            "pass_power_k": _summary_rate(
                cases_all_trials_passed,
                len(grouped),
            ),
        },
        "security": {
            "passed": security_passed,
            "failed": total - security_passed,
            "rate": _summary_rate(security_passed, total),
            "all_trials_passed": security_passed == total,
        },
        "score_layers": score_layers,
        "usage": {
            "model_calls": model_call_count,
            **{key: usage[key] for key in sorted(usage)},
        },
        "latency_ms": {
            "case": _summary_distribution(
                [record.duration_ms for record in records]
            ),
            "model_call": _summary_distribution(
                model_call_latencies
            ),
        },
        "business_state": {
            "changed_trials": changed_trials,
            "unknown_trials": unknown_trials,
            "all_trials_unchanged": (
                changed_trials == 0 and unknown_trials == 0
            ),
        },
        "errors": dict(sorted(errors.items())),
    }


def _require_completed_paid_bundle_records(
    *,
    label: str,
    manifest: ReadonlyManifest,
    summary: ReadonlySummary,
    records: list[ReadonlyCaseRecord],
    expected_case_count: int,
    expected_trials: int,
    expected_case_ids: tuple[str, ...] | None = None,
) -> None:
    trials_by_case: dict[str, set[int]] = {}
    for item in records:
        trials_by_case.setdefault(item.case_id, set()).add(
            item.trial
        )
    expected_trial_numbers = set(range(1, expected_trials + 1))
    if (
        len(records) != expected_case_count * expected_trials
        or len(trials_by_case) != expected_case_count
        or any(
            trials != expected_trial_numbers
            for trials in trials_by_case.values()
        )
        or (
            expected_case_ids is not None
            and set(trials_by_case) != set(expected_case_ids)
        )
        or summary.planned_trials != expected_trials
        or summary.total_trials
        != expected_case_count * expected_trials
    ):
        raise ValueError(
            f"{label} evidence is missing canonical case trials"
        )
    budget = summary.budget
    run_budget = budget.run
    cumulative_budget = budget.cumulative
    attempt_evidence = budget.attempt_evidence
    summary_price = budget.price
    if (
        budget.enforcement_mode != "persistent_sqlite"
        or budget.run_status != "completed"
        or summary_price is None
        or attempt_evidence is None
        or run_budget.reserved_count != 0
        or run_budget.uncertain_count != 0
        or cumulative_budget.reserved_count != 0
        or cumulative_budget.uncertain_count != 0
        or run_budget.committed_cny != run_budget.settled_cny
        or cumulative_budget.committed_cny
        != cumulative_budget.settled_cny
        or Decimal(run_budget.committed_cny) > Decimal("18")
        or Decimal(cumulative_budget.committed_cny) > Decimal("18")
    ):
        raise ValueError(
            f"{label} budget is not completed and settled"
        )
    try:
        canonical_price = require_canonical_paid_budget(
            price=summary_price,
            expected_model=manifest.model.requested_model,
            run_hard_limit_cny=run_budget.hard_limit_cny,
            run_execution_limit_cny=run_budget.execution_limit_cny,
            cumulative_hard_limit_cny=(
                cumulative_budget.hard_limit_cny
            ),
            cumulative_execution_limit_cny=(
                cumulative_budget.execution_limit_cny
            ),
        )
        require_canonical_attempt_reservation(
            canonical_price=canonical_price,
            max_output_tokens=(
                manifest.model.generation_config.max_tokens
            ),
            reservation_cny_per_attempt=(
                budget.reservation_cny_per_attempt
            ),
        )
    except CanonicalPricingError as exc:
        raise ValueError(
            f"{label} pricing or reservation is not canonical"
        ) from exc
    if (
        manifest.harness.canonical_price_snapshot_sha256
        != canonical_price_file_sha256()
        or manifest.created_at < canonical_price.captured_at
        or manifest.completed_at > canonical_price.valid_until
    ):
        raise ValueError(
            f"{label} run is outside canonical pricing"
        )
    observed_models: set[str] = set()
    expected_agent_contract_count = len(
        get_read_only_tool_contracts()
    )
    for record in records:
        calls = record.model_calls
        agent_calls = [
            call for call in calls if call.phase == "agent"
        ]
        judge_calls = [
            call
            for call in calls
            if call.phase == "semantic_judge"
        ]
        if (
            not agent_calls
            or len(judge_calls) != 1
            or len(agent_calls) + len(judge_calls) != len(calls)
            or [call.sequence for call in agent_calls]
            != list(range(1, len(agent_calls) + 1))
            or judge_calls[0].sequence != 1
            or judge_calls[0].tool_contract_count != 0
            or bool(judge_calls[0].tool_calls)
            or any(
                call.tool_contract_count
                != expected_agent_contract_count
                for call in agent_calls
            )
        ):
            raise ValueError(
                f"{label} each completed trial requires consecutive "
                "agent calls and one isolated semantic-judge call"
            )
        for call in calls:
            if (
                call.started_at.tzinfo is None
                or not (
                    record.started_at
                    <= call.started_at
                    <= record.completed_at
                )
            ):
                raise ValueError(
                    f"{label} model-call time is outside its trial record"
                )
            if (
                call.status != "success"
                or call.usage is None
                or call.provider_attempts != 1
                or call.observed_model
                != manifest.model.requested_model
                or call.error_code is not None
                or call.http_status is not None
            ):
                raise ValueError(
                    f"{label} model-call usage is not exact"
                )
            observed_models.add(call.observed_model)
    if (
        observed_models != {manifest.model.requested_model}
        or manifest.model.observed_models != sorted(observed_models)
    ):
        raise ValueError(
            f"{label} manifest observed models differ from records"
        )
    model_calls = [
        call for item in records for call in item.model_calls
    ]
    expected_buckets: Counter[
        tuple[str, str, str, str]
    ] = Counter()
    recomputed_cost_units = 0
    try:
        for call in model_calls:
            assert call.usage is not None
            cost = calculate_usage_cost_from_rates(
                rates_cny=canonical_price.rates_cny.model_dump(),
                tokens_per_price_unit=(
                    canonical_price.tokens_per_price_unit
                ),
                usage=call.usage,
            )
            recomputed_cost_units += cost.units
            expected_buckets[
                (
                    (
                        "settled_exact"
                        if cost.mode == "exact"
                        else "settled_upper_bound"
                    ),
                    cost.mode,
                    budget.reservation_cny_per_attempt,
                    format(units_to_cny(cost.units), "f"),
                )
            ] += 1
    except BudgetUsageError as exc:
        raise ValueError(
            f"{label} usage cannot be priced"
        ) from exc
    actual_buckets: Counter[
        tuple[str, str, str, str]
    ] = Counter()
    for bucket in attempt_evidence.run:
        if (
            bucket.status
            not in {"settled_exact", "settled_upper_bound"}
            or bucket.settlement_mode is None
            or bucket.known_cost_cny is None
        ):
            raise ValueError(
                f"{label} current-run attempt is unsettled"
            )
        actual_buckets[
            (
                bucket.status,
                bucket.settlement_mode,
                bucket.reserved_cny,
                bucket.known_cost_cny,
            )
        ] += bucket.count
    provider_attempts = sum(
        call.provider_attempts or 0 for call in model_calls
    )
    cumulative_committed = Decimal(
        cumulative_budget.committed_cny
    )
    expected_remaining = max(
        Decimal("0"),
        Decimal(cumulative_budget.execution_limit_cny)
        - cumulative_committed,
    )
    if (
        actual_buckets != expected_buckets
        or run_budget.attempt_count != provider_attempts
        or cny_to_units(Decimal(run_budget.settled_cny))
        != recomputed_cost_units
        or Decimal(cumulative_budget.remaining_execution_cny)
        != expected_remaining
    ):
        raise ValueError(
            f"{label} attempt buckets or costs differ from records"
        )


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
            or self.manifest.budget.execution_limit_cny
            != self.summary.budget.cumulative.execution_limit_cny
        ):
            raise ValueError("Manifest and summary budget limits differ")
        if (
            self.manifest.budget.reservation_cny_per_attempt
            != self.summary.budget.reservation_cny_per_attempt
        ):
            raise ValueError(
                "Manifest and summary budget reservations differ"
            )
        if (
            self.manifest.budget.run_status
            != self.summary.budget.run_status
        ):
            raise ValueError("Manifest and summary budget status differ")
        budget_identity = self.summary.budget.run_identity
        if self.manifest.budget.enforcement_mode == "persistent_sqlite":
            if (
                budget_identity is None
                or budget_identity.run_id != self.manifest.run_id
                or budget_identity.purpose != self.manifest.purpose
            ):
                raise ValueError(
                    "Manifest and persisted budget run identity differ"
                )
        summary_price = self.summary.budget.price
        if self.manifest.budget.enforcement_mode == "persistent_sqlite":
            if (
                summary_price is None
                or self.manifest.budget.price_snapshot_sha256
                != summary_price.snapshot_sha256
                or self.manifest.budget.price_source_url
                != summary_price.source_url
                or self.manifest.budget.usage_source_url
                != summary_price.usage_source_url
                or self.manifest.budget.price_captured_at
                != summary_price.captured_at
                or self.manifest.budget.price_valid_until
                != summary_price.valid_until
                or summary_price.model
                != self.manifest.model.requested_model
            ):
                raise ValueError(
                    "Manifest and summary pricing evidence differ"
                )
        case_keys = [(item.case_id, item.trial) for item in self.cases]
        trajectory_keys = [
            (item.case_id, item.trial)
            for item in self.trajectories
        ]
        if len(case_keys) != len(set(case_keys)):
            raise ValueError("Duplicate case/trial records")
        if sorted(case_keys) != sorted(trajectory_keys):
            raise ValueError("Case index and trajectories differ")
        cases_by_key = {
            (item.case_id, item.trial): item
            for item in self.cases
        }
        trajectories_by_key = {
            (item.case_id, item.trial): item
            for item in self.trajectories
        }
        if any(
            case.model_dump(mode="json")
            != trajectories_by_key[key].model_dump(mode="json")
            for key, case in cases_by_key.items()
        ):
            raise ValueError("Case records and trajectories differ")
        if self.summary.total_trials != len(self.cases):
            raise ValueError("Summary trial count differs from cases")
        if self.manifest.eval.case_count != len({item.case_id for item in self.cases}):
            raise ValueError("Manifest case count differs from cases")
        if any(
            item.split != self.manifest.eval.split
            for item in (*self.cases, *self.trajectories)
        ):
            raise ValueError("Case split differs from manifest")
        actual_summary = self.summary.model_dump(mode="json")
        actual_summary.pop("budget")
        expected_summary = _recompute_summary_without_budget(
            run_id=self.manifest.run_id,
            records=self.cases,
            planned_trials=self.summary.planned_trials,
        )
        if actual_summary != expected_summary:
            raise ValueError(
                "Summary differs from the evidence records"
            )
        if self.manifest.purpose == "diagnostic":
            require_completed_diagnostic_evidence(
                label="diagnostic",
                budget=self.summary.budget.model_dump(mode="json"),
                records=[
                    record.model_dump(mode="python")
                    for record in self.cases
                ],
                requested_model=self.manifest.model.requested_model,
                observed_models=self.manifest.model.observed_models,
                max_output_tokens=(
                    self.manifest.model.generation_config.max_tokens
                ),
                run_id=self.manifest.run_id,
                started_at=self.manifest.created_at,
                completed_at=self.manifest.completed_at,
                canonical_price_snapshot_sha256=(
                    self.manifest.harness
                    .canonical_price_snapshot_sha256
                ),
            )
        if self.manifest.purpose == "dev_repeat":
            contract = nonformal_paid_contract("dev_repeat")
            _require_completed_paid_bundle_records(
                label="dev_repeat",
                manifest=self.manifest,
                summary=self.summary,
                records=self.cases,
                expected_case_count=contract.case_count,
                expected_trials=contract.planned_trials,
                expected_case_ids=contract.case_ids,
            )
        if (
            self.manifest.purpose == "holdout_formal"
            and self.manifest.schema_version == "2.0"
        ):
            _require_completed_paid_bundle_records(
                label="formal v2",
                manifest=self.manifest,
                summary=self.summary,
                records=self.cases,
                expected_case_count=20,
                expected_trials=4,
            )
            trials_by_case: dict[str, set[int]] = {}
            for item in self.cases:
                trials_by_case.setdefault(item.case_id, set()).add(
                    item.trial
                )
            observed_models = {
                call.observed_model
                for item in self.cases
                for call in item.model_calls
                if call.observed_model is not None
            }
            provider_attempts = sum(
                call.provider_attempts or 0
                for item in self.cases
                for call in item.model_calls
            )
            model_calls = [
                call
                for item in self.cases
                for call in item.model_calls
            ]
            run_budget = self.summary.budget.run
            cumulative_budget = self.summary.budget.cumulative
            if summary_price is None:
                raise ValueError(
                    "Formal v2 evidence requires exact pricing"
                )
            try:
                canonical_price = require_canonical_paid_budget(
                    price=summary_price,
                    expected_model=(
                        self.manifest.model.requested_model
                    ),
                    run_hard_limit_cny=(
                        run_budget.hard_limit_cny
                    ),
                    run_execution_limit_cny=(
                        run_budget.execution_limit_cny
                    ),
                    cumulative_hard_limit_cny=(
                        cumulative_budget.hard_limit_cny
                    ),
                    cumulative_execution_limit_cny=(
                        cumulative_budget.execution_limit_cny
                    ),
                )
                require_canonical_attempt_reservation(
                    canonical_price=canonical_price,
                    max_output_tokens=(
                        self.manifest.model.generation_config.max_tokens
                    ),
                    reservation_cny_per_attempt=(
                        self.summary.budget
                        .reservation_cny_per_attempt
                    ),
                )
            except CanonicalPricingError as exc:
                raise ValueError(
                    "Formal v2 pricing or reservation is not canonical"
                ) from exc
            if (
                self.manifest.harness
                .canonical_price_snapshot_sha256
                != canonical_price_file_sha256()
                or self.manifest.created_at
                < canonical_price.captured_at
                or self.manifest.completed_at
                > canonical_price.valid_until
            ):
                raise ValueError(
                    "Formal v2 run is outside its canonical pricing contract"
                )
            if any(
                call.status != "success"
                or call.usage is None
                or call.provider_attempts != 1
                for call in model_calls
            ):
                raise ValueError(
                    "Formal v2 evidence requires exact call usage"
                )
            try:
                recomputed_cost_units = sum(
                    calculate_usage_cost_from_rates(
                        rates_cny=canonical_price.rates_cny.model_dump(),
                        tokens_per_price_unit=(
                            canonical_price.tokens_per_price_unit
                        ),
                        usage=call.usage,
                    ).units
                    for call in model_calls
                    if call.usage is not None
                )
            except BudgetUsageError as exc:
                raise ValueError(
                    "Formal v2 model-call usage could not be priced"
                ) from exc
            cumulative_committed = Decimal(
                cumulative_budget.committed_cny
            )
            run_committed = Decimal(run_budget.committed_cny)
            expected_remaining = Decimal(
                cumulative_budget.execution_limit_cny
            ) - cumulative_committed
            if (
                len(self.cases) != 80
                or len(trials_by_case) != 20
                or any(
                    trials != {1, 2, 3, 4}
                    for trials in trials_by_case.values()
                )
                or self.summary.planned_trials != 4
                or self.summary.total_trials != 80
                or observed_models
                != {self.manifest.model.requested_model}
                or run_budget.attempt_count != provider_attempts
                or run_budget.reserved_count != 0
                or run_budget.uncertain_count != 0
                or cumulative_budget.reserved_count != 0
                or cumulative_budget.uncertain_count != 0
                or run_budget.committed_cny
                != run_budget.settled_cny
                or cumulative_budget.committed_cny
                != cumulative_budget.settled_cny
                or cny_to_units(Decimal(run_budget.settled_cny))
                != recomputed_cost_units
                or cumulative_committed < run_committed
                or cumulative_committed
                > Decimal(
                    cumulative_budget.execution_limit_cny
                )
                or Decimal(cumulative_budget.remaining_execution_cny)
                != expected_remaining
            ):
                raise ValueError(
                    "Formal v2 evidence failed cross-validation"
                )
        return self


def validate_readonly_payload(
    payload: Mapping[str, Any],
) -> ReadonlyEvidenceBundle:
    return ReadonlyEvidenceBundle.model_validate(payload)


def validate_readonly_bundle(bundle_path: Path) -> ReadonlyEvidenceBundle:
    bundle = validate_readonly_payload(verify_eval_bundle(bundle_path))
    if (
        bundle.manifest.purpose == "holdout_formal"
        and bundle.manifest.schema_version == "2.0"
    ):
        verify_private_eval_bundle_permissions(bundle_path)
    return bundle
