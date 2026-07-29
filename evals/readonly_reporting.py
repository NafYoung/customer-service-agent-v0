from __future__ import annotations

import math
import platform
import subprocess
import uuid
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import urlparse

from app.agent.deepseek_budget import (
    BudgetUsageError,
    calculate_usage_cost_from_rates,
    cny_to_units,
)
from app.config import Settings
from app.tools.contracts import get_read_only_tool_contracts
from evals.evidence import stable_sha256
from evals.evidence_schema import BudgetSummary
from evals.file_snapshot import FileSnapshot, read_file_snapshot
from evals.readonly_eval import (
    SCORE_CATEGORIES,
    ReadonlyEvalCase,
    ReadonlyEvalResult,
)
from evals.semantic_judge import (
    SEMANTIC_JUDGE_PROMPT_PATH,
    SEMANTIC_JUDGE_VERSION,
)

if TYPE_CHECKING:
    from evals.calibration_attestation import (
        ValidatedCalibrationAttestation,
        ValidatedCalibrationReview,
    )

ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "app" / "agent" / "readonly_system_prompt.md"
AGENT_LOOP_PATH = ROOT / "app" / "agent" / "readonly.py"
SCORER_PATH = ROOT / "evals" / "readonly_eval.py"
SEED_PATH = ROOT / "app" / "seed.py"
SETTINGS_PATH = ROOT / "app" / "config.py"
MODEL_ADAPTER_PATH = ROOT / "app" / "agent" / "openai_compatible.py"
MODEL_FACTORY_PATH = ROOT / "app" / "agent" / "factory.py"
EVAL_RUNNER_PATH = ROOT / "evals" / "run_readonly_agent_evals.py"
SEMANTIC_JUDGE_SOURCE_PATH = ROOT / "evals" / "semantic_judge.py"
SEMANTIC_CALIBRATION_SOURCE_PATH = (
    ROOT / "evals" / "semantic_calibration.py"
)
SEMANTIC_CALIBRATION_VALIDATOR_PATH = (
    ROOT / "evals" / "calibration_attestation.py"
)
SEMANTIC_CALIBRATION_RUNNER_PATH = (
    ROOT / "evals" / "run_semantic_judge_calibration.py"
)
SEMANTIC_CALIBRATION_FIXTURE_PATH = (
    ROOT / "evals" / "semantic_judge_calibration_cases.jsonl"
)
FILE_SNAPSHOT_PATH = ROOT / "evals" / "file_snapshot.py"
HOLDOUT_LOCK_PATH = ROOT / "evals" / "holdout_lock.py"
EVIDENCE_SCHEMA_PATH = ROOT / "evals" / "evidence_schema.py"
READONLY_REPORTING_PATH = ROOT / "evals" / "readonly_reporting.py"
EVIDENCE_SOURCE_PATH = ROOT / "evals" / "evidence.py"
BUDGET_GUARD_PATH = ROOT / "app" / "agent" / "deepseek_budget.py"
POLICY_DIR = ROOT / "policies"
_SOURCE_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".yml", ".yaml"}
_SOURCE_ROOTS = ("app", "evals", "policies", "scripts", "tests")
_TOP_LEVEL_SOURCES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
)
READONLY_SCORER_VERSION = "readonly-scorer-v6"


@dataclass(frozen=True)
class FormalHoldoutEvidence:
    declaration_manifest_sha256: str
    lock_start_receipt_sha256: str
    declared_harness_sha256: str


@dataclass(frozen=True)
class FrozenReadonlyHarness:
    """One in-memory identity shared by declaration, execution, and evidence."""

    agent_system_prompt: str
    semantic_judge_system_prompt: str
    policy_documents: Mapping[str, str]
    tool_contracts: tuple[dict[str, Any], ...]
    calibration_fixture_snapshot: FileSnapshot
    fingerprints: Mapping[str, str]


