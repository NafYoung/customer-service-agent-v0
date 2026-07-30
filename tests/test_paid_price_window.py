from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.agent.deepseek_budget import (
    PAID_PURPOSES,
    BudgetInvariantError,
    BudgetPriceWindowError,
    DeepSeekBudgetGuard,
    DeepSeekPriceSnapshot,
    SQLiteBudgetLedger,
)
from app.agent.factory import build_deepseek_client
from app.agent.openai_compatible import ModelAPIError
from app.config import Settings
from evals import run_readonly_agent_evals
from evals.evidence_schema import BudgetAttemptBucket, BudgetSummary
from evals.readonly_eval import ReadonlyEvalCase


def _price_snapshot() -> DeepSeekPriceSnapshot:
    return DeepSeekPriceSnapshot.model_validate(
        {
            "schema_version": "1.0",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "currency": "CNY",
            "tokens_per_price_unit": 1_000_000,
            "rates_cny": {
                "prompt_cache_hit": "0.02",
                "prompt_cache_miss": "1",
                "completion": "2",
            },
            "limits": {
                "context_tokens": 1_000_000,
                "max_output_tokens": 384_000,
            },
            "source_url": (
                "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
            ),
            "usage_source_url": (
                "https://api-docs.deepseek.com/api/create-chat-completion/"
            ),
            "captured_at": "2026-07-29T08:58:58Z",
            "valid_until": "2026-07-30T08:58:58Z",
        }
    )


def _ledger(tmp_path) -> SQLiteBudgetLedger:
    return SQLiteBudgetLedger(
        path=tmp_path / "private" / "budget.sqlite3",
        hard_limit_cny=Decimal("20"),
        execution_limit_cny=Decimal("18"),
    )


def _settings(*, timeout_seconds: float = 30) -> Settings:
    return Settings(
        deepseek_api_key="fixture-key",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-flash",
        deepseek_timeout_seconds=timeout_seconds,
        deepseek_max_tokens=1024,
        deepseek_max_retries=2,
    )


def _success_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "provider-window-test"},
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


def test_paid_purpose_allowlist_is_closed_and_canonical() -> None:
    assert PAID_PURPOSES == {
        "diagnostic",
        "dev_repeat",
        "holdout_formal",
        "semantic_judge_calibration",
    }


@pytest.mark.parametrize(
    "purpose",
    [
        "unknown-paid-run",
        "",
        "   ",
        True,
        1,
    ],
)
def test_ledger_rejects_noncanonical_paid_purpose_before_run_insert(
    tmp_path,
    purpose: object,
) -> None:
    ledger = _ledger(tmp_path)

    with pytest.raises(BudgetInvariantError, match="purpose"):
        ledger.start_run(
            run_id="eval-invalid-purpose-0001",
            purpose=purpose,  # type: ignore[arg-type]
            price_snapshot=_price_snapshot(),
        )


def test_unknown_purpose_cannot_reach_paid_http(tmp_path) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _success_response()

    with pytest.raises(BudgetInvariantError, match="purpose"):
        guard = DeepSeekBudgetGuard(
            ledger=_ledger(tmp_path),
            run_id="eval-invalid-purpose-http",
            purpose="arbitrary-private-suite",
            price_snapshot=_price_snapshot(),
            model="deepseek-v4-flash",
            max_output_tokens=1024,
            now=datetime(2026, 7, 29, 12, tzinfo=UTC),
        )
        client = build_deepseek_client(
            _settings(),
            budget_guard=guard,
            transport=httpx.MockTransport(handler),
        )
        try:
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
        finally:
            client.close()

    assert call_count == 0


