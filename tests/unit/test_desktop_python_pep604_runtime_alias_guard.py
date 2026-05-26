"""Guard against runtime-evaluated PEP 604 aliases in desktop-packaged Python imports."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_IMPORT_GRAPH_ROOTS = [
    REPO_ROOT / 'desktop-tauri' / 'src-tauri' / 'python',
    REPO_ROOT / 'utils',
    REPO_ROOT / 'config.py',
]


def _iter_python_files():
    for root in DESKTOP_IMPORT_GRAPH_ROOTS:
        if root.is_file():
            yield root
            continue
        for path in root.rglob('*.py'):
            yield path


def _has_runtime_union_alias(node: ast.Assign) -> bool:
    # Runtime assignment like: Alias = Dict[str, int | str]
    value = node.value
    if not isinstance(value, ast.Subscript):
        return False
    return any(isinstance(subnode, ast.BinOp) and isinstance(subnode.op, ast.BitOr) for subnode in ast.walk(value))


def test_no_runtime_pep604_type_aliases_in_desktop_packaged_import_graph():
    offenders: list[str] = []
    for path in _iter_python_files():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _has_runtime_union_alias(node):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert offenders == [], (
        'Runtime-evaluated PEP 604 union aliases are not Python 3.9 compatible in desktop import paths: '
        + ', '.join(offenders)
    )
