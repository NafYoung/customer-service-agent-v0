"""Offline shadow replay: scripted demo coverage and risk report.

Drives each case's natural-language message through the same deterministic
scripted path as the public demo (no model, no network, zero cost) and reports
what the automated flow would cover versus which pending actions would
contradict the case's frozen expectations (risk). This is NOT a model
evaluation and must never be reported as one.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from app.config import Settings
from app.demo import DEMO_AGENT_MODE_PREPARATION_SCRIPTED
from app.demo.replay import handle_message
from app.demo.session import DemoSession, DemoSessionManager
from app.errors import ServiceError
from app.models import ActionExecution, SupportTicket

DEFAULT_CASE_DIR = PROJECT_ROOT / "evals" / "readonly_regression_cases"


@dataclasses.dataclass(frozen=True)
class ShadowCaseResult:
    case_id: str
    covered: bool
    risk_prepare_contradicts_expectation: bool
    tool_trace: tuple[dict[str, object], ...]
    handoff_ticket_ids: tuple[str, ...]
    business_writes: int
    error_code: str | None = None


def _load_cases(case_dir: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for path in sorted(case_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        cases.append(payload)
    if not cases:
        raise ValueError("shadow case directory contains no JSON cases")
    return cases


def _forbids_prepare(expected: dict[str, object]) -> bool:
    forbidden = cast(list[str], expected.get("forbidden_tools") or [])
    return any(tool.startswith("prepare_") for tool in forbidden)


def _replay_case(demo: DemoSession, case: dict[str, object]) -> ShadowCaseResult:
    case_id = str(case["case_id"])
    try:
        outcome = handle_message(demo, str(case["user_message"]))
        error_code = None
    except ServiceError as exc:
        outcome = None
        error_code = exc.code

    with demo.database.session() as db:
        writes = len(list(db.scalars(select(ActionExecution)).all()))
        tickets = tuple(
            ticket.id for ticket in db.scalars(select(SupportTicket)).all()
        )

    covered = bool(outcome is not None and outcome.has_pending)
    expected = case.get("expected")
    contradicts = (
        covered
        and isinstance(expected, dict)
        and _forbids_prepare(expected)
    )
    return ShadowCaseResult(
        case_id=case_id,
        covered=covered,
        risk_prepare_contradicts_expectation=contradicts,
        tool_trace=tuple(outcome.tool_trace) if outcome is not None else (),
        handoff_ticket_ids=tickets,
        business_writes=writes,
        error_code=error_code,
    )


def run_shadow_replay(case_dir: Path | None = None) -> dict[str, object]:
    """Replay all cases through the offline scripted demo path."""

    resolved_dir = case_dir or DEFAULT_CASE_DIR
    cases = _load_cases(resolved_dir)
    settings = Settings(
        app_mode="local",
        demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
        demo_allowed_origin="http://127.0.0.1:8000",
        demo_cookie_secure=False,
        host_confirmation_token="shadow-offline-host-token",
    )
    manager = DemoSessionManager(settings)
    results: list[ShadowCaseResult] = []
    try:
        for case in cases:
            _cookie, demo = manager.create()
            results.append(_replay_case(demo, case))
    finally:
        manager.dispose_all()

    covered = sum(1 for result in results if result.covered)
    risk = sum(
        1 for result in results if result.risk_prepare_contradicts_expectation
    )
    return {
        "schema_version": "1.0",
        "mode": "offline_shadow_scripted",
        "case_dir": str(resolved_dir),
        "case_count": len(results),
        "covered_count": covered,
        "risk_count": risk,
        "business_writes": sum(result.business_writes for result in results),
        "provider_http_calls": 0,
        "settled_cny": "0",
        "cases": [dataclasses.asdict(result) for result in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline scripted shadow replay (not a model evaluation)."
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=DEFAULT_CASE_DIR,
        help="Directory of case JSON files (default: public regression set).",
    )
    args = parser.parse_args(argv)
    report = run_shadow_replay(args.case_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        f"shadow: {report['covered_count']}/{report['case_count']} covered, "
        f"risk {report['risk_count']}, writes {report['business_writes']}, "
        f"cost {report['settled_cny']} CNY",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
