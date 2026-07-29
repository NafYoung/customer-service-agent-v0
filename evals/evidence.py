from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.openai_compatible import (
    AssistantTurn,
    ChatModel,
    Message,
    ModelAdapterError,
    ToolContract,
)
from app.database import Base

_RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{7,79}$")
_CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "auth_token",
    "cookie",
    "debug_admin_token",
    "deepseek_api_key",
    "email",
    "host_confirmation_token",
    "password",
    "secret",
    "set-cookie",
    "token",
    "verification_code",
}
_RUNTIME_TABLES = {"auth_sessions", "tool_events"}
_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s\"']+")
_MACOS_USER_PATH_PATTERN = re.compile(r"/Users/[^/\s]+/")


class ArtifactIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelToolCallEvidence:
    tool_call_id: str
    tool_name: str
    arguments: str


@dataclass(frozen=True)
class ModelCallEvidence:
    sequence: int
    status: str
    started_at: str
    latency_ms: int
    message_count: int
    tool_contract_count: int
    phase: str = "agent"
    tool_calls: tuple[ModelToolCallEvidence, ...] = ()
    finish_reason: str | None = None
    response_id: str | None = None
    observed_model: str | None = None
    usage: dict[str, int] | None = None
    error_code: str | None = None
    http_status: int | None = None
    provider_request_id: str | None = None
    provider_attempts: int | None = None


class ObservedChatModel:
    """Record non-content model-call metadata without changing model behavior."""

    def __init__(self, model: ChatModel, *, phase: str = "agent"):
        self._model = model
        self._phase = phase
        self.calls: list[ModelCallEvidence] = []

    def complete(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolContract],
    ) -> AssistantTurn:
        sequence = len(self.calls) + 1
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        try:
            turn = self._model.complete(messages=messages, tools=tools)
        except ModelAdapterError as exc:
            self.calls.append(
                ModelCallEvidence(
                    sequence=sequence,
                    status="error",
                    started_at=started_at,
                    latency_ms=_elapsed_ms(started),
                    message_count=len(messages),
                    tool_contract_count=len(tools),
                    phase=self._phase,
                    error_code=exc.code,
                    http_status=exc.status_code,
                    provider_request_id=exc.request_id,
                    provider_attempts=exc.attempts,
                )
            )
            raise
        except Exception:
            self.calls.append(
                ModelCallEvidence(
                    sequence=sequence,
                    status="error",
                    started_at=started_at,
                    latency_ms=_elapsed_ms(started),
                    message_count=len(messages),
                    tool_contract_count=len(tools),
                    phase=self._phase,
                    error_code="UNEXPECTED_MODEL_ERROR",
                )
            )
            raise

        self.calls.append(
            ModelCallEvidence(
                sequence=sequence,
                status="success",
                started_at=started_at,
                latency_ms=_elapsed_ms(started),
                message_count=len(messages),
                tool_contract_count=len(tools),
                phase=self._phase,
                tool_calls=tuple(
                    ModelToolCallEvidence(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        arguments=call.arguments,
                    )
                    for call in turn.tool_calls
                ),
                finish_reason=turn.finish_reason,
                response_id=turn.response_id,
                observed_model=turn.model,
                usage=dict(turn.usage) if turn.usage is not None else None,
                provider_request_id=turn.provider_request_id,
                provider_attempts=turn.provider_attempts,
            )
        )
        return turn


@dataclass(frozen=True)
class TableState:
    row_count: int
    sha256: str


@dataclass(frozen=True)
class BusinessStateSnapshot:
    sha256: str
    tables: dict[str, TableState]


@dataclass(frozen=True)
class BusinessStateDelta:
    changed: bool
    changed_tables: tuple[str, ...]
    before_sha256: str
    after_sha256: str


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def capture_business_state(session: Session) -> BusinessStateSnapshot:
    """Hash every non-runtime database table without exporting raw row values."""

    tables: dict[str, TableState] = {}
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        if table.name in _RUNTIME_TABLES:
            continue
        rows = [
            {
                column.name: _jsonable(row[column.name])
                for column in table.columns
            }
            for row in session.execute(select(table)).mappings()
        ]
        rows.sort(key=_canonical_json)
        tables[table.name] = TableState(
            row_count=len(rows),
            sha256=stable_sha256(rows),
        )

    return BusinessStateSnapshot(
        sha256=stable_sha256(
            {
                name: asdict(table_state)
                for name, table_state in tables.items()
            }
        ),
        tables=tables,
    )


