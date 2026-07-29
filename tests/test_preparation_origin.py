from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.errors import ConflictError, ServiceError
from app.models import Approval
from app.tools.facade import ToolCallContext


def test_same_prepare_origin_and_request_is_idempotent(
    app,
    auth_headers,
):
    tools = app.state.tools
    context = ToolCallContext(
        auth_token=auth_headers["Authorization"].removeprefix("Bearer "),
        conversation_id=auth_headers["X-Conversation-ID"],
        server_run_id="srv-idempotent-origin",
        origin_tool_call_id="call-idempotent-origin",
    )

    with app.state.database.session() as session:
        first = tools.prepare_cancel_order(
            session,
            order_id="ORD-1001",
            user_note="同一次准备",
            context=context,
        )
    with app.state.database.session() as session:
        replay = tools.prepare_cancel_order(
            session,
            order_id="ORD-1001",
            user_note="同一次准备",
            context=context,
        )

    assert replay.approval_id == first.approval_id
    assert replay.preview_hash == first.preview_hash
    with app.state.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 1


@pytest.mark.parametrize(
    ("server_run_id", "tool_call_id"),
    [
        ("", ""),
        ("   ", "call-whitespace"),
        ("srv-whitespace", "\t"),
        ("s" * 81, "call-too-long-run"),
        ("srv-too-long-call", "c" * 201),
    ],
)
def test_invalid_prepare_origin_is_rejected(
    app,
    auth_headers,
    server_run_id: str,
    tool_call_id: str,
):
    tools = app.state.tools
    context = ToolCallContext(
        auth_token=auth_headers["Authorization"].removeprefix("Bearer "),
        conversation_id=auth_headers["X-Conversation-ID"],
        server_run_id=server_run_id,
        origin_tool_call_id=tool_call_id,
    )

    with pytest.raises(ServiceError) as caught:
        with app.state.database.session() as session:
            tools.prepare_cancel_order(
                session,
                order_id="ORD-1001",
                user_note=None,
                context=context,
            )

    assert caught.value.code == "PREPARATION_ORIGIN_INVALID"
    with app.state.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0


def test_same_prepare_origin_with_different_request_conflicts(
    app,
    auth_headers,
):
    tools = app.state.tools
    context = ToolCallContext(
        auth_token=auth_headers["Authorization"].removeprefix("Bearer "),
        conversation_id=auth_headers["X-Conversation-ID"],
        server_run_id="srv-origin-conflict",
        origin_tool_call_id="call-origin-conflict",
    )

    with app.state.database.session() as session:
        tools.prepare_cancel_order(
            session,
            order_id="ORD-1001",
            user_note="最初请求",
            context=context,
        )

    with pytest.raises(ConflictError) as caught:
        with app.state.database.session() as session:
            tools.prepare_cancel_order(
                session,
                order_id="ORD-1001",
                user_note="篡改后的请求",
                context=context,
            )

    assert caught.value.code == "PREPARATION_ORIGIN_CONFLICT"
    with app.state.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 1


def test_same_origin_replay_rejects_a_tampered_stored_preview(
    app,
    auth_headers,
):
    tools = app.state.tools
    context = ToolCallContext(
        auth_token=auth_headers["Authorization"].removeprefix("Bearer "),
        conversation_id=auth_headers["X-Conversation-ID"],
        server_run_id="srv-origin-replay-integrity",
        origin_tool_call_id="call-origin-replay-integrity",
    )
    with app.state.database.session() as session:
        prepared = tools.prepare_cancel_order(
            session,
            order_id="ORD-1001",
            user_note=None,
            context=context,
        )
    with app.state.database.session() as session:
        approval = session.get(Approval, prepared.approval_id)
        assert approval is not None
        approval.preview = {
            **approval.preview,
            "effect": "tampered stored effect",
        }

    with pytest.raises(ServiceError) as caught:
        with app.state.database.session() as session:
            tools.prepare_cancel_order(
                session,
                order_id="ORD-1001",
                user_note=None,
                context=context,
            )

    assert caught.value.code == "APPROVAL_INTEGRITY_ERROR"


def test_generic_http_prepare_remains_compatible_without_agent_origin(
    client,
    auth_headers,
    app,
):
    response = client.post(
        "/v1/actions/prepare",
        headers=auth_headers,
        json={"action_type": "CANCEL_ORDER", "order_id": "ORD-1001"},
    )

    assert response.status_code == 200, response.text
    with app.state.database.session() as session:
        approval = session.get(Approval, response.json()["approval_id"])
        assert approval is not None
        assert approval.origin_server_run_id is None
        assert approval.origin_tool_call_id is None


def test_origin_tampering_invalidates_the_canonical_preview(
    client,
    app,
    auth_headers,
    host_headers,
):
    tools = app.state.tools
    context = ToolCallContext(
        auth_token=auth_headers["Authorization"].removeprefix("Bearer "),
        conversation_id=auth_headers["X-Conversation-ID"],
        server_run_id="srv-origin-integrity",
        origin_tool_call_id="call-origin-integrity",
    )
    with app.state.database.session() as session:
        prepared = tools.prepare_cancel_order(
            session,
            order_id="ORD-1001",
            user_note=None,
            context=context,
        )
    with app.state.database.session() as session:
        approval = session.get(Approval, prepared.approval_id)
        assert approval is not None
        approval.origin_tool_call_id = "call-origin-tampered"

    response = client.post(
        f"/v1/actions/{prepared.approval_id}/present",
        headers=host_headers,
        json={"preview_hash": prepared.preview_hash},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "APPROVAL_INTEGRITY_ERROR"
