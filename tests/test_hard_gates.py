"""Hard gates: state-machine request gate and DB partial-unique backstops."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.domain.state_machine import assert_request_allowed_from_order_status
from app.enums import OrderStatus, RequestStatus
from app.errors import ConflictError
from app.models import ReturnRequest


def test_request_gate_allows_only_delivered_orders():
    assert_request_allowed_from_order_status(OrderStatus.DELIVERED)
    for blocked in (
        OrderStatus.PAID,
        OrderStatus.PROCESSING,
        OrderStatus.SHIPPED,
        OrderStatus.CANCELLED,
    ):
        with pytest.raises(ConflictError, match="不允许发起退货或换货"):
            assert_request_allowed_from_order_status(blocked)


def _prepare(client: TestClient, headers: dict[str, str], payload: dict) -> dict:
    response = client.post("/v1/actions/prepare", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _present_and_confirm(
    client: TestClient,
    host_headers: dict[str, str],
    prepared: dict,
    *,
    ui_event_id: str,
) -> None:
    presented = client.post(
        f"/v1/actions/{prepared['approval_id']}/present",
        headers=host_headers,
        json={"preview_hash": prepared["preview_hash"]},
    )
    assert presented.status_code == 200, presented.text
    confirmed = client.post(
        f"/v1/actions/{prepared['approval_id']}/confirm",
        headers=host_headers,
        json={
            "preview_hash": prepared["preview_hash"],
            "ui_event_id": ui_event_id,
            "confirmation_source": "BUTTON",
        },
    )
    assert confirmed.status_code == 200, confirmed.text


def test_partial_unique_index_blocks_second_active_return(
    client: TestClient,
    auth_headers: dict[str, str],
    host_headers: dict[str, str],
):
    prepared = _prepare(
        client,
        auth_headers,
        {
            "action_type": "RETURN_ITEM",
            "order_id": "ORD-1003",
            "order_item_id": "ITEM-1003-A",
            "declared_condition": "NEW_UNWORN",
            "issue_type": "CHANGED_MIND",
        },
    )
    _present_and_confirm(
        client,
        host_headers,
        prepared,
        ui_event_id="ui-return-partial-unique-0001",
    )

    with client.app.state.database.session() as db:
        existing = db.scalars(select(ReturnRequest)).all()
        assert len(existing) == 1
        duplicate = ReturnRequest(
            id="RET-DUPLICATE-0001",
            customer_id=existing[0].customer_id,
            order_id="ORD-1003",
            order_item_id="ITEM-1003-A",
            reason="CHANGED_MIND",
            declared_condition="NEW_UNWORN",
            status=RequestStatus.REQUESTED.value,
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
