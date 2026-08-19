"""Demo handoff: host reject / session limits / budget exhaustion → SupportTicket.

The Agent keeps its exact 9-tool allowlist; handoff tickets are created by the
host (demo runtime), never by a model tool call.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.openai_compatible import ModelAPIError
from app.config import Settings
from app.demo import (
    APP_MODE_PUBLIC_DEMO,
    DEMO_AGENT_MODE_PREPARATION_LIVE,
    DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
)
from app.demo.live_runner import run_preparation_live
from app.demo.security import DEMO_COOKIE_NAME_LOCAL
from app.demo.session import DemoSessionManager
from app.errors import ServiceError
from app.main import create_app
from app.models import SupportTicket

ORIGIN = "http://testserver"
HOST_TOKEN = "demo-handoff-host-token"


def _json_headers(csrf: str | None = None) -> dict[str, str]:
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers


def _scripted_app(**overrides: object):
    return create_app(
        settings=Settings(
            app_mode=APP_MODE_PUBLIC_DEMO,
            demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
            demo_allowed_origin=ORIGIN,
            demo_cookie_secure=False,
            host_confirmation_token=HOST_TOKEN,
            deepseek_api_key="sk-should-never-leak-or-be-used",
            enable_debug_routes=False,
            **overrides,
        ),
        seed_demo=False,
    )


def _start_session(client: TestClient) -> dict:
    response = client.post("/demo/session", headers=_json_headers(), json={})
    assert response.status_code == 200, response.text
    return response.json()


def _tickets(demo) -> list[SupportTicket]:
    with demo.database.session() as db:
        return list(db.scalars(select(SupportTicket)).all())


def test_reject_creates_handoff_ticket():
    app = _scripted_app()
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = _start_session(client)["csrf_token"]
        prepared = client.post(
            "/demo/messages",
            headers=_json_headers(csrf),
            json={"message": "取消订单 ORD-1001"},
        )
        assert prepared.status_code == 200, prepared.text
        presented = client.post(
            "/demo/pending-action/presented",
            headers=_json_headers(csrf),
            json={},
        )
        assert presented.status_code == 200
        rejected = client.post(
            "/demo/pending-action/reject",
            headers=_json_headers(csrf),
            json={},
        )
        assert rejected.status_code == 200, rejected.text
        payload = rejected.json()
        assert payload["status"] == "REJECTED"
        ticket_id = payload["handoff_ticket_id"]
        assert isinstance(ticket_id, str) and ticket_id.startswith("TKT-")
        assert ticket_id in payload["message"]

        demo = app.state.demo_sessions.get(client.cookies.get(DEMO_COOKIE_NAME_LOCAL))
        tickets = _tickets(demo)
        assert len(tickets) == 1
        assert tickets[0].id == ticket_id
        assert tickets[0].category == "HOST_REJECT"
        assert tickets[0].order_id == "ORD-1001"
        assert tickets[0].customer_id == demo.customer_id
        assert demo.handoff_reasons == {"host_reject"}


def test_message_limit_creates_one_deduped_ticket():
    app = _scripted_app(demo_max_messages_per_session=2)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = _start_session(client)["csrf_token"]
        for _ in range(2):
            response = client.post(
                "/demo/messages",
                headers=_json_headers(csrf),
                json={"message": "查一下我的订单"},
            )
            assert response.status_code == 200, response.text

        limited = client.post(
            "/demo/messages",
            headers=_json_headers(csrf),
            json={"message": "查一下我的订单"},
        )
        assert limited.status_code == 429
        assert "TKT-" in limited.json()["error"]["message"]

        demo = app.state.demo_sessions.get(client.cookies.get(DEMO_COOKIE_NAME_LOCAL))
        assert len(_tickets(demo)) == 1
        assert _tickets(demo)[0].category == "SESSION_LIMIT"

        limited_again = client.post(
            "/demo/messages",
            headers=_json_headers(csrf),
            json={"message": "查一下我的订单"},
        )
        assert limited_again.status_code == 429
        assert "TKT-" not in limited_again.json()["error"]["message"]
        assert len(_tickets(demo)) == 1


def test_prepare_limit_creates_session_limit_ticket_after_reject_ticket():
    app = _scripted_app(
        demo_max_messages_per_session=30,
        demo_max_prepare_per_session=1,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = _start_session(client)["csrf_token"]
        prepared = client.post(
            "/demo/messages",
            headers=_json_headers(csrf),
            json={"message": "取消订单 ORD-1001"},
        )
        assert prepared.status_code == 200 and prepared.json()["has_pending_action"]
        presented = client.post(
            "/demo/pending-action/presented",
            headers=_json_headers(csrf),
            json={},
        )
        assert presented.status_code == 200
        rejected = client.post(
            "/demo/pending-action/reject",
            headers=_json_headers(csrf),
            json={},
        )
        assert rejected.status_code == 200

        limited = client.post(
            "/demo/messages",
            headers=_json_headers(csrf),
            json={"message": "取消订单 ORD-1001"},
        )
        assert limited.status_code == 429
        assert "TKT-" in limited.json()["error"]["message"]

        demo = app.state.demo_sessions.get(client.cookies.get(DEMO_COOKIE_NAME_LOCAL))
        tickets = _tickets(demo)
        assert len(tickets) == 2
        assert {ticket.category for ticket in tickets} == {
            "HOST_REJECT",
            "SESSION_LIMIT",
        }
        assert demo.handoff_reasons == {"host_reject", "prepare_limit"}


class _ExplodingAgent:
    def run(self, *args, **kwargs):
        raise ModelAPIError(
            "MODEL_BUDGET_EXHAUSTED",
            "budget exhausted",
            error_stage="reserve_attempt",
        )


class _FakeModel:
    def close(self) -> None:
        pass


def test_live_budget_exhaustion_creates_handoff_ticket():
    settings = Settings(
        app_mode="local",
        demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_LIVE,
        deepseek_api_key="sk-local-test-key",
        demo_allowed_origin=ORIGIN,
        demo_cookie_secure=False,
        host_confirmation_token=HOST_TOKEN,
    )
    manager = DemoSessionManager(settings)
    _, demo = manager.create()
    try:
        with (
            patch(
                "app.demo.live_runner._live_budget_guard",
                return_value=None,
            ),
            patch(
                "app.demo.live_runner.build_deepseek_client",
                return_value=_FakeModel(),
            ),
            patch(
                "app.demo.live_runner.build_preparation_agent",
                return_value=_ExplodingAgent(),
            ),
        ):
            with pytest.raises(ServiceError) as exc_info:
                run_preparation_live(demo, message="取消订单 ORD-1001")
        assert exc_info.value.code == "DEMO_LIVE_BUDGET_EXHAUSTED"
        assert exc_info.value.status_code == 409
        tickets = _tickets(demo)
        assert len(tickets) == 1
        assert tickets[0].category == "BUDGET_EXHAUSTED"
        assert demo.handoff_reasons == {"budget_exhausted"}
    finally:
        manager.dispose_all()


def test_non_budget_live_failure_does_not_create_ticket():
    settings = Settings(
        app_mode="local",
        demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_LIVE,
        deepseek_api_key="sk-local-test-key",
        demo_allowed_origin=ORIGIN,
        demo_cookie_secure=False,
        host_confirmation_token=HOST_TOKEN,
    )
    manager = DemoSessionManager(settings)
    _, demo = manager.create()
    try:
        class _HttpErrorAgent:
            def run(self, *args, **kwargs):
                raise ModelAPIError(
                    "MODEL_HTTP_ERROR",
                    "provider down",
                    error_stage="provider_attempt",
                )

        with (
            patch(
                "app.demo.live_runner._live_budget_guard",
                return_value=None,
            ),
            patch(
                "app.demo.live_runner.build_deepseek_client",
                return_value=_FakeModel(),
            ),
            patch(
                "app.demo.live_runner.build_preparation_agent",
                return_value=_HttpErrorAgent(),
            ),
        ):
            with pytest.raises(ServiceError) as exc_info:
                run_preparation_live(demo, message="取消订单 ORD-1001")
        assert exc_info.value.code == "DEMO_LIVE_AGENT_FAILED"
        assert _tickets(demo) == []
        assert demo.handoff_reasons == set()
    finally:
        manager.dispose_all()
