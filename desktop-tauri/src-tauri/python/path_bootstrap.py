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
        resources_root / "_up_",  # tauri ".." resources are rewritten under _up_
        resources_root / "resources",
        resources_root / "resources" / "_up_",
        script_path.parent.parent.parent,
        script_path.parent.parent.parent / "resources",
        script_path.parent.parent.parent / "resources" / "_up_",
        script_path.parent.parent.parent / "Resources",
        script_path.parent.parent.parent / "Resources" / "_up_",
    ]

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
