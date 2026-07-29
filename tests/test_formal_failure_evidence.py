from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from evals.canonical_pricing import canonical_budget_price_payload
from evals.evidence import ArtifactIntegrityError, stable_sha256
from evals.evidence_schema import validate_readonly_bundle
from evals.formal_failure_evidence import (
    FormalFailureContext,
    FormalFailureEvidenceError,
    validate_formal_failure_bundle,
    write_formal_failure_bundle,
)
from evals.readonly_eval import SCORE_CATEGORIES
from evals.readonly_reporting import (
    current_readonly_harness_fingerprints,
    offline_budget_report,
    readonly_harness_snapshot,
    readonly_model_snapshot,
)


def _context(*, run_id: str = "formal-failed-20260729-a1") -> FormalFailureContext:
    settings = Settings()
    source = {
        "git_commit": "1" * 40,
        "git_dirty": False,
        "source_tree_sha256": "2" * 64,
        "python_version": "3.11-test",
        "platform": "test-platform",
        "package_versions": {
            "fastapi": "test",
            "httpx": "test",
        },
    }
    harness_fingerprints = current_readonly_harness_fingerprints(settings)
    harness = readonly_harness_snapshot(
        settings=settings,
        fingerprints=harness_fingerprints,
    )
    model = readonly_model_snapshot(
        settings=settings,
        observed_models=[settings.deepseek_model],
    )
    runtime_identity_sha256 = stable_sha256(
        {
            "source": source,
            "harness": harness,
            "model": model,
        }
    )
    harness_sha256 = str(harness["runtime_harness_sha256"])
    return FormalFailureContext.model_validate(
        {
            "run_id": run_id,
            "created_at": "2026-07-29T16:00:00+00:00",
            "failed_at": "2026-07-29T16:00:03+00:00",
            "failure_stage": "suite_execution",
            "failure_code": "MODEL_HTTP_ERROR",
            "max_output_tokens": 1024,
            "source": source,
            "runtime_harness": harness,
            "runtime_model": model,
            "case_set": {
                "name": "readonly-holdout-v2",
                "sha256": "3" * 64,
                "planned_case_count": 20,
                "planned_trials": 4,
            },
            "formal_holdout": {
                "declaration_manifest_sha256": "4" * 64,
                "lock_start_receipt_sha256": "5" * 64,
                "declared_harness_sha256": harness_sha256,
                "runtime_harness_sha256": harness_sha256,
                "regression_bundle_integrity_sha256": "7" * 64,
                "regression_gate_sha256": "8" * 64,
                "regression_run_id": ("eval-20260729-dev-repeat-public-binding"),
                "regression_source_git_commit": "1" * 40,
                "regression_case_set_name": "readonly-regression-v1",
                "regression_case_set_sha256": "9" * 64,
                "regression_harness_sha256": harness_sha256,
                "regression_source_tree_sha256": "2" * 64,
                "regression_source_identity_sha256": stable_sha256(source),
                "regression_runtime_identity_sha256": (runtime_identity_sha256),
            },
        }
    )


def _case_record(
    *,
    case_id: str = "formal-case-01",
    trial: int = 1,
    status: str = "passed",
) -> dict[str, object]:
    scores = {category: status == "passed" for category in SCORE_CATEGORIES}
    completed_at = datetime(2026, 7, 29, 16, 0, trial, tzinfo=UTC)
    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "split": "holdout",
        "trial": trial,
        "case_run_id": f"failed-run-{case_id}-t{trial}",
        "input_sha256": "7" * 64,
        "started_at": (completed_at - timedelta(milliseconds=20)).isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": 20,
        "status": status,
        "termination_reason": (
            "completed" if status == "passed" else "MODEL_HTTP_ERROR"
        ),
        "error_code": None if status == "passed" else "MODEL_HTTP_ERROR",
        "final_text": "safe partial answer",
        "model_calls": [],
        "tool_trace": [],
        "business_state": {
            "changed": False,
            "changed_tables": [],
            "before_sha256": "8" * 64,
            "after_sha256": "8" * 64,
        },
        "counted_action_records": 0,
        "scores": scores,
        "score_checks": [
            {
                "category": category,
                "message": f"{category} check",
                "passed": passed,
            }
            for category, passed in scores.items()
        ],
        "checks": [
            f"{category} check" for category, passed in scores.items() if passed
        ],
        "failures": [
            f"{category} check" for category, passed in scores.items() if not passed
        ],
    }


