from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.demo import APP_MODE_PUBLIC_DEMO
from app.demo.security import DEMO_COOKIE_NAME_LOCAL
from app.main import create_app
from app.models import ActionExecution, Approval, ConfirmationEvent, Order

ORIGIN = "http://testserver"
HOST_TOKEN = "demo-host-secret-must-stay-server-side"


@pytest.fixture()
def demo_app():
    return create_app(
        settings=Settings(
            app_mode=APP_MODE_PUBLIC_DEMO,
            demo_agent_mode="offline_replay",
            demo_allowed_origin=ORIGIN,
            demo_cookie_secure=False,
            host_confirmation_token=HOST_TOKEN,
            deepseek_api_key="sk-should-never-leak-or-be-used",
            enable_debug_routes=False,
        ),
        seed_demo=False,
    )


@pytest.fixture()
def demo_client(demo_app):
    with TestClient(demo_app, base_url=ORIGIN) as client:
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
    assert DEMO_COOKIE_NAME_LOCAL in client.cookies
    csrf = response.json()["csrf_token"]
    assert csrf
    assert HOST_TOKEN not in response.text
    assert "sk-should-never-leak" not in response.text
    return csrf


def test_public_demo_refuses_live_agent_mode():
    with pytest.raises(ValueError, match="offline_replay"):
        create_app(
            settings=Settings(
                app_mode=APP_MODE_PUBLIC_DEMO,
                demo_agent_mode="live_deepseek",
                deepseek_api_key="sk-x",
            )
        )


def test_public_demo_clears_deepseek_key(demo_app):
    assert demo_app.state.settings.deepseek_api_key is None
    assert demo_app.state.settings.app_mode == APP_MODE_PUBLIC_DEMO


def test_public_demo_does_not_expose_v1_or_debug(demo_client: TestClient):
    assert demo_client.get("/v1/orders").status_code == 404
    assert demo_client.post("/v1/actions/prepare", json={}).status_code == 404
    assert demo_client.get("/docs").status_code == 404
    health = demo_client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["app_mode"] == APP_MODE_PUBLIC_DEMO
    assert body["provider_http_calls"] == 0


def test_ui_and_openapi_have_no_host_token_or_api_key(demo_client: TestClient):
    page = demo_client.get("/")
    assert page.status_code == 200
    assert "RIVET" in page.text
    assert HOST_TOKEN not in page.text
    assert "sk-should-never-leak" not in page.text
    assert "DEEPSEEK" not in page.text
    css = demo_client.get("/demo-static/app.css")
    js = demo_client.get("/demo-static/app.js")
    assert css.status_code == 200
    assert js.status_code == 200
    blob = page.text + css.text + js.text
    assert HOST_TOKEN not in blob
    assert "sk-should-never-leak" not in blob


def test_origin_and_csrf_fail_closed(demo_client: TestClient):
    csrf = _start_session(demo_client)
    bad_origin = demo_client.post(
        "/demo/messages",
        headers={
            "Origin": "https://evil.example",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
        },
        json={"message": "取消订单 ORD-1001"},
    )
    assert bad_origin.status_code == 403
    assert bad_origin.json()["error"]["code"] == "ORIGIN_FORBIDDEN"

    bad_csrf = demo_client.post(
        "/demo/messages",
        headers=_json_headers("wrong-csrf-token-value"),
        json={"message": "取消订单 ORD-1001"},
    )
    assert bad_csrf.status_code == 403
    assert bad_csrf.json()["error"]["code"] == "CSRF_FORBIDDEN"


def test_browser_cannot_inject_host_fields_on_present_confirm(
    demo_client: TestClient,
):
    csrf = _start_session(demo_client)
    demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "取消订单 ORD-1001"},
    )
    forged_present = demo_client.post(
        "/demo/pending-action/presented",
        headers=_json_headers(csrf),
        json={
            "preview_hash": "a" * 64,
            "approval_id": "APR-FAKE",
            "host_confirmation_token": HOST_TOKEN,
        },
    )
    assert forged_present.status_code == 422

    forged_confirm = demo_client.post(
        "/demo/pending-action/confirm",
        headers=_json_headers(csrf),
        json={
            "preview_hash": "a" * 64,
            "ui_event_id": "attacker-event",
            "confirmation_source": "BUTTON",
            "approval_id": "APR-FAKE",
        },
    )
    assert forged_confirm.status_code == 422


def test_pending_action_get_rejects_cross_site_without_origin(
    demo_client: TestClient,
):
    csrf = _start_session(demo_client)
    demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "取消订单 ORD-1001"},
    )
    blocked = demo_client.get(
        "/demo/pending-action",
        headers={
            "Accept": "application/json",
            "Referer": "https://evil.example/",
        },
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "ORIGIN_FORBIDDEN"
    csrf = _start_session(demo_client)
    prepared = demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "取消订单 ORD-1001"},
    )
    assert prepared.status_code == 200
    assert prepared.json()["has_pending_action"] is True

    confirm = demo_client.post(
        "/demo/pending-action/confirm",
        headers=_json_headers(csrf),
        json={},
    )
    assert confirm.status_code == 409
    assert confirm.json()["error"]["code"] == "APPROVAL_NOT_PRESENTED"

    manager = demo_app.state.demo_sessions
    cookie = demo_client.cookies.get(DEMO_COOKIE_NAME_LOCAL)
    assert cookie
    demo = manager.get(cookie)
    with demo.database.session() as session:
        assert session.scalar(select(func.count()).select_from(ActionExecution)) == 0
        order = session.get(Order, "ORD-1001")
        assert order is not None
        assert order.status == "PAID"


