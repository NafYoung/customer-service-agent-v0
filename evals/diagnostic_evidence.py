from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

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
from evals.nonformal_paid_contract import nonformal_paid_contract

_LOCAL_ZERO_CALL_ERRORS = {
    "EMPTY_USER_MESSAGE",
    "TOOL_VALIDATION_INVARIANT",
    "UNEXPECTED_EVAL_ERROR",
}
_SETTLED_STATUSES = {
    "settled_exact",
    "settled_upper_bound",
}


def _as_datetime(value: Any, *, label: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp must be timezone-aware")
    return parsed


def _as_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} evidence is invalid")
    return value


def _counted_attempts(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} provider attempts are invalid")
    return value


def _require_offline_attempt_count(value: Any, *, label: str) -> None:
    if value is not None and (type(value) is not int or value != 0):
        raise ValueError(f"{label} offline call cannot claim a provider attempt")


def _require_call_protocol(
    *,
    label: str,
    records: Sequence[Mapping[str, Any]],
    requested_model: str,
    paid: bool,
) -> tuple[
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
    set[str],
]:
    expected_tool_contract_count = len(get_read_only_tool_contracts())
    success_calls: list[Mapping[str, Any]] = []
    error_calls: list[Mapping[str, Any]] = []
    observed_models: set[str] = set()
    for record in records:
        record_started = _as_datetime(
            record["started_at"],
            label=f"{label} record",
        )
        record_completed = _as_datetime(
            record["completed_at"],
            label=f"{label} record",
        )
        calls = [
            _as_mapping(call, label=f"{label} model call")
            for call in record["model_calls"]
        ]
        if (
            any(call["phase"] != "agent" for call in calls)
            or [call["sequence"] for call in calls] != list(range(1, len(calls) + 1))
            or any(
                call["tool_contract_count"] != expected_tool_contract_count
                for call in calls
            )
        ):
            raise ValueError(
                f"{label} requires consecutive agent calls with the "
                "read-only tool contract"
            )
        if not calls:
            if (
                record["status"] == "passed"
                or record["error_code"] not in _LOCAL_ZERO_CALL_ERRORS
            ):
                raise ValueError(
                    f"{label} zero-call records require an explicit local failure"
                )
            continue
        record_successes = 0
        record_error_codes: set[str] = set()
        for call in calls:
            call_started = _as_datetime(
                call["started_at"],
                label=f"{label} model call",
            )
            if not record_started <= call_started <= record_completed:
                raise ValueError(f"{label} model call is outside its record window")
            if call["status"] == "success":
                provider_attempts = call["provider_attempts"]
                if paid:
                    attempts = _counted_attempts(
                        provider_attempts,
                        label=f"{label} success call",
                    )
                    if attempts < 1:
                        raise ValueError(
                            f"{label} paid success call requires a provider attempt"
                        )
                else:
                    _require_offline_attempt_count(
                        provider_attempts,
                        label=label,
                    )
                if (
                    call["usage"] is None
                    or call["observed_model"] is None
                    or call["error_code"] is not None
                    or call["http_status"] is not None
                ):
                    raise ValueError(f"{label} success call protocol is inconsistent")
                observed_model = call["observed_model"]
                if not isinstance(observed_model, str):
                    raise ValueError(f"{label} observed model is invalid")
                if paid and observed_model != requested_model:
                    raise ValueError(
                        f"{label} paid success call observed the wrong model"
                    )
                if not paid and (
                    observed_model == requested_model
                    or "deepseek" in observed_model.casefold()
                ):
                    raise ValueError(f"{label} offline evidence cannot claim DeepSeek")
                record_successes += 1
                observed_models.add(observed_model)
                success_calls.append(call)
                continue
            provider_attempts = call["provider_attempts"]
            if paid:
                _counted_attempts(
                    provider_attempts,
                    label=f"{label} error call",
                )
            else:
                _require_offline_attempt_count(
                    provider_attempts,
                    label=label,
                )
            if (
                call["usage"] is not None
                or call["observed_model"] is not None
                or call["finish_reason"] is not None
                or call["response_id"] is not None
                or call["tool_calls"]
                or call["error_code"] is None
            ):
                raise ValueError(f"{label} error call protocol is inconsistent")
            record_error_codes.add(call["error_code"])
            error_calls.append(call)
        if record["status"] == "passed":
            if (
                record_successes < 1
                or record_error_codes
                or record["error_code"] is not None
            ):
                raise ValueError(
                    f"{label} passed record requires successful calls only"
                )
        elif (record_successes == 0 and not record_error_codes) or (
            record_error_codes and record["error_code"] not in record_error_codes
        ):
            raise ValueError(f"{label} failed record does not match its model calls")
    return success_calls, error_calls, observed_models


