from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.tools.contracts import (
    PREPARATION_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    get_tool_contracts,
)

ROOT = Path(__file__).resolve().parents[1]
FACTS_PATH = "docs/status.facts.json"
AGENTS_PATH = "AGENTS.md"
STATUS_DOC = "docs/09_project_status.md"
PUBLISH_DOC = "docs/12_phase6_publish_checklist.md"
TESTING_INDEX = "docs/testing/README.md"
CONTRACTS_PATH = "app/tools/contracts.py"
_RETIRED_MARKERS = ("退役", "retired")


def load_status_facts(path: Path | None = None) -> dict[str, Any]:
    target = path or (ROOT / FACTS_PATH)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{FACTS_PATH}: expected a JSON object")
    return payload


def find_status_fact_drift(
    *,
    root: Path | None = None,
    facts: Mapping[str, Any] | None = None,
    documents: Mapping[str, str] | None = None,
) -> list[str]:
    base = root or ROOT
    loaded = dict(facts) if facts is not None else load_status_facts(base / FACTS_PATH)
    texts = _document_texts(base, documents)
    return [
        *_schema_violations(loaded),
        *_code_violations(loaded, base),
        *_document_violations(loaded, texts),
    ]


def _document_texts(
    root: Path,
    documents: Mapping[str, str] | None,
) -> dict[str, str]:
    paths = (AGENTS_PATH, STATUS_DOC, PUBLISH_DOC, TESTING_INDEX)
    texts: dict[str, str] = {}
    for path in paths:
        if documents is not None and path in documents:
            texts[path] = documents[path]
            continue
        target = root / path
        texts[path] = (
            target.read_text(encoding="utf-8") if target.is_file() else ""
        )
    return texts


def _schema_violations(facts: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    holdout = facts.get("holdout")
    if not isinstance(holdout, Mapping):
        return [f"{FACTS_PATH}: holdout is missing"]
    for version in ("v1", "v2"):
        record = holdout.get(version)
        if not isinstance(record, Mapping) or "lifecycle" not in record:
            violations.append(f"{FACTS_PATH}: holdout.{version}.lifecycle is missing")
    demo = facts.get("demo")
    if (
        not isinstance(demo, Mapping)
        or "url" not in demo
        or "agent_mode" not in demo
    ):
        violations.append(f"{FACTS_PATH}: demo.url or demo.agent_mode is missing")
    tools = facts.get("tools")
    if not isinstance(tools, Mapping):
        violations.append(f"{FACTS_PATH}: tools is missing")
    else:
        for key in ("read_only", "preparation", "exported_contracts"):
            if key not in tools:
                violations.append(f"{FACTS_PATH}: tools.{key} is missing")
    price = facts.get("price")
    if (
        not isinstance(price, Mapping)
        or "snapshot" not in price
        or "valid_until" not in price
    ):
        violations.append(
            f"{FACTS_PATH}: price.snapshot or price.valid_until is missing"
        )
    historical = facts.get("historical_docs")
    if (
        not isinstance(historical, Mapping)
        or historical.get("docs/testing") != "historical"
    ):
        violations.append(
            f"{FACTS_PATH}: historical_docs.docs/testing must be historical"
        )
    return violations


def _code_violations(facts: Mapping[str, Any], root: Path) -> list[str]:
    tools = facts.get("tools")
    if not isinstance(tools, Mapping):
        return []
    expected = {
        "read_only": len(READ_ONLY_TOOL_NAMES),
        "preparation": len(PREPARATION_TOOL_NAMES),
        "exported_contracts": len(get_tool_contracts()),
    }
    violations = [
        f"{FACTS_PATH}: tools.{key} does not match {CONTRACTS_PATH}"
        for key, count in expected.items()
        if tools.get(key) != count
    ]
    price = facts.get("price")
    if not isinstance(price, Mapping):
        return violations
    snapshot_name = str(price.get("snapshot", ""))
    snapshot = root / snapshot_name
    if not snapshot.is_file():
        violations.append(f"{FACTS_PATH}: price.snapshot is missing")
        return violations
    recorded = json.loads(snapshot.read_text(encoding="utf-8"))
    if recorded.get("valid_until") != price.get("valid_until"):
        violations.append(
            f"{FACTS_PATH}: price.valid_until does not match {snapshot_name}"
        )
    return violations


def _document_violations(
    facts: Mapping[str, Any],
    texts: Mapping[str, str],
) -> list[str]:
    violations: list[str] = []
    holdout = facts.get("holdout")
    if isinstance(holdout, Mapping):
        for version, record in holdout.items():
            if not isinstance(record, Mapping):
                continue
            label = f"holdout {version}"
            lifecycle = str(record.get("lifecycle", ""))
            for doc in (AGENTS_PATH, STATUS_DOC):
                text = texts.get(doc, "")
                if label.lower() not in text.lower():
                    violations.append(f"{doc}: {label} does not match {FACTS_PATH}")
                    continue
                if lifecycle == "retired" and not any(
                    marker in text for marker in _RETIRED_MARKERS
                ):
                    violations.append(
                        f"{doc}: {label} lifecycle does not match {FACTS_PATH}"
                    )

    demo = facts.get("demo")
    if isinstance(demo, Mapping):
        mode = str(demo.get("agent_mode", ""))
        url = str(demo.get("url", "")).rstrip("/")
        status_text = texts.get(STATUS_DOC, "")
        if mode and mode not in status_text:
            violations.append(
                f"{STATUS_DOC}: demo_agent_mode does not match {FACTS_PATH}"
            )
        if url and url not in status_text:
            violations.append(f"{STATUS_DOC}: demo url does not match {FACTS_PATH}")
        publish_text = texts.get(PUBLISH_DOC, "")
        if url and url not in publish_text:
            violations.append(f"{PUBLISH_DOC}: demo url does not match {FACTS_PATH}")

    price = facts.get("price")
    if isinstance(price, Mapping):
        valid_date = str(price.get("valid_until", ""))[:10]
        if valid_date and valid_date not in texts.get(STATUS_DOC, ""):
            violations.append(
                f"{STATUS_DOC}: price valid_until does not match {FACTS_PATH}"
            )

    historical = facts.get("historical_docs")
    testing_is_historical = (
        isinstance(historical, Mapping)
        and historical.get("docs/testing") == "historical"
    )
    if testing_is_historical:
        testing_text = texts.get(TESTING_INDEX, "")
        if "historical" not in testing_text.lower() and "史稿" not in testing_text:
            violations.append(
                f"{TESTING_INDEX}: docs/testing is not marked historical. 改 {FACTS_PATH}"
            )
    return violations
