from __future__ import annotations

import uuid

from sqlalchemy import Select, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import ApprovalStatus, HandoffReason, TicketStatus
from app.errors import ConflictError, ServiceError, ValidationError
from app.models import Approval, SupportTicket
from app.schemas import (
    ManualHandoffRead,
    ManualHandoffRequest,
    TicketCreateRequest,
    TicketRead,
)
from app.services.orders import OrderService


class TicketService:
    def __init__(self, order_service: OrderService):
        self.order_service = order_service

    def create_ticket(
        self,
        session: Session,
        *,
        customer_id: str,
        request: TicketCreateRequest,
    ) -> TicketRead:
        if request.order_id:
            self.order_service.get_order_model(
                session,
                customer_id=customer_id,
                order_id=request.order_id,
            )

        ticket = SupportTicket(
            id=f"TKT-{uuid.uuid4().hex[:10].upper()}",
            customer_id=customer_id,
            order_id=request.order_id,
            category=request.category,
            priority=str(request.priority),
            summary=request.summary,
            status=TicketStatus.OPEN.value,
        )
        session.add(ticket)
        session.flush()
        return TicketRead.model_validate(ticket)

    @staticmethod
    def _required_trusted_identifier(
        value: str | None,
        *,
        field_name: str,
        max_length: int,
    ) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                "HANDOFF_TRUSTED_CONTEXT_REQUIRED",
                f"人工接管需要可信的{field_name}。",
            )
        if value != value.strip() or len(value) > max_length:
            raise ValidationError(
                "HANDOFF_TRUSTED_CONTEXT_INVALID",
                f"人工接管的{field_name}格式无效。",
            )
        return value

    @staticmethod
    def _manual_handoff_query(
        *,
        customer_id: str,
        conversation_id: str,
    ) -> Select[tuple[SupportTicket]]:
        return (
            select(SupportTicket)
            .where(
                SupportTicket.customer_id == customer_id,
                SupportTicket.conversation_id == conversation_id,
            )
            .with_for_update()
        )

    @staticmethod
    def _manual_handoff_read(
        ticket: SupportTicket,
        *,
        idempotent_replay: bool,
    ) -> ManualHandoffRead:
        if (
            ticket.conversation_id is None
            or ticket.origin_server_run_id is None
            or ticket.transfer_reason is None
        ):
            raise ServiceError(
                "HANDOFF_INTEGRITY_ERROR",
                "人工接管工单缺少可信来源字段。",
                status_code=500,
            )
        try:
            transfer_reason = HandoffReason(ticket.transfer_reason)
        except ValueError as exc:
            raise ServiceError(
                "HANDOFF_INTEGRITY_ERROR",
                "人工接管工单包含无效的转交原因。",
                status_code=500,
            ) from exc
        return ManualHandoffRead(
            id=ticket.id,
            order_id=ticket.order_id,
            category=ticket.category,
            priority=ticket.priority,
            summary=ticket.summary,
            status=ticket.status,
            created_at=ticket.created_at,
            conversation_id=ticket.conversation_id,
            origin_server_run_id=ticket.origin_server_run_id,
            transfer_reason=transfer_reason,
            idempotent_replay=idempotent_replay,
        )

    @staticmethod
    def _same_manual_payload(
        ticket: SupportTicket,
        request: ManualHandoffRequest,
    ) -> bool:
        return (
            ticket.order_id == request.order_id
            and ticket.transfer_reason == HandoffReason(request.transfer_reason).value
            and ticket.summary == request.summary
            and ticket.priority == str(request.priority)
        )

    def get_manual_handoff(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
    ) -> ManualHandoffRead | None:
        customer_id = self._required_trusted_identifier(
            customer_id,
            field_name="客户标识",
            max_length=40,
        )
        conversation_id = self._required_trusted_identifier(
            conversation_id,
            field_name="会话标识",
            max_length=120,
        )
        ticket = session.scalar(
            self._manual_handoff_query(
                customer_id=customer_id,
                conversation_id=conversation_id,
            )
        )
        if ticket is None:
            return None
        return self._manual_handoff_read(ticket, idempotent_replay=False)

    def assert_agent_owned(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
    ) -> None:
        handoff = self.get_manual_handoff(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
        )
        if handoff is not None:
            raise ConflictError(
                "CONVERSATION_MANUAL",
                "该会话已转人工，不能继续调用 Agent。",
                status_code=409,
            )

    def _existing_manual_handoff_or_conflict(
        self,
        ticket: SupportTicket,
        *,
        request: ManualHandoffRequest,
    ) -> ManualHandoffRead:
        if self._same_manual_payload(ticket, request):
            return self._manual_handoff_read(ticket, idempotent_replay=True)
        raise ConflictError(
            "HANDOFF_ALREADY_MANUAL",
            "该会话已转人工，不能覆盖原工单。",
            status_code=409,
        )

    def create_manual_handoff(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
        origin_server_run_id: str,
        request: ManualHandoffRequest,
    ) -> ManualHandoffRead:
        customer_id = self._required_trusted_identifier(
            customer_id,
            field_name="客户标识",
            max_length=40,
        )
        conversation_id = self._required_trusted_identifier(
            conversation_id,
            field_name="会话标识",
            max_length=120,
        )
        origin_server_run_id = self._required_trusted_identifier(
            origin_server_run_id,
            field_name="运行标识",
            max_length=80,
        )

        session.execute(
            update(Approval)
            .where(
                Approval.customer_id == customer_id,
                Approval.conversation_id == conversation_id,
                Approval.status.in_(
                    {
                        ApprovalStatus.PREPARED.value,
                        ApprovalStatus.PRESENTED.value,
                        ApprovalStatus.CONFIRMED.value,
                    }
                ),
            )
            .values(status=ApprovalStatus.CANCELLED.value)
        )

        existing = session.scalar(
            self._manual_handoff_query(
                customer_id=customer_id,
                conversation_id=conversation_id,
            )
        )
        if existing is not None:
            return self._existing_manual_handoff_or_conflict(existing, request=request)

        if request.order_id:
            self.order_service.get_order_model(
                session,
                customer_id=customer_id,
                order_id=request.order_id,
            )

        ticket = SupportTicket(
            id=f"TKT-{uuid.uuid4().hex[:10].upper()}",
            customer_id=customer_id,
            conversation_id=conversation_id,
            origin_server_run_id=origin_server_run_id,
            order_id=request.order_id,
            category=HandoffReason(request.transfer_reason).value,
            transfer_reason=HandoffReason(request.transfer_reason).value,
            priority=str(request.priority),
            summary=request.summary,
            status=TicketStatus.OPEN.value,
        )
        try:
            with session.begin_nested():
                session.add(ticket)
                session.flush()
        except IntegrityError as exc:
            existing = session.scalar(
                self._manual_handoff_query(
                    customer_id=customer_id,
                    conversation_id=conversation_id,
                )
            )
            if existing is not None:
                return self._existing_manual_handoff_or_conflict(existing, request=request)
            raise ConflictError(
                "HANDOFF_CONCURRENT_CONFLICT",
                "人工接管写入发生并发冲突，请重试。",
                status_code=409,
            ) from exc
        return self._manual_handoff_read(ticket, idempotent_replay=False)
