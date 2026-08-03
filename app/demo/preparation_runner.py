"""Run PreparationAgent with a scripted model for public_demo (zero provider HTTP)."""

from __future__ import annotations

import uuid

from app.agent.factory import build_preparation_agent
from app.agent.readonly import AgentRunError
from app.agent.scripted import ScriptedModel, final_turn, tool_turn
from app.demo.replay import ReplayMatch
from app.demo.session import DemoSession, bump_or_limit, tool_context
from app.errors import ServiceError
from app.schemas import PrepareActionResponse


def _final_reply_for(match: ReplayMatch) -> str:
    if match.kind == "cancel":
        return (
            "Preparation Agent 已查询订单并准备取消 ORD-1001。"
            "请在右侧确认卡核对数据库中的规范预览；确认前不会执行。"
        )
    if match.kind == "return":
        return (
            "Preparation Agent 已准备退货申请（ORD-1003 / ITEM-1003-A）。"
            "此步骤不会直接退款；请核对确认卡后再确认。"
        )
    return (
        "Preparation Agent 已准备将 ORD-1003 的德训鞋换为 43 码。"
        "库存预占只会在你确认后发生；请先核对确认卡。"
    )


def _scripted_model_for(match: ReplayMatch, *, final_reply: str) -> ScriptedModel:
    """Multi-round trajectory: lookup → eligibility → single prepare → final text."""

    if match.kind == "cancel":
        return ScriptedModel(
            tool_turn(
                "get_order",
                '{"order_id":"ORD-1001"}',
                call_id="demo-script-get-order",
            ),
            tool_turn(
                "check_action_eligibility",
                '{"action_type":"CANCEL_ORDER","order_id":"ORD-1001"}',
                call_id="demo-script-eligibility",
            ),
            tool_turn(
                "prepare_cancel_order",
                '{"order_id":"ORD-1001"}',
                call_id="demo-script-prepare-cancel",
            ),
            final_turn(final_reply),
        )
    if match.kind == "return":
        return ScriptedModel(
            tool_turn(
                "get_order",
                '{"order_id":"ORD-1003"}',
                call_id="demo-script-get-order",
            ),
            tool_turn(
                "check_action_eligibility",
                (
                    '{"action_type":"RETURN_ITEM","order_id":"ORD-1003",'
                    '"order_item_id":"ITEM-1003-A",'
                    '"declared_condition":"NEW_UNWORN",'
                    '"issue_type":"CHANGED_MIND"}'
                ),
                call_id="demo-script-eligibility",
            ),
            tool_turn(
                "prepare_return",
                (
                    '{"order_id":"ORD-1003","order_item_id":"ITEM-1003-A",'
                    '"declared_condition":"NEW_UNWORN",'
                    '"issue_type":"CHANGED_MIND"}'
                ),
                call_id="demo-script-prepare-return",
            ),
            final_turn(final_reply),
        )
    if match.kind == "exchange":
        return ScriptedModel(
            tool_turn(
                "get_order",
                '{"order_id":"ORD-1003"}',
                call_id="demo-script-get-order",
            ),
            tool_turn(
                "check_action_eligibility",
                (
                    '{"action_type":"EXCHANGE_ITEM","order_id":"ORD-1003",'
                    '"order_item_id":"ITEM-1003-A","target_size":"43",'
                    '"declared_condition":"NEW_UNWORN",'
                    '"issue_type":"SIZE_MISMATCH"}'
                ),
                call_id="demo-script-eligibility",
            ),
            tool_turn(
                "prepare_exchange",
                (
                    '{"order_id":"ORD-1003","order_item_id":"ITEM-1003-A",'
                    '"target_size":"43","declared_condition":"NEW_UNWORN",'
                    '"issue_type":"SIZE_MISMATCH"}'
                ),
                call_id="demo-script-prepare-exchange",
            ),
            final_turn(final_reply),
        )
    raise ServiceError(
        "DEMO_SCRIPT_UNKNOWN",
        "未知 scripted Preparation 场景。",
        status_code=500,
    )


def run_preparation_scripted(
    session: DemoSession,
    *,
    message: str,
    match: ReplayMatch,
) -> tuple[str, PrepareActionResponse]:
    """Drive PreparationAgent with scripted turns; write session pending fields."""

    bump_or_limit(
        session,
        counter="prepare_count",
        limit=session.settings.demo_max_prepare_per_session,
        code="DEMO_PREPARE_LIMIT",
        message="本会话准备次数已达上限，请重置演示。",
    )
    final_reply = _final_reply_for(match)
    model = _scripted_model_for(match, final_reply=final_reply)
    agent = build_preparation_agent(
        model=model,
        tools=session.tools,
        max_tool_rounds=session.settings.agent_max_tool_rounds,
        max_tool_calls=session.settings.agent_max_tool_calls,
    )
    # Origin tool call id is bound by PreparationAgent for the prepare call.
    context = tool_context(session)
    try:
        with session.database.session() as db:
            result = agent.run(
                db,
                user_text=message,
                context=context,
            )
    except AgentRunError as exc:
        raise ServiceError(
            exc.code,
            str(exc),
            status_code=409,
        ) from exc

    prepared = result.prepared_action
    if prepared is None:
        raise ServiceError(
            "DEMO_PREPARE_MISSING",
            "Preparation Agent 未生成待确认预览。",
            status_code=500,
        )

    session.pending_approval_id = prepared.approval_id
    session.pending_preview_hash = prepared.preview_hash
    session.pending_ui_event_id = f"demo-ui-{uuid.uuid4().hex}"
    return result.final_text or final_reply, prepared
