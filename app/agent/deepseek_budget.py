from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CNY_UNITS_PER_CNY = 100_000_000
_RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{7,79}$")
_SETTLED_STATUSES = {"settled_exact", "settled_upper_bound"}
_COMMITTED_STATUSES = {"reserved", "uncertain", *_SETTLED_STATUSES}


class BudgetError(RuntimeError):
    """Base class for local budget failures that never includes request data."""


class BudgetExceededError(BudgetError):
    pass


class BudgetInvariantError(BudgetError):
    pass


class BudgetUsageError(BudgetError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceRates(_StrictModel):
    prompt_cache_hit: str
    prompt_cache_miss: str
    completion: str

    @field_validator("*")
    @classmethod
    def validate_decimal_rate(cls, value: str) -> str:
        amount = _parse_decimal(value, field_name="price rate")
        if amount < 0:
            raise ValueError("Price rates cannot be negative")
        cny_to_units(amount / Decimal(1_000_000))
        return value


class ModelLimits(_StrictModel):
    context_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)


class DeepSeekPriceSnapshot(_StrictModel):
    schema_version: Literal["1.0"]
    provider: Literal["deepseek"]
    model: str
    currency: Literal["CNY"]
    tokens_per_price_unit: Literal[1_000_000]
    rates_cny: PriceRates
    limits: ModelLimits
    source_url: str
    usage_source_url: str
    captured_at: datetime
    valid_until: datetime

    @field_validator("source_url", "usage_source_url")
    @classmethod
    def require_official_https_url(cls, value: str) -> str:
        if not value.startswith("https://api-docs.deepseek.com/"):
            raise ValueError("Pricing sources must be official DeepSeek HTTPS URLs")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> DeepSeekPriceSnapshot:
        if self.captured_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("Pricing timestamps must be timezone-aware")
        if self.valid_until <= self.captured_at:
            raise ValueError("Pricing valid_until must follow captured_at")
        return self

    @property
    def sha256(self) -> str:
        payload = self.model_dump(mode="json")
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def require_current(
        self,
        *,
        expected_model: str,
        now: datetime | None = None,
    ) -> None:
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            raise BudgetInvariantError("Pricing check time must be timezone-aware")
        if self.model != expected_model:
            raise BudgetInvariantError(
                "Pricing snapshot model does not match the configured model."
            )
        if checked_at < self.captured_at:
            raise BudgetInvariantError(
                "Pricing snapshot is not active yet."
            )
        if checked_at > self.valid_until:
            raise BudgetInvariantError(
                "Pricing snapshot has expired; refresh it before paid calls."
            )


@dataclass(frozen=True)
class UsageCost:
    units: int
    cny: Decimal
    mode: Literal["exact", "upper_bound", "reservation"]


@dataclass(frozen=True)
class BudgetReservation:
    attempt_id: str
    run_id: str
    logical_call_id: str
    attempt_number: int
    model: str
    reserved_units: int


def _parse_decimal(value: str | Decimal, *, field_name: str) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not amount.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return amount


def cny_to_units(amount: Decimal) -> int:
    if amount < 0:
        raise ValueError("CNY amount cannot be negative")
    scaled = amount * CNY_UNITS_PER_CNY
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError(
            "CNY amount is more precise than the budget ledger supports"
        )
    return int(integral)


def units_to_cny(units: int) -> Decimal:
    if units < 0:
        raise ValueError("Budget units cannot be negative")
    return Decimal(units) / CNY_UNITS_PER_CNY


def format_cny(units: int) -> str:
    amount = units_to_cny(units)
    normalized = amount.normalize()
    return format(normalized, "f")


def _usage_token(
    usage: Mapping[str, Any],
    field: str,
    *,
    required: bool = True,
) -> int | None:
    value = usage.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BudgetUsageError(
            f"Provider usage field {field} must be a non-negative integer."
        )
    return value


def _rate(snapshot: DeepSeekPriceSnapshot, field: str) -> Decimal:
    value = getattr(snapshot.rates_cny, field)
    return _parse_decimal(value, field_name=field)


def calculate_usage_cost(
    snapshot: DeepSeekPriceSnapshot,
    usage: Mapping[str, Any],
) -> UsageCost:
    return calculate_usage_cost_from_rates(
        rates_cny=snapshot.rates_cny.model_dump(),
        tokens_per_price_unit=snapshot.tokens_per_price_unit,
        usage=usage,
    )


