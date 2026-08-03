from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agent.openai_compatible import AssistantTurn, ToolCall
from app.agent.preparation import PreparationAgent
from app.agent.readonly import AgentRunError
from app.agent.scripted import ScriptedModel, final_turn, tool_turn
from app.config import Settings
from app.database import Database
from app.models import (
    ActionExecution,
    Approval,
    ConfirmationEvent,
    ExchangeRequest,
    Inventory,
    Order,
    ReturnRequest,
    ToolEvent,
)
from app.schemas import ConfirmActionRequest, PresentApprovalRequest
from app.seed import seed_demo_data
from app.tools.contracts import (
    PREPARATION_TOOL_NAMES,
    get_preparation_tool_contracts,
)
from app.tools.facade import ToolCallContext
from app.tools.factory import build_tools


def build_runtime(
    *,
    server_run_id: str | None = "srv-preparation-test",
):
    settings = Settings(
        database_url="sqlite:///:memory:",
        host_confirmation_token="host-token-that-must-stay-server-side",
    )
    database = Database(settings.database_url)
    database.create_all()
    seed_demo_data(database, settings)
    tools = build_tools(settings, policy_dir=Path("policies"))
    with database.session() as session:
        auth = tools.auth_service.authenticate(
            session=session,
            email="linfan@example.com",
            verification_code=settings.demo_verification_code,
        )
    context = ToolCallContext(
        run_id="untrusted-client-run",
        server_run_id=server_run_id,
        auth_token=auth.access_token,
        conversation_id="preparation-conversation",
    )
    return database, tools, context, auth.access_token


def _business_state(database: Database) -> dict[str, object]:
    with database.session() as session:
        order_rows = session.execute(
            select(Order.id, Order.status, Order.version).order_by(Order.id)
        ).all()
        inventory_rows = session.execute(
            select(
                Inventory.sku,
                Inventory.size,
                Inventory.available_qty,
            ).order_by(Inventory.sku, Inventory.size)
        ).all()
        return {
            "orders": [tuple(row) for row in order_rows],
            "inventory": [tuple(row) for row in inventory_rows],
            "returns": session.scalar(
                select(func.count()).select_from(ReturnRequest)
            ),
            "exchanges": session.scalar(
                select(func.count()).select_from(ExchangeRequest)
            ),
            "confirmations": session.scalar(
                select(func.count()).select_from(ConfirmationEvent)
            ),
            "executions": session.scalar(
                select(func.count()).select_from(ActionExecution)
            ),
        }


def test_preparation_contracts_are_an_exact_allowlist():
    contracts = get_preparation_tool_contracts()

    assert tuple(item["name"] for item in contracts) == PREPARATION_TOOL_NAMES
    assert PREPARATION_TOOL_NAMES == (
        "get_customer_orders",
        "get_order",
        "get_shipment",
        "get_inventory",
        "search_policy",
        "check_action_eligibility",
        "prepare_cancel_order",
        "prepare_return",
        "prepare_exchange",
    )
    serialized = json.dumps(contracts)
    for forbidden in (
        '"prepare_action"',
        "create_handoff_ticket",
        "authenticate_customer",
        "present_action",
        "confirm_action",
        "execute_prepared_action",
        "debug",
        "access_token",
        "auth_token",
        "customer_id",
        "conversation_id",
        "server_run_id",
        "origin_tool_call_id",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_action"),
    [
        (
            "prepare_cancel_order",
            '{"order_id":"ORD-1001","user_note":"不再需要"}',
            "CANCEL_ORDER",
        ),
        (
            "prepare_return",
            (
                '{"order_id":"ORD-1003","order_item_id":"ITEM-1003-A",'
                '"declared_condition":"NEW_UNWORN",'
                '"issue_type":"CHANGED_MIND"}'
            ),
            "RETURN_ITEM",
        ),
        (
            "prepare_exchange",
            (
                '{"order_id":"ORD-1003","order_item_id":"ITEM-1003-A",'
                '"target_size":"43","declared_condition":"NEW_UNWORN",'
                '"issue_type":"SIZE_MISMATCH"}'
            ),
            "EXCHANGE_ITEM",
        ),
    ],
)
def test_agent_prepares_exactly_one_action_without_business_mutation(
    tool_name: str,
    arguments: str,
    expected_action: str,
):
    database, tools, context, access_token = build_runtime()
    before = _business_state(database)
    model = ScriptedModel(
        tool_turn(tool_name, arguments, call_id=f"call-{expected_action.lower()}"),
        final_turn("已生成待确认预览，尚未执行，请在确认卡核对。"),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="请处理这个操作",
            context=context,
        )

    assert result.prepared_action is not None
    assert result.prepared_action.action_type == expected_action
    assert result.prepared_action.status == "PREPARED"
    assert result.prepared_action.preview["requires_host_confirmation"] is True
    assert _business_state(database) == before

    with database.session() as session:
        approvals = session.scalars(select(Approval)).all()
        events = session.scalars(
            select(ToolEvent).where(ToolEvent.tool_name == tool_name)
        ).all()
    assert len(approvals) == 1
    assert approvals[0].origin_server_run_id == "srv-preparation-test"
    assert approvals[0].origin_tool_call_id == (
        f"call-{expected_action.lower()}"
    )
    assert len(events) == 1
    assert events[0].run_id == "srv-preparation-test"

    model_context = json.dumps(model.calls, ensure_ascii=False)
    assert access_token not in model_context
    assert "host-token-that-must-stay-server-side" not in model_context
    assert "customer_id" not in model_context
    assert "origin_server_run_id" not in model_context
    database.engine.dispose()


