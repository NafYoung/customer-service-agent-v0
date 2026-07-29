from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Sequence

from app.config import Settings
from evals.evidence import stable_sha256
from evals.readonly_eval import ReadonlyEvalCase
from evals.readonly_reporting import current_readonly_harness_fingerprints


class HoldoutLockError(RuntimeError):
    """A declared formal holdout cannot be started or finalized safely."""


@dataclass(frozen=True)
class HoldoutDeclaration:
    case_set_name: str
    case_set_sha256: str
    manifest_sha256: str
    scorer_version: str


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutLockError(
            "The declared holdout manifest could not be read safely."
        ) from exc
    if not isinstance(payload, dict):
        raise HoldoutLockError(
            "The declared holdout manifest must be a JSON object."
        )
    return payload


def validate_holdout_declaration(
    *,
    manifest_path: Path,
    case_set_name: str,
    cases: Sequence[ReadonlyEvalCase],
    settings: Settings | None = None,
) -> HoldoutDeclaration:
    """Bind a sealed case set to the exact current read-only harness."""

    manifest = _read_manifest(manifest_path)
    if (
        type(manifest.get("formal_runs_allowed")) is not int
        or manifest.get("formal_runs_allowed") != 1
        or type(manifest.get("formal_runs_completed")) is not int
        or manifest.get("formal_runs_completed") != 0
        or manifest.get("lifecycle_status") != "sealed"
        or manifest.get("rerun_policy") != "prohibited"
    ):
        raise HoldoutLockError(
            "The declared formal run is no longer available."
        )
    if manifest.get("case_set_name") != case_set_name:
        raise HoldoutLockError(
            "The declared holdout name does not match the requested case set."
        )

    case_payloads = [
        case.model_dump(mode="json")
        for case in sorted(cases, key=lambda item: item.case_id)
    ]
    case_set_sha256 = stable_sha256(case_payloads)
    if (
        type(manifest.get("case_count")) is not int
        or manifest.get("case_count") != len(cases)
        or manifest.get("case_set_sha256") != case_set_sha256
    ):
        raise HoldoutLockError(
            "The declared holdout cases do not match the sealed case set."
        )

    current_harness = current_readonly_harness_fingerprints(settings)
    if any(
        manifest.get(field_name) != expected
        for field_name, expected in current_harness.items()
    ):
        raise HoldoutLockError(
            "The current code does not match the frozen harness declaration."
        )

    return HoldoutDeclaration(
        case_set_name=case_set_name,
        case_set_sha256=case_set_sha256,
        manifest_sha256=_file_sha256(manifest_path),
        scorer_version=current_harness["scorer_version"],
    )


def _write_exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise HoldoutLockError(
            "This formal holdout declaration has already been consumed."
        ) from exc
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def acquire_holdout_run_lock(
    *,
    lock_root: Path,
    declaration: HoldoutDeclaration,
    run_id: str,
    now: datetime | None = None,
) -> Path:
    """Consume the one formal run immediately before provider use."""

    if lock_root.exists() and lock_root.is_symlink():
        raise HoldoutLockError("The private holdout lock root cannot be a symlink.")
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_root.chmod(0o700)
    lock_path = lock_root / f"{declaration.case_set_sha256}.json"
    created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    _write_exclusive_json(
        lock_path,
        {
            "schema_version": "1.0",
            "case_set_name": declaration.case_set_name,
            "case_set_sha256": declaration.case_set_sha256,
            "manifest_sha256": declaration.manifest_sha256,
            "scorer_version": declaration.scorer_version,
            "run_id": run_id,
            "status": "started",
            "created_at": created_at,
            "completed_at": None,
        },
    )
    return lock_path


def finalize_holdout_run_lock(
    *,
    lock_path: Path,
    status: Literal["completed", "failed"],
    run_id: str,
    now: datetime | None = None,
) -> None:
    """Record the terminal outcome without making the declaration reusable."""

    payload = _read_manifest(lock_path)
    if payload.get("run_id") != run_id or payload.get("status") != "started":
        raise HoldoutLockError(
            "The formal holdout lock cannot be finalized in its current state."
        )
    payload["status"] = status
    payload["completed_at"] = (
        now or datetime.now(UTC)
    ).astimezone(UTC).isoformat()

    temporary_path = lock_path.with_name(
        f".{lock_path.name}.{os.getpid()}.tmp"
    )
    _write_exclusive_json(temporary_path, payload)
    try:
        os.replace(temporary_path, lock_path)
        lock_path.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