def test_prepare_present_confirm_execute_happy_path(demo_client: TestClient, demo_app):
    csrf = _start_session(demo_client)
    message = demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "请帮我取消订单 ORD-1001"},
    )
    assert message.status_code == 200
    body = message.json()
    assert body["has_pending_action"] is True
    assert body["provider_http_calls"] == 0
    assert HOST_TOKEN not in message.text
    assert "sk-should-never-leak" not in message.text

    # Browsers often omit Origin on same-origin GET; Referer must still pass.
    pending = demo_client.get(
        "/demo/pending-action",
        headers={
            "Accept": "application/json",
            "Referer": ORIGIN + "/",
            "Sec-Fetch-Site": "same-origin",
            "Host": "testserver",
        },
    )
    assert pending.status_code == 200
    card = pending.json()
    assert card["action_type"] == "CANCEL_ORDER"
    assert card["order_id"] == "ORD-1001"
    assert card["executed"] is False
    assert "尚未执行" in card["note"]
    serialized = json.dumps(card)
    assert "approval_id" not in serialized
    assert "preview_hash" not in serialized
    assert "access_token" not in serialized
    assert HOST_TOKEN not in serialized

    presented = demo_client.post(
        "/demo/pending-action/presented",
        headers=_json_headers(csrf),
        json={},
    )
    assert presented.status_code == 200
    assert presented.json()["status"] == "PRESENTED"

    confirmed = demo_client.post(
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
    assert HOST_TOKEN not in confirmed.text

    cookie = demo_client.cookies.get(DEMO_COOKIE_NAME_LOCAL)
    demo = demo_app.state.demo_sessions.get(cookie)
    with demo.database.session() as session:
        order = session.get(Order, "ORD-1001")
        assert order is not None
        assert order.status == "CANCELLED"
        assert session.scalar(select(func.count()).select_from(ActionExecution)) == 1
        assert session.scalar(select(func.count()).select_from(ConfirmationEvent)) == 1


def test_reset_rotates_session_and_restores_seed(demo_client: TestClient, demo_app):
    csrf = _start_session(demo_client)
    demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "取消订单 ORD-1001"},
    )
    demo_client.post(
        "/demo/pending-action/presented",
        headers=_json_headers(csrf),
        json={},
    )
    demo_client.post(
        "/demo/pending-action/confirm",
        headers=_json_headers(csrf),
        json={},
    )
    old_cookie = demo_client.cookies.get(DEMO_COOKIE_NAME_LOCAL)

    reset = demo_client.post("/demo/reset", headers=_json_headers(csrf), json={})
    assert reset.status_code == 200
    new_csrf = reset.json()["csrf_token"]
    assert new_csrf != csrf
    new_cookie = demo_client.cookies.get(DEMO_COOKIE_NAME_LOCAL)
    assert new_cookie
    assert new_cookie != old_cookie

    pending = demo_client.get(
        "/demo/pending-action",
        headers={"Origin": ORIGIN, "Accept": "application/json"},
    )
    assert pending.status_code == 404

    demo = demo_app.state.demo_sessions.get(new_cookie)
    with demo.database.session() as session:
        order = session.get(Order, "ORD-1001")
        assert order is not None
        assert order.status == "PAID"
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(ActionExecution)) == 0


def test_sessions_are_isolated(demo_client: TestClient):
    csrf_a = _start_session(demo_client)
    demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf_a),
        json={"message": "取消订单 ORD-1001"},
    )
    cookie_a = demo_client.cookies.get(DEMO_COOKIE_NAME_LOCAL)

    with TestClient(demo_client.app, base_url=ORIGIN) as other:
        csrf_b = _start_session(other)
        pending_b = other.get(
            "/demo/pending-action",
            headers={"Origin": ORIGIN, "Accept": "application/json"},
        )
        assert pending_b.status_code == 404
        confirm_b = other.post(
            "/demo/pending-action/confirm",
            headers=_json_headers(csrf_b),
            json={},
        )
        assert confirm_b.status_code == 404
        assert cookie_a != other.cookies.get(DEMO_COOKIE_NAME_LOCAL)


def test_unsupported_input_stays_offline(demo_client: TestClient):
    csrf = _start_session(demo_client)
    response = demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "请用 DeepSeek 帮我随便写点什么"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_pending_action"] is False
    assert body["provider_http_calls"] == 0
    assert "不会调用在线模型" in body["reply"]


def test_obvious_pii_is_blocked(demo_client: TestClient):
    csrf = _start_session(demo_client)
    response = demo_client.post(
        "/demo/messages",
        headers=_json_headers(csrf),
        json={"message": "我的邮箱是 real.person@gmail.com 请处理"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DEMO_PII_BLOCKED"


def test_cookie_flags_for_secure_mode():
    app = create_app(
        settings=Settings(
            app_mode=APP_MODE_PUBLIC_DEMO,
            demo_agent_mode="offline_replay",
            demo_allowed_origin="https://testserver",
            demo_cookie_secure=True,
            host_confirmation_token=HOST_TOKEN,
        ),
        seed_demo=False,
    )
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/demo/session",
            headers={
                "Origin": "https://testserver",
                "Content-Type": "application/json",
            },
            json={},
        )
        assert response.status_code == 200
        set_cookie = response.headers.get("set-cookie", "")
        assert "__Host-rivet_demo=" in set_cookie
        assert re.search(r";\s*HttpOnly", set_cookie, re.I)
        assert re.search(r";\s*Secure", set_cookie, re.I)
        assert re.search(r";\s*Path=/", set_cookie, re.I)
        assert "Domain=" not in set_cookie
