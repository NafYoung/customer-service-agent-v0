from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.rules import (
    RULES_VERSION,
    check_cancel_eligibility,
    check_exchange_eligibility,
    check_return_eligibility,
)
from app.domain.state_machine import assert_order_transition
from app.enums import (
    ActionType,
    ApprovalStatus,
    ConfirmationSource,
    IssueType,
    ItemCondition,
    OrderStatus,
    RequestStatus,
)
from app.errors import ConflictError, NotFoundError, ServiceError, ValidationError
from app.models import (
    ActionExecution,
    Approval,
    ConfirmationEvent,
    DecisionSnapshot,
    ExchangeRequest,
    Inventory,
    OrderItem,
    ReturnRequest,
)
from app.schemas import (
    ConfirmActionRequest,
    ConfirmationRecorded,
    EligibilityRequest,
    EligibilityResponse,
    ExecuteActionResponse,
    PrepareActionRequest,
    PrepareActionResponse,
    PresentApprovalRequest,
    PresentApprovalResponse,
)
from app.services.orders import OrderService
from app.utils import utcnow

_ACTIVE_REQUEST_STATUSES = {
    RequestStatus.REQUESTED.value,
    RequestStatus.APPROVED.value,
    RequestStatus.ITEM_RECEIVED.value,
}
_ACTIVE_APPROVAL_STATUSES = {
    ApprovalStatus.PREPARED.value,
    ApprovalStatus.PRESENTED.value,
    ApprovalStatus.CONFIRMED.value,
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_INDEX_PATH = PROJECT_ROOT / "policies" / "index.json"


def _policy_version_map() -> dict[str, str]:
    """Policy-id → version map from the versioned repo index at decision time."""

    payload = json.loads(POLICY_INDEX_PATH.read_text(encoding="utf-8"))
    return {
        str(entry["policy_id"]): str(entry["version"])
        for entry in payload
    }


def _approval_preview_hash(approval: Approval) -> str:
    confirmation_document = {
        "schema_version": "v2",
        "approval_id": approval.id,
        "customer_id": approval.customer_id,
        "conversation_id": approval.conversation_id,
        "origin_server_run_id": approval.origin_server_run_id,
        "origin_tool_call_id": approval.origin_tool_call_id,
        "action_type": approval.action_type,
        "order_id": approval.order_id,
        "order_item_id": approval.order_item_id,
        "payload": approval.payload,
        "preview": approval.preview,
        "order_version": approval.order_version,
        "expires_at": f"{approval.expires_at.isoformat(timespec='microseconds')}Z",
    }
    canonical = json.dumps(
        confirmation_document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ActionService:
    def __init__(self, order_service: OrderService, *, approval_ttl_minutes: int):
        self.order_service = order_service
        self.approval_ttl_minutes = approval_ttl_minutes

    @staticmethod
    def _find_item(order_items: list[OrderItem], item_id: str | None) -> OrderItem:
        if not item_id:
            raise ValidationError(
                "ORDER_ITEM_REQUIRED",
                "退货或换货必须指定订单商品。",
            )
        item = next((candidate for candidate in order_items if candidate.id == item_id), None)
        if item is None:
            raise NotFoundError(
                "ORDER_ITEM_NOT_FOUND",
                "未在该订单中找到指定商品。",
                status_code=404,
            )
        return item

    @staticmethod
    def _has_active_request(session: Session, order_item_id: str) -> bool:
        active_return = session.scalar(
            select(ReturnRequest.id).where(
                ReturnRequest.order_item_id == order_item_id,
                ReturnRequest.status.in_(_ACTIVE_REQUEST_STATUSES),
            )
        )
        if active_return is not None:
            return True
        active_exchange = session.scalar(
            select(ExchangeRequest.id).where(
                ExchangeRequest.order_item_id == order_item_id,
                ExchangeRequest.status.in_(_ACTIVE_REQUEST_STATUSES),
            )
        )
        return active_exchange is not None

    def check_eligibility(
        self,
        session: Session,
        *,
        customer_id: str,
        request: EligibilityRequest,
        now: datetime | None = None,
    ) -> EligibilityResponse:
        now = now or utcnow()
        action_type = ActionType(request.action_type)
        order = self.order_service.get_order_model(
            session,
            customer_id=customer_id,
            order_id=request.order_id,
        )
        order_status = OrderStatus(order.status)

        if action_type == ActionType.CANCEL_ORDER:
            decision = check_cancel_eligibility(order_status)
            return EligibilityResponse(**decision.__dict__)

        item = self._find_item(order.items, request.order_item_id)
        issue_type = IssueType(request.issue_type)
        declared_condition = ItemCondition(request.declared_condition)
        has_active_request = self._has_active_request(session, item.id)

        if action_type == ActionType.RETURN_ITEM:
            decision = check_return_eligibility(
                order_status=order_status,
                delivered_at=order.delivered_at,
                is_final_sale=item.is_final_sale,
                declared_condition=declared_condition,
                issue_type=issue_type,
                has_active_request=has_active_request,
                now=now,
            )
            return EligibilityResponse(**decision.__dict__)

        if action_type == ActionType.EXCHANGE_ITEM:
            target_inventory_qty = 0
            if request.target_size:
                inventory = session.scalar(
                    select(Inventory).where(
                        Inventory.sku == item.sku,
                        Inventory.size == request.target_size,
                    )
                )
                target_inventory_qty = inventory.available_qty if inventory else 0

            decision = check_exchange_eligibility(
                order_status=order_status,
                delivered_at=order.delivered_at,
                current_size=item.size,
                target_size=request.target_size,
                target_inventory_qty=target_inventory_qty,
                is_final_sale=item.is_final_sale,
                declared_condition=declared_condition,
                issue_type=issue_type,
                has_active_request=has_active_request,
                now=now,
            )
            return EligibilityResponse(**decision.__dict__)

        raise ValidationError("INVALID_ACTION", "不支持该操作类型。")

    @staticmethod
    def _prepare_response(approval: Approval) -> PrepareActionResponse:
        return PrepareActionResponse(
            approval_id=approval.id,
            action_type=ActionType(approval.action_type),
            status=ApprovalStatus(approval.status),
            preview=approval.preview,
            preview_hash=approval.preview_hash,
            expires_at=approval.expires_at,
        )

    @staticmethod
    def _assert_origin_pair(
        *,
        origin_server_run_id: str | None,
        origin_tool_call_id: str | None,
    ) -> None:
        if origin_server_run_id is None and origin_tool_call_id is None:
            return
        if origin_server_run_id is None or origin_tool_call_id is None:
            raise ValidationError(
                "PREPARATION_ORIGIN_REQUIRED",
                "Agent 操作准备需要可信运行标识和工具调用标识。",
            )
        if (
            not origin_server_run_id.strip()
            or not origin_tool_call_id.strip()
            or origin_server_run_id != origin_server_run_id.strip()
            or origin_tool_call_id != origin_tool_call_id.strip()
            or len(origin_server_run_id) > 80
            or len(origin_tool_call_id) > 200
        ):
            raise ValidationError(
                "PREPARATION_ORIGIN_INVALID",
                "Agent 操作准备的可信来源标识格式无效。",
            )

    @staticmethod
    def _find_origin_replay(
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        request_payload: dict[str, object],
        origin_server_run_id: str | None,
        origin_tool_call_id: str | None,
    ) -> Approval | None:
        if origin_server_run_id is None or origin_tool_call_id is None:
            return None
        approval = session.scalar(
            select(Approval)
            .where(
                Approval.origin_server_run_id == origin_server_run_id,
                Approval.origin_tool_call_id == origin_tool_call_id,
            )
            .with_for_update()
        )
        if approval is None:
            return None
        if (
            approval.customer_id != customer_id
            or approval.conversation_id != conversation_id
            or approval.payload != request_payload
        ):
            raise ConflictError(
                "PREPARATION_ORIGIN_CONFLICT",
                "该工具调用来源已绑定到另一份操作准备。",
                status_code=409,
            )
        if _approval_preview_hash(approval) != approval.preview_hash:
            raise ServiceError(
                "APPROVAL_INTEGRITY_ERROR",
                "审批快照完整性校验失败。",
                status_code=500,
            )
        return approval

    def prepare_action(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        request: PrepareActionRequest,
        origin_server_run_id: str | None = None,
        origin_tool_call_id: str | None = None,
        now: datetime | None = None,
    ) -> PrepareActionResponse:
        now = now or utcnow()
        self._assert_origin_pair(
            origin_server_run_id=origin_server_run_id,
            origin_tool_call_id=origin_tool_call_id,
        )
        request_payload = request.model_dump(mode="json")
        replay = self._find_origin_replay(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            request_payload=request_payload,
            origin_server_run_id=origin_server_run_id,
            origin_tool_call_id=origin_tool_call_id,
        )
        if replay is not None:
            return self._prepare_response(replay)

        eligibility = self.check_eligibility(
            session,
            customer_id=customer_id,
            request=EligibilityRequest.model_validate(
                request.model_dump(exclude={"user_note"})
            ),
            now=now,
        )
        if not eligibility.allowed:
            raise ValidationError(
                str(eligibility.reason_code),
                eligibility.user_message,
            )

        action_type = ActionType(request.action_type)
        order = self.order_service.get_order_model(
            session,
            customer_id=customer_id,
            order_id=request.order_id,
        )
        item = None
        if action_type != ActionType.CANCEL_ORDER:
            item = self._find_item(order.items, request.order_item_id)

        preview: dict[str, object] = {
            "requires_host_confirmation": True,
            "action_type": action_type.value,
            "order_id": order.id,
            "current_order_status": order.status,
            "policy_decision": eligibility.user_message,
        }
        if action_type == ActionType.CANCEL_ORDER:
            preview.update(
                {
                    "effect": "将订单状态变更为 CANCELLED",
                    "target_order_status": OrderStatus.CANCELLED.value,
                }
            )
        elif action_type == ActionType.RETURN_ITEM and item is not None:
            preview.update(
                {
                    "effect": "创建退货申请；不会在此步骤直接退款",
                    "order_item_id": item.id,
                    "product_name": item.product_name,
                    "size": item.size,
                    "declared_condition": ItemCondition(request.declared_condition).value,
                    "issue_type": IssueType(request.issue_type).value,
                }
            )
        elif action_type == ActionType.EXCHANGE_ITEM and item is not None:
            preview.update(
                {
                    "effect": "创建换货申请并预占一个目标尺码库存",
                    "order_item_id": item.id,
                    "product_name": item.product_name,
                    "current_size": item.size,
                    "target_size": request.target_size,
                    "declared_condition": ItemCondition(request.declared_condition).value,
                    "issue_type": IssueType(request.issue_type).value,
                }
            )

        approval_id = f"APR-{uuid.uuid4().hex[:12].upper()}"
        prior_approvals = session.scalars(
            select(Approval).where(
                Approval.customer_id == customer_id,
                Approval.conversation_id == conversation_id,
                Approval.status.in_(_ACTIVE_APPROVAL_STATUSES),
            ).with_for_update()
        ).all()

        approval = Approval(
            id=approval_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            origin_server_run_id=origin_server_run_id,
            origin_tool_call_id=origin_tool_call_id,
            action_type=action_type.value,
            order_id=order.id,
            order_item_id=item.id if item else None,
            payload=request_payload,
            preview=preview,
            preview_hash="",
            status=ApprovalStatus.PREPARED.value,
            order_version=order.version,
            created_at=now,
            expires_at=now + timedelta(minutes=self.approval_ttl_minutes),
        )
        preview_hash = _approval_preview_hash(approval)
        approval.preview_hash = preview_hash
        session.add(approval)
        session.flush()
        for prior in prior_approvals:
            prior.status = ApprovalStatus.SUPERSEDED.value
            prior.superseded_at = now
            prior.superseded_by_id = approval_id
            confirmation = session.scalar(
                select(ConfirmationEvent).where(
                    ConfirmationEvent.approval_id == prior.id
                )
            )
            if confirmation is not None and confirmation.consumed_at is None:
                confirmation.consumed_at = now
        session.flush()
        return self._prepare_response(approval)

    @staticmethod
    def _owned_approval(
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        approval_id: str,
    ) -> Approval:
        approval = session.scalar(
            select(Approval).where(
                Approval.id == approval_id,
                Approval.customer_id == customer_id,
                Approval.conversation_id == conversation_id,
            ).with_for_update()
        )
        if approval is None:
            raise NotFoundError(
                "APPROVAL_NOT_FOUND",
                "未找到该待确认操作。",
                status_code=404,
            )
        return approval

    @staticmethod
    def _assert_preview_matches(approval: Approval, preview_hash: str) -> None:
        canonical_hash = _approval_preview_hash(approval)
        if canonical_hash != approval.preview_hash:
            raise ServiceError(
                "APPROVAL_INTEGRITY_ERROR",
                "审批快照完整性校验失败。",
                status_code=500,
            )
        if preview_hash != approval.preview_hash:
            raise ConflictError(
                "PREVIEW_MISMATCH",
                "确认内容与当前操作预览不一致。",
                status_code=409,
            )

    @staticmethod
    def _assert_not_expired(approval: Approval, now: datetime) -> None:
        if approval.expires_at <= now:
            raise ConflictError(
                "APPROVAL_EXPIRED",
                "确认已过期，请重新生成操作预览。",
                status_code=409,
            )

    @staticmethod
    def _raise_for_terminal_status(approval: Approval) -> None:
        if approval.status == ApprovalStatus.SUPERSEDED.value:
            raise ConflictError(
                "APPROVAL_SUPERSEDED",
                "该操作预览已被同一会话中的新预览替代。",
                status_code=409,
            )
        if approval.status == ApprovalStatus.EXPIRED.value:
            raise ConflictError(
                "APPROVAL_EXPIRED",
                "确认已过期，请重新生成操作预览。",
                status_code=409,
            )
        if approval.status == ApprovalStatus.FAILED.value:
            raise ConflictError(
                "APPROVAL_FAILED",
                "该确认操作执行失败，请重新生成操作预览。",
                status_code=409,
            )
        if approval.status == ApprovalStatus.CANCELLED.value:
            raise ConflictError(
                "APPROVAL_CANCELLED",
                "该操作预览已取消。",
                status_code=409,
            )

    def present_action(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        approval_id: str,
        request: PresentApprovalRequest,
        now: datetime | None = None,
    ) -> PresentApprovalResponse:
        now = now or utcnow()
        approval = self._owned_approval(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
        )
        self._raise_for_terminal_status(approval)
        self._assert_not_expired(approval, now)
        self._assert_preview_matches(approval, request.preview_hash)

        if approval.status == ApprovalStatus.PRESENTED.value:
            assert approval.presented_at is not None
            return PresentApprovalResponse(
                approval_id=approval.id,
                status=ApprovalStatus.PRESENTED,
                preview_hash=approval.preview_hash,
                presented_at=approval.presented_at,
            )
        if approval.status != ApprovalStatus.PREPARED.value:
            raise ConflictError(
                "APPROVAL_NOT_PREPARED",
                "当前操作预览不能进入展示状态。",
                status_code=409,
            )

        approval.status = ApprovalStatus.PRESENTED.value
        approval.presented_at = now
        session.flush()
        return PresentApprovalResponse(
            approval_id=approval.id,
            status=ApprovalStatus.PRESENTED,
            preview_hash=approval.preview_hash,
            presented_at=now,
        )

    def record_confirmation(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        approval_id: str,
        request: ConfirmActionRequest,
        now: datetime | None = None,
    ) -> ConfirmationRecorded:
        now = now or utcnow()
        approval = self._owned_approval(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
        )
        self._assert_preview_matches(approval, request.preview_hash)

        existing_confirmation = session.scalar(
            select(ConfirmationEvent).where(
                ConfirmationEvent.approval_id == approval.id
            )
        )
        prior_execution = session.scalar(
            select(ActionExecution).where(
                ActionExecution.approval_id == approval.id
            )
        )
        if prior_execution is not None:
            if (
                existing_confirmation is None
                or existing_confirmation.customer_id != customer_id
                or existing_confirmation.conversation_id != conversation_id
                or existing_confirmation.ui_event_id != request.ui_event_id
                or existing_confirmation.preview_hash != request.preview_hash
                or existing_confirmation.confirmation_source
                != ConfirmationSource(request.confirmation_source).value
            ):
                raise ConflictError(
                    "APPROVAL_ALREADY_CONFIRMED",
                    "该操作已经由另一确认事件确认。",
                    status_code=409,
                )
            return ConfirmationRecorded(
                confirmation_event_id=existing_confirmation.id,
                approval_id=approval.id,
                status=ApprovalStatus.EXECUTED,
            )

        self._raise_for_terminal_status(approval)
        self._assert_not_expired(approval, now)

        if approval.status == ApprovalStatus.PREPARED.value:
            raise ConflictError(
                "APPROVAL_NOT_PRESENTED",
                "必须先由宿主应用展示完整操作预览。",
                status_code=409,
            )

        if approval.status == ApprovalStatus.CONFIRMED.value:
            if (
                existing_confirmation is not None
                and existing_confirmation.customer_id == customer_id
                and existing_confirmation.conversation_id == conversation_id
                and existing_confirmation.ui_event_id == request.ui_event_id
                and existing_confirmation.preview_hash == request.preview_hash
                and existing_confirmation.confirmation_source
                == ConfirmationSource(request.confirmation_source).value
            ):
                return ConfirmationRecorded(
                    confirmation_event_id=existing_confirmation.id,
                    approval_id=approval.id,
                    status=ApprovalStatus.CONFIRMED,
                )
            raise ConflictError(
                "APPROVAL_ALREADY_CONFIRMED",
                "该操作已经由另一确认事件确认。",
                status_code=409,
            )
        if approval.status != ApprovalStatus.PRESENTED.value:
            raise ConflictError(
                "APPROVAL_NOT_PRESENTED",
                "当前操作尚未处于可确认状态。",
                status_code=409,
            )

        reused_ui_event = session.scalar(
            select(ConfirmationEvent).where(
                ConfirmationEvent.ui_event_id == request.ui_event_id
            )
        )
        if reused_ui_event is not None:
            raise ConflictError(
                "CONFIRMATION_EVENT_REUSED",
                "该确认事件已用于其他操作。",
                status_code=409,
            )

        confirmation = ConfirmationEvent(
            id=f"CNF-{uuid.uuid4().hex[:12].upper()}",
            approval_id=approval.id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            ui_event_id=request.ui_event_id,
            preview_hash=request.preview_hash,
            confirmation_source=ConfirmationSource(request.confirmation_source).value,
            confirmed_at=now,
        )
        approval.status = ApprovalStatus.CONFIRMED.value
        approval.confirmed_at = now
        session.add(confirmation)
        try:
            session.flush()
        except IntegrityError as exc:
            # SQLite ignores FOR UPDATE; unique(approval_id/ui_event_id) is the
            # last-resort guard when two writers race past the PRESENTED check.
            raise ConflictError(
                "APPROVAL_ALREADY_CONFIRMED",
                "该操作已经由另一确认事件确认。",
                status_code=409,
            ) from exc
        return ConfirmationRecorded(
            confirmation_event_id=confirmation.id,
            approval_id=approval.id,
            status=ApprovalStatus.CONFIRMED,
        )

    def execute_confirmed_action(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        approval_id: str,
        confirmation_event_id: str,
        now: datetime | None = None,
    ) -> ExecuteActionResponse:
        now = now or utcnow()
        approval = self._owned_approval(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
        )
        self._assert_preview_matches(approval, approval.preview_hash)

        confirmation = session.scalar(
            select(ConfirmationEvent).where(
                ConfirmationEvent.id == confirmation_event_id,
                ConfirmationEvent.approval_id == approval.id,
                ConfirmationEvent.customer_id == customer_id,
                ConfirmationEvent.conversation_id == conversation_id,
                ConfirmationEvent.preview_hash == approval.preview_hash,
            )
        )
        if confirmation is None:
            raise NotFoundError(
                "CONFIRMATION_NOT_FOUND",
                "未找到该可信确认事件。",
                status_code=404,
            )

        prior_execution = session.scalar(
            select(ActionExecution).where(
                ActionExecution.approval_id == approval.id
            )
        )
        if prior_execution is not None:
            return ExecuteActionResponse(
                execution_id=prior_execution.id,
                approval_id=prior_execution.approval_id,
                action_type=ActionType(prior_execution.action_type),
                idempotent_replay=True,
                result=prior_execution.result,
            )

        self._raise_for_terminal_status(approval)
        self._assert_not_expired(approval, now)
        if approval.status != ApprovalStatus.CONFIRMED.value:
            raise ConflictError(
                "APPROVAL_NOT_CONFIRMED",
                "该操作没有可信确认事件。",
                status_code=409,
            )

        if confirmation.consumed_at is not None:
            raise ConflictError(
                "CONFIRMATION_ALREADY_CONSUMED",
                "该确认事件已经被消费。",
                status_code=409,
            )
        self._assert_preview_matches(approval, confirmation.preview_hash)

        order = self.order_service.get_order_model(
            session,
            customer_id=customer_id,
            order_id=approval.order_id,
        )
        if order.version != approval.order_version:
            raise ConflictError(
                "STALE_APPROVAL",
                "订单状态已发生变化，请重新检查并确认。",
                status_code=409,
            )

        prepared_request = PrepareActionRequest.model_validate(approval.payload)
        eligibility = self.check_eligibility(
            session,
            customer_id=customer_id,
            request=EligibilityRequest.model_validate(
                prepared_request.model_dump(exclude={"user_note"})
            ),
            now=now,
        )
        if not eligibility.allowed:
            raise ConflictError(
                str(eligibility.reason_code),
                f"执行前复核失败：{eligibility.user_message}",
                status_code=409,
            )

        approval.status = ApprovalStatus.EXECUTING.value
        session.flush()
        action_type = ActionType(approval.action_type)
        result: dict[str, object]

        if action_type == ActionType.CANCEL_ORDER:
            assert_order_transition(OrderStatus(order.status), OrderStatus.CANCELLED)
            order.status = OrderStatus.CANCELLED.value
            order.version += 1
            session.flush()
            result = {
                "outcome": "ORDER_CANCELLED",
                "order_id": order.id,
                "final_order_status": order.status,
                "final_order_version": order.version,
            }

        elif action_type == ActionType.RETURN_ITEM:
            return_request = ReturnRequest(
                id=f"RET-{uuid.uuid4().hex[:12].upper()}",
                customer_id=customer_id,
                order_id=order.id,
                order_item_id=approval.order_item_id or "",
                reason=IssueType(prepared_request.issue_type).value,
                declared_condition=ItemCondition(prepared_request.declared_condition).value,
                status=RequestStatus.REQUESTED.value,
                created_at=now,
            )
            session.add(return_request)
            session.flush()
            verified_return = session.get(ReturnRequest, return_request.id)
            if verified_return is None:
                raise ConflictError(
                    "WRITE_VERIFICATION_FAILED",
                    "退货申请写入后未能验证最终状态。",
                    status_code=500,
                )
            result = {
                "outcome": "RETURN_REQUEST_CREATED",
                "return_request_id": verified_return.id,
                "request_status": verified_return.status,
                "order_id": verified_return.order_id,
                "order_item_id": verified_return.order_item_id,
                "final_order_status": order.status,
            }

        elif action_type == ActionType.EXCHANGE_ITEM:
            item = self._find_item(order.items, approval.order_item_id)
            target_size = prepared_request.target_size
            inventory = session.scalar(
                select(Inventory).where(
                    Inventory.sku == item.sku,
                    Inventory.size == target_size,
                )
            )
            if inventory is None or inventory.available_qty <= 0:
                raise ConflictError(
                    "OUT_OF_STOCK",
                    "执行时目标尺码已无库存，请重新选择。",
                    status_code=409,
                )
            inventory.available_qty -= 1
            inventory.updated_at = now
            exchange_request = ExchangeRequest(
                id=f"EXC-{uuid.uuid4().hex[:12].upper()}",
                customer_id=customer_id,
                order_id=order.id,
                order_item_id=item.id,
                target_size=target_size or "",
                reason=IssueType(prepared_request.issue_type).value,
                declared_condition=ItemCondition(prepared_request.declared_condition).value,
                status=RequestStatus.REQUESTED.value,
                created_at=now,
            )
            session.add(exchange_request)
            session.flush()
            verified_exchange = session.get(ExchangeRequest, exchange_request.id)
            if verified_exchange is None:
                raise ConflictError(
                    "WRITE_VERIFICATION_FAILED",
                    "换货申请写入后未能验证最终状态。",
                    status_code=500,
                )
            result = {
                "outcome": "EXCHANGE_REQUEST_CREATED",
                "exchange_request_id": verified_exchange.id,
                "request_status": verified_exchange.status,
                "order_id": verified_exchange.order_id,
                "order_item_id": verified_exchange.order_item_id,
                "target_size": verified_exchange.target_size,
                "remaining_target_inventory": inventory.available_qty,
                "final_order_status": order.status,
            }

        else:
            raise ValidationError("INVALID_ACTION", "不支持该操作类型。")

        execution = ActionExecution(
            id=f"EXE-{uuid.uuid4().hex[:12].upper()}",
            approval_id=approval.id,
            action_type=action_type.value,
            result=result,
            created_at=now,
        )
        confirmation.consumed_at = now
        approval.status = ApprovalStatus.EXECUTED.value
        approval.executed_at = now
        session.add(execution)
        session.flush()

        snapshot = DecisionSnapshot(
            id=f"DEC-{uuid.uuid4().hex[:12].upper()}",
            customer_id=customer_id,
            approval_id=approval.id,
            execution_id=execution.id,
            confirmation_event_id=confirmation.id,
            order_id=order.id,
            action_type=action_type.value,
            confirmation_source=confirmation.confirmation_source,
            rule_version=RULES_VERSION,
            policy_versions=_policy_version_map(),
            eligibility_inputs=prepared_request.model_dump(
                mode="json",
                exclude={"user_note"},
            ),
            eligibility_decision={
                "allowed": eligibility.allowed,
                "reason_code": str(eligibility.reason_code),
                "user_message": eligibility.user_message,
            },
            model_cost_cny=None,
            created_at=now,
        )
        session.add(snapshot)
        session.flush()
        return ExecuteActionResponse(
            execution_id=execution.id,
            approval_id=approval.id,
            action_type=action_type,
            idempotent_replay=False,
            result=result,
        )

    def mark_failed(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        approval_id: str,
        failure_code: str,
        now: datetime | None = None,
    ) -> None:
        approval = self._owned_approval(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
        )
        if approval.status == ApprovalStatus.CONFIRMED.value:
            approval.status = ApprovalStatus.FAILED.value
            approval.failed_at = now or utcnow()
            approval.failure_code = failure_code
            confirmation = session.scalar(
                select(ConfirmationEvent).where(
                    ConfirmationEvent.approval_id == approval.id
                )
            )
            if confirmation is not None and confirmation.consumed_at is None:
                confirmation.consumed_at = now or utcnow()
            session.flush()

    def mark_expired(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        approval_id: str,
        now: datetime | None = None,
    ) -> None:
        approval = self._owned_approval(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
        )
        if approval.status in _ACTIVE_APPROVAL_STATUSES:
            approval.status = ApprovalStatus.EXPIRED.value
            approval.failed_at = now or utcnow()
            approval.failure_code = "APPROVAL_EXPIRED"
            confirmation = session.scalar(
                select(ConfirmationEvent).where(
                    ConfirmationEvent.approval_id == approval.id
                )
            )
            if confirmation is not None and confirmation.consumed_at is None:
                confirmation.consumed_at = now or utcnow()
            session.flush()
