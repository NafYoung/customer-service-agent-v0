from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.errors import AuthenticationError, ServiceError, ValidationError
from app.host.flow import (
    HostApprovalCard,
    HostMessageRequest,
    HostMessageResponse,
    HostPilotFlow,
)
from app.schemas import ExecuteActionResponse, ManualHandoffRead, ManualHandoffRequest

host_router = APIRouter(prefix="/v1/host", tags=["trusted-host"])
_security = HTTPBearer(auto_error=True)


def _flow(request: Request) -> HostPilotFlow:
    return request.app.state.host_flow


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


async def _require_empty_body(request: Request) -> None:
    if await request.body():
        raise ValidationError(
            "HOST_BODY_MUST_BE_EMPTY",
            "受信宿主确认端点不接受请求体。",
            status_code=422,
        )


@host_router.post("/messages", response_model=HostMessageResponse)
def send_host_message(
    payload: HostMessageRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_security)],
    x_conversation_id: Annotated[str, Header(min_length=1, max_length=120)],
    x_host_confirmation_token: Annotated[str | None, Header()] = None,
) -> HostMessageResponse:
    _require_host_confirmation(request, x_host_confirmation_token)
    with request.app.state.database.session() as session:
        return _flow(request).handle_message(
            session,
            auth_token=credentials.credentials,
            conversation_id=x_conversation_id,
            text=payload.text,
        )


@host_router.post("/handoffs", response_model=ManualHandoffRead)
def create_host_handoff(
    payload: ManualHandoffRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_security)],
    x_conversation_id: Annotated[str, Header(min_length=1, max_length=120)],
    x_host_confirmation_token: Annotated[str | None, Header()] = None,
) -> ManualHandoffRead:
    _require_host_confirmation(request, x_host_confirmation_token)
    with request.app.state.database.session() as session:
        return _flow(request).create_explicit_handoff(
            session,
            auth_token=credentials.credentials,
            conversation_id=x_conversation_id,
            request=payload,
        )


@host_router.post(
    "/approvals/{approval_id}/present",
    response_model=HostApprovalCard,
)
async def present_host_approval(
    approval_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_security)],
    x_conversation_id: Annotated[str, Header(min_length=1, max_length=120)],
    x_host_confirmation_token: Annotated[str | None, Header()] = None,
) -> HostApprovalCard:
    _require_host_confirmation(request, x_host_confirmation_token)
    await _require_empty_body(request)
    try:
        with request.app.state.database.session() as session:
            return _flow(request).present_approval(
                session,
                auth_token=credentials.credentials,
                conversation_id=x_conversation_id,
                approval_id=approval_id,
            )
    except ServiceError as exc:
        if exc.code == "APPROVAL_EXPIRED":
            with request.app.state.database.session() as session:
                _flow(request).mark_confirmation_failed(
                    session,
                    auth_token=credentials.credentials,
                    conversation_id=x_conversation_id,
                    approval_id=approval_id,
                    failure_code=exc.code,
                )
        raise


@host_router.post(
    "/approvals/{approval_id}/confirm",
    response_model=ExecuteActionResponse,
)
async def confirm_host_approval(
    approval_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_security)],
    x_conversation_id: Annotated[str, Header(min_length=1, max_length=120)],
    x_host_confirmation_token: Annotated[str | None, Header()] = None,
) -> ExecuteActionResponse:
    _require_host_confirmation(request, x_host_confirmation_token)
    await _require_empty_body(request)
    flow = _flow(request)
    try:
        with request.app.state.database.session() as session:
            confirmation = flow.record_confirmation(
                session,
                auth_token=credentials.credentials,
                conversation_id=x_conversation_id,
                approval_id=approval_id,
            )
    except ServiceError as exc:
        if exc.code == "APPROVAL_EXPIRED":
            with request.app.state.database.session() as session:
                flow.mark_confirmation_failed(
                    session,
                    auth_token=credentials.credentials,
                    conversation_id=x_conversation_id,
                    approval_id=approval_id,
                    failure_code=exc.code,
                )
        raise

    try:
        with request.app.state.database.session() as session:
            return flow.execute_confirmation(
                session,
                auth_token=credentials.credentials,
                conversation_id=x_conversation_id,
                approval_id=approval_id,
                confirmation_event_id=confirmation.confirmation_event_id,
            )
    except ServiceError as exc:
        if exc.status_code < 500:
            with request.app.state.database.session() as session:
                flow.mark_confirmation_failed(
                    session,
                    auth_token=credentials.credentials,
                    conversation_id=x_conversation_id,
                    approval_id=approval_id,
                    failure_code=exc.code,
                )
        raise