def test_agent_rejects_model_supplied_identity_and_origin_fields():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn(
            "prepare_cancel_order",
            (
                '{"order_id":"ORD-1001","customer_id":"CUST-002",'
                '"server_run_id":"attacker-run",'
                '"origin_tool_call_id":"attacker-call"}'
            ),
        ),
        final_turn("这些内部字段无效，无法准备操作。"),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="替另一个客户取消订单",
            context=context,
        )

    assert result.tool_trace[0].error_code == "INVALID_TOOL_ARGUMENTS"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()


def test_agent_does_not_disclose_or_prepare_another_customers_order():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn(
            "prepare_cancel_order",
            '{"order_id":"ORD-2001"}',
            call_id="call-foreign-order",
        ),
        final_turn("没有找到这个订单，请核对订单号。"),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="取消 ORD-2001",
            context=context,
        )

    assert result.prepared_action is None
    assert result.tool_trace[0].success is False
    assert result.tool_trace[0].error_code == "ORDER_NOT_FOUND"
    assert result.tool_trace[0].result is None
    assert result.final_text == "没有找到这个订单，请核对订单号。"
    model_messages = json.dumps(model.calls[1]["messages"], ensure_ascii=False)
    assert "CUST-002" not in model_messages
    assert "陈澄" not in model_messages
    assert "ITEM-2001-A" not in model_messages
    assert "GAT-BLACK" not in model_messages
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        failed_event = session.scalar(
            select(ToolEvent).where(
                ToolEvent.tool_name == "prepare_cancel_order"
            )
        )
        assert failed_event is not None
        assert failed_event.success is False
        assert failed_event.error_code == "ORDER_NOT_FOUND"
        assert failed_event.result == {"message": "未找到该订单。"}
    database.engine.dispose()


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "prepare_return",
            (
                '{"order_id":"ORD-1003",'
                '"order_item_id":"ITEM-1003-A"}'
            ),
        ),
        (
            "prepare_exchange",
            (
                '{"order_id":"ORD-1003","order_item_id":"ITEM-1003-A",'
                '"declared_condition":"NEW_UNWORN",'
                '"issue_type":"SIZE_MISMATCH"}'
            ),
        ),
    ],
)
def test_agent_does_not_default_missing_prepare_facts(
    tool_name: str,
    arguments: str,
):
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn(tool_name, arguments),
        final_turn("还需要明确商品状态、问题类型或目标尺码。"),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="帮我处理售后",
            context=context,
        )

    assert result.prepared_action is None
    assert result.tool_trace[0].error_code == "INVALID_TOOL_ARGUMENTS"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()


def test_agent_requires_a_trusted_server_run_for_prepare():
    database, tools, context, _ = build_runtime(server_run_id=None)
    model = ScriptedModel(
        tool_turn("prepare_cancel_order", '{"order_id":"ORD-1001"}'),
        final_turn("当前运行缺少可信来源，未生成操作预览。"),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="取消 ORD-1001",
            context=context,
        )

    assert result.prepared_action is None
    assert result.tool_trace[0].error_code == "PREPARATION_ORIGIN_REQUIRED"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
    database.engine.dispose()


