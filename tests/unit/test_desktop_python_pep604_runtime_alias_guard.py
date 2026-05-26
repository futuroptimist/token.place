"""Guardrail: desktop-packaged Python import graph must avoid runtime PEP 604 aliases."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (
    REPO_ROOT / "desktop-tauri" / "src-tauri" / "python",
    REPO_ROOT / "utils",
    REPO_ROOT / "config.py",
)


def _is_runtime_alias_with_pep604(assign: ast.Assign) -> bool:
    return isinstance(assign.value, ast.BinOp) and isinstance(assign.value.op, ast.BitOr)


def test_no_runtime_type_alias_assignments_using_pep604_union() -> None:
    offenders: list[str] = []

    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            files.append(root)
        else:
            files.extend(sorted(root.rglob("*.py")))

    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not _is_runtime_alias_with_pep604(node):
                continue
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not target_names:
                continue
            rel = path.relative_to(REPO_ROOT)
            offenders.append(f"{rel}:{node.lineno}:{','.join(target_names)}")

    assert offenders == [], (
        "Runtime-evaluated PEP 604 aliases are Python 3.9-incompatible; "
        f"replace with typing.Union. Offenders: {offenders}"
    )
