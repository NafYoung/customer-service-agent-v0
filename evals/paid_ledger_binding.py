"""Bind persistent_sqlite paid evidence to the fixed private budget ledger."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from app.agent.deepseek_budget import (
    BudgetError,
    SQLiteBudgetLedger,
)
from evals.canonical_pricing import (
    FORMAL_EXECUTION_LIMIT_CNY,
    FORMAL_HARD_LIMIT_CNY,
)
from evals.evidence_schema import (
    BudgetAmountSummary,
    BudgetAttemptBucket,
    BudgetRunIdentity,
    BudgetSummary,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUDGET_LEDGER = ROOT / "artifacts" / "private" / "deepseek-budget.sqlite3"


class PaidLedgerBindingError(ValueError):
    """Paid evidence does not match the trusted persistent ledger."""


def _budget_amount_identity(
    amount: BudgetAmountSummary,
) -> dict[str, object]:
    payload = amount.model_dump(mode="json")
    # Ledger remaining_execution_cny is cumulative and can move after this run.
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
        bucket.response_content_sha256,
        bucket.count,
    )


def read_trusted_budget_evidence(
    *,
    run_id: str,
    ledger_path: Path | None = None,
) -> dict[str, object]:
    """Read the fixed private ledger without accepting caller evidence."""

    path = DEFAULT_BUDGET_LEDGER if ledger_path is None else ledger_path
    try:
        return SQLiteBudgetLedger.read_existing_evidence_snapshot(
            path=path,
            hard_limit_cny=FORMAL_HARD_LIMIT_CNY,
            execution_limit_cny=FORMAL_EXECUTION_LIMIT_CNY,
            run_id=run_id,
        )
    except BudgetError as exc:
        raise PaidLedgerBindingError(
            "The trusted persistent budget ledger is unavailable or invalid."
        ) from exc


def require_persistent_budget_matches_trusted_ledger(
    *,
    budget: BudgetSummary | Mapping[str, Any],
    label: str = "paid evidence",
    ledger_path: Path | None = None,
) -> None:
    """Fail closed unless a persistent_sqlite budget matches the live ledger."""

    try:
        report_budget = (
            budget
            if isinstance(budget, BudgetSummary)
            else BudgetSummary.model_validate(budget)
        )
    except ValidationError as exc:
        raise PaidLedgerBindingError(
            f"{label} budget evidence is invalid."
        ) from exc
    if report_budget.enforcement_mode != "persistent_sqlite":
        return
    identity = report_budget.run_identity
    attempt_evidence = report_budget.attempt_evidence
    if identity is None or attempt_evidence is None:
        raise PaidLedgerBindingError(
            f"{label} persistent budget is incomplete for ledger binding."
        )
    if report_budget.run_status == "completed":
        if identity.completed_at is None or identity.status != "completed":
            raise PaidLedgerBindingError(
                f"{label} persistent budget is incomplete for ledger binding."
            )
    elif report_budget.run_status == "active":
        if identity.completed_at is not None or identity.status != "active":
            raise PaidLedgerBindingError(
                f"{label} persistent budget is incomplete for ledger binding."
            )
    else:
        raise PaidLedgerBindingError(
            f"{label} persistent budget status is not bindable."
        )
    trusted_payload = read_trusted_budget_evidence(
        run_id=identity.run_id,
        ledger_path=ledger_path,
    )
    try:
        trusted_identity = BudgetRunIdentity.model_validate(
            trusted_payload["run_identity"]
        )
        trusted_run = BudgetAmountSummary.model_validate(
            trusted_payload["run"]
        )
        trusted_attempts = [
            BudgetAttemptBucket.model_validate(bucket)
            for bucket in (
                trusted_payload["attempt_evidence"]["run"]  # type: ignore[index]
            )
        ]
    except (KeyError, TypeError, ValidationError) as exc:
        raise PaidLedgerBindingError(
            f"{label} trusted ledger snapshot is invalid."
        ) from exc
    if (
        trusted_identity.model_dump(mode="json")
        != identity.model_dump(mode="json")
        or _budget_amount_identity(trusted_run)
        != _budget_amount_identity(report_budget.run)
        or Counter(
            _attempt_bucket_identity(bucket)
            for bucket in trusted_attempts
        )
        != Counter(
            _attempt_bucket_identity(bucket)
            for bucket in attempt_evidence.run
        )
    ):
        raise PaidLedgerBindingError(
            f"{label} does not match the trusted persistent ledger run."
        )
