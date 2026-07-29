from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.agent.openai_compatible import AssistantTurn, ToolCall
from app.agent.readonly import AgentRunError, ReadOnlyAgent
from app.config import Settings
from app.database import Database
from app.models import Approval, ToolEvent
from app.seed import seed_demo_data
from app.tools.contracts import READ_ONLY_TOOL_NAMES, get_read_only_tool_contracts
from app.tools.facade import ToolCallContext
from app.tools.factory import build_tools


class ScriptedModel:
    def __init__(self, *turns: AssistantTurn):
        self.turns = list(turns)
        self.calls: list[dict[str, object]] = []

    def complete(self, *, messages, tools):
        self.calls.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
            }
        )
        if not self.turns:
            raise AssertionError("scripted model ran out of turns")
        return self.turns.pop(0)


def tool_turn(name: str, arguments: str, *, call_id: str = "call-1"):
    return AssistantTurn(
        content=None,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        finish_reason="tool_calls",
        usage=None,
    )


def final_turn(content: str):
    return AssistantTurn(
        content=content,
        tool_calls=(),
        finish_reason="stop",
        usage=None,
    )


def build_runtime():
    settings = Settings(
        database_url="sqlite:///:memory:",
        host_confirmation_token="host-token-that-must-stay-server-side",
    )
    database = Database(settings.database_url)
    database.create_all()
    seed_demo_data(database, settings)
    tools = build_tools(settings, policy_dir=__import__("pathlib").Path("policies"))
    with database.session() as session:
        auth = tools.auth_service.authenticate(
            session=session,
            email="linfan@example.com",
            verification_code=settings.demo_verification_code,
        )
    context = ToolCallContext(
        run_id="readonly-agent-test",
        auth_token=auth.access_token,
        conversation_id="readonly-conversation",
    )
    return database, tools, context, auth.access_token


def test_read_only_contracts_are_an_exact_allowlist():
    contracts = get_read_only_tool_contracts()
    assert tuple(item["name"] for item in contracts) == READ_ONLY_TOOL_NAMES
    assert READ_ONLY_TOOL_NAMES == (
        "get_customer_orders",
        "get_order",
        "get_shipment",
        "get_inventory",
        "search_policy",
        "check_action_eligibility",
    )
    serialized = json.dumps(contracts)
    for forbidden in (
        "prepare_cancel_order",
        "prepare_return",
        "prepare_exchange",
        "create_handoff_ticket",
        "execute_prepared_action",
        "access_token",
        "auth_token",
        "verification_code",
        "customer_id",
    ):
        assert forbidden not in serialized


def test_agent_executes_read_only_tool_loop_without_putting_secrets_in_model_context():
    database, tools, context, access_token = build_runtime()
    model = ScriptedModel(
        tool_turn("get_order", '{"order_id":"ORD-1002"}'),
        final_turn("ORD-1002 已发货，当前正在运输中。"),
    )
    agent = ReadOnlyAgent(model=model, tools=tools, max_tool_rounds=3)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="帮我查一下 ORD-1002 到哪了",
            context=context,
        )

    assert result.final_text == "ORD-1002 已发货，当前正在运输中。"
    assert len(result.tool_trace) == 1
    assert result.tool_trace[0].tool_name == "get_order"
    assert result.tool_trace[0].success is True
    model_context = json.dumps(model.calls, ensure_ascii=False)
    assert access_token not in model_context
    assert "host-token-that-must-stay-server-side" not in model_context
    assert "customer_id" not in model_context
    assert tuple(
        contract["name"] for contract in model.calls[0]["tools"]
    ) == READ_ONLY_TOOL_NAMES

    with database.session() as session:
        assert session.scalar(
            select(func.count())
            .select_from(ToolEvent)
            .where(ToolEvent.tool_name == "get_order")
        ) == 1
    database.engine.dispose()


def test_agent_fails_closed_on_forbidden_tool_without_creating_approval():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn(
            "prepare_cancel_order",
            '{"order_id":"ORD-1001","user_note":null}',
        )
    )
    agent = ReadOnlyAgent(model=model, tools=tools, max_tool_rounds=3)

    with database.session() as session:
        with pytest.raises(AgentRunError) as caught:
            agent.run(
                session,
                user_text="直接取消 ORD-1001",
                context=context,
            )

    assert caught.value.code == "FORBIDDEN_TOOL_CALL"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(
            select(func.count())
            .select_from(ToolEvent)
            .where(ToolEvent.tool_name.like("prepare%"))
        ) == 0
    database.engine.dispose()


