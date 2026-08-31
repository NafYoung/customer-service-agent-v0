from __future__ import annotations

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
    DeepSeekHostPriceSnapshot,
    load_host_price_snapshot,
    worst_case_attempt_cost,
)
from app.agent.host_runtime import (
    HOST_PRICE_SNAPSHOT_PATH,
    build_host_preparation_model_factory,
)
from app.config import Settings

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
        host_budget_ledger_path=str(tmp_path / "private" / "host-budget.sqlite3"),
    )


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
            now_provider=lambda: HISTORICAL_POLICY_NOW,
        )

    assert ledger_path.exists() is False


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
    with sqlite3.connect(settings.host_budget_ledger_path) as connection:
        run = connection.execute(
            "SELECT run_id, purpose, status FROM budget_runs"
        ).fetchone()
    assert run == (
        "host-pilot-run-0123456789abcdef0123456789abcdef",
        "host_pilot",
        "completed",
    )


def test_create_app_builds_live_factory_only_when_explicitly_enabled(
    tmp_path: Path,
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
        host_budget_ledger_path=str(tmp_path / "host-budget.sqlite3"),
    )
    enabled = main_module.create_app(
        settings=enabled_settings,
        seed_demo=False,
    )
    assert enabled.state.host_flow.has_model_runtime is True
    assert built == [enabled_settings]