def compare_business_states(
    before: BusinessStateSnapshot,
    after: BusinessStateSnapshot,
) -> BusinessStateDelta:
    table_names = sorted(set(before.tables) | set(after.tables))
    changed_tables = tuple(
        table_name
        for table_name in table_names
        if before.tables.get(table_name) != after.tables.get(table_name)
    )
    return BusinessStateDelta(
        changed=bool(changed_tables),
        changed_tables=changed_tables,
        before_sha256=before.sha256,
        after_sha256=after.sha256,
    )


def sanitize_for_evidence(
    value: Any,
    *,
    secret_values: Sequence[str] = (),
    key: str | None = None,
) -> Any:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return None
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for item_key, item_value in value.items():
            normalized_key = str(item_key)
            if normalized_key.casefold() in _SENSITIVE_KEYS:
                continue
            sanitized[normalized_key] = sanitize_for_evidence(
                item_value,
                secret_values=secret_values,
                key=normalized_key,
            )
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            sanitize_for_evidence(item, secret_values=secret_values)
            for item in value
        ]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return sanitize_for_evidence(
            value.value,
            secret_values=secret_values,
        )
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        sanitized_text = value
        for secret in sorted(
            {item for item in secret_values if item},
            key=len,
            reverse=True,
        ):
            sanitized_text = sanitized_text.replace(secret, "[REDACTED]")
        sanitized_text = _BEARER_PATTERN.sub("Bearer [REDACTED]", sanitized_text)
        sanitized_text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", sanitized_text)
        sanitized_text = _MACOS_USER_PATH_PATTERN.sub(
            "/Users/[REDACTED]/",
            sanitized_text,
        )
        return sanitized_text
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return sanitize_for_evidence(
        repr(value),
        secret_values=secret_values,
    )


def _write_private_text(path: Path, content: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_run_id(run_id: str) -> None:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            "run_id must be 8-80 lowercase URL-safe characters"
        )


