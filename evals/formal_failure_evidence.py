from __future__ import annotations

import stat
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.deepseek_budget import (
    BudgetUsageError,
    calculate_usage_cost,
    cny_to_units,
)
from evals.canonical_pricing import (
    CanonicalPricingError,
    require_canonical_attempt_reservation,
    require_canonical_paid_budget,
)
from evals.evidence import verify_eval_bundle, write_eval_bundle
from evals.evidence_schema import (
    ArtifactPaths,
    BudgetSummary,
    IntegrityIndex,
    ReadonlyCaseRecord,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
FailureStage = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$"),
]
FailureCode = Annotated[
    str,
    Field(pattern=r"^[A-Z][A-Z0-9_]{2,95}$"),
]


class FormalFailureEvidenceError(RuntimeError):
    """A failed-attempt bundle violates its private storage contract."""


class StrictFailureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FormalFailureSource(StrictFailureModel):
    git_commit: GitCommit
    git_dirty: Literal[False]
    source_tree_sha256: Sha256


class FormalFailureCaseSet(StrictFailureModel):
    name: Literal["readonly-holdout-v2"]
    sha256: Sha256
    planned_case_count: Literal[20]
    planned_trials: Literal[4]


class FormalFailureHoldoutBindings(StrictFailureModel):
    declaration_manifest_sha256: Sha256
    lock_start_receipt_sha256: Sha256
    declared_harness_sha256: Sha256
    runtime_harness_sha256: Sha256

    @model_validator(mode="after")
    def require_declared_runtime_harness(self) -> FormalFailureHoldoutBindings:
        if self.declared_harness_sha256 != self.runtime_harness_sha256:
            raise ValueError(
                "Declared and runtime harness fingerprints differ"
            )
        return self


class FormalFailureContext(StrictFailureModel):
    """Frozen identity shared by the failed-attempt manifest and receipts."""

    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$")
    created_at: datetime
    failed_at: datetime
    failure_stage: FailureStage
    failure_code: FailureCode
    max_output_tokens: int = Field(ge=1)
    source: FormalFailureSource
    case_set: FormalFailureCaseSet
    formal_holdout: FormalFailureHoldoutBindings

    @model_validator(mode="after")
    def validate_timestamps(self) -> FormalFailureContext:
        if (
            self.created_at.tzinfo is None
            or self.failed_at.tzinfo is None
            or self.failed_at < self.created_at
        ):
            raise ValueError(
                "Failed-attempt timestamps must be aware and ordered"
            )
        return self


class FormalFailureManifest(StrictFailureModel):
    schema_version: Literal["1.0"]
    artifact_kind: Literal["formal_holdout_failed_attempt"]
    status: Literal["failed"]
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$")
    created_at: datetime
    failed_at: datetime
    failure_stage: FailureStage
    failure_code: FailureCode
    max_output_tokens: int = Field(ge=1)
    source: FormalFailureSource
    case_set: FormalFailureCaseSet
    formal_holdout: FormalFailureHoldoutBindings
    completed_record_count: int = Field(ge=0, le=80)
    artifacts: ArtifactPaths

    @model_validator(mode="after")
    def validate_timestamps(self) -> FormalFailureManifest:
        if (
            self.created_at.tzinfo is None
            or self.failed_at.tzinfo is None
            or self.failed_at < self.created_at
        ):
            raise ValueError(
                "Failed-attempt timestamps must be aware and ordered"
            )
        return self


