from __future__ import annotations

import argparse
import ast
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST_ADAPTER = "app/host/confirmation.py"
PRICE_MODULE = "app/agent/budget/price.py"
AGENT_PREFIX = "app/agent/"
DEMO_PREFIX = "app/demo/"
APP_PREFIX = "app/"

AGENT_ORDER_WRITE_NAMES = frozenset(
    {
        "ActionService",
        "mark_failed",
        "mark_expired",
        "execute_confirmed_action",
        "record_confirmation",
        "confirm_and_execute",
    }
)
ALLOWED_EVALS_IMPORTS = frozenset(
    {
        (
            "app/demo/live_runner.py",
            "evals.canonical_pricing",
            "load_canonical_price_snapshot",
        )
    }
)


def find_import_lint_violations(*, root: Path | None = None) -> list[str]:
    base = root or ROOT
    violations: list[str] = []
    app_root = base / "app"
    if not app_root.is_dir():
        return violations
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(base).as_posix()
        violations.extend(
            source_violations(path.read_text(encoding="utf-8"), path=relative)
        )
    return violations


def source_violations(source: str, *, path: str) -> list[str]:
    tree = ast.parse(source)
    violations: list[str] = []
    if _is_agent_path(path):
        violations.extend(_agent_write_violations(tree, path))
    if path.startswith(APP_PREFIX):
        violations.extend(_evals_import_violations(tree, path))
    if path.startswith(DEMO_PREFIX):
        violations.extend(_demo_private_violations(tree, path))
    return violations


def _is_agent_path(path: str) -> bool:
    return path.startswith(AGENT_PREFIX) or path == "app/agent.py"


def _agent_write_violations(tree: ast.AST, path: str) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if not isinstance(lineno, int):
            continue
        for name in _order_write_names(node):
            violations.append(_agent_message(path, lineno, name))
    return violations


def _order_write_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name) and node.id in AGENT_ORDER_WRITE_NAMES:
        return [node.id]
    if isinstance(node, ast.Attribute) and node.attr in AGENT_ORDER_WRITE_NAMES:
        return [node.attr]
    if isinstance(node, ast.ImportFrom) and _is_write_module(node.module):
        imported: list[str] = []
        for alias in node.names:
            if alias.name == "*":
                imported.append("*")
            elif alias.name in AGENT_ORDER_WRITE_NAMES:
                imported.append(alias.name)
        return imported
    if isinstance(node, ast.Import):
        modules: list[str] = []
        for alias in node.names:
            if _is_write_module(alias.name):
                modules.append(alias.name)
        return modules
    return []


def _is_write_module(module: str | None) -> bool:
    if module is None:
        return False
    return module in {"app.services.actions", "app.host.confirmation"} or module.startswith(
        ("app.services.actions.", "app.host.confirmation.")
    )


def _agent_message(path: str, line: int, name: str) -> str:
    return (
        f"{path}:{line}: app/agent must not write orders ({name}). 改 {HOST_ADAPTER}"
    )


def _evals_import_violations(tree: ast.AST, path: str) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_evals_module(alias.name):
                    continue
                if (path, alias.name, "*") in ALLOWED_EVALS_IMPORTS:
                    continue
                violations.append(_evals_message(path, node.lineno, alias.name, "*"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not _is_evals_module(module):
                continue
            for alias in node.names:
                if (path, module, alias.name) in ALLOWED_EVALS_IMPORTS:
                    continue
                violations.append(_evals_message(path, node.lineno, module, alias.name))
    return violations


def _is_evals_module(module: str) -> bool:
    return module == "evals" or module.startswith("evals.")


def _evals_message(path: str, line: int, module: str, name: str) -> str:
    imported = module if name == "*" else f"{module}.{name}"
    return f"{path}:{line}: app/ must not import evals ({imported}). 改 {PRICE_MODULE}"


def _demo_private_violations(tree: ast.AST, path: str) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not _is_private_attr(node.attr):
            continue
        if not _refers_to_action_service(node.value):
            continue
        violations.append(
            f"{path}:{node.lineno}: demo must not call ActionService private "
            f"methods ({node.attr}). 改 {HOST_ADAPTER}"
        )
    return violations


def _is_private_attr(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _refers_to_action_service(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"ActionService", "action_service"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"ActionService", "action_service"}
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check app import boundaries.")
    parser.parse_args(argv)
    problems = find_import_lint_violations()
    if problems:
        print("\n".join(problems))
        return 1
    print("Import lint is clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
