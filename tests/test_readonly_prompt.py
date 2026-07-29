from __future__ import annotations

from pathlib import Path

PROMPT = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "agent"
    / "readonly_system_prompt.md"
).read_text(encoding="utf-8")


def test_prompt_routes_explicit_eligibility_requests_directly():
    required_guidance = (
        "call `check_action_eligibility` directly",
        "Do not call `get_customer_orders` when an explicit order ID is present",
        "Do not call `get_order`, `get_inventory`, or `search_policy` first",
        "authoritative for ownership, order state, return window, final-sale, and inventory checks",
    )
    for statement in required_guidance:
        assert statement in PROMPT


def test_prompt_limits_lookup_tools_to_their_user_requested_purpose():
    required_routes = (
        "Use `get_customer_orders` only when the customer asks to list their orders",
        "Use `get_inventory` when the customer separately asks for an inventory quantity",
        "Use `search_policy` when the customer separately asks for policy text",
        "After an eligibility result, answer without additional tools",
    )
    for statement in required_routes:
        assert statement in PROMPT


def test_prompt_preserves_missing_fact_and_read_only_boundaries():
    assert "Never invent missing eligibility fields" in PROMPT
    assert "ask for the smallest missing fact" in PROMPT
    assert "Never claim that cancellation, return, exchange, refund" in PROMPT