def calculate_usage_cost_from_rates(
    *,
    rates_cny: Mapping[str, Any],
    tokens_per_price_unit: int,
    usage: Mapping[str, Any],
) -> UsageCost:
    """Recompute one recorded provider call without trusting a ledger total."""

    if (
        isinstance(tokens_per_price_unit, bool)
        or not isinstance(tokens_per_price_unit, int)
        or tokens_per_price_unit < 1
    ):
        raise BudgetUsageError(
            "Pricing token unit must be a positive integer."
        )
    required_rates = (
        "prompt_cache_hit",
        "prompt_cache_miss",
        "completion",
    )
    if set(rates_cny) != set(required_rates):
        raise BudgetUsageError(
            "Pricing rates must contain the exact required fields."
        )
    try:
        parsed_rates = {
            field: _parse_decimal(
                rates_cny[field],
                field_name=field,
            )
            for field in required_rates
        }
    except ValueError as exc:
        raise BudgetUsageError(
            "Pricing rates contain an invalid decimal."
        ) from exc
    if any(rate < 0 for rate in parsed_rates.values()):
        raise BudgetUsageError("Pricing rates cannot be negative.")

    prompt_tokens = _usage_token(usage, "prompt_tokens")
    completion_tokens = _usage_token(usage, "completion_tokens")
    total_tokens = _usage_token(usage, "total_tokens")
    assert prompt_tokens is not None
    assert completion_tokens is not None
    assert total_tokens is not None
    if total_tokens != prompt_tokens + completion_tokens:
        raise BudgetUsageError(
            "Provider usage total does not equal prompt plus completion tokens."
        )

    cache_hit = _usage_token(
        usage,
        "prompt_cache_hit_tokens",
        required=False,
    )
    cache_miss = _usage_token(
        usage,
        "prompt_cache_miss_tokens",
        required=False,
    )
    if (cache_hit is None) != (cache_miss is None):
        raise BudgetUsageError(
            "Provider usage must include both cache token fields or neither."
        )

    if cache_hit is None:
        cache_hit = 0
        cache_miss = prompt_tokens
        mode: Literal["exact", "upper_bound"] = "upper_bound"
    else:
        assert cache_miss is not None
        if cache_hit + cache_miss != prompt_tokens:
            raise BudgetUsageError(
                "Provider cache token fields do not equal prompt tokens."
            )
        mode = "exact"

    cost_cny = (
        Decimal(cache_hit) * parsed_rates["prompt_cache_hit"]
        + Decimal(cache_miss) * parsed_rates["prompt_cache_miss"]
        + Decimal(completion_tokens) * parsed_rates["completion"]
    ) / tokens_per_price_unit
    return UsageCost(
        units=cny_to_units(cost_cny),
        cny=cost_cny,
        mode=mode,
    )


def worst_case_attempt_cost(
    snapshot: DeepSeekPriceSnapshot,
    *,
    max_output_tokens: int,
) -> UsageCost:
    if not 1 <= max_output_tokens <= snapshot.limits.max_output_tokens:
        raise BudgetInvariantError(
            "Configured max output tokens exceed the pricing snapshot limit."
        )
    cost_cny = (
        Decimal(snapshot.limits.context_tokens)
        * _rate(snapshot, "prompt_cache_miss")
        + Decimal(max_output_tokens) * _rate(snapshot, "completion")
    ) / snapshot.tokens_per_price_unit
    return UsageCost(
        units=cny_to_units(cost_cny),
        cny=cost_cny,
        mode="reservation",
    )


def load_price_snapshot(path: Path) -> DeepSeekPriceSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BudgetInvariantError(
            "Unable to load the versioned DeepSeek pricing snapshot."
        ) from exc
    try:
        return DeepSeekPriceSnapshot.model_validate(payload)
    except ValueError as exc:
        raise BudgetInvariantError(
            "The versioned DeepSeek pricing snapshot is invalid."
        ) from exc


