"""Local-only live Preparation Agent runner (DeepSeek + budget ledger)."""

from __future__ import annotations

import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

from app.agent.deepseek_budget import (
    BudgetExceededError,
    DeepSeekBudgetGuard,
    SQLiteBudgetLedger,
)
from app.agent.factory import build_deepseek_client, build_preparation_agent
from app.agent.openai_compatible import ModelAPIError
from app.agent.readonly import AgentRunError
from app.demo.preparation_runner import project_tool_trace
from app.demo.security import token_hash
from app.demo.session import (
    DemoSession,
    bump_or_limit,
    ensure_handoff_ticket,
    tool_context,
)
from app.errors import ServiceError
from evals.canonical_pricing import load_canonical_price_snapshot

_LEDGER_DIR = Path(tempfile.gettempdir()) / "rivet-demo-live-budget"

_ACTION_INTENT_MARKERS = ("取消", "退货", "退款", "换货", "换成", "调换")


def _resolve_live_model(session: DemoSession, message: str) -> str:
    """Model routing reservation: action intent vs plain query.

    Empty settings fall back to the canonical deepseek_model; routing to a
    different model fails closed at the budget guard unless that model has its
    own pricing snapshot.
    """

    settings = session.settings
    if any(marker in message for marker in _ACTION_INTENT_MARKERS):
        return settings.demo_live_action_model or settings.deepseek_model
    return settings.demo_live_query_model or settings.deepseek_model


def _live_budget_guard(session: DemoSession) -> DeepSeekBudgetGuard:
    settings = session.settings
    _LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger_path = _LEDGER_DIR / f"{session.server_run_id}.sqlite3"
    return DeepSeekBudgetGuard(
        ledger=SQLiteBudgetLedger(
            path=ledger_path,
            hard_limit_cny=Decimal("20"),
            execution_limit_cny=Decimal("18"),
        ),
        # Ledger run_id 只允许小写字母/数字/._-：server_run_id 来自
        # token_urlsafe（含大写），必须派生为小写哈希。
        run_id=f"demo-live-{token_hash(session.server_run_id)[:16]}",
        purpose="diagnostic",
        price_snapshot=load_canonical_price_snapshot(),
        model=settings.deepseek_model,
        max_output_tokens=settings.deepseek_max_tokens,
    )


def run_preparation_live(
    session: DemoSession, *, message: str
) -> tuple[str, bool, int]:
    """Run live PreparationAgent; may prepare zero or one pending action.

    Returns (reply, has_pending, provider_http_delta).
    """

    if not session.settings.deepseek_api_key:
        raise ServiceError(
            "DEMO_LIVE_KEY_MISSING",
            "preparation_live 需要配置 DEEPSEEK_API_KEY（仅本地；public_demo 禁止）。",
            status_code=503,
        )

    if (
        session.live_attempt_count
        >= session.settings.demo_max_live_attempts_per_session
    ):
        ticket_id = ensure_handoff_ticket(
            session,
            reason="conversation_budget",
            category="CONVERSATION_BUDGET",
            summary=(
                "本会话 live 模型调用次数达到每会话软闸门上限，"
                "自动转人工跟进。"
            ),
        )
        return (
            f"本会话模型调用次数已达软闸门上限，已生成人工工单 {ticket_id}，"
            "后续由人工客服跟进。",
            False,
            0,
        )

    guard = _live_budget_guard(session)
    model_name = _resolve_live_model(session, message)
    model = build_deepseek_client(
        session.settings,
        budget_guard=guard,
        model=model_name,
    )
    agent = build_preparation_agent(
        model=model,
        tools=session.tools,
        max_tool_rounds=session.settings.agent_max_tool_rounds,
        max_tool_calls=session.settings.agent_max_tool_calls,
    )
    context = tool_context(session)
    history = list(session.chat_history)
    try:
        with session.database.session() as db:
            result = agent.run(
                db,
                user_text=message,
                context=context,
                history=history,
            )
    except AgentRunError as exc:
        raise ServiceError(exc.code, str(exc), status_code=409) from exc
    except Exception as exc:  # noqa: BLE001 — surface provider/budget failures
        if isinstance(exc, BudgetExceededError) or (
            isinstance(exc, ModelAPIError)
            and getattr(exc, "code", None) == "MODEL_BUDGET_EXHAUSTED"
        ):
            ensure_handoff_ticket(
                session,
                reason="budget_exhausted",
                category="BUDGET_EXHAUSTED",
                summary="预算闸门拒绝付费请求（预算耗尽），已停止模型调用并转人工跟进。",
            )
            raise ServiceError(
                "DEMO_LIVE_BUDGET_EXHAUSTED",
                "预算耗尽：已停止模型调用并生成人工工单，后续由人工客服跟进。",
                status_code=409,
            ) from exc
        raise ServiceError(
            "DEMO_LIVE_AGENT_FAILED",
            f"Live Preparation Agent 失败：{exc}",
            status_code=502,
        ) from exc
    finally:
        close = getattr(model, "close", None)
        if callable(close):
            close()

    session.chat_history.append({"role": "user", "content": message})
    session.chat_history.append(
        {"role": "assistant", "content": result.final_text or ""}
    )
    # Cap history to keep live prompts bounded.
    if len(session.chat_history) > 12:
        session.chat_history = session.chat_history[-12:]
    session.last_tool_trace = project_tool_trace(result.tool_trace)
    provider_delta = len(result.model_turns)
    session.live_attempt_count += provider_delta

    prepared = result.prepared_action
    if prepared is None:
        return (
            result.final_text or "已处理，当前没有待确认操作。",
            False,
            provider_delta,
        )

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
            "order_id": (
                str(prepared.preview["order_id"])
                if prepared.preview.get("order_id") is not None
                else None
            ),
        },
    )
    session.pending_approval_id = prepared.approval_id
    session.pending_preview_hash = prepared.preview_hash
    session.pending_ui_event_id = f"demo-ui-{uuid.uuid4().hex}"
    session.pending_slot = None
    return (
        result.final_text or "已生成待确认预览，请核对确认卡。",
        True,
        provider_delta,
    )
