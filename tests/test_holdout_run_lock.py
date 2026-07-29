from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from evals.evidence import stable_sha256
from evals.holdout_lock import (
    HoldoutLockError,
    acquire_holdout_run_lock,
    finalize_holdout_run_lock,
    validate_holdout_declaration,
)
from evals.readonly_eval import ReadonlyEvalCase
from evals.readonly_reporting import current_readonly_harness_fingerprints
from evals.run_readonly_agent_evals import _build_parser, _validate_args


def _cases() -> list[ReadonlyEvalCase]:
    return [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": "sealed-case-one",
                "user_message": "请查我的订单。",
                "expected": {},
            }
        )
    ]


def _manifest(
    path: Path,
    *,
    formal_runs_allowed: int = 1,
    formal_runs_completed: int = 0,
    scorer_sha256: str | None = None,
    settings: Settings | None = None,
) -> Path:
    cases = _cases()
    harness = current_readonly_harness_fingerprints(settings)
    payload = {
        "schema_version": "1.0",
        "case_set_name": "readonly-holdout-v2",
        "case_count": len(cases),
        "case_set_sha256": stable_sha256(
            [case.model_dump(mode="json") for case in cases]
        ),
        **harness,
        "formal_runs_allowed": formal_runs_allowed,
        "formal_runs_completed": formal_runs_completed,
        "lifecycle_status": "sealed",
        "rerun_policy": "prohibited",
    }
    if scorer_sha256 is not None:
        payload["scorer_sha256"] = scorer_sha256
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def test_holdout_lock_is_exclusive_and_final_status_is_persisted(
    tmp_path: Path,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
    )
    lock_root = tmp_path / "private-locks"
    lock_path = acquire_holdout_run_lock(
        lock_root=lock_root,
        declaration=declaration,
        run_id="eval-20260729-holdout-v2",
        now=datetime(2026, 7, 29, 10, tzinfo=UTC),
    )

    assert stat.S_IMODE(lock_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    with pytest.raises(HoldoutLockError, match="already been consumed"):
        acquire_holdout_run_lock(
            lock_root=lock_root,
            declaration=declaration,
            run_id="eval-20260729-holdout-v2-second",
        )

    finalize_holdout_run_lock(
        lock_path=lock_path,
        status="completed",
        run_id="eval-20260729-holdout-v2",
        now=datetime(2026, 7, 29, 10, 5, tzinfo=UTC),
    )
    lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock_payload["status"] == "completed"
    assert lock_payload["run_id"] == "eval-20260729-holdout-v2"
    assert "case_set_sha256" in lock_payload
    assert "user_message" not in json.dumps(lock_payload)


def test_same_case_hash_cannot_get_a_second_lock_by_renaming(
    tmp_path: Path,
) -> None:
    declaration = validate_holdout_declaration(
        manifest_path=_manifest(tmp_path / "manifest.json"),
        case_set_name="readonly-holdout-v2",
        cases=_cases(),
    )
    acquire_holdout_run_lock(
        lock_root=tmp_path / "private-locks",
        declaration=declaration,
        run_id="eval-20260729-holdout-v2",
    )

    renamed = replace(
        declaration,
        case_set_name="renamed-holdout-v2",
        manifest_sha256="f" * 64,
    )
    with pytest.raises(HoldoutLockError, match="already been consumed"):
        acquire_holdout_run_lock(
            lock_root=tmp_path / "private-locks",
            declaration=renamed,
            run_id="eval-20260729-renamed-holdout-v2",
        )


@pytest.mark.parametrize(
    (
        "formal_runs_allowed",
        "formal_runs_completed",
        "scorer_sha256",
        "message",
    ),
    [
        (1, 1, None, "formal run is no longer available"),
        (True, 0, None, "formal run is no longer available"),
        (1, False, None, "formal run is no longer available"),
        (1, 0, "0" * 64, "frozen harness"),
    ],
)
def test_holdout_declaration_fails_closed_before_model_use(
    tmp_path: Path,
    formal_runs_allowed: int,
    formal_runs_completed: int,
    scorer_sha256: str | None,
    message: str,
) -> None:
    with pytest.raises(HoldoutLockError, match=message):
        validate_holdout_declaration(
            manifest_path=_manifest(
                tmp_path / "manifest.json",
                formal_runs_allowed=formal_runs_allowed,
                formal_runs_completed=formal_runs_completed,
                scorer_sha256=scorer_sha256,
            ),
            case_set_name="readonly-holdout-v2",
            cases=_cases(),
        )


def test_holdout_declaration_freezes_paid_model_runtime(
    tmp_path: Path,
) -> None:
    sealed_settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=0,
        deepseek_max_tokens=1024,
        deepseek_max_retries=2,
        deepseek_timeout_seconds=30,
        agent_max_tool_rounds=4,
        agent_max_tool_calls=12,
    )
    manifest_path = _manifest(
        tmp_path / "manifest.json",
        settings=sealed_settings,
    )
    changed_settings = Settings(
        deepseek_model="deepseek-v4-flash",
        deepseek_temperature=1,
        deepseek_max_tokens=1,
        deepseek_max_retries=0,
        deepseek_timeout_seconds=5,
        agent_max_tool_rounds=1,
        agent_max_tool_calls=1,
    )

    assert (
        current_readonly_harness_fingerprints(sealed_settings)
        != current_readonly_harness_fingerprints(changed_settings)
    )
    with pytest.raises(HoldoutLockError, match="frozen harness"):
        validate_holdout_declaration(
            manifest_path=manifest_path,
            case_set_name="readonly-holdout-v2",
            cases=_cases(),
            settings=changed_settings,
        )


def test_holdout_cli_requires_a_declared_manifest() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--purpose",
            "holdout_formal",
            "--split",
            "holdout",
            "--case-dir",
            "private-holdout-cases",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4",
        ]
    )

    with pytest.raises(SystemExit):
        _validate_args(parser, args)


@pytest.mark.parametrize("purpose", ["diagnostic", "dev_repeat"])
def test_holdout_split_cannot_bypass_the_formal_runner(
    purpose: str,
) -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--purpose",
            purpose,
            "--split",
            "holdout",
            "--case-dir",
            "private-holdout-cases",
            "--case-set-name",
            "readonly-holdout-v2",
            "--trials",
            "4" if purpose == "dev_repeat" else "1",
        ]
    )

    with pytest.raises(SystemExit):
        _validate_args(parser, args)
