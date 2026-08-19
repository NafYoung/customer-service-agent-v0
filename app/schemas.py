from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.enums import (
    ActionType,
    ApprovalStatus,
    ConfirmationSource,
    EligibilityReason,
    EvidenceKind,
    IssueType,
    ItemCondition,
    OrderStatus,
    TicketPriority,
)


class APIModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        use_enum_values=True,
    )


class AuthRequest(APIModel):
    email: EmailStr
    verification_code: str = Field(min_length=4, max_length=12)


class AuthResponse(APIModel):
    access_token: str
    token_type: str = "bearer"
    customer_id: str
    expires_at: datetime


class OrderItemRead(APIModel):
    id: str
    sku: str
    product_name: str
    size: str | None
    quantity: int
    unit_price_cents: int
    is_final_sale: bool


class ShipmentRead(APIModel):
    carrier: str
    tracking_number: str
    status: str
    estimated_delivery_at: datetime | None
    updated_at: datetime


class OrderRead(APIModel):
    id: str
    customer_id: str
    status: OrderStatus
    region: str
    channel: str
    version: int
    created_at: datetime
    shipped_at: datetime | None
    delivered_at: datetime | None
    items: list[OrderItemRead]
    shipment: ShipmentRead | None = None


class InventoryRead(APIModel):
    sku: str
    size: str
    available_qty: int
    updated_at: datetime


class EligibilityRequest(APIModel):
    action_type: ActionType
    order_id: str
    order_item_id: str | None = None
    target_size: str | None = None
    declared_condition: ItemCondition = ItemCondition.NEW_UNWORN
    issue_type: IssueType = IssueType.CHANGED_MIND


class EligibilityResponse(APIModel):
    allowed: bool
    reason_code: EligibilityReason
    user_message: str
    available_alternative: str | None = None


class PrepareActionRequest(EligibilityRequest):
    user_note: str | None = Field(default=None, max_length=500)


class PrepareActionResponse(APIModel):
    approval_id: str
    action_type: ActionType
    status: ApprovalStatus
    preview: dict[str, Any]
    preview_hash: str
    expires_at: datetime


class PresentApprovalRequest(APIModel):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PresentApprovalResponse(APIModel):
    approval_id: str
    status: ApprovalStatus
    preview_hash: str
    presented_at: datetime


class ConfirmActionRequest(APIModel):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ui_event_id: str = Field(min_length=8, max_length=120)
    confirmation_source: ConfirmationSource


class ConfirmationRecorded(APIModel):
    confirmation_event_id: str
    approval_id: str
    status: ApprovalStatus


class ExecuteActionResponse(APIModel):
    execution_id: str
    approval_id: str
    action_type: ActionType
    idempotent_replay: bool = False
    result: dict[str, Any]


class TicketCreateRequest(APIModel):
    order_id: str | None = None
    category: str = Field(min_length=2, max_length=60)
    summary: str = Field(min_length=5, max_length=1000)
    priority: TicketPriority = TicketPriority.NORMAL


class VerifyEvidenceRequest(APIModel):
    """宿主侧凭证校验占位请求：不进任何 Agent allowlist。"""

    order_id: str = Field(min_length=3, max_length=40)
    evidence_kind: EvidenceKind
    evidence_ref: str = Field(min_length=8, max_length=200)
    declared_notes: str | None = Field(default=None, max_length=500)


class EvidenceVerificationRead(APIModel):
    order_id: str
    evidence_kind: str
    evidence_ref: str
    verdict: Literal["MOCK_ACCEPTED", "MOCK_FORGED"]
    note: str


class TicketRead(APIModel):
    id: str
    order_id: str | None
    category: str
    priority: str
    summary: str
    status: str
    created_at: datetime


class PolicySearchRequest(APIModel):
    query: str = Field(min_length=2, max_length=300)
    region: str = "CN"
    channel: str = "ONLINE"
    top_k: int = Field(default=3, ge=1, le=10)


class PolicySearchHit(APIModel):
    policy_id: str
    title: str
    version: str
    effective_date: str
    score: int
    excerpt: str


class PolicySearchResponse(APIModel):
    hits: list[PolicySearchHit]


class ToolEventRead(APIModel):
    id: str
    run_id: str | None
    customer_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    result: Any | None
    success: bool
    error_code: str | None
    latency_ms: int
    created_at: datetime


class DebugToolEventRead(APIModel):
    id: str
    run_id: str | None
    tool_name: str
    success: bool
    error_code: str | None
    latency_ms: int
    created_at: datetime
