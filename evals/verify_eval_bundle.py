from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError

from evals.evidence import ArtifactIntegrityError
from evals.evidence_schema import validate_readonly_bundle
from evals.formal_failure_evidence import (
    FormalFailureEvidenceError,
    validate_formal_failure_bundle,
)
from evals.holdout_lock import (
    HoldoutLockError,
    verify_failed_holdout_receipt_chain,
    verify_holdout_receipt_chain,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify checksums and schema for a read-only Eval bundle."
    )
    parser.add_argument("bundle_path", type=Path)
    parser.add_argument("--holdout-manifest", type=Path)
    parser.add_argument("--holdout-start", type=Path)
    parser.add_argument("--holdout-terminal", type=Path)
    parser.add_argument(
        "--failed-attempt",
        action="store_true",
        help="Validate an isolated formal failed-attempt bundle.",
    )
    args = parser.parse_args(argv)
    chain_paths = (
        args.holdout_manifest,
        args.holdout_start,
        args.holdout_terminal,
    )
    if any(path is not None for path in chain_paths) and not all(
        path is not None for path in chain_paths
    ):
        parser.error(
            "formal chain verification requires --holdout-manifest, "
            "--holdout-start, and --holdout-terminal"
        )

    try:
        failed_bundle = (
            validate_formal_failure_bundle(args.bundle_path)
            if args.failed_attempt
            else None
        )
        readonly_bundle = (
            None
            if args.failed_attempt
            else validate_readonly_bundle(args.bundle_path)
        )
        if all(path is not None for path in chain_paths):
            assert args.holdout_manifest is not None
            assert args.holdout_start is not None
            assert args.holdout_terminal is not None
            if args.failed_attempt:
                verify_failed_holdout_receipt_chain(
                    manifest_path=args.holdout_manifest,
                    start_path=args.holdout_start,
                    terminal_path=args.holdout_terminal,
                    bundle_path=args.bundle_path,
                )
            else:
                verify_holdout_receipt_chain(
                    manifest_path=args.holdout_manifest,
                    start_path=args.holdout_start,
                    terminal_path=args.holdout_terminal,
                    bundle_path=args.bundle_path,
                )
    except (
        ArtifactIntegrityError,
        FormalFailureEvidenceError,
        HoldoutLockError,
        ValidationError,
        OSError,
    ) as exc:
        print(f"INVALID: {type(exc).__name__}")
        return 1

    chain_suffix = (
        " with complete formal receipt chain"
        if all(path is not None for path in chain_paths)
        else ""
    )
    if args.failed_attempt:
        assert failed_bundle is not None
        print(
            "VALID FAILED ATTEMPT: "
            f"{failed_bundle.manifest.run_id} "
            f"({failed_bundle.manifest.completed_record_count} "
            "partial records)"
            f"{chain_suffix}"
        )
    else:
        assert readonly_bundle is not None
        print(
            "VALID: "
            f"{readonly_bundle.manifest.run_id} "
            f"({readonly_bundle.summary.strict.passed}/"
            f"{readonly_bundle.summary.total_trials} strict trials)"
            f"{chain_suffix}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
