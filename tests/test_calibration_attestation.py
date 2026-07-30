from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.deepseek_budget import (
    DeepSeekBudgetGuard,
    SQLiteBudgetLedger,
    load_price_snapshot,
    logical_call_sha256,
)
from app.config import Settings
from evals import calibration_attestation as calibration_attestation_module
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
from evals.semantic_judge import (
    SemanticJudgeVerdict,
    semantic_verdict_content_sha256,
)

FIXTURE_PATH = Path("evals/semantic_judge_calibration_cases.jsonl")
CASE_DIR = Path("evals/readonly_regression_cases")
PRICE_SNAPSHOT_PATH = Path(
    "pricing/deepseek-v4-flash-2026-07-30.json"
)
_TRUSTED_TEST_COMMIT = "1" * 40


@pytest.fixture(autouse=True)
def _trusted_clean_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def require_clean_source(*, expected_commit=None):
        if (
            expected_commit is not None
            and expected_commit != _TRUSTED_TEST_COMMIT
        ):
            raise ValueError("stale trusted commit")
        return _TRUSTED_TEST_COMMIT

    monkeypatch.setattr(
        calibration_attestation_module,
        "require_clean_git_worktree",
        require_clean_source,
        raising=False,
    )


@pytest.fixture(autouse=True)
def _trusted_test_budget_loader(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.node.name.startswith(
        (
            "test_calibration_attestation_rejects_synthetic_report_without_live_ledger",
            "test_calibration_attestation_rejects_untrusted_ledger",
            "test_calibration_attestation_accepts_matching_temporary_ledger",
            "test_independent_review_rejects_go_when_report_verdicts_mismatch_fixtures",
        )
    ):
        return

    def read_trusted_budget(*, run_id: str) -> dict[str, object]:
        report_budget = _valid_report()["budget"]
        assert report_budget["run_identity"]["run_id"] == run_id
        return deepcopy(
            {
                "run_identity": report_budget["run_identity"],
                "run": report_budget["run"],
                "cumulative": report_budget["cumulative"],
                "attempt_evidence": report_budget["attempt_evidence"],
            }
        )

    monkeypatch.setattr(
        calibration_attestation_module,
        "_read_trusted_budget_evidence",
        read_trusted_budget,
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


def _synthetic_logical_call_sha256(index: int) -> str:
    return logical_call_sha256(
        f"synthetic-calibration-logical-call-{index:02d}"
    )


def _model_call(
    model_name: str,
    *,
    logical_call_hash: str,
    response_content_sha256: str,
) -> dict[str, object]:
    return {
        "sequence": 1,
        "status": "success",
        "started_at": "2026-08-01T12:00:00+00:00",
        "latency_ms": 1,
        "message_count": 2,
        "tool_contract_count": 0,
        "phase": "semantic_judge",
        "tool_calls": [],
        "finish_reason": "stop",
        "response_id": None,
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
        "logical_call_sha256": logical_call_hash,
        "response_content_sha256": response_content_sha256,
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


def _settled_budget(
    attempt_count: int,
    *,
    response_digests: list[str] | None = None,
) -> dict[str, object]:
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
    attempt_buckets = [
        {
            "logical_call_sha256": (
                _synthetic_logical_call_sha256(index)
            ),
            "status": "settled_upper_bound",
            "settlement_mode": "upper_bound",
            "reserved_cny": "1.002048",
            "known_cost_cny": "0.00002",
            "error_code": None,
            "completed_at": "2026-08-01T12:04:59+00:00",
            "response_content_sha256": (
                response_digests[index]
                if response_digests is not None
                else None
            ),
            "count": 1,
        }
        for index in range(attempt_count)
    ]
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
            "started_at": "2026-08-01T12:00:00+00:00",
            "completed_at": "2026-08-01T12:05:00+00:00",
        },
        "price": _canonical_price_summary(),
        "reservation_cny_per_attempt": "1.002048",
        "run": dict(snapshot),
        "cumulative": dict(snapshot),
        "attempt_evidence": {
            "run": attempt_buckets,
            "cumulative": deepcopy(attempt_buckets),
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
    results: list[CalibrationResult] = []
    digests: list[str] = []
    for index, fixture in enumerate(fixtures):
        verdict = _verdict_for_fixture(fixture)
        digest = semantic_verdict_content_sha256(
            SemanticJudgeVerdict.model_validate(verdict)
        )
        digests.append(digest)
        results.append(
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
                verdict=verdict,
                model_calls=(
                    _model_call(
                        settings.deepseek_model,
                        logical_call_hash=(
                            _synthetic_logical_call_sha256(index)
                        ),
                        response_content_sha256=digest,
                    ),
                ),
            )
        )
    return {
        "schema_version": "2.0",
        "attestation_kind": "semantic_judge_holdout_eligibility",
        "run_id": "eval-20260729-calibration-attestation",
        "source_git_commit": "1" * 40,
        "started_at": "2026-08-01T12:00:00+00:00",
        "completed_at": "2026-08-01T12:05:00+00:00",
        "fixture_sha256": _file_sha256(FIXTURE_PATH),
        "contract_set_sha256": canonical_contract_set_sha256(cases),
        "harness": current_readonly_harness_fingerprints(settings),
        "summary": asdict(summarize_calibration(results)),
        "budget": _settled_budget(len(fixtures), response_digests=digests),
        "results": [asdict(result) for result in results],
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _matching_temporary_calibration_ledger(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_digests: list[str] | None = None,
) -> tuple[Path, dict[str, object], datetime]:
    fixed_now = datetime(2026, 8, 1, 12, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(
        "app.agent.deepseek_budget.datetime",
        _FixedDateTime,
    )
    ledger_path = tmp_path / "trusted-private" / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(
        path=ledger_path,
        hard_limit_cny=Decimal("20"),
        execution_limit_cny=Decimal("18"),
    )
    price_snapshot = load_price_snapshot(PRICE_SNAPSHOT_PATH)
    run_id = "eval-20260729-calibration-attestation"
    guard = DeepSeekBudgetGuard(
        ledger=ledger,
        run_id=run_id,
        purpose="semantic_judge_calibration",
        price_snapshot=price_snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now=fixed_now,
        now_provider=lambda: fixed_now,
    )
    for index in range(49):
        reservation = guard.reserve_attempt(
            logical_call_id=(
                f"synthetic-calibration-logical-call-{index:02d}"
            ),
            attempt_number=1,
        )
        digest = report_digests[index] if report_digests else None
        guard.settle_attempt(
            reservation=reservation,
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            provider_request_id=None,
            response_content_sha256=digest,
        )
    guard.close()
    return ledger_path, guard.snapshot(), fixed_now


def _report_with_matching_temporary_ledger(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, datetime]:
    fixtures = load_calibration_fixtures(FIXTURE_PATH)
    digests = [
        semantic_verdict_content_sha256(
            SemanticJudgeVerdict.model_validate(
                _verdict_for_fixture(fixture)
            )
        )
        for fixture in fixtures
    ]
    ledger_path, budget, fixed_now = (
        _matching_temporary_calibration_ledger(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            report_digests=digests,
        )
    )
    report = _valid_report()
    run_identity = budget["run_identity"]
    assert isinstance(run_identity, dict)
    report["started_at"] = run_identity["started_at"]
    report["completed_at"] = run_identity["completed_at"]
    report["budget"] = budget
    for result in report["results"]:
        result["model_calls"][0]["started_at"] = run_identity[
            "started_at"
        ]
    report_path = _write_json(
        tmp_path / "matching-live-ledger-report.json",
        report,
    )
    return ledger_path, report_path, fixed_now


def test_calibration_attestation_rejects_synthetic_report_without_live_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_ledger = tmp_path / "missing-private-ledger.sqlite3"
    monkeypatch.setattr(
        calibration_attestation_module,
        "DEFAULT_BUDGET_LEDGER",
        missing_ledger,
        raising=False,
    )
    report_path = _write_json(
        tmp_path / "synthetic-49-of-49.json",
        _valid_report(),
    )

    with pytest.raises(
        CalibrationAttestationError,
        match="ledger|budget|persistent",
    ):
        validate_calibration_attestation(
            report_path=report_path,
            settings=Settings(deepseek_temperature=0),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )

    assert not missing_ledger.exists()


def test_calibration_attestation_accepts_matching_temporary_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, report_path, fixed_now = (
        _report_with_matching_temporary_ledger(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
        )
    )
    monkeypatch.setattr(
        calibration_attestation_module,
        "DEFAULT_BUDGET_LEDGER",
        ledger_path,
    )

    attestation = validate_calibration_attestation(
        report_path=report_path,
        settings=Settings(deepseek_temperature=0),
        now=fixed_now,
    )

    assert attestation.result_count == 49
    assert attestation.run_id == "eval-20260729-calibration-attestation"


def test_calibration_attestation_rejects_untrusted_ledger_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, report_path, fixed_now = (
        _report_with_matching_temporary_ledger(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
        )
    )
    ledger_path.chmod(0o644)
    monkeypatch.setattr(
        calibration_attestation_module,
        "DEFAULT_BUDGET_LEDGER",
        ledger_path,
    )

    with pytest.raises(
        CalibrationAttestationError,
        match="ledger|budget|persistent",
    ):
        validate_calibration_attestation(
            report_path=report_path,
            settings=Settings(deepseek_temperature=0),
            now=fixed_now,
        )


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
        now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )

    stale = _valid_report()
    stale["started_at"] = "2026-07-27T12:00:00+00:00"
    stale["completed_at"] = "2026-07-27T12:05:00+00:00"
    with pytest.raises(CalibrationAttestationError, match="fresh|old"):
        validate_calibration_attestation(
            report_path=_write_json(tmp_path / "stale.json", stale),
            settings=settings,
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )


def test_calibration_attestation_accepts_canonical_trailing_slash_endpoint(
    tmp_path: Path,
) -> None:
    report = _valid_report()
    settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_base_url="https://api.deepseek.com/",
        deepseek_temperature=0,
    )
    report["harness"] = current_readonly_harness_fingerprints(settings)

    validated = validate_calibration_attestation(
        report_path=_write_json(
            tmp_path / "canonical-trailing-slash.json",
            report,
        ),
        settings=settings,
        now=datetime(2026, 8, 1, 13, tzinfo=UTC),
    )

    assert validated.run_id == report["run_id"]


def test_calibration_attestation_rejects_caller_supplied_forged_harness(
    tmp_path: Path,
) -> None:
    report = _valid_report()
    forged_harness = dict(report["harness"])
    forged_harness["prompt_sha256"] = "0" * 64
    report["harness"] = forged_harness

    with pytest.raises(
        CalibrationAttestationError,
        match="harness|trusted|source",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "forged-self-certified-harness.json",
                report,
            ),
            settings=Settings(deepseek_temperature=0),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
            harness_fingerprints=forged_harness,
        )


def test_calibration_attestation_rejects_report_commit_not_current_trusted_head(
    tmp_path: Path,
) -> None:
    report = _valid_report()
    report["source_git_commit"] = "0" * 40

    with pytest.raises(
        CalibrationAttestationError,
        match="commit|source|trusted",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "forged-source-commit.json",
                report,
            ),
            settings=Settings(deepseek_temperature=0),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
            harness_fingerprints=report["harness"],
        )


