from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.enums import (
    ActionType,
    ConfirmationSource,
    IssueType,
    ItemCondition,
    TicketPriority,
)
from app.errors import ServiceError
from app.models import Inventory, Order, ToolEvent
from app.schemas import (
    ConfirmActionRequest,
    EligibilityRequest,
    PresentApprovalRequest,
    TicketCreateRequest,
)
from app.seed import seed_demo_data
from app.tools.facade import ToolCallContext
from app.tools.factory import build_tools


@dataclass
class CaseResult:
    task_id: str
    passed: bool
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def expect(self, condition: bool, message: str) -> None:
        (self.checks if condition else self.failures).append(message)


def load_cases() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "evals" / "cases").glob("*.json"))
    ]


def run_case(case: dict[str, Any]) -> CaseResult:
    task_id = case["task_id"]
    result = CaseResult(task_id=task_id, passed=False)
    settings = Settings(
        database_url="sqlite:///:memory:",
        host_confirmation_token="reference-eval-host-token",
    )
    database = Database(settings.database_url)
    database.create_all()
    seed_demo_data(database, settings)
    tools = build_tools(settings, policy_dir=ROOT / "policies")
    conversation_id = f"eval-conversation-{task_id}"

    with database.session() as session:
        auth = tools.auth_service.authenticate(
            session=session,
            email="linfan@example.com",
            verification_code=settings.demo_verification_code,
        )
    context = ToolCallContext(
        run_id=task_id,
        auth_token=auth.access_token,
        conversation_id=conversation_id,
    )

    plan = case["reference_plan"]
    expected = case["expected"]
    observed_error: str | None = None
    observed_outcome: str | None = None
    eligibility = None
    inventory_before: int | None = None
    inventory_after: int | None = None
    host_stages: list[str] = []

    try:
        if plan["kind"] == "read_order":
            with database.session() as session:
                tools.get_order(
                    session,
                    order_id=plan["order_id"],
                    context=context,
                )

        elif plan["kind"] == "action":
            request = EligibilityRequest.model_validate(plan["request"])
            if ActionType(request.action_type) == ActionType.EXCHANGE_ITEM and request.target_size:
                with database.session() as session:
                    order = cast(Order, session.get(Order, request.order_id))
                    item = next(
                        item for item in order.items if item.id == request.order_item_id
                    )
                    inventory = session.scalar(
                        select(Inventory).where(
                            Inventory.sku == item.sku,
                            Inventory.size == request.target_size,
                        )
                    )
                    inventory_before = inventory.available_qty if inventory else 0

            with database.session() as session:
                eligibility = tools.check_action_eligibility(
                    session,
                    request=request,
                    context=context,
                )

            approval = None
            if plan.get("prepare"):
                action_type = ActionType(request.action_type)
                with database.session() as session:
                    if action_type == ActionType.CANCEL_ORDER:
                        approval = tools.prepare_cancel_order(
                            session,
                            order_id=request.order_id,
                            user_note="reference eval",
                            context=context,
                        )
                    elif action_type == ActionType.RETURN_ITEM:
                        approval = tools.prepare_return(
                            session,
                            order_id=request.order_id,
                            order_item_id=request.order_item_id or "",
                            declared_condition=ItemCondition(request.declared_condition),
                            issue_type=IssueType(request.issue_type),
                            user_note="reference eval",
                            context=context,
                        )
                    elif action_type == ActionType.EXCHANGE_ITEM:
                        approval = tools.prepare_exchange(
                            session,
                            order_id=request.order_id,
                            order_item_id=request.order_item_id or "",
                            target_size=request.target_size or "",
                            declared_condition=ItemCondition(request.declared_condition),
                            issue_type=IssueType(request.issue_type),
                            user_note="reference eval",
                            context=context,
                        )

            if approval and plan.get("present"):
                with database.session() as session:
                    tools.action_service.present_action(
                        session,
                        customer_id=auth.customer_id,
                        conversation_id=conversation_id,
                        approval_id=approval.approval_id,
                        request=PresentApprovalRequest(
                            preview_hash=approval.preview_hash,
                        ),
                    )
                host_stages.append("PRESENTED")

            if approval and (
                plan.get("confirm")
                or plan.get("attempt_confirm_without_presenting")
            ):
                with database.session() as session:
                    confirmation = tools.action_service.record_confirmation(
                        session,
                        customer_id=auth.customer_id,
                        conversation_id=conversation_id,
                        approval_id=approval.approval_id,
                        request=ConfirmActionRequest(
                            preview_hash=approval.preview_hash,
                            ui_event_id=f"eval-ui-{task_id}",
                            confirmation_source=ConfirmationSource.BUTTON,
                        ),
                    )
                    execution = None
                    if plan.get("confirm"):
                        execution = tools.action_service.execute_confirmed_action(
                            session,
                            customer_id=auth.customer_id,
                            conversation_id=conversation_id,
                            approval_id=approval.approval_id,
                            confirmation_event_id=confirmation.confirmation_event_id,
                        )
                host_stages.append("CONFIRMED")
                if execution is not None:
                    host_stages.append("EXECUTED")
                    observed_outcome = str(execution.result.get("outcome"))

            if (
                eligibility is not None
                and str(eligibility.reason_code) == "HUMAN_REVIEW_REQUIRED"
                and plan.get("create_ticket_on_human_review")
            ):
                with database.session() as session:
                    tools.create_handoff_ticket(
                        session,
                        request=TicketCreateRequest(
                            order_id=request.order_id,
                            category="DEFECTIVE_ITEM",
                            summary="Reference eval: route defective item to human review.",
                            priority=TicketPriority.HIGH,
                        ),
                        context=context,
                    )
                    observed_outcome = "HANDOFF_TICKET_CREATED"

            if ActionType(request.action_type) == ActionType.EXCHANGE_ITEM and request.target_size:
                with database.session() as session:
                    order = cast(Order, session.get(Order, request.order_id))
                    item = next(
                        item for item in order.items if item.id == request.order_item_id
                    )
                    inventory = session.scalar(
                        select(Inventory).where(
                            Inventory.sku == item.sku,
                            Inventory.size == request.target_size,
                        )
                    )
                    inventory_after = inventory.available_qty if inventory else 0
        else:
            raise ValueError(f"Unsupported plan kind: {plan['kind']}")

    except ServiceError as exc:
        observed_error = exc.code

    if "eligibility_allowed" in expected:
        result.expect(
            eligibility is not None
            and eligibility.allowed is expected["eligibility_allowed"],
            f"eligibility_allowed == {expected['eligibility_allowed']}",
        )
    if "eligibility_reason" in expected:
        result.expect(
            eligibility is not None
            and str(eligibility.reason_code) == expected["eligibility_reason"],
            f"eligibility_reason == {expected['eligibility_reason']}",
        )
    if "outcome" in expected:
        result.expect(
            observed_outcome == expected["outcome"],
            f"outcome == {expected['outcome']}",
        )
    if "error_code" in expected:
        result.expect(
            observed_error == expected["error_code"],
            f"error_code == {expected['error_code']}",
        )
    if "final_order_status" in expected:
        order_id = plan.get("request", {}).get("order_id")
        with database.session() as session:
            final_order = session.get(Order, order_id)
            final_status = final_order.status if final_order else None
        result.expect(
            final_status == expected["final_order_status"],
            f"final_order_status == {expected['final_order_status']}",
        )
    if "target_inventory_delta" in expected:
        observed_delta = (
            inventory_after - inventory_before
            if inventory_after is not None and inventory_before is not None
            else None
        )
        result.expect(
            observed_delta == expected["target_inventory_delta"],
            f"target_inventory_delta == {expected['target_inventory_delta']}",
        )

    with database.session() as session:
        tool_names = session.scalars(
            select(ToolEvent.tool_name)
            .where(ToolEvent.run_id == task_id)
            .order_by(ToolEvent.created_at)
        ).all()

    for required in expected.get("required_tools", []):
        result.expect(required in tool_names, f"required tool called: {required}")
    for forbidden in expected.get("forbidden_tools", []):
        result.expect(forbidden not in tool_names, f"forbidden tool not called: {forbidden}")
    for required in expected.get("required_host_stages", []):
        result.expect(required in host_stages, f"required host stage reached: {required}")
    for forbidden in expected.get("forbidden_host_stages", []):
        result.expect(
            forbidden not in host_stages,
            f"forbidden host stage not reached: {forbidden}",
        )

    result.passed = not result.failures
    database.engine.dispose()
    return result


def main() -> int:
    results = [run_case(case) for case in load_cases()]
    print("| case | result | checks |")
    print("|---|---:|---:|")
    for item in results:
        status = "PASS" if item.passed else "FAIL"
        print(f"| {item.task_id} | {status} | {len(item.checks)} |")
        for failure in item.failures:
            print(f"  - {item.task_id}: {failure}")

    passed = sum(item.passed for item in results)
    print(f"\n{passed}/{len(results)} reference cases passed.")
    print(
        "This runner validates the deterministic environment, tool contracts, "
        "and graders. It does not yet measure an LLM's language understanding."
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
