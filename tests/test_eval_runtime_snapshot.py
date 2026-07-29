from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.config import Settings
from evals import readonly_reporting, semantic_calibration
from evals.file_snapshot import FileSnapshotError, read_file_snapshot

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "evals" / "semantic_judge_calibration_cases.jsonl"


def test_file_snapshot_rejects_symlink_and_oversized_input(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("trusted", encoding="utf-8")
    symlink = tmp_path / "symlink.txt"
    symlink.symlink_to(target)

    with pytest.raises(FileSnapshotError):
        read_file_snapshot(symlink)
    with pytest.raises(FileSnapshotError, match="size"):
        read_file_snapshot(target, max_bytes=3)


def test_calibration_fixture_parse_and_hash_share_one_snapshot() -> None:
    load_snapshot = getattr(
        semantic_calibration,
        "load_calibration_fixtures_snapshot",
    )

    fixtures, snapshot = load_snapshot(FIXTURE_PATH)

    assert len(fixtures) == 49
    assert snapshot.raw == FIXTURE_PATH.read_bytes()
    assert snapshot.sha256 == hashlib.sha256(snapshot.raw).hexdigest()


def test_frozen_harness_binds_exact_runtime_inputs() -> None:
    freeze_harness = getattr(
        readonly_reporting,
        "freeze_readonly_harness",
    )

    frozen = freeze_harness(Settings())

    assert frozen.agent_system_prompt
    assert frozen.semantic_judge_system_prompt
    assert "index.json" in frozen.policy_documents
    assert frozen.tool_contracts
    assert frozen.fingerprints["prompt_sha256"] == hashlib.sha256(
        frozen.agent_system_prompt.encode("utf-8")
    ).hexdigest()
    assert frozen.fingerprints[
        "semantic_judge_prompt_sha256"
    ] == hashlib.sha256(
        frozen.semantic_judge_system_prompt.encode("utf-8")
    ).hexdigest()
    assert frozen.fingerprints[
        "semantic_calibration_corpus_sha256"
    ] == frozen.calibration_fixture_snapshot.sha256
    assert frozen.fingerprints["evidence_protocol_sha256"]
