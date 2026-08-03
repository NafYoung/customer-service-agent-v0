"""Shared demo intent match types (avoid replay ↔ slots circular imports)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

PrepareKind = Literal["cancel", "return", "exchange"]


@dataclass(frozen=True)
class ReplayMatch:
    kind: PrepareKind
    reply: str
    order_id: str
    order_item_id: str | None = None
    target_size: str | None = None


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", text.casefold())
