from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

HOST_CONFIRMATION_TOKEN = "pytest-host-confirmation-token"
DEBUG_ADMIN_TOKEN = "pytest-debug-admin-token"
CONVERSATION_ID = "pytest-conversation"


@pytest.fixture()
def app():
    return create_app(
        settings=Settings(
            database_url="sqlite:///:memory:",
            host_confirmation_token=HOST_CONFIRMATION_TOKEN,
        )
    )


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/v1/auth/verify",
        headers={"X-Run-ID": "pytest-run"},
        json={
            "email": "linfan@example.com",
            "verification_code": "246810",
        },
    )
    assert response.status_code == 200
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Run-ID": "pytest-run",
        "X-Conversation-ID": CONVERSATION_ID,
    }


@pytest.fixture()
def host_headers(auth_headers: dict[str, str]) -> dict[str, str]:
    return {
        **auth_headers,
        "X-Host-Confirmation-Token": HOST_CONFIRMATION_TOKEN,
    }


@pytest.fixture()
def debug_app():
    return create_app(
        settings=Settings(
            database_url="sqlite:///:memory:",
            host_confirmation_token=HOST_CONFIRMATION_TOKEN,
            enable_debug_routes=True,
            debug_admin_token=DEBUG_ADMIN_TOKEN,
        )
    )


@pytest.fixture()
def debug_client(debug_app):
    with TestClient(debug_app) as test_client:
        yield test_client


@pytest.fixture()
def debug_auth_headers(debug_client: TestClient) -> dict[str, str]:
    response = debug_client.post(
        "/v1/auth/verify",
        headers={"X-Run-ID": "pytest-debug-run"},
        json={
            "email": "linfan@example.com",
            "verification_code": "246810",
        },
    )
    assert response.status_code == 200
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Run-ID": "pytest-debug-run",
        "X-Conversation-ID": "pytest-debug-conversation",
    }


@pytest.fixture()
def debug_admin_headers() -> dict[str, str]:
    return {"X-Debug-Admin-Token": DEBUG_ADMIN_TOKEN}
