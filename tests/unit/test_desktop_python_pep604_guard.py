"""Regression guards for runtime-evaluated PEP 604 aliases in desktop-packaged imports."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _python_files_in_scope() -> list[Path]:
    roots = [
        REPO_ROOT / 'desktop-tauri' / 'src-tauri' / 'python',
        REPO_ROOT / 'utils',
        REPO_ROOT / 'config.py',
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        files.extend(sorted(root.rglob('*.py')))
    return files


def _is_runtime_alias_with_pep604(node: ast.Assign) -> bool:
    return isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.BitOr)


def test_desktop_packaged_import_graph_has_no_runtime_pep604_alias_assignments() -> None:
    violations: list[str] = []
    for path in _python_files_in_scope():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_runtime_alias_with_pep604(node):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert violations == [], (
        'Runtime-evaluated PEP 604 alias assignment(s) found; use typing.Union or postpone into annotations: '
        + ', '.join(violations)
    )


def test_resource_monitor_imports_cleanly_for_desktop_bridge_path() -> None:
    import utils.system.resource_monitor as resource_monitor

    metrics = resource_monitor._gpu_metrics_default()
    assert 'gpu_available' in metrics