def test_calibration_attestation_rejects_dirty_source_before_report_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = _write_json(tmp_path / "dirty-source.json", _valid_report())

    def reject_dirty_source(*, expected_commit=None):
        del expected_commit
        raise ValueError("dirty source")

    def fail_report_read(*args, **kwargs):
        del args, kwargs
        raise AssertionError("report must not be read before source precheck")

    monkeypatch.setattr(
        calibration_attestation_module,
        "require_clean_git_worktree",
        reject_dirty_source,
    )
    monkeypatch.setattr(
        calibration_attestation_module,
        "read_json_object_snapshot",
        fail_report_read,
    )

    with pytest.raises(
        CalibrationAttestationError,
        match="source|clean|trusted",
    ):
        validate_calibration_attestation(
            report_path=report_path,
            settings=Settings(deepseek_temperature=0),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )


def test_calibration_attestation_rejects_commit_drift_during_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = _write_json(tmp_path / "stale-source.json", _valid_report())
    clean_checks = 0

    def drift_after_freeze(*, expected_commit=None):
        nonlocal clean_checks
        clean_checks += 1
        if expected_commit is not None:
            raise ValueError("commit changed during trusted freeze")
        return _TRUSTED_TEST_COMMIT

    def fail_report_read(*args, **kwargs):
        del args, kwargs
        raise AssertionError("report must not be read after source drift")

    monkeypatch.setattr(
        calibration_attestation_module,
        "require_clean_git_worktree",
        drift_after_freeze,
    )
    monkeypatch.setattr(
        calibration_attestation_module,
        "read_json_object_snapshot",
        fail_report_read,
    )

    with pytest.raises(
        CalibrationAttestationError,
        match="source|commit|trusted",
    ):
        validate_calibration_attestation(
            report_path=report_path,
            settings=Settings(deepseek_temperature=0),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )

    assert clean_checks == 2


