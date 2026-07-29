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


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verdict_for_fixture(fixture) -> dict[str, object]:
    visible_answer = fixture.assistant_answer
    grounded_span = visible_answer[: min(300, len(visible_answer))]
    claims = [
        {
            "id": claim_id,
            "relation": relation,
            "evidence_spans": (
                [] if relation == "not_mentioned" else [grounded_span]
            ),
        }
        for claim_id, relation
        in fixture.effective_expected_relations.items()
    ]
    contradiction_evidence: list[str] = []
    if fixture.expected_material_self_contradiction:
        contradiction_evidence = [
            visible_answer[0],
            visible_answer[-1],
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
        "price": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "currency": "CNY",
            "snapshot_sha256": "c" * 64,
            "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
            "usage_source_url": (
                "https://api-docs.deepseek.com/api/create-chat-completion/"
            ),
            "captured_at": "2026-07-29T08:58:58+00:00",
            "valid_until": "2026-07-30T08:58:58+00:00",
            "rates_cny": {
                "prompt_cache_hit": "0.02",
                "prompt_cache_miss": "1",
                "completion": "2",
            },
            "tokens_per_price_unit": 1_000_000,
        },
        "reservation_cny_per_attempt": "0.01",
        "run": dict(snapshot),
        "cumulative": dict(snapshot),
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
    with pytest.raises(CalibrationAttestationError, match="budget"):
        validate_calibration_attestation(
            report_path=_write_json(tmp_path / "unsettled.json", unsettled),
            settings=settings,
            now=datetime(2026, 7, 29, 13, tzinfo=UTC),
        )

    retried = _valid_report()
    retried["results"][0]["model_calls"][0]["provider_attempts"] = 2
    retried["budget"]["run"]["attempt_count"] += 1
    retried["budget"]["cumulative"]["attempt_count"] += 1
    with pytest.raises(
        CalibrationAttestationError,
        match="attempt|protocol|budget",
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
        match="budget|cost|usage",
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