def create_server_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ").casefold()
    return f"eval-{timestamp}-{uuid.uuid4().hex[:12]}"


def _file_sha256(path: Path) -> str:
    return read_file_snapshot(path).sha256


def _source_fingerprints() -> dict[str, str]:
    paths: set[Path] = set()
    for root_name in _SOURCE_ROOTS:
        source_root = ROOT / root_name
        if not source_root.exists():
            continue
        paths.update(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and path.suffix in _SOURCE_SUFFIXES
            and "__pycache__" not in path.parts
        )
    paths.update(
        path
        for name in _TOP_LEVEL_SOURCES
        if (path := ROOT / name).is_file()
    )
    return {
        path.relative_to(ROOT).as_posix(): _file_sha256(path)
        for path in sorted(paths)
    }


def _git_snapshot() -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    return commit or None, dirty


def require_clean_git_worktree(
    *,
    expected_commit: str | None = None,
) -> str:
    """Fail before paid work unless HEAD is resolved and the tree is clean."""

    commit, dirty = _git_snapshot()
    if commit is None:
        raise ValueError(
            "A resolved Git commit is required for formal evidence."
        )
    if dirty is not False:
        raise ValueError(
            "A clean Git worktree is required for formal evidence."
        )
    if expected_commit is not None and commit != expected_commit:
        raise ValueError(
            "The Git commit changed during formal evidence collection."
        )
    return commit


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package_name in ("fastapi", "httpx", "pydantic", "sqlalchemy"):
        try:
            versions[package_name] = metadata.version(package_name)
        except metadata.PackageNotFoundError:
            versions[package_name] = "not-installed"
    return versions


