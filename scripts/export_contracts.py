from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Committed HTTP contracts must never depend on a developer's local debug mode.
os.environ["ENABLE_DEBUG_ROUTES"] = "false"

from app.main import create_app
from app.tools.contracts import (
    get_preparation_tool_contracts,
    get_read_only_tool_contracts,
    get_tool_contracts,
)


def render_contracts() -> dict[str, str]:
    app = create_app(seed_demo=False)
    return {
        "tool_contracts.schema.json": json.dumps(
            get_tool_contracts(),
            ensure_ascii=False,
            indent=2,
        ),
        "readonly_tool_contracts.schema.json": json.dumps(
            get_read_only_tool_contracts(),
            ensure_ascii=False,
            indent=2,
        ),
        "preparation_tool_contracts.schema.json": json.dumps(
            get_preparation_tool_contracts(),
            ensure_ascii=False,
            indent=2,
        ),
        "openapi.json": json.dumps(
            app.openapi(),
            ensure_ascii=False,
            indent=2,
        ),
    }


def find_stale_contracts(
    *,
    output_dir: Path | None = None,
    rendered: Mapping[str, str] | None = None,
) -> list[str]:
    target_dir = output_dir or ROOT / "docs"
    expected = dict(rendered or render_contracts())
    return sorted(
        name
        for name, content in expected.items()
        if not (target_dir / name).is_file()
        or (target_dir / name).read_text(encoding="utf-8") != content
    )


def write_contracts(
    *,
    output_dir: Path | None = None,
    rendered: Mapping[str, str] | None = None,
) -> None:
    target_dir = output_dir or ROOT / "docs"
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (rendered or render_contracts()).items():
        (target_dir / name).write_text(content, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export or verify committed API and Agent contracts."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail without writing when committed contracts are stale.",
    )
    args = parser.parse_args(argv)
    if args.check:
        stale = find_stale_contracts()
        if stale:
            print("STALE CONTRACTS: " + ", ".join(stale))
            return 1
        print("Contracts are fresh.")
        return 0
    write_contracts()
    print("Contracts exported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
