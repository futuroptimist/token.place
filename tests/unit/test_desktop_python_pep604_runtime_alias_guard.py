"""Guard against runtime-evaluated PEP 604 type-alias assignments in desktop Python import paths."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (
    REPO_ROOT / "desktop-tauri" / "src-tauri" / "python",
    REPO_ROOT / "utils",
    REPO_ROOT / "config.py",
)


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            files.append(root)
            continue
        files.extend(sorted(root.rglob("*.py")))
    return files


def _is_runtime_union_alias(value: ast.AST) -> bool:
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
        return True
    if isinstance(value, ast.Subscript):
        return _is_runtime_union_alias(value.slice)
    if isinstance(value, ast.Tuple):
        return any(_is_runtime_union_alias(elt) for elt in value.elts)
    if isinstance(value, ast.Dict):
        return any(_is_runtime_union_alias(k) for k in value.keys if k is not None) or any(
            _is_runtime_union_alias(v) for v in value.values
        )
    if isinstance(value, ast.List):
        return any(_is_runtime_union_alias(elt) for elt in value.elts)
    return False


def test_desktop_packaged_import_graph_has_no_runtime_pep604_type_alias_assignments() -> None:
    violations: list[str] = []

    for path in _iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if _is_runtime_union_alias(node.value):
                targets = [ast.unparse(target) for target in node.targets]
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{node.lineno} assigns runtime union alias: {', '.join(targets)}")

    assert not violations, "\n".join(violations)