def _persistent_budget_report(
    *,
    run_id: str,
    attempt_count: int,
    committed_cny: str = "0",
) -> dict[str, object]:
    committed = Decimal(committed_cny)
    reservation = Decimal("1.002048")
    attempt_buckets: list[dict[str, object]] = []
    settled_cny = committed_cny
    if attempt_count > 0 and committed >= reservation * attempt_count:
        if committed == reservation * attempt_count:
            attempt_buckets.append(
                {
                    "status": "uncertain",
                    "settlement_mode": None,
                    "reserved_cny": format(reservation, "f"),
                    "known_cost_cny": None,
                    "count": attempt_count,
                }
            )
            settled_cny = "0"
        else:
            if attempt_count > 1:
                attempt_buckets.append(
                    {
                        "status": "uncertain",
                        "settlement_mode": None,
                        "reserved_cny": format(reservation, "f"),
                        "known_cost_cny": None,
                        "count": attempt_count - 1,
                    }
                )
            attempt_buckets.append(
                {
                    "status": "uncertain",
                    "settlement_mode": "exact",
                    "reserved_cny": format(reservation, "f"),
                    "known_cost_cny": format(
                        committed - reservation * (attempt_count - 1),
                        "f",
                    ),
                    "count": 1,
                }
            )
            settled_cny = "0"
    amount = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": committed_cny,
        "settled_cny": settled_cny,
        "remaining_execution_cny": format(
            max(Decimal("0"), Decimal("18") - committed),
            "f",
        ),
        "attempt_count": attempt_count,
        "reserved_count": 0,
        "uncertain_count": (attempt_count if attempt_buckets else 0),
    }
    price = canonical_budget_price_payload()
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
        "run_status": "completed",
        "run_identity": {
            "run_id": run_id,
            "purpose": "holdout_formal",
            "model": price["model"],
            "price_sha256": price["snapshot_sha256"],
            "status": "completed",
            "started_at": "2026-07-29T15:59:00+00:00",
            "completed_at": "2026-07-29T16:00:02+00:00",
        },
        "price": price,
        "reservation_cny_per_attempt": "1.002048",
        "run": dict(amount),
        "cumulative": dict(amount),
        "attempt_evidence": {
            "run": list(attempt_buckets),
            "cumulative": list(attempt_buckets),
        },
    }


def _budget_report_with_attempt_buckets(
    *,
    run_id: str,
    buckets: list[dict[str, object]],
) -> dict[str, object]:
    committed = Decimal("0")
    settled = Decimal("0")
    attempt_count = 0
    reserved_count = 0
    uncertain_count = 0
    for bucket in buckets:
        count = int(bucket["count"])
        reserved = Decimal(str(bucket["reserved_cny"]))
        known_raw = bucket["known_cost_cny"]
        known = Decimal(str(known_raw)) if known_raw is not None else None
        status = bucket["status"]
        attempt_count += count
        if status == "reserved":
            reserved_count += count
        if status == "uncertain":
            uncertain_count += count
        if status in {"settled_exact", "settled_upper_bound"}:
            assert known is not None
            committed += known * count
            settled += known * count
        else:
            committed += max(reserved, known or reserved) * count
    report = _persistent_budget_report(
        run_id=run_id,
        attempt_count=0,
    )
    amount = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": format(committed, "f"),
        "settled_cny": format(settled, "f"),
        "remaining_execution_cny": format(
            max(Decimal("0"), Decimal("18") - committed),
            "f",
        ),
        "attempt_count": attempt_count,
        "reserved_count": reserved_count,
        "uncertain_count": uncertain_count,
    }
    report["run"] = dict(amount)
    report["cumulative"] = dict(amount)
    report["attempt_evidence"] = {
        "run": list(buckets),
        "cumulative": list(buckets),
    }
    return report


