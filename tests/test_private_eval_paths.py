from __future__ import annotations

from pathlib import Path

import pytest

from evals.private_paths import (
    PrivatePathError,
    prepare_fixed_private_output_root,
    require_private_case_directory,
    require_private_input_file,
)


def test_formal_output_is_fixed_inside_owner_only_private_root(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "artifacts" / "private"
    allowed_root = private_root / "eval-runs"

    prepared = prepare_fixed_private_output_root(
        allowed_root,
        allowed_root=allowed_root,
        private_root=private_root,
    )

    assert prepared == allowed_root
    assert prepared.stat().st_mode & 0o077 == 0
    assert private_root.stat().st_mode & 0o077 == 0
    with pytest.raises(PrivatePathError, match="fixed|private"):
        prepare_fixed_private_output_root(
            tmp_path / "docs" / "formal-private-leak",
            allowed_root=allowed_root,
            private_root=private_root,
        )


def test_formal_private_paths_reject_symlinks_and_loose_permissions(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    case_dir = private_root / "cases"
    case_dir.mkdir(mode=0o700)
    case_path = case_dir / "case.json"
    case_path.write_text("{}", encoding="utf-8")
    case_path.chmod(0o600)
    seal_path = private_root / "seal.json"
    seal_path.write_text("{}", encoding="utf-8")
    seal_path.chmod(0o600)

    assert require_private_case_directory(
        case_dir,
        private_root=private_root,
    ) == case_dir
    assert require_private_input_file(
        seal_path,
        private_root=private_root,
        label="seal",
    ) == seal_path

    case_path.chmod(0o644)
    with pytest.raises(PrivatePathError, match="owner-only"):
        require_private_case_directory(
            case_dir,
            private_root=private_root,
        )
    case_path.chmod(0o600)
    alias = private_root / "case-alias.json"
    alias.symlink_to(case_path)
    with pytest.raises(PrivatePathError, match="symlink|regular"):
        require_private_input_file(
            alias,
            private_root=private_root,
            label="case alias",
        )
