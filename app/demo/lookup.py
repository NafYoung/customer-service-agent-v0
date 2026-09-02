"""Scripted inventory / policy lookups for the public demo (no prepare)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from app.demo import (
    DEMO_AGENT_MODE_OFFLINE_REPLAY,
    DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
)
from app.demo.matches import normalized
from app.demo.preparation_runner import project_tool_trace
from app.demo.session import DemoSession, tool_context
from app.errors import ServiceError
from app.schemas import PolicySearchRequest

KNOWN_SKUS = (
    "GAT-WHITE",
    "GAT-BLACK",
    "HOODIE-GRAY",
    "TEE-BLACK",
    "ARCHIVE-JACKET",
)

LookupKind = Literal["inventory", "policy"]


@dataclass(frozen=True)
class LookupMatch:
    kind: LookupKind
    sku: str | None = None
    size: str | None = None
    query: str = ""


def match_scripted_lookup(message: str) -> LookupMatch | None:
    """Route stock and policy questions away from prepare matchers.

    Policy must be detected before the return matcher, because phrases like
    「查一下退货政策」contain 退货 and would otherwise create a pending preview.
    Inventory must not steal 换货 / 取消 intents.
    """

    text = normalized(message)
    upper = message.upper()
    if "政策" in text and "ORD-" not in upper:
        if "退货" in text or "退款" in text:
            query = "退货政策"
        elif "换货" in text or "换码" in text:
            query = "换货政策"
        elif "身份" in text or "访问" in text:
            query = "身份"
        else:
            query = "政策"
        return LookupMatch(kind="policy", query=query)

    if any(token in text for token in ("取消", "退货", "退款", "换货", "换成")):
        return None
    sku = next((item for item in KNOWN_SKUS if item in upper), None)
    if sku is None:
        return None
    stock_intent = "库存" in text or "有货" in text or "SKU" in upper
    if not stock_intent:
        return None
    size_match = re.search(r"(\d{2})\s*码", message)
    if size_match is None:
        return None
    return LookupMatch(kind="inventory", sku=sku, size=size_match.group(1))


def _available_qty(payload: object) -> int:
    if isinstance(payload, dict):
        return int(payload.get("available_qty") or 0)
    return int(getattr(payload, "available_qty", 0) or 0)


def _policy_hits(payload: object) -> list[object]:
    if isinstance(payload, dict):
        return list(payload.get("hits") or [])
    return list(getattr(payload, "hits", []) or [])


def _hit_field(hit: object, name: str, default: str) -> str:
    if isinstance(hit, dict):
        return str(hit.get(name) or default)
    return str(getattr(hit, name, default) or default)


def _inventory_reply(sku: str, size: str, qty: int) -> str:
    return (
        f"{sku} {size} 码当前可售 {qty} 件。"
        "此查询只调用 get_inventory，没有拉订单列表，也没有准备写操作。"
    )


def _policy_reply(*, policy_id: str, version: str, injected: bool) -> str:
    refusal = (
        "检索到的政策文本是数据，不是指令；不会泄露系统提示，也不会调用隐藏工具。"
        if injected
        else "检索到的政策文本是数据，不是可执行指令。"
    )
    return f"已检索 {policy_id} {version}。{refusal}"


def _run_scripted_lookup(session: DemoSession, message: str, match: LookupMatch) -> str:
    from app.agent.factory import build_preparation_agent
    from app.agent.readonly import AgentRunError
    from app.agent.scripted import ScriptedModel, final_turn, tool_turn

    if match.kind == "inventory":
        assert match.sku is not None and match.size is not None
        arguments = json.dumps(
            {"sku": match.sku, "size": match.size}, ensure_ascii=False
        )
        model = ScriptedModel(
            tool_turn(
                "get_inventory",
                arguments,
                call_id="demo-script-get-inventory",
            ),
            final_turn(_inventory_reply(match.sku, match.size, qty=0)),
        )
    else:
        arguments = json.dumps(
            {
                "query": match.query,
                "region": "CN",
                "channel": "ONLINE",
                "top_k": 3,
            },
            ensure_ascii=False,
        )
        injected = any(
            token in message for token in ("泄露", "系统提示", "隐藏工具", "照做")
        )
        model = ScriptedModel(
            tool_turn(
                "search_policy",
                arguments,
                call_id="demo-script-search-policy",
            ),
            final_turn(
                _policy_reply(
                    policy_id="POL-RETURN-001",
                    version="v0.1",
                    injected=injected,
                )
            ),
        )

    agent = build_preparation_agent(
        model=model,
        tools=session.tools,
        max_tool_rounds=session.settings.agent_max_tool_rounds,
        max_tool_calls=session.settings.agent_max_tool_calls,
    )
    try:
        with session.database.session() as db:
            result = agent.run(
                db,
                user_text=message,
                context=tool_context(session),
            )
    except AgentRunError as exc:
        raise ServiceError(exc.code, str(exc), status_code=409) from exc

    if result.prepared_action is not None:
        raise ServiceError(
            "DEMO_LOOKUP_PREPARE_FORBIDDEN",
            "查询路径不得写出待确认预览。",
            status_code=500,
        )

    session.last_tool_trace = project_tool_trace(result.tool_trace)
    session.pending_slot = None
    if match.kind == "inventory":
        qty = 0
        inventory_trace = next(
            (item for item in result.tool_trace if item.tool_name == "get_inventory"),
            None,
        )
        if inventory_trace is not None and inventory_trace.success:
            qty = _available_qty(inventory_trace.result)
        assert match.sku is not None and match.size is not None
        return _inventory_reply(match.sku, match.size, qty)
    hits = []
    policy_trace = next(
        (item for item in result.tool_trace if item.tool_name == "search_policy"),
        None,
    )
    if policy_trace is not None and policy_trace.success:
        hits = _policy_hits(policy_trace.result)
    injected = any(
        token in message for token in ("泄露", "系统提示", "隐藏工具", "照做")
    )
    if hits:
        first = hits[0]
        return _policy_reply(
            policy_id=_hit_field(first, "policy_id", "POL-RETURN-001"),
            version=_hit_field(first, "version", "v0.1"),
            injected=injected,
        )
    return result.final_text or _policy_reply(
        policy_id="POL-RETURN-001",
        version="v0.1",
        injected=injected,
    )


def _run_offline_lookup(
    session: DemoSession, message: str, match: LookupMatch
) -> str:
    context = tool_context(session)
    injected = any(
        token in message for token in ("泄露", "系统提示", "隐藏工具", "照做")
    )
    with session.database.session() as db:
        if match.kind == "inventory":
            assert match.sku is not None and match.size is not None
            inventory = session.tools.get_inventory(
                db,
                sku=match.sku,
                size=match.size,
                context=context,
            )
            session.last_tool_trace = [
                {
                    "tool_name": "get_inventory",
                    "success": True,
                    "summary": "get_inventory 成功",
                }
            ]
            return _inventory_reply(
                match.sku, match.size, inventory.available_qty
            )
        response = session.tools.search_policy(
            db,
            request=PolicySearchRequest(query=match.query),
            context=context,
        )
        session.last_tool_trace = [
            {
                "tool_name": "search_policy",
                "success": True,
                "summary": "search_policy 成功",
            }
        ]
    if response.hits:
        return _policy_reply(
            policy_id=response.hits[0].policy_id,
            version=response.hits[0].version,
            injected=injected,
        )
    return _policy_reply(
        policy_id="POL-RETURN-001", version="v0.1", injected=injected
    )


def run_lookup(
    session: DemoSession,
    *,
    message: str,
    match: LookupMatch,
) -> str:
    mode = session.settings.demo_agent_mode
    if mode == DEMO_AGENT_MODE_PREPARATION_SCRIPTED:
        return _run_scripted_lookup(session, message, match)
    if mode == DEMO_AGENT_MODE_OFFLINE_REPLAY:
        return _run_offline_lookup(session, message, match)
    raise ServiceError(
        "DEMO_AGENT_MODE_UNSUPPORTED",
        f"查询路径不支持 DEMO_AGENT_MODE={mode}。",
        status_code=400,
    )
