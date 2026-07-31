from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.demo.schemas import DemoConfirmationCard, DemoConfirmResponse
from app.demo.session import DemoSession, bump_or_limit
from app.enums import ApprovalStatus, ConfirmationSource
from app.errors import ConflictError, NotFoundError, ServiceError
from app.models import Approval
from app.schemas import ConfirmActionRequest, PresentApprovalRequest
from app.services.actions import ActionService


def _active_approvals(
    session: Session,
    *,
    customer_id: str,
    conversation_id: str,
    server_run_id: str,
) -> list[Approval]:
    return list(
        session.scalars(
            select(Approval).where(
                Approval.customer_id == customer_id,
                Approval.conversation_id == conversation_id,
                Approval.origin_server_run_id == server_run_id,
                Approval.status.in_(
                    (
                        ApprovalStatus.PREPARED.value,
                        ApprovalStatus.PRESENTED.value,
                        ApprovalStatus.CONFIRMED.value,
                    )
                ),
            )
        ).all()
    )


def load_pending_approval(db: Session, demo: DemoSession) -> Approval:
    if not demo.pending_approval_id or not demo.pending_preview_hash:
        raise NotFoundError(
            "DEMO_NO_PENDING_ACTION",
            "当前没有待确认操作。",
            status_code=404,
        )
    approvals = _active_approvals(
        db,
        customer_id=demo.customer_id,
        conversation_id=demo.conversation_id,
        server_run_id=demo.server_run_id,
    )
    if len(approvals) != 1:
        raise ConflictError(
            "DEMO_PENDING_ACTION_AMBIGUOUS",
            "待确认操作状态不一致，已失败关闭。",
            status_code=409,
        )
    approval = approvals[0]
    if approval.id != demo.pending_approval_id:
        raise ConflictError(
            "DEMO_PENDING_ACTION_MISMATCH",
            "会话中的待确认操作与数据库不一致。",
            status_code=409,
        )
    ActionService._assert_preview_matches(approval, demo.pending_preview_hash)
    if approval.origin_server_run_id != demo.server_run_id:
        raise ConflictError(
            "DEMO_ORIGIN_MISMATCH",
            "审批来源不是当前服务端运行。",
            status_code=409,
        )
    return approval


def project_confirmation_card(approval: Approval) -> DemoConfirmationCard:
    preview: dict[str, Any] = dict(approval.preview or {})
    known_actions = {"CANCEL_ORDER", "RETURN_ITEM", "EXCHANGE_ITEM"}
    action_type = str(preview.get("action_type") or approval.action_type)
    if action_type not in known_actions:
        raise ServiceError(
            "DEMO_UNKNOWN_ACTION",
            "未知操作类型，确认卡失败关闭。",
            status_code=500,
        )
    effect = preview.get("effect")
    policy_decision = preview.get("policy_decision")
    if not isinstance(effect, str) or not isinstance(policy_decision, str):
        raise ServiceError(
            "DEMO_PREVIEW_INCOMPLETE",
            "规范预览缺少必要展示字段。",
            status_code=500,
        )
    return DemoConfirmationCard(
        action_type=action_type,
        order_id=str(preview.get("order_id") or approval.order_id),
        order_item_id=(
            str(preview["order_item_id"])
            if preview.get("order_item_id") is not None
            else approval.order_item_id
        ),
        product_name=(
            str(preview["product_name"])
            if preview.get("product_name") is not None
            else None
        ),
        size=str(preview["size"]) if preview.get("size") is not None else None,
        current_size=(
            str(preview["current_size"])
            if preview.get("current_size") is not None
            else None
        ),
        target_size=(
            str(preview["target_size"])
            if preview.get("target_size") is not None
            else None
        ),
        quantity=1 if action_type != "CANCEL_ORDER" else None,
        effect=effect,
        policy_decision=policy_decision,
        current_order_status=(
            str(preview["current_order_status"])
            if preview.get("current_order_status") is not None
            else None
        ),
        declared_condition=(
            str(preview["declared_condition"])
            if preview.get("declared_condition") is not None
            else None
        ),
        issue_type=(
            str(preview["issue_type"])
            if preview.get("issue_type") is not None
            else None
        ),
        expires_at=approval.expires_at,
        status=approval.status,
        executed=approval.status == ApprovalStatus.EXECUTED.value,
    )


def present_pending(demo: DemoSession) -> DemoConfirmationCard:
    with demo.database.session() as db:
        approval = load_pending_approval(db, demo)
        assert demo.pending_preview_hash is not None
        demo.tools.action_service.present_action(
            db,
            customer_id=demo.customer_id,
            conversation_id=demo.conversation_id,
            approval_id=approval.id,
            request=PresentApprovalRequest(preview_hash=demo.pending_preview_hash),
        )
        refreshed = load_pending_approval(db, demo)
        return project_confirmation_card(refreshed)


def confirm_pending(
    demo: DemoSession,
    *,
    provider_http_calls: int,
) -> DemoConfirmResponse:
    if not demo.pending_ui_event_id or not demo.pending_preview_hash:
        raise NotFoundError(
            "DEMO_NO_PENDING_ACTION",
            "当前没有待确认操作。",
            status_code=404,
        )
    bump_or_limit(
        demo,
        counter="confirm_count",
        limit=demo.settings.demo_max_confirm_per_session,
        code="DEMO_CONFIRM_LIMIT",
        message="本会话确认次数已达上限，请重置演示。",
    )
    approval_id = demo.pending_approval_id
    preview_hash = demo.pending_preview_hash
    ui_event_id = demo.pending_ui_event_id
    assert approval_id is not None

    with demo.database.session() as db:
        confirmation = demo.tools.action_service.record_confirmation(
            db,
            customer_id=demo.customer_id,
            conversation_id=demo.conversation_id,
            approval_id=approval_id,
            request=ConfirmActionRequest(
                preview_hash=preview_hash,
                ui_event_id=ui_event_id,
                confirmation_source=ConfirmationSource.BUTTON,
            ),
        )

    with demo.database.session() as db:
        executed = demo.tools.action_service.execute_confirmed_action(
            db,
            customer_id=demo.customer_id,
            conversation_id=demo.conversation_id,
            approval_id=approval_id,
            confirmation_event_id=confirmation.confirmation_event_id,
        )

    demo.pending_approval_id = None
    demo.pending_preview_hash = None
    demo.pending_ui_event_id = None

    result = executed.result or {}
    summary_parts = [
        f"操作 {executed.action_type} 已执行",
        f"订单 {result.get('order_id', approval_id)}",
    ]
    if "final_order_status" in result:
        summary_parts.append(f"订单状态 → {result['final_order_status']}")
    if "outcome" in result:
        summary_parts.append(str(result["outcome"]))
    return DemoConfirmResponse(
        status="EXECUTED",
        action_type=str(executed.action_type),
        order_id=str(result.get("order_id") or ""),
        result_summary="；".join(summary_parts),
        idempotent_replay=executed.idempotent_replay,
        provider_http_calls=provider_http_calls,
    )