@pytest.mark.parametrize(
    "attack",
    ["final_source_tree", "final_commit"],
)
def test_calibration_attestation_rechecks_trusted_source_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    report = _valid_report()
    tree_checks = 0
    clean_checks = 0

    def source_tree_sha256():
        nonlocal tree_checks
        tree_checks += 1
        if attack == "final_source_tree" and tree_checks == 3:
            return "f" * 64
        return "e" * 64

    def require_clean_source(*, expected_commit=None):
        nonlocal clean_checks
        clean_checks += 1
        if attack == "final_commit" and clean_checks == 3:
            raise ValueError("commit drifted before validated return")
        if expected_commit not in {None, _TRUSTED_TEST_COMMIT}:
            raise ValueError("unexpected trusted commit")
        return _TRUSTED_TEST_COMMIT

    monkeypatch.setattr(
        calibration_attestation_module,
        "current_source_tree_sha256",
        source_tree_sha256,
    )
    monkeypatch.setattr(
        calibration_attestation_module,
        "require_clean_git_worktree",
        require_clean_source,
    )

    with pytest.raises(
        CalibrationAttestationError,
        match="source|commit|trusted|changed",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / f"{attack}.json",
                report,
            ),
            settings=Settings(deepseek_temperature=0),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
            harness_fingerprints=report["harness"],
        )

    assert tree_checks == 3
    assert clean_checks == (2 if attack == "final_source_tree" else 3)


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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "started_at",
    [
        "2020-01-01T00:00:00+00:00",
        "2026-08-01T12:01:00",
        "2026-08-01T12:06:00+00:00",
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
                "logical_call_sha256": "b" * 64,
                "status": "reserved",
                "settlement_mode": None,
                "reserved_cny": "9.999999",
                "known_cost_cny": None,
                "error_code": None,
                "completed_at": None,
                "count": 1,
            }
        ],
        "cumulative": [
            {
                "logical_call_sha256": "c" * 64,
                "status": "uncertain",
                "settlement_mode": "upper_bound",
                "reserved_cny": "8",
                "known_cost_cny": "19",
                "error_code": "COST_EXCEEDS_RESERVATION",
                "completed_at": "2026-08-01T12:04:59+00:00",
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
        now=datetime(2026, 8, 1, 13, tzinfo=UTC),
    )
    fixtures = load_calibration_fixtures(FIXTURE_PATH)
    sample_count = math.ceil(len(fixtures) * 0.10)
    required_ids = required_review_fixture_ids(attestation)
    assert len(required_ids) == sample_count
    review = {
        "schema_version": "1.0",
        "calibration_report_sha256": attestation.report_sha256,
        "reviewer_id": "independent-semantic-reviewer-v1",
        "reviewed_at": "2026-08-01T12:30:00+00:00",
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
        report_path=report_path,
        now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            report_path=report_path,
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
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
            report_path=report_path,
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )

    before_report = deepcopy(review)
    before_report["reviewed_at"] = "2026-08-01T11:59:00+00:00"
    with pytest.raises(CalibrationAttestationError, match="after|time"):
        validate_calibration_review(
            review_path=_write_json(
                tmp_path / "before-report.json",
                before_report,
            ),
            attestation=attestation,
            report_path=report_path,
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )


def _apply_fixture_valid_digest_divergent_rewrite(
    report: dict[str, object],
) -> dict[str, object]:
    """Mutate one result to another fixture-valid verdict with a new digest."""

    from evals.semantic_calibration import (
        validate_calibration_verdict_grounding,
    )
    from evals.semantic_judge import validate_semantic_verdict_grounding

    fixtures = load_calibration_fixtures(FIXTURE_PATH)
    cases = {case.case_id: case for case in load_cases(CASE_DIR)}
    results = report["results"]
    assert isinstance(results, list)
    for index, fixture in enumerate(fixtures):
        case = cases[fixture.case_id]
        contract = case.expected.semantic_contract
        assert contract is not None
        base = _verdict_for_fixture(fixture)
        for claim in base["claims"]:
            regions = fixture.acceptable_evidence_regions.get(
                claim["id"],
                [],
            )
            if (
                claim["relation"] in {"not_mentioned"}
                or len(regions) < 2
            ):
                continue
            rewritten = {
                "claims": [dict(item) for item in base["claims"]],
                "material_self_contradiction": base[
                    "material_self_contradiction"
                ],
                "contradiction_evidence": list(
                    base["contradiction_evidence"]
                ),
            }
            for item in rewritten["claims"]:
                if item["id"] == claim["id"]:
                    item["evidence_spans"] = [regions[1]]
                    break
            try:
                verdict = SemanticJudgeVerdict.model_validate(rewritten)
                validate_semantic_verdict_grounding(
                    verdict=verdict,
                    contract=contract,
                    assistant_answer=fixture.assistant_answer,
                )
                validate_calibration_verdict_grounding(
                    fixture=fixture,
                    verdict=verdict,
                )
            except Exception:
                continue
            result = results[index]
            assert isinstance(result, dict)
            result["verdict"] = rewritten
            result["observed_relations"] = {
                item["id"]: item["relation"]
                for item in rewritten["claims"]
            }
            return result
    pytest.skip("no fixture with an alternate grounded evidence region")


def test_calibration_attestation_rejects_rewritten_verdict_keeping_stale_response_digest(
    tmp_path: Path,
) -> None:
    """P1-1: real call digests cannot be reused after verdict rewrite."""

    report = _valid_report()
    result = _apply_fixture_valid_digest_divergent_rewrite(report)
    original_digest = result["model_calls"][0]["response_content_sha256"]
    assert isinstance(result["verdict"], dict)
    assert (
        semantic_verdict_content_sha256(
            SemanticJudgeVerdict.model_validate(result["verdict"])
        )
        != original_digest
    )

    with pytest.raises(
        CalibrationAttestationError,
        match="response digest|verdict content|protocol",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "rewritten-verdict.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )


def test_calibration_attestation_rejects_dual_rewrite_of_verdict_and_digest(
    tmp_path: Path,
) -> None:
    """P1-1: rewriting BOTH verdict and report digest still fails vs ledger."""

    report = _valid_report()
    result = _apply_fixture_valid_digest_divergent_rewrite(report)
    assert isinstance(result["verdict"], dict)
    new_digest = semantic_verdict_content_sha256(
        SemanticJudgeVerdict.model_validate(result["verdict"])
    )
    result["model_calls"][0]["response_content_sha256"] = new_digest
    call_hash = result["model_calls"][0]["logical_call_sha256"]
    budget = report["budget"]
    assert isinstance(budget, dict)
    for scope in ("run", "cumulative"):
        for bucket in budget["attempt_evidence"][scope]:
            if bucket["logical_call_sha256"] == call_hash:
                bucket["response_content_sha256"] = new_digest

    with pytest.raises(
        CalibrationAttestationError,
        match="ledger|response|digest|attempt",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "dual-rewritten.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )


def test_calibration_attestation_rejects_missing_response_content_digest(
    tmp_path: Path,
) -> None:
    report = _valid_report()
    report["results"][0]["model_calls"][0].pop("response_content_sha256")

    with pytest.raises(
        CalibrationAttestationError,
        match="response|protocol|model call",
    ):
        validate_calibration_attestation(
            report_path=_write_json(
                tmp_path / "missing-response-digest.json",
                report,
            ),
            settings=Settings(
                deepseek_model="deepseek-v4-flash",
                deepseek_temperature=0,
            ),
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )


def test_independent_review_rejects_go_when_report_verdicts_mismatch_fixtures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-2: Literal[True] review checkboxes cannot paper over bad verdicts."""

    settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=0,
    )
    ledger_path, good_report_path, fixed_now = (
        _report_with_matching_temporary_ledger(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
        )
    )
    monkeypatch.setattr(
        calibration_attestation_module,
        "DEFAULT_BUDGET_LEDGER",
        ledger_path,
        raising=False,
    )
    attestation = validate_calibration_attestation(
        report_path=good_report_path,
        settings=settings,
        now=fixed_now + timedelta(hours=1),
    )
    tampered = json.loads(good_report_path.read_text(encoding="utf-8"))
    target = tampered["results"][0]
    target_id = target["fixture_id"]
    claim = target["verdict"]["claims"][0]
    original_relation = claim["relation"]
    claim["relation"] = (
        "not_mentioned"
        if original_relation != "not_mentioned"
        else "entailed"
    )
    if claim["relation"] == "not_mentioned":
        claim["evidence_spans"] = []
    else:
        fixture = next(
            item
            for item in load_calibration_fixtures(FIXTURE_PATH)
            if item.fixture_id == target_id
        )
        regions = fixture.acceptable_evidence_regions[claim["id"]]
        claim["evidence_spans"] = [regions[0]]
    target["observed_relations"] = {
        item["id"]: item["relation"]
        for item in target["verdict"]["claims"]
    }
    target["model_calls"][0]["response_content_sha256"] = (
        semantic_verdict_content_sha256(
            SemanticJudgeVerdict.model_validate(target["verdict"])
        )
    )
    tampered_path = _write_json(tmp_path / "tampered-report.json", tampered)
    forged_attestation = type(attestation)(
        report_sha256=_file_sha256(tampered_path),
        run_id=attestation.run_id,
        source_git_commit=attestation.source_git_commit,
        fixture_sha256=attestation.fixture_sha256,
        contract_set_sha256=attestation.contract_set_sha256,
        harness_sha256=attestation.harness_sha256,
        result_count=attestation.result_count,
        fixture_ids=attestation.fixture_ids,
        fixture_kinds=attestation.fixture_kinds,
        completed_at=attestation.completed_at,
    )
    required_ids = list(required_review_fixture_ids(forged_attestation))
    review_ids = list(dict.fromkeys([*required_ids, target_id]))
    review = {
        "schema_version": "1.0",
        "calibration_report_sha256": forged_attestation.report_sha256,
        "reviewer_id": "independent-semantic-reviewer-v1",
        "reviewed_at": "2026-08-01T12:30:00+00:00",
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
            for fixture_id in review_ids
        ],
        "notes": "Grounding and expected relations independently checked.",
    }

    with pytest.raises(
        CalibrationAttestationError,
        match="relations|contradiction|grounding|fixture|replay",
    ):
        validate_calibration_review(
            review_path=_write_json(
                tmp_path / "forged-go-review.json",
                review,
            ),
            attestation=forged_attestation,
            report_path=tampered_path,
            now=fixed_now + timedelta(hours=1),
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
            now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        )