def test_request_is_blocked_before_reservation_when_price_window_is_too_short(
    tmp_path,
) -> None:
    snapshot = _price_snapshot()
    checked_at = snapshot.valid_until - timedelta(seconds=33)
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _success_response()

    guard = DeepSeekBudgetGuard(
        ledger=_ledger(tmp_path),
        run_id="eval-price-window-too-short",
        purpose="diagnostic",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now_provider=lambda: checked_at,
    )
    client = build_deepseek_client(
        _settings(timeout_seconds=30),
        budget_guard=guard,
        transport=httpx.MockTransport(handler),
    )
    checked_at = snapshot.valid_until - timedelta(seconds=31)
    try:
        with pytest.raises(ModelAPIError) as caught:
            client.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        client.close()

    assert caught.value.code == "MODEL_PRICE_EXPIRED"
    assert call_count == 0
    report = guard.snapshot()["run"]
    assert report["attempt_count"] == 0
    assert report["committed_cny"] == "0"


def test_client_construction_rejects_price_window_shorter_than_timeout(
    tmp_path,
) -> None:
    snapshot = _price_snapshot()
    checked_at = snapshot.valid_until - timedelta(seconds=31)
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _success_response()

    guard = DeepSeekBudgetGuard(
        ledger=_ledger(tmp_path),
        run_id="eval-client-preflight-window",
        purpose="diagnostic",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now_provider=lambda: checked_at,
    )

    with pytest.raises(BudgetPriceWindowError, match="too short"):
        build_deepseek_client(
            _settings(timeout_seconds=30),
            budget_guard=guard,
            transport=httpx.MockTransport(handler),
        )

    assert call_count == 0
    assert guard.snapshot()["run"]["attempt_count"] == 0


def test_formal_cli_rejects_short_price_window_before_start_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    snapshot = _price_snapshot()
    checked_at = snapshot.valid_until - timedelta(seconds=31)
    call_count = 0
    lock_calls = 0
    lock_root = tmp_path / "formal-locks"

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _success_response()

    guard = DeepSeekBudgetGuard(
        ledger=_ledger(tmp_path),
        run_id="eval-formal-prelock-window",
        purpose="holdout_formal",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now_provider=lambda: checked_at,
    )
    cases = [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": "private-holdout-00",
                "user_message": "private input",
                "expected": {
                    "semantic_contract": {
                        "required_claims": [
                            {
                                "id": "safe_00",
                                "category": "security",
                                "proposition": "the response remains safe",
                            }
                        ],
                        "forbidden_claims": [],
                    }
                },
            }
        )
    ]
    frozen = SimpleNamespace(
        calibration_fixture_snapshot=object(),
        fingerprints={"frozen": "1"},
    )

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "Settings",
        lambda: _settings(timeout_seconds=30),
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "load_cases",
        lambda path: cases,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "prepare_fixed_private_output_root",
        lambda requested, **kwargs: requested,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_case_directory",
        lambda path, **kwargs: path,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_private_input_file",
        lambda path, **kwargs: path,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "require_clean_git_worktree",
        lambda **kwargs: "1" * 40,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "freeze_readonly_harness",
        lambda settings: frozen,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "current_source_tree_sha256",
        lambda: "2" * 64,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_calibration_attestation",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_calibration_review",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_regression_gate",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "validate_holdout_declaration",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_budget_guard",
        lambda **kwargs: guard,
    )

    def build_client(settings, *, budget_guard):
        return build_deepseek_client(
            settings,
            budget_guard=budget_guard,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "build_deepseek_client",
        build_client,
    )

    def acquire_lock(**kwargs):
        nonlocal lock_calls
        lock_calls += 1
        raise AssertionError("formal start lock must not be reached")

    monkeypatch.setattr(
        run_readonly_agent_evals,
        "acquire_holdout_run_lock_with_hash",
        acquire_lock,
    )
    monkeypatch.setattr(
        run_readonly_agent_evals,
        "DEFAULT_HOLDOUT_LOCK_ROOT",
        lock_root,
    )

    exit_code = run_readonly_agent_evals.main(
        [
            "--case-dir",
            str(tmp_path / "private-cases"),
            "--output-root",
            str(tmp_path / "output"),
            "--run-id",
            "eval-formal-prelock-window",
            "--purpose",
            "holdout_formal",
            "--split",
            "holdout",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4",
            "--holdout-manifest",
            str(tmp_path / "manifest.json"),
            "--calibration-report",
            str(tmp_path / "calibration.json"),
            "--calibration-review",
            str(tmp_path / "review.json"),
            "--regression-bundle",
            str(tmp_path / "regression-bundle"),
        ]
    )

    assert exit_code == 2
    assert call_count == 0
    assert lock_calls == 0
    assert not lock_root.exists()


