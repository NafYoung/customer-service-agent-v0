from __future__ import annotations

import shutil
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evals import run_readonly_agent_evals as runner
from evals.evidence_schema import (
    validate_readonly_payload,
)
from evals.readonly_eval import DEFAULT_CASE_DIR, ReadonlyEvalCase, load_cases
from evals.readonly_reporting import (
    build_readonly_manifest,
    result_to_record,
    summarize_results,
)
from tests.paid_gate.helpers import (
    REGRESSION_CASE_DIR,
    _CountingModel,
    _dev_repeat_payload,
    _result,
    _settings,
)


def test_public_dev_repeat_rejects_budget_identity_before_price_window() -> None:
    payload = _dev_repeat_payload()
    payload["summary"]["budget"]["run_identity"]["started_at"] = (
        "2026-07-28T00:00:00+00:00"
    )

    with pytest.raises(
        ValueError,
        match="price|window|identity|budget",
    ):
        validate_readonly_payload(payload)

@pytest.mark.parametrize(
    ("purpose", "source_dir", "case_set_name", "truncate"),
    [
        (
            "diagnostic",
            DEFAULT_CASE_DIR,
            "readonly-dev-v1",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "readonly-regression-v1",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "wrong-regression-name",
            False,
        ),
        (
            "dev_repeat",
            REGRESSION_CASE_DIR,
            "readonly-regression-v1",
            True,
        ),
    ],
)
def test_nonformal_paid_cli_rejects_noncanonical_case_identity_before_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    source_dir: Path,
    case_set_name: str,
    truncate: bool,
) -> None:
    case_dir = source_dir
    if not truncate and case_set_name in {
        "readonly-dev-v1",
        "readonly-regression-v1",
    }:
        case_dir = tmp_path / f"external-{purpose}"
        shutil.copytree(source_dir, case_dir)
    original_load_cases = runner.load_cases
    if truncate:
        monkeypatch.setattr(
            runner,
            "load_cases",
            lambda path: original_load_cases(path)[:-1],
        )
    monkeypatch.setattr(runner, "Settings", _settings)
    monkeypatch.setattr(
        runner,
        "freeze_readonly_harness",
        lambda settings: object(),
    )
    reached = {"budget": 0}

    def reject_budget(**kwargs):
        reached["budget"] += 1
        raise ValueError("budget guard must not be reached")

    monkeypatch.setattr(
        runner,
        "build_deepseek_budget_guard",
        reject_budget,
    )

    status = runner.main(
        [
            "--run-id",
            f"eval-20260729-{purpose}-gate",
            "--purpose",
            purpose,
            "--split",
            "dev",
            "--case-dir",
            str(case_dir),
            "--case-set-name",
            case_set_name,
            "--trials",
            "4" if purpose == "dev_repeat" else "1",
            "--output-root",
            str(tmp_path / "output"),
        ]
    )

    assert status == 2
    assert reached == {"budget": 0}

@pytest.mark.parametrize("attack", ["absolute", "traversal"])
def test_cli_rejects_unsafe_run_id_before_output_path_probe(
    tmp_path: Path,
    attack: str,
) -> None:
    outside = tmp_path / "outside-existing"
    outside.mkdir()
    run_id = str(outside) if attack == "absolute" else "../outside-existing"

    with pytest.raises(SystemExit):
        runner.main(
            [
                "--run-id",
                run_id,
                "--output-root",
                str(tmp_path / "output"),
            ]
        )

@pytest.mark.parametrize(
    ("purpose", "case_set_name", "trials"),
    [
        ("diagnostic", "arbitrary-diagnostic-v1", 1),
        ("dev_repeat", "arbitrary-repeat-v1", 4),
        ("unknown-purpose", "arbitrary-unknown-v1", 1),
    ],
)
def test_programmatic_runner_rejects_invalid_scope_before_model_calls(
    tmp_path: Path,
    purpose: str,
    case_set_name: str,
    trials: int,
) -> None:
    model = _CountingModel()
    output_root = tmp_path / "output"
    arbitrary_case = ReadonlyEvalCase.model_validate(
        {
            "case_id": "arbitrary-programmatic-case",
            "user_message": "arbitrary",
            "expected": {},
        }
    )

    with pytest.raises(ValueError, match="purpose|canonical|case"):
        runner.run_eval_suite(
            model=model,
            settings=_settings(),
            cases=[arbitrary_case],
            run_id="eval-20260729-programmatic-preflight",
            purpose=purpose,
            split="dev",
            case_set_name=case_set_name,
            trials=trials,
            output_root=output_root,
        )

    assert model.calls == 0
    assert not output_root.exists()

def test_diagnostic_manifest_and_schema_reject_noncanonical_identity() -> None:
    arbitrary_case = ReadonlyEvalCase.model_validate(
        {
            "case_id": "arbitrary-diagnostic-case",
            "user_message": "arbitrary",
            "expected": {},
        }
    )
    result = _result(case=arbitrary_case, trial=1)
    started = datetime(2026, 8, 20, 12, tzinfo=UTC)

    with pytest.raises(ValueError, match="canonical|case"):
        build_readonly_manifest(
            run_id="eval-20260729-diagnostic-builder-forgery",
            purpose="diagnostic",
            split="dev",
            case_set_name="arbitrary-diagnostic-v1",
            cases=[arbitrary_case],
            results=[result],
            settings=_settings(),
            planned_trials=1,
            started_at=started,
            completed_at=started + timedelta(minutes=1),
        )

    canonical_cases = load_cases(DEFAULT_CASE_DIR)
    canonical_results = [
        _result(case=case, trial=1) for case in canonical_cases
    ]
    for result in canonical_results:
        result.model_calls = (
            replace(
                result.model_calls[0],
                observed_model="offline-eval-model",
                provider_attempts=0,
            ),
        )
    manifest = build_readonly_manifest(
        run_id="eval-20260729-diagnostic-schema-forgery",
        purpose="diagnostic",
        split="dev",
        case_set_name="readonly-dev-v1",
        cases=canonical_cases,
        results=canonical_results,
        settings=_settings(),
        planned_trials=1,
        started_at=started,
        completed_at=started + timedelta(minutes=1),
    )
    manifest["eval"]["case_set_name"] = "arbitrary-diagnostic-v1"
    manifest["artifacts"] = {
        "cases": "cases.jsonl",
        "summary": "summary.json",
        "trajectories": "trajectories/",
        "integrity": "integrity.json",
    }
    records = [result_to_record(item, split="dev") for item in canonical_results]
    payload = {
        "manifest": manifest,
        "cases": records,
        "summary": summarize_results(
            run_id=manifest["run_id"],
            results=canonical_results,
            planned_trials=1,
        ),
        "trajectories": deepcopy(records),
        "integrity": {
            "schema_version": "1.0",
            "algorithm": "sha256",
            "files": {},
        },
    }

    with pytest.raises(ValueError, match="diagnostic|canonical|case"):
        validate_readonly_payload(payload)
