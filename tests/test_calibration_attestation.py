from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.deepseek_budget import load_price_snapshot
from app.config import Settings
from evals.calibration_attestation import (
    CalibrationAttestationError,
    canonical_contract_set_sha256,
    required_review_fixture_ids,
    validate_calibration_attestation,
    validate_calibration_review,
)
from evals.readonly_eval import load_cases
from evals.readonly_reporting import current_readonly_harness_fingerprints
from evals.semantic_calibration import (
    CalibrationResult,
    load_calibration_fixtures,
    summarize_calibration,
    validate_calibration_coverage,
)

FIXTURE_PATH = Path("evals/semantic_judge_calibration_cases.jsonl")
CASE_DIR = Path("evals/readonly_regression_cases")
PRICE_SNAPSHOT_PATH = Path(
    "pricing/deepseek-v4-flash-2026-07-29.json"
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verdict_for_fixture(fixture) -> dict[str, object]:
    claims = []
    for claim_id, relation in (
        fixture.effective_expected_relations.items()
    ):
        regions = fixture.acceptable_evidence_regions[claim_id]
        evidence_spans: list[str]
        if relation == "not_mentioned":
            evidence_spans = []
        elif relation == "both_or_ambiguous":
            evidence_spans = [
                next(
                    region
                    for region in regions
                    if region in side
                )
                for side in fixture.contradiction_evidence_sides
            ]
        else:
            evidence_spans = [regions[0]]
        claims.append(
            {
                "id": claim_id,
                "relation": relation,
                "evidence_spans": evidence_spans,
            }
        )
    contradiction_evidence: list[str] = []
    if fixture.expected_material_self_contradiction:
        contradiction_evidence = [
            fixture.contradiction_evidence_sides[0][0],
            fixture.contradiction_evidence_sides[1][0],
        ]
    return {
        "claims": claims,
        "material_self_contradiction": (
            fixture.expected_material_self_contradiction
        ),
        "contradiction_evidence": contradiction_evidence,
    }


def _model_call(model_name: str) -> dict[str, object]:
    return {
        "sequence": 1,
        "status": "success",
        "started_at": "2026-07-29T12:00:00+00:00",
        "latency_ms": 1,
        "message_count": 2,
        "tool_contract_count": 0,
        "phase": "semantic_judge",
        "tool_calls": [],
        "finish_reason": "stop",
        "response_id": "calibration-response",
        "observed_model": model_name,
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
        "error_code": None,
        "http_status": None,
        "provider_request_id": None,
        "provider_attempts": 1,
    }


def _canonical_price_summary() -> dict[str, object]:
    snapshot = load_price_snapshot(PRICE_SNAPSHOT_PATH)
    payload = snapshot.model_dump(mode="json")
    return {
        "provider": payload["provider"],
        "model": payload["model"],
        "currency": payload["currency"],
        "snapshot_sha256": snapshot.sha256,
        "source_url": payload["source_url"],
        "usage_source_url": payload["usage_source_url"],
        "captured_at": payload["captured_at"],
        "valid_until": payload["valid_until"],
        "rates_cny": payload["rates_cny"],
        "tokens_per_price_unit": payload["tokens_per_price_unit"],
    }


def _settled_budget(attempt_count: int) -> dict[str, object]:
    settled = Decimal(attempt_count) * Decimal("0.00002")
    settled_cny = format(settled, "f")
    snapshot = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": settled_cny,
        "settled_cny": settled_cny,
        "remaining_execution_cny": format(
            Decimal("18") - settled,
            "f",
        ),
        "attempt_count": attempt_count,
        "reserved_count": 0,
        "uncertain_count": 0,
    }
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
        "run_status": "completed",
        "run_identity": {
            "run_id": "eval-20260729-calibration-attestation",
            "purpose": "semantic_judge_calibration",
            "model": "deepseek-v4-flash",
            "price_sha256": _canonical_price_summary()[
                "snapshot_sha256"
            ],
            "status": "completed",
            "started_at": "2026-07-29T12:00:00+00:00",
            "completed_at": "2026-07-29T12:05:00+00:00",
        },
        "price": _canonical_price_summary(),
        "reservation_cny_per_attempt": "1.002048",
        "run": dict(snapshot),
        "cumulative": dict(snapshot),
        "attempt_evidence": {
            "run": [
                {
                    "status": "settled_upper_bound",
                    "settlement_mode": "upper_bound",
                    "reserved_cny": "1.002048",
                    "known_cost_cny": "0.00002",
                    "count": attempt_count,
                }
            ],
            "cumulative": [
                {
                    "status": "settled_upper_bound",
                    "settlement_mode": "upper_bound",
                    "reserved_cny": "1.002048",
                    "known_cost_cny": "0.00002",
                    "count": attempt_count,
                }
            ],
        },
    }


