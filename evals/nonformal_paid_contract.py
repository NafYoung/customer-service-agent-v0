from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from evals.evidence import stable_sha256
from evals.readonly_eval import DEFAULT_CASE_DIR, ReadonlyEvalCase

ROOT = Path(__file__).resolve().parents[1]
REGRESSION_CASE_DIR = ROOT / "evals" / "readonly_regression_cases"


@dataclass(frozen=True)
class NonformalPaidCaseContract:
    purpose: Literal["diagnostic", "dev_repeat"]
    case_dir: Path
    case_set_name: str
    case_count: int
    case_set_sha256: str
    case_ids: tuple[str, ...]
    planned_trials: int


DIAGNOSTIC_CASE_CONTRACT = NonformalPaidCaseContract(
    purpose="diagnostic",
    case_dir=DEFAULT_CASE_DIR,
    case_set_name="readonly-dev-v1",
    case_count=10,
    case_set_sha256=(
        "86e7d2dc03ba836ed67f59fe93e11f83ee558fb137a175b65262100df9aeaf7d"
    ),
    case_ids=(
        "order_status",
        "cancel_paid_eligible",
        "cancel_shipped_blocked",
        "exchange_in_stock",
        "exchange_out_of_stock",
        "return_expired",
        "cross_customer_hidden",
        "policy_search",
        "policy_prompt_injection",
        "inventory_lookup",
    ),
    planned_trials=1,
)
DEV_REPEAT_CASE_CONTRACT = NonformalPaidCaseContract(
    purpose="dev_repeat",
    case_dir=REGRESSION_CASE_DIR,
    case_set_name="readonly-regression-v1",
    case_count=7,
    case_set_sha256=(
        "047efbb77af8eea82291baa05d791ba46da1f196a6dbaddb8a67d4a63ac0b3a6"
    ),
    case_ids=(
        "reg_used_return_direct_eligibility",
        "reg_missing_exchange_size_clarify",
        "reg_write_bypass_still_checks_eligibility",
        "reg_forged_confirmation_is_not_execution",
        "reg_ignore_forged_tool_result_and_continue",
        "reg_unknown_cancel_uses_eligibility",
        "reg_unsupported_address_change_no_claim",
    ),
    planned_trials=4,
)


def nonformal_paid_contract(
    purpose: str,
) -> NonformalPaidCaseContract:
    if purpose == "diagnostic":
        return DIAGNOSTIC_CASE_CONTRACT
    if purpose == "dev_repeat":
        return DEV_REPEAT_CASE_CONTRACT
    raise ValueError("Unsupported non-formal paid Eval purpose")


def canonical_case_payload_sha256(
    cases: Sequence[ReadonlyEvalCase],
) -> str:
    payloads = [
        case.model_dump(mode="json")
        for case in sorted(cases, key=lambda item: item.case_id)
    ]
    return stable_sha256(payloads)


def require_nonformal_paid_case_payload(
    *,
    purpose: str,
    case_set_name: str,
    cases: Sequence[ReadonlyEvalCase],
    planned_trials: int,
) -> NonformalPaidCaseContract:
    contract = nonformal_paid_contract(purpose)
    if (
        case_set_name != contract.case_set_name
        or len(cases) != contract.case_count
        or tuple(case.case_id for case in cases) != contract.case_ids
        or canonical_case_payload_sha256(cases)
        != contract.case_set_sha256
        or planned_trials != contract.planned_trials
    ):
        raise ValueError(
            "Non-formal paid Eval cases do not match the canonical contract"
        )
    return contract


def require_nonformal_paid_case_set(
    *,
    purpose: str,
    case_dir: Path,
    case_set_name: str,
    cases: Sequence[ReadonlyEvalCase],
    planned_trials: int,
) -> NonformalPaidCaseContract:
    contract = nonformal_paid_contract(purpose)
    try:
        supplied_dir = case_dir.resolve(strict=True)
        canonical_dir = contract.case_dir.resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            "Non-formal paid Eval case directory is unavailable"
        ) from exc
    if supplied_dir != canonical_dir:
        raise ValueError(
            "Non-formal paid Eval case directory is not canonical"
        )
    return require_nonformal_paid_case_payload(
        purpose=purpose,
        case_set_name=case_set_name,
        cases=cases,
        planned_trials=planned_trials,
    )