@pytest.mark.parametrize("run_status", ["active", "completed"])
def test_failed_attempt_preserves_cross_window_budget_lifecycle(
    tmp_path: Path,
    run_status: str,
) -> None:
    price = canonical_budget_price_payload()
    valid_until = datetime.fromisoformat(str(price["valid_until"]))
    context = _context().model_dump(mode="json")
    context["created_at"] = (valid_until - timedelta(seconds=1)).isoformat()
    context["failed_at"] = (valid_until + timedelta(seconds=2)).isoformat()
    budget = _persistent_budget_report(
        run_id=str(context["run_id"]),
        attempt_count=0,
    )
    budget["run_status"] = run_status
    budget["run_identity"]["status"] = run_status
    budget["run_identity"]["completed_at"] = (
        (valid_until + timedelta(seconds=1)).isoformat()
        if run_status == "completed"
        else None
    )

    bundle = write_formal_failure_bundle(
        output_root=tmp_path,
        context=context,
        case_records=[],
        records_captured=True,
        budget_summary=budget,
    )

    validated = validate_formal_failure_bundle(bundle)
    assert validated.summary.budget is not None
    assert validated.summary.budget.run_status == run_status


def _model_error_with_attempts(attempts: int) -> dict[str, object]:
    return {
        "sequence": 1,
        "status": "error",
        "started_at": "2026-07-29T15:59:59+00:00",
        "latency_ms": 20,
        "message_count": 2,
        "tool_contract_count": 6,
        "phase": "agent",
        "tool_calls": [],
        "finish_reason": None,
        "response_id": None,
        "observed_model": None,
        "usage": None,
        "error_code": "MODEL_HTTP_ERROR",
        "http_status": 500,
        "provider_request_id": None,
        "provider_attempts": attempts,
    }


def _successful_model_call_with_usage(
    *,
    prompt_tokens: int,
) -> dict[str, object]:
    return {
        "sequence": 1,
        "status": "success",
        "started_at": "2026-07-29T15:59:59+00:00",
        "latency_ms": 20,
        "message_count": 2,
        "tool_contract_count": 6,
        "phase": "agent",
        "tool_calls": [],
        "finish_reason": "stop",
        "response_id": "response-visible-usage-1",
        "observed_model": "deepseek-v4-flash",
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 0,
            "total_tokens": prompt_tokens,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": prompt_tokens,
        },
        "error_code": None,
        "http_status": None,
        "provider_request_id": "provider-visible-usage-1",
        "provider_attempts": 1,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_integrity_entry(bundle_path: Path, relative_path: str) -> None:
    integrity_path = bundle_path / "integrity.json"
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    changed_path = bundle_path / relative_path
    integrity["files"][relative_path] = {
        "sha256": _sha256(changed_path),
        "bytes": changed_path.stat().st_size,
    }
    integrity_path.write_text(
        json.dumps(integrity, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _assert_private_tree(bundle_path: Path) -> None:
    for path in (bundle_path, *bundle_path.rglob("*")):
        mode = path.lstat().st_mode
        if path.is_dir():
            assert stat.S_ISDIR(mode)
            assert stat.S_IMODE(mode) == 0o700
        else:
            assert stat.S_ISREG(mode)
            assert stat.S_IMODE(mode) == 0o600


def test_zero_record_failed_attempt_is_private_and_not_completed_evidence(
    tmp_path: Path,
) -> None:
    context = _context()

    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=context,
        case_records=[],
        records_captured=True,
        budget_summary=None,
    )

    bundle = validate_formal_failure_bundle(bundle_path)
    assert bundle.manifest.artifact_kind == "formal_holdout_failed_attempt"
    assert bundle.manifest.status == "failed"
    assert bundle.manifest.completed_record_count == 0
    assert bundle.summary.record_capture_status == "captured"
    assert bundle.summary.partial is not None
    assert bundle.summary.partial.completed_record_count == 0
    assert bundle.summary.budget_capture_status == "unavailable"
    assert bundle.summary.budget is None
    assert bundle.cases == []
    assert bundle.trajectories == []
    _assert_private_tree(bundle_path)

    with pytest.raises(ValidationError):
        validate_readonly_bundle(bundle_path)


def test_partial_failed_attempt_cross_validates_records_and_budget(
    tmp_path: Path,
) -> None:
    records = [
        _case_record(),
        _case_record(trial=2, status="failed"),
    ]

    run_id = "formal-failed-20260729-a2"
    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id=run_id),
        case_records=records,
        records_captured=True,
        budget_summary=_persistent_budget_report(
            run_id=run_id,
            attempt_count=0,
        ),
    )

    bundle = validate_formal_failure_bundle(bundle_path)
    assert bundle.manifest.completed_record_count == 2
    assert bundle.summary.completed_record_count == 2
    assert bundle.summary.partial is not None
    assert bundle.summary.partial.unique_case_count == 1
    assert bundle.summary.partial.passed_record_count == 1
    assert bundle.summary.partial.failed_record_count == 1
    assert bundle.summary.partial.error_counts == {"MODEL_HTTP_ERROR": 1}
    assert bundle.summary.budget_capture_status == "captured"
    assert bundle.summary.budget is not None
    assert bundle.summary.budget_attempt_delta == 0
    assert bundle.summary.budget_limit_breached is False
    assert [item.model_dump(mode="json") for item in bundle.cases] == [
        item.model_dump(mode="json") for item in bundle.trajectories
    ]


