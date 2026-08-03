from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.demo import (
    DEMO_AGENT_MODE_OFFLINE_REPLAY,
    DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
)
from app.demo.session import DemoSession, bump_or_limit, tool_context
from app.enums import IssueType, ItemCondition
from app.errors import ValidationError
from app.schemas import PrepareActionResponse

PrepareKind = Literal["cancel", "return", "exchange"]


@dataclass(frozen=True)
class ReplayMatch:
    kind: PrepareKind
    reply: str
    order_id: str
    order_item_id: str | None = None
    target_size: str | None = None


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text.casefold())


def match_offline_replay(message: str) -> ReplayMatch | None:
    """Map a small set of Chinese demo intents to deterministic prepare paths."""

    text = _normalized(message)
    upper = message.upper()

    if "取消" in text and ("ORD-1001" in upper or "1001" in upper or "德训" in text):
        return ReplayMatch(
            kind="cancel",
            order_id="ORD-1001",
            reply=(
                "已根据离线演示脚本准备取消订单 ORD-1001。"
                "请在右侧确认卡核对数据库中的规范预览；确认前不会执行。"
            ),
        )
    if "换货" in text or ("换成" in text and "码" in text):
        return ReplayMatch(
            kind="exchange",
            order_id="ORD-1003",
            order_item_id="ITEM-1003-A",
            target_size="43",
            reply=(
                "已根据离线演示脚本准备将 ORD-1003 的德训鞋换为 43 码。"
                "库存预占只会在你确认后发生；请先核对确认卡。"
            ),
        )
    if "退货" in text or "退款" in text:
        return ReplayMatch(
            kind="return",
            order_id="ORD-1003",
            order_item_id="ITEM-1003-A",
            reply=(
                "已根据离线演示脚本准备退货申请（ORD-1003 / ITEM-1003-A）。"
                "此步骤不会直接退款；请核对确认卡后再确认。"
            ),
        )
    return None


UNSUPPORTED_REPLY = (
    "当前公开演示仅支持固定场景，不会调用在线模型。\n"
    "可尝试：\n"
    "• 取消订单 ORD-1001\n"
    "• 退货 ORD-1003\n"
    "• 把 ORD-1003 换成 43 码\n"
    "确认卡只渲染服务端数据库中的 canonical preview。"
)


LOOKUP_REPLY = (
    "演示客户「林帆」常用订单：\n"
    "• ORD-1001 — 已支付，可取消\n"
    "• ORD-1003 — 已签收，可退货/换货（ITEM-1003-A）\n"
    "请直接说出要准备的操作；公开模式不会联网调用 DeepSeek。"
)


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
    return prepared


def handle_message(session: DemoSession, message: str) -> tuple[str, bool]:
    bump_or_limit(
        session,
        counter="message_count",
        limit=session.settings.demo_max_messages_per_session,
        code="DEMO_MESSAGE_LIMIT",
        message="本会话消息条数已达上限，请重置演示。",
    )
    match = match_offline_replay(message)
    mode = session.settings.demo_agent_mode
    if match is not None:
        if mode == DEMO_AGENT_MODE_PREPARATION_SCRIPTED:
            from app.demo.preparation_runner import run_preparation_scripted

            reply, _prepared = run_preparation_scripted(
                session,
                message=message,
                match=match,
            )
            return reply, True
        if mode != DEMO_AGENT_MODE_OFFLINE_REPLAY:
            raise ValidationError(
                "DEMO_AGENT_MODE_UNSUPPORTED",
                f"不支持的 DEMO_AGENT_MODE: {mode}",
            )
        with session.database.session() as db:
            run_offline_prepare(session, db, match)
        return match.reply, True

    text = _normalized(message)
    if "订单" in text or "查" in text:
        return LOOKUP_REPLY, False
    return UNSUPPORTED_REPLY, False


SUPPORTED_SCENARIOS: tuple[str, ...] = (
    "取消订单 ORD-1001",
    "退货 ORD-1003",
    "换货 ORD-1003 → 43 码",
)
