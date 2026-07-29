from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from evals.calibration_attestation import (
    CalibrationAttestationError,
    canonical_contract_set_sha256,
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
        for claim_id, relation in fixture.expected_relations.items()
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
    snapshot = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": "0.01",
        "settled_cny": "0.01",
        "remaining_execution_cny": "17.99",
        "attempt_count": attempt_count,
        "reserved_count": 0,
        "uncertain_count": 0,
    }
    return {
        "schema_version": "1.0",
        "enforcement_mode": "persistent_sqlite",
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
            "rates_cny": {},
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
            observed_relations=dict(fixture.expected_relations),
            verdict=_verdict_for_fixture(fixture),
            model_calls=(_model_call(settings.deepseek_model),),
        )
        for fixture in fixtures
    ]
    return {
        "schema_version": "2.0",
        "attestation_kind": "semantic_judge_holdout_eligibility",
        "run_id": "eval-20260729-calibration-attestation",
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
    )

    assert attestation.report_sha256 == _file_sha256(report_path)
    assert attestation.result_count == len(
        load_calibration_fixtures(FIXTURE_PATH)
    )
    assert attestation.run_id == "eval-20260729-calibration-attestation"

    tampered = _valid_report()
    tampered["results"][0]["passed"] = False
    with pytest.raises(
        CalibrationAttestationError,
        match="result|summary",
    ):
        validate_calibration_attestation(
            report_path=_write_json(tmp_path / "tampered.json", tampered),
            settings=settings,
        )

    unsettled = _valid_report()
    unsettled["budget"]["run"]["uncertain_count"] = 1
    with pytest.raises(CalibrationAttestationError, match="budget"):
        validate_calibration_attestation(
            report_path=_write_json(tmp_path / "unsettled.json", unsettled),
            settings=settings,
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
    )
    fixtures = load_calibration_fixtures(FIXTURE_PATH)
    sample_count = math.ceil(len(fixtures) * 0.10)
    review = {
        "schema_version": "1.0",
        "calibration_report_sha256": attestation.report_sha256,
        "reviewer_id": "independent-semantic-reviewer-v1",
        "reviewed_at": datetime.now(UTC).isoformat(),
        "conclusion": "GO",
        "reviewed_fixture_ids": [
            fixture.fixture_id
            for fixture in fixtures[:sample_count]
        ],
        "notes": "Grounding and expected relations independently checked.",
    }
    review_path = _write_json(tmp_path / "review.json", review)

    validated_review = validate_calibration_review(
        review_path=review_path,
        attestation=attestation,
    )

    assert validated_review.review_sha256 == _file_sha256(review_path)
    assert validated_review.reviewed_count == sample_count

    too_small = deepcopy(review)
    too_small["reviewed_fixture_ids"] = too_small[
        "reviewed_fixture_ids"
    ][:-1]
    with pytest.raises(CalibrationAttestationError, match="10%"):
        validate_calibration_review(
            review_path=_write_json(tmp_path / "too-small.json", too_small),
            attestation=attestation,
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
        )
