from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.errors import AuthenticationError, ServiceError
from app.models import ToolEvent
from app.schemas import (
    AuthRequest,
    AuthResponse,
    ConfirmActionRequest,
    DebugToolEventRead,
    EligibilityRequest,
    EligibilityResponse,
    ExecuteActionResponse,
    InventoryRead,
    OrderRead,
    PolicySearchRequest,
    PolicySearchResponse,
    PrepareActionRequest,
    PrepareActionResponse,
    PresentApprovalRequest,
    PresentApprovalResponse,
    ShipmentRead,
    TicketCreateRequest,
    TicketRead,
)
from app.tools.facade import CustomerServiceTools, ToolCallContext

router = APIRouter(prefix="/v1")
debug_router = APIRouter(prefix="/v1")
security = HTTPBearer(auto_error=True)


def _tools(request: Request) -> CustomerServiceTools:
    return request.app.state.tools


def _context(
    credentials: HTTPAuthorizationCredentials | None,
    run_id: str | None,
    conversation_id: str | None = None,
) -> ToolCallContext:
    return ToolCallContext(
        run_id=run_id,
        auth_token=credentials.credentials if credentials else None,
        conversation_id=conversation_id,
    )


def _require_host_confirmation(
    request: Request,
    provided_token: str | None,
) -> None:
    configured_token = request.app.state.settings.host_confirmation_token
    if (
        not configured_token
        or not provided_token
        or not hmac.compare_digest(provided_token, configured_token)
    ):
        raise AuthenticationError(
            "HOST_AUTH_REQUIRED",
            "该操作只能由受信宿主确认通道调用。",
            status_code=401,
        )


def _require_debug_admin(
    request: Request,
    provided_token: str | None,
) -> None:
    configured_token = request.app.state.settings.debug_admin_token
    if (
        not configured_token
        or not provided_token
        or not hmac.compare_digest(provided_token, configured_token)
    ):
        raise AuthenticationError(
            "ADMIN_AUTH_REQUIRED",
            "需要调试管理员凭据。",
            status_code=401,
        )


def _terminalize_expired_approval(
    request: Request,
    *,
    auth_token: str,
    conversation_id: str,
    approval_id: str,
) -> None:
    tools = _tools(request)
    with request.app.state.database.session() as session:
        customer_id = tools.auth_service.resolve_customer_id(session, auth_token)
        tools.action_service.mark_expired(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
        )


@router.post("/auth/verify", response_model=AuthResponse, tags=["identity"])
def authenticate_customer(
    payload: AuthRequest,
    request: Request,
) -> AuthResponse:
    with request.app.state.database.session() as session:
        return _tools(request).auth_service.authenticate(
            session,
            email=str(payload.email),
            verification_code=payload.verification_code,
        )


@router.get("/orders", response_model=list[OrderRead], tags=["orders"])
def get_customer_orders(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    x_run_id: Annotated[str | None, Header()] = None,
) -> list[OrderRead]:
    with request.app.state.database.session() as session:
        return _tools(request).get_customer_orders(
            session,
            context=_context(credentials, x_run_id),
        )


@router.get("/orders/{order_id}", response_model=OrderRead, tags=["orders"])
def get_order(
    order_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    x_run_id: Annotated[str | None, Header()] = None,
) -> OrderRead:
    with request.app.state.database.session() as session:
        return _tools(request).get_order(
            session,
            order_id=order_id,
            context=_context(credentials, x_run_id),
        )


@router.get(
    "/orders/{order_id}/shipment",
    response_model=ShipmentRead,
    tags=["orders"],
)
def get_shipment(
    order_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    x_run_id: Annotated[str | None, Header()] = None,
) -> ShipmentRead:
    with request.app.state.database.session() as session:
        return _tools(request).get_shipment(
            session,
            order_id=order_id,
            context=_context(credentials, x_run_id),
        )


@router.get("/inventory", response_model=InventoryRead, tags=["orders"])
def get_inventory(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    sku: Annotated[str, Query(min_length=2, max_length=80)],
    size: Annotated[str, Query(min_length=1, max_length=32)],
    x_run_id: Annotated[str | None, Header()] = None,
) -> InventoryRead:
    with request.app.state.database.session() as session:
        return _tools(request).get_inventory(
            session,
            sku=sku,
            size=size,
            context=_context(credentials, x_run_id),
        )


@router.post("/policies/search", response_model=PolicySearchResponse, tags=["policies"])
def search_policy(
    payload: PolicySearchRequest,
    request: Request,
    x_run_id: Annotated[str | None, Header()] = None,
) -> PolicySearchResponse:
    with request.app.state.database.session() as session:
        return _tools(request).search_policy(
            session,
            request=payload,
            context=ToolCallContext(run_id=x_run_id),
        )


@router.post(
    "/actions/eligibility",
    response_model=EligibilityResponse,
    tags=["actions"],
)
def check_action_eligibility(
    payload: EligibilityRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    x_run_id: Annotated[str | None, Header()] = None,
) -> EligibilityResponse:
    with request.app.state.database.session() as session:
        return _tools(request).check_action_eligibility(
            session,
            request=payload,
            context=_context(credentials, x_run_id),
        )