def _require_offline_diagnostic(
    *,
    label: str,
    budget: Mapping[str, Any],
    success_calls: Sequence[Mapping[str, Any]],
    error_calls: Sequence[Mapping[str, Any]],
) -> None:
    run = _as_mapping(budget["run"], label=f"{label} run budget")
    cumulative = _as_mapping(
        budget["cumulative"],
        label=f"{label} cumulative budget",
    )
    zero_fields = (
        "committed_cny",
        "settled_cny",
        "attempt_count",
        "reserved_count",
        "uncertain_count",
    )
    if (
        budget["reservation_cny_per_attempt"] != "0"
        or any(
            Decimal(str(amount[field])) != 0
            for amount in (run, cumulative)
            for field in zero_fields
        )
        or any(
            call["provider_attempts"] is not None and call["provider_attempts"] != 0
            for call in (*success_calls, *error_calls)
        )
    ):
        raise ValueError(f"{label} offline evidence contains paid provider attempts")


def _require_paid_diagnostic(
    *,
    label: str,
    budget: Mapping[str, Any],
    success_calls: Sequence[Mapping[str, Any]],
    error_calls: Sequence[Mapping[str, Any]],
    requested_model: str,
    max_output_tokens: int,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    canonical_price_snapshot_sha256: str | None,
) -> None:
    run = _as_mapping(budget["run"], label=f"{label} run budget")
    cumulative = _as_mapping(
        budget["cumulative"],
        label=f"{label} cumulative budget",
    )
    identity = _as_mapping(
        budget["run_identity"],
        label=f"{label} budget identity",
    )
    price = _as_mapping(
        budget["price"],
        label=f"{label} budget price",
    )
    attempt_evidence = _as_mapping(
        budget["attempt_evidence"],
        label=f"{label} attempt evidence",
    )
    run_buckets = [
        _as_mapping(bucket, label=f"{label} attempt bucket")
        for bucket in attempt_evidence["run"]
    ]
    if (
        budget["run_status"] != "completed"
        or identity["status"] != "completed"
        or identity["run_id"] != run_id
        or identity["purpose"] != "diagnostic"
        or identity["model"] != requested_model
        or identity["completed_at"] is None
        or run["reserved_count"] != 0
        or any(bucket["status"] == "reserved" for bucket in run_buckets)
        or Decimal(run["committed_cny"]) > Decimal("18")
        or Decimal(cumulative["committed_cny"]) > Decimal("18")
    ):
        raise ValueError(f"{label} paid budget identity or lifecycle is inconsistent")
    try:
        canonical_price = require_canonical_paid_budget(
            price=price,
            expected_model=requested_model,
            run_hard_limit_cny=run["hard_limit_cny"],
            run_execution_limit_cny=run["execution_limit_cny"],
            cumulative_hard_limit_cny=cumulative["hard_limit_cny"],
            cumulative_execution_limit_cny=(cumulative["execution_limit_cny"]),
        )
        require_canonical_attempt_reservation(
            canonical_price=canonical_price,
            max_output_tokens=max_output_tokens,
            reservation_cny_per_attempt=(budget["reservation_cny_per_attempt"]),
        )
    except CanonicalPricingError as exc:
        raise ValueError(f"{label} pricing or reservation is not canonical") from exc
    identity_started = _as_datetime(
        identity["started_at"],
        label=f"{label} budget identity",
    )
    identity_completed = _as_datetime(
        identity["completed_at"],
        label=f"{label} budget identity",
    )
    crossed_price_window = completed_at > canonical_price.valid_until
    has_price_expiry_failure = any(
        call["error_code"] == "MODEL_PRICE_EXPIRED" for call in error_calls
    )
    if (
        canonical_price_snapshot_sha256 != canonical_price_file_sha256()
        or not (
            canonical_price.captured_at
            <= identity_started
            <= started_at
            < canonical_price.valid_until
        )
        or not (started_at <= identity_completed <= completed_at)
        or (crossed_price_window and not has_price_expiry_failure)
    ):
        raise ValueError(f"{label} run is outside its canonical paid window")
    expected_settled: Counter[tuple[str, str, str, str]] = Counter()
    settled_units = 0
    try:
        for call in success_calls:
            usage = _as_mapping(
                call["usage"],
                label=f"{label} success usage",
            )
            cost = calculate_usage_cost_from_rates(
                rates_cny=canonical_price.rates_cny.model_dump(),
                tokens_per_price_unit=(canonical_price.tokens_per_price_unit),
                usage=usage,
            )
            settled_units += cost.units
            expected_settled[
                (
                    (
                        "settled_exact"
                        if cost.mode == "exact"
                        else "settled_upper_bound"
                    ),
                    cost.mode,
                    budget["reservation_cny_per_attempt"],
                    format(units_to_cny(cost.units), "f"),
                )
            ] += 1
    except BudgetUsageError as exc:
        raise ValueError(f"{label} success usage cannot be priced") from exc
    actual_settled: Counter[tuple[str, str, str, str]] = Counter()
    actual_uncertain = 0
    for bucket in run_buckets:
        count = bucket["count"]
        if bucket["status"] in _SETTLED_STATUSES:
            if bucket["settlement_mode"] is None or bucket["known_cost_cny"] is None:
                raise ValueError(f"{label} settled attempt bucket is incomplete")
            actual_settled[
                (
                    bucket["status"],
                    bucket["settlement_mode"],
                    bucket["reserved_cny"],
                    bucket["known_cost_cny"],
                )
            ] += count
        elif bucket["status"] == "uncertain":
            actual_uncertain += count
        else:
            raise ValueError(f"{label} completed diagnostic retains a reserved attempt")
    expected_uncertain = sum(
        _counted_attempts(
            call["provider_attempts"],
            label=f"{label} success call",
        )
        - 1
        for call in success_calls
    ) + sum(
        _counted_attempts(
            call["provider_attempts"],
            label=f"{label} error call",
        )
        for call in error_calls
    )
    provider_attempts = sum(
        _counted_attempts(
            call["provider_attempts"],
            label=f"{label} model call",
        )
        for call in (*success_calls, *error_calls)
    )
    if (
        actual_settled != expected_settled
        or actual_uncertain != expected_uncertain
        or run["attempt_count"] != provider_attempts
        or run["uncertain_count"] != expected_uncertain
        or cny_to_units(Decimal(run["settled_cny"])) != settled_units
    ):
        raise ValueError(f"{label} attempt buckets or usage costs differ from records")