def test_failed_attempt_rejects_budget_identity_before_price_window(
    tmp_path: Path,
) -> None:
    run_id = "formal-failed-20260729-early-budget"
    budget = _persistent_budget_report(
        run_id=run_id,
        attempt_count=0,
    )
    budget["run_identity"]["started_at"] = "2026-07-28T00:00:00+00:00"

    with pytest.raises(
        (ValidationError, ValueError),
        match="price|window|identity|budget",
    ):
        write_formal_failure_bundle(
            output_root=tmp_path / "failed-attempts",
            context=_context(run_id=run_id),
            case_records=[],
            records_captured=True,
            budget_summary=budget,
        )


def test_failed_attempt_rejects_offline_or_underreported_budget(
    tmp_path: Path,
) -> None:
    run_id = "formal-failed-20260729-budget"
    failed_record = _case_record(status="failed")
    failed_record["model_calls"] = [_model_error_with_attempts(3)]

    with pytest.raises(
        (ValidationError, ValueError),
        match="persistent|budget",
    ):
        write_formal_failure_bundle(
            output_root=tmp_path / "offline",
            context=_context(run_id=run_id),
            case_records=[failed_record],
            records_captured=True,
            budget_summary=offline_budget_report(),
        )

    with pytest.raises(
        (ValidationError, ValueError),
        match="attempt|budget",
    ):
        write_formal_failure_bundle(
            output_root=tmp_path / "underreported",
            context=_context(run_id=run_id),
            case_records=[failed_record],
            records_captured=True,
            budget_summary=_persistent_budget_report(
                run_id=run_id,
                attempt_count=0,
            ),
        )


def test_failed_attempt_preserves_a_real_budget_overrun(
    tmp_path: Path,
) -> None:
    run_id = "formal-failed-20260729-overrun"
    failed_record = _case_record(status="failed")
    failed_record["model_calls"] = [_model_error_with_attempts(3)]

    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id=run_id),
        case_records=[failed_record],
        records_captured=True,
        budget_summary=_persistent_budget_report(
            run_id=run_id,
            attempt_count=4,
            committed_cny="18.1",
        ),
    )

    bundle = validate_formal_failure_bundle(bundle_path)
    assert bundle.summary.budget_attempt_delta == 1
    assert bundle.summary.budget_limit_breached is True
    assert bundle.summary.budget is not None
    assert bundle.summary.budget.cumulative.committed_cny == "18.1"


def test_failed_attempt_rejects_false_breach_with_hidden_bucket_cost(
    tmp_path: Path,
) -> None:
    run_id = "formal-failed-20260729-false-breach"
    failed_record = _case_record(status="failed")
    failed_record["model_calls"] = [_model_error_with_attempts(3)]
    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id=run_id),
        case_records=[failed_record],
        records_captured=True,
        budget_summary=_persistent_budget_report(
            run_id=run_id,
            attempt_count=4,
            committed_cny="18.1",
        ),
    )
    summary_path = bundle_path / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for scope in ("run", "cumulative"):
        summary["budget"][scope]["committed_cny"] = "17.9"
        summary["budget"][scope]["remaining_execution_cny"] = "0.1"
    summary["budget_limit_breached"] = False
    summary_path.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _refresh_integrity_entry(bundle_path, "summary.json")

    with pytest.raises(
        ValidationError,
        match="attempt evidence|budget|derivations",
    ):
        validate_formal_failure_bundle(bundle_path)


