from __future__ import annotations

import json
from pathlib import Path

from evals.readonly_eval import load_cases

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "evals" / "readonly_holdout_v1.manifest.json"
REGRESSION_CASE_DIR = ROOT / "evals" / "readonly_regression_cases"
POSTMORTEM_PATH = (
    ROOT / "docs" / "testing" / "readonly-holdout-v1-postmortem.md"
)


def test_retired_holdout_manifest_records_single_formal_run() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["case_set_name"] == "readonly-holdout-v1"
    assert manifest["case_count"] == 20
    assert manifest["formal_runs_allowed"] == 1
    assert manifest["formal_runs_completed"] == 1
    assert manifest["lifecycle_status"] == "retired"
    assert manifest["rerun_policy"] == "prohibited"
    assert manifest["formal_run"]["strict"] == {
        "passed": 46,
        "total": 80,
        "rate": 0.575,
    }
    assert manifest["formal_run"]["pass_power_4"] == 0.35
    assert manifest["formal_run"]["business_state_changed_trials"] == 0
    assert manifest["formal_run"]["cost_cny"] == "0.08381112"


def test_public_manifest_does_not_disclose_private_case_fields() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8")

    for forbidden in (
        "case_id",
        "user_message",
        "expected",
        "/Users/",
        "artifacts/private",
    ):
        assert forbidden not in text


def test_public_regression_cases_are_valid_and_distinct() -> None:
    cases = load_cases(REGRESSION_CASE_DIR)

    assert len(cases) == 7
    assert len({case.case_id for case in cases}) == 7
    assert all(case.case_id.startswith("reg_") for case in cases)


def test_public_postmortem_marks_v1_retired_without_private_content() -> None:
    text = POSTMORTEM_PATH.read_text(encoding="utf-8")

    assert "已退役" in text
    assert "禁止重跑" in text
    assert "46 / 80" in text
    assert "¥0.08381112" in text
    assert "artifacts/private" not in text
    assert "hv1_" not in text
