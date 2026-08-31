from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import app.main as main_module
from app.agent.deepseek_budget import (
    BudgetInvariantError,
    BudgetPriceWindowError,
    DeepSeekBudgetGuard,
    DeepSeekHostPriceSnapshot,
    DeepSeekPriceSnapshot,
    SQLiteBudgetLedger,
    load_host_price_snapshot,
    worst_case_attempt_cost,
)
from app.agent.host_runtime import (
    GLOBAL_BUDGET_LEDGER_PATH,
    HOST_PRICE_SNAPSHOT_PATH,
    build_host_preparation_model_factory,
)
from app.agent.openai_compatible import ModelAPIError
from app.config import Settings
from evals.run_readonly_agent_evals import DEFAULT_BUDGET_LEDGER

HISTORICAL_POLICY_NOW = datetime(2026, 8, 31, 18, tzinfo=UTC)


def _settings(tmp_path: Path, *, api_key: str | None = "fixture-key") -> Settings:
    return Settings(
        deepseek_api_key=api_key,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-flash",
        deepseek_timeout_seconds=30,
        deepseek_max_tokens=1024,
        deepseek_temperature=0,
        deepseek_max_retries=0,
    )


def _ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "private" / "host-budget.sqlite3"


def test_host_price_policy_binds_official_usd_peak_to_cny_upper_bound() -> None:
    snapshot = load_host_price_snapshot(HOST_PRICE_SNAPSHOT_PATH)

    assert snapshot.billing_currency == "USD"
    assert snapshot.currency == "CNY"
    assert snapshot.pricing_tier_policy == "always_peak"
    assert snapshot.cny_per_usd_upper_bound == "10"
    assert snapshot.peak_rates_usd.model_dump() == {
        "prompt_cache_hit": "0.014",
        "prompt_cache_miss": "0.44",
        "completion": "1.32",
    }
    assert snapshot.rates_cny.model_dump() == {
        "prompt_cache_hit": "0.14",
        "prompt_cache_miss": "4.4",
        "completion": "13.2",
    }
    reservation = worst_case_attempt_cost(snapshot, max_output_tokens=1024)
    assert reservation.cny == Decimal("4.4135168")


