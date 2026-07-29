from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from app.models import ToolEvent
from app.services.policies import PolicyService


def test_policy_search_returns_citable_metadata(client):
    response = client.post(
        "/v1/policies/search",
        json={"query": "鞋子尺码小了，想换大一码"},
    )
    assert response.status_code == 200
    hits = response.json()["hits"]
    assert hits
    assert hits[0]["policy_id"] == "POL-EXCHANGE-001"
    assert hits[0]["version"] == "v0.1"


def test_policy_service_freezes_index_and_body_at_construction(
    tmp_path: Path,
) -> None:
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    body_path = policy_dir / "returns.md"
    body_path.write_text(
        "# 退货政策\n原始政策正文：退货申请需要人工复核。\n",
        encoding="utf-8",
    )
    (policy_dir / "index.json").write_text(
        json.dumps(
            [
                {
                    "policy_id": "POL-RETURN-TEST",
                    "title": "退货政策",
                    "version": "v-test",
                    "effective_date": "2026-07-29",
                    "region": "CN",
                    "channel": "ONLINE",
                    "keywords": ["退货"],
                    "file": "returns.md",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = PolicyService(policy_dir)
    body_path.write_text(
        "# 退货政策\n篡改后的政策正文：无需复核。\n",
        encoding="utf-8",
    )

    response = service.search(query="退货")

    assert response.hits[0].excerpt == "原始政策正文：退货申请需要人工复核。"


def test_defect_is_handed_off(client, auth_headers):
    eligibility = client.post(
        "/v1/actions/eligibility",
        headers=auth_headers,
        json={
            "action_type": "RETURN_ITEM",
            "order_id": "ORD-1003",
            "order_item_id": "ITEM-1003-A",
            "declared_condition": "DAMAGED",
            "issue_type": "DEFECTIVE",
        },
    )
    assert eligibility.json()["reason_code"] == "HUMAN_REVIEW_REQUIRED"

    ticket = client.post(
        "/v1/tickets",
        headers=auth_headers,
        json={
            "order_id": "ORD-1003",
            "category": "DEFECTIVE_ITEM",
            "summary": "鞋底出现开胶，申请人工核验。",
            "priority": "HIGH",
        },
    )
    assert ticket.status_code == 200
    assert ticket.json()["status"] == "OPEN"


def test_authentication_is_not_recorded_as_an_agent_tool_event(client, app):
    response = client.post(
        "/v1/auth/verify",
        headers={"X-Run-ID": "redaction-test"},
        json={
            "email": "linfan@example.com",
            "verification_code": "246810",
        },
    )
    assert response.status_code == 200

    with app.state.database.session() as session:
        auth_tool_events = session.scalar(
            select(func.count())
            .select_from(ToolEvent)
            .where(ToolEvent.tool_name == "authenticate_customer")
        )
    assert auth_tool_events == 0


def test_failed_tool_call_is_visible_in_safe_admin_trace(
    debug_client,
    debug_auth_headers,
    debug_admin_headers,
):
    response = debug_client.post(
        "/v1/actions/prepare",
        headers=debug_auth_headers,
        json={"action_type": "CANCEL_ORDER", "order_id": "ORD-1002"},
    )
    assert response.status_code == 400

    events_response = debug_client.get(
        "/v1/debug/tool-events",
        headers=debug_admin_headers,
        params={"run_id": "pytest-debug-run", "limit": 10},
    )
    assert events_response.status_code == 200, events_response.text
    events = events_response.json()
    matching = [
        event
        for event in events
        if event["tool_name"] == "prepare_action" and event["success"] is False
    ]
    assert matching
    assert matching[0]["error_code"] == "ORDER_ALREADY_SHIPPED"


def test_shipment_tool_returns_structured_record(client, auth_headers):
    response = client.get("/v1/orders/ORD-1002/shipment", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["tracking_number"] == "DEMO10020001"
    assert response.json()["status"] == "IN_TRANSIT"


def test_exported_agent_tool_contracts_are_unique_and_closed():
    from app.tools.contracts import get_tool_contracts

    contracts = get_tool_contracts()
    names = [contract["name"] for contract in contracts]
    expected_names = {
        "get_customer_orders",
        "get_order",
        "get_shipment",
        "get_inventory",
        "search_policy",
        "check_action_eligibility",
        "prepare_cancel_order",
        "prepare_return",
        "prepare_exchange",
        "create_handoff_ticket",
    }
    assert len(names) == len(set(names))
    assert set(names) == expected_names
    assert "authenticate_customer" not in names
    assert "execute_prepared_action" not in names
    for contract in contracts:
        assert contract["input_schema"].get("additionalProperties") is False
