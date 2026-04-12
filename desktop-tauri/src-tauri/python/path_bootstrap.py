"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import sys
from pathlib import Path


def _candidate_roots(resources_root: Path, script_path: Path) -> list[Path]:
    candidates: list[Path] = [
        resources_root,
        script_path.parent.parent.parent,
    ]

    # Tauri rewrites ".." path traversals in bundle resources as nested _up_ folders.
    up_cursor = resources_root
    for _ in range(4):
        up_cursor = up_cursor / "_up_"
        candidates.append(up_cursor)

    if len(script_path.parents) > 3:
        candidates.append(script_path.parents[3])

    return candidates


def ensure_runtime_import_paths(script_file: str) -> None:
    """Add likely import roots for development and packaged desktop layouts."""

    script_path = Path(script_file).resolve()
    resources_root = script_path.parent.parent

    valid_candidates: list[str] = []
    for candidate in _candidate_roots(resources_root, script_path):
        if not candidate.exists():
            continue
        has_runtime_modules = (candidate / "utils").is_dir() or (candidate / "config.py").is_file()
        if has_runtime_modules:
            candidate_str = str(candidate)
            if candidate_str not in valid_candidates:
                valid_candidates.append(candidate_str)

    # Preserve candidate priority: first valid candidate should be first on sys.path.
    for candidate_str in reversed(valid_candidates):
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)
