from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Committed HTTP contracts must never depend on a developer's local debug mode.
os.environ["ENABLE_DEBUG_ROUTES"] = "false"

from app.main import create_app
from app.tools.contracts import (
    HOST_TOOL_NAMES,
    PREPARATION_TOOL_NAMES,
    PREPARE_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    get_preparation_tool_contracts,
    get_read_only_tool_contracts,
    get_tool_contracts,
)
from scripts.check_status_facts import find_status_fact_drift

CONTRACTS_PATH = "app/tools/contracts.py"
TOOL_NAME_SECTION_BEGIN = (
    "<!-- BEGIN generated tool names: scripts/export_contracts.py -->"
)
TOOL_NAME_SECTION_END = "<!-- END generated tool names -->"
_LIST_ITEM = re.compile(r"^\s*(?:\d+\.|[-*])\s+`([A-Za-z0-9_]+)`\s*$")
_QUOTED_COLLECTION = re.compile(r"[\(\[\{]([^\[\]\{\}\(\)]*)[\)\]\}]", re.S)
_QUOTED_NAME = re.compile(r"""["']([A-Za-z0-9_]+)["']""")
_FENCE_LINE = re.compile(r"^\s*```")
_GENERATED_BEGIN_MARK = "<!-- BEGIN generated tool names:"


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


def exported_tool_names() -> frozenset[str]:
    return frozenset(
        str(contract["name"]) for contract in get_tool_contracts()
    )


def render_tool_name_section() -> str:
    names = [str(contract["name"]) for contract in get_tool_contracts()]
    lines = [f"`get_tool_contracts()` 导出 {len(names)} 个工具名：", ""]
    lines.extend(
        f"{index}. `{name}`" for index, name in enumerate(names, start=1)
    )
    if HOST_TOOL_NAMES:
        host = "、".join(f"`{name}`" for name in HOST_TOOL_NAMES)
        lines.extend(["", f"宿主专用工具名不进入 Agent 白名单：{host}。"])
    return "\n".join(lines) + "\n"


def marked_tool_name_section() -> str:
    body = render_tool_name_section().rstrip("\n")
    return f"{TOOL_NAME_SECTION_BEGIN}\n{body}\n{TOOL_NAME_SECTION_END}"


def marked_tool_name_fence(label: str, names: Sequence[str]) -> str:
    begin = f"{_GENERATED_BEGIN_MARK} {label} -->"
    body = "```text\n" + "\n".join(names) + "\n```"
    return f"{begin}\n{body}\n{TOOL_NAME_SECTION_END}"


def generated_tool_name_fences() -> tuple[tuple[Path, str], ...]:
    return (
        (
            ROOT / "docs" / "05_deepseek_readonly_agent_v1.md",
            marked_tool_name_fence("READ_ONLY_TOOL_NAMES", READ_ONLY_TOOL_NAMES),
        ),
        (
            ROOT / "docs" / "07_preparation_agent_v1.md",
            marked_tool_name_fence(
                "PREPARATION_TOOL_NAMES",
                PREPARATION_TOOL_NAMES,
            ),
        ),
        (
            ROOT / "docs" / "04_agent_integration_plan.md",
            marked_tool_name_fence("PREPARE_TOOL_NAMES", PREPARE_TOOL_NAMES),
        ),
    )


def find_tool_name_section_drift(path: Path | None = None) -> list[str]:
    target = path or (ROOT / "docs" / "02_tool_contracts_v0.md")
    text = target.read_text(encoding="utf-8")
    block = marked_tool_name_section()
    display = _display_path(target)
    if (
        text.count(block) == 1
        and text.count(TOOL_NAME_SECTION_BEGIN) == 1
        and text.count(TOOL_NAME_SECTION_END) == 1
    ):
        return []
    if (
        TOOL_NAME_SECTION_BEGIN not in text
        or TOOL_NAME_SECTION_END not in text
    ):
        return [
            f"{display}: missing generated tool-name section. "
            f"改 {CONTRACTS_PATH}"
        ]
    return [
        f"{display}: generated tool-name section does not match "
        f"get_tool_contracts(). 改 {CONTRACTS_PATH}"
    ]


def find_generated_tool_name_fence_drift() -> list[str]:
    problems: list[str] = []
    for path, block in generated_tool_name_fences():
        text = path.read_text(encoding="utf-8")
        if text.count(block) == 1:
            continue
        problems.append(
            f"{_display_path(path)}: generated tool-name fence does not "
            f"match {CONTRACTS_PATH}. 改 {CONTRACTS_PATH}"
        )
    return problems


def write_generated_tool_name_fences() -> None:
    for path, block in generated_tool_name_fences():
        text = path.read_text(encoding="utf-8")
        begin = block.splitlines()[0]
        start = text.find(begin)
        end = -1 if start < 0 else text.find(TOOL_NAME_SECTION_END, start)
        if start < 0 or end < 0:
            raise ValueError(
                f"{_display_path(path)}: missing generated tool-name fence. "
                f"改 {CONTRACTS_PATH}"
            )
        end += len(TOOL_NAME_SECTION_END)
        path.write_text(text[:start] + block + text[end:], encoding="utf-8")