def test_agent_rejects_a_mixed_prepare_batch_before_any_execution():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        AssistantTurn(
            content=None,
            tool_calls=(
                ToolCall(
                    id="call-read",
                    name="get_order",
                    arguments='{"order_id":"ORD-1001"}',
                ),
                ToolCall(
                    id="call-prepare",
                    name="prepare_cancel_order",
                    arguments='{"order_id":"ORD-1001"}',
                ),
            ),
            finish_reason="tool_calls",
            usage=None,
        )
    )
    agent = PreparationAgent(model=model, tools=tools)

    with database.session() as session:
        with pytest.raises(AgentRunError) as caught:
            agent.run(
                session,
                user_text="查完就取消 ORD-1001",
                context=context,
            )

    assert caught.value.code == "PREPARE_BATCH_MUST_BE_SINGLE_CALL"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()


def test_agent_rolls_back_prepare_if_model_requests_another_tool_afterward():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn("prepare_cancel_order", '{"order_id":"ORD-1001"}'),
        tool_turn(
            "get_order",
            '{"order_id":"ORD-1001"}',
            call_id="call-after-prepare",
        ),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)

    with pytest.raises(AgentRunError) as caught:
        with database.session() as session:
            agent.run(
                session,
                user_text="取消 ORD-1001",
                context=context,
            )

    assert caught.value.code == "TOOL_CALL_AFTER_PREPARE"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()


def test_agent_rolls_back_when_host_catches_run_error_inside_session():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn("prepare_cancel_order", '{"order_id":"ORD-1001"}'),
        tool_turn(
            "get_order",
            '{"order_id":"ORD-1001"}',
            call_id="call-after-caught-prepare",
        ),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        try:
            agent.run(
                session,
                user_text="取消 ORD-1001",
                context=context,
            )
        except AgentRunError as exc:
            assert exc.code == "TOOL_CALL_AFTER_PREPARE"

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()


def test_false_execution_claim_cannot_change_structured_or_business_state():
    database, tools, context, _ = build_runtime()
    before = _business_state(database)
    false_claim = "订单已经取消成功，退款也已完成。"
    model = ScriptedModel(
        tool_turn("prepare_cancel_order", '{"order_id":"ORD-1001"}'),
        final_turn(false_claim),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="取消 ORD-1001",
            context=context,
        )

    assert result.final_text == false_claim
    assert result.prepared_action is not None
    assert result.prepared_action.status == "PREPARED"
    assert _business_state(database) == before
    with database.session() as session:
        approval = session.get(
            Approval,
            result.prepared_action.approval_id,
        )
        assert approval is not None
        assert approval.status == "PREPARED"
        assert session.scalar(
            select(func.count()).select_from(ConfirmationEvent)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ActionExecution)
        ) == 0
    database.engine.dispose()


