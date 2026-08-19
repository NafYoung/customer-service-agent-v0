from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.enums import ActionType, IssueType, ItemCondition
from app.errors import ServiceError, ValidationError
from app.models import ToolEvent
from app.schemas import (
    EligibilityRequest,
    EligibilityResponse,
    EvidenceVerificationRead,
    InventoryRead,
    OrderRead,
    PolicySearchRequest,
    PolicySearchResponse,
    PrepareActionRequest,
    PrepareActionResponse,
    ShipmentRead,
    TicketCreateRequest,
    TicketRead,
    VerifyEvidenceRequest,
)
from app.services.actions import ActionService
from app.services.auth import AuthService
from app.services.orders import OrderService
from app.services.policies import PolicyService
from app.services.tickets import TicketService

T = TypeVar("T")
_SENSITIVE_KEYS = {
    "verification_code",
    "access_token",
    "auth_token",
    "authorization",
    "token",
}


@dataclass(frozen=True)
class ToolCallContext:
    run_id: str | None = None
    server_run_id: str | None = None
    origin_tool_call_id: str | None = None
    atomic_run: bool = False
    auth_token: str | None = None
    conversation_id: str | None = None


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key and key.casefold() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {item_key: _redact(item, key=item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class CustomerServiceTools:
    """Stable tool boundary for HTTP clients and a future LLM agent.

    Customer identity is derived from the server-side auth token. The model is
    never allowed to choose an arbitrary customer_id for an authenticated tool.
    """

    def __init__(
        self,
        *,
        auth_service: AuthService,
        order_service: OrderService,
        action_service: ActionService,
        policy_service: PolicyService,
        ticket_service: TicketService,
    ):
        self.auth_service = auth_service
        self.order_service = order_service
        self.action_service = action_service
        self.policy_service = policy_service
        self.ticket_service = ticket_service

    def _call(
        self,
        session: Session,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolCallContext,
        customer_id: str | None,
        operation: Callable[[], T],
    ) -> T:
        started = time.perf_counter()

        def invoke_and_record() -> T:
            result = operation()
            event = ToolEvent(
                id=f"TEV-{uuid.uuid4().hex[:12].upper()}",
                run_id=context.server_run_id or context.run_id,
                customer_id=customer_id,
                tool_name=tool_name,
                arguments=_redact(_jsonable(arguments)),
                result=_redact(_jsonable(result)),
                success=True,
                error_code=None,
                latency_ms=max(
                    0,
                    int((time.perf_counter() - started) * 1000),
                ),
            )
            session.add(event)
            session.flush()
            return result

        def record_failure(exc: ServiceError, *, commit: bool) -> None:
            event = ToolEvent(
                id=f"TEV-{uuid.uuid4().hex[:12].upper()}",
                run_id=context.server_run_id or context.run_id,
                customer_id=customer_id,
                tool_name=tool_name,
                arguments=_redact(_jsonable(arguments)),
                result={"message": exc.message},
                success=False,
                error_code=exc.code,
                latency_ms=max(
                    0,
                    int((time.perf_counter() - started) * 1000),
                ),
            )
            session.add(event)
            if commit:
                session.commit()
            else:
                session.flush()

        if context.atomic_run:
            try:
                with session.begin_nested():
                    return invoke_and_record()
            except ServiceError as exc:
                record_failure(exc, commit=False)
                raise

        try:
            return invoke_and_record()
        except ServiceError as exc:
            # Roll back any partial business mutation first, then persist the
            # failed tool event in a clean transaction. Each HTTP endpoint in
            # v0 performs exactly one tool call, so this boundary is explicit.
            session.rollback()
            record_failure(exc, commit=True)
            raise

    def _customer_id(self, session: Session, context: ToolCallContext) -> str:
        if not context.auth_token:
            from app.errors import AuthenticationError

            raise AuthenticationError(
                "AUTHENTICATION_REQUIRED",
                "该工具需要先验证客户身份。",
                status_code=401,
            )
        return self.auth_service.resolve_customer_id(session, context.auth_token)

    @staticmethod
    def _conversation_id(context: ToolCallContext) -> str:
        if not context.conversation_id:
            raise ValidationError(
                "CONVERSATION_REQUIRED",
                "该操作需要可信宿主提供会话标识。",
            )
        return context.conversation_id

    def get_customer_orders(
        self,
        session: Session,
        *,
        context: ToolCallContext,
    ) -> list[OrderRead]:
        customer_id = self._customer_id(session, context)
        return self._call(
            session,
            tool_name="get_customer_orders",
            arguments={},
            context=context,
            customer_id=customer_id,
            operation=lambda: self.order_service.list_orders(session, customer_id=customer_id),
        )

    def get_order(
        self,
        session: Session,
        *,
        order_id: str,
        context: ToolCallContext,
    ) -> OrderRead:
        customer_id = self._customer_id(session, context)
        return self._call(
            session,
            tool_name="get_order",
            arguments={"order_id": order_id},
            context=context,
            customer_id=customer_id,
            operation=lambda: self.order_service.get_order(
                session,
                customer_id=customer_id,
                order_id=order_id,
            ),
        )

    def get_shipment(
        self,
        session: Session,
        *,
        order_id: str,
        context: ToolCallContext,
    ) -> ShipmentRead:
        customer_id = self._customer_id(session, context)
        return self._call(
            session,
            tool_name="get_shipment",
            arguments={"order_id": order_id},
            context=context,
            customer_id=customer_id,
            operation=lambda: self.order_service.get_shipment(
                session,
                customer_id=customer_id,
                order_id=order_id,
            ),
        )

    def get_inventory(
        self,
        session: Session,
        *,
        sku: str,
        size: str,
        context: ToolCallContext,
    ) -> InventoryRead:
        customer_id = self._customer_id(session, context)
        return self._call(
            session,
            tool_name="get_inventory",
            arguments={"sku": sku, "size": size},
            context=context,
            customer_id=customer_id,
            operation=lambda: self.order_service.get_inventory(session, sku=sku, size=size),
        )

    def search_policy(
        self,
        session: Session,
        *,
        request: PolicySearchRequest,
        context: ToolCallContext,
    ) -> PolicySearchResponse:
        customer_id = None
        if context.auth_token:
            customer_id = self.auth_service.resolve_customer_id(session, context.auth_token)
        return self._call(
            session,
            tool_name="search_policy",
            arguments=request.model_dump(mode="json"),
            context=context,
            customer_id=customer_id,
            operation=lambda: self.policy_service.search(**request.model_dump()),
        )

    def check_action_eligibility(
        self,
        session: Session,
        *,
        request: EligibilityRequest,
        context: ToolCallContext,
    ) -> EligibilityResponse:
        customer_id = self._customer_id(session, context)
        return self._call(
            session,
            tool_name="check_action_eligibility",
            arguments=request.model_dump(mode="json"),
            context=context,
            customer_id=customer_id,
            operation=lambda: self.action_service.check_eligibility(
                session,
                customer_id=customer_id,
                request=request,
            ),
        )

    def prepare_action(
        self,
        session: Session,
        *,
        request: PrepareActionRequest,
        context: ToolCallContext,
    ) -> PrepareActionResponse:
        customer_id = self._customer_id(session, context)
        conversation_id = self._conversation_id(context)
        return self._call(
            session,
            tool_name="prepare_action",
            arguments=request.model_dump(mode="json"),
            context=context,
            customer_id=customer_id,
            operation=lambda: self.action_service.prepare_action(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                request=request,
                origin_server_run_id=context.server_run_id,
                origin_tool_call_id=context.origin_tool_call_id,
            ),
        )

    def prepare_cancel_order(
        self,
        session: Session,
        *,
        order_id: str,
        user_note: str | None,
        context: ToolCallContext,
    ) -> PrepareActionResponse:
        customer_id = self._customer_id(session, context)
        conversation_id = self._conversation_id(context)
        request = PrepareActionRequest(
            action_type=ActionType.CANCEL_ORDER,
            order_id=order_id,
            user_note=user_note,
        )
        return self._call(
            session,
            tool_name="prepare_cancel_order",
            arguments=request.model_dump(mode="json"),
            context=context,
            customer_id=customer_id,
            operation=lambda: self.action_service.prepare_action(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                request=request,
                origin_server_run_id=context.server_run_id,
                origin_tool_call_id=context.origin_tool_call_id,
            ),
        )

    def prepare_return(
        self,
        session: Session,
        *,
        order_id: str,
        order_item_id: str,
        declared_condition: ItemCondition,
        issue_type: IssueType,
        user_note: str | None,
        context: ToolCallContext,
    ) -> PrepareActionResponse:
        customer_id = self._customer_id(session, context)
        conversation_id = self._conversation_id(context)
        request = PrepareActionRequest(
            action_type=ActionType.RETURN_ITEM,
            order_id=order_id,
            order_item_id=order_item_id,
            declared_condition=declared_condition,
            issue_type=issue_type,
            user_note=user_note,
        )
        return self._call(
            session,
            tool_name="prepare_return",
            arguments=request.model_dump(mode="json"),
            context=context,
            customer_id=customer_id,
            operation=lambda: self.action_service.prepare_action(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                request=request,
                origin_server_run_id=context.server_run_id,
                origin_tool_call_id=context.origin_tool_call_id,
            ),
        )

    def prepare_exchange(
        self,
        session: Session,
        *,
        order_id: str,
        order_item_id: str,
        target_size: str,
        declared_condition: ItemCondition,
        issue_type: IssueType,
        user_note: str | None,
        context: ToolCallContext,
    ) -> PrepareActionResponse:
        customer_id = self._customer_id(session, context)
        conversation_id = self._conversation_id(context)
        request = PrepareActionRequest(
            action_type=ActionType.EXCHANGE_ITEM,
            order_id=order_id,
            order_item_id=order_item_id,
            target_size=target_size,
            declared_condition=declared_condition,
            issue_type=issue_type,
            user_note=user_note,
        )
        return self._call(
            session,
            tool_name="prepare_exchange",
            arguments=request.model_dump(mode="json"),
            context=context,
            customer_id=customer_id,
            operation=lambda: self.action_service.prepare_action(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                request=request,
                origin_server_run_id=context.server_run_id,
                origin_tool_call_id=context.origin_tool_call_id,
            ),
        )

    def create_handoff_ticket(
        self,
        session: Session,
        *,
        request: TicketCreateRequest,
        context: ToolCallContext,
    ) -> TicketRead:
        customer_id = self._customer_id(session, context)
        return self._call(
            session,
            tool_name="create_handoff_ticket",
            arguments=request.model_dump(mode="json"),
            context=context,
            customer_id=customer_id,
            operation=lambda: self.ticket_service.create_ticket(
                session,
                customer_id=customer_id,
                request=request,
            ),
        )

    def verify_return_evidence(
        self,
        session: Session,
        *,
        request: VerifyEvidenceRequest,
        context: ToolCallContext,
    ) -> EvidenceVerificationRead:
        """Host-only deterministic placeholder for evidence verification.

        Never exposed to the Agent allowlist (see HOST_TOOL_NAMES). Ownership
        is rechecked server-side; the verdict is a deterministic mock until a
        real CV pipeline or human review replaces it.
        """

        customer_id = self._customer_id(session, context)
        self.order_service.get_order_model(
            session,
            customer_id=customer_id,
            order_id=request.order_id,
        )
        return self._call(
            session,
            tool_name="verify_return_evidence",
            arguments=request.model_dump(mode="json"),
            context=context,
            customer_id=customer_id,
            operation=lambda: EvidenceVerificationRead(
                order_id=request.order_id,
                evidence_kind=str(request.evidence_kind),
                evidence_ref=request.evidence_ref,
                verdict=(
                    "MOCK_FORGED"
                    if request.evidence_ref.upper().startswith("FORGED-")
                    else "MOCK_ACCEPTED"
                ),
                note=(
                    "演示用确定性占位校验：未接入真实 CV/多模态模型；"
                    "生产环境需真实凭证识别与人工复核。"
                ),
            ),
        )
