from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.demo import (
    DEMO_AGENT_MODE_OFFLINE_REPLAY,
    DEMO_AGENT_MODE_PREPARATION_LIVE,
    DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
)
from app.demo.matches import ReplayMatch, normalized
from app.demo.session import DemoSession, bump_or_limit, tool_context
from app.demo.slots import continue_pending_slot, detect_incomplete_intent
from app.enums import IssueType, ItemCondition
from app.errors import ValidationError
from app.schemas import PrepareActionResponse


@dataclass(frozen=True)
class MessageOutcome:
    reply: str
    has_pending: bool
    tool_trace: tuple[dict[str, Any], ...] = ()
    provider_http_delta: int = 0


def match_offline_replay(message: str) -> ReplayMatch | None:
    """Map a small set of Chinese demo intents to deterministic prepare paths."""

    text = normalized(message)
    upper = message.upper()

    if "取消" in text and ("ORD-1001" in upper or "1001" in upper or "德训" in text):
        return ReplayMatch(
            kind="cancel",
            order_id="ORD-1001",
            reply=(
                "已根据离线演示脚本准备取消订单 ORD-1001"
                "（依据 POL-CANCEL-001 v0.1）。"
                "请在右侧确认卡核对数据库中的规范预览；确认前不会执行。"
            ),
        )
    if "换货" in text or ("换成" in text and "码" in text):
        # Require an explicit size token for complete exchange match.
        if not re.search(r"\d{2}\s*码|[：:]\s*\d{2}|\d{2}$", message):
            if "换" in text and "码" not in text and "43" not in message:
                return None
        size_match = re.search(r"(\d{2})\s*码|换成\s*(\d{2})|(\d{2})$", message)
        target = "43"
        if size_match:
            target = next(g for g in size_match.groups() if g)
        return ReplayMatch(
            kind="exchange",
            order_id="ORD-1003",
            order_item_id="ITEM-1003-A",
            target_size=target,
            reply=(
                f"已根据离线演示脚本准备将 ORD-1003 的德训鞋换为 {target} 码"
                "（依据 POL-EXCHANGE-001 v0.1）。"
                "库存预占只会在你确认后发生；请先核对确认卡。"
            ),
        )
    if ("退货" in text or "退款" in text) and (
        "ORD-1003" in upper or "1003" in upper or "ITEM" in upper or len(text) > 4
    ):
        # Short bare 「退货」goes to slot fill; longer phrases with order still match.
        if text in {"退货", "退款", "我要退货", "想退货", "我想退货"}:
            return None
        return ReplayMatch(
            kind="return",
            order_id="ORD-1003",
            order_item_id="ITEM-1003-A",
            reply=(
                "已根据离线演示脚本准备退货申请（ORD-1003 / ITEM-1003-A）"
                "（依据 POL-RETURN-001 v0.1）。"
                "此步骤不会直接退款；请核对确认卡后再确认。"
            ),
        )
    return None


UNSUPPORTED_REPLY = (
    "当前演示支持固定售后场景（scripted）或本地 live DeepSeek。"
    "公开演示路径不会调用在线模型。\n"
    "可尝试：\n"
    "• 取消订单 ORD-1001\n"
    "• 退货 / 退货 ORD-1003\n"
    "• 把 ORD-1003 换成 43 码\n"
    "• 查一下我的订单\n"
    "确认卡只渲染服务端数据库中的 canonical preview。"
)


LOOKUP_REPLY = (
    "演示客户「林帆」常用订单：\n"
    "• ORD-1001 — 已支付，可取消\n"
    "• ORD-1003 — 已签收，可退货/换货（ITEM-1003-A）\n"
    "请直接说出要准备的操作。"
)


MODE_LABELS = {
    DEMO_AGENT_MODE_OFFLINE_REPLAY: "离线脚本 · 零密钥",
    DEMO_AGENT_MODE_PREPARATION_SCRIPTED: "Preparation Agent · scripted",
    DEMO_AGENT_MODE_PREPARATION_LIVE: "Preparation Agent · live DeepSeek",
}


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode, mode)


