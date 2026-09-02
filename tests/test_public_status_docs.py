from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 现役入口：禁止再写已过时的「未接入 / 未部署」说法。
# 调研报告与历史 TDD 不在本门范围内。
_STATUS_DOCS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "docs" / "README_DETAILED.md",
    PROJECT_ROOT / "docs" / "09_project_status.md",
    PROJECT_ROOT / "docs" / "10_public_demo_status.md",
    PROJECT_ROOT / "docs" / "12_phase6_publish_checklist.md",
    PROJECT_ROOT / "docs" / "15_review_recommendations.md",
)

_FORBIDDEN_PHRASES = (
    "公开宿主 UI 尚未接入",
    "可选托管演示仍待选平台",
    "托管演示 URL | **未部署**",
    "托管演示 URL |**未部署**",
    "托管仍见",
)


def test_status_docs_exist_and_name_the_public_demo() -> None:
    for path in _STATUS_DOCS:
        assert path.is_file(), f"missing status doc: {path}"
        text = path.read_text(encoding="utf-8")
        assert "rivet-public-demo.onrender.com" in text, path


def test_status_docs_do_not_claim_ui_or_demo_are_missing() -> None:
    for path in _STATUS_DOCS:
        text = path.read_text(encoding="utf-8")
        for phrase in _FORBIDDEN_PHRASES:
            assert phrase not in text, f"{path} still contains {phrase!r}"


def test_price_snapshot_expiry_is_documented_without_rewriting_rates() -> None:
    status = (PROJECT_ROOT / "docs" / "09_project_status.md").read_text(
        encoding="utf-8"
    )
    assert "valid_until" in status
    assert "墙钟已过期" in status
    assert "失败关闭" in status

    snapshot = (
        PROJECT_ROOT / "pricing" / "deepseek-v4-flash-2026-08-19.json"
    ).read_text(encoding="utf-8")
    assert '"valid_until": "2026-08-26T03:15:00Z"' in snapshot
