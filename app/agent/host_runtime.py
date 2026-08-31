from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from app.agent.deepseek_budget import (
    BudgetInvariantError,
    DeepSeekBudgetGuard,
    DeepSeekHostPriceSnapshot,
    SQLiteBudgetLedger,
)
from app.agent.factory import build_deepseek_client
from app.agent.openai_compatible import OpenAICompatibleChatClient
from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOST_PRICE_SNAPSHOT_PATH = (
    PROJECT_ROOT
    / "pricing"
    / "deepseek-v4-flash-host-policy-2026-08-31.json"
)
EXPECTED_HOST_PRICE_FILE_SHA256 = (
    "e58b96b34c22ca3e1de196077afdf51ebb556ea283431b33335cdf72a729aef1"
)
GLOBAL_BUDGET_LEDGER_PATH = (
    PROJECT_ROOT / "artifacts" / "private" / "deepseek-budget.sqlite3"
)
_OFFICIAL_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_HOST_MODEL = "deepseek-v4-flash"
_HOST_HARD_LIMIT_CNY = Decimal("20")
_HOST_EXECUTION_LIMIT_CNY = Decimal("18")


def _load_frozen_host_price_snapshot(
    path: Path,
    *,
    expected_file_sha256: str,
) -> DeepSeekHostPriceSnapshot:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BudgetInvariantError(
            "Unable to load the frozen host pricing policy."
        ) from exc
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    if observed_sha256 != expected_file_sha256:
        raise BudgetInvariantError(
            "Host pricing policy identity does not match the reviewed file."
        )
    try:
        return DeepSeekHostPriceSnapshot.model_validate_json(raw)
    except ValueError as exc:
        raise BudgetInvariantError(
            "The frozen host pricing policy is invalid."
        ) from exc


def _host_budget_ledger_path(configured_path: Path | None) -> Path:
    if configured_path is None:
        return GLOBAL_BUDGET_LEDGER_PATH
    path = Path(configured_path)
    if not path.is_absolute():
        raise ValueError("Host budget ledger path must be absolute")
    return path


def _require_host_settings(
    settings: Settings,
    *,
    price_snapshot: DeepSeekHostPriceSnapshot,
) -> None:
    if (
        not isinstance(settings.deepseek_api_key, str)
        or not settings.deepseek_api_key.strip()
    ):
        raise ValueError("DEEPSEEK_API_KEY is required for the live host runtime")
    if (
        not isinstance(settings.deepseek_base_url, str)
        or settings.deepseek_base_url.rstrip("/")
        != _OFFICIAL_DEEPSEEK_BASE_URL
    ):
        raise ValueError(
            "DEEPSEEK_BASE_URL must use the official DeepSeek HTTPS endpoint"
        )
    if settings.deepseek_model != _HOST_MODEL:
        raise ValueError("DEEPSEEK_MODEL must match the host price policy model")
    if (
        isinstance(settings.deepseek_temperature, bool)
        or not isinstance(settings.deepseek_temperature, (int, float))
        or not math.isfinite(settings.deepseek_temperature)
        or settings.deepseek_temperature != 0
    ):
        raise ValueError("DEEPSEEK_TEMPERATURE must be 0 for the live host runtime")
    if (
        isinstance(settings.deepseek_max_tokens, bool)
        or not isinstance(settings.deepseek_max_tokens, int)
        or not 1 <= settings.deepseek_max_tokens <= price_snapshot.limits.max_output_tokens
    ):
        raise ValueError(
            "DEEPSEEK_MAX_TOKENS must be within the host price policy limit"
        )


def build_host_preparation_model_factory(
    settings: Settings,
    *,
    price_path: Path = HOST_PRICE_SNAPSHOT_PATH,
    expected_price_file_sha256: str = EXPECTED_HOST_PRICE_FILE_SHA256,
    ledger_path: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> Callable[[str], OpenAICompatibleChatClient]:
    """Build a per-turn paid client factory guarded by one private ledger."""

    clock = now_provider or (lambda: datetime.now(UTC))
    price_snapshot = _load_frozen_host_price_snapshot(
        Path(price_path),
        expected_file_sha256=expected_price_file_sha256,
    )
    _require_host_settings(settings, price_snapshot=price_snapshot)
    price_snapshot.require_current(
        expected_model=_HOST_MODEL,
        now=clock(),
    )

    ledger = SQLiteBudgetLedger(
        path=_host_budget_ledger_path(ledger_path),
        hard_limit_cny=_HOST_HARD_LIMIT_CNY,
        execution_limit_cny=_HOST_EXECUTION_LIMIT_CNY,
    )

    def build_model(server_run_id: str) -> OpenAICompatibleChatClient:
        if not isinstance(server_run_id, str) or not server_run_id:
            raise ValueError("Host server run ID is required")
        budget_guard = DeepSeekBudgetGuard(
            ledger=ledger,
            run_id=f"host-pilot-{server_run_id.casefold()}",
            purpose="host_pilot",
            price_snapshot=price_snapshot,
            model=_HOST_MODEL,
            max_output_tokens=settings.deepseek_max_tokens,
            now_provider=clock,
        )
        try:
            return build_deepseek_client(
                settings,
                budget_guard=budget_guard,
                transport=transport,
            )
        except Exception:
            budget_guard.close()
            raise

    return build_model