def freeze_readonly_harness(
    settings: Settings | None = None,
) -> FrozenReadonlyHarness:
    """Read every runtime text input once and fingerprint those same bytes."""

    runtime_settings = settings or Settings()
    prompt_snapshot = read_file_snapshot(PROMPT_PATH)
    semantic_prompt_snapshot = read_file_snapshot(
        SEMANTIC_JUDGE_PROMPT_PATH
    )
    calibration_fixture_snapshot = read_file_snapshot(
        SEMANTIC_CALIBRATION_FIXTURE_PATH
    )
    policy_snapshots = {
        path.name: read_file_snapshot(path)
        for path in sorted(POLICY_DIR.iterdir())
        if path.is_file()
    }
    policy_documents = MappingProxyType(
        {
            name: snapshot.text()
            for name, snapshot in policy_snapshots.items()
        }
    )
    policy_fingerprints = {
        f"policies/{name}": snapshot.sha256
        for name, snapshot in policy_snapshots.items()
    }
    tool_contracts = tuple(
        deepcopy(get_read_only_tool_contracts())
    )
    evidence_protocol_paths = (
        FILE_SNAPSHOT_PATH,
        HOLDOUT_LOCK_PATH,
        EVIDENCE_SCHEMA_PATH,
        READONLY_REPORTING_PATH,
        EVIDENCE_SOURCE_PATH,
        BUDGET_GUARD_PATH,
    )
    evidence_protocol_fingerprints = {
        path.relative_to(ROOT).as_posix(): _file_sha256(path)
        for path in evidence_protocol_paths
    }
    fingerprints = {
        "prompt_sha256": prompt_snapshot.sha256,
        "tool_contracts_sha256": stable_sha256(
            tool_contracts
        ),
        "policies_sha256": stable_sha256(policy_fingerprints),
        "seed_sha256": _file_sha256(SEED_PATH),
        "agent_loop_sha256": _file_sha256(AGENT_LOOP_PATH),
        "scorer_version": READONLY_SCORER_VERSION,
        "scorer_sha256": _file_sha256(SCORER_PATH),
        "semantic_judge_version": SEMANTIC_JUDGE_VERSION,
        "semantic_judge_prompt_sha256": semantic_prompt_snapshot.sha256,
        "semantic_judge_source_sha256": _file_sha256(
            SEMANTIC_JUDGE_SOURCE_PATH
        ),
        "semantic_calibration_source_sha256": _file_sha256(
            SEMANTIC_CALIBRATION_SOURCE_PATH
        ),
        "semantic_calibration_validator_sha256": _file_sha256(
            SEMANTIC_CALIBRATION_VALIDATOR_PATH
        ),
        "semantic_calibration_runner_sha256": _file_sha256(
            SEMANTIC_CALIBRATION_RUNNER_PATH
        ),
        "semantic_calibration_corpus_sha256": (
            calibration_fixture_snapshot.sha256
        ),
        "evidence_protocol_sha256": stable_sha256(
            evidence_protocol_fingerprints
        ),
        "model_runtime_sha256": stable_sha256(
            {
                "settings_source_sha256": _file_sha256(SETTINGS_PATH),
                "adapter_source_sha256": _file_sha256(MODEL_ADAPTER_PATH),
                "factory_source_sha256": _file_sha256(MODEL_FACTORY_PATH),
                "runner_source_sha256": _file_sha256(EVAL_RUNNER_PATH),
                "provider": "deepseek",
                "base_url": runtime_settings.deepseek_base_url.rstrip("/"),
                "requested_model": runtime_settings.deepseek_model,
                "stream": False,
                "thinking": "disabled",
                "temperature": runtime_settings.deepseek_temperature,
                "tool_choice": "auto",
                "semantic_judge_response_format": "json_object",
                "seed": None,
                "max_tokens": runtime_settings.deepseek_max_tokens,
                "timeout_seconds": runtime_settings.deepseek_timeout_seconds,
                "max_retries": runtime_settings.deepseek_max_retries,
                "agent_max_tool_rounds": (
                    runtime_settings.agent_max_tool_rounds
                ),
                "agent_max_tool_calls": runtime_settings.agent_max_tool_calls,
            }
        ),
    }
    return FrozenReadonlyHarness(
        agent_system_prompt=prompt_snapshot.text(),
        semantic_judge_system_prompt=semantic_prompt_snapshot.text(),
        policy_documents=policy_documents,
        tool_contracts=tool_contracts,
        calibration_fixture_snapshot=calibration_fixture_snapshot,
        fingerprints=MappingProxyType(fingerprints),
    )


def current_readonly_harness_fingerprints(
    settings: Settings | None = None,
) -> dict[str, str]:
    """Return a detached copy of the current frozen holdout identity."""

    return dict(freeze_readonly_harness(settings).fingerprints)


def _rate(passed: int, total: int) -> float:
    return round(passed / total, 6) if total else 0.0


def _distribution(values: Sequence[int]) -> dict[str, int | float | None]:
    if not values:
        return {
            "p50": None,
            "p95": None,
            "max": None,
            "total": 0,
        }
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        p50: int | float = ordered[midpoint]
    else:
        p50 = (ordered[midpoint - 1] + ordered[midpoint]) / 2
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "p50": p50,
        "p95": ordered[p95_index],
        "max": ordered[-1],
        "total": sum(ordered),
    }


def offline_budget_report() -> dict[str, Any]:
    empty = {
        "currency": "CNY",
        "hard_limit_cny": "20",
        "execution_limit_cny": "18",
        "committed_cny": "0",
        "settled_cny": "0",
        "remaining_execution_cny": "18",
        "attempt_count": 0,
        "reserved_count": 0,
        "uncertain_count": 0,
    }
    return {
        "schema_version": "1.0",
        "enforcement_mode": "offline_no_paid_provider",
        "run_status": "completed",
        "price": None,
        "reservation_cny_per_attempt": "0",
        "run": dict(empty),
        "cumulative": dict(empty),
    }


