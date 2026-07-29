from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.services.actions import ActionService
from app.services.auth import AuthService
from app.services.orders import OrderService
from app.services.policies import PolicyService
from app.services.tickets import TicketService
from app.tools.facade import CustomerServiceTools


def build_tools(settings: Settings, *, policy_dir: Path) -> CustomerServiceTools:
    order_service = OrderService()
    return CustomerServiceTools(
        auth_service=AuthService(session_minutes=settings.auth_session_minutes),
        order_service=order_service,
        action_service=ActionService(
            order_service,
            approval_ttl_minutes=settings.approval_ttl_minutes,
        ),
        policy_service=PolicyService(policy_dir),
        ticket_service=TicketService(order_service),
    )