def require_completed_diagnostic_evidence(
    *,
    label: str,
    budget: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    requested_model: str,
    observed_models: Sequence[str],
    max_output_tokens: int,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    canonical_price_snapshot_sha256: str | None,
) -> None:
    """Bind a completed diagnostic bundle to model and budget facts."""

    contract = nonformal_paid_contract("diagnostic")
    if (
        len(records) != contract.case_count
        or tuple(record["case_id"] for record in records) != contract.case_ids
        or any(record["trial"] != 1 for record in records)
    ):
        raise ValueError(f"{label} records do not match the canonical diagnostic cases")
    paid = budget["enforcement_mode"] == "persistent_sqlite"
    success_calls, error_calls, recomputed_models = _require_call_protocol(
        label=label,
        records=records,
        requested_model=requested_model,
        paid=paid,
    )
    if list(observed_models) != sorted(recomputed_models):
        raise ValueError(f"{label} observed models differ from successful calls")
    if paid:
        _require_paid_diagnostic(
            label=label,
            budget=budget,
            success_calls=success_calls,
            error_calls=error_calls,
            requested_model=requested_model,
            max_output_tokens=max_output_tokens,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            canonical_price_snapshot_sha256=(canonical_price_snapshot_sha256),
        )
    else:
        _require_offline_diagnostic(
            label=label,
            budget=budget,
            success_calls=success_calls,
            error_calls=error_calls,
        )
