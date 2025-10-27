"""Regression tests for dependency footprint of client_simplified."""

from __future__ import annotations

import ast
from pathlib import Path


CLIENT_SIMPLIFIED_PATH = Path(__file__).resolve().parents[2] / "client_simplified.py"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_client_simplified_avoids_unused_typing_import():
    """Simplified CLI client should stay dependency-light (docs/CHANGELOG promise)."""

    modules = _imported_modules(CLIENT_SIMPLIFIED_PATH)
    assert "typing" not in modules, (
        "client_simplified.py should not import typing; use builtin generics instead"
    )