class SQLiteBudgetLedger:
    """Persistent, process-safe upper-bound ledger for paid model attempts."""

    def __init__(
        self,
        *,
        path: Path,
        hard_limit_cny: Decimal,
        execution_limit_cny: Decimal,
    ):
        self.path = path
        self.hard_limit_units = cny_to_units(hard_limit_cny)
        self.execution_limit_units = cny_to_units(execution_limit_cny)
        if self.execution_limit_units > self.hard_limit_units:
            raise BudgetInvariantError(
                "Execution limit cannot exceed the hard budget limit."
            )
        self._prepare_private_path()
        self._initialize()

    def _prepare_private_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        if self.path.is_symlink():
            raise BudgetInvariantError("Budget ledger cannot be a symbolic link.")
        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS budget_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_runs (
                    run_id TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    model TEXT NOT NULL,
                    price_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS budget_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES budget_runs(run_id),
                    logical_call_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    reserved_units INTEGER NOT NULL,
                    settled_units INTEGER,
                    status TEXT NOT NULL,
                    settlement_mode TEXT,
                    usage_json TEXT,
                    provider_request_id TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    settled_at TEXT,
                    UNIQUE(run_id, logical_call_id, attempt_number)
                );
                """
            )
            expected_meta = {
                "schema_version": "1.0",
                "currency": "CNY",
                "units_per_cny": str(CNY_UNITS_PER_CNY),
                "hard_limit_units": str(self.hard_limit_units),
                "execution_limit_units": str(self.execution_limit_units),
            }
            for key, value in expected_meta.items():
                existing = connection.execute(
                    "SELECT value FROM budget_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO budget_meta(key, value) VALUES (?, ?)",
                        (key, value),
                    )
                elif existing["value"] != value:
                    raise BudgetInvariantError(
                        "Budget ledger configuration does not match this process."
                    )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def start_run(
        self,
        *,
        run_id: str,
        purpose: str,
        price_snapshot: DeepSeekPriceSnapshot,
    ) -> None:
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise BudgetInvariantError("Budget run_id is invalid.")
        if not purpose.strip():
            raise BudgetInvariantError("Budget purpose cannot be empty.")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO budget_runs(
                    run_id, purpose, model, price_sha256, status, started_at
                ) VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (
                    run_id,
                    purpose,
                    price_snapshot.model,
                    price_snapshot.sha256,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise BudgetInvariantError(
                "Budget run_id already exists; choose a fresh server run ID."
            ) from exc
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> BudgetReservation:
        return BudgetReservation(
            attempt_id=row["attempt_id"],
            run_id=row["run_id"],
            logical_call_id=row["logical_call_id"],
            attempt_number=row["attempt_number"],
            model=row["model"],
            reserved_units=row["reserved_units"],
        )

    @staticmethod
    def _committed_units(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(
                CASE
                    WHEN status IN ('settled_exact', 'settled_upper_bound')
                    THEN settled_units
                    ELSE reserved_units
                END
            ), 0) AS committed
            FROM budget_attempts
            WHERE status IN (
                'reserved',
                'uncertain',
                'settled_exact',
                'settled_upper_bound'
            )
            """
        ).fetchone()
        return int(row["committed"])

    def reserve_attempt(
        self,
        *,
        run_id: str,
        logical_call_id: str,
        attempt_number: int,
        model: str,
        reserved_units: int,
    ) -> BudgetReservation:
        if attempt_number < 1 or reserved_units < 1:
            raise BudgetInvariantError("Attempt reservation is invalid.")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT *
                FROM budget_attempts
                WHERE run_id = ?
                  AND logical_call_id = ?
                  AND attempt_number = ?
                """,
                (run_id, logical_call_id, attempt_number),
            ).fetchone()
            if existing is not None:
                if (
                    existing["model"] != model
                    or existing["reserved_units"] != reserved_units
                ):
                    raise BudgetInvariantError(
                        "Repeated attempt reservation parameters differ."
                    )
                connection.execute("COMMIT")
                return self._reservation_from_row(existing)

            run = connection.execute(
                """
                SELECT model, status
                FROM budget_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None or run["status"] != "active":
                raise BudgetInvariantError("Budget run is missing or not active.")
            if run["model"] != model:
                raise BudgetInvariantError(
                    "Attempt model does not match the budget run."
                )

            committed = self._committed_units(connection)
            if committed + reserved_units > self.execution_limit_units:
                raise BudgetExceededError(
                    "Paid model request blocked by the local CNY budget limit."
                )
            attempt_id = f"budget-attempt-{uuid.uuid4().hex}"
            created_at = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO budget_attempts(
                    attempt_id,
                    run_id,
                    logical_call_id,
                    attempt_number,
                    model,
                    reserved_units,
                    status,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    attempt_id,
                    run_id,
                    logical_call_id,
                    attempt_number,
                    model,
                    reserved_units,
                    created_at,
                ),
            )
            connection.execute("COMMIT")
            return BudgetReservation(
                attempt_id=attempt_id,
                run_id=run_id,
                logical_call_id=logical_call_id,
                attempt_number=attempt_number,
                model=model,
                reserved_units=reserved_units,
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def settle_attempt(
        self,
        *,
        reservation: BudgetReservation,
        price_snapshot: DeepSeekPriceSnapshot,
        usage: Mapping[str, Any],
        provider_request_id: str | None,
    ) -> UsageCost:
        cost = calculate_usage_cost(price_snapshot, usage)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM budget_attempts WHERE attempt_id = ?",
                (reservation.attempt_id,),
            ).fetchone()
            if row is None:
                raise BudgetInvariantError("Budget reservation is missing.")
            if row["status"] in _SETTLED_STATUSES:
                if (
                    row["settled_units"] != cost.units
                    or row["settlement_mode"] != cost.mode
                ):
                    raise BudgetInvariantError(
                        "Repeated settlement data does not match."
                    )
                connection.execute("COMMIT")
                return cost
            if row["status"] != "reserved":
                raise BudgetInvariantError(
                    "Only a reserved attempt can be settled."
                )
            if cost.units > row["reserved_units"]:
                connection.execute(
                    """
                    UPDATE budget_attempts
                    SET status = 'uncertain',
                        error_code = 'COST_EXCEEDS_RESERVATION',
                        settled_at = ?
                    WHERE attempt_id = ?
                    """,
                    (datetime.now(UTC).isoformat(), reservation.attempt_id),
                )
                connection.execute("COMMIT")
                raise BudgetInvariantError(
                    "Provider usage cost exceeded the reserved upper bound."
                )
            status = (
                "settled_exact"
                if cost.mode == "exact"
                else "settled_upper_bound"
            )
            safe_usage = {
                key: value
                for key, value in usage.items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
            }
            connection.execute(
                """
                UPDATE budget_attempts
                SET settled_units = ?,
                    status = ?,
                    settlement_mode = ?,
                    usage_json = ?,
                    provider_request_id = ?,
                    settled_at = ?
                WHERE attempt_id = ?
                """,
                (
                    cost.units,
                    status,
                    cost.mode,
                    json.dumps(safe_usage, sort_keys=True),
                    provider_request_id,
                    datetime.now(UTC).isoformat(),
                    reservation.attempt_id,
                ),
            )
            connection.execute("COMMIT")
            return cost
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_uncertain(
        self,
        *,
        reservation: BudgetReservation,
        error_code: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM budget_attempts WHERE attempt_id = ?",
                (reservation.attempt_id,),
            ).fetchone()
            if row is None:
                raise BudgetInvariantError("Budget reservation is missing.")
            if row["status"] == "reserved":
                connection.execute(
                    """
                    UPDATE budget_attempts
                    SET status = 'uncertain',
                        error_code = ?,
                        settled_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        error_code,
                        datetime.now(UTC).isoformat(),
                        reservation.attempt_id,
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def complete_run(self, run_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE budget_runs
                SET status = 'completed', completed_at = ?
                WHERE run_id = ? AND status = 'active'
                """,
                (datetime.now(UTC).isoformat(), run_id),
            )
            if updated.rowcount != 1:
                raise BudgetInvariantError("Budget run is missing or not active.")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def snapshot(self, *, run_id: str | None = None) -> dict[str, Any]:
        connection = self._connect()
        try:
            where = ""
            params: tuple[str, ...] = ()
            if run_id is not None:
                where = "WHERE run_id = ?"
                params = (run_id,)
            row = connection.execute(
                f"""
                SELECT
                    COALESCE(SUM(
                        CASE
                            WHEN status IN (
                                'settled_exact',
                                'settled_upper_bound'
                            )
                            THEN settled_units
                            ELSE reserved_units
                        END
                    ), 0) AS committed,
                    COALESCE(SUM(
                        CASE
                            WHEN status IN (
                                'settled_exact',
                                'settled_upper_bound'
                            )
                            THEN settled_units
                            ELSE 0
                        END
                    ), 0) AS settled,
                    SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END)
                        AS reserved_count,
                    SUM(CASE WHEN status = 'uncertain' THEN 1 ELSE 0 END)
                        AS uncertain_count,
                    COUNT(*) AS attempt_count
                FROM budget_attempts
                {where}
                """,
                params,
            ).fetchone()
            committed = int(row["committed"])
            return {
                "currency": "CNY",
                "hard_limit_cny": format_cny(self.hard_limit_units),
                "execution_limit_cny": format_cny(
                    self.execution_limit_units
                ),
                "committed_cny": format_cny(committed),
                "settled_cny": format_cny(int(row["settled"])),
                "remaining_execution_cny": format_cny(
                    max(0, self.execution_limit_units - committed)
                ),
                "attempt_count": int(row["attempt_count"]),
                "reserved_count": int(row["reserved_count"] or 0),
                "uncertain_count": int(row["uncertain_count"] or 0),
            }
        finally:
            connection.close()


