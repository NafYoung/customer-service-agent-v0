from __future__ import annotations

import copy
from pathlib import Path

from app.tools.contracts import (
    PREPARATION_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    get_tool_contracts,
)
from scripts import export_contracts
from scripts.check_status_facts import (
    FACTS_PATH,
    STATUS_DOC,
    find_status_fact_drift,
    load_status_facts,
)


def test_committed_status_facts_match_docs_and_code():
    assert find_status_fact_drift() == []
    assert export_contracts.main(["--check"]) == 0


def test_agents_and_status_doc_agree_on_holdout_retirement():
    facts = load_status_facts()
    assert facts["holdout"]["v1"]["lifecycle"] == "retired"
    assert facts["holdout"]["v2"]["lifecycle"] == "retired"
    agents = Path("AGENTS.md").read_text(encoding="utf-8")
    status = Path(STATUS_DOC).read_text(encoding="utf-8")
    for document in (agents, status):
        lowered = document.lower()
        assert "holdout v1" in lowered
        assert "holdout v2" in lowered
        assert "退役" in document or "retired" in document


def test_facts_tool_counts_match_contracts():
    facts = load_status_facts()
    assert facts["tools"]["read_only"] == len(READ_ONLY_TOOL_NAMES)
    assert facts["tools"]["preparation"] == len(PREPARATION_TOOL_NAMES)
    assert facts["tools"]["exported_contracts"] == len(get_tool_contracts())


def test_wrong_demo_agent_mode_names_docs_09_and_facts():
    facts = copy.deepcopy(load_status_facts())
    facts["demo"]["agent_mode"] = "offline_replay"

    violations = find_status_fact_drift(facts=facts)
    joined = "\n".join(violations)

    assert violations
    assert STATUS_DOC in joined
    assert FACTS_PATH in joined
    assert "demo_agent_mode" in joined


def test_missing_demo_url_in_publish_checklist_names_facts():
    publish = Path("docs/12_phase6_publish_checklist.md").read_text(encoding="utf-8")
    documents = {
        "docs/12_phase6_publish_checklist.md": publish.replace(
            "https://rivet-public-demo.onrender.com",
            "https://example.invalid",
        )
    }

    violations = find_status_fact_drift(documents=documents)
    joined = "\n".join(violations)

    assert "docs/12_phase6_publish_checklist.md" in joined
    assert FACTS_PATH in joined
