from __future__ import annotations

from pathlib import Path

from scripts import export_contracts


def test_committed_contracts_match_current_application_schema():
    assert export_contracts.find_stale_contracts() == []
    assert export_contracts.main(["--check"]) == 0


def test_contract_freshness_check_detects_missing_or_changed_file(tmp_path):
    rendered = {
        "tool_contracts.schema.json": "{}\n",
        "openapi.json": '{"openapi":"3.1.0"}\n',
    }
    (tmp_path / "tool_contracts.schema.json").write_text(
        "stale",
        encoding="utf-8",
    )

    stale = export_contracts.find_stale_contracts(
        output_dir=Path(tmp_path),
        rendered=rendered,
    )

    assert stale == [
        "openapi.json",
        "tool_contracts.schema.json",
    ]
