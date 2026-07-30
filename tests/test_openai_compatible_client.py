from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.agent.deepseek_budget import (
    DeepSeekBudgetGuard,
    SQLiteBudgetLedger,
    load_price_snapshot,
)
from app.agent.factory import build_deepseek_client
from app.agent.openai_compatible import (
    ModelAPIError,
    ModelProtocolError,
    OpenAICompatibleChatClient,
)
from app.config import Settings
from app.tools.contracts import get_read_only_tool_contracts

ROOT = Path(__file__).resolve().parents[1]
PRICE_SNAPSHOT_PATH = (
    ROOT / "pricing" / "deepseek-v4-flash-2026-07-30.json"
)


def _budget_guard(
    tmp_path,
    *,
    run_id: str,
    execution_limit_cny: str = "18",
):
    snapshot = load_price_snapshot(PRICE_SNAPSHOT_PATH)
    return DeepSeekBudgetGuard(
        ledger=SQLiteBudgetLedger(
            path=tmp_path / "private" / "budget.sqlite3",
            hard_limit_cny=Decimal("20"),
            execution_limit_cny=Decimal(execution_limit_cny),
        ),
        run_id=run_id,
        purpose="diagnostic",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )


def test_client_sends_openai_compatible_tool_request_and_parses_tool_call():
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("Authorization")
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-success-1"},
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-order-1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_order",
                                        "arguments": '{"order_id":"ORD-1002"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    client = OpenAICompatibleChatClient(
        api_key="fixture-key",
        base_url="https://api.deepseek.com/",
        model="deepseek-test-model",
        max_tokens=777,
        temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},
        transport=httpx.MockTransport(handler),
    )
    try:
        turn = client.complete(
            messages=[{"role": "user", "content": "查 ORD-1002"}],
            tools=get_read_only_tool_contracts(),
        )
    finally:
        client.close()

    assert observed["url"] == "https://api.deepseek.com/chat/completions"
    assert observed["authorization"] == "Bearer fixture-key"
    body = observed["body"]
    assert body["model"] == "deepseek-test-model"
    assert body["stream"] is False
    assert body["max_tokens"] == 777
    assert body["temperature"] == 0.2
    assert body["tool_choice"] == "auto"
    assert body["thinking"] == {"type": "disabled"}
    assert {
        item["function"]["name"] for item in body["tools"]
    } == {
        "get_customer_orders",
        "get_order",
        "get_shipment",
        "get_inventory",
        "search_policy",
        "check_action_eligibility",
    }
    assert all(
        item["type"] == "function"
        and item["function"]["parameters"].get("additionalProperties") is False
        for item in body["tools"]
    )
    assert turn.finish_reason == "tool_calls"
    assert turn.content is None
    assert turn.tool_calls[0].id == "call-order-1"
    assert turn.tool_calls[0].name == "get_order"
    assert turn.tool_calls[0].arguments == '{"order_id":"ORD-1002"}'
    assert turn.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }
    assert turn.provider_request_id == "provider-request-success-1"
    assert turn.provider_attempts == 1
    assert "fixture-key" not in repr(turn)


def test_client_json_mode_is_tool_free_and_cannot_be_overridden():
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"claims":[]}',
                        },
                    }
                ],
                "usage": {"total_tokens": 8},
            },
        )

    client = OpenAICompatibleChatClient(
        api_key="fixture-key",
        base_url="https://api.deepseek.com",
        model="deepseek-test-model",
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
        transport=httpx.MockTransport(handler),
    )
    try:
        turn = client.complete_json(
            messages=[{"role": "user", "content": "Return JSON."}],
        )
    finally:
        client.close()

    body = observed["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert "tools" not in body
    assert "tool_choice" not in body
    assert turn.content == '{"claims":[]}'

    with pytest.raises(ValueError, match="response_format"):
        OpenAICompatibleChatClient(
            api_key="fixture-key",
            base_url="https://api.deepseek.com",
            model="deepseek-test-model",
            extra_body={"response_format": {"type": "text"}},
        )


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_client_maps_http_errors_without_leaking_secret(status_code: int):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"x-request-id": "provider-request-123"},
            json={"error": {"message": "provider detail must not be echoed"}},
        )

    client = OpenAICompatibleChatClient(
        api_key="do-not-leak-this-provider-key",
        base_url="https://api.deepseek.com",
        model="deepseek-test-model",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ModelAPIError) as caught:
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        client.close()

    error = caught.value
    assert error.status_code == status_code
    assert error.request_id == "provider-request-123"
    assert error.attempts == 1
    assert error.code == "MODEL_HTTP_ERROR"
    assert "do-not-leak-this-provider-key" not in str(error)
    assert "provider detail must not be echoed" not in str(error)


