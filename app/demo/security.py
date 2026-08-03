from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app.errors import AuthenticationError, AuthorizationError, ValidationError

DEMO_COOKIE_NAME_HOST = "__Host-rivet_demo"
DEMO_COOKIE_NAME_LOCAL = "rivet_demo"
DEMO_CSRF_HEADER = "X-CSRF-Token"


def demo_cookie_name(*, secure: bool) -> str:
    # __Host- cookies require the Secure attribute; use a local name over HTTP.
    return DEMO_COOKIE_NAME_HOST if secure else DEMO_COOKIE_NAME_LOCAL


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hashes_match(provided: str, expected_hash: str) -> bool:
    return hmac.compare_digest(token_hash(provided), expected_hash)


def set_demo_cookie(
    response: Response,
    *,
    raw_token: str,
    secure: bool,
    max_age_seconds: int,
) -> None:
    response.set_cookie(
        key=demo_cookie_name(secure=secure),
        value=raw_token,
        max_age=max_age_seconds,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


def clear_demo_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        key=demo_cookie_name(secure=secure),
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def read_demo_cookie(request: Request) -> str | None:
    return (
        request.cookies.get(DEMO_COOKIE_NAME_HOST)
        or request.cookies.get(DEMO_COOKIE_NAME_LOCAL)
    )


def require_json_content_type(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type != "application/json":
        raise ValidationError(
            "UNSUPPORTED_MEDIA_TYPE",
            "公开演示接口仅接受 application/json。",
            status_code=415,
        )


def require_origin(request: Request, allowed_origin: str) -> None:
    """Allow same-origin demo traffic even when browsers omit Origin on GET.

    fetch() same-origin GET often has no Origin header; POST includes it.
    Reject cross-site callers via mismatched Origin/Referer.
    """

    allowed = allowed_origin.rstrip("/")
    origin = request.headers.get("origin")
    if origin is not None:
        if origin.rstrip("/") == allowed:
            return
        raise AuthorizationError(
            "ORIGIN_FORBIDDEN",
            "请求来源不被允许。",
            status_code=403,
        )

    referer = (request.headers.get("referer") or "").strip()
    if referer == allowed or referer.startswith(allowed + "/"):
        return

    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").casefold()
    host = (request.headers.get("host") or "").split(":", 1)[0].casefold()
    allowed_host = allowed.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[
        0
    ].casefold()
    if sec_fetch_site == "same-origin" and host == allowed_host:
        return

    raise AuthorizationError(
        "ORIGIN_FORBIDDEN",
        "请求来源不被允许。",
        status_code=403,
    )


def require_csrf(request: Request, expected_hash: str) -> None:
    provided = request.headers.get(DEMO_CSRF_HEADER)
    if not provided or not hashes_match(provided, expected_hash):
        raise AuthorizationError(
            "CSRF_FORBIDDEN",
            "跨站请求伪造校验失败。",
            status_code=403,
        )


_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?\d[\d\-\s]{8,}\d)")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def assert_no_obvious_pii(text: str) -> None:
    if _EMAIL_RE.search(text) or _PHONE_RE.search(text) or _CARD_RE.search(text):
        raise ValidationError(
            "DEMO_PII_BLOCKED",
            "公开演示只接受虚构数据，请勿输入真实邮箱、手机号或卡号。",
            status_code=400,
        )


def security_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        "Cache-Control": "no-store",
    }


def apply_security_headers(response: Response) -> Response:
    for key, value in security_headers().items():
        response.headers[key] = value
    return response


def error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def json_error(
    *,
    status_code: int,
    code: str,
    message: str,
    retry_after: int | None = None,
) -> JSONResponse:
    headers = security_headers()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message),
        headers=headers,
    )


def require_session_cookie(request: Request) -> str:
    raw = read_demo_cookie(request)
    if not raw:
        raise AuthenticationError(
            "DEMO_SESSION_REQUIRED",
            "请先创建公开演示会话。",
            status_code=401,
        )
    return raw