def write_eval_bundle(
    *,
    output_root: Path,
    run_id: str,
    manifest: Mapping[str, Any],
    case_records: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    secret_values: Sequence[str] = (),
) -> Path:
    """Atomically persist a private, integrity-indexed Eval evidence bundle."""

    _validate_run_id(run_id)
    if manifest.get("run_id") not in {None, run_id}:
        raise ValueError("manifest run_id must match the output run_id")

    output_root.mkdir(parents=True, exist_ok=True)
    final_dir = output_root / run_id
    if final_dir.exists():
        raise FileExistsError(f"Eval bundle already exists: {run_id}")

    lock_path = output_root / f".{run_id}.lock"
    lock_descriptor: int | None = None
    temporary_dir: Path | None = None
    try:
        lock_descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(lock_descriptor)
        lock_descriptor = None

        if final_dir.exists():
            raise FileExistsError(f"Eval bundle already exists: {run_id}")

        temporary_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{run_id}.tmp-",
                dir=output_root,
            )
        )
        os.chmod(temporary_dir, 0o700)

        sanitized_manifest = sanitize_for_evidence(
            {**dict(manifest), "run_id": run_id},
            secret_values=secret_values,
        )
        sanitized_cases = [
            sanitize_for_evidence(item, secret_values=secret_values)
            for item in case_records
        ]
        sanitized_summary = sanitize_for_evidence(
            summary,
            secret_values=secret_values,
        )
        sanitized_manifest["artifacts"] = {
            "cases": "cases.jsonl",
            "summary": "summary.json",
            "trajectories": "trajectories/",
            "integrity": "integrity.json",
        }

        manifest_path = temporary_dir / "manifest.json"
        cases_path = temporary_dir / "cases.jsonl"
        summary_path = temporary_dir / "summary.json"
        trajectories_dir = temporary_dir / "trajectories"
        trajectories_dir.mkdir(mode=0o700)
        os.chmod(trajectories_dir, 0o700)
        _write_private_text(
            manifest_path,
            json.dumps(
                sanitized_manifest,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        _write_private_text(
            cases_path,
            "".join(
                _canonical_json(case_record) + "\n"
                for case_record in sanitized_cases
            ),
        )
        _write_private_text(
            summary_path,
            json.dumps(
                sanitized_summary,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )

        trajectory_paths: list[Path] = []
        seen_trajectories: set[tuple[str, int]] = set()
        for case_record in sanitized_cases:
            case_id = case_record.get("case_id")
            trial = case_record.get("trial")
            if (
                not isinstance(case_id, str)
                or not _CASE_ID_PATTERN.fullmatch(case_id)
                or not isinstance(trial, int)
                or isinstance(trial, bool)
                or trial < 1
            ):
                raise ValueError(
                    "Each case record requires a safe case_id and positive trial"
                )
            trajectory_key = (case_id, trial)
            if trajectory_key in seen_trajectories:
                raise ValueError(
                    f"Duplicate case trajectory: {case_id} trial {trial}"
                )
            seen_trajectories.add(trajectory_key)

            case_dir = trajectories_dir / case_id
            case_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(case_dir, 0o700)
            trajectory_path = case_dir / f"{trial}.json"
            _write_private_text(
                trajectory_path,
                json.dumps(
                    case_record,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )
            trajectory_paths.append(trajectory_path)

        indexed_paths = [
            manifest_path,
            cases_path,
            summary_path,
            *trajectory_paths,
        ]
        integrity = {
            "schema_version": "1.0",
            "algorithm": "sha256",
            "files": {
                path.relative_to(temporary_dir).as_posix(): {
                    "sha256": _file_sha256(path),
                    "bytes": path.stat().st_size,
                }
                for path in indexed_paths
            },
        }
        _write_private_text(
            temporary_dir / "integrity.json",
            json.dumps(
                integrity,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )

        os.rename(temporary_dir, final_dir)
        temporary_dir = None
        return final_dir
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if temporary_dir is not None and temporary_dir.exists():
            shutil.rmtree(temporary_dir)
        lock_path.unlink(missing_ok=True)


def verify_eval_bundle(bundle_path: Path) -> dict[str, Any]:
    required_root_items = {
        "manifest.json",
        "cases.jsonl",
        "summary.json",
        "integrity.json",
        "trajectories",
    }
    if not bundle_path.is_dir():
        raise ArtifactIntegrityError("Eval bundle directory is missing")
    actual_root_items = {
        path.name
        for path in bundle_path.iterdir()
    }
    if actual_root_items != required_root_items:
        raise ArtifactIntegrityError("Eval bundle file set is invalid")

    try:
        integrity = json.loads(
            (bundle_path / "integrity.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError("Integrity index is unreadable") from exc

    indexed_files = integrity.get("files", {})
    actual_indexed_files = {
        path.relative_to(bundle_path).as_posix()
        for path in bundle_path.rglob("*")
        if path.is_file() and path.name != "integrity.json"
    }
    if (
        integrity.get("algorithm") != "sha256"
        or not isinstance(indexed_files, dict)
        or set(indexed_files) != actual_indexed_files
        or not {
            "manifest.json",
            "cases.jsonl",
            "summary.json",
        }.issubset(indexed_files)
    ):
        raise ArtifactIntegrityError("Integrity index schema is invalid")

    for filename, expected in indexed_files.items():
        path = bundle_path / filename
        if (
            _file_sha256(path) != expected.get("sha256")
            or path.stat().st_size != expected.get("bytes")
        ):
            raise ArtifactIntegrityError(
                f"Artifact checksum mismatch: {filename}"
            )

    try:
        manifest = json.loads(
            (bundle_path / "manifest.json").read_text(encoding="utf-8")
        )
        summary = json.loads(
            (bundle_path / "summary.json").read_text(encoding="utf-8")
        )
        cases = [
            json.loads(line)
            for line in (bundle_path / "cases.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        trajectories = [
            json.loads((bundle_path / filename).read_text(encoding="utf-8"))
            for filename in sorted(indexed_files)
            if filename.startswith("trajectories/")
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError("Artifact payload is unreadable") from exc

    if manifest.get("run_id") != bundle_path.name:
        raise ArtifactIntegrityError("Manifest run_id does not match directory")

    return {
        "manifest": manifest,
        "cases": cases,
        "summary": summary,
        "trajectories": trajectories,
        "integrity": integrity,
    }


def verify_private_eval_bundle_permissions(bundle_path: Path) -> None:
    """Require an owner-only, symlink-free formal evidence bundle."""

    def validate_path(path: Path) -> None:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ArtifactIntegrityError(
                "Private Eval bundles cannot contain symlinks"
            )
        if stat.S_ISDIR(mode):
            if stat.S_IMODE(mode) != 0o700:
                raise ArtifactIntegrityError(
                    "Private Eval bundle directories must use mode 0700"
                )
        elif stat.S_ISREG(mode):
            if stat.S_IMODE(mode) != 0o600:
                raise ArtifactIntegrityError(
                    "Private Eval bundle files must use mode 0600"
                )
        else:
            raise ArtifactIntegrityError(
                "Private Eval bundles may contain only regular files "
                "and directories"
            )

    try:
        for ancestor in bundle_path.parents:
            if stat.S_ISLNK(ancestor.lstat().st_mode):
                raise ArtifactIntegrityError(
                    "Private Eval bundle paths cannot traverse symlinks"
                )
        validate_path(bundle_path)
        for path in bundle_path.rglob("*"):
            validate_path(path)
    except ArtifactIntegrityError:
        raise
    except OSError as exc:
        raise ArtifactIntegrityError(
            "Private Eval bundle permissions are unreadable"
        ) from exc