def run_offline_prepare(
    session: DemoSession,
    db: Session,
    match: ReplayMatch,
) -> PrepareActionResponse:
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
    tool_call_id = f"demo-tool-{uuid.uuid4().hex[:12]}"
    context = tool_context(session, tool_call_id=tool_call_id)
    tools = session.tools
    if match.kind == "cancel":
        prepared = tools.prepare_cancel_order(
            db,
            order_id=match.order_id,
            user_note=None,
            context=context,
        )
    elif match.kind == "return":
        assert match.order_item_id is not None
        prepared = tools.prepare_return(
            db,
            order_id=match.order_id,
            order_item_id=match.order_item_id,
            declared_condition=ItemCondition.NEW_UNWORN,
            issue_type=IssueType.CHANGED_MIND,
            user_note=None,
            context=context,
        )
    elif match.kind == "exchange":
        assert match.order_item_id is not None
        assert match.target_size is not None
        prepared = tools.prepare_exchange(
            db,
            order_id=match.order_id,
            order_item_id=match.order_item_id,
            target_size=match.target_size,
            declared_condition=ItemCondition.NEW_UNWORN,
            issue_type=IssueType.SIZE_MISMATCH,
            user_note=None,
            context=context,
        )
    else:
        raise ValidationError("DEMO_REPLAY_UNKNOWN", "未知离线脚本。")

    session.pending_approval_id = prepared.approval_id
    session.pending_preview_hash = prepared.preview_hash
    session.pending_ui_event_id = f"demo-ui-{uuid.uuid4().hex}"
    session.last_tool_trace = [
        {
            "tool_name": (
                "prepare_cancel_order"
                if match.kind == "cancel"
                else f"prepare_{match.kind}"
            ),
            "success": True,
            "summary": f"直调 prepare（{match.kind}）成功",
        }
    ]
    return prepared


def _prepare_from_match(
    session: DemoSession,
    message: str,
    match: ReplayMatch,
) -> MessageOutcome:
    mode = session.settings.demo_agent_mode
    if mode == DEMO_AGENT_MODE_PREPARATION_SCRIPTED:
        from app.demo.preparation_runner import run_preparation_scripted

        reply, _prepared = run_preparation_scripted(
            session,
            message=message,
            match=match,
        )
        return MessageOutcome(
            reply=reply,
            has_pending=True,
            tool_trace=tuple(session.last_tool_trace),
        )
    if mode != DEMO_AGENT_MODE_OFFLINE_REPLAY:
        raise ValidationError(
            "DEMO_AGENT_MODE_UNSUPPORTED",
            f"不支持的 DEMO_AGENT_MODE: {mode}",
        )
    with session.database.session() as db:
        run_offline_prepare(session, db, match)
    return MessageOutcome(
        reply=match.reply or f"已准备 {match.kind}（{match.order_id}）。",
        has_pending=True,
        tool_trace=tuple(session.last_tool_trace),
    )


def handle_message(session: DemoSession, message: str) -> MessageOutcome:
    bump_or_limit(
        session,
        counter="message_count",
        limit=session.settings.demo_max_messages_per_session,
        code="DEMO_MESSAGE_LIMIT",
        message="本会话消息条数已达上限，请重置演示。",
        handoff={
            "reason": "message_limit",
            "category": "SESSION_LIMIT",
            "summary": "本会话消息条数达到上限，自动转人工跟进。",
        },
    )
    mode = session.settings.demo_agent_mode
    session.last_tool_trace = []

    if mode == DEMO_AGENT_MODE_PREPARATION_LIVE:
        from app.demo.live_runner import run_preparation_live

        reply, has_pending, provider_delta = run_preparation_live(
            session, message=message
        )
        return MessageOutcome(
            reply=reply,
            has_pending=has_pending,
            tool_trace=tuple(session.last_tool_trace),
            provider_http_delta=provider_delta,
        )

    clarify, slot_match = continue_pending_slot(session, message)
    if clarify is not None:
        return MessageOutcome(reply=clarify, has_pending=False)
    if slot_match is not None:
        return _prepare_from_match(session, message, slot_match)

    incomplete = detect_incomplete_intent(message)
    if incomplete is not None:
        session.pending_slot = incomplete
        return MessageOutcome(reply=incomplete.prompt, has_pending=False)

    match = match_offline_replay(message)
    if match is not None:
        return _prepare_from_match(session, message, match)

    text = normalized(message)
    if "订单" in text or "查" in text:
        return MessageOutcome(reply=LOOKUP_REPLY, has_pending=False)
    return MessageOutcome(reply=UNSUPPORTED_REPLY, has_pending=False)


SUPPORTED_SCENARIOS: tuple[str, ...] = (
    "取消订单 ORD-1001",
    "我想退货",
    "退货 ORD-1003",
    "把 ORD-1003 换成 43 码",
    "查一下我的订单",
)
