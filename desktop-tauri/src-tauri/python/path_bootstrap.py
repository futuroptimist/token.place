"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_runtime_import_paths(script_file: str) -> None:
    """Add likely import roots for development and packaged desktop layouts."""

    script_path = Path(script_file).resolve()
    resources_root = script_path.parent.parent
    candidates = [
        resources_root,  # bundled resources root in packaged apps
        script_path.parent.parent.parent,
    ]

    # Tauri rewrites out-of-tree bundle resources into nested `_up_` directories.
    # For paths like ../../utils, the runtime location becomes resources/_up_/_up_/utils.
    up_dir = resources_root
    for _ in range(4):
        up_dir = up_dir / "_up_"
        candidates.append(up_dir)

    if len(script_path.parents) > 3:
        candidates.append(script_path.parents[3])  # repo root in development tree

    valid_candidates: list[str] = []
    for candidate in candidates:
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
