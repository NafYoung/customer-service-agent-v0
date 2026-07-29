from __future__ import annotations

from datetime import timedelta

from app.domain.rules import (
    check_cancel_eligibility,
    check_exchange_eligibility,
    check_return_eligibility,
)
from app.enums import (
    EligibilityReason,
    IssueType,
    ItemCondition,
    OrderStatus,
)
from app.utils import utcnow


def test_cancel_only_before_shipping():
    assert check_cancel_eligibility(OrderStatus.PAID).allowed is True
    denied = check_cancel_eligibility(OrderStatus.SHIPPED)
    assert denied.allowed is False
    assert denied.reason_code == EligibilityReason.ORDER_ALREADY_SHIPPED


def test_return_window_is_enforced():
    now = utcnow()
    allowed = check_return_eligibility(
        order_status=OrderStatus.DELIVERED,
        delivered_at=now - timedelta(days=3),
        is_final_sale=False,
        declared_condition=ItemCondition.NEW_UNWORN,
        issue_type=IssueType.CHANGED_MIND,
        has_active_request=False,
        now=now,
    )
    expired = check_return_eligibility(
        order_status=OrderStatus.DELIVERED,
        delivered_at=now - timedelta(days=8),
        is_final_sale=False,
        declared_condition=ItemCondition.NEW_UNWORN,
        issue_type=IssueType.CHANGED_MIND,
        has_active_request=False,
        now=now,
    )
    assert allowed.allowed is True
    assert expired.reason_code == EligibilityReason.RETURN_WINDOW_EXPIRED


def test_defect_is_routed_to_human_review():
    now = utcnow()
    decision = check_return_eligibility(
        order_status=OrderStatus.DELIVERED,
        delivered_at=now - timedelta(days=1),
        is_final_sale=False,
        declared_condition=ItemCondition.DAMAGED,
        issue_type=IssueType.DEFECTIVE,
        has_active_request=False,
        now=now,
    )
    assert decision.allowed is False
    assert decision.reason_code == EligibilityReason.HUMAN_REVIEW_REQUIRED


def test_exchange_requires_real_inventory_and_new_size():
    now = utcnow()
    no_stock = check_exchange_eligibility(
        order_status=OrderStatus.DELIVERED,
        delivered_at=now - timedelta(days=1),
        current_size="42",
        target_size="44",
        target_inventory_qty=0,
        is_final_sale=False,
        declared_condition=ItemCondition.NEW_UNWORN,
        issue_type=IssueType.SIZE_MISMATCH,
        has_active_request=False,
        now=now,
    )
    unchanged = check_exchange_eligibility(
        order_status=OrderStatus.DELIVERED,
        delivered_at=now - timedelta(days=1),
        current_size="42",
        target_size="42",
        target_inventory_qty=10,
        is_final_sale=False,
        declared_condition=ItemCondition.NEW_UNWORN,
        issue_type=IssueType.SIZE_MISMATCH,
        has_active_request=False,
        now=now,
    )
    assert no_stock.reason_code == EligibilityReason.OUT_OF_STOCK
    assert unchanged.reason_code == EligibilityReason.TARGET_SIZE_UNCHANGED