def _budget_manifest(report: dict[str, Any]) -> dict[str, Any]:
    price = report.get("price")
    run = report["run"]
    return {
        "schema_version": "1.0",
        "enforcement_mode": report["enforcement_mode"],
        "run_status": report.get("run_status"),
        "currency": run["currency"],
        "hard_limit_cny": run["hard_limit_cny"],
        "execution_limit_cny": run["execution_limit_cny"],
        "reservation_cny_per_attempt": report[
            "reservation_cny_per_attempt"
        ],
        "price_snapshot_sha256": (
            price["snapshot_sha256"] if price is not None else None
        ),
        "price_source_url": (
            price["source_url"] if price is not None else None
        ),
        "usage_source_url": (
            price["usage_source_url"] if price is not None else None
        ),
        "price_captured_at": (
            price["captured_at"] if price is not None else None
        ),
        "price_valid_until": (
            price["valid_until"] if price is not None else None
        ),
    }


def summarize_results(
    *,
    run_id: str,
    results: Sequence[ReadonlyEvalResult],
    planned_trials: int,
    budget_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if planned_trials < 1:
        raise ValueError("planned_trials must be at least 1")

    total = len(results)
    strict_passed = sum(result.passed for result in results)
    grouped: dict[str, list[ReadonlyEvalResult]] = defaultdict(list)
    for result in results:
        grouped[result.case_id].append(result)
    cases_all_trials_passed = sum(
        len(case_results) == planned_trials
        and all(result.passed for result in case_results)
        for case_results in grouped.values()
    )

    layer_summary: dict[str, dict[str, int | float]] = {}
    for category in SCORE_CATEGORIES:
        passed = sum(
            result.score_status[category]
            for result in results
        )
        layer_summary[category] = {
            "passed": passed,
            "failed": total - passed,
            "rate": _rate(passed, total),
        }

    usage_totals: Counter[str] = Counter()
    model_call_latencies: list[int] = []
    model_call_count = 0
    for result in results:
        for call in result.model_calls:
            model_call_count += 1
            model_call_latencies.append(call.latency_ms)
            if call.usage:
                usage_totals.update(call.usage)

    security_passed = sum(
        result.score_status["security"]
        for result in results
    )
    changed_trials = sum(
        bool(
            result.business_state_delta
            and result.business_state_delta.changed
        )
        for result in results
    )
    unknown_state_trials = sum(
        result.business_state_delta is None
        for result in results
    )
    error_counts = Counter(
        result.error_code
        for result in results
        if result.error_code is not None
    )

    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "total_cases": len(grouped),
        "planned_trials": planned_trials,
        "total_trials": total,
        "strict": {
            "passed": strict_passed,
            "failed": total - strict_passed,
            "rate": _rate(strict_passed, total),
        },
        "reliability": {
            "k": planned_trials,
            "cases_all_trials_passed": cases_all_trials_passed,
            "case_count": len(grouped),
            "pass_power_k": _rate(
                cases_all_trials_passed,
                len(grouped),
            ),
        },
        "security": {
            "passed": security_passed,
            "failed": total - security_passed,
            "rate": _rate(security_passed, total),
            "all_trials_passed": security_passed == total,
        },
        "score_layers": layer_summary,
        "usage": {
            "model_calls": model_call_count,
            **{
                key: usage_totals[key]
                for key in sorted(usage_totals)
            },
        },
        "latency_ms": {
            "case": _distribution(
                [result.duration_ms for result in results]
            ),
            "model_call": _distribution(model_call_latencies),
        },
        "business_state": {
            "changed_trials": changed_trials,
            "unknown_trials": unknown_state_trials,
            "all_trials_unchanged": (
                changed_trials == 0 and unknown_state_trials == 0
            ),
        },
        "errors": dict(sorted(error_counts.items())),
        "budget": budget_report or offline_budget_report(),
    }


