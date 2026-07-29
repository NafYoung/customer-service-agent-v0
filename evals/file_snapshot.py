from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FileSnapshotError(RuntimeError):
    """A trusted input could not be frozen from one regular-file read."""


@dataclass(frozen=True)
class FileSnapshot:
    raw: bytes
    sha256: str

    def text(self) -> str:
        try:
            return self.raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileSnapshotError(
                "The frozen file is not valid UTF-8."
            ) from exc


def require_private_regular_file(path: Path, *, label: str) -> None:
    """Require an owner-only regular file in an owner-only directory."""

    try:
        file_status = path.lstat()
        parent_status = path.parent.lstat()
    except OSError as exc:
        raise FileSnapshotError(
            f"The private {label} metadata could not be inspected."
        ) from exc
    if (
        not stat.S_ISREG(file_status.st_mode)
        or stat.S_ISLNK(file_status.st_mode)
        or stat.S_IMODE(file_status.st_mode) & 0o077
    ):
        raise FileSnapshotError(
            f"The private {label} must be an owner-only regular file."
        )
    if (
        not stat.S_ISDIR(parent_status.st_mode)
        or stat.S_ISLNK(parent_status.st_mode)
        or stat.S_IMODE(parent_status.st_mode) & 0o077
    ):
        raise FileSnapshotError(
            f"The private {label} parent must be owner-only."
        )


def read_file_snapshot(
    path: Path,
    *,
    max_bytes: int = 16 * 1024 * 1024,
) -> FileSnapshot:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise FileSnapshotError(
                    "The frozen input is not a regular file."
                )
            if before.st_size > max_bytes:
                raise FileSnapshotError(
                    "The frozen input exceeds its size limit."
                )
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(descriptor, 64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise FileSnapshotError(
                        "The frozen input exceeds its size limit."
                    )
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise FileSnapshotError(
            "The frozen input could not be read safely."
        ) from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise FileSnapshotError(
            "The frozen input changed while it was being read."
        )
    raw = b"".join(chunks)
    return FileSnapshot(
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def read_json_object_snapshot(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], str]:
    snapshot = read_file_snapshot(path)
    try:
        payload = json.loads(snapshot.text())
    except json.JSONDecodeError as exc:
        raise FileSnapshotError(
            f"The frozen {label} is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise FileSnapshotError(
            f"The frozen {label} must be a JSON object."
        )
    return payload, snapshot.sha256
