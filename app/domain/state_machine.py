from __future__ import annotations

from app.enums import OrderStatus
from app.errors import ConflictError

_ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PAID: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
    OrderStatus.PROCESSING: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


def assert_order_transition(current: OrderStatus, target: OrderStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ConflictError(
            "INVALID_ORDER_TRANSITION",
            f"订单状态不能从 {current.value} 变更为 {target.value}",
            status_code=409,
        )
