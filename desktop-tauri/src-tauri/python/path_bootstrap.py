"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_runtime_import_paths(script_file: str) -> None:
    """Add likely import roots for development and packaged desktop layouts."""

    script_path = Path(script_file).resolve()
    candidates = [
        script_path.parent.parent,  # bundled resources root in packaged apps
        script_path.parent.parent.parent,
    ]

    if len(script_path.parents) > 3:
        candidates.append(script_path.parents[3])  # repo root in development tree

    for candidate in candidates:
        if not candidate.exists():
            continue
        has_runtime_modules = (candidate / "utils").is_dir() or (candidate / "config.py").is_file()
        if has_runtime_modules:
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)
