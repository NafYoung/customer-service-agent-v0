from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.enums import TicketStatus
from app.models import SupportTicket
from app.schemas import TicketCreateRequest, TicketRead
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