def test_client_rejects_malformed_success_response():
    client = OpenAICompatibleChatClient(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-test-model",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": []})
        ),
    )
    try:
        with pytest.raises(ModelProtocolError) as caught:
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        client.close()

    assert caught.value.code == "MODEL_PROTOCOL_ERROR"


def test_client_retries_transient_statuses_with_a_fixed_limit():
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429)
        if call_count == 2:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ]
            },
        )

    client = OpenAICompatibleChatClient(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-test-model",
        max_retries=2,
        retry_backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        turn = client.complete(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    finally:
        client.close()

    assert turn.content == "ok"
    assert call_count == 3
    assert turn.provider_attempts == 3


def test_budget_guard_reserves_every_retry_and_settles_success(tmp_path):
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429)
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-budget-2"},
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 100,
                    "total_tokens": 1_100,
                },
            },
        )

    guard = _budget_guard(
        tmp_path,
        run_id="eval-budget-retry-0001",
    )
    client = OpenAICompatibleChatClient(
        api_key="fixture-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        max_tokens=1024,
        max_retries=1,
        retry_backoff_seconds=0,
        budget_guard=guard,
        transport=httpx.MockTransport(handler),
    )
    try:
        turn = client.complete(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    finally:
        client.close()

    report = guard.snapshot()
    assert turn.content == "ok"
    assert call_count == 2
    assert report["run"]["attempt_count"] == 2
    assert report["run"]["uncertain_count"] == 1
    assert report["run"]["settled_cny"] == "0.0012"
    assert turn.logical_call_sha256 is not None
    assert len(turn.logical_call_sha256) == 64
    assert {
        bucket["logical_call_sha256"]
        for bucket in report["attempt_evidence"]["run"]
    } == {turn.logical_call_sha256}


def test_budget_guard_blocks_retry_before_second_http_attempt(tmp_path):
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503)

    guard = _budget_guard(
        tmp_path,
        run_id="eval-budget-block-0001",
        execution_limit_cny="1.002048",
    )
    client = OpenAICompatibleChatClient(
        api_key="fixture-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        max_tokens=1024,
        max_retries=1,
        retry_backoff_seconds=0,
        budget_guard=guard,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ModelAPIError) as caught:
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        client.close()

    assert caught.value.code == "MODEL_BUDGET_EXHAUSTED"
    assert caught.value.error_stage == "reserve_attempt"
    assert caught.value.logical_call_sha256 is not None
    assert call_count == 1
    report = guard.snapshot()
    assert report["run"]["uncertain_count"] == 1
    assert {
        bucket["logical_call_sha256"]
        for bucket in report["attempt_evidence"]["run"]
    } == {caught.value.logical_call_sha256}


def test_missing_usage_preserves_provider_stage_and_ledger_namespace(
    tmp_path,
) -> None:
    guard = _budget_guard(
        tmp_path,
        run_id="eval-budget-missing-usage",
    )
    client = OpenAICompatibleChatClient(
        api_key="fixture-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        max_tokens=1024,
        budget_guard=guard,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "ok",
                            },
                        }
                    ]
                },
            )
        ),
    )
    try:
        with pytest.raises(ModelAPIError) as caught:
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        client.close()

    assert caught.value.code == "MODEL_BUDGET_USAGE_ERROR"
    assert caught.value.error_stage == "provider_attempt"
    assert caught.value.logical_call_sha256 is not None
    bucket = guard.snapshot()["attempt_evidence"]["run"][0]
    assert bucket["logical_call_sha256"] == caught.value.logical_call_sha256
    assert bucket["error_code"] == "MISSING_PROVIDER_USAGE"


def test_budget_guard_retains_malformed_success_reservation(tmp_path):
    guard = _budget_guard(
        tmp_path,
        run_id="eval-budget-malformed-1",
    )
    client = OpenAICompatibleChatClient(
        api_key="fixture-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        max_tokens=1024,
        budget_guard=guard,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": []})
        ),
    )
    try:
        with pytest.raises(ModelProtocolError):
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        client.close()

    report = guard.snapshot()["run"]
    assert report["attempt_count"] == 1
    assert report["uncertain_count"] == 1
    assert report["committed_cny"] == "1.002048"


