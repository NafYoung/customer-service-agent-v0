"""Local-only live Preparation Agent runner (DeepSeek + budget ledger)."""

from __future__ import annotations

import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

from app.agent.deepseek_budget import DeepSeekBudgetGuard, SQLiteBudgetLedger
from app.agent.factory import build_deepseek_client, build_preparation_agent
from app.agent.readonly import AgentRunError
from app.demo.preparation_runner import project_tool_trace
from app.demo.session import DemoSession, bump_or_limit, tool_context
from app.errors import ServiceError
from evals.canonical_pricing import load_canonical_price_snapshot

_LEDGER_DIR = Path(tempfile.gettempdir()) / "rivet-demo-live-budget"


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
        run_id=f"demo-live-{session.server_run_id}",
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

    guard = _live_budget_guard(session)
    model = build_deepseek_client(session.settings, budget_guard=guard)
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