def _valid_report() -> dict[str, object]:
    fixtures = load_calibration_fixtures(FIXTURE_PATH)
    cases = load_cases(CASE_DIR)
    validate_calibration_coverage(fixtures=fixtures, cases=cases)
    settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=0,
    )
    results = [
        CalibrationResult(
            fixture_id=fixture.fixture_id,
            case_id=fixture.case_id,
            kind=fixture.kind,
            expected_gate_pass=fixture.expected_gate_pass,
            observed_gate_pass=fixture.expected_gate_pass,
            exact_relations_match=True,
            contradiction_match=True,
            passed=True,
            error_code=None,
            observed_relations=dict(
                fixture.effective_expected_relations
            ),
            verdict=_verdict_for_fixture(fixture),
            model_calls=(_model_call(settings.deepseek_model),),
        )
        for fixture in fixtures
    ]
    return {
        "schema_version": "2.0",
        "attestation_kind": "semantic_judge_holdout_eligibility",
        "run_id": "eval-20260729-calibration-attestation",
        "source_git_commit": "1" * 40,
        "started_at": "2026-07-29T12:00:00+00:00",
        "completed_at": "2026-07-29T12:05:00+00:00",
        "fixture_sha256": _file_sha256(FIXTURE_PATH),
        "contract_set_sha256": canonical_contract_set_sha256(cases),
        "harness": current_readonly_harness_fingerprints(settings),
        "summary": asdict(summarize_calibration(results)),
        "budget": _settled_budget(len(fixtures)),
        "results": [asdict(result) for result in results],
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_calibration_attestation_recomputes_results_summary_and_budget(
    tmp_path: Path,
) -> None:
    settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=0,
    )
    report_path = _write_json(tmp_path / "report.json", _valid_report())

    attestation = validate_calibration_attestation(
        report_path=report_path,
        settings=settings,
        now=datetime(2026, 7, 29, 13, tzinfo=UTC),
    )

    assert attestation.report_sha256 == _file_sha256(report_path)
    assert attestation.result_count == len(
        load_calibration_fixtures(FIXTURE_PATH)
    )
    assert attestation.run_id == "eval-20260729-calibration-attestation"
    assert attestation.source_git_commit == "1" * 40

    tampered = _valid_report()
    tampered["results"][0]["passed"] = False
    with pytest.raises(
        CalibrationAttestationError,
        match="result|summary",
    ):
        validate_calibration_attestation(
            report_path=_write_json(tmp_path / "tampered.json", tampered),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    unsettled = _valid_report()
    unsettled["budget"]["run"]["uncertain_count"] = 1
    with pytest.raises(
        CalibrationAttestationError,
        match="budget|schema",
    ):
        validate_calibration_attestation(
            report_path=_write_json(tmp_path / "unsettled.json", unsettled),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    understated_cumulative = _valid_report()
    understated_cumulative["budget"]["cumulative"][
        "committed_cny"
    ] = "0"
    understated_cumulative["budget"]["cumulative"][
        "settled_cny"
    ] = "0"
    understated_cumulative["budget"]["cumulative"][
        "remaining_execution_cny"
    ] = "18"
    with pytest.raises(
        CalibrationAttestationError,
        match="budget|cumulative|commit|schema",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "understated-cumulative.json",
                understated_cumulative,
            ),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    retried = _valid_report()
    retried["results"][0]["model_calls"][0]["provider_attempts"] = 2
    retried["budget"]["run"]["attempt_count"] += 1
    retried["budget"]["cumulative"]["attempt_count"] += 1
    with pytest.raises(
        CalibrationAttestationError,
        match="attempt|protocol|budget|schema",
    ):
        validate_calibration_attestation(
            report_path=_write_json(tmp_path / "retried.json", retried),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    drifted_price_unit = _valid_report()
    drifted_price_unit["budget"]["price"]["tokens_per_price_unit"] = 1
    with pytest.raises(
        CalibrationAttestationError,
        match="schema|budget|cost|usage",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "drifted-price-unit.json",
                drifted_price_unit,
            ),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    drifted_model = _valid_report()
    drifted_model["results"][0]["model_calls"][0][
        "observed_model"
    ] = "different-model"
    with pytest.raises(CalibrationAttestationError, match="model"):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "drifted-model.json",
                drifted_model,
            ),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    stale = _valid_report()
    stale["started_at"] = "2026-07-27T12:00:00+00:00"
    stale["completed_at"] = "2026-07-27T12:05:00+00:00"
    with pytest.raises(CalibrationAttestationError, match="fresh|old"):
        validate_calibration_attestation(
            report_path=_write_json(tmp_path / "stale.json", stale),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    unbound_source = _valid_report()
    unbound_source.pop("source_git_commit")
    with pytest.raises(CalibrationAttestationError, match="schema|source"):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "unbound-source.json",
                unbound_source,
            ),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("temperature", "base_url", "model"),
    [
        (1, "https://api.deepseek.com", "deepseek-v4-flash"),
        (0.7, "https://api.deepseek.com", "deepseek-v4-flash"),
        (
            0,
            "https://api.deepseek.com@attacker.example",
            "deepseek-v4-flash",
        ),
        (0, "https://api.deepseek.com/v1", "deepseek-v4-flash"),
        (0, "https://api.deepseek.com:8443", "deepseek-v4-flash"),
        (0, "https://api.deepseek.com", "attacker-model"),
    ],
)
def test_calibration_attestation_rejects_noncanonical_runtime_settings(
    tmp_path: Path,
    temperature: float,
    base_url: str,
    model: str,
) -> None:
    report = _valid_report()
    settings = Settings(
        deepseek_model=model,
        deepseek_base_url=base_url,
        deepseek_temperature=temperature,
    )
    report["harness"] = current_readonly_harness_fingerprints(settings)

    with pytest.raises(
        CalibrationAttestationError,
        match="runtime|temperature|endpoint|model",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path
                / (
                    f"runtime-{temperature}-"
                    f"{hashlib.sha256(base_url.encode()).hexdigest()[:8]}-"
                    f"{model}.json"
                ),
                report,
            ),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "error"),
        ("phase", "agent"),
        ("finish_reason", "tool_calls"),
        ("message_count", 0),
        (
            "tool_calls",
            [
                {
                    "tool_call_id": "forged-call",
                    "tool_name": "execute_action",
                    "arguments": "{}",
                }
            ],
        ),
        ("error_code", "forged_error"),
        ("http_status", 200),
        ("provider_attempts", 0),
    ],
)
def test_calibration_attestation_rejects_invalid_model_call_protocol(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    report = _valid_report()
    report["results"][0]["model_calls"][0][field] = value

    with pytest.raises(
        CalibrationAttestationError,
        match="model call|protocol|schema",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / f"invalid-call-{field}.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "started_at",
    [
        "2020-01-01T00:00:00+00:00",
        "2026-07-29T12:01:00",
        "2026-07-29T12:06:00+00:00",
    ],
)
def test_calibration_attestation_binds_call_time_to_report_budget_and_price(
    tmp_path: Path,
    started_at: str,
) -> None:
    report = _valid_report()
    report["results"][0]["model_calls"][0]["started_at"] = started_at

    with pytest.raises(
        CalibrationAttestationError,
        match="time|timestamp|protocol|schema",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / f"invalid-call-time-{started_at[:4]}.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


def test_calibration_attestation_binds_budget_identity_lifecycle(
    tmp_path: Path,
) -> None:
    report = _valid_report()
    report["budget"]["run_identity"]["started_at"] = (
        "2020-01-01T00:00:00+00:00"
    )

    with pytest.raises(
        CalibrationAttestationError,
        match="budget|time|identity|lifecycle",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "invalid-budget-identity-time.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "attack",
    [
        "missing_attempt_evidence",
        "wrong_settlement_mode",
    ],
)
def test_calibration_attestation_binds_attempt_buckets_to_model_calls(
    tmp_path: Path,
    attack: str,
) -> None:
    report = _valid_report()
    budget = report["budget"]
    if attack == "missing_attempt_evidence":
        budget.pop("attempt_evidence")
    else:
        for scope in ("run", "cumulative"):
            bucket = budget["attempt_evidence"][scope][0]
            bucket["status"] = "settled_exact"
            bucket["settlement_mode"] = "exact"

    with pytest.raises(
        CalibrationAttestationError,
        match="attempt|budget|schema|cost",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / f"{attack}.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


def test_calibration_attestation_rejects_contradictory_attempt_buckets(
    tmp_path: Path,
) -> None:
    report = _valid_report()
    budget = report["budget"]
    budget["reservation_cny_per_attempt"] = "9.999999"
    budget["run"].update(
        {
            "committed_cny": "9.999999",
            "settled_cny": "0",
            "remaining_execution_cny": "0",
            "attempt_count": 1,
            "reserved_count": 1,
            "uncertain_count": 0,
        }
    )
    budget["cumulative"].update(
        {
            "committed_cny": "38",
            "settled_cny": "0",
            "remaining_execution_cny": "0",
            "attempt_count": 2,
            "reserved_count": 0,
            "uncertain_count": 2,
        }
    )
    budget["attempt_evidence"] = {
        "run": [
            {
                "status": "reserved",
                "settlement_mode": None,
                "reserved_cny": "9.999999",
                "known_cost_cny": None,
                "count": 1,
            }
        ],
        "cumulative": [
            {
                "status": "uncertain",
                "settlement_mode": "upper_bound",
                "reserved_cny": "8",
                "known_cost_cny": "19",
                "count": 2,
            }
        ],
    }

    with pytest.raises(
        CalibrationAttestationError,
        match="attempt|budget|schema|reservation",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "contradictory-attempts.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "attack",
    [
        "forged_price",
        "lower_limits",
        "lower_reservation",
        "max_tokens_drift",
    ],
)
def test_calibration_attestation_rejects_noncanonical_budget_contract(
    tmp_path: Path,
    attack: str,
) -> None:
    report = _valid_report()
    budget = report["budget"]
    if attack == "forged_price":
        fake_sha256 = "0" * 64
        budget["run_identity"]["price_sha256"] = fake_sha256
        budget["price"]["snapshot_sha256"] = fake_sha256
        budget["price"]["rates_cny"] = {
            "prompt_cache_hit": "0",
            "prompt_cache_miss": "0",
            "completion": "0",
        }
        for scope in ("run", "cumulative"):
            budget[scope]["committed_cny"] = "0"
            budget[scope]["settled_cny"] = "0"
            budget[scope]["remaining_execution_cny"] = "18"
    elif attack == "lower_limits":
        for scope in ("run", "cumulative"):
            budget[scope]["hard_limit_cny"] = "5"
            budget[scope]["execution_limit_cny"] = "5"
            budget[scope]["remaining_execution_cny"] = format(
                Decimal("5")
                - Decimal(budget[scope]["committed_cny"]),
                "f",
            )
    elif attack == "lower_reservation":
        budget["reservation_cny_per_attempt"] = "0"
    settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=0,
        deepseek_max_tokens=(
            2048 if attack == "max_tokens_drift" else 1024
        ),
    )
    if attack == "max_tokens_drift":
        report["harness"] = current_readonly_harness_fingerprints(
            settings
        )

    with pytest.raises(
        CalibrationAttestationError,
        match=(
            "pricing|price|budget|limit|reservation|max_tokens|schema"
        ),
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / f"{attack}.json",
                report,
            ),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


def test_calibration_attestation_rejects_a_settled_execution_overrun(
    tmp_path: Path,
) -> None:
    report = _valid_report()
    report["results"][0]["model_calls"][0]["usage"] = {
        "prompt_tokens": 18_000_000,
        "completion_tokens": 0,
        "total_tokens": 18_000_000,
    }
    for scope in ("run", "cumulative"):
        report["budget"][scope]["committed_cny"] = "18.00096"
        report["budget"][scope]["settled_cny"] = "18.00096"
        report["budget"][scope]["remaining_execution_cny"] = "0"

    with pytest.raises(
        CalibrationAttestationError,
        match="budget|limit|overrun|schema",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "execution-overrun.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


def test_independent_review_is_bound_and_samples_ten_percent(
    tmp_path: Path,
) -> None:
    settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=0,
    )
    report_path = _write_json(tmp_path / "report.json", _valid_report())
    attestation = validate_calibration_attestation(
        report_path=report_path,
        settings=settings,
        now=datetime(2026, 7, 29, 13, tzinfo=UTC),
    )
    fixtures = load_calibration_fixtures(FIXTURE_PATH)
    sample_count = math.ceil(len(fixtures) * 0.10)
    required_ids = required_review_fixture_ids(attestation)
    assert len(required_ids) == sample_count
    review = {
        "schema_version": "1.0",
        "calibration_report_sha256": attestation.report_sha256,
        "reviewer_id": "independent-semantic-reviewer-v1",
        "reviewed_at": "2026-07-29T12:30:00+00:00",
        "conclusion": "GO",
        "implementation_independence_declared": True,
        "items": [
            {
                "fixture_id": fixture_id,
                "relations_match": True,
                "grounding_valid": True,
                "contradiction_label_matches": True,
                "notes": "Independently checked against the public fixture.",
            }
            for fixture_id in required_ids
        ],
        "notes": "Grounding and expected relations independently checked.",
    }
    review_path = _write_json(tmp_path / "review.json", review)

    validated_review = validate_calibration_review(
        review_path=review_path,
        attestation=attestation,
        now=datetime(2026, 7, 29, 13, tzinfo=UTC),
    )

    assert validated_review.review_sha256 == _file_sha256(review_path)
    assert validated_review.reviewed_count == sample_count

    too_small = deepcopy(review)
    too_small["items"] = too_small["items"][:-1]
    with pytest.raises(
        CalibrationAttestationError,
        match="sample|10%",
    ):
        validate_calibration_review(
            review_path=_write_json(tmp_path / "too-small.json", too_small),
            attestation=attestation,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    wrong_report = deepcopy(review)
    wrong_report["calibration_report_sha256"] = "0" * 64
    with pytest.raises(
        CalibrationAttestationError,
        match="calibration report",
    ):
        validate_calibration_review(
            review_path=_write_json(
                tmp_path / "wrong-report.json",
                wrong_report,
            ),
            attestation=attestation,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    before_report = deepcopy(review)
    before_report["reviewed_at"] = "2026-07-29T11:59:00+00:00"
    with pytest.raises(CalibrationAttestationError, match="after|time"):
        validate_calibration_review(
            review_path=_write_json(
                tmp_path / "before-report.json",
                before_report,
            ),
            attestation=attestation,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )


def test_calibration_attestations_reject_public_file_permissions(
    tmp_path: Path,
) -> None:
    settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=0,
    )
    report_path = _write_json(
        tmp_path / "public-report.json",
        _valid_report(),
    )
    report_path.chmod(0o644)

    with pytest.raises(
        CalibrationAttestationError,
        match="schema|owner-only|private",
    ):
        validate_calibration_attestation(
            report_path=report_path,
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )
