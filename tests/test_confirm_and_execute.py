from __future__ import annotations

import inspect
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.demo.host import (
    DEMO_ON_EXECUTE_4XX,
    DEMO_ON_EXECUTE_4XX_EXPIRES_ON,
    confirm_pending,
)
from app.demo.session import DemoSession
from app.enums import ApprovalStatus
from app.errors import ServiceError
from app.host.confirmation import ExecuteFailurePolicy, confirm_and_execute
from app.models import Approval, ConfirmationEvent, Order
from app.utils import utcnow
from tests.conftest import CONVERSATION_ID
from tests.test_api_actions import _prepare, _present


def test_execute_confirmed_action_is_only_in_the_service_and_adapter() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    hits = sorted(
        path.relative_to(app_root.parent).as_posix()
        for path in app_root.rglob("*.py")
        if "execute_confirmed_action" in path.read_text(encoding="utf-8")
    )
    assert hits == [
        "app/host/confirmation.py",
        "app/services/actions.py",
    ]


def test_demo_leave_retryable_exception_is_dated() -> None:
    assert DEMO_ON_EXECUTE_4XX is ExecuteFailurePolicy.LEAVE_RETRYABLE
    default = inspect.signature(confirm_and_execute).parameters["on_execute_4xx"].default
    assert default is ExecuteFailurePolicy.MARK_FAILED
    assert DEMO_ON_EXECUTE_4XX_EXPIRES_ON == date(2027, 1, 4)
    assert date.today() < DEMO_ON_EXECUTE_4XX_EXPIRES_ON


def test_demo_confirm_leaves_stale_approval_retryable(
    client,
    auth_headers,
    host_headers,
    app,
) -> None:
    prepared = _prepare(
        client,
        auth_headers,
        {"action_type": "CANCEL_ORDER", "order_id": "ORD-1001"},
    )
    _present(client, host_headers, prepared)
    with app.state.database.session() as session:
        order = session.get(Order, "ORD-1001")
        assert order is not None
        order.version += 1
        customer_id = order.customer_id

    demo = DemoSession(
        cookie_token_hash="hash",
        csrf_token="csrf",
        csrf_token_hash="csrf-hash",
        customer_id=customer_id,
        customer_display_name="林帆",
        auth_token="token",
        conversation_id=CONVERSATION_ID,
        server_run_id="pytest-run",
        pending_approval_id=prepared["approval_id"],
        pending_preview_hash=prepared["preview_hash"],
        pending_ui_event_id="ui-demo-retryable",
        message_count=0,
        prepare_count=0,
        confirm_count=0,
        live_attempt_count=0,
        expires_at=utcnow() + timedelta(hours=1),
        database=app.state.database,
        tools=app.state.tools,
        settings=app.state.settings,
    )
    with pytest.raises(ServiceError) as exc_info:
        confirm_pending(demo, provider_http_calls=0)

    assert exc_info.value.code == "STALE_APPROVAL"
    with app.state.database.session() as session:
        approval = session.get(Approval, prepared["approval_id"])
        confirmation = session.scalar(
            select(ConfirmationEvent).where(
                ConfirmationEvent.approval_id == prepared["approval_id"]
            )
        )
    assert approval is not None
    assert approval.status == ApprovalStatus.CONFIRMED.value
    assert confirmation is not None
    assert confirmation.consumed_at is None