def test_agent_returns_invalid_arguments_to_model_without_executing_tool():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn(
            "get_order",
            '{"order_id":"ORD-2001","customer_id":"CUST-002"}',
        ),
        final_turn("我无法使用该参数查询订单，请确认订单号。"),
    )
    agent = ReadOnlyAgent(model=model, tools=tools, max_tool_rounds=3)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="查另一个客户的订单",
            context=context,
        )

    assert result.tool_trace[0].success is False
    assert result.tool_trace[0].error_code == "INVALID_TOOL_ARGUMENTS"
    assert result.tool_trace[0].result is None
    second_request = model.calls[1]["messages"]
    tool_message = next(
        message for message in second_request if message["role"] == "tool"
    )
    tool_payload = json.loads(tool_message["content"])
    assert tool_payload["ok"] is False
    assert tool_payload["error"]["code"] == "INVALID_TOOL_ARGUMENTS"
    with database.session() as session:
        assert session.scalar(
            select(func.count())
            .select_from(ToolEvent)
            .where(ToolEvent.tool_name == "get_order")
        ) == 0
    database.engine.dispose()


def test_agent_surfaces_customer_safe_tool_error_and_can_finish():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn("get_order", '{"order_id":"ORD-2001"}'),
        final_turn("没有找到这个订单，请核对订单号。"),
    )
    agent = ReadOnlyAgent(model=model, tools=tools, max_tool_rounds=3)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="查一下 ORD-2001",
            context=context,
        )

    assert result.tool_trace[0].success is False
    assert result.tool_trace[0].error_code == "ORDER_NOT_FOUND"
    tool_payload = json.loads(
        next(
            message
            for message in model.calls[1]["messages"]
            if message["role"] == "tool"
        )["content"]
    )
    assert tool_payload["ok"] is False
    assert tool_payload["error"]["code"] == "ORDER_NOT_FOUND"
    assert result.final_text == "没有找到这个订单，请核对订单号。"
    database.engine.dispose()


def test_agent_stops_before_executing_tools_beyond_round_limit():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn("get_customer_orders", "{}", call_id="call-1"),
        tool_turn("get_customer_orders", "{}", call_id="call-2"),
    )
    agent = ReadOnlyAgent(model=model, tools=tools, max_tool_rounds=1)

    with database.session() as session:
        with pytest.raises(AgentRunError) as caught:
            agent.run(
                session,
                user_text="反复查我的订单",
                context=context,
            )

    assert caught.value.code == "MAX_TOOL_ROUNDS_EXCEEDED"
    with database.session() as session:
        assert session.scalar(
            select(func.count())
            .select_from(ToolEvent)
            .where(ToolEvent.tool_name == "get_customer_orders")
        ) == 1
    database.engine.dispose()


def test_agent_rejects_empty_final_answer():
    database, tools, context, _ = build_runtime()
    agent = ReadOnlyAgent(
        model=ScriptedModel(final_turn("  ")),
        tools=tools,
        max_tool_rounds=1,
    )

    with database.session() as session:
        with pytest.raises(AgentRunError) as caught:
            agent.run(
                session,
                user_text="你好",
                context=context,
            )

    assert caught.value.code == "EMPTY_MODEL_RESPONSE"
    database.engine.dispose()


def test_agent_executes_multiple_structured_tool_calls_in_one_round():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        AssistantTurn(
            content="",
            tool_calls=(
                ToolCall(
                    id="call-order",
                    name="get_order",
                    arguments='{"order_id":"ORD-1003"}',
                ),
                ToolCall(
                    id="call-inventory",
                    name="get_inventory",
                    arguments='{"sku":"GAT-WHITE","size":"43"}',
                ),
            ),
            finish_reason="tool_calls",
            usage=None,
        ),
        final_turn("订单已送达，43 码当前有库存。"),
    )
    agent = ReadOnlyAgent(model=model, tools=tools, max_tool_rounds=1)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="查 ORD-1003 和 GAT-WHITE 43 码库存",
            context=context,
        )

    assert [item.tool_name for item in result.tool_trace] == [
        "get_order",
        "get_inventory",
    ]
    second_messages = model.calls[1]["messages"]
    assistant_message = next(
        message for message in second_messages if message["role"] == "assistant"
    )
    assert len(assistant_message["tool_calls"]) == 2
    assert {
        message["tool_call_id"]
        for message in second_messages
        if message["role"] == "tool"
    } == {"call-order", "call-inventory"}
    database.engine.dispose()