class FormalFailurePartialSummary(StrictFailureModel):
    completed_record_count: int = Field(ge=0, le=80)
    unique_case_count: int = Field(ge=0, le=20)
    passed_record_count: int = Field(ge=0, le=80)
    failed_record_count: int = Field(ge=0, le=80)
    model_call_count: NonNegativeInt
    provider_attempt_count: NonNegativeInt
    usage: dict[str, NonNegativeInt]
    business_state_changed_count: int = Field(ge=0, le=80)
    business_state_unknown_count: int = Field(ge=0, le=80)
    error_counts: dict[str, NonNegativeInt]

    @model_validator(mode="after")
    def validate_record_counts(self) -> FormalFailurePartialSummary:
        if (
            self.passed_record_count + self.failed_record_count
            != self.completed_record_count
            or self.unique_case_count > self.completed_record_count
            or (
                self.business_state_changed_count
                + self.business_state_unknown_count
                > self.completed_record_count
            )
        ):
            raise ValueError("Partial summary record counts are inconsistent")
        return self


class FormalFailureSummary(StrictFailureModel):
    schema_version: Literal["1.0"]
    artifact_kind: Literal["formal_holdout_failed_attempt"]
    status: Literal["failed"]
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,79}$")
    failure_stage: FailureStage
    failure_code: FailureCode
    completed_record_count: int = Field(ge=0, le=80)
    record_capture_status: Literal["captured", "unavailable"]
    partial: FormalFailurePartialSummary | None
    budget_capture_status: Literal["captured", "unavailable"]
    budget: BudgetSummary | None
    budget_attempt_delta: NonNegativeInt | None
    budget_limit_breached: bool | None

    @model_validator(mode="after")
    def validate_capture_states(self) -> FormalFailureSummary:
        if self.record_capture_status == "captured":
            if self.partial is None:
                raise ValueError(
                    "Captured records require a partial summary"
                )
        elif (
            self.partial is not None
            or self.completed_record_count != 0
        ):
            raise ValueError(
                "Unavailable records cannot claim partial results"
            )
        if (self.budget_capture_status == "captured") != (
            self.budget is not None
        ):
            raise ValueError(
                "Budget capture status does not match budget evidence"
            )
        if self.budget is None:
            if (
                self.budget_attempt_delta is not None
                or self.budget_limit_breached is not None
            ):
                raise ValueError(
                    "Unavailable budget cannot claim derived evidence"
                )
            return self
        if (
            self.budget.enforcement_mode != "persistent_sqlite"
            or self.budget.run_identity is None
            or self.budget.price is None
            or self.budget.attempt_evidence is None
        ):
            raise ValueError(
                "Captured formal failure budget must include persistent "
                "attempt evidence"
            )
        captured_provider_attempts = (
            self.partial.provider_attempt_count
            if self.partial is not None
            else 0
        )
        budget_attempts = self.budget.run.attempt_count
        if budget_attempts < captured_provider_attempts:
            raise ValueError(
                "Captured budget underreports provider attempts"
            )
        expected_attempt_delta = (
            budget_attempts - captured_provider_attempts
        )
        expected_limit_breached = Decimal(
            self.budget.cumulative.committed_cny
        ) > Decimal(
            self.budget.cumulative.execution_limit_cny
        )
        if (
            self.budget_attempt_delta != expected_attempt_delta
            or self.budget_limit_breached
            is not expected_limit_breached
        ):
            raise ValueError(
                "Failed-attempt budget derivations are inconsistent"
            )
        return self


def _bucket_key(bucket: Any) -> tuple[str, str | None, str, str | None]:
    return (
        bucket.status,
        bucket.settlement_mode,
        bucket.reserved_cny,
        bucket.known_cost_cny,
    )


def _recompute_attempt_amounts(
    buckets: Sequence[Any],
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
        count = bucket.count
        attempt_count += count
        if bucket.status == "reserved":
            reserved_count += count
        if bucket.status == "uncertain":
            uncertain_count += count
        if bucket.status in {
            "settled_exact",
            "settled_upper_bound",
        }:
            assert known_units is not None
            committed_units += known_units * count
            settled_units += known_units * count
        else:
            committed_units += max(
                reserved_units,
                known_units or reserved_units,
            ) * count
    return {
        "committed_units": committed_units,
        "settled_units": settled_units,
        "attempt_count": attempt_count,
        "reserved_count": reserved_count,
        "uncertain_count": uncertain_count,
    }


def _require_attempt_totals(
    *,
    buckets: Sequence[Any],
    amount: Any,
    reservation_cny: str | None,
    cumulative: bool,
) -> None:
    if reservation_cny is not None and any(
        bucket.reserved_cny != reservation_cny for bucket in buckets
    ):
        raise ValueError(
            "Failed-attempt bucket reservation differs from the paid guard"
        )
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
            "Failed-attempt budget totals differ from attempt evidence"
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
                "Failed-attempt remaining budget is inconsistent"
            )


