"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _has_runtime_modules(path: Path) -> bool:
    return (path / "utils").is_dir() or (path / "config.py").is_file()


def _resource_candidates(resource_dir: Path) -> list[Path]:
    # Tauri rewrites ".." path segments into nested "_up_" directories.
    candidates = [resource_dir]
    nested = resource_dir
    for _ in range(4):
        nested = nested / "_up_"
        candidates.append(nested)
    return candidates


def ensure_runtime_import_paths(script_file: str) -> None:
    """Add likely import roots for development and packaged desktop layouts."""

    script_path = Path(script_file).resolve()
    script_root = script_path.parent.parent
    env_import_root = os.environ.get("TOKEN_PLACE_PYTHON_IMPORT_ROOT", "").strip()
    env_resource_dir = os.environ.get("TOKEN_PLACE_RESOURCE_DIR", "").strip()
    candidates = [
        script_root,  # bundled resources root in packaged apps
        script_root / "resources",  # no-bundle/debug layout when script is under <exe>/python
        script_root / "Resources",  # macOS-style resources casing
        script_root / "_up_",  # tauri ".." resources are rewritten under _up_
        script_path.parent.parent.parent,
    ]
    if env_import_root:
        candidates.insert(0, Path(env_import_root))
    if env_resource_dir:
        candidates = _resource_candidates(Path(env_resource_dir)) + candidates

    if len(script_path.parents) > 3:
        candidates.append(script_path.parents[3])  # repo root in development tree

    valid_candidates: list[str] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        has_runtime_modules = _has_runtime_modules(candidate)
        if has_runtime_modules:
            candidate_str = str(candidate)
            if candidate_str not in valid_candidates:
                valid_candidates.append(candidate_str)

    # Preserve candidate priority: first valid candidate should be first on sys.path.
    for candidate_str in reversed(valid_candidates):
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)