def test_agent_rejects_duplicate_tool_call_id_across_rounds():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn("get_customer_orders", "{}", call_id="duplicate-id"),
        tool_turn(
            "get_inventory",
            '{"sku":"GAT-WHITE","size":"43"}',
            call_id="duplicate-id",
        ),
    )
    agent = ReadOnlyAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        with pytest.raises(AgentRunError) as caught:
            agent.run(
                session,
                user_text="先查订单再查库存",
                context=context,
            )

    assert caught.value.code == "DUPLICATE_TOOL_CALL_ID"
    with database.session() as session:
        assert session.scalar(
            select(func.count())
            .select_from(ToolEvent)
            .where(ToolEvent.tool_name == "get_customer_orders")
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(ToolEvent)
            .where(ToolEvent.tool_name == "get_inventory")
        ) == 0
    database.engine.dispose()


def test_agent_rejects_excessive_parallel_calls_before_any_execution():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        AssistantTurn(
            content=None,
            tool_calls=tuple(
                ToolCall(
                    id=f"call-{index}",
                    name="get_customer_orders",
                    arguments="{}",
                )
                for index in range(3)
            ),
            finish_reason="tool_calls",
            usage=None,
        )
    )
    agent = ReadOnlyAgent(
        model=model,
        tools=tools,
        max_tool_rounds=2,
        max_tool_calls=2,
    )

    with database.session() as session:
        with pytest.raises(AgentRunError) as caught:
            agent.run(
                session,
                user_text="反复查订单",
                context=context,
            )

    assert caught.value.code == "MAX_TOOL_CALLS_EXCEEDED"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()


@pytest.mark.parametrize(
    "arguments",
    [
        (
            '{"action_type":"RETURN_ITEM","order_id":"ORD-1003",'
            '"order_item_id":"ITEM-1003-A"}'
        ),
        (
            '{"action_type":"EXCHANGE_ITEM","order_id":"ORD-1003",'
            '"order_item_id":"ITEM-1003-A","declared_condition":"NEW_UNWORN",'
            '"issue_type":"SIZE_MISMATCH"}'
        ),
    ],
)
def test_agent_does_not_default_missing_return_or_exchange_facts(
    arguments: str,
):
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        tool_turn("check_action_eligibility", arguments),
        final_turn("还需要商品状态、问题类型或目标尺码，才能判断资格。"),
    )
    agent = ReadOnlyAgent(model=model, tools=tools, max_tool_rounds=2)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="我想办理售后。",
            context=context,
        )

    assert result.tool_trace[0].success is False
    assert result.tool_trace[0].error_code == "INVALID_TOOL_ARGUMENTS"
    with database.session() as session:
        assert session.scalar(
            select(func.count())
            .select_from(ToolEvent)
            .where(ToolEvent.tool_name == "check_action_eligibility")
        ) == 0
    database.engine.dispose()


def test_agent_preflights_entire_batch_before_forbidden_tool_failure():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        AssistantTurn(
            content=None,
            tool_calls=(
                ToolCall(
                    id="call-safe",
                    name="get_order",
                    arguments='{"order_id":"ORD-1001"}',
                ),
                ToolCall(
                    id="call-forbidden",
                    name="prepare_cancel_order",
                    arguments='{"order_id":"ORD-1001"}',
                ),
            ),
            finish_reason="tool_calls",
            usage=None,
        )
    )
    agent = ReadOnlyAgent(model=model, tools=tools)

    with database.session() as session:
        with pytest.raises(AgentRunError) as caught:
            agent.run(
                session,
                user_text="查完就直接取消 ORD-1001",
                context=context,
            )

    assert caught.value.code == "FORBIDDEN_TOOL_CALL"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()


def test_agent_rejects_entire_batch_when_one_call_has_invalid_schema():
    database, tools, context, _ = build_runtime()
    model = ScriptedModel(
        AssistantTurn(
            content=None,
            tool_calls=(
                ToolCall(
                    id="call-valid",
                    name="get_order",
                    arguments='{"order_id":"ORD-1001"}',
                ),
                ToolCall(
                    id="call-invalid",
                    name="get_inventory",
                    arguments='{"sku":"GAT-WHITE"}',
                ),
            ),
            finish_reason="tool_calls",
            usage=None,
        ),
        final_turn("库存查询还缺少尺码，请补充。"),
    )
    agent = ReadOnlyAgent(model=model, tools=tools)

    with database.session() as session:
        result = agent.run(
            session,
            user_text="查订单和库存",
            context=context,
        )

    assert [item.error_code for item in result.tool_trace] == [
        "TOOL_BATCH_REJECTED",
        "INVALID_TOOL_ARGUMENTS",
    ]
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ToolEvent)) == 0
    database.engine.dispose()
