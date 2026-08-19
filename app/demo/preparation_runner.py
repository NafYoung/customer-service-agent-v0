"""Run PreparationAgent with a scripted model for public_demo (zero provider HTTP)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.agent.factory import build_preparation_agent
from app.agent.readonly import AgentRunError, ToolTrace
from app.agent.scripted import ScriptedModel, final_turn, tool_turn
from app.demo.matches import ReplayMatch
from app.demo.session import DemoSession, bump_or_limit, tool_context
from app.errors import ServiceError
from app.schemas import PrepareActionResponse


def _final_reply_for(match: ReplayMatch) -> str:
    if match.kind == "cancel":
        return (
            f"Preparation Agent 已查询订单并准备取消 {match.order_id}。"
            "请在右侧确认卡核对数据库中的规范预览；确认前不会执行。"
        )
    if match.kind == "return":
        return (
            f"Preparation Agent 已准备退货申请（{match.order_id} / "
            f"{match.order_item_id}）。"
            "此步骤不会直接退款；请核对确认卡后再确认。"
        )
    return (
        f"Preparation Agent 已准备将 {match.order_id} 换为 "
        f"{match.target_size} 码。"
        "库存预占只会在你确认后发生；请先核对确认卡。"
    )


def _scripted_model_for(match: ReplayMatch, *, final_reply: str) -> ScriptedModel:
    """Multi-round trajectory: lookup → eligibility → single prepare → final text."""

    order_id = match.order_id
    if match.kind == "cancel":
        return ScriptedModel(
            tool_turn(
                "get_order",
                json.dumps({"order_id": order_id}, ensure_ascii=False),
                call_id="demo-script-get-order",
            ),
            tool_turn(
                "check_action_eligibility",
                json.dumps(
                    {"action_type": "CANCEL_ORDER", "order_id": order_id},
                    ensure_ascii=False,
                ),
                call_id="demo-script-eligibility",
            ),
            tool_turn(
                "prepare_cancel_order",
                json.dumps({"order_id": order_id}, ensure_ascii=False),
                call_id="demo-script-prepare-cancel",
            ),
            final_turn(final_reply),
        )
    if match.kind == "return":
        assert match.order_item_id is not None
        payload = {
            "order_id": order_id,
            "order_item_id": match.order_item_id,
            "declared_condition": "NEW_UNWORN",
            "issue_type": "CHANGED_MIND",
        }
        return ScriptedModel(
            tool_turn(
                "get_order",
                json.dumps({"order_id": order_id}, ensure_ascii=False),
                call_id="demo-script-get-order",
            ),
            tool_turn(
                "check_action_eligibility",
                json.dumps(
                    {"action_type": "RETURN_ITEM", **payload},
                    ensure_ascii=False,
                ),
                call_id="demo-script-eligibility",
            ),
            tool_turn(
                "prepare_return",
                json.dumps(payload, ensure_ascii=False),
                call_id="demo-script-prepare-return",
            ),
            final_turn(final_reply),
        )
    if match.kind == "exchange":
        assert match.order_item_id is not None
        assert match.target_size is not None
        payload = {
            "order_id": order_id,
            "order_item_id": match.order_item_id,
            "target_size": match.target_size,
            "declared_condition": "NEW_UNWORN",
            "issue_type": "SIZE_MISMATCH",
        }
        return ScriptedModel(
            tool_turn(
                "get_order",
                json.dumps({"order_id": order_id}, ensure_ascii=False),
                call_id="demo-script-get-order",
            ),
            tool_turn(
                "check_action_eligibility",
                json.dumps(
                    {"action_type": "EXCHANGE_ITEM", **payload},
                    ensure_ascii=False,
                ),
                call_id="demo-script-eligibility",
            ),
            tool_turn(
                "prepare_exchange",
                json.dumps(payload, ensure_ascii=False),
                call_id="demo-script-prepare-exchange",
            ),
            final_turn(final_reply),
        )
    raise ServiceError(
        "DEMO_SCRIPT_UNKNOWN",
        "未知 scripted Preparation 场景。",
        status_code=500,
    )


def project_tool_trace(trace: tuple[ToolTrace, ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in trace:
        if item.success:
            summary = f"{item.tool_name} 成功"
        else:
            summary = f"{item.tool_name} 失败"
            if item.error_code:
                summary = f"{summary}（{item.error_code}）"
        items.append(
            {
                "tool_name": item.tool_name,
                "success": item.success,
                "summary": summary,
            }
        )
    return items


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
        handoff={
            "reason": "prepare_limit",
            "category": "SESSION_LIMIT",
            "summary": "本会话准备次数达到上限，自动转人工跟进。",
            "order_id": match.order_id,
        },
    )
    final_reply = _final_reply_for(match)
    model = _scripted_model_for(match, final_reply=final_reply)
    agent = build_preparation_agent(
        model=model,
        tools=session.tools,
        max_tool_rounds=session.settings.agent_max_tool_rounds,
        max_tool_calls=session.settings.agent_max_tool_calls,
    )
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
    session.last_tool_trace = project_tool_trace(result.tool_trace)
    session.pending_slot = None
    return result.final_text or final_reply, prepared
