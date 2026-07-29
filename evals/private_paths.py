from __future__ import annotations

import os
import stat
from pathlib import Path

from evals.file_snapshot import (
    FileSnapshotError,
    require_private_regular_file,
)


class PrivatePathError(RuntimeError):
    """A private Eval artifact path violates its containment contract."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _reject_existing_symlink_components(path: Path) -> None:
    current = _absolute(path)
    for candidate in (current, *current.parents):
        try:
            if candidate.is_symlink():
                raise PrivatePathError(
                    "Private artifact paths cannot contain symlinks."
                )
        except OSError as exc:
            raise PrivatePathError(
                "Private artifact path metadata is unreadable."
            ) from exc


def _require_contained(path: Path, *, private_root: Path) -> Path:
    absolute = _absolute(path)
    root = _absolute(private_root)
    _reject_existing_symlink_components(absolute)
    _reject_existing_symlink_components(root)
    try:
        absolute.resolve(strict=False).relative_to(
            root.resolve(strict=False)
        )
    except ValueError as exc:
        raise PrivatePathError(
            "Formal artifacts must stay inside the fixed private root."
        ) from exc
    return absolute


def prepare_fixed_private_output_root(
    requested: Path,
    *,
    allowed_root: Path,
    private_root: Path,
) -> Path:
    """Create one fixed owner-only output root and reject aliases/escapes."""

    requested_absolute = _require_contained(
        requested,
        private_root=private_root,
    )
    allowed_absolute = _require_contained(
        allowed_root,
        private_root=private_root,
    )
    if (
        requested_absolute != allowed_absolute
        or requested_absolute.resolve(strict=False)
        != allowed_absolute.resolve(strict=False)
    ):
        raise PrivatePathError(
            "Formal output must use the fixed private output root."
        )
    private_absolute = _absolute(private_root)
    private_absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_absolute.chmod(0o700)
    allowed_absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = allowed_absolute
    while True:
        current.chmod(0o700)
        if current == private_absolute:
            break
        if private_absolute not in current.parents:
            raise PrivatePathError(
                "Formal output escaped the fixed private root."
            )
        current = current.parent
    return allowed_absolute


def require_private_input_file(
    path: Path,
    *,
    private_root: Path,
    label: str,
) -> Path:
    """Require a private input file and owner-only directory ancestry."""

    absolute = _require_contained(path, private_root=private_root)
    try:
        require_private_regular_file(absolute, label=label)
    except FileSnapshotError as exc:
        raise PrivatePathError(str(exc)) from exc
    if stat.S_IMODE(absolute.lstat().st_mode) != 0o600:
        raise PrivatePathError(
            f"The private {label} must use file mode 0600."
        )
    root = _absolute(private_root)
    current = absolute.parent
    while True:
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise PrivatePathError(
                f"The private {label} directory is unreadable."
            ) from exc
        if (
            not stat.S_ISDIR(mode)
            or stat.S_ISLNK(mode)
            or stat.S_IMODE(mode) & 0o077
        ):
            raise PrivatePathError(
                f"The private {label} directory must be owner-only."
            )
        if current == root:
            break
        if root not in current.parents:
            raise PrivatePathError(
                f"The private {label} escaped its private root."
            )
        current = current.parent
    return absolute


def require_private_case_directory(
    path: Path,
    *,
    private_root: Path,
) -> Path:
    """Require an owner-only case directory containing private JSON files."""

    absolute = _require_contained(path, private_root=private_root)
    root = _absolute(private_root)
    current = absolute
    while True:
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise PrivatePathError(
                "The private holdout case directory is unreadable."
            ) from exc
        if (
            not stat.S_ISDIR(mode)
            or stat.S_ISLNK(mode)
            or stat.S_IMODE(mode) & 0o077
        ):
            raise PrivatePathError(
                "Private holdout case directories must be owner-only."
            )
        if current == root:
            break
        if root not in current.parents:
            raise PrivatePathError(
                "The holdout case directory escaped its private root."
            )
        current = current.parent
    case_paths = sorted(absolute.glob("*.json"))
    if not case_paths:
        raise PrivatePathError(
            "The private holdout case directory contains no JSON cases."
        )
    for case_path in case_paths:
        require_private_input_file(
            case_path,
            private_root=root,
            label="holdout case",
        )
    return absolute