class FormalFailureEvidenceBundle(StrictFailureModel):
    manifest: FormalFailureManifest
    cases: list[ReadonlyCaseRecord]
    summary: FormalFailureSummary
    trajectories: list[ReadonlyCaseRecord]
    integrity: IntegrityIndex

    @model_validator(mode="after")
    def cross_validate(self) -> FormalFailureEvidenceBundle:
        manifest = self.manifest
        summary = self.summary
        if (
            manifest.run_id != summary.run_id
            or manifest.failure_stage != summary.failure_stage
            or manifest.failure_code != summary.failure_code
        ):
            raise ValueError(
                "Failed-attempt manifest and summary identity differ"
            )
        if summary.budget is not None:
            budget_identity = summary.budget.run_identity
            budget_price = summary.budget.price
            if (
                budget_identity is None
                or budget_price is None
                or budget_identity.run_id != manifest.run_id
                or budget_identity.purpose != "holdout_formal"
            ):
                raise ValueError(
                    "Failed-attempt budget identity differs from the run"
                )
            try:
                canonical_price = require_canonical_paid_budget(
                    price=budget_price,
                    expected_model=budget_identity.model,
                    run_hard_limit_cny=(
                        summary.budget.run.hard_limit_cny
                    ),
                    run_execution_limit_cny=(
                        summary.budget.run.execution_limit_cny
                    ),
                    cumulative_hard_limit_cny=(
                        summary.budget.cumulative.hard_limit_cny
                    ),
                    cumulative_execution_limit_cny=(
                        summary.budget.cumulative.execution_limit_cny
                    ),
                )
            except CanonicalPricingError as exc:
                raise ValueError(
                    "Failed-attempt budget pricing is not canonical"
                ) from exc
            try:
                require_canonical_attempt_reservation(
                    canonical_price=canonical_price,
                    max_output_tokens=manifest.max_output_tokens,
                    reservation_cny_per_attempt=(
                        summary.budget.reservation_cny_per_attempt
                    ),
                )
            except CanonicalPricingError as exc:
                raise ValueError(
                    "Failed-attempt reservation is not canonical"
                ) from exc
            attempt_evidence = summary.budget.attempt_evidence
            if attempt_evidence is None:
                raise ValueError(
                    "Failed-attempt budget lacks attempt evidence"
                )
            _require_attempt_totals(
                buckets=attempt_evidence.run,
                amount=summary.budget.run,
                reservation_cny=(
                    summary.budget.reservation_cny_per_attempt
                ),
                cumulative=False,
            )
            _require_attempt_totals(
                buckets=attempt_evidence.cumulative,
                amount=summary.budget.cumulative,
                reservation_cny=None,
                cumulative=True,
            )
            run_bucket_counts: Counter[
                tuple[str, str | None, str, str | None]
            ] = Counter()
            cumulative_bucket_counts: Counter[
                tuple[str, str | None, str, str | None]
            ] = Counter()
            for bucket in attempt_evidence.run:
                run_bucket_counts[_bucket_key(bucket)] += bucket.count
            for bucket in attempt_evidence.cumulative:
                cumulative_bucket_counts[_bucket_key(bucket)] += (
                    bucket.count
                )
            if any(
                count > cumulative_bucket_counts[key]
                for key, count in run_bucket_counts.items()
            ):
                raise ValueError(
                    "Failed-attempt run buckets are absent from cumulative "
                    "attempt evidence"
                )
            available_settled_attempts: Counter[
                tuple[str, str, int]
            ] = Counter()
            for bucket in attempt_evidence.run:
                if bucket.status not in {
                    "settled_exact",
                    "settled_upper_bound",
                }:
                    continue
                assert bucket.settlement_mode is not None
                assert bucket.known_cost_cny is not None
                available_settled_attempts[
                    (
                        bucket.status,
                        bucket.settlement_mode,
                        cny_to_units(
                            Decimal(bucket.known_cost_cny)
                        ),
                    )
                ] += bucket.count
            observable_usage_units = 0
            priced_attempt_count = 0
            captured_provider_attempts = 0
            for case in self.cases:
                for call in case.model_calls:
                    provider_attempts = call.provider_attempts
                    if call.status == "success":
                        if (
                            call.usage is None
                            or provider_attempts is None
                            or isinstance(provider_attempts, bool)
                            or provider_attempts < 1
                            or call.error_code is not None
                            or call.http_status is not None
                            or call.observed_model
                            != budget_identity.model
                        ):
                            raise ValueError(
                                "Failed-attempt success model-call "
                                "protocol is inconsistent"
                            )
                        try:
                            priced_usage = calculate_usage_cost(
                                canonical_price,
                                call.usage,
                            )
                        except BudgetUsageError as exc:
                            raise ValueError(
                                "Failed-attempt observable usage "
                                "cannot be priced"
                            ) from exc
                        bucket_key = (
                            (
                                "settled_exact"
                                if priced_usage.mode == "exact"
                                else "settled_upper_bound"
                            ),
                            priced_usage.mode,
                            priced_usage.units,
                        )
                        if available_settled_attempts[bucket_key] < 1:
                            raise ValueError(
                                "Failed-attempt observable usage has no "
                                "matching settled ledger bucket"
                            )
                        available_settled_attempts[bucket_key] -= 1
                        observable_usage_units += priced_usage.units
                        priced_attempt_count += 1
                        captured_provider_attempts += provider_attempts
                    elif (
                        call.usage is not None
                        or call.error_code is None
                        or call.tool_calls
                        or call.finish_reason is not None
                        or call.response_id is not None
                        or call.observed_model is not None
                    ):
                        raise ValueError(
                            "Failed-attempt error model-call "
                            "protocol is inconsistent"
                        )
                    elif provider_attempts is not None:
                        if (
                            isinstance(provider_attempts, bool)
                            or provider_attempts < 0
                        ):
                            raise ValueError(
                                "Failed-attempt error model-call "
                                "attempt count is invalid"
                            )
                        captured_provider_attempts += provider_attempts
            run_committed_units = cny_to_units(
                Decimal(summary.budget.run.committed_cny)
            )
            run_settled_units = cny_to_units(
                Decimal(summary.budget.run.settled_cny)
            )
            if (
                run_committed_units < observable_usage_units
                or run_settled_units < observable_usage_units
            ):
                raise ValueError(
                    "Failed-attempt budget hides observable usage cost"
                )
            unknown_observed_attempts = (
                captured_provider_attempts - priced_attempt_count
            )
            unfinalized_attempts = sum(
                bucket.count
                for bucket in attempt_evidence.run
                if bucket.status in {"reserved", "uncertain"}
            )
            if unfinalized_attempts < unknown_observed_attempts:
                raise ValueError(
                    "Failed-attempt retries or errors lack reserved or "
                    "uncertain ledger evidence"
                )
            if (
                summary.budget.run.attempt_count
                == priced_attempt_count
                and (
                    run_committed_units != observable_usage_units
                    or run_settled_units != observable_usage_units
                    or summary.budget.run.reserved_count != 0
                    or summary.budget.run.uncertain_count != 0
                )
            ):
                raise ValueError(
                    "Fully observed failed-attempt budget commitment "
                    "differs from canonical usage cost"
                )

        case_keys = [(item.case_id, item.trial) for item in self.cases]
        trajectory_keys = [
            (item.case_id, item.trial) for item in self.trajectories
        ]
        if (
            len(case_keys) != len(set(case_keys))
            or len(trajectory_keys) != len(set(trajectory_keys))
        ):
            raise ValueError("Duplicate failed-attempt case/trial records")
        if sorted(case_keys) != sorted(trajectory_keys):
            raise ValueError(
                "Failed-attempt case index and trajectories differ"
            )
        trajectories_by_key = {
            (item.case_id, item.trial): item
            for item in self.trajectories
        }
        if any(
            item.model_dump(mode="json")
            != trajectories_by_key[key].model_dump(mode="json")
            for key, item in zip(case_keys, self.cases, strict=True)
        ):
            raise ValueError(
                "Failed-attempt case records and trajectories differ"
            )

        completed_record_count = len(self.cases)
        unique_case_count = len({item.case_id for item in self.cases})
        if (
            manifest.completed_record_count != completed_record_count
            or summary.completed_record_count != completed_record_count
            or completed_record_count
            > (
                manifest.case_set.planned_case_count
                * manifest.case_set.planned_trials
            )
            or unique_case_count > manifest.case_set.planned_case_count
        ):
            raise ValueError(
                "Failed-attempt completed record counts differ"
            )
        if summary.record_capture_status == "unavailable" and self.cases:
            raise ValueError(
                "Unavailable record capture cannot contain records"
            )
        if any(
            item.split != "holdout"
            or item.trial > manifest.case_set.planned_trials
            or item.started_at.tzinfo is None
            or item.completed_at.tzinfo is None
            or item.completed_at < item.started_at
            or item.completed_at > manifest.failed_at
            for item in (*self.cases, *self.trajectories)
        ):
            raise ValueError(
                "Failed-attempt records violate holdout or time bounds"
            )

        expected_partial = (
            _summarize_partial_records(self.cases)
            if summary.record_capture_status == "captured"
            else None
        )
        if summary.partial != expected_partial:
            raise ValueError(
                "Failed-attempt partial summary differs from records"
            )
        return self


