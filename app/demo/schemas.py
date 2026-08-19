from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas import APIModel


class EmptyDemoBody(APIModel):
    """Browser must not submit internal host or approval fields."""


class DemoSessionResponse(APIModel):
    csrf_token: str
    customer_display_name: str
    supported_scenarios: list[str]
    expires_at: datetime
    demo_agent_mode: str
    mode_label: str


class DemoMessageRequest(APIModel):
    message: str = Field(min_length=1, max_length=500)


class DemoToolTraceItem(APIModel):
    tool_name: str
    success: bool
    summary: str


class DemoMessageResponse(APIModel):
    reply: str
    has_pending_action: bool
    provider_http_calls: int = 0
    tool_trace: list[DemoToolTraceItem] = Field(default_factory=list)


class DemoConfirmationCard(APIModel):
    """Browser-safe projection of the canonical DB preview only."""

    action_type: str
    order_id: str
    order_item_id: str | None = None
    product_name: str | None = None
    size: str | None = None
    current_size: str | None = None
    target_size: str | None = None
    quantity: int | None = None
    effect: str
    policy_decision: str
    current_order_status: str | None = None
    declared_condition: str | None = None
    issue_type: str | None = None
    expires_at: datetime
    status: str
    executed: bool = False
    note: str = "尚未执行；仅在你点击确认后才会写入业务状态。"


class DemoConfirmResponse(APIModel):
    status: str
    action_type: str
    order_id: str
    result_summary: str
    idempotent_replay: bool = False
    provider_http_calls: int = 0


class DemoRejectResponse(APIModel):
    status: str
    message: str
    handoff_ticket_id: str | None = None


class DemoResetResponse(APIModel):
    csrf_token: str
    customer_display_name: str
    message: str
    expires_at: datetime
    demo_agent_mode: str
    mode_label: str