def result_to_record(
    result: ReadonlyEvalResult,
    *,
    split: str,
) -> dict[str, Any]:
    business_state = (
        asdict(result.business_state_delta)
        if result.business_state_delta is not None
        else {
            "changed": None,
            "changed_tables": [],
            "before_sha256": None,
            "after_sha256": None,
        }
    )
    return {
        "schema_version": "1.0",
        "case_id": result.case_id,
        "split": split,
        "trial": result.trial,
        "case_run_id": result.case_run_id,
        "input_sha256": result.input_sha256,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "duration_ms": result.duration_ms,
        "status": "passed" if result.passed else "failed",
        "termination_reason": result.error_code or "completed",
        "error_code": result.error_code,
        "final_text": result.final_text,
        "model_calls": [
            asdict(call)
            for call in result.model_calls
        ],
        "tool_trace": [
            asdict(item)
            for item in result.tool_trace
        ],
        "business_state": business_state,
        "counted_action_records": result.business_write_count,
        "scores": result.score_status,
        "score_checks": [
            asdict(check)
            for check in result.score_checks
        ],
        "checks": list(result.checks),
        "failures": list(result.failures),
    }


def build_readonly_manifest(
    *,
    run_id: str,
    purpose: str,
    split: str,
    case_set_name: str,
    cases: Sequence[ReadonlyEvalCase],
    results: Sequence[ReadonlyEvalResult],
    settings: Settings,
    planned_trials: int,
    started_at: datetime,
    completed_at: datetime,
    budget_report: dict[str, Any] | None = None,
    calibration_attestation: (
        ValidatedCalibrationAttestation | None
    ) = None,
    calibration_review: ValidatedCalibrationReview | None = None,
    formal_holdout_evidence: FormalHoldoutEvidence | None = None,
    harness_fingerprints: dict[str, str] | None = None,
    source_git_commit: str | None = None,
) -> dict[str, Any]:
    if purpose not in {"diagnostic", "dev_repeat", "holdout_formal"}:
        raise ValueError("Unsupported Eval purpose")
    if split not in {"dev", "holdout"}:
        raise ValueError("Unsupported Eval split")
    if planned_trials < 1:
        raise ValueError("planned_trials must be at least 1")
    runtime_harness_fingerprints = (
        dict(harness_fingerprints)
        if harness_fingerprints is not None
        else current_readonly_harness_fingerprints(settings)
    )
    if purpose == "holdout_formal":
        if split != "holdout":
            raise ValueError("holdout_formal requires the holdout split")
        if (
            calibration_attestation is None
            or calibration_review is None
            or formal_holdout_evidence is None
        ):
            raise ValueError(
                "formal holdout evidence requires calibration and lock "
                "attestations"
            )
        if (
            formal_holdout_evidence.declared_harness_sha256
            != stable_sha256(runtime_harness_fingerprints)
        ):
            raise ValueError(
                "formal holdout declaration does not match runtime harness"
            )
        if (
            len(cases) != 20
            or planned_trials != 4
            or any(
                case.expected.semantic_contract is None
                for case in cases
            )
        ):
            raise ValueError(
                "formal holdout requires 20 semantic cases and 4 trials"
            )
        result_counts = Counter(result.case_id for result in results)
        if (
            len(results) != 80
            or set(result_counts) != {case.case_id for case in cases}
            or any(count != 4 for count in result_counts.values())
        ):
            raise ValueError(
                "formal holdout evidence is missing case trials"
            )
        try:
            validated_budget = BudgetSummary.model_validate(
                budget_report
            )
        except ValueError as exc:
            raise ValueError(
                "formal holdout budget evidence is invalid"
            ) from exc
        if (
            validated_budget.enforcement_mode != "persistent_sqlite"
            or validated_budget.run_status != "completed"
            or validated_budget.run.reserved_count != 0
            or validated_budget.run.uncertain_count != 0
            or validated_budget.cumulative.reserved_count != 0
            or validated_budget.cumulative.uncertain_count != 0
            or (
                validated_budget.run.committed_cny
                != validated_budget.run.settled_cny
            )
            or (
                validated_budget.cumulative.committed_cny
                != validated_budget.cumulative.settled_cny
            )
        ):
            raise ValueError(
                "formal holdout budget must be completed and settled"
            )
        provider_attempts = sum(
            call.provider_attempts or 0
            for result in results
            for call in result.model_calls
        )
        if validated_budget.run.attempt_count != provider_attempts:
            raise ValueError(
                "formal holdout budget attempts do not match evidence"
            )
        model_calls = [
            call
            for result in results
            for call in result.model_calls
        ]
        if (
            validated_budget.price is None
            or any(
                call.status != "success"
                or call.usage is None
                or call.provider_attempts != 1
                for call in model_calls
            )
        ):
            raise ValueError(
                "formal holdout calls require exact single-attempt usage"
            )
        try:
            recomputed_cost_units = sum(
                calculate_usage_cost_from_rates(
                    rates_cny=(
                        validated_budget.price.rates_cny.model_dump()
                    ),
                    tokens_per_price_unit=(
                        validated_budget.price.tokens_per_price_unit
                    ),
                    usage=call.usage,
                ).units
                for call in model_calls
                if call.usage is not None
            )
        except BudgetUsageError as exc:
            raise ValueError(
                "formal holdout call usage could not be priced"
            ) from exc
        if cny_to_units(
            Decimal(validated_budget.run.settled_cny)
        ) != recomputed_cost_units:
            raise ValueError(
                "formal holdout settled cost does not match call usage"
            )
        drifted_models = {
            call.observed_model
            for result in results
            for call in result.model_calls
            if call.observed_model is not None
            and call.observed_model != settings.deepseek_model
        }
        observed_formal_models = {
            call.observed_model
            for result in results
            for call in result.model_calls
            if call.observed_model is not None
        }
        if (
            drifted_models
            or observed_formal_models != {settings.deepseek_model}
        ):
            raise ValueError(
                "formal holdout observed model does not match requested model"
            )
    elif (
        calibration_attestation is not None
        or calibration_review is not None
        or formal_holdout_evidence is not None
    ):
        raise ValueError(
            "formal attestations are only valid for holdout_formal"
        )

    commit, dirty = _git_snapshot()
    if source_git_commit is not None:
        if commit != source_git_commit or dirty is not False:
            raise ValueError(
                "formal source commit changed or became dirty during the run"
            )
        commit = source_git_commit
    source_fingerprints = _source_fingerprints()
    case_payloads = [
        case.model_dump(mode="json")
        for case in sorted(cases, key=lambda item: item.case_id)
    ]
    result_counts = Counter(result.case_id for result in results)
    completed_trials = (
        min(result_counts.values())
        if result_counts and len(result_counts) == len(cases)
        else 0
    )
    observed_models = sorted(
        {
            call.observed_model
            for result in results
            for call in result.model_calls
            if call.observed_model
        }
    )
    harness_fingerprints = runtime_harness_fingerprints
    eval_metadata: dict[str, Any] = {
        "suite_name": "readonly-agent",
        "suite_version": "2.0",
        "split": split,
        "case_set_name": case_set_name,
        "case_count": len(cases),
        "case_set_sha256": stable_sha256(case_payloads),
        "scorer_version": harness_fingerprints["scorer_version"],
        "scorer_sha256": harness_fingerprints["scorer_sha256"],
    }
    if split == "dev":
        eval_metadata["case_ids"] = [case.case_id for case in cases]
    if calibration_attestation is not None and calibration_review is not None:
        eval_metadata["semantic_calibration"] = {
            "report_sha256": calibration_attestation.report_sha256,
            "review_sha256": calibration_review.review_sha256,
            "run_id": calibration_attestation.run_id,
            "source_git_commit": (
                calibration_attestation.source_git_commit
            ),
            "fixture_sha256": calibration_attestation.fixture_sha256,
            "contract_set_sha256": (
                calibration_attestation.contract_set_sha256
            ),
            "harness_sha256": calibration_attestation.harness_sha256,
            "reviewer_id": calibration_review.reviewer_id,
            "reviewed_count": calibration_review.reviewed_count,
        }
    if formal_holdout_evidence is not None:
        eval_metadata["formal_holdout"] = asdict(
            formal_holdout_evidence
        )

    endpoint = urlparse(settings.deepseek_base_url)
    status = (
        "completed"
        if len(results) == len(cases) * planned_trials
        else "partial"
    )
    return {
        "schema_version": "2.0",
        "run_id": run_id,
        "purpose": purpose,
        "status": status,
        "created_at": started_at.astimezone(UTC).isoformat(),
        "completed_at": completed_at.astimezone(UTC).isoformat(),
        "source": {
            "git_commit": commit,
            "git_dirty": dirty,
            "source_tree_sha256": stable_sha256(source_fingerprints),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "package_versions": _package_versions(),
        },
        "eval": eval_metadata,
        "harness": {
            "runtime_harness_sha256": stable_sha256(
                harness_fingerprints
            ),
            "prompt_sha256": harness_fingerprints["prompt_sha256"],
            "tool_contracts_sha256": harness_fingerprints[
                "tool_contracts_sha256"
            ],
            "policies_sha256": harness_fingerprints["policies_sha256"],
            "seed_data_sha256": harness_fingerprints["seed_sha256"],
            "agent_loop_sha256": harness_fingerprints[
                "agent_loop_sha256"
            ],
            "model_runtime_sha256": harness_fingerprints[
                "model_runtime_sha256"
            ],
            "semantic_judge_version": harness_fingerprints[
                "semantic_judge_version"
            ],
            "semantic_judge_prompt_sha256": harness_fingerprints[
                "semantic_judge_prompt_sha256"
            ],
            "semantic_judge_source_sha256": harness_fingerprints[
                "semantic_judge_source_sha256"
            ],
            "semantic_calibration_source_sha256": harness_fingerprints[
                "semantic_calibration_source_sha256"
            ],
            "semantic_calibration_validator_sha256": harness_fingerprints[
                "semantic_calibration_validator_sha256"
            ],
            "semantic_calibration_runner_sha256": harness_fingerprints[
                "semantic_calibration_runner_sha256"
            ],
            "semantic_calibration_corpus_sha256": harness_fingerprints[
                "semantic_calibration_corpus_sha256"
            ],
            "evidence_protocol_sha256": harness_fingerprints[
                "evidence_protocol_sha256"
            ],
            "max_tool_rounds": settings.agent_max_tool_rounds,
            "max_tool_calls": settings.agent_max_tool_calls,
        },
        "model": {
            "provider": "deepseek",
            "requested_model": settings.deepseek_model,
            "observed_models": observed_models,
            "base_url_host": endpoint.hostname,
            "generation_config": {
                "stream": False,
                "thinking": "disabled",
                "temperature": settings.deepseek_temperature,
                "seed": None,
                "max_tokens": settings.deepseek_max_tokens,
            },
            "timeout_seconds": settings.deepseek_timeout_seconds,
            "retry_policy": {
                "max_retries": settings.deepseek_max_retries,
                "backoff": "bounded_exponential_with_jitter",
            },
            "semantic_judge": {
                "version": SEMANTIC_JUDGE_VERSION,
                "response_format": "json_object",
                "tools_enabled": False,
                "temperature": settings.deepseek_temperature,
                "thinking": "disabled",
            },
        },
        "execution": {
            "planned_trials": planned_trials,
            "completed_trials": completed_trials,
            "seed_policy": "provider_seed_not_configured",
            "concurrency": 1,
            "case_order": (
                [case.case_id for case in cases]
                if split == "dev"
                else "withheld"
            ),
        },
        "budget": _budget_manifest(
            budget_report or offline_budget_report()
        ),
    }
