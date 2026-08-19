from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.types import EligibilityDecision
from app.enums import (
    EligibilityReason,
    IssueType,
    ItemCondition,
    OrderStatus,
)

RETURN_WINDOW_DAYS = 7
# 决策审计快照记录的确定性规则代码版本；规则语义变更时必须递增。
RULES_VERSION = "v0.1"
_HUMAN_REVIEW_ISSUES = {
    IssueType.WRONG_ITEM,
    IssueType.DEFECTIVE,
    IssueType.DAMAGED_IN_TRANSIT,
}
_ALLOWED_AUTO_SERVICE_CONDITIONS = {
    ItemCondition.NEW_UNWORN,
    ItemCondition.OPENED_UNUSED,
}


def check_cancel_eligibility(order_status: OrderStatus) -> EligibilityDecision:
    if order_status == OrderStatus.CANCELLED:
        return EligibilityDecision(
            False,
            EligibilityReason.ORDER_ALREADY_CANCELLED,
            "该订单已经取消，不能重复操作。",
        )
    if order_status in {OrderStatus.SHIPPED, OrderStatus.DELIVERED}:
        return EligibilityDecision(
            False,
            EligibilityReason.ORDER_ALREADY_SHIPPED,
            "订单已经发货，不能直接取消。",
            available_alternative="签收后申请退货",
        )
    if order_status in {OrderStatus.PAID, OrderStatus.PROCESSING}:
        return EligibilityDecision(
            True,
            EligibilityReason.ELIGIBLE,
            "订单尚未发货，可以申请取消。",
        )
    return EligibilityDecision(
        False,
        EligibilityReason.INVALID_ACTION,
        "当前订单状态不支持取消。",
    )


def check_return_eligibility(
    *,
    order_status: OrderStatus,
    delivered_at: datetime | None,
    is_final_sale: bool,
    declared_condition: ItemCondition,
    issue_type: IssueType,
    has_active_request: bool,
    now: datetime,
) -> EligibilityDecision:
    if issue_type in _HUMAN_REVIEW_ISSUES:
        return EligibilityDecision(
            False,
            EligibilityReason.HUMAN_REVIEW_REQUIRED,
            "该问题需要人工核验商品情况和责任归属。",
            available_alternative="创建人工客服工单",
        )
    if order_status != OrderStatus.DELIVERED or delivered_at is None:
        return EligibilityDecision(
            False,
            EligibilityReason.ORDER_NOT_DELIVERED,
            "订单尚未签收，暂不能发起退货。",
        )
    if now > delivered_at + timedelta(days=RETURN_WINDOW_DAYS):
        return EligibilityDecision(
            False,
            EligibilityReason.RETURN_WINDOW_EXPIRED,
            f"该订单已超过 {RETURN_WINDOW_DAYS} 天自助退货期限。",
            available_alternative="创建人工客服工单",
        )
    if is_final_sale:
        return EligibilityDecision(
            False,
            EligibilityReason.FINAL_SALE,
            "该商品属于演示规则中的特价不退商品。",
        )
    if declared_condition not in _ALLOWED_AUTO_SERVICE_CONDITIONS:
        return EligibilityDecision(
            False,
            EligibilityReason.ITEM_CONDITION_NOT_ALLOWED,
            "商品当前状态不满足自助退货条件。",
            available_alternative="创建人工客服工单",
        )
    if has_active_request:
        return EligibilityDecision(
            False,
            EligibilityReason.ACTIVE_REQUEST_EXISTS,
            "该商品已经存在处理中的售后申请。",
        )
    return EligibilityDecision(
        True,
        EligibilityReason.ELIGIBLE,
        "该商品满足自助退货条件。",
    )


def check_exchange_eligibility(
    *,
    order_status: OrderStatus,
    delivered_at: datetime | None,
    current_size: str | None,
    target_size: str | None,
    target_inventory_qty: int,
    is_final_sale: bool,
    declared_condition: ItemCondition,
    issue_type: IssueType,
    has_active_request: bool,
    now: datetime,
) -> EligibilityDecision:
    if issue_type in _HUMAN_REVIEW_ISSUES:
        return EligibilityDecision(
            False,
            EligibilityReason.HUMAN_REVIEW_REQUIRED,
            "该问题需要人工核验，暂不支持自动换货。",
            available_alternative="创建人工客服工单",
        )
    if order_status != OrderStatus.DELIVERED or delivered_at is None:
        return EligibilityDecision(
            False,
            EligibilityReason.ORDER_NOT_DELIVERED,
            "订单尚未签收，暂不能发起换货。",
        )
    if now > delivered_at + timedelta(days=RETURN_WINDOW_DAYS):
        return EligibilityDecision(
            False,
            EligibilityReason.RETURN_WINDOW_EXPIRED,
            f"该订单已超过 {RETURN_WINDOW_DAYS} 天自助换货期限。",
            available_alternative="创建人工客服工单",
        )
    if is_final_sale:
        return EligibilityDecision(
            False,
            EligibilityReason.FINAL_SALE,
            "该商品属于演示规则中的特价不换商品。",
        )
    if declared_condition not in _ALLOWED_AUTO_SERVICE_CONDITIONS:
        return EligibilityDecision(
            False,
            EligibilityReason.ITEM_CONDITION_NOT_ALLOWED,
            "商品当前状态不满足自助换货条件。",
            available_alternative="创建人工客服工单",
        )
    if not target_size:
        return EligibilityDecision(
            False,
            EligibilityReason.TARGET_SIZE_REQUIRED,
            "需要先确认希望更换的目标尺码。",
        )
    if current_size == target_size:
        return EligibilityDecision(
            False,
            EligibilityReason.TARGET_SIZE_UNCHANGED,
            "目标尺码与原尺码相同。",
        )
    if target_inventory_qty <= 0:
        return EligibilityDecision(
            False,
            EligibilityReason.OUT_OF_STOCK,
            "目标尺码当前无可用库存。",
            available_alternative="选择其他尺码或申请退货",
        )
    if has_active_request:
        return EligibilityDecision(
            False,
            EligibilityReason.ACTIVE_REQUEST_EXISTS,
            "该商品已经存在处理中的售后申请。",
        )
    return EligibilityDecision(
        True,
        EligibilityReason.ELIGIBLE,
        "该商品满足自助换货条件，目标尺码有库存。",
    )
