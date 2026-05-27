"""Guard against runtime-evaluated PEP 604 type-alias assignments in desktop Python import paths."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (
    REPO_ROOT / "desktop-tauri" / "src-tauri" / "python",
    REPO_ROOT / "utils",
    REPO_ROOT / "config.py",
    REPO_ROOT / "encrypt.py",
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


def _is_typing_like_name(node: ast.AST) -> bool:
    typing_like_names = {
        "Dict",
        "List",
        "Set",
        "FrozenSet",
        "Tuple",
        "Mapping",
        "MutableMapping",
        "Sequence",
        "Iterable",
        "Optional",
        "Union",
        "Literal",
        "Annotated",
    }
    builtins_generics = {"dict", "list", "set", "frozenset", "tuple"}
    if isinstance(node, ast.Name):
        return node.id in typing_like_names or node.id in builtins_generics
    if isinstance(node, ast.Attribute):
        return node.attr in typing_like_names or node.attr in builtins_generics
    return False


def _contains_typing_like_context(value: ast.AST) -> bool:
    if isinstance(value, ast.Subscript) and _is_typing_like_name(value.value):
        return True
    return any(isinstance(node, ast.Subscript) and _is_typing_like_name(node.value) for node in ast.walk(value))


def _is_runtime_typing_union_alias(value: ast.AST) -> bool:
    return _is_runtime_union_alias(value) and _contains_typing_like_context(value)


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


def _iter_import_time_child_bodies(node: ast.AST) -> list[list[ast.stmt]]:
    if isinstance(node, ast.ClassDef):
        return [node.body]
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return [node.body, node.orelse]
    if isinstance(node, ast.With):
        return [node.body]
    if isinstance(node, ast.Try):
        bodies: list[list[ast.stmt]] = [node.body, node.orelse, node.finalbody]
        bodies.extend(handler.body for handler in node.handlers)
        return bodies
    if isinstance(node, ast.Match):
        return [case.body for case in node.cases]
    return []


def _iter_import_time_assignment_nodes(tree: ast.AST):
    stack: list[list[ast.stmt]] = [getattr(tree, "body", [])]
    while stack:
        body = stack.pop()
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                yield node
            stack.extend(_iter_import_time_child_bodies(node))


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

            if _is_runtime_typing_union_alias(value):
                target_names = [ast.unparse(target) for target in targets]
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{node.lineno} assigns runtime union alias: {', '.join(target_names)}")

    assert not violations, "\n".join(violations)


def test_desktop_packaged_import_graph_has_no_unconditional_dataclass_slots_true() -> None:
    violations: list[str] = []

    for path in _iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                is_dataclass = isinstance(func, ast.Name) and func.id == "dataclass"
                if not is_dataclass:
                    continue
                for keyword in decorator.keywords:
                    if keyword.arg == "slots" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                        violations.append(
                            f"{rel}:{node.lineno} uses dataclass(slots=True) without Python-version gating"
                        )

    assert not violations, "\n".join(violations)


def _first_assignment_from_source(source: str) -> ast.Assign | ast.AnnAssign:
    tree = ast.parse(source)
    for node in _iter_import_time_assignment_nodes(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            return node
    raise AssertionError("expected assignment in source")


def test_runtime_typing_union_alias_detection_regressions() -> None:
    ann_assign = _first_assignment_from_source(
        "from typing import Dict, TypeAlias\nGpuMetrics: TypeAlias = Dict[str, float | int | bool]\n"
    )
    assert _contains_type_alias_annotation(ann_assign.annotation)
    assert _is_runtime_typing_union_alias(ann_assign.value)

    class_assign = _first_assignment_from_source(
        "from typing import Dict\nclass Metrics:\n    GpuMetrics = Dict[str, float | int | bool]\n"
    )
    assert isinstance(class_assign, ast.Assign)
    assert _is_runtime_typing_union_alias(class_assign.value)

    conditional_assign = _first_assignment_from_source(
        "from typing import Dict\nif True:\n    GpuMetrics = Dict[str, float | int | bool]\n"
    )
    assert isinstance(conditional_assign, ast.Assign)
    assert _is_runtime_typing_union_alias(conditional_assign.value)

    bitwise_assign = _first_assignment_from_source("FLAGS = READ | WRITE\n")
    assert isinstance(bitwise_assign, ast.Assign)
    assert _is_runtime_union_alias(bitwise_assign.value)
    assert not _is_runtime_typing_union_alias(bitwise_assign.value)