@pytest.mark.parametrize(
    ("prompt_tokens", "run_id"),
    [
        (1, "formal-failed-20260729-visible-cost"),
        (19_000_000, "formal-failed-20260729-hidden-overrun"),
    ],
)
def test_failed_attempt_rejects_visible_usage_hidden_by_zero_budget(
    tmp_path: Path,
    prompt_tokens: int,
    run_id: str,
) -> None:
    failed_record = _case_record(status="failed")
    failed_record["model_calls"] = [
        _successful_model_call_with_usage(
            prompt_tokens=prompt_tokens,
        )
    ]

    with pytest.raises(
        (ValidationError, ValueError),
        match="budget|cost|usage|commit",
    ):
        write_formal_failure_bundle(
            output_root=tmp_path / run_id,
            context=_context(run_id=run_id),
            case_records=[failed_record],
            records_captured=True,
            budget_summary=_persistent_budget_report(
                run_id=run_id,
                attempt_count=1,
                committed_cny="0",
            ),
        )


def test_failed_attempt_accepts_canonical_usage_matched_to_ledger_bucket(
    tmp_path: Path,
) -> None:
    run_id = "formal-failed-20260729-visible-settled"
    failed_record = _case_record(status="failed")
    failed_record["model_calls"] = [_successful_model_call_with_usage(prompt_tokens=1)]
    budget = _persistent_budget_report(
        run_id=run_id,
        attempt_count=1,
        committed_cny="0.000001",
    )
    for scope in ("run", "cumulative"):
        amount = budget[scope]
        assert isinstance(amount, dict)
        amount.update(
            {
                "settled_cny": "0.000001",
                "remaining_execution_cny": "17.999999",
            }
        )
    budget["attempt_evidence"] = {
        "run": [
            {
                "status": "settled_exact",
                "settlement_mode": "exact",
                "reserved_cny": "1.002048",
                "known_cost_cny": "0.000001",
                "count": 1,
            }
        ],
        "cumulative": [
            {
                "status": "settled_exact",
                "settlement_mode": "exact",
                "reserved_cny": "1.002048",
                "known_cost_cny": "0.000001",
                "count": 1,
            }
        ],
    }

    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / run_id,
        context=_context(run_id=run_id),
        case_records=[failed_record],
        records_captured=True,
        budget_summary=budget,
    )

    bundle = validate_formal_failure_bundle(bundle_path)
    assert bundle.summary.budget is not None
    assert bundle.summary.budget.run.committed_cny == "0.000001"


def test_failed_attempt_rejects_coordinated_underreservation(
    tmp_path: Path,
) -> None:
    run_id = "formal-failed-20260729-underreserved"
    failed_record = _case_record(status="failed")
    failed_record["model_calls"] = [_successful_model_call_with_usage(prompt_tokens=1)]
    budget = _budget_report_with_attempt_buckets(
        run_id=run_id,
        buckets=[
            {
                "status": "settled_exact",
                "settlement_mode": "exact",
                "reserved_cny": "0.000001",
                "known_cost_cny": "0.000001",
                "count": 1,
            }
        ],
    )
    budget["reservation_cny_per_attempt"] = "0.000001"

    with pytest.raises(
        (ValidationError, ValueError),
        match="reservation|canonical",
    ):
        write_formal_failure_bundle(
            output_root=tmp_path / run_id,
            context=_context(run_id=run_id),
            case_records=[failed_record],
            records_captured=True,
            budget_summary=budget,
        )


