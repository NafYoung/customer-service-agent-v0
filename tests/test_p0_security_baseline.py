from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models import ActionExecution, Approval, ConfirmationEvent, Order, ToolEvent
from app.schemas import ConfirmActionRequest
from app.tools.contracts import get_tool_contracts

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST_CONFIRMATION_TOKEN = "host-confirmation-test-token"


def test_docker_build_context_excludes_local_env_file():
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(
        encoding="utf-8"
    ).splitlines()
    rules = {
        line.strip()
        for line in dockerignore
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert ".env" in rules
    assert ".env.*" in rules
    assert "!.env.example" in rules


def _create_test_app(**settings_overrides):
    settings = Settings(
        database_url="sqlite:///:memory:",
        host_confirmation_token=HOST_CONFIRMATION_TOKEN,
        **settings_overrides,
    )
    return create_app(settings=settings)


def _authenticate(client: TestClient, email: str) -> str:
    response = client.post(
        "/v1/auth/verify",
        json={"email": email, "verification_code": "246810"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(
    token: str,
    *,
    conversation_id: str,
    run_id: str = "p0-security-run",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Host-Confirmation-Token": HOST_CONFIRMATION_TOKEN,
        "X-Conversation-ID": conversation_id,
        "X-Run-ID": run_id,
    }


def _prepare_cancel(
    client: TestClient,
    headers: dict[str, str],
    *,
    order_id: str = "ORD-1001",
) -> dict:
    response = client.post(
        "/v1/actions/prepare",
        headers=headers,
        json={"action_type": "CANCEL_ORDER", "order_id": order_id},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "PREPARED"
    assert len(payload["preview_hash"]) == 64
    return payload


def _prepare_return(
    client: TestClient,
    headers: dict[str, str],
) -> dict:
    response = client.post(
        "/v1/actions/prepare",
        headers=headers,
        json={
            "action_type": "RETURN_ITEM",
            "order_id": "ORD-1003",
            "order_item_id": "ITEM-1003-A",
            "declared_condition": "NEW_UNWORN",
            "issue_type": "CHANGED_MIND",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "PREPARED"
    assert len(payload["preview_hash"]) == 64
    return payload


def _present(
    client: TestClient,
    headers: dict[str, str],
    prepared: dict,
) -> dict:
    response = client.post(
        f"/v1/actions/{prepared['approval_id']}/present",
        headers=headers,
        json={"preview_hash": prepared["preview_hash"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PRESENTED"
    return response.json()


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


def test_debug_routes_are_disabled_by_default():
    app = _create_test_app()
    with TestClient(app) as client:
        response = client.get(
            "/v1/debug/tool-events",
            params={"run_id": "anything"},
        )
    assert response.status_code == 404


def test_agent_contract_excludes_authentication_and_execution():
    contracts = get_tool_contracts()
    names = {contract["name"] for contract in contracts}
    assert names == {
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
        "verify_return_evidence",
    }
    serialized = repr(contracts)
    for forbidden_field in {
        "verification_code",
        "access_token",
        "auth_token",
        "explicit_confirmation",
        "idempotency_key",
        "ui_event_id",
        "confirmation_source",
    }:
        assert forbidden_field not in serialized


def test_authentication_is_outside_agent_tool_trace():
    app = _create_test_app()
    with TestClient(app) as client:
        _authenticate(client, "linfan@example.com")
        with app.state.database.session() as session:
            event_count = session.scalar(select(func.count()).select_from(ToolEvent))
    assert event_count == 0


def test_confirmation_requires_presented_matching_preview_and_is_idempotent():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        headers = _headers(token, conversation_id="conv-confirm-1")
        prepared = _prepare_cancel(client, headers)

        customer_only_headers = dict(headers)
        customer_only_headers.pop("X-Host-Confirmation-Token")
        untrusted_host = client.post(
            f"/v1/actions/{prepared['approval_id']}/present",
            headers=customer_only_headers,
            json={"preview_hash": prepared["preview_hash"]},
        )
        assert untrusted_host.status_code == 401
        assert untrusted_host.json()["error"]["code"] == "HOST_AUTH_REQUIRED"

        not_presented = _confirm(
            client,
            headers,
            prepared,
            ui_event_id="ui-confirm-too-early",
        )
        assert not_presented.status_code == 409
        assert not_presented.json()["error"]["code"] == "APPROVAL_NOT_PRESENTED"

        mismatched_preview = client.post(
            f"/v1/actions/{prepared['approval_id']}/present",
            headers=headers,
            json={"preview_hash": "0" * 64},
        )
        assert mismatched_preview.status_code == 409
        assert mismatched_preview.json()["error"]["code"] == "PREVIEW_MISMATCH"

        _present(client, headers, prepared)
        first = _confirm(
            client,
            headers,
            prepared,
            ui_event_id="ui-confirm-1",
        )
        replay = _confirm(
            client,
            headers,
            prepared,
            ui_event_id="ui-confirm-1",
        )
        different_event = _confirm(
            client,
            headers,
            prepared,
            ui_event_id="ui-confirm-different-event",
        )
        assert first.status_code == 200, first.text
        assert first.json()["result"]["final_order_status"] == "CANCELLED"
        assert first.json()["idempotent_replay"] is False
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["execution_id"] == first.json()["execution_id"]
        assert different_event.status_code == 409
        assert (
            different_event.json()["error"]["code"]
            == "APPROVAL_ALREADY_CONFIRMED"
        )

        with app.state.database.session() as session:
            confirmation = session.scalar(
                select(ConfirmationEvent).where(
                    ConfirmationEvent.approval_id == prepared["approval_id"]
                )
            )
            confirmation_count = session.scalar(
                select(func.count()).select_from(ConfirmationEvent)
            )
            execution_count = session.scalar(
                select(func.count()).select_from(ActionExecution)
            )
            approval = session.get(Approval, prepared["approval_id"])
            assert confirmation is not None
            assert confirmation.preview_hash == prepared["preview_hash"]
            assert confirmation.consumed_at is not None
            assert confirmation_count == 1
            assert execution_count == 1
            assert approval is not None
            assert approval.status == "EXECUTED"


def test_host_confirmation_models_reject_legacy_execution_fields():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        headers = _headers(token, conversation_id="conv-extra-fields")
        prepared = _prepare_cancel(client, headers)
        _present(client, headers, prepared)
        response = client.post(
            f"/v1/actions/{prepared['approval_id']}/confirm",
            headers=headers,
            json={
                "preview_hash": prepared["preview_hash"],
                "ui_event_id": "ui-extra-fields",
                "confirmation_source": "BUTTON",
                "explicit_confirmation": True,
                "idempotency_key": "attacker-controlled",
            },
        )

    assert response.status_code == 422


def test_prepare_requires_conversation_binding():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        response = client.post(
            "/v1/actions/prepare",
            headers={"Authorization": f"Bearer {token}"},
            json={"action_type": "CANCEL_ORDER", "order_id": "ORD-1001"},
        )

    assert response.status_code == 422


def test_tampered_approval_snapshot_fails_closed():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        headers = _headers(token, conversation_id="conv-integrity")
        prepared = _prepare_cancel(client, headers)

        with app.state.database.session() as session:
            approval = session.get(Approval, prepared["approval_id"])
            assert approval is not None
            approval.preview = {**approval.preview, "effect": "attacker changed effect"}

        response = client.post(
            f"/v1/actions/{prepared['approval_id']}/present",
            headers=headers,
            json={"preview_hash": prepared["preview_hash"]},
        )
        with app.state.database.session() as session:
            execution_count = session.scalar(
                select(func.count()).select_from(ActionExecution)
            )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "APPROVAL_INTEGRITY_ERROR"
    assert execution_count == 0


def test_stale_confirmed_action_is_terminalized_without_business_write():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        headers = _headers(token, conversation_id="conv-stale")
        prepared = _prepare_cancel(client, headers)
        _present(client, headers, prepared)

        with app.state.database.session() as session:
            order = session.get(Order, "ORD-1001")
            assert order is not None
            order.version += 1

        response = _confirm(
            client,
            headers,
            prepared,
            ui_event_id="ui-stale-confirmation",
        )
        with app.state.database.session() as session:
            approval = session.get(Approval, prepared["approval_id"])
            confirmation = session.scalar(
                select(ConfirmationEvent).where(
                    ConfirmationEvent.approval_id == prepared["approval_id"]
                )
            )
            execution_count = session.scalar(
                select(func.count()).select_from(ActionExecution)
            )
            order = session.get(Order, "ORD-1001")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_APPROVAL"
    assert approval is not None and approval.status == "FAILED"
    assert confirmation is not None and confirmation.consumed_at is not None
    assert execution_count == 0
    assert order is not None and order.status == "PAID"


def test_expired_presented_action_is_terminalized_without_confirmation():
    import app.services.actions as action_module

    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        headers = _headers(token, conversation_id="conv-expired")
        prepared = _prepare_cancel(client, headers)
        _present(client, headers, prepared)

        with app.state.database.session() as session:
            approval = session.get(Approval, prepared["approval_id"])
            assert approval is not None
            after_expiry = approval.expires_at + timedelta(seconds=1)

        original_utcnow = action_module.utcnow
        action_module.utcnow = lambda: after_expiry
        try:
            response = _confirm(
                client,
                headers,
                prepared,
                ui_event_id="ui-expired-confirmation",
            )
        finally:
            action_module.utcnow = original_utcnow
        with app.state.database.session() as session:
            approval = session.get(Approval, prepared["approval_id"])
            confirmation_count = session.scalar(
                select(func.count()).select_from(ConfirmationEvent)
            )
            execution_count = session.scalar(
                select(func.count()).select_from(ActionExecution)
            )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_EXPIRED"
    assert approval is not None and approval.status == "EXPIRED"
    assert confirmation_count == 0
    assert execution_count == 0


def test_cross_customer_cannot_replay_another_customers_execution():
    app = _create_test_app()
    with TestClient(app) as client:
        customer_a = _authenticate(client, "linfan@example.com")
        customer_b = _authenticate(client, "chencheng@example.com")
        headers_a = _headers(customer_a, conversation_id="conv-owner-a")
        headers_b = _headers(customer_b, conversation_id="conv-owner-a")

        prepared = _prepare_cancel(client, headers_a)
        _present(client, headers_a, prepared)
        executed = _confirm(
            client,
            headers_a,
            prepared,
            ui_event_id="ui-owner-a",
        )
        assert executed.status_code == 200, executed.text

        replay_as_b = _confirm(
            client,
            headers_b,
            prepared,
            ui_event_id="ui-attacker-b",
        )

    assert replay_as_b.status_code == 404
    assert replay_as_b.json()["error"]["code"] == "APPROVAL_NOT_FOUND"
    assert "ORD-1001" not in replay_as_b.text
    assert "ORDER_CANCELLED" not in replay_as_b.text


def test_same_customer_cannot_replay_execution_from_another_conversation():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        owner_headers = _headers(token, conversation_id="conv-owner")
        other_headers = _headers(token, conversation_id="conv-other")

        prepared = _prepare_cancel(client, owner_headers)
        _present(client, owner_headers, prepared)
        executed = _confirm(
            client,
            owner_headers,
            prepared,
            ui_event_id="ui-owner",
        )
        assert executed.status_code == 200, executed.text

        replay_from_other_conversation = _confirm(
            client,
            other_headers,
            prepared,
            ui_event_id="ui-other-conversation",
        )

    assert replay_from_other_conversation.status_code == 404
    assert replay_from_other_conversation.json()["error"]["code"] == "APPROVAL_NOT_FOUND"
    assert "ORD-1001" not in replay_from_other_conversation.text


def test_ui_confirmation_event_cannot_be_reused_for_another_approval():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        cancel_headers = _headers(token, conversation_id="conv-cancel")
        return_headers = _headers(token, conversation_id="conv-return")
        cancel = _prepare_cancel(client, cancel_headers)
        returned = _prepare_return(client, return_headers)
        _present(client, cancel_headers, cancel)
        _present(client, return_headers, returned)

        first = _confirm(
            client,
            cancel_headers,
            cancel,
            ui_event_id="ui-shared-event",
        )
        second = _confirm(
            client,
            return_headers,
            returned,
            ui_event_id="ui-shared-event",
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFIRMATION_EVENT_REUSED"


def test_new_approval_supersedes_old_approval_in_same_conversation():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        headers = _headers(token, conversation_id="conv-supersede")
        first = _prepare_cancel(client, headers)
        second = _prepare_cancel(client, headers)

        stale_presentation = client.post(
            f"/v1/actions/{first['approval_id']}/present",
            headers=headers,
            json={"preview_hash": first["preview_hash"]},
        )
        assert first["approval_id"] != second["approval_id"]
        assert stale_presentation.status_code == 409
        assert stale_presentation.json()["error"]["code"] == "APPROVAL_SUPERSEDED"

        with app.state.database.session() as session:
            first_approval = session.get(Approval, first["approval_id"])
            second_approval = session.get(Approval, second["approval_id"])
            assert first_approval is not None
            assert second_approval is not None
            assert first_approval.status == "SUPERSEDED"
            assert second_approval.status == "PREPARED"


def test_new_preview_invalidates_a_recorded_unexecuted_confirmation():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        headers = _headers(token, conversation_id="conv-supersede-confirmed")
        first = _prepare_cancel(client, headers)
        _present(client, headers, first)

        with app.state.database.session() as session:
            customer_id = app.state.tools.auth_service.resolve_customer_id(
                session,
                token,
            )
            confirmation = app.state.tools.action_service.record_confirmation(
                session,
                customer_id=customer_id,
                conversation_id="conv-supersede-confirmed",
                approval_id=first["approval_id"],
                request=ConfirmActionRequest(
                    preview_hash=first["preview_hash"],
                    ui_event_id="ui-confirm-before-new-preview",
                    confirmation_source="BUTTON",
                ),
            )

        second = _prepare_cancel(client, headers)

        with app.state.database.session() as session:
            first_approval = session.get(Approval, first["approval_id"])
            recorded_confirmation = session.get(
                ConfirmationEvent,
                confirmation.confirmation_event_id,
            )

    assert first["approval_id"] != second["approval_id"]
    assert first_approval is not None
    assert first_approval.status == "SUPERSEDED"
    assert recorded_confirmation is not None
    assert recorded_confirmation.consumed_at is not None


def test_legacy_execute_route_is_not_available():
    app = _create_test_app()
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        response = client.post(
            "/v1/actions/execute",
            headers=_headers(token, conversation_id="conv-legacy"),
            json={
                "approval_id": "APR-LEGACY",
                "idempotency_key": "legacy-execute-0001",
                "explicit_confirmation": True,
            },
        )
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    openapi_paths = app.openapi()["paths"]
    assert "/v1/actions/execute" not in openapi_paths
    assert "/v1/debug/tool-events" not in openapi_paths


def test_enabled_debug_route_requires_admin_and_returns_safe_trace_fields():
    settings = Settings(
        database_url="sqlite:///:memory:",
        host_confirmation_token=HOST_CONFIRMATION_TOKEN,
        enable_debug_routes=True,
        debug_admin_token="debug-test-token",
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        token = _authenticate(client, "linfan@example.com")
        headers = _headers(
            token,
            conversation_id="conv-debug",
            run_id="debug-run",
        )
        order = client.get("/v1/orders/ORD-1001", headers=headers)
        assert order.status_code == 200

        unauthenticated = client.get(
            "/v1/debug/tool-events",
            params={"run_id": "debug-run"},
        )
        authenticated = client.get(
            "/v1/debug/tool-events",
            params={"run_id": "debug-run"},
            headers={"X-Debug-Admin-Token": "debug-test-token"},
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200, authenticated.text
    events = authenticated.json()
    assert events
    assert events[0]["run_id"] == "debug-run"
    assert set(events[0]) == {
        "id",
        "run_id",
        "tool_name",
        "success",
        "error_code",
        "latency_ms",
        "created_at",
    }


def test_debug_routes_fail_closed_without_admin_token():
    settings = Settings(
        database_url="sqlite:///:memory:",
        host_confirmation_token=HOST_CONFIRMATION_TOKEN,
        enable_debug_routes=True,
        debug_admin_token=None,
    )
    try:
        create_app(settings=settings)
    except ValueError as exc:
        assert "DEBUG_ADMIN_TOKEN" in str(exc)
    else:
        raise AssertionError("debug routes must not start without an admin token")


def test_compose_binds_demo_api_to_loopback_only():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '127.0.0.1:8000:8000' in compose
