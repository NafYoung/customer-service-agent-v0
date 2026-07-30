from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evals.paid_attempt_binding import require_paid_attempt_bindings

VALID_UNTIL = datetime(2026, 8, 6, 17, 20, 0, tzinfo=UTC)


def _call(
    *,
    logical_hash: str | None,
    status: str,
    attempts: int,
    error_code: str | None = None,
    error_stage: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "provider_attempts": attempts,
        "logical_call_sha256": logical_hash,
        "error_code": error_code,
        "error_stage": error_stage,
    }


def _bucket(
    *,
    logical_hash: str,
    status: str = "uncertain",
    error_code: str | None = "MODEL_HTTP_ERROR",
    completed_at: datetime | None = None,
    count: int = 1,
) -> dict[str, object]:
    return {
        "logical_call_sha256": logical_hash,
        "status": status,
        "error_code": error_code,
        "completed_at": completed_at or (VALID_UNTIL - timedelta(seconds=1)),
        "count": count,
    }


def test_paid_attempt_binding_rejects_swapped_provider_outcomes() -> None:
    calls = [
        _call(
            logical_hash="a" * 64,
            status="error",
            attempts=1,
            error_code="MODEL_PRICE_EXPIRED",
            error_stage="provider_attempt",
        ),
        _call(
            logical_hash="b" * 64,
            status="error",
            attempts=1,
            error_code="MODEL_HTTP_ERROR",
            error_stage="provider_attempt",
        ),
    ]
    buckets = [
        _bucket(logical_hash="a" * 64, error_code="MODEL_HTTP_ERROR"),
        _bucket(
            logical_hash="b" * 64,
            error_code="MODEL_PRICE_EXPIRED",
            completed_at=VALID_UNTIL + timedelta(seconds=1),
        ),
    ]

    with pytest.raises(ValueError, match="logical|outcome|price|hash"):
        require_paid_attempt_bindings(
            label="diagnostic",
            model_calls=calls,
            attempt_buckets=buckets,
            price_valid_until=VALID_UNTIL,
        )

@pytest.mark.parametrize(
    ("model_error", "ledger_error"),
    [
        ("MODEL_BUDGET_USAGE_ERROR", "MISSING_PROVIDER_USAGE"),
        ("MODEL_BUDGET_USAGE_ERROR", "INVALID_PROVIDER_USAGE"),
        ("MODEL_BUDGET_ERROR", "COST_EXCEEDS_RESERVATION"),
        ("MODEL_BUDGET_ERROR", "MODEL_BUDGET_ERROR"),
    ],
)
def test_paid_attempt_binding_accepts_error_namespace_aliases(
    model_error: str,
    ledger_error: str,
) -> None:
    require_paid_attempt_bindings(
        label="diagnostic",
        model_calls=[
            _call(
                logical_hash="a" * 64,
                status="error",
                attempts=1,
                error_code=model_error,
                error_stage="provider_attempt",
            )
        ],
        attempt_buckets=[
            _bucket(
                logical_hash="a" * 64,
                error_code=ledger_error,
            )
        ],
        price_valid_until=VALID_UNTIL,
    )


@pytest.mark.parametrize(
    ("attempts", "terminal_error", "buckets"),
    [
        (
            1,
            "MODEL_BUDGET_EXHAUSTED",
            [_bucket(logical_hash="a" * 64, error_code="MODEL_HTTP_ERROR")],
        ),
        (
            1,
            "MODEL_PRICE_EXPIRED",
            [_bucket(logical_hash="a" * 64, error_code="MODEL_HTTP_ERROR")],
        ),
        (0, "MODEL_PRICE_EXPIRED", []),
    ],
)
def test_paid_attempt_binding_accepts_local_reserve_terminal_errors(
    attempts: int,
    terminal_error: str,
    buckets: list[dict[str, object]],
) -> None:
    require_paid_attempt_bindings(
        label="diagnostic",
        model_calls=[
            _call(
                logical_hash="a" * 64,
                status="error",
                attempts=attempts,
                error_code=terminal_error,
                error_stage="reserve_attempt",
            )
        ],
        attempt_buckets=buckets,
        price_valid_until=VALID_UNTIL,
    )


@pytest.mark.parametrize(
    "calls",
    [
        [
            _call(
                logical_hash=None,
                status="error",
                attempts=1,
                error_code="MODEL_HTTP_ERROR",
                error_stage="provider_attempt",
            )
        ],
        [
            _call(
                logical_hash="a" * 64,
                status="error",
                attempts=1,
                error_code="MODEL_HTTP_ERROR",
                error_stage="provider_attempt",
            ),
            _call(
                logical_hash="a" * 64,
                status="error",
                attempts=1,
                error_code="MODEL_HTTP_ERROR",
                error_stage="provider_attempt",
            ),
        ],
    ],
)
def test_paid_attempt_binding_requires_unique_attempted_call_hashes(
    calls: list[dict[str, object]],
) -> None:
    buckets = [
        _bucket(
            logical_hash="a" * 64,
            error_code="MODEL_HTTP_ERROR",
            count=len(calls),
        )
    ]

    with pytest.raises(ValueError, match="logical|hash|unique"):
        require_paid_attempt_bindings(
            label="diagnostic",
            model_calls=calls,
            attempt_buckets=buckets,
            price_valid_until=VALID_UNTIL,
        )


def test_paid_attempt_binding_requires_one_settled_success_attempt() -> None:
    call = _call(
        logical_hash="a" * 64,
        status="success",
        attempts=2,
    )
    valid_buckets = [
        _bucket(logical_hash="a" * 64, error_code="MODEL_HTTP_ERROR"),
        _bucket(
            logical_hash="a" * 64,
            status="settled_upper_bound",
            error_code=None,
        ),
    ]

    require_paid_attempt_bindings(
        label="formal",
        model_calls=[call],
        attempt_buckets=valid_buckets,
        price_valid_until=VALID_UNTIL,
    )

    with pytest.raises(ValueError, match="settled|attempt"):
        require_paid_attempt_bindings(
            label="formal",
            model_calls=[call],
            attempt_buckets=[
                _bucket(
                    logical_hash="a" * 64,
                    status="settled_upper_bound",
                    error_code=None,
                    count=2,
                )
            ],
            price_valid_until=VALID_UNTIL,
        )


def test_provider_price_expiry_requires_post_window_same_hash_outcome() -> None:
    call = _call(
        logical_hash="a" * 64,
        status="error",
        attempts=1,
        error_code="MODEL_PRICE_EXPIRED",
        error_stage="provider_attempt",
    )

    with pytest.raises(ValueError, match="price|window|outcome"):
        require_paid_attempt_bindings(
            label="diagnostic",
            model_calls=[call],
            attempt_buckets=[
                _bucket(
                    logical_hash="a" * 64,
                    error_code="MODEL_PRICE_EXPIRED",
                    completed_at=VALID_UNTIL - timedelta(microseconds=1),
                )
            ],
            price_valid_until=VALID_UNTIL,
        )
