from __future__ import annotations

import json
from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agent.openai_compatible import AssistantTurn, ToolCall
from app.config import Settings
from app.main import create_app
from app.models import (
    ActionExecution,
    Approval,
    ConfirmationEvent,
    Order,
    SupportTicket,
)


HOST_TOKEN = "pytest-pilot-host-token"


class ScriptedModel:
    def __init__(self, *turns: AssistantTurn):
        self.turns = list(turns)
        self.calls: list[dict[str, object]] = []

    def complete(self, *, messages, tools):
        self.calls.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
            }
        )
        if not self.turns:
            raise AssertionError("scripted model ran out of turns")
        return self.turns.pop(0)


class QueueModelFactory:
    def __init__(self, *models: ScriptedModel):
        self.models = list(models)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.models:
            raise AssertionError("model factory must not be called again")
        return self.models.pop(0)


def _tool_turn(name: str, arguments: str, *, call_id: str) -> AssistantTurn:
    return AssistantTurn(
        content=None,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        finish_reason="tool_calls",
        usage=None,
    )


def _final_turn(content: str) -> AssistantTurn:
    return AssistantTurn(
        content=content,
        tool_calls=(),
        finish_reason="stop",
        usage=None,
    )


def _build_app(factory=None):
    return create_app(
        settings=Settings(
            database_url="sqlite:///:memory:",
            host_confirmation_token=HOST_TOKEN,
        ),
        preparation_model_factory=factory,
    )


