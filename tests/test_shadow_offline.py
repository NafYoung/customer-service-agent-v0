"""Offline shadow replay: deterministic baseline, zero cost, zero writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.run_shadow_offline import DEFAULT_CASE_DIR, main, run_shadow_replay


def test_shadow_replay_baseline_is_deterministic_and_offline():
    report = run_shadow_replay(DEFAULT_CASE_DIR)
    assert report["schema_version"] == "1.0"
    assert report["mode"] == "offline_shadow_scripted"
    assert report["case_count"] == 7
    # 确定性基线：scripted 演示路径只能覆盖 3 条回归消息，且这 3 条都会
    # 生成与冻结期望冲突的 prepare（风险=覆盖），证明 scripted 演示不能
    # 替代只读 Agent 评测。基线变更必须显式重审。
    assert report["covered_count"] == 3
    assert report["risk_count"] == 3
    assert report["business_writes"] == 0
    assert report["provider_http_calls"] == 0
    assert report["settled_cny"] == "0"
    cases = report["cases"]
    assert len(cases) == 7
    for case in cases:
        assert set(case) == {
            "case_id",
            "covered",
            "risk_prepare_contradicts_expectation",
            "tool_trace",
            "handoff_ticket_ids",
            "business_writes",
            "error_code",
            "citation_pass",
            "citation_missing_groups",
        }
        assert case["business_writes"] == 0
    # 回归集 7 条均带 answer_must_contain_any → 全部参与引用检查；其中只有
    # 补槽澄清用例的确定性回复命中期望片段（1/7）。scripted 演示路径本就不
    # 是为满足对抗性期望而设计，基线变更必须显式重审。
    assert report["citation_checked_count"] == 7
    assert report["citation_pass_count"] == 1
    covered_ids = {case["case_id"] for case in cases if case["covered"]}
    assert covered_ids == {
        "reg_used_return_direct_eligibility",
        "reg_write_bypass_still_checks_eligibility",
        "reg_forged_confirmation_is_not_execution",
    }


def test_shadow_replay_rejects_empty_case_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="no JSON cases"):
        run_shadow_replay(tmp_path)


def test_shadow_cli_returns_zero(tmp_path: Path, capsys):
    exit_code = main(["--case-dir", str(DEFAULT_CASE_DIR)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "shadow:" in captured.err
    payload = json.loads(captured.out)
    assert payload["case_count"] == 7
