from __future__ import annotations

from pathlib import Path

from app.tools.contracts import (
    PREPARATION_TOOL_NAMES,
    PREPARE_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
)
from scripts import export_contracts


def test_committed_contracts_match_current_application_schema():
    assert export_contracts.find_stale_contracts() == []
    assert export_contracts.main(["--check"]) == 0


def test_handwritten_six_tuple_fails_and_names_contract_module():
    source = (
        "NAMES = (\n"
        '    "get_customer_orders",\n'
        '    "get_order",\n'
        '    "get_shipment",\n'
        '    "get_inventory",\n'
        '    "search_policy",\n'
        '    "check_action_eligibility",\n'
        ")\n"
    )

    violations = export_contracts.handwritten_roster_violations(
        source,
        path="tests/handwritten_six_tuple.py",
    )

    assert violations == [
        "tests/handwritten_six_tuple.py:1: handwritten tool-name list. "
        "改 app/tools/contracts.py"
    ]


def test_case_string_and_mixed_denylist_are_not_tool_rosters():
    source = (
        'tool_turn("prepare_cancel_order", "{}")\n'
        "forbidden = (\n"
        '    "prepare_cancel_order",\n'
        '    "execute_prepared_action",\n'
        ")\n"
    )

    assert (
        export_contracts.handwritten_roster_violations(
            source,
            path="tests/case_strings.py",
        )
        == []
    )


def test_blank_line_does_not_hide_a_tool_name_list():
    source = "1. `get_order`\n\n2. `get_shipment`\n"

    violations = export_contracts.handwritten_roster_violations(
        source,
        path="docs/handwritten.md",
    )

    assert violations == [
        "docs/handwritten.md:1: handwritten tool-name list. "
        "改 app/tools/contracts.py"
    ]


def _text_fence(names: tuple[str, ...]) -> str:
    return "```text\n" + "\n".join(names) + "\n```\n"


def test_fenced_tool_name_lists_fail_at_every_known_site():
    sites = (
        ("docs/05_deepseek_readonly_agent_v1.md", READ_ONLY_TOOL_NAMES),
        ("docs/07_preparation_agent_v1.md", PREPARATION_TOOL_NAMES),
        ("docs/04_agent_integration_plan.md", PREPARE_TOOL_NAMES),
        ("docs/new_fenced_roster.md", READ_ONLY_TOOL_NAMES),
    )

    for path, names in sites:
        violations = export_contracts.handwritten_roster_violations(
            _text_fence(names),
            path=path,
        )
        assert violations == [
            f"{path}:1: handwritten tool-name list. 改 app/tools/contracts.py"
        ]


def test_mixed_fenced_text_is_not_a_tool_roster():
    source = (
        "```text\n"
        "generic prepare_action\n"
        "create_handoff_ticket\n"
        "authenticate_customer\n"
        "```\n"
    )

    assert (
        export_contracts.handwritten_roster_violations(
            source,
            path="docs/07_preparation_agent_v1.md",
        )
        == []
    )


def test_generated_tool_name_fence_is_not_a_handwritten_roster():
    source = (
        export_contracts.marked_tool_name_fence(
            "READ_ONLY_TOOL_NAMES",
            READ_ONLY_TOOL_NAMES,
        )
        + "\n"
    )

    assert (
        export_contracts.handwritten_roster_violations(
            source,
            path="docs/05_deepseek_readonly_agent_v1.md",
        )
        == []
    )


def test_generated_tool_name_block_is_not_a_handwritten_roster():
    source = export_contracts.marked_tool_name_section() + "\n"

    assert (
        export_contracts.handwritten_roster_violations(
            source,
            path="docs/02_tool_contracts_v0.md",
        )
        == []
    )


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