def test_response_crossing_price_window_is_uncertain_and_not_retried(
    tmp_path,
) -> None:
    snapshot = _price_snapshot()
    checked_at = snapshot.valid_until - timedelta(seconds=60)
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal checked_at, call_count
        call_count += 1
        checked_at = snapshot.valid_until + timedelta(microseconds=1)
        return _success_response()

    guard = DeepSeekBudgetGuard(
        ledger=_ledger(tmp_path),
        run_id="eval-response-crosses-window",
        purpose="diagnostic",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now_provider=lambda: checked_at,
    )
    client = build_deepseek_client(
        _settings(timeout_seconds=30),
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
    assert caught.value.error_stage == "provider_attempt"
    assert call_count == 1
    report = guard.snapshot()
    assert report["run"]["attempt_count"] == 1
    assert report["run"]["uncertain_count"] == 1
    assert report["run"]["committed_cny"] == "1.002048"
    bucket = report["attempt_evidence"]["run"][0]
    assert bucket == {
        "logical_call_sha256": bucket["logical_call_sha256"],
        "status": "uncertain",
        "settlement_mode": "upper_bound",
        "reserved_cny": "1.002048",
        "known_cost_cny": "0.0012",
        "error_code": "MODEL_PRICE_EXPIRED",
        "completed_at": bucket["completed_at"],
        "response_content_sha256": None,
        "count": 1,
    }
    assert len(bucket["logical_call_sha256"]) == 64
    assert datetime.fromisoformat(bucket["completed_at"]).tzinfo is not None
    validated = BudgetSummary.model_validate(report)
    assert validated.run.committed_cny == "1.002048"
    assert validated.run.settled_cny == "0"
    assert validated.run.uncertain_count == 1


def test_settled_bucket_still_cannot_exceed_its_reservation() -> None:
    with pytest.raises(ValueError, match="Settled budget"):
        BudgetAttemptBucket.model_validate(
            {
                "logical_call_sha256": "a" * 64,
                "status": "settled_upper_bound",
                "settlement_mode": "upper_bound",
                "reserved_cny": "1",
                "known_cost_cny": "1.00000001",
                "error_code": None,
                "completed_at": "2026-07-29T12:00:01+00:00",
                "count": 1,
            }
        )


def test_retryable_http_error_crossing_price_window_is_not_retried(
    tmp_path,
) -> None:
    snapshot = _price_snapshot()
    checked_at = snapshot.valid_until - timedelta(seconds=60)
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal checked_at, call_count
        call_count += 1
        checked_at = snapshot.valid_until + timedelta(microseconds=1)
        return httpx.Response(
            503,
            headers={"x-request-id": "provider-window-http-error"},
        )

    guard = DeepSeekBudgetGuard(
        ledger=_ledger(tmp_path),
        run_id="eval-http-error-crosses-window",
        purpose="diagnostic",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now_provider=lambda: checked_at,
    )
    client = build_deepseek_client(
        _settings(timeout_seconds=30),
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
        "response_content_sha256": None,
        "count": 1,
    }


def test_response_inside_price_window_still_settles_normally(tmp_path) -> None:
    snapshot = _price_snapshot()
    checked_at = snapshot.valid_until - timedelta(seconds=120)
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _success_response()

    guard = DeepSeekBudgetGuard(
        ledger=_ledger(tmp_path),
        run_id="eval-response-inside-window",
        purpose="diagnostic",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now_provider=lambda: checked_at,
    )
    client = build_deepseek_client(
        _settings(timeout_seconds=30),
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

    assert turn.content == "ok"
    assert call_count == 1
    report = guard.snapshot()["run"]
    assert report["attempt_count"] == 1
    assert report["uncertain_count"] == 0
    assert report["settled_cny"] == "0.0012"


@pytest.mark.parametrize(
    "invalid_seconds",
    [-1, True, float("nan"), float("inf")],
)
def test_guard_rejects_invalid_minimum_price_window(
    tmp_path,
    invalid_seconds: object,
) -> None:
    with pytest.raises(BudgetInvariantError, match="finite non-negative"):
        DeepSeekBudgetGuard(
            ledger=_ledger(tmp_path),
            run_id="eval-invalid-price-window",
            purpose="diagnostic",
            price_snapshot=_price_snapshot(),
            model="deepseek-v4-flash",
            max_output_tokens=1024,
            now=datetime(2026, 7, 29, 12, tzinfo=UTC),
            minimum_price_validity_seconds=invalid_seconds,  # type: ignore[arg-type]
        )


def test_closed_guard_rejects_timeout_binding_and_reservation(tmp_path) -> None:
    guard = DeepSeekBudgetGuard(
        ledger=_ledger(tmp_path),
        run_id="eval-closed-price-window",
        purpose="diagnostic",
        price_snapshot=_price_snapshot(),
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )
    guard.close()
    guard.close()

    with pytest.raises(BudgetInvariantError, match="closed"):
        guard.bind_request_timeout(timeout_seconds=30)
    with pytest.raises(BudgetInvariantError, match="closed"):
        guard.reserve_attempt(
            logical_call_id="must-not-reserve",
            attempt_number=1,
        )


def test_expired_response_marking_is_idempotent_and_never_downgrades_settled(
    tmp_path,
) -> None:
    snapshot = _price_snapshot()
    checked_at = snapshot.valid_until - timedelta(seconds=120)
    guard = DeepSeekBudgetGuard(
        ledger=_ledger(tmp_path),
        run_id="eval-price-mark-idempotent",
        purpose="diagnostic",
        price_snapshot=snapshot,
        model="deepseek-v4-flash",
        max_output_tokens=1024,
        now_provider=lambda: checked_at,
    )
    guard.bind_request_timeout(timeout_seconds=30)
    expired = guard.reserve_attempt(
        logical_call_id="expired-response",
        attempt_number=1,
    )
    settled = guard.reserve_attempt(
        logical_call_id="settled-response",
        attempt_number=1,
    )
    usage = {
        "prompt_tokens": 1_000,
        "completion_tokens": 100,
        "total_tokens": 1_100,
    }
    guard.settle_attempt(
        reservation=settled,
        usage=usage,
        provider_request_id="settled-request",
    )
    checked_at = snapshot.valid_until + timedelta(microseconds=1)

    for _ in range(2):
        with pytest.raises(BudgetPriceWindowError):
            guard.ensure_response_in_price_window(
                reservation=expired,
                usage=usage,
                provider_request_id="expired-request",
            )
    guard.mark_uncertain(
        reservation=settled,
        error_code="MUST_NOT_DOWNGRADE",
    )

    buckets = {
        bucket["status"]: bucket
        for bucket in guard.snapshot()["attempt_evidence"]["run"]
    }
    assert set(buckets) == {"settled_upper_bound", "uncertain"}
    assert buckets["settled_upper_bound"]["error_code"] is None
    assert buckets["uncertain"]["error_code"] == "MODEL_PRICE_EXPIRED"
    assert all(
        bucket["settlement_mode"] == "upper_bound"
        and bucket["reserved_cny"] == "1.002048"
        and bucket["known_cost_cny"] == "0.0012"
        and bucket["count"] == 1
        and datetime.fromisoformat(bucket["completed_at"]).tzinfo is not None
        for bucket in buckets.values()
    )
