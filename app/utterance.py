"""One reader for order id, target size, and intent in a customer sentence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Intent = Literal["cancel", "return", "exchange"]

_ORDER_RE = re.compile(r"ORD-?\s*(\d{4})", re.IGNORECASE)
_SIZE_RE = re.compile(
    r"(?:目标尺码|想换成|换到|换成|尺码)\s*[：:]?\s*"
    r"(\d{2}(?!\d)|[A-Za-z]|[一二三四五六七八九十]+)"
    r"|(\d+)\s*码"
    r"|([A-Za-z])\s*码",
    re.IGNORECASE,
)
_EXCHANGE_MARKERS = ("换货", "换成", "换到", "调换")
_RETURN_MARKERS = ("退货", "退款")


@dataclass(frozen=True)
class CustomerUtterance:
    order_id: str | None
    target_size: str | None
    intent: Intent | None
    has_target_size: bool


def parse_customer_utterance(text: str) -> CustomerUtterance:
    """Return the order id, target size, and intent written in `text`."""

    sentence = text.strip()
    target_size, has_target_size = _read_size(sentence)
    return CustomerUtterance(
        order_id=_order_id(sentence),
        target_size=target_size,
        intent=_intent(sentence),
        has_target_size=has_target_size,
    )


def _order_id(text: str) -> str | None:
    match = _ORDER_RE.search(text)
    if match is None:
        return None
    return f"ORD-{match.group(1)}"


def _prepare_size(token: str) -> str | None:
    if token.isdigit():
        return token
    if len(token) == 1 and token.isascii() and token.isalpha():
        return token
    return None


def _read_size(text: str) -> tuple[str | None, bool]:
    labeled_size: str | None = None
    saw_labeled = False
    bare_size: str | None = None
    for match in _SIZE_RE.finditer(text):
        labeled = match.group(1)
        if labeled is not None:
            saw_labeled = True
            if labeled_size is None:
                labeled_size = _prepare_size(labeled)
            continue
        if bare_size is None:
            bare = match.group(2) or match.group(3)
            if bare is not None:
                bare_size = _prepare_size(bare)
    if labeled_size is not None:
        return labeled_size, True
    if bare_size is not None:
        return bare_size, True
    if saw_labeled:
        return None, True
    return None, False


def _intent(text: str) -> Intent | None:
    if any(marker in text for marker in _EXCHANGE_MARKERS):
        return "exchange"
    if any(marker in text for marker in _RETURN_MARKERS):
        return "return"
    if "取消" in text:
        return "cancel"
    return None
