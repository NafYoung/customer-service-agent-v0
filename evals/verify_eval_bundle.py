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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify checksums and schema for a read-only Eval bundle."
    )
    parser.add_argument("bundle_path", type=Path)
    args = parser.parse_args(argv)

    try:
        bundle = validate_readonly_bundle(args.bundle_path)
    except (ArtifactIntegrityError, ValidationError, OSError) as exc:
        print(f"INVALID: {type(exc).__name__}")
        return 1

    print(
        "VALID: "
        f"{bundle.manifest.run_id} "
        f"({bundle.summary.strict.passed}/"
        f"{bundle.summary.total_trials} strict trials)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