@pytest.mark.parametrize("call_kind", ["success_retry", "error_retry"])
def test_failed_attempt_matches_retries_to_uncertain_attempt_buckets(
    tmp_path: Path,
    call_kind: str,
) -> None:
    run_id = f"formal-failed-20260729-{call_kind}"
    failed_record = _case_record(status="failed")
    uncertain_bucket = {
        "status": "uncertain",
        "settlement_mode": None,
        "reserved_cny": "1.002048",
        "known_cost_cny": None,
        "count": 2 if call_kind == "success_retry" else 3,
    }
    buckets = [uncertain_bucket]
    if call_kind == "success_retry":
        success_call = _successful_model_call_with_usage(prompt_tokens=1)
        success_call["provider_attempts"] = 3
        failed_record["model_calls"] = [success_call]
        buckets.append(
            {
                "status": "settled_exact",
                "settlement_mode": "exact",
                "reserved_cny": "1.002048",
                "known_cost_cny": "0.000001",
                "count": 1,
            }
        )
    else:
        failed_record["model_calls"] = [_model_error_with_attempts(3)]

    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / run_id,
        context=_context(run_id=run_id),
        case_records=[failed_record],
        records_captured=True,
        budget_summary=_budget_report_with_attempt_buckets(
            run_id=run_id,
            buckets=buckets,
        ),
    )

    bundle = validate_formal_failure_bundle(bundle_path)
    assert bundle.summary.budget is not None
    assert bundle.summary.budget.run.uncertain_count == (uncertain_bucket["count"])


def test_failed_attempt_rejects_usage_on_an_error_model_call(
    tmp_path: Path,
) -> None:
    run_id = "formal-failed-20260729-error-usage"
    failed_record = _case_record(status="failed")
    error_call = _model_error_with_attempts(1)
    error_call["usage"] = {
        "prompt_tokens": 1,
        "completion_tokens": 0,
        "total_tokens": 1,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 1,
    }
    failed_record["model_calls"] = [error_call]

    with pytest.raises(
        (ValidationError, ValueError),
        match="protocol|usage|error",
    ):
        write_formal_failure_bundle(
            output_root=tmp_path / run_id,
            context=_context(run_id=run_id),
            case_records=[failed_record],
            records_captured=True,
            budget_summary=_persistent_budget_report(
                run_id=run_id,
                attempt_count=1,
                committed_cny="0.000001",
            ),
        )


@pytest.mark.parametrize(
    "records",
    [
        [_case_record(), _case_record()],
        [{**_case_record(), "split": "dev"}],
        [_case_record(trial=5)],
    ],
)
def test_writer_rejects_invalid_partial_record_sets(
    tmp_path: Path,
    records: list[dict[str, object]],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        write_formal_failure_bundle(
            output_root=tmp_path / "failed-attempts",
            context=_context(run_id="formal-failed-20260729-a3"),
            case_records=records,
            records_captured=True,
            budget_summary=None,
        )


def test_integrity_tampering_is_rejected(tmp_path: Path) -> None:
    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id="formal-failed-20260729-a4"),
        case_records=[_case_record()],
        records_captured=True,
        budget_summary=None,
    )
    summary_path = bundle_path / "summary.json"
    summary_path.write_text(
        summary_path.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactIntegrityError):
        validate_formal_failure_bundle(bundle_path)


def test_logical_content_tampering_is_rejected_after_reindex(
    tmp_path: Path,
) -> None:
    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id="formal-failed-20260729-a5"),
        case_records=[_case_record()],
        records_captured=True,
        budget_summary=None,
    )
    trajectory_path = bundle_path / "trajectories" / "formal-case-01" / "1.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    trajectory["final_text"] = "forged divergent trajectory"
    trajectory_path.write_text(
        json.dumps(
            trajectory,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _refresh_integrity_entry(
        bundle_path,
        "trajectories/formal-case-01/1.json",
    )

    with pytest.raises(ValidationError, match="records and trajectories differ"):
        validate_formal_failure_bundle(bundle_path)


def test_permission_downgrade_is_rejected(tmp_path: Path) -> None:
    bundle_path = write_formal_failure_bundle(
        output_root=tmp_path / "failed-attempts",
        context=_context(run_id="formal-failed-20260729-a6"),
        case_records=[],
        records_captured=False,
        budget_summary=None,
    )
    (bundle_path / "summary.json").chmod(0o644)

    with pytest.raises(FormalFailureEvidenceError, match="owner-only"):
        validate_formal_failure_bundle(bundle_path)
