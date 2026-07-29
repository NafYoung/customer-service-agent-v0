from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AuthenticationError
from app.models import AuthSession, Customer
from app.schemas import AuthResponse
from app.utils import utcnow

_DEMO_HASH_NAMESPACE = "customer-service-agent-v0"


def hash_verification_code(code: str) -> str:
    payload = f"{_DEMO_HASH_NAMESPACE}:{code}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class AuthService:
    def __init__(self, *, session_minutes: int):
        self.session_minutes = session_minutes

    def authenticate(self, session: Session, *, email: str, verification_code: str) -> AuthResponse:
        customer = session.scalar(select(Customer).where(Customer.email == email.lower().strip()))
        if customer is None:
            raise AuthenticationError(
                "INVALID_CREDENTIALS",
                "邮箱或验证码不正确。",
                status_code=401,
            )

        supplied_hash = hash_verification_code(verification_code)
        if not hmac.compare_digest(supplied_hash, customer.verification_code_hash):
            raise AuthenticationError(
                "INVALID_CREDENTIALS",
                "邮箱或验证码不正确。",
                status_code=401,
            )

        now = utcnow()
        auth_session = AuthSession(
            token=secrets.token_urlsafe(32),
            customer_id=customer.id,
            created_at=now,
            expires_at=now + timedelta(minutes=self.session_minutes),
        )
        session.add(auth_session)
        session.flush()
        return AuthResponse(
            access_token=auth_session.token,
            customer_id=customer.id,
            expires_at=auth_session.expires_at,
        )

    def resolve_customer_id(self, session: Session, token: str) -> str:
        auth_session = session.get(AuthSession, token)
        if auth_session is None:
            raise AuthenticationError(
                "INVALID_TOKEN",
                "登录状态无效。",
                status_code=401,
            )
        if auth_session.expires_at <= utcnow():
            session.delete(auth_session)
            raise AuthenticationError(
                "TOKEN_EXPIRED",
                "登录状态已过期，请重新验证身份。",
                status_code=401,
            )
        return auth_session.customer_id
