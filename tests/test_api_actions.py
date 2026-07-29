from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Order


def _prepare(client: TestClient, headers: dict[str, str], payload: dict) -> dict:
    response = client.post("/v1/actions/prepare", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared["status"] == "PREPARED"
    assert len(prepared["preview_hash"]) == 64
    return prepared


def _present(
    client: TestClient,
    headers: dict[str, str],
    prepared: dict,
) -> None:
    response = client.post(
        f"/v1/actions/{prepared['approval_id']}/present",
        headers=headers,
        json={"preview_hash": prepared["preview_hash"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PRESENTED"


def _confirm(
    client: TestClient,
    headers: dict[str, str],
    prepared: dict,
    *,
    ui_event_id: str,
):
    return client.post(
        f"/v1/actions/{prepared['approval_id']}/confirm",
        headers=headers,
        json={
            "preview_hash": prepared["preview_hash"],
            "ui_event_id": ui_event_id,
            "confirmation_source": "BUTTON",
        },
    )


def test_cancel_order_and_idempotent_replay(client, auth_headers, host_headers):
    prepared = _prepare(
        client,
        auth_headers,
        {"action_type": "CANCEL_ORDER", "order_id": "ORD-1001"},
    )
    _present(client, host_headers, prepared)
    first = _confirm(
        client,
        host_headers,
        prepared,
        ui_event_id="ui-cancel-ord-1001",
    )
    replay = _confirm(
        client,
        host_headers,
        prepared,
        ui_event_id="ui-cancel-ord-1001",
    )

    assert first.status_code == 200
    assert first.json()["result"]["final_order_status"] == "CANCELLED"
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["execution_id"] == first.json()["execution_id"]


def test_write_requires_presented_host_confirmation(
    client,
    auth_headers,
    host_headers,
):
    prepared = _prepare(
        client,
        auth_headers,
        {"action_type": "CANCEL_ORDER", "order_id": "ORD-1001"},
    )
    response = _confirm(
        client,
        host_headers,
        prepared,
        ui_event_id="ui-cancel-before-present",
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_NOT_PRESENTED"


def test_shipped_order_cannot_be_cancelled(client, auth_headers):
    response = client.post(
        "/v1/actions/prepare",
        headers=auth_headers,
        json={"action_type": "CANCEL_ORDER", "order_id": "ORD-1002"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ORDER_ALREADY_SHIPPED"


def test_exchange_reserves_inventory(client, auth_headers, host_headers):
    before = client.get(
        "/v1/inventory",
        headers=auth_headers,
        params={"sku": "GAT-WHITE", "size": "43"},
    ).json()["available_qty"]

    prepared = _prepare(
        client,
        auth_headers,
        {
            "action_type": "EXCHANGE_ITEM",
            "order_id": "ORD-1003",
            "order_item_id": "ITEM-1003-A",
            "target_size": "43",
            "declared_condition": "NEW_UNWORN",
            "issue_type": "SIZE_MISMATCH",
        },
    )
    _present(client, host_headers, prepared)
    response = _confirm(
        client,
        host_headers,
        prepared,
        ui_event_id="ui-exchange-ord-1003",
    )
    after = client.get(
        "/v1/inventory",
        headers=auth_headers,
        params={"sku": "GAT-WHITE", "size": "43"},
    ).json()["available_qty"]

    assert response.status_code == 200
    assert response.json()["result"]["outcome"] == "EXCHANGE_REQUEST_CREATED"
    assert after == before - 1


def test_exchange_out_of_stock_is_rejected(client, auth_headers):
    response = client.post(
        "/v1/actions/eligibility",
        headers=auth_headers,
        json={
            "action_type": "EXCHANGE_ITEM",
            "order_id": "ORD-1003",
            "order_item_id": "ITEM-1003-A",
            "target_size": "44",
            "declared_condition": "NEW_UNWORN",
            "issue_type": "SIZE_MISMATCH",
        },
    )
    assert response.status_code == 200
    assert response.json()["allowed"] is False
    assert response.json()["reason_code"] == "OUT_OF_STOCK"


def test_return_expired_and_final_sale_are_rejected(client, auth_headers):
    expired = client.post(
        "/v1/actions/eligibility",
        headers=auth_headers,
        json={
            "action_type": "RETURN_ITEM",
            "order_id": "ORD-1004",
            "order_item_id": "ITEM-1004-A",
        },
    )
    final_sale = client.post(
        "/v1/actions/eligibility",
        headers=auth_headers,
        json={
            "action_type": "RETURN_ITEM",
            "order_id": "ORD-1005",
            "order_item_id": "ITEM-1005-A",
        },
    )
    assert expired.json()["reason_code"] == "RETURN_WINDOW_EXPIRED"
    assert final_sale.json()["reason_code"] == "FINAL_SALE"


def test_cross_customer_order_is_not_disclosed(client, auth_headers):
    response = client.get("/v1/orders/ORD-2001", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_stale_approval_is_blocked(client, auth_headers, host_headers, app):
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

    response = _confirm(
        client,
        host_headers,
        prepared,
        ui_event_id="ui-cancel-stale-approval",
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_APPROVAL"