class DeepSeekBudgetGuard:
    """Reserve before every provider attempt and settle only trusted usage."""

    def __init__(
        self,
        *,
        ledger: SQLiteBudgetLedger,
        run_id: str,
        purpose: str,
        price_snapshot: DeepSeekPriceSnapshot,
        model: str,
        max_output_tokens: int,
        now: datetime | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        price_snapshot.require_current(
            expected_model=model,
            now=now or self._now_provider(),
        )
        self._ledger = ledger
        self._run_id = run_id
        self._price_snapshot = price_snapshot
        self._model = model
        self._reservation_cost = worst_case_attempt_cost(
            price_snapshot,
            max_output_tokens=max_output_tokens,
        )
        self._closed = False
        self._ledger.start_run(
            run_id=run_id,
            purpose=purpose,
            price_snapshot=price_snapshot,
        )

    def reserve_attempt(
        self,
        *,
        logical_call_id: str,
        attempt_number: int,
    ) -> BudgetReservation:
        if self._closed:
            raise BudgetInvariantError("Budget guard is already closed.")
        self._price_snapshot.require_current(
            expected_model=self._model,
            now=self._now_provider(),
        )
        return self._ledger.reserve_attempt(
            run_id=self._run_id,
            logical_call_id=logical_call_id,
            attempt_number=attempt_number,
            model=self._model,
            reserved_units=self._reservation_cost.units,
        )

    def settle_attempt(
        self,
        *,
        reservation: BudgetReservation,
        usage: Mapping[str, Any],
        provider_request_id: str | None,
    ) -> UsageCost:
        try:
            return self._ledger.settle_attempt(
                reservation=reservation,
                price_snapshot=self._price_snapshot,
                usage=usage,
                provider_request_id=provider_request_id,
            )
        except BudgetUsageError:
            self._ledger.mark_uncertain(
                reservation=reservation,
                error_code="INVALID_PROVIDER_USAGE",
            )
            raise

    def mark_uncertain(
        self,
        *,
        reservation: BudgetReservation,
        error_code: str,
    ) -> None:
        self._ledger.mark_uncertain(
            reservation=reservation,
            error_code=error_code,
        )

    def snapshot(self) -> dict[str, Any]:
        run_snapshot = self._ledger.snapshot(run_id=self._run_id)
        cumulative_snapshot = self._ledger.snapshot()
        run_snapshot["remaining_execution_cny"] = cumulative_snapshot[
            "remaining_execution_cny"
        ]
        return {
            "schema_version": "1.0",
            "enforcement_mode": "persistent_sqlite",
            "run_status": "completed" if self._closed else "active",
            "price": {
                "provider": self._price_snapshot.provider,
                "model": self._price_snapshot.model,
                "currency": self._price_snapshot.currency,
                "snapshot_sha256": self._price_snapshot.sha256,
                "source_url": self._price_snapshot.source_url,
                "usage_source_url": self._price_snapshot.usage_source_url,
                "captured_at": self._price_snapshot.captured_at.isoformat(),
                "valid_until": self._price_snapshot.valid_until.isoformat(),
                "rates_cny": self._price_snapshot.rates_cny.model_dump(),
                "tokens_per_price_unit": (
                    self._price_snapshot.tokens_per_price_unit
                ),
            },
            "reservation_cny_per_attempt": format_cny(
                self._reservation_cost.units
            ),
            "run": run_snapshot,
            "cumulative": cumulative_snapshot,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._ledger.complete_run(self._run_id)
        self._closed = True
