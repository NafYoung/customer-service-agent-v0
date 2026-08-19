"""DecisionSnapshot audit rows are written atomically with each execution."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.rules import RULES_VERSION
from app.models import DecisionSnapshot


def _prepare(client: TestClient, headers: dict[str, str], payload: dict) -> dict:
    response = client.post("/v1/actions/prepare", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


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


def _snapshots(client: TestClient) -> list[DecisionSnapshot]:
    with client.app.state.database.session() as db:
        return list(db.scalars(select(DecisionSnapshot)).all())


def test_execute_writes_decision_snapshot_without_secrets(
    client: TestClient,
    auth_headers: dict[str, str],
    host_headers: dict[str, str],
):
    prepared = _prepare(
        client,
        auth_headers,
        {"action_type": "CANCEL_ORDER", "order_id": "ORD-1001"},
    )
    _present(client, host_headers, prepared)
    executed = _confirm(
        client,
        host_headers,
        prepared,
        ui_event_id="ui-decision-snapshot-0001",
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["idempotent_replay"] is False

    snapshots = _snapshots(client)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.id.startswith("DEC-")
    assert snapshot.approval_id == prepared["approval_id"]
    assert snapshot.execution_id == executed.json()["execution_id"]
    assert snapshot.confirmation_event_id is not None
    assert snapshot.order_id == "ORD-1001"
    assert snapshot.action_type == "CANCEL_ORDER"
    assert snapshot.confirmation_source == "BUTTON"
    assert snapshot.rule_version == RULES_VERSION
    assert snapshot.policy_versions["POL-CANCEL-001"] == "v0.1"
    assert snapshot.policy_versions["POL-RETURN-001"] == "v0.1"
    assert snapshot.eligibility_inputs["order_id"] == "ORD-1001"
    assert "user_note" not in snapshot.eligibility_inputs
    assert snapshot.eligibility_decision["allowed"] is True
    assert snapshot.model_cost_cny is None

    blob = json.dumps(
        {
            **snapshot.eligibility_inputs,
            **snapshot.eligibility_decision,
            **snapshot.policy_versions,
        },
        ensure_ascii=False,
    )
    for secret_marker in ("access_token", "verification_code", "sk-"):
        assert secret_marker not in blob


def test_idempotent_replay_does_not_write_second_snapshot(
    client: TestClient,
    auth_headers: dict[str, str],
    host_headers: dict[str, str],
):
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
        ui_event_id="ui-decision-replay-0001",
    )
    assert first.status_code == 200
    replay = _confirm(
        client,
        host_headers,
        prepared,
        ui_event_id="ui-decision-replay-0001",
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    snapshots = _snapshots(client)
    assert len(snapshots) == 1
    assert snapshots[0].execution_id == first.json()["execution_id"]