def _authenticate(client: TestClient, *, email: str = "linfan@example.com") -> str:
    response = client.post(
        "/v1/auth/verify",
        json={"email": email, "verification_code": "246810"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _host_headers(token: str, *, conversation_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Conversation-ID": conversation_id,
        "X-Host-Confirmation-Token": HOST_TOKEN,
    }


def test_host_cancel_flow_uses_canonical_card_and_empty_body_confirmation():
    model = ScriptedModel(
        _tool_turn(
            "prepare_cancel_order",
            '{"order_id":"ORD-1001","user_note":"不再需要"}',
            call_id="call-host-cancel",
        ),
        _final_turn("已生成待确认操作。"),
    )
    factory = QueueModelFactory(model)
    app = _build_app(factory)

    with TestClient(app) as client:
        token = _authenticate(client)
        headers = _host_headers(token, conversation_id="pilot-cancel")
        message = client.post(
            "/v1/host/messages",
            headers=headers,
            json={"text": "请取消 ORD-1001"},
        )

        assert message.status_code == 200, message.text
        payload = message.json()
        assert payload["mode"] == "AGENT"
        assert payload["pending_approval_id"]
        assert payload["handoff"] is None
        assert "尚未执行" in payload["safety_notice"]
        assert "preview_hash" not in json.dumps(payload)
        approval_id = payload["pending_approval_id"]

        with app.state.database.session() as session:
            order = session.get(Order, "ORD-1001")
            approval = session.get(Approval, approval_id)
            assert order is not None and order.status == "PAID"
            assert approval is not None and approval.status == "PREPARED"
            assert session.scalar(select(func.count()).select_from(ActionExecution)) == 0

        presented = client.post(
            f"/v1/host/approvals/{approval_id}/present",
            headers=headers,
        )
        assert presented.status_code == 200, presented.text
        card = presented.json()
        assert card["status"] == "PRESENTED"
        assert card["preview"]["order_id"] == "ORD-1001"
        assert "preview_hash" not in card

        rejected_body = client.post(
            f"/v1/host/approvals/{approval_id}/confirm",
            headers=headers,
            json={},
        )
        assert rejected_body.status_code == 422
        assert rejected_body.json()["error"]["code"] == "HOST_BODY_MUST_BE_EMPTY"

        first = client.post(
            f"/v1/host/approvals/{approval_id}/confirm",
            headers=headers,
        )
        replay = client.post(
            f"/v1/host/approvals/{approval_id}/confirm",
            headers=headers,
        )
        assert first.status_code == 200, first.text
        assert first.json()["result"]["final_order_status"] == "CANCELLED"
        assert first.json()["idempotent_replay"] is False
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["execution_id"] == first.json()["execution_id"]

        with app.state.database.session() as session:
            confirmation = session.scalar(
                select(ConfirmationEvent).where(
                    ConfirmationEvent.approval_id == approval_id
                )
            )
            assert confirmation is not None
            assert confirmation.ui_event_id == f"host-button:{approval_id}"

    exposed = json.dumps(model.calls, ensure_ascii=False)
    assert token not in exposed
    assert HOST_TOKEN not in exposed
    assert "pilot-cancel" not in exposed
    assert "customer_id" not in exposed
    assert factory.calls == 1


def test_human_review_trace_enters_manual_mode_and_stops_future_model_calls():
    model = ScriptedModel(
        _tool_turn(
            "check_action_eligibility",
            (
                '{"action_type":"RETURN_ITEM","order_id":"ORD-1003",'
                '"order_item_id":"ITEM-1003-A","declared_condition":"DAMAGED",'
                '"issue_type":"DEFECTIVE"}'
            ),
            call_id="call-human-review",
        ),
        _final_turn("这个情况需要进一步处理。"),
    )
    factory = QueueModelFactory(model)
    app = _build_app(factory)

    with TestClient(app) as client:
        token = _authenticate(client)
        headers = _host_headers(token, conversation_id="pilot-handoff")
        first = client.post(
            "/v1/host/messages",
            headers=headers,
            json={"text": "ORD-1003 的鞋穿了一次就开胶了"},
        )
        assert first.status_code == 200, first.text
        response = first.json()
        assert response["mode"] == "MANUAL"
        assert response["pending_approval_id"] is None
        assert response["handoff"]["transfer_reason"] == "DEFECTIVE_ITEM"
        assert response["handoff"]["status"] == "OPEN"
        assert "人工" in response["safety_notice"]

        second = client.post(
            "/v1/host/messages",
            headers=headers,
            json={"text": "现在进展如何？"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["mode"] == "MANUAL"
        assert second.json()["handoff"]["id"] == response["handoff"]["id"]

        with app.state.database.session() as session:
            tickets = session.scalars(select(SupportTicket)).all()
            assert len(tickets) == 1
            assert tickets[0].conversation_id == "pilot-handoff"
            assert tickets[0].origin_server_run_id == response["server_run_id"]
            assert tickets[0].transfer_reason == "DEFECTIVE_ITEM"
            assert session.scalar(select(func.count()).select_from(Approval)) == 0
            assert session.scalar(select(func.count()).select_from(ActionExecution)) == 0

    assert factory.calls == 1


def test_human_review_wins_over_later_prepare_without_leaving_an_approval():
    model = ScriptedModel(
        _tool_turn(
            "check_action_eligibility",
            (
                '{"action_type":"RETURN_ITEM","order_id":"ORD-1003",'
                '"order_item_id":"ITEM-1003-A","declared_condition":"DAMAGED",'
                '"issue_type":"DEFECTIVE"}'
            ),
            call_id="call-human-review-before-prepare",
        ),
        _tool_turn(
            "prepare_cancel_order",
            '{"order_id":"ORD-1001","user_note":"错误的后续提议"}',
            call_id="call-prepare-after-human-review",
        ),
        _final_turn("已为另一个订单生成待确认操作。"),
    )
    factory = QueueModelFactory(model)
    app = _build_app(factory)

    with TestClient(app) as client:
        token = _authenticate(client)
        response = client.post(
            "/v1/host/messages",
            headers=_host_headers(token, conversation_id="pilot-handoff-wins"),
            json={"text": "先处理开胶问题，再取消另一张订单"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "MANUAL"
        assert response.json()["pending_approval_id"] is None

        with app.state.database.session() as session:
            assert session.scalar(select(func.count()).select_from(SupportTicket)) == 1
            assert session.scalar(select(func.count()).select_from(Approval)) == 0
            assert session.scalar(select(func.count()).select_from(ConfirmationEvent)) == 0
            assert session.scalar(select(func.count()).select_from(ActionExecution)) == 0

    assert factory.calls == 1


def test_explicit_handoff_is_idempotent_and_internal_fields_are_forbidden():
    factory = QueueModelFactory()
    app = _build_app(factory)
    request_payload = {
        "order_id": "ORD-1003",
        "transfer_reason": "AUTOMATION_UNSAFE",
        "summary": "客户主动要求人工继续处理。",
        "priority": "NORMAL",
    }

    with TestClient(app) as client:
        token = _authenticate(client)
        headers = _host_headers(token, conversation_id="pilot-explicit-handoff")
        first = client.post(
            "/v1/host/handoffs",
            headers=headers,
            json=request_payload,
        )
        replay = client.post(
            "/v1/host/handoffs",
            headers=headers,
            json=request_payload,
        )
        assert first.status_code == 200, first.text
        assert replay.status_code == 200, replay.text
        assert first.json()["idempotent_replay"] is False
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["id"] == first.json()["id"]

        conflict = client.post(
            "/v1/host/handoffs",
            headers=headers,
            json={**request_payload, "summary": "试图覆盖原摘要。"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "HANDOFF_ALREADY_MANUAL"

        injected = client.post(
            "/v1/host/handoffs",
            headers=_host_headers(token, conversation_id="pilot-injected-handoff"),
            json={
                **request_payload,
                "customer_id": "CUST-002",
                "server_run_id": "attacker-run",
                "conversation_id": "attacker-conversation",
            },
        )
        assert injected.status_code == 422

        message = client.post(
            "/v1/host/messages",
            headers=headers,
            json={"text": "不要再让机器人回复"},
        )
        assert message.status_code == 200
        assert message.json()["mode"] == "MANUAL"

        with app.state.database.session() as session:
            assert session.scalar(select(func.count()).select_from(SupportTicket)) == 1

    assert factory.calls == 0


def test_host_routes_require_trusted_host_and_runtime_fails_closed():
    app = _build_app()
    with TestClient(app) as client:
        token = _authenticate(client)
        untrusted_headers = {
            "Authorization": f"Bearer {token}",
            "X-Conversation-ID": "pilot-no-host",
        }
        untrusted = client.post(
            "/v1/host/messages",
            headers=untrusted_headers,
            json={"text": "取消 ORD-1001"},
        )
        assert untrusted.status_code == 401
        assert untrusted.json()["error"]["code"] == "HOST_AUTH_REQUIRED"

        unavailable = client.post(
            "/v1/host/messages",
            headers={**untrusted_headers, "X-Host-Confirmation-Token": HOST_TOKEN},
            json={"text": "取消 ORD-1001"},
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "AGENT_RUNTIME_UNAVAILABLE"


def test_model_execution_claim_cannot_create_business_state():
    model = ScriptedModel(_final_turn("已经为你取消订单。"))
    factory = QueueModelFactory(model)
    app = _build_app(factory)

    with TestClient(app) as client:
        token = _authenticate(client)
        response = client.post(
            "/v1/host/messages",
            headers=_host_headers(token, conversation_id="pilot-false-claim"),
            json={"text": "取消 ORD-1001"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["reply"] == "已经为你取消订单。"
        assert response.json()["pending_approval_id"] is None
        assert "没有执行" in response.json()["safety_notice"]

        with app.state.database.session() as session:
            order = session.get(Order, "ORD-1001")
            assert order is not None and order.status == "PAID"
            assert session.scalar(select(func.count()).select_from(Approval)) == 0
            assert session.scalar(select(func.count()).select_from(ConfirmationEvent)) == 0
            assert session.scalar(select(func.count()).select_from(ActionExecution)) == 0
