from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app.config import Settings
from app.demo.host import (
    confirm_pending,
    load_pending_approval,
    present_pending,
    project_confirmation_card,
)
from app.demo.replay import SUPPORTED_SCENARIOS, handle_message
from app.demo.schemas import (
    DemoConfirmationCard,
    DemoConfirmResponse,
    DemoMessageRequest,
    DemoMessageResponse,
    DemoResetResponse,
    DemoSessionResponse,
    EmptyDemoBody,
)
from app.demo.security import (
    apply_security_headers,
    assert_no_obvious_pii,
    clear_demo_cookie,
    json_error,
    require_csrf,
    require_json_content_type,
    require_origin,
    require_session_cookie,
    set_demo_cookie,
)
from app.demo.session import DemoSession, DemoSessionManager
from app.errors import ServiceError

router = APIRouter(prefix="/demo", tags=["public-demo"])
STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "demo"


def _manager(request: Request) -> DemoSessionManager:
    return request.app.state.demo_sessions


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _guard_mutating(request: Request) -> None:
    settings = _settings(request)
    require_origin(request, settings.demo_allowed_origin)
    require_json_content_type(request)


def _guard_session_mutating(request: Request) -> tuple[str, DemoSession]:
    _guard_mutating(request)
    raw = require_session_cookie(request)
    demo = _manager(request).get(raw)
    require_csrf(request, demo.csrf_token_hash)
    return raw, demo


@router.post("/session", response_model=DemoSessionResponse)
def create_demo_session(
    payload: EmptyDemoBody,
    request: Request,
    response: Response,
) -> DemoSessionResponse:
    del payload
    _guard_mutating(request)
    cookie_token, demo = _manager(request).create()
    set_demo_cookie(
        response,
        raw_token=cookie_token,
        secure=_settings(request).demo_cookie_secure,
        max_age_seconds=_settings(request).demo_session_ttl_minutes * 60,
    )
    apply_security_headers(response)
    return DemoSessionResponse(
        csrf_token=demo.csrf_token,
        customer_display_name=demo.customer_display_name,
        supported_scenarios=list(SUPPORTED_SCENARIOS),
        expires_at=demo.expires_at,
    )


@router.post("/messages", response_model=DemoMessageResponse)
def post_demo_message(
    payload: DemoMessageRequest,
    request: Request,
    response: Response,
) -> DemoMessageResponse:
    _, demo = _guard_session_mutating(request)
    assert_no_obvious_pii(payload.message)
    reply, has_pending = handle_message(demo, payload.message)
    apply_security_headers(response)
    return DemoMessageResponse(
        reply=reply,
        has_pending_action=has_pending,
        provider_http_calls=_manager(request).provider_http_calls,
    )


@router.get("/pending-action", response_model=DemoConfirmationCard)
def get_pending_action(request: Request, response: Response) -> DemoConfirmationCard:
    settings = _settings(request)
    require_origin(request, settings.demo_allowed_origin)
    raw = require_session_cookie(request)
    demo = _manager(request).get(raw)
    with demo.database.session() as db:
        approval = load_pending_approval(db, demo)
        card = project_confirmation_card(approval)
    apply_security_headers(response)
    return card


@router.post("/pending-action/presented", response_model=DemoConfirmationCard)
def present_pending_action(
    payload: EmptyDemoBody,
    request: Request,
    response: Response,
) -> DemoConfirmationCard:
    del payload
    _, demo = _guard_session_mutating(request)
    card = present_pending(demo)
    apply_security_headers(response)
    return card


@router.post("/pending-action/confirm", response_model=DemoConfirmResponse)
def confirm_pending_action(
    payload: EmptyDemoBody,
    request: Request,
    response: Response,
) -> DemoConfirmResponse:
    del payload
    _, demo = _guard_session_mutating(request)
    result = confirm_pending(
        demo,
        provider_http_calls=_manager(request).provider_http_calls,
    )
    apply_security_headers(response)
    return result


@router.post("/reset", response_model=DemoResetResponse)
def reset_demo_session(
    payload: EmptyDemoBody,
    request: Request,
    response: Response,
) -> DemoResetResponse:
    del payload
    _guard_mutating(request)
    raw = require_session_cookie(request)
    demo_before = _manager(request).get(raw)
    require_csrf(request, demo_before.csrf_token_hash)
    cookie_token, demo = _manager(request).reset(raw)
    clear_demo_cookie(response, secure=_settings(request).demo_cookie_secure)
    set_demo_cookie(
        response,
        raw_token=cookie_token,
        secure=_settings(request).demo_cookie_secure,
        max_age_seconds=_settings(request).demo_session_ttl_minutes * 60,
    )
    apply_security_headers(response)
    return DemoResetResponse(
        csrf_token=demo.csrf_token,
        customer_display_name=demo.customer_display_name,
        message="演示数据已重置，会话 Cookie 已轮换。",
        expires_at=demo.expires_at,
    )


def demo_index(_: Request) -> Response:
    index = STATIC_DIR / "index.html"
    response: Response = FileResponse(index, media_type="text/html; charset=utf-8")
    return apply_security_headers(response)


def handle_demo_service_error(_: Request, exc: ServiceError) -> JSONResponse:
    retry_after = 30 if exc.status_code == 429 else None
    return json_error(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        retry_after=retry_after,
    )
