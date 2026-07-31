from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models import ActionExecution, ConfirmationEvent, Order
from tests.test_api_actions import _confirm, _prepare, _present

HOST_CONFIRMATION_TOKEN = "pytest-concurrency-host-token"
CONVERSATION_ID = "pytest-concurrency-conversation"


@pytest.fixture()
def file_app(tmp_path: Path):
    """File-backed SQLite so concurrent threads share one database.

    Production ``Database`` already sets ``check_same_thread=False`` for all
    sqlite URLs; this fixture only switches off ``:memory:`` so two connections
    see the same file.
    """

    db_path = tmp_path / "concurrency.db"
    return create_app(
        settings=Settings(
            database_url=f"sqlite:///{db_path}",
            host_confirmation_token=HOST_CONFIRMATION_TOKEN,
        )
    )


@pytest.fixture()
def file_client(file_app):
    with TestClient(file_app) as test_client:
        yield test_client


@pytest.fixture()
def file_auth_headers(file_client: TestClient) -> dict[str, str]:
    response = file_client.post(
        "/v1/auth/verify",
        headers={"X-Run-ID": "pytest-concurrency-run"},
        json={
            "email": "linfan@example.com",
            "verification_code": "246810",
        },
    )
    assert response.status_code == 200
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Run-ID": "pytest-concurrency-run",
        "X-Conversation-ID": CONVERSATION_ID,
    }


@pytest.fixture()
def file_host_headers(file_auth_headers: dict[str, str]) -> dict[str, str]:
    return {
        **file_auth_headers,
        "X-Host-Confirmation-Token": HOST_CONFIRMATION_TOKEN,
    }


def test_idempotent_reconfirm_same_ui_event_returns_same_execution_id(
    file_client: TestClient,
    file_auth_headers: dict[str, str],
    file_host_headers: dict[str, str],
    file_app,
):
    prepared = _prepare(
        file_client,
        file_auth_headers,
        {"action_type": "CANCEL_ORDER", "order_id": "ORD-1001"},
    )
    _present(file_client, file_host_headers, prepared)

    first = _confirm(
        file_client,
        file_host_headers,
        prepared,
        ui_event_id="ui-concurrency-idempotent",
    )
    replay = _confirm(
        file_client,
        file_host_headers,
        prepared,
        ui_event_id="ui-concurrency-idempotent",
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["execution_id"] == first.json()["execution_id"]
    assert first.json()["result"]["final_order_status"] == "CANCELLED"

    with file_app.state.database.session() as session:
        confirmations = session.scalar(select(func.count()).select_from(ConfirmationEvent))
        executions = session.scalar(select(func.count()).select_from(ActionExecution))
        order = session.get(Order, "ORD-1001")
        assert confirmations == 1
        assert executions == 1
        assert order is not None
        assert order.status == "CANCELLED"


def test_concurrent_double_confirm_different_ui_events_one_winner(
    file_client: TestClient,
    file_auth_headers: dict[str, str],
    file_host_headers: dict[str, str],
    file_app,
):
    prepared = _prepare(
        file_client,
        file_auth_headers,
        {"action_type": "CANCEL_ORDER", "order_id": "ORD-1001"},
    )
    _present(file_client, file_host_headers, prepared)

    def _confirm_once(ui_event_id: str):
        # Fresh client per thread; same app / file DB.
        with TestClient(file_app) as thread_client:
            return _confirm(
                thread_client,
                file_host_headers,
                prepared,
                ui_event_id=ui_event_id,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_confirm_once, "ui-concurrency-race-a"),
            pool.submit(_confirm_once, "ui-concurrency-race-b"),
        ]
        responses = [future.result(timeout=30) for future in as_completed(futures)]

    statuses = sorted(response.status_code for response in responses)
    winners = [response for response in responses if response.status_code == 200]
    losers = [response for response in responses if response.status_code == 409]

    assert len(winners) == 1, [(r.status_code, r.text) for r in responses]
    assert len(losers) == 1, [(r.status_code, r.text) for r in responses]
    assert statuses == [200, 409]
    assert winners[0].json()["result"]["final_order_status"] == "CANCELLED"
    assert winners[0].json()["idempotent_replay"] is False
    assert losers[0].json()["error"]["code"] == "APPROVAL_ALREADY_CONFIRMED"

    with file_app.state.database.session() as session:
        confirmations = session.scalar(select(func.count()).select_from(ConfirmationEvent))
        executions = session.scalar(select(func.count()).select_from(ActionExecution))
        order = session.get(Order, "ORD-1001")
        assert confirmations == 1
        assert executions == 1
        assert order is not None
        assert order.status == "CANCELLED"
        # Cancel transitions once: seed ORD-1001 starts at PAID version 1 → CANCELLED version 2
        assert order.version == 2