def test_malformed_success_crossing_price_window_reports_expiry_first(
    tmp_path,
):
    snapshot = load_price_snapshot(PRICE_SNAPSHOT_PATH)
    checked_at = snapshot.valid_until - timedelta(seconds=60)
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal checked_at, call_count
        call_count += 1
        checked_at = snapshot.valid_until + timedelta(microseconds=1)
        return httpx.Response(
            200,
            headers={"x-request-id": "malformed-cross-window"},
            json={"choices": []},
        )

    guard = DeepSeekBudgetGuard(
        ledger=SQLiteBudgetLedger(
            path=tmp_path / "private" / "budget.sqlite3",
            hard_limit_cny=Decimal("20"),
            execution_limit_cny=Decimal("18"),
        ),
        run_id="eval-malformed-cross-window",
        purpose="diagnostic",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now_provider=lambda: checked_at,
    )
    client = OpenAICompatibleChatClient(
        api_key="fixture-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        max_tokens=1024,
        max_retries=2,
        budget_guard=guard,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ModelAPIError) as caught:
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        client.close()

    assert caught.value.code == "MODEL_PRICE_EXPIRED"
    assert caught.value.attempts == 1
    assert call_count == 1
    bucket = guard.snapshot()["attempt_evidence"]["run"][0]
    assert bucket == {
        "logical_call_sha256": bucket["logical_call_sha256"],
        "status": "uncertain",
        "settlement_mode": None,
        "reserved_cny": "1.002048",
        "known_cost_cny": None,
        "error_code": "MODEL_PRICE_EXPIRED",
        "completed_at": bucket["completed_at"],
        "count": 1,
    }


@pytest.mark.parametrize(
    ("finish_reason", "expected_code"),
    [
        ("length", "MODEL_INCOMPLETE_RESPONSE"),
        ("content_filter", "MODEL_INCOMPLETE_RESPONSE"),
        ("insufficient_system_resource", "MODEL_INSUFFICIENT_RESOURCE"),
        ("future_unknown_reason", "MODEL_PROTOCOL_ERROR"),
        (None, "MODEL_PROTOCOL_ERROR"),
    ],
)
def test_client_fails_closed_on_incomplete_or_unknown_finish_reason(
    finish_reason: str | None,
    expected_code: str,
):
    client = OpenAICompatibleChatClient(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-test-model",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": finish_reason,
                            "message": {
                                "role": "assistant",
                                "content": "possibly incomplete",
                            },
                        }
                    ]
                },
            )
        ),
    )
    try:
        with pytest.raises((ModelAPIError, ModelProtocolError)) as caught:
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        client.close()

    assert caught.value.code == expected_code


def test_deepseek_factory_uses_v4_flash_and_explicitly_disables_thinking(
    tmp_path,
):
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    settings = Settings(
        deepseek_api_key="factory-test-key",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-flash",
        deepseek_max_tokens=1024,
    )
    client = build_deepseek_client(
        settings,
        budget_guard=_budget_guard(
            tmp_path,
            run_id="eval-budget-factory-01",
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        client.complete(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
    finally:
        client.close()

    assert observed["url"] == "https://api.deepseek.com/chat/completions"
    assert observed["body"]["model"] == "deepseek-v4-flash"
    assert observed["body"]["stream"] is False
    assert observed["body"]["max_tokens"] == 1024
    assert observed["body"]["temperature"] == 0.0
    assert observed["body"]["thinking"] == {"type": "disabled"}


def test_deepseek_factory_refuses_paid_client_without_budget_guard():
    settings = Settings(
        deepseek_api_key="factory-test-key",
        deepseek_base_url="https://api.deepseek.com",
    )

    with pytest.raises(ValueError, match="budget guard"):
        build_deepseek_client(settings)


def test_deepseek_factory_requires_key_only_when_model_is_constructed():
    settings = Settings(deepseek_api_key=None)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        build_deepseek_client(settings)


def test_deepseek_factory_requires_https_for_bearer_credentials():
    settings = Settings(
        deepseek_api_key="factory-test-key",
        deepseek_base_url="http://api.deepseek.com",
    )
    with pytest.raises(ValueError, match="HTTPS"):
        build_deepseek_client(settings)


def test_client_rejects_extra_body_overrides_of_common_protocol_fields():
    with pytest.raises(ValueError, match="reserved"):
        OpenAICompatibleChatClient(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model="deepseek-test-model",
            extra_body={"messages": [{"role": "user", "content": "override"}]},
        )


@pytest.mark.parametrize("temperature", [-0.1, 2.1])
def test_client_rejects_temperature_outside_provider_range(
    temperature: float,
):
    with pytest.raises(ValueError, match="temperature"):
        OpenAICompatibleChatClient(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model="deepseek-test-model",
            temperature=temperature,
        )