def write_tool_name_section(path: Path | None = None) -> None:
    target = path or (ROOT / "docs" / "02_tool_contracts_v0.md")
    text = target.read_text(encoding="utf-8")
    block = marked_tool_name_section()
    pattern = re.compile(
        re.escape(TOOL_NAME_SECTION_BEGIN)
        + r".*?"
        + re.escape(TOOL_NAME_SECTION_END),
        re.S,
    )
    if pattern.search(text) is None:
        raise ValueError(
            f"{_display_path(target)}: missing generated tool-name section. "
            f"改 {CONTRACTS_PATH}"
        )
    updated = pattern.sub(lambda _match: block, text, count=1)
    target.write_text(updated, encoding="utf-8")


def handwritten_roster_violations(source: str, *, path: str) -> list[str]:
    if path.endswith(".py"):
        return _python_roster_violations(source, path)
    return _markdown_roster_violations(source, path)


def find_handwritten_tool_rosters(root: Path | None = None) -> list[str]:
    base = root or ROOT
    violations: list[str] = []
    tests = base / "tests"
    if tests.is_dir():
        for path in sorted(tests.rglob("*.py")):
            violations.extend(
                handwritten_roster_violations(
                    path.read_text(encoding="utf-8"),
                    path=_display_path(path),
                )
            )
    docs = base / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            violations.extend(
                handwritten_roster_violations(
                    path.read_text(encoding="utf-8"),
                    path=_display_path(path),
                )
            )
    return violations


def _python_roster_violations(source: str, path: str) -> list[str]:
    tree = ast.parse(source)
    names = exported_tool_names()
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            continue
        values = _string_collection(node)
        if values is None or not _is_tool_roster(values, names):
            continue
        violations.append(_violation(path, node.lineno))
    return violations


def _markdown_roster_violations(source: str, path: str) -> list[str]:
    names = exported_tool_names()
    spans = _generated_line_spans(source)
    violations = _markdown_list_violations(source, path, names, spans)
    violations.extend(_fenced_roster_violations(source, path, names, spans))
    for match in _QUOTED_COLLECTION.finditer(source):
        values = _pure_quoted_names(match.group(1))
        if values is None or not _is_tool_roster(values, names):
            continue
        line = source.count("\n", 0, match.start()) + 1
        if _line_in_spans(line, spans):
            continue
        violations.append(_violation(path, line))
    return violations


def _markdown_list_violations(
    source: str,
    path: str,
    names: frozenset[str],
    spans: list[tuple[int, int]],
) -> list[str]:
    violations: list[str] = []
    group: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal group
        if (
            len(group) >= 2
            and not _line_in_spans(group[0][0], spans)
            and _is_tool_roster([name for _, name in group], names)
        ):
            violations.append(_violation(path, group[0][0]))
        group = []

    for line_number, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        match = _LIST_ITEM.match(line)
        if match is None or _line_in_spans(line_number, spans):
            flush()
            continue
        name = match.group(1)
        if name not in names:
            flush()
            continue
        group.append((line_number, name))
    flush()
    return violations


def _string_collection(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None
    values: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            values.append(elt.value)
            continue
        return None
    return values


def _pure_quoted_names(inner: str) -> list[str] | None:
    found = _QUOTED_NAME.findall(inner)
    if len(found) < 2:
        return None
    remainder = _QUOTED_NAME.sub("", inner)
    if re.fullmatch(r"[\s,]*", remainder) is None:
        return None
    return found


def _is_tool_roster(values: list[str], names: frozenset[str]) -> bool:
    return len(values) >= 2 and all(value in names for value in values)


def _fenced_roster_violations(
    source: str,
    path: str,
    names: frozenset[str],
    spans: list[tuple[int, int]],
) -> list[str]:
    violations: list[str] = []
    in_fence = False
    fence_line = 0
    body: list[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        if _FENCE_LINE.match(line) is None:
            if in_fence and line.strip():
                body.append(line.strip())
            continue
        if not in_fence:
            in_fence = True
            fence_line = line_number
            body = []
            continue
        if _is_tool_roster(body, names) and not _line_in_spans(fence_line, spans):
            violations.append(_violation(path, fence_line))
        in_fence = False
        body = []
    return violations


def _generated_line_spans(source: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for line_number, line in enumerate(source.splitlines(), start=1):
        if _GENERATED_BEGIN_MARK in line:
            start = line_number
        elif TOOL_NAME_SECTION_END in line and start is not None:
            spans.append((start, line_number))
            start = None
    return spans


def _line_in_spans(line: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in spans)


def _violation(path: str, line: int) -> str:
    return f"{path}:{line}: handwritten tool-name list. 改 {CONTRACTS_PATH}"


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


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
        problems = [
            *(
                ["STALE CONTRACTS: " + ", ".join(stale)]
                if (stale := find_stale_contracts())
                else []
            ),
            *find_handwritten_tool_rosters(),
            *find_tool_name_section_drift(),
            *find_generated_tool_name_fence_drift(),
            *find_status_fact_drift(),
        ]
        if problems:
            print("\n".join(problems))
            return 1
        print("Contracts are fresh.")
        return 0
    write_contracts()
    write_tool_name_section()
    write_generated_tool_name_fences()
    print("Contracts exported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
