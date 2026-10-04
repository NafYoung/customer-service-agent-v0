from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from app.agent.budget.errors import (
    BudgetInvariantError,
    BudgetPriceWindowError,
    BudgetUsageError,
)
from app.agent.budget.ledger import (
    BudgetReservation,
    SQLiteBudgetLedger,
    require_paid_purpose,
)
from app.agent.budget.price import (
    PRICE_WINDOW_SAFETY_MARGIN_SECONDS,
    DeepSeekPriceSnapshot,
    UsageCost,
    format_cny,
    worst_case_attempt_cost,
)


def _require_nonnegative_seconds(value: object, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise BudgetInvariantError(
            f"{field_name} must be a finite non-negative number."
        )
    return float(value)



class DeepSeekBudgetGuard:
    """Reserve before every provider attempt and settle only trusted usage."""

    def __init__(
        self,
        *,
        ledger: SQLiteBudgetLedger,
        run_id: str,
        purpose: str,
        price_snapshot: DeepSeekPriceSnapshot,
        model: str,
        max_output_tokens: int,
        now: datetime | None = None,
        now_provider: Callable[[], datetime] | None = None,
        minimum_price_validity_seconds: float = 0,
    ):
        canonical_purpose = require_paid_purpose(purpose)
        if now_provider is not None:
            self._now_provider = now_provider
        elif now is not None:
            frozen_now = now
            self._now_provider = lambda: frozen_now
        else:
            self._now_provider = lambda: datetime.now(UTC)
        self._minimum_price_validity_seconds = _require_nonnegative_seconds(
            minimum_price_validity_seconds,
            field_name="Minimum pricing validity window",
        )
        price_snapshot.require_current(
            expected_model=model,
            now=self._now_provider(),
        )
        self._ledger = ledger
        self._ledger.bind_now_provider(self._now_provider)
        self._run_id = run_id
        self._price_snapshot = price_snapshot
        self._model = model
        self._reservation_cost = worst_case_attempt_cost(
            price_snapshot,
            max_output_tokens=max_output_tokens,
        )
        self._closed = False
        self._ledger.start_run(
            run_id=run_id,
            purpose=canonical_purpose,
            price_snapshot=price_snapshot,
        )

    def bind_request_timeout(self, *, timeout_seconds: float) -> None:
        """Bind the provider's maximum request duration before any HTTP call."""

        if self._closed:
            raise BudgetInvariantError("Budget guard is already closed.")
        timeout = _require_nonnegative_seconds(
            timeout_seconds,
            field_name="Provider request timeout",
        )
        required_window = timeout + PRICE_WINDOW_SAFETY_MARGIN_SECONDS
        bound_window = max(
            self._minimum_price_validity_seconds,
            required_window,
        )
        self._price_snapshot.require_current(
            expected_model=self._model,
            now=self._now_provider(),
            minimum_remaining=timedelta(seconds=bound_window),
        )
        self._minimum_price_validity_seconds = bound_window

    def reserve_attempt(
        self,
        *,
        logical_call_id: str,
        attempt_number: int,
    ) -> BudgetReservation:
        if self._closed:
            raise BudgetInvariantError("Budget guard is already closed.")
        self._price_snapshot.require_current(
            expected_model=self._model,
            now=self._now_provider(),
            minimum_remaining=timedelta(
                seconds=self._minimum_price_validity_seconds
            ),
        )
        return self._ledger.reserve_attempt(
            run_id=self._run_id,
            logical_call_id=logical_call_id,
            attempt_number=attempt_number,
            model=self._model,
            reserved_units=self._reservation_cost.units,
        )

    def settle_attempt(
        self,
        *,
        reservation: BudgetReservation,
        usage: Mapping[str, Any],
        provider_request_id: str | None,
        response_content_sha256: str | None = None,
    ) -> UsageCost:
        try:
            return self._ledger.settle_attempt(
                reservation=reservation,
                price_snapshot=self._price_snapshot,
                usage=usage,
                provider_request_id=provider_request_id,
                response_content_sha256=response_content_sha256,
            )
        except BudgetUsageError:
            self._ledger.mark_uncertain(
                reservation=reservation,
                error_code="INVALID_PROVIDER_USAGE",
            )
            raise

    def bind_response_content_sha256(
        self,
        *,
        logical_call_sha256: str,
        response_content_sha256: str,
    ) -> None:
        if self._closed:
            raise BudgetInvariantError("Budget guard is already closed.")
        self._ledger.bind_response_content_sha256(
            run_id=self._run_id,
            logical_call_sha256_value=logical_call_sha256,
            response_content_sha256=response_content_sha256,
        )

    def ensure_response_in_price_window(
        self,
        *,
        reservation: BudgetReservation,
        usage: Mapping[str, Any] | None,
        provider_request_id: str | None,
    ) -> None:
        """Fail closed if provider processing crossed the price validity edge."""

        try:
            self._price_snapshot.require_current(
                expected_model=self._model,
                now=self._now_provider(),
            )
        except BudgetPriceWindowError:
            self._ledger.mark_uncertain(
                reservation=reservation,
                error_code="MODEL_PRICE_EXPIRED",
                price_snapshot=(
                    self._price_snapshot if usage is not None else None
                ),
                usage=usage,
                provider_request_id=provider_request_id,
            )
            raise

    def mark_uncertain(
        self,
        *,
        reservation: BudgetReservation,
        error_code: str,
    ) -> None:
        self._ledger.mark_uncertain(
            reservation=reservation,
            error_code=error_code,
        )

    def snapshot(self) -> dict[str, Any]:
        ledger_snapshot = self._ledger.evidence_snapshot(
            run_id=self._run_id,
        )
        run_identity = ledger_snapshot["run_identity"]
        return {
            "schema_version": "1.0",
            "enforcement_mode": "persistent_sqlite",
            "run_status": run_identity["status"],
            "run_identity": run_identity,
            "price": {
                "provider": self._price_snapshot.provider,
                "model": self._price_snapshot.model,
                "currency": self._price_snapshot.currency,
                "snapshot_sha256": self._price_snapshot.sha256,
                "source_url": self._price_snapshot.source_url,
                "usage_source_url": self._price_snapshot.usage_source_url,
                "captured_at": self._price_snapshot.captured_at.isoformat(),
                "valid_until": self._price_snapshot.valid_until.isoformat(),
                "rates_cny": self._price_snapshot.rates_cny.model_dump(),
                "tokens_per_price_unit": (
                    self._price_snapshot.tokens_per_price_unit
                ),
            },
            "reservation_cny_per_attempt": format_cny(
                self._reservation_cost.units
            ),
            "run": ledger_snapshot["run"],
            "cumulative": ledger_snapshot["cumulative"],
            "attempt_evidence": ledger_snapshot[
                "attempt_evidence"
            ],
        }

    def close(self) -> None:
        if self._closed:
            return
        self._ledger.complete_run(self._run_id)
        self._closed = True
