"""Citation enforcement: prompts, scripted replies, independent citation cases."""

from __future__ import annotations

from pathlib import Path

from app.demo.matches import ReplayMatch
from app.demo.preparation_runner import _final_reply_for
from evals.run_shadow_offline import run_shadow_replay

CITATION_CASE_DIR = (
    Path(__file__).resolve().parents[1] / "evals" / "readonly_citation_cases"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_prompts_require_non_fabricated_citations():
    preparation = (
        PROJECT_ROOT / "app" / "agent" / "preparation_system_prompt.md"
    ).read_text(encoding="utf-8")
    readonly = (
        PROJECT_ROOT / "app" / "agent" / "readonly_system_prompt.md"
    ).read_text(encoding="utf-8")
    for prompt in (preparation, readonly):
        assert "Evidence citations" in prompt
        assert "Never invent" in prompt
        assert "policy id and version" in prompt


def test_scripted_replies_carry_policy_citations():
    cancel = _final_reply_for(
        ReplayMatch(kind="cancel", reply="", order_id="ORD-1001")
    )
    assert "POL-CANCEL-001 v0.1" in cancel
    assert "ORD-1001" in cancel

    returned = _final_reply_for(
        ReplayMatch(
            kind="return",
            reply="",
            order_id="ORD-1003",
            order_item_id="ITEM-1003-A",
        )
    )
    assert "POL-RETURN-001 v0.1" in returned

    exchange = _final_reply_for(
        ReplayMatch(
            kind="exchange",
            reply="",
            order_id="ORD-1003",
            order_item_id="ITEM-1003-A",
            target_size="43",
        )
    )
    assert "POL-EXCHANGE-001 v0.1" in exchange
    assert "43" in exchange


def test_citation_cases_pass_offline_shadow_check():
    report = run_shadow_replay(CITATION_CASE_DIR)
    assert report["case_count"] == 3
    assert report["citation_checked_count"] == 3
    assert report["citation_pass_count"] == 3
    assert report["business_writes"] == 0
    assert report["settled_cny"] == "0"
    for case in report["cases"]:
        assert case["citation_pass"] is True, case
        assert case["citation_missing_groups"] == ()
