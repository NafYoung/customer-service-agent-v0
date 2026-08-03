"""Demo UX: reject pending, slot fill, tool_trace, local preparation_live gates."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.demo import (
    APP_MODE_PUBLIC_DEMO,
    DEMO_AGENT_MODE_PREPARATION_LIVE,
    DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
)
from app.demo.security import DEMO_COOKIE_NAME_LOCAL
from app.enums import ApprovalStatus
from app.main import create_app
from app.models import ActionExecution, Approval

ORIGIN = "http://testserver"
HOST_TOKEN = "demo-ux-host-secret-must-stay-server-side"


@pytest.fixture()
def scripted_app():
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
def scripted_client(scripted_app):
    with TestClient(scripted_app, base_url=ORIGIN) as client:
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


def _start_session(client: TestClient) -> dict:
    response = client.post("/demo/session", headers=_json_headers(), json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["demo_agent_mode"] == DEMO_AGENT_MODE_PREPARATION_SCRIPTED
    assert "mode_label" in body
    assert body["csrf_token"]
    return body


def test_public_demo_refuses_preparation_live():
    with pytest.raises(ValueError, match="refuse live model network"):
        create_app(
            settings=Settings(
                app_mode=APP_MODE_PUBLIC_DEMO,
                demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_LIVE,
                deepseek_api_key="sk-x",
                demo_allowed_origin=ORIGIN,
                host_confirmation_token=HOST_TOKEN,
            )
        )


def test_local_live_requires_key_and_origin():
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        create_app(
            settings=Settings(
                app_mode="local",
                demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_LIVE,
                deepseek_api_key=None,
                demo_allowed_origin=ORIGIN,
                host_confirmation_token=HOST_TOKEN,
                enable_debug_routes=False,
            ),
            seed_demo=False,
        )
    with pytest.raises(ValueError, match="DEMO_ALLOWED_ORIGIN"):
        create_app(
            settings=Settings(
                app_mode="local",
                demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_LIVE,
                deepseek_api_key="sk-local-only",
                demo_allowed_origin="",
                host_confirmation_token=HOST_TOKEN,
                enable_debug_routes=False,
            ),
            seed_demo=False,
        )


def test_local_live_mounts_demo_and_keeps_docs():
    app = create_app(
        settings=Settings(
            app_mode="local",
            demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_LIVE,
            deepseek_api_key="sk-local-only-not-for-public",
            demo_allowed_origin=ORIGIN,
            demo_cookie_secure=False,
            host_confirmation_token=HOST_TOKEN,
            enable_debug_routes=False,
            database_url="sqlite:///:memory:",
        ),
        seed_demo=False,
    )
    assert app.state.settings.deepseek_api_key == "sk-local-only-not-for-public"
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/").status_code == 200
        assert client.get("/docs").status_code == 200
        health = client.get("/health").json()
        assert health["demo_agent_mode"] == DEMO_AGENT_MODE_PREPARATION_LIVE
        assert health["provider_http_calls"] == 0
        session = client.post("/demo/session", headers=_json_headers(), json={})
        assert session.status_code == 200
        assert session.json()["demo_agent_mode"] == DEMO_AGENT_MODE_PREPARATION_LIVE
        assert "live" in session.json()["mode_label"].lower() or "DeepSeek" in (
            session.json()["mode_label"]
        )


def test_message_returns_tool_trace(scripted_client: TestClient):
    csrf = _start_session(scripted_client)["csrf_token"]
    message = scripted_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "取消订单 ORD-1001"},
    )
    assert message.status_code == 200, message.text
    body = message.json()
    assert body["has_pending_action"] is True
    assert isinstance(body["tool_trace"], list)
    assert len(body["tool_trace"]) >= 3
    names = [item["tool_name"] for item in body["tool_trace"]]
    assert "get_order" in names
    assert "prepare_cancel_order" in names
    assert all("summary" in item for item in body["tool_trace"])
    assert HOST_TOKEN not in message.text


def test_slot_fill_return_then_prepare(scripted_client: TestClient):
    csrf = _start_session(scripted_client)["csrf_token"]
    first = scripted_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "我想退货"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["has_pending_action"] is False
    assert body["tool_trace"] == []
    assert "ORD-1003" in body["reply"]

    second = scripted_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "ORD-1003"},
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["has_pending_action"] is True
    assert any(item["tool_name"] == "prepare_return" for item in body2["tool_trace"])
    card = scripted_client.get(
        "/demo/pending-action",
        headers={"Origin": ORIGIN, "Accept": "application/json"},
    )
    assert card.status_code == 200
    assert card.json()["action_type"] == "RETURN_ITEM"


def test_reject_pending_supersedes_without_execution(
    scripted_client: TestClient,
    scripted_app,
):
    csrf = _start_session(scripted_client)["csrf_token"]
    scripted_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "取消订单 ORD-1001"},
    )
    presented = scripted_client.post(
        "/demo/pending-action/presented",
        headers=_json_headers(csrf),
        json={},
    )
    assert presented.status_code == 200

    rejected = scripted_client.post(
        "/demo/pending-action/reject",
        headers=_json_headers(csrf),
        json={},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"
    assert "未写入" in rejected.json()["message"]

    missing = scripted_client.get(
        "/demo/pending-action",
        headers={"Origin": ORIGIN, "Accept": "application/json"},
    )
    assert missing.status_code == 404

    cookie = scripted_client.cookies.get(DEMO_COOKIE_NAME_LOCAL)
    demo = scripted_app.state.demo_sessions.get(cookie)
    with demo.database.session() as session:
        approvals = list(session.scalars(select(Approval)).all())
        assert len(approvals) == 1
        assert approvals[0].status == ApprovalStatus.SUPERSEDED.value
        executions = list(session.scalars(select(ActionExecution)).all())
        assert executions == []


def test_local_live_message_uses_runner_without_network():
    app = create_app(
        settings=Settings(
            app_mode="local",
            demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_LIVE,
            deepseek_api_key="sk-local-only-not-for-public",
            demo_allowed_origin=ORIGIN,
            demo_cookie_secure=False,
            host_confirmation_token=HOST_TOKEN,
            enable_debug_routes=False,
            database_url="sqlite:///:memory:",
        ),
        seed_demo=False,
    )
    with (
        patch(
            "app.demo.live_runner.run_preparation_live",
            return_value=(
                "mock live reply",
                False,
                2,
            ),
        ) as mocked,
        TestClient(app, base_url=ORIGIN) as client,
    ):
        csrf = client.post("/demo/session", headers=_json_headers(), json={}).json()[
            "csrf_token"
        ]
        message = client.post(
            "/demo/messages",
            headers=_json_headers(csrf),
            json={"message": "查一下我的订单"},
        )
        assert message.status_code == 200, message.text
        body = message.json()
        assert body["reply"] == "mock live reply"
        assert body["has_pending_action"] is False
        assert body["provider_http_calls"] == 2
        mocked.assert_called_once()
        health = client.get("/health").json()
        assert health["provider_http_calls"] == 2
