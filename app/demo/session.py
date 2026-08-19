from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, TypedDict

from app.config import Settings
from app.database import Database
from app.demo import DEMO_AGENT_MODE_PREPARATION_LIVE
from app.demo.security import random_token, token_hash
from app.errors import ConflictError, ServiceError, ValidationError
from app.schemas import TicketCreateRequest, TicketRead
from app.seed import seed_demo_data
from app.tools.facade import CustomerServiceTools, ToolCallContext
from app.tools.factory import build_tools
from app.utils import utcnow

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = PROJECT_ROOT / "policies"
DEMO_CUSTOMER_EMAIL = "linfan@example.com"
DEMO_CUSTOMER_DISPLAY_NAME = "林帆"


@dataclass
class PendingSlot:
    """Multi-turn slot fill state before Preparation Agent prepare."""

    kind: str
    order_id: str | None = None
    order_item_id: str | None = None
    target_size: str | None = None
    prompt: str = ""


@dataclass
class DemoSession:
    cookie_token_hash: str
    csrf_token: str
    csrf_token_hash: str
    customer_id: str
    customer_display_name: str
    auth_token: str
    conversation_id: str
    server_run_id: str
    pending_approval_id: str | None
    pending_preview_hash: str | None
    pending_ui_event_id: str | None
    message_count: int
    prepare_count: int
    confirm_count: int
    expires_at: datetime
    database: Database
    tools: CustomerServiceTools
    settings: Settings
    pending_slot: PendingSlot | None = None
    chat_history: list[dict[str, str]] = field(default_factory=list)
    last_tool_trace: list[dict[str, Any]] = field(default_factory=list)
    handoff_ticket_ids: list[str] = field(default_factory=list)
    handoff_reasons: set[str] = field(default_factory=set)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at

    def clear_pending_action(self) -> None:
        self.pending_approval_id = None
        self.pending_preview_hash = None
        self.pending_ui_event_id = None
        self.last_tool_trace = []


class DemoSessionManager:
    """In-process ephemeral demo sessions with isolated SQLite runtimes."""

    def __init__(self, base_settings: Settings):
        self._base_settings = base_settings
        self._sessions: dict[str, DemoSession] = {}
        self._lock = Lock()
        self.provider_http_calls = 0

    @property
    def active_count(self) -> int:
        with self._lock:
            self._purge_locked(utcnow())
            return len(self._sessions)

    def create(self) -> tuple[str, DemoSession]:
        with self._lock:
            now = utcnow()
            self._purge_locked(now)
            if len(self._sessions) >= self._base_settings.demo_max_active_sessions:
                raise ConflictError(
                    "DEMO_SESSION_LIMIT",
                    "公开演示活动会话已达上限，请稍后重试。",
                    status_code=429,
                )
            cookie_token = random_token(32)
            session = self._build_session(cookie_token=cookie_token, now=now)
            self._sessions[session.cookie_token_hash] = session
            return cookie_token, session

    def get(self, cookie_token: str) -> DemoSession:
        with self._lock:
            now = utcnow()
            self._purge_locked(now)
            session = self._sessions.get(token_hash(cookie_token))
            if session is None or session.is_expired(now):
                if session is not None:
                    self._dispose_locked(session.cookie_token_hash)
                raise ValidationError(
                    "DEMO_SESSION_EXPIRED",
                    "演示会话已过期，请重新开始。",
                    status_code=401,
                )
            return session

    def reset(self, cookie_token: str) -> tuple[str, DemoSession]:
        with self._lock:
            now = utcnow()
            self._purge_locked(now)
            old_hash = token_hash(cookie_token)
            if old_hash in self._sessions:
                self._dispose_locked(old_hash)
            cookie_token_new = random_token(32)
            session = self._build_session(cookie_token=cookie_token_new, now=now)
            self._sessions[session.cookie_token_hash] = session
            return cookie_token_new, session

    def dispose_all(self) -> None:
        with self._lock:
            for key in list(self._sessions):
                self._dispose_locked(key)

    def _build_session(self, *, cookie_token: str, now: datetime) -> DemoSession:
        csrf_token = random_token(32)
        host_token = (
            self._base_settings.host_confirmation_token
            or f"demo-host-{random_token(16)}"
        )
        # public_demo already cleared the key at app boot; local preparation_live
        # must keep DEEPSEEK_API_KEY on the ephemeral session settings.
        keep_key = (
            self._base_settings.demo_agent_mode == DEMO_AGENT_MODE_PREPARATION_LIVE
            and self._base_settings.deepseek_api_key
        )
        session_settings = replace(
            self._base_settings,
            database_url="sqlite:///:memory:",
            host_confirmation_token=host_token,
            enable_debug_routes=False,
            debug_admin_token=None,
            deepseek_api_key=(
                self._base_settings.deepseek_api_key if keep_key else None
            ),
        )
        database = Database(session_settings.database_url)
        database.create_all()
        seed_demo_data(database, session_settings)
        tools = build_tools(session_settings, policy_dir=POLICY_DIR)
        with database.session() as db_session:
            auth = tools.auth_service.authenticate(
                db_session,
                email=DEMO_CUSTOMER_EMAIL,
                verification_code=session_settings.demo_verification_code,
            )
        return DemoSession(
            cookie_token_hash=token_hash(cookie_token),
            csrf_token=csrf_token,
            csrf_token_hash=token_hash(csrf_token),
            customer_id=auth.customer_id,
            customer_display_name=DEMO_CUSTOMER_DISPLAY_NAME,
            auth_token=auth.access_token,
            conversation_id=f"demo-conv-{random_token(8)}",
            server_run_id=f"demo-run-{random_token(8)}",
            pending_approval_id=None,
            pending_preview_hash=None,
            pending_ui_event_id=None,
            message_count=0,
            prepare_count=0,
            confirm_count=0,
            expires_at=now
            + timedelta(minutes=session_settings.demo_session_ttl_minutes),
            database=database,
            tools=tools,
            settings=session_settings,
            pending_slot=None,
            chat_history=[],
            last_tool_trace=[],
            handoff_ticket_ids=[],
            handoff_reasons=set(),
        )

    def _purge_locked(self, now: datetime) -> None:
        expired = [
            key for key, session in self._sessions.items() if session.is_expired(now)
        ]
        for key in expired:
            self._dispose_locked(key)

    def _dispose_locked(self, cookie_token_hash: str) -> None:
        session = self._sessions.pop(cookie_token_hash, None)
        if session is not None:
            session.database.engine.dispose()


class HandoffSpec(TypedDict, total=False):
    reason: str
    category: str
    summary: str
    order_id: str | None


def ensure_handoff_ticket(
    session: DemoSession,
    *,
    reason: str,
    category: str,
    summary: str,
    order_id: str | None = None,
) -> str | None:
    """Host-side handoff ticket, deduplicated per session reason.

    The Agent keeps its exact 9-tool allowlist; creating a handoff ticket is a
    host decision, never a model tool. Returns the ticket id, or None when this
    reason already produced a ticket in the session.
    """

    if reason in session.handoff_reasons:
        return None
    request = TicketCreateRequest(
        order_id=order_id,
        category=category,
        summary=summary,
    )
    with session.database.session() as db:
        ticket: TicketRead = session.tools.create_handoff_ticket(
            db,
            request=request,
            context=tool_context(
                session,
                tool_call_id=f"demo-handoff-{uuid.uuid4().hex[:12]}",
            ),
        )
    session.handoff_reasons.add(reason)
    session.handoff_ticket_ids.append(ticket.id)
    return ticket.id


def bump_or_limit(
    session: DemoSession,
    *,
    counter: str,
    limit: int,
    code: str,
    message: str,
    handoff: HandoffSpec | None = None,
) -> None:
    current = getattr(session, counter)
    if current >= limit:
        ticket_id = None
        if handoff is not None:
            ticket_id = ensure_handoff_ticket(
                session,
                reason=handoff["reason"],
                category=handoff["category"],
                summary=handoff["summary"],
                order_id=handoff.get("order_id"),
            )
        suffix = (
            f" 已生成人工工单 {ticket_id}，后续由人工客服跟进。"
            if ticket_id
            else ""
        )
        raise ServiceError(code, message + suffix, status_code=429)
    setattr(session, counter, current + 1)


def tool_context(
    session: DemoSession,
    *,
    tool_call_id: str | None = None,
) -> ToolCallContext:
    return ToolCallContext(
        run_id=session.server_run_id,
        server_run_id=session.server_run_id,
        origin_tool_call_id=tool_call_id,
        auth_token=session.auth_token,
        conversation_id=session.conversation_id,
    )