@router.post("/actions/prepare", response_model=PrepareActionResponse, tags=["actions"])
def prepare_action(
    payload: PrepareActionRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    x_conversation_id: Annotated[str, Header(min_length=1, max_length=120)],
    x_run_id: Annotated[str | None, Header()] = None,
) -> PrepareActionResponse:
    with request.app.state.database.session() as session:
        return _tools(request).prepare_action(
            session,
            request=payload,
            context=_context(credentials, x_run_id, x_conversation_id),
        )


@router.post(
    "/actions/{approval_id}/present",
    response_model=PresentApprovalResponse,
    tags=["host-confirmation"],
)
def present_prepared_action(
    approval_id: str,
    payload: PresentApprovalRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    x_conversation_id: Annotated[str, Header(min_length=1, max_length=120)],
    x_host_confirmation_token: Annotated[str | None, Header()] = None,
) -> PresentApprovalResponse:
    _require_host_confirmation(request, x_host_confirmation_token)
    try:
        with request.app.state.database.session() as session:
            customer_id = _tools(request).auth_service.resolve_customer_id(
                session,
                credentials.credentials,
            )
            return _tools(request).action_service.present_action(
                session,
                customer_id=customer_id,
                conversation_id=x_conversation_id,
                approval_id=approval_id,
                request=payload,
            )
    except ServiceError as exc:
        if exc.code == "APPROVAL_EXPIRED":
            _terminalize_expired_approval(
                request,
                auth_token=credentials.credentials,
                conversation_id=x_conversation_id,
                approval_id=approval_id,
            )
        raise


@router.post(
    "/actions/{approval_id}/confirm",
    response_model=ExecuteActionResponse,
    tags=["host-confirmation"],
)
def confirm_prepared_action(
    approval_id: str,
    payload: ConfirmActionRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    x_conversation_id: Annotated[str, Header(min_length=1, max_length=120)],
    x_host_confirmation_token: Annotated[str | None, Header()] = None,
) -> ExecuteActionResponse:
    _require_host_confirmation(request, x_host_confirmation_token)
    tools = _tools(request)

    try:
        with request.app.state.database.session() as session:
            customer_id = tools.auth_service.resolve_customer_id(
                session,
                credentials.credentials,
            )
            confirmation = tools.action_service.record_confirmation(
                session,
                customer_id=customer_id,
                conversation_id=x_conversation_id,
                approval_id=approval_id,
                request=payload,
            )
    except ServiceError as exc:
        if exc.code == "APPROVAL_EXPIRED":
            _terminalize_expired_approval(
                request,
                auth_token=credentials.credentials,
                conversation_id=x_conversation_id,
                approval_id=approval_id,
            )
        raise

    try:
        with request.app.state.database.session() as session:
            customer_id = tools.auth_service.resolve_customer_id(
                session,
                credentials.credentials,
            )
            return tools.action_service.execute_confirmed_action(
                session,
                customer_id=customer_id,
                conversation_id=x_conversation_id,
                approval_id=approval_id,
                confirmation_event_id=confirmation.confirmation_event_id,
            )
    except ServiceError as exc:
        if exc.status_code < 500:
            with request.app.state.database.session() as session:
                customer_id = tools.auth_service.resolve_customer_id(
                    session,
                    credentials.credentials,
                )
                if exc.code == "APPROVAL_EXPIRED":
                    tools.action_service.mark_expired(
                        session,
                        customer_id=customer_id,
                        conversation_id=x_conversation_id,
                        approval_id=approval_id,
                    )
                else:
                    tools.action_service.mark_failed(
                        session,
                        customer_id=customer_id,
                        conversation_id=x_conversation_id,
                        approval_id=approval_id,
                        failure_code=exc.code,
                    )
        raise


@router.post("/tickets", response_model=TicketRead, tags=["handoff"])
def create_handoff_ticket(
    payload: TicketCreateRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    x_run_id: Annotated[str | None, Header()] = None,
) -> TicketRead:
    with request.app.state.database.session() as session:
        return _tools(request).create_handoff_ticket(
            session,
            request=payload,
            context=_context(credentials, x_run_id),
        )


@debug_router.get(
    "/debug/tool-events",
    response_model=list[DebugToolEventRead],
    tags=["debug"],
)
def list_tool_events(
    request: Request,
    run_id: Annotated[str, Query(min_length=1, max_length=120)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    x_debug_admin_token: Annotated[str | None, Header()] = None,
) -> list[DebugToolEventRead]:
    _require_debug_admin(request, x_debug_admin_token)
    with request.app.state.database.session() as session:
        statement = (
            select(ToolEvent)
            .where(ToolEvent.run_id == run_id)
            .order_by(ToolEvent.created_at.desc())
            .limit(limit)
        )
        events = session.scalars(statement).all()
        return [DebugToolEventRead.model_validate(event) for event in events]
