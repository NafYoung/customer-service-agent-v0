from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.demo import (
    APP_MODE_PUBLIC_DEMO,
    DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
)
from app.demo.security import DEMO_COOKIE_NAME_LOCAL
from app.main import create_app
from app.models import ActionExecution, Approval, ConfirmationEvent, Order, ToolEvent

ORIGIN = "http://testserver"
HOST_TOKEN = "demo-prep-host-secret-must-stay-server-side"


@pytest.fixture()
def prep_demo_app():
    return create_app(
        settings=Settings(
            app_mode=APP_MODE_PUBLIC_DEMO,
            demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
            demo_allowed_origin=ORIGIN,
            demo_cookie_secure=False,
            host_confirmation_token=HOST_TOKEN,
            deepseek_api_key="sk-should-never-leak-or-be-used",
            enable_debug_routes=False,
        ),
        seed_demo=False,
    )


@pytest.fixture()
def prep_demo_client(prep_demo_app):
    with TestClient(prep_demo_app, base_url=ORIGIN) as client:
        yield client


def _json_headers(csrf: str | None = None) -> dict[str, str]:
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers


def _start_session(client: TestClient) -> str:
    response = client.post("/demo/session", headers=_json_headers(), json={})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_health_reports_preparation_scripted(prep_demo_client: TestClient, prep_demo_app):
    assert prep_demo_app.state.settings.demo_agent_mode == (
        DEMO_AGENT_MODE_PREPARATION_SCRIPTED
    )
    assert prep_demo_app.state.settings.deepseek_api_key is None
    health = prep_demo_client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["app_mode"] == APP_MODE_PUBLIC_DEMO
    assert body["demo_agent_mode"] == DEMO_AGENT_MODE_PREPARATION_SCRIPTED
    assert body["provider_http_calls"] == 0


def test_preparation_scripted_cancel_happy_path(
    prep_demo_client: TestClient,
    prep_demo_app,
):
    csrf = _start_session(prep_demo_client)
    message = prep_demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "请帮我取消订单 ORD-1001"},
    )
    assert message.status_code == 200, message.text
    body = message.json()
    assert body["has_pending_action"] is True
    assert body["provider_http_calls"] == 0
    assert "Preparation Agent" in body["reply"]
    assert HOST_TOKEN not in message.text
    assert "sk-should-never-leak" not in message.text

    cookie = prep_demo_client.cookies.get(DEMO_COOKIE_NAME_LOCAL)
    demo = prep_demo_app.state.demo_sessions.get(cookie)
    with demo.database.session() as session:
        approvals = list(session.scalars(select(Approval)).all())
        assert len(approvals) == 1
        approval = approvals[0]
        assert approval.origin_server_run_id == demo.server_run_id
        assert approval.origin_tool_call_id
        assert approval.origin_tool_call_id == "demo-script-prepare-cancel"
        tool_names = set(
            session.scalars(select(ToolEvent.tool_name)).all()
        )
        assert "get_order" in tool_names
        assert "check_action_eligibility" in tool_names
        assert "prepare_cancel_order" in tool_names
        assert session.scalar(select(func.count()).select_from(ActionExecution)) == 0

    pending = prep_demo_client.get(
        "/demo/pending-action",
        headers={"Origin": ORIGIN, "Accept": "application/json"},
    )
    assert pending.status_code == 200
    card = pending.json()
    assert card["action_type"] == "CANCEL_ORDER"
    assert card["order_id"] == "ORD-1001"
    assert card["executed"] is False
    serialized = json.dumps(card)
    assert "approval_id" not in serialized
    assert "preview_hash" not in serialized

    presented = prep_demo_client.post(
        "/demo/pending-action/presented",
        headers=_json_headers(csrf),
        json={},
    )
    assert presented.status_code == 200
    assert presented.json()["status"] == "PRESENTED"

    confirmed = prep_demo_client.post(
        "/demo/pending-action/confirm",
        headers=_json_headers(csrf),
        json={},
    )
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["status"] == "EXECUTED"
    assert result["action_type"] == "CANCEL_ORDER"
    assert result["order_id"] == "ORD-1001"
    assert result["provider_http_calls"] == 0

    with demo.database.session() as session:
        order = session.get(Order, "ORD-1001")
        assert order is not None
        assert order.status == "CANCELLED"
        assert session.scalar(select(func.count()).select_from(ActionExecution)) == 1
        assert session.scalar(select(func.count()).select_from(ConfirmationEvent)) == 1


def test_preparation_scripted_return_and_exchange_prepare(
    prep_demo_client: TestClient,
    prep_demo_app,
):
    csrf = _start_session(prep_demo_client)
    returned = prep_demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "我要退货 ORD-1003"},
    )
    assert returned.status_code == 200
    assert returned.json()["has_pending_action"] is True
    card = prep_demo_client.get(
        "/demo/pending-action",
        headers={"Origin": ORIGIN, "Accept": "application/json"},
    )
    assert card.status_code == 200
    assert card.json()["action_type"] == "RETURN_ITEM"

    # New prepare supersedes prior pending in the same conversation.
    exchanged = prep_demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "把 ORD-1003 换成 43 码"},
    )
    assert exchanged.status_code == 200
    assert exchanged.json()["has_pending_action"] is True
    card2 = prep_demo_client.get(
        "/demo/pending-action",
        headers={"Origin": ORIGIN, "Accept": "application/json"},
    )
    assert card2.status_code == 200
    assert card2.json()["action_type"] == "EXCHANGE_ITEM"
    assert card2.json()["target_size"] == "43"

    cookie = prep_demo_client.cookies.get(DEMO_COOKIE_NAME_LOCAL)
    demo = prep_demo_app.state.demo_sessions.get(cookie)
    with demo.database.session() as session:
        active = [
            row
            for row in session.scalars(select(Approval)).all()
            if row.status in {"PREPARED", "PRESENTED", "CONFIRMED"}
        ]
        assert len(active) == 1
        assert active[0].action_type == "EXCHANGE_ITEM"
        assert active[0].origin_tool_call_id == "demo-script-prepare-exchange"


def test_public_demo_still_refuses_live_deepseek():
    with pytest.raises(ValueError, match="refuse live model network"):
        create_app(
            settings=Settings(
                app_mode=APP_MODE_PUBLIC_DEMO,
                demo_agent_mode="live_deepseek",
                deepseek_api_key="sk-x",
            )
        )