def _summarize_partial_records(
    records: Sequence[ReadonlyCaseRecord],
) -> FormalFailurePartialSummary:
    usage: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    model_call_count = 0
    provider_attempt_count = 0
    for record in records:
        if record.error_code is not None:
            error_counts[record.error_code] += 1
        for call in record.model_calls:
            model_call_count += 1
            provider_attempt_count += call.provider_attempts or 0
            if call.usage is not None:
                usage.update(call.usage)
    return FormalFailurePartialSummary(
        completed_record_count=len(records),
        unique_case_count=len({item.case_id for item in records}),
        passed_record_count=sum(
            item.status == "passed" for item in records
        ),
        failed_record_count=sum(
            item.status == "failed" for item in records
        ),
        model_call_count=model_call_count,
        provider_attempt_count=provider_attempt_count,
        usage=dict(sorted(usage.items())),
        business_state_changed_count=sum(
            item.business_state.changed is True for item in records
        ),
        business_state_unknown_count=sum(
            item.business_state.changed is None for item in records
        ),
        error_counts=dict(sorted(error_counts.items())),
    )


def _artifact_paths() -> ArtifactPaths:
    return ArtifactPaths(
        cases="cases.jsonl",
        summary="summary.json",
        trajectories="trajectories/",
        integrity="integrity.json",
    )


