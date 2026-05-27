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


def _contains_runtime_bitor(value: ast.AST) -> bool:
    return any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) for node in ast.walk(value))


def _contains_type_alias_annotation(annotation: ast.AST | None) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == "TypeAlias":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "TypeAlias":
            return True
    return False


def _known_safe_runtime_bitor_assignment(target: ast.AST, value: ast.AST) -> bool:
    """Narrow escape hatch for legitimate runtime bitwise assignments.

    Import-time `|` in assignment RHS is forbidden by default for Python 3.9 safety
    in desktop-packaged Python. Add explicit cases here only when known-safe and
    clearly non-typing.
    """
    return (
        isinstance(target, ast.Name)
        and target.id == "FLAGS"
        and isinstance(value, ast.BinOp)
        and isinstance(value.op, ast.BitOr)
        and isinstance(value.left, ast.Name)
        and value.left.id == "READ"
        and isinstance(value.right, ast.Name)
        and value.right.id == "WRITE"
    )


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
            elif isinstance(node, ast.AnnAssign):
                value = node.value
                targets = [node.target]
            else:
                continue

            if value is None or not _contains_runtime_bitor(value):
                continue

            if all(_known_safe_runtime_bitor_assignment(target, value) for target in targets):
                continue

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
                        rel = path.relative_to(REPO_ROOT)
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
    assert _contains_runtime_bitor(ann_assign.value)

    class_assign = _first_assignment_from_source(
        "from typing import Dict\nclass Metrics:\n    GpuMetrics = Dict[str, float | int | bool]\n"
    )
    assert isinstance(class_assign, ast.Assign)
    assert _contains_runtime_bitor(class_assign.value)

    conditional_assign = _first_assignment_from_source(
        "from typing import Dict\nif True:\n    GpuMetrics = Dict[str, float | int | bool]\n"
    )
    assert isinstance(conditional_assign, ast.Assign)
    assert _contains_runtime_bitor(conditional_assign.value)

    direct_alias_assign = _first_assignment_from_source("GpuMetricValue = float | int | bool\n")
    assert isinstance(direct_alias_assign, ast.Assign)
    assert _contains_runtime_bitor(direct_alias_assign.value)

    callable_alias_assign = _first_assignment_from_source(
        "from typing import Callable\nBytesOrText = Callable[[str | bytes], None]\n"
    )
    assert isinstance(callable_alias_assign, ast.Assign)
    assert _contains_runtime_bitor(callable_alias_assign.value)

    capitalized_alias_assign = _first_assignment_from_source("PathLike = str | Path\n")
    assert isinstance(capitalized_alias_assign, ast.Assign)
    assert _contains_runtime_bitor(capitalized_alias_assign.value)

    wrapper_alias_assign = _first_assignment_from_source(
        "from typing import Type\nTypePath = Type[str | Path]\n"
    )
    assert isinstance(wrapper_alias_assign, ast.Assign)
    assert _contains_runtime_bitor(wrapper_alias_assign.value)

    acronym_alias_assign = _first_assignment_from_source("MaybeUUID = UUID | str\n")
    assert isinstance(acronym_alias_assign, ast.Assign)
    assert _contains_runtime_bitor(acronym_alias_assign.value)

    all_acronym_alias_assign = _first_assignment_from_source("MaybeURL = URL | UUID\n")
    assert isinstance(all_acronym_alias_assign, ast.Assign)
    assert _contains_runtime_bitor(all_acronym_alias_assign.value)

    bitwise_assign = _first_assignment_from_source("FLAGS = READ | WRITE\n")
    assert isinstance(bitwise_assign, ast.Assign)
    assert _contains_runtime_bitor(bitwise_assign.value)
    assert _known_safe_runtime_bitor_assignment(bitwise_assign.targets[0], bitwise_assign.value)

    uppercase_direct_alias_assign = _first_assignment_from_source("T = str | bytes\n")
    assert isinstance(uppercase_direct_alias_assign, ast.Assign)
    assert _contains_runtime_bitor(uppercase_direct_alias_assign.value)

    uppercase_acronym_alias_assign = _first_assignment_from_source("URL = UUID | URL\n")
    assert isinstance(uppercase_acronym_alias_assign, ast.Assign)
    assert _contains_runtime_bitor(uppercase_acronym_alias_assign.value)
