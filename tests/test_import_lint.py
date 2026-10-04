from __future__ import annotations

from pathlib import Path

from scripts import export_contracts
from scripts.check_import_lint import (
    HOST_ADAPTER,
    PRICE_MODULE,
    find_import_lint_violations,
    source_violations,
)


def test_committed_app_import_graph_is_clean() -> None:
    assert find_import_lint_violations() == []
    assert export_contracts.main(["--check"]) == 0


def test_importing_action_service_mark_failed_from_agent_fails() -> None:
    source = (
        "from app.services.actions import ActionService\n"
        "ActionService.mark_failed\n"
    )

    violations = source_violations(source, path="app/agent/forbidden.py")

    assert (
        "app/agent/forbidden.py:2: app/agent must not write orders "
        f"(mark_failed). 改 {HOST_ADAPTER}"
    ) in violations
    assert (
        "app/agent/forbidden.py:1: app/agent must not write orders "
        f"(ActionService). 改 {HOST_ADAPTER}"
    ) in violations


def test_new_app_evals_import_fails() -> None:
    source = "from evals.canonical_pricing import load_canonical_price_snapshot\n"

    assert source_violations(source, path="app/agent/budget/extra.py") == [
        "app/agent/budget/extra.py:1: app/ must not import evals "
        f"(evals.canonical_pricing.load_canonical_price_snapshot). 改 {PRICE_MODULE}"
    ]


def test_live_runner_canonical_price_import_stays_allowed() -> None:
    source = "from evals.canonical_pricing import load_canonical_price_snapshot\n"

    assert source_violations(source, path="app/demo/live_runner.py") == []


def test_demo_private_action_service_call_fails() -> None:
    source = "ActionService._assert_preview_matches(approval, preview_hash)\n"

    assert source_violations(source, path="app/demo/host.py") == [
        "app/demo/host.py:1: demo must not call ActionService private methods "
        f"(_assert_preview_matches). 改 {HOST_ADAPTER}"
    ]


def test_demo_host_uses_adapter_preview_check() -> None:
    host = Path("app/demo/host.py").read_text(encoding="utf-8")
    adapter = Path("app/host/confirmation.py").read_text(encoding="utf-8")

    assert "ActionService._assert_preview_matches" not in host
    assert "assert_preview_matches(" in host
    assert "from app.host.confirmation import" in host
    assert "def assert_preview_matches(" in adapter


def test_assert_preview_matches_stays_off_demo() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    hits = sorted(
        path.relative_to(app_root.parent).as_posix()
        for path in app_root.rglob("*.py")
        if "_assert_preview_matches" in path.read_text(encoding="utf-8")
    )
    assert hits == [
        "app/host/confirmation.py",
        "app/services/actions.py",
    ]
