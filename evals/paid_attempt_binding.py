from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Sequence

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SETTLED_STATUSES = {"settled_exact", "settled_upper_bound"}
_ALLOWED_LEDGER_OUTCOMES = {
    "MODEL_BUDGET_USAGE_ERROR": {
        "MISSING_PROVIDER_USAGE",
        "INVALID_PROVIDER_USAGE",
    },
    "MODEL_BUDGET_ERROR": {
        "COST_EXCEEDS_RESERVATION",
        "MODEL_BUDGET_ERROR",
    },
}
_LOCAL_RESERVE_ERRORS = {
    "MODEL_BUDGET_ERROR",
    "MODEL_BUDGET_EXHAUSTED",
    "MODEL_PRICE_EXPIRED",
}


def _require_datetime(value: object, *, label: str) -> datetime:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} attempt outcome time is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} attempt outcome time must be timezone-aware")
    return parsed


def require_paid_attempt_bindings(
    *,
    label: str,
    model_calls: Sequence[Mapping[str, Any]],
    attempt_buckets: Sequence[Mapping[str, Any]],
    price_valid_until: datetime,
    allow_unbound_attempts: int = 0,
) -> None:
    """Bind each paid logical model call to its persisted attempt outcomes."""

    if (
        type(allow_unbound_attempts) is not int
        or allow_unbound_attempts < 0
    ):
        raise ValueError(f"{label} unbound attempt allowance is invalid")
    buckets_by_hash: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    total_bucket_attempts = 0
    for bucket in attempt_buckets:
        logical_hash = bucket.get("logical_call_sha256")
        count = bucket.get("count")
        if (
            not isinstance(logical_hash, str)
            or _SHA256_PATTERN.fullmatch(logical_hash) is None
            or type(count) is not int
            or count < 1
        ):
            raise ValueError(f"{label} ledger logical-call bucket is invalid")
        buckets_by_hash[logical_hash].append(bucket)
        total_bucket_attempts += count

    attempted_hashes: set[str] = set()
    bound_bucket_attempts = 0
    for call in model_calls:
        attempts = call.get("provider_attempts")
        if type(attempts) is not int or attempts < 0:
            raise ValueError(f"{label} paid provider attempt count is invalid")
        status = call.get("status")
        error_code = call.get("error_code")
        error_stage = call.get("error_stage")
        logical_hash = call.get("logical_call_sha256")
        if attempts == 0:
            if (
                status != "error"
                or error_stage != "reserve_attempt"
                or error_code not in _LOCAL_RESERVE_ERRORS
            ):
                raise ValueError(
                    f"{label} zero-attempt call lacks a reserve terminal stage"
                )
            if (
                isinstance(logical_hash, str)
                and logical_hash in buckets_by_hash
            ):
                raise ValueError(
                    f"{label} zero-attempt logical call has ledger outcomes"
                )
            continue
        if (
            not isinstance(logical_hash, str)
            or _SHA256_PATTERN.fullmatch(logical_hash) is None
            or logical_hash in attempted_hashes
        ):
            raise ValueError(
                f"{label} attempted logical-call hash is missing or not unique"
            )
        attempted_hashes.add(logical_hash)
        call_buckets = buckets_by_hash.get(logical_hash, [])
        call_bucket_attempts = sum(
            int(bucket["count"]) for bucket in call_buckets
        )
        if call_bucket_attempts != attempts:
            raise ValueError(
                f"{label} logical-call attempt count differs from ledger outcomes"
            )
        bound_bucket_attempts += call_bucket_attempts
        settled_attempts = sum(
            int(bucket["count"])
            for bucket in call_buckets
            if bucket.get("status") in _SETTLED_STATUSES
        )
        uncertain_attempts = sum(
            int(bucket["count"])
            for bucket in call_buckets
            if bucket.get("status") == "uncertain"
        )
        if status == "success":
            if (
                error_code is not None
                or error_stage is not None
                or settled_attempts != 1
                or uncertain_attempts != attempts - 1
            ):
                raise ValueError(
                    f"{label} successful logical call lacks exactly one "
                    "settled attempt"
                )
            continue
        if (
            status != "error"
            or not isinstance(error_code, str)
            or settled_attempts != 0
            or uncertain_attempts != attempts
        ):
            raise ValueError(
                f"{label} failed logical-call attempt states are inconsistent"
            )
        if error_stage == "reserve_attempt":
            if error_code not in _LOCAL_RESERVE_ERRORS:
                raise ValueError(
                    f"{label} reserve terminal error is not locally generated"
                )
            continue
        if error_stage != "provider_attempt":
            raise ValueError(
                f"{label} failed paid call lacks a terminal error stage"
            )
        allowed_outcomes = _ALLOWED_LEDGER_OUTCOMES.get(
            error_code,
            {error_code},
        )
        matching_outcomes = [
            bucket
            for bucket in call_buckets
            if bucket.get("error_code") in allowed_outcomes
        ]
        if not matching_outcomes:
            raise ValueError(
                f"{label} provider error has no same-hash ledger outcome"
            )
        if error_code == "MODEL_PRICE_EXPIRED" and not any(
            bucket.get("error_code") == "MODEL_PRICE_EXPIRED"
            and _require_datetime(
                bucket.get("completed_at"),
                label=label,
            )
            >= price_valid_until
            for bucket in matching_outcomes
        ):
            raise ValueError(
                f"{label} provider price-expiry outcome is before its window"
            )

    unbound_bucket_attempts = total_bucket_attempts - bound_bucket_attempts
    if unbound_bucket_attempts != allow_unbound_attempts:
        raise ValueError(
            f"{label} ledger contains unbound logical-call attempts"
        )
