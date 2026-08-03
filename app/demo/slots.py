"""Multi-turn slot filling for demo intents before prepare."""

from __future__ import annotations

import re

from app.demo.matches import PrepareKind, ReplayMatch, normalized
from app.demo.session import DemoSession, PendingSlot

_ORDER_RE = re.compile(r"ORD-?\s*(\d{4})", re.IGNORECASE)
_SIZE_RE = re.compile(
    r"(?:换成|换到|目标尺码|尺码)\s*[：:]?\s*(\d{2}|[A-Za-z])|"
    r"(\d{2})\s*码",
    re.IGNORECASE,
)

_DEFAULTS = {
    "cancel": {"order_id": "ORD-1001"},
    "return": {"order_id": "ORD-1003", "order_item_id": "ITEM-1003-A"},
    "exchange": {
        "order_id": "ORD-1003",
        "order_item_id": "ITEM-1003-A",
        "target_size": "43",
    },
}


def _extract_order_id(message: str) -> str | None:
    match = _ORDER_RE.search(message)
    if not match:
        return None
    return f"ORD-{match.group(1)}"


def _extract_size(message: str) -> str | None:
    match = _SIZE_RE.search(message)
    if not match:
        return None
    return next(group for group in match.groups() if group)


def _prompt_for(kind: PrepareKind, *, missing: str) -> str:
    if missing == "order_id":
        if kind == "cancel":
            return (
                "好的，要取消哪一笔订单？可回复订单号，例如 ORD-1001"
                "（已支付、未发货的德训鞋订单）。"
            )
        if kind == "return":
            return (
                "好的，要退哪一笔？可回复订单号，例如 ORD-1003"
                "（已签收，商品 ITEM-1003-A）。"
            )
        return (
            "好的，要换哪一笔？可回复订单号，例如 ORD-1003，"
            "并说明目标尺码（如 43）。"
        )
    if missing == "target_size":
        return "请补充目标尺码，例如「换成 43 码」。确认前不会预占库存。"
    return "还需要补充信息后才能准备操作。"


def detect_incomplete_intent(message: str) -> PendingSlot | None:
    """Return a slot-fill prompt when intent is clear but facts are missing."""

    text = normalized(message)
    order_id = _extract_order_id(message)
    size = _extract_size(message)

    if "取消" in text and not order_id and "1001" not in message.upper():
        return PendingSlot(
            kind="cancel",
            prompt=_prompt_for("cancel", missing="order_id"),
        )
    if ("换货" in text or ("换成" in text and "码" in text)) and not size:
        return PendingSlot(
            kind="exchange",
            order_id=order_id or "ORD-1003",
            order_item_id="ITEM-1003-A",
            prompt=_prompt_for("exchange", missing="target_size"),
        )
    if ("退货" in text or "退款" in text) and not order_id and "1003" not in message.upper():
        # Bare 「退货」without order → ask; 「退货 ORD-1003」handled by matcher.
        if "退" in text and len(text) <= 6:
            return PendingSlot(
                kind="return",
                prompt=_prompt_for("return", missing="order_id"),
            )
    return None


def continue_pending_slot(
    session: DemoSession,
    message: str,
) -> tuple[str | None, ReplayMatch | None]:
    """Consume a follow-up message against pending_slot.

    Returns (clarify_reply, match_ready_to_prepare).
    """

    slot = session.pending_slot
    if slot is None:
        return None, None

    order_id = _extract_order_id(message) or slot.order_id
    size = _extract_size(message) or slot.target_size
    text = normalized(message)

    if slot.kind == "cancel":
        if order_id is None and ("1001" in message.upper() or "德训" in text):
            order_id = "ORD-1001"
        if order_id is None:
            return _prompt_for("cancel", missing="order_id"), None
        session.pending_slot = None
        return None, ReplayMatch(
            kind="cancel",
            order_id=order_id,
            reply="",
        )

    if slot.kind == "return":
        if order_id is None and "1003" in message.upper():
            order_id = "ORD-1003"
        if order_id is None:
            return _prompt_for("return", missing="order_id"), None
        defaults = _DEFAULTS["return"]
        session.pending_slot = None
        return None, ReplayMatch(
            kind="return",
            order_id=order_id,
            order_item_id=defaults["order_item_id"],
            reply="",
        )

    if slot.kind == "exchange":
        if order_id is None:
            order_id = slot.order_id or "ORD-1003"
        if size is None:
            session.pending_slot = PendingSlot(
                kind="exchange",
                order_id=order_id,
                order_item_id="ITEM-1003-A",
                prompt=_prompt_for("exchange", missing="target_size"),
            )
            return session.pending_slot.prompt, None
        session.pending_slot = None
        return None, ReplayMatch(
            kind="exchange",
            order_id=order_id,
            order_item_id="ITEM-1003-A",
            target_size=size,
            reply="",
        )

    session.pending_slot = None
    return "当前补问状态无效，请重新说明要取消、退货还是换货。", None
