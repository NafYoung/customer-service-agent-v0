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
_REQUEST_ALLOWED_FROM: set[OrderStatus] = {OrderStatus.DELIVERED}


def assert_order_transition(current: OrderStatus, target: OrderStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ConflictError(
            "INVALID_ORDER_TRANSITION",
            f"订单状态不能从 {current.value} 变更为 {target.value}",
            status_code=409,
        )


def assert_request_allowed_from_order_status(current: OrderStatus) -> None:
    """退货/换货申请只允许从已签收订单发起（与资格规则同源的第二道硬闸）。"""

    if current not in _REQUEST_ALLOWED_FROM:
        raise ConflictError(
            "ORDER_NOT_DELIVERED",
            f"订单当前状态 {current.value} 不允许发起退货或换货申请。",
            status_code=409,
        )