def test_host_price_policy_rejects_a_lower_derived_rate() -> None:
    payload = json.loads(HOST_PRICE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    payload["rates_cny"]["completion"] = "13.19"

    with pytest.raises(ValidationError, match="derived|upper bound"):
        DeepSeekHostPriceSnapshot.model_validate(payload)


def test_host_price_policy_rejects_synchronized_rate_and_fx_downgrade() -> None:
    payload = json.loads(HOST_PRICE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    payload["peak_rates_usd"] = {
        "prompt_cache_hit": "0.01",
        "prompt_cache_miss": "0.4",
        "completion": "1.2",
    }
    payload["cny_per_usd_upper_bound"] = "5"
    payload["rates_cny"] = {
        "prompt_cache_hit": "0.05",
        "prompt_cache_miss": "2",
        "completion": "6",
    }

    with pytest.raises(ValidationError, match="official|policy|floor"):
        DeepSeekHostPriceSnapshot.model_validate(payload)


def test_host_price_policy_rejects_zero_provider_rates() -> None:
    payload = json.loads(HOST_PRICE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    payload["peak_rates_usd"]["completion"] = "0"
    payload["rates_cny"]["completion"] = "0"

    with pytest.raises(ValidationError, match="positive"):
        DeepSeekHostPriceSnapshot.model_validate(payload)


def test_host_price_policy_hash_binds_usd_rates_and_fx_policy() -> None:
    snapshot = load_host_price_snapshot(HOST_PRICE_SNAPSHOT_PATH)
    payload = snapshot.model_dump(mode="json")
    payload["peak_rates_usd"]["completion"] = "1.33"
    payload["rates_cny"]["completion"] = "13.3"
    changed = DeepSeekHostPriceSnapshot.model_validate(payload)

    assert changed.sha256 != snapshot.sha256


def test_host_runtime_factory_fails_before_ledger_without_key(tmp_path: Path) -> None:
    ledger_path = tmp_path / "private" / "host-budget.sqlite3"

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        build_host_preparation_model_factory(
            _settings(tmp_path, api_key=None),
            ledger_path=_ledger_path(tmp_path),
            now_provider=lambda: HISTORICAL_POLICY_NOW,
        )

    assert ledger_path.exists() is False


def test_host_runtime_factory_rejects_expired_policy_before_ledger(
    tmp_path: Path,
) -> None:
    payload = json.loads(HOST_PRICE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    payload["valid_until"] = "2026-09-01T00:00:00Z"
    expired_path = tmp_path / "expired-policy.json"
    expired_bytes = json.dumps(payload).encode("utf-8")
    expired_path.write_bytes(expired_bytes)
    ledger_path = tmp_path / "private" / "host-budget.sqlite3"

    with pytest.raises(BudgetPriceWindowError, match="expired"):
        build_host_preparation_model_factory(
            _settings(tmp_path),
            price_path=expired_path,
            expected_price_file_sha256=hashlib.sha256(expired_bytes).hexdigest(),
            ledger_path=_ledger_path(tmp_path),
            now_provider=lambda: datetime(2026, 9, 1, 1, tzinfo=UTC),
        )

    assert ledger_path.exists() is False


def test_host_runtime_rejects_price_file_identity_mismatch_before_ledger(
    tmp_path: Path,
) -> None:
    payload = json.loads(HOST_PRICE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    payload["valid_until"] = "2026-09-08T17:40:23Z"
    changed_path = tmp_path / "changed-policy.json"
    changed_path.write_text(json.dumps(payload), encoding="utf-8")
    ledger_path = tmp_path / "private" / "host-budget.sqlite3"

    with pytest.raises(BudgetInvariantError, match="identity"):
        build_host_preparation_model_factory(
            _settings(tmp_path),
            price_path=changed_path,
            ledger_path=_ledger_path(tmp_path),
            now_provider=lambda: HISTORICAL_POLICY_NOW,
        )

    assert ledger_path.exists() is False


def test_host_runtime_uses_one_absolute_global_budget_ledger() -> None:
    assert GLOBAL_BUDGET_LEDGER_PATH.is_absolute()
    assert GLOBAL_BUDGET_LEDGER_PATH == DEFAULT_BUDGET_LEDGER


def test_host_runtime_rejects_custom_relative_budget_ledger(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        build_host_preparation_model_factory(
            _settings(tmp_path),
            ledger_path=Path("relative/host-budget.sqlite3"),
            now_provider=lambda: HISTORICAL_POLICY_NOW,
        )


def test_host_pilot_purpose_is_bound_to_v2_price_policy(tmp_path: Path) -> None:
    legacy_payload = json.loads(
        (
            HOST_PRICE_SNAPSHOT_PATH.parent
            / "deepseek-v4-flash-2026-07-29.json"
        ).read_text(encoding="utf-8")
    )
    legacy = DeepSeekPriceSnapshot.model_validate(legacy_payload)
    host = load_host_price_snapshot(HOST_PRICE_SNAPSHOT_PATH)

    with pytest.raises(BudgetInvariantError, match="host_pilot|pricing"):
        DeepSeekBudgetGuard(
            ledger=SQLiteBudgetLedger(
                path=tmp_path / "legacy.sqlite3",
                hard_limit_cny=Decimal("20"),
                execution_limit_cny=Decimal("18"),
            ),
            run_id="host-pilot-legacy-policy",
            purpose="host_pilot",
            price_snapshot=legacy,
            model=legacy.model,
            max_output_tokens=1024,
            now=datetime(2026, 7, 29, 12, tzinfo=UTC),
        )
    with pytest.raises(BudgetInvariantError, match="host_pilot|pricing"):
        DeepSeekBudgetGuard(
            ledger=SQLiteBudgetLedger(
                path=tmp_path / "host-as-diagnostic.sqlite3",
                hard_limit_cny=Decimal("20"),
                execution_limit_cny=Decimal("18"),
            ),
            run_id="diagnostic-host-policy",
            purpose="diagnostic",
            price_snapshot=host,
            model=host.model,
            max_output_tokens=1024,
            now=HISTORICAL_POLICY_NOW,
        )


def test_host_runtime_uses_persistent_guard_and_closes_run(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("Authorization")
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

    settings = _settings(tmp_path)
    factory = build_host_preparation_model_factory(
        settings,
        ledger_path=_ledger_path(tmp_path),
        transport=httpx.MockTransport(handler),
        now_provider=lambda: HISTORICAL_POLICY_NOW,
    )
    model = factory("RUN-0123456789ABCDEF0123456789ABCDEF")
    public = model.public_runtime_config()
    assert public["budget_guard_attached"] is True
    assert "fixture-key" not in json.dumps(public)

    turn = model.complete(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
    )
    model.close()

    assert turn.content == "ok"
    assert observed["authorization"] == "Bearer fixture-key"
    assert observed["body"]["thinking"] == {"type": "disabled"}
    with sqlite3.connect(_ledger_path(tmp_path)) as connection:
        run = connection.execute(
            "SELECT run_id, purpose, status FROM budget_runs"
        ).fetchone()
    assert run == (
        "host-pilot-run-0123456789abcdef0123456789abcdef",
        "host_pilot",
        "completed",
    )


def test_host_runtime_blocks_fifth_uncertain_attempt_before_http(
    tmp_path: Path,
) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503)

    factory = build_host_preparation_model_factory(
        _settings(tmp_path),
        ledger_path=_ledger_path(tmp_path),
        transport=httpx.MockTransport(handler),
        now_provider=lambda: HISTORICAL_POLICY_NOW,
    )
    error_codes: list[str] = []
    for index in range(5):
        model = factory(f"RUN-{index:032X}")
        try:
            with pytest.raises(ModelAPIError) as caught:
                model.complete(
                    messages=[{"role": "user", "content": "hello"}],
                    tools=[],
                )
            error_codes.append(getattr(caught.value, "code", ""))
        finally:
            model.close()

    assert call_count == 4
    assert error_codes == ["MODEL_HTTP_ERROR"] * 4 + ["MODEL_BUDGET_EXHAUSTED"]


def test_host_runtime_maps_sqlite_reservation_failure_before_http(
    tmp_path: Path,
    monkeypatch,
) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200)

    factory = build_host_preparation_model_factory(
        _settings(tmp_path),
        ledger_path=_ledger_path(tmp_path),
        transport=httpx.MockTransport(handler),
        now_provider=lambda: HISTORICAL_POLICY_NOW,
    )
    model = factory("RUN-FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF")

    def locked_reservation(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SQLiteBudgetLedger, "reserve_attempt", locked_reservation)
    try:
        with pytest.raises(ModelAPIError) as caught:
            model.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
            )
    finally:
        model.close()

    assert caught.value.code == "MODEL_BUDGET_ERROR"
    assert caught.value.error_stage == "reserve_attempt"
    assert call_count == 0


def test_create_app_builds_live_factory_only_when_explicitly_enabled(
    monkeypatch,
) -> None:
    built: list[Settings] = []

    def fake_builder(settings: Settings):
        built.append(settings)
        return lambda _server_run_id: None

    monkeypatch.setattr(
        main_module,
        "build_host_preparation_model_factory",
        fake_builder,
    )
    disabled = main_module.create_app(
        settings=Settings(
            database_url="sqlite:///:memory:",
            enable_live_preparation_agent=False,
        ),
        seed_demo=False,
    )
    assert disabled.state.host_flow.has_model_runtime is False
    assert built == []

    enabled_settings = Settings(
        database_url="sqlite:///:memory:",
        enable_live_preparation_agent=True,
    )
    enabled = main_module.create_app(
        settings=enabled_settings,
        seed_demo=False,
    )
    assert enabled.state.host_flow.has_model_runtime is True
    assert built == [enabled_settings]