def _require_private_bundle_permissions(bundle_path: Path) -> None:
    try:
        paths = (bundle_path, *bundle_path.rglob("*"))
        for path in paths:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise FormalFailureEvidenceError(
                    "Failed-attempt evidence cannot contain symlinks"
                )
            if stat.S_ISDIR(mode):
                expected_mode = 0o700
            elif stat.S_ISREG(mode):
                expected_mode = 0o600
            else:
                raise FormalFailureEvidenceError(
                    "Failed-attempt evidence contains a special file"
                )
            if stat.S_IMODE(mode) != expected_mode:
                raise FormalFailureEvidenceError(
                    "Failed-attempt evidence must remain owner-only"
                )
    except OSError as exc:
        raise FormalFailureEvidenceError(
            "Failed-attempt evidence metadata is unreadable"
        ) from exc


def validate_formal_failure_payload(
    payload: Mapping[str, Any],
) -> FormalFailureEvidenceBundle:
    """Validate the strict failed-attempt schema and cross-file contract."""

    return FormalFailureEvidenceBundle.model_validate(payload)


def validate_formal_failure_bundle(
    bundle_path: Path,
) -> FormalFailureEvidenceBundle:
    """Verify private modes, indexed bytes, schema, and record equality."""

    _require_private_bundle_permissions(bundle_path)
    return validate_formal_failure_payload(verify_eval_bundle(bundle_path))


