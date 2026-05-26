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


def _contains_type_alias_annotation(annotation: ast.AST | None) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == "TypeAlias":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "TypeAlias":
            return True
    return False


def _is_alias_like_target(target: ast.AST) -> bool:
    return isinstance(target, ast.Name) and bool(target.id) and target.id[0].isupper()


def _iter_import_time_assignment_nodes(tree: ast.AST):
    stack = [tree]
    while stack:
        scope = stack.pop()
        body = getattr(scope, "body", [])
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                yield node
            if isinstance(node, ast.ClassDef):
                stack.append(node)


def test_desktop_packaged_import_graph_has_no_runtime_pep604_type_alias_assignments() -> None:
    violations: list[str] = []

    for path in _iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _iter_import_time_assignment_nodes(tree):
            if isinstance(node, ast.Assign):
                value = node.value
                targets = node.targets
                alias_like = all(_is_alias_like_target(target) for target in targets)
            elif isinstance(node, ast.AnnAssign):
                value = node.value
                targets = [node.target]
                alias_like = _contains_type_alias_annotation(node.annotation) or _is_alias_like_target(node.target)
            else:
                continue

            if value is None or not alias_like:
                continue

            if _is_runtime_union_alias(value):
                target_names = [ast.unparse(target) for target in targets]
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{node.lineno} assigns runtime union alias: {', '.join(target_names)}")

    assert not violations, "\n".join(violations)