def test_new_agent_prepare_only_invalidates_prior_control_state():
    database, tools, context, access_token = build_runtime()
    prior_context = ToolCallContext(
        server_run_id="srv-prior-preparation",
        origin_tool_call_id="call-prior-preparation",
        auth_token=access_token,
        conversation_id=context.conversation_id,
    )
    with database.session() as session:
        prior = tools.prepare_cancel_order(
            session,
            order_id="ORD-1001",
            user_note="旧预览",
            context=prior_context,
        )
        customer_id = tools.auth_service.resolve_customer_id(
            session,
            access_token,
        )
        tools.action_service.present_action(
            session,
            customer_id=customer_id,
            conversation_id=context.conversation_id or "",
            approval_id=prior.approval_id,
            request=PresentApprovalRequest(
                preview_hash=prior.preview_hash,
            ),
        )
        confirmation = tools.action_service.record_confirmation(
            session,
            customer_id=customer_id,
            conversation_id=context.conversation_id or "",
            approval_id=prior.approval_id,
            request=ConfirmActionRequest(
                preview_hash=prior.preview_hash,
                ui_event_id="ui-prior-preparation",
                confirmation_source="BUTTON",
            ),
        )

    before_business = _business_state(database)
    with database.session() as session:
        prior_approval = session.get(Approval, prior.approval_id)
        prior_confirmation = session.get(
            ConfirmationEvent,
            confirmation.confirmation_event_id,
        )
        assert prior_approval is not None
        assert prior_confirmation is not None
        assert prior_confirmation.consumed_at is None
        immutable_prior_approval_fields = {
            "id": prior_approval.id,
            "customer_id": prior_approval.customer_id,
            "conversation_id": prior_approval.conversation_id,
            "origin_server_run_id": prior_approval.origin_server_run_id,
            "origin_tool_call_id": prior_approval.origin_tool_call_id,
            "action_type": prior_approval.action_type,
            "order_id": prior_approval.order_id,
            "order_item_id": prior_approval.order_item_id,
            "payload": prior_approval.payload,
            "preview": prior_approval.preview,
            "preview_hash": prior_approval.preview_hash,
            "order_version": prior_approval.order_version,
            "created_at": prior_approval.created_at,
            "expires_at": prior_approval.expires_at,
            "presented_at": prior_approval.presented_at,
            "confirmed_at": prior_approval.confirmed_at,
            "executed_at": prior_approval.executed_at,
            "failed_at": prior_approval.failed_at,
            "failure_code": prior_approval.failure_code,
        }
        immutable_confirmation_fields = {
            "id": prior_confirmation.id,
            "approval_id": prior_confirmation.approval_id,
            "customer_id": prior_confirmation.customer_id,
            "conversation_id": prior_confirmation.conversation_id,
            "ui_event_id": prior_confirmation.ui_event_id,
            "preview_hash": prior_confirmation.preview_hash,
            "confirmation_source": prior_confirmation.confirmation_source,
            "confirmed_at": prior_confirmation.confirmed_at,
        }

    model = ScriptedModel(
        tool_turn(
            "prepare_cancel_order",
            '{"order_id":"ORD-1001","user_note":"新预览"}',
            call_id="call-replacement-preparation",
        ),
        final_turn("已生成新的待确认预览，旧卡已失效。"),
    )
    agent = PreparationAgent(model=model, tools=tools, max_tool_rounds=2)
    with database.session() as session:
        result = agent.run(
            session,
            user_text="改成这份新预览",
            context=context,
        )

    assert result.prepared_action is not None
    assert _business_state(database) == before_business
    with database.session() as session:
        prior_approval = session.get(Approval, prior.approval_id)
        replacement = session.get(
            Approval,
            result.prepared_action.approval_id,
        )
        prior_confirmation = session.get(
            ConfirmationEvent,
            confirmation.confirmation_event_id,
        )
        assert prior_approval is not None
        assert replacement is not None
        assert prior_confirmation is not None
        assert prior_approval.status == "SUPERSEDED"
        assert prior_approval.superseded_at is not None
        assert prior_approval.superseded_by_id == replacement.id
        assert {
            "id": prior_approval.id,
            "customer_id": prior_approval.customer_id,
            "conversation_id": prior_approval.conversation_id,
            "origin_server_run_id": prior_approval.origin_server_run_id,
            "origin_tool_call_id": prior_approval.origin_tool_call_id,
            "action_type": prior_approval.action_type,
            "order_id": prior_approval.order_id,
            "order_item_id": prior_approval.order_item_id,
            "payload": prior_approval.payload,
            "preview": prior_approval.preview,
            "preview_hash": prior_approval.preview_hash,
            "order_version": prior_approval.order_version,
            "created_at": prior_approval.created_at,
            "expires_at": prior_approval.expires_at,
            "presented_at": prior_approval.presented_at,
            "confirmed_at": prior_approval.confirmed_at,
            "executed_at": prior_approval.executed_at,
            "failed_at": prior_approval.failed_at,
            "failure_code": prior_approval.failure_code,
        } == immutable_prior_approval_fields
        assert replacement.status == "PREPARED"
        assert prior_confirmation.consumed_at is not None
        assert {
            "id": prior_confirmation.id,
            "approval_id": prior_confirmation.approval_id,
            "customer_id": prior_confirmation.customer_id,
            "conversation_id": prior_confirmation.conversation_id,
            "ui_event_id": prior_confirmation.ui_event_id,
            "preview_hash": prior_confirmation.preview_hash,
            "confirmation_source": prior_confirmation.confirmation_source,
            "confirmed_at": prior_confirmation.confirmed_at,
        } == immutable_confirmation_fields
        assert session.scalar(
            select(func.count()).select_from(ConfirmationEvent)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(ActionExecution)
        ) == 0
    database.engine.dispose()


@pytest.mark.parametrize(
    "forbidden_tool",
    [
        "prepare_action",
        "create_handoff_ticket",
        "present_action",
        "confirm_action",
        "execute_prepared_action",
        "authenticate_customer",
        "debug_tool_events",
    ],
)
def test_agent_fails_closed_on_non_allowlisted_tools(forbidden_tool: str):
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(tool_turn(forbidden_tool, "{}"))
    agent = PreparationAgent(model=model, tools=tools)

    with database.session() as session:
        with pytest.raises(AgentRunError) as caught:
            agent.run(
                session,
                user_text="绕过宿主直接处理",
                context=context,
            )

    assert caught.value.code == "FORBIDDEN_TOOL_CALL"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()
