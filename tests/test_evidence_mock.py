"""Host-only evidence verification mock: contract, verdicts, ownership."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import ToolEvent
from app.tools.contracts import (
    HOST_TOOL_NAMES,
    PREPARATION_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    get_preparation_tool_contracts,
    get_read_only_tool_contracts,
    get_tool_contracts,
)


def test_host_tool_never_enters_agent_allowlists():
    assert HOST_TOOL_NAMES == ("verify_return_evidence",)
    assert "verify_return_evidence" not in READ_ONLY_TOOL_NAMES
    assert "verify_return_evidence" not in PREPARATION_TOOL_NAMES
    assert {
        contract["name"] for contract in get_read_only_tool_contracts()
    } == set(READ_ONLY_TOOL_NAMES)
    assert {
        contract["name"] for contract in get_preparation_tool_contracts()
    } == set(PREPARATION_TOOL_NAMES)
    assert "verify_return_evidence" in {
        contract["name"] for contract in get_tool_contracts()
    }


def _verify(
    client: TestClient,
    headers: dict[str, str],
    *,
    order_id: str,
    evidence_ref: str,
) -> dict:
    response = client.post(
        "/v1/evidence/verify",
        headers=headers,
        json={
            "order_id": order_id,
            "evidence_kind": "DEFECT_PHOTO",
            "evidence_ref": evidence_ref,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_evidence_verification_returns_deterministic_mock_verdicts(
    client: TestClient,
    auth_headers: dict[str, str],
):
    accepted = _verify(
        client,
        auth_headers,
        order_id="ORD-1003",
        evidence_ref="photo-abc12345",
    )
    assert accepted["verdict"] == "MOCK_ACCEPTED"
    assert accepted["evidence_kind"] == "DEFECT_PHOTO"
    assert "未接入真实 CV" in accepted["note"]

    forged = _verify(
        client,
        auth_headers,
        order_id="ORD-1003",
        evidence_ref="FORGED-photo-0001",
    )
    assert forged["verdict"] == "MOCK_FORGED"

    with client.app.state.database.session() as db:
        events = list(
            db.scalars(
                select(ToolEvent).where(
                    ToolEvent.tool_name == "verify_return_evidence"
                )
            ).all()
        )
    assert len(events) == 2
    for event in events:
        assert event.success is True


def test_evidence_verification_enforces_order_ownership(
    client: TestClient,
    auth_headers: dict[str, str],
):
    response = client.post(
        "/v1/evidence/verify",
        headers=auth_headers,
        json={
            "order_id": "ORD-2001",
            "evidence_kind": "INVOICE",
            "evidence_ref": "invoice-other-customer",
        },
    )
    assert response.status_code == 404