def write_formal_failure_bundle(
    *,
    output_root: Path,
    context: FormalFailureContext | Mapping[str, Any],
    case_records: Sequence[ReadonlyCaseRecord | Mapping[str, Any]],
    records_captured: bool,
    budget_summary: BudgetSummary | Mapping[str, Any] | None,
    secret_values: Sequence[str] = (),
) -> Path:
    """Atomically write one private formal failed-attempt evidence bundle."""

    frozen_context = FormalFailureContext.model_validate(context)
    records = [
        ReadonlyCaseRecord.model_validate(record)
        for record in case_records
    ]
    if not records_captured and records:
        raise ValueError(
            "Unavailable record capture cannot contain case records"
        )
    budget = (
        BudgetSummary.model_validate(budget_summary)
        if budget_summary is not None
        else None
    )
    partial_summary = (
        _summarize_partial_records(records)
        if records_captured
        else None
    )
    completed_record_count = len(records)
    manifest = FormalFailureManifest(
        schema_version="1.0",
        artifact_kind="formal_holdout_failed_attempt",
        status="failed",
        completed_record_count=completed_record_count,
        artifacts=_artifact_paths(),
        **frozen_context.model_dump(),
    )
    summary = FormalFailureSummary(
        schema_version="1.0",
        artifact_kind="formal_holdout_failed_attempt",
        status="failed",
        run_id=frozen_context.run_id,
        failure_stage=frozen_context.failure_stage,
        failure_code=frozen_context.failure_code,
        completed_record_count=completed_record_count,
        record_capture_status=(
            "captured" if records_captured else "unavailable"
        ),
        partial=partial_summary,
        budget_capture_status=(
            "captured" if budget is not None else "unavailable"
        ),
        budget=budget,
        budget_attempt_delta=(
            budget.run.attempt_count
            - (
                partial_summary.provider_attempt_count
                if partial_summary is not None
                else 0
            )
            if budget is not None
            else None
        ),
        budget_limit_breached=(
            Decimal(budget.cumulative.committed_cny)
            > Decimal(
                budget.cumulative.execution_limit_cny
            )
            if budget is not None
            else None
        ),
    )

    record_payloads = [
        record.model_dump(mode="json") for record in records
    ]
    validate_formal_failure_payload(
        {
            "manifest": manifest.model_dump(mode="json"),
            "cases": record_payloads,
            "summary": summary.model_dump(mode="json"),
            "trajectories": record_payloads,
            "integrity": {
                "schema_version": "1.0",
                "algorithm": "sha256",
                "files": {},
            },
        }
    )
    bundle_path = write_eval_bundle(
        output_root=output_root,
        run_id=frozen_context.run_id,
        manifest=manifest.model_dump(mode="json"),
        case_records=record_payloads,
        summary=summary.model_dump(mode="json"),
        secret_values=secret_values,
    )
    validate_formal_failure_bundle(bundle_path)
    return bundle_path
