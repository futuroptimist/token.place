from __future__ import annotations

import os
import sys
from pathlib import Path


def _looks_like_import_root(path: Path) -> bool:
    return path.joinpath("utils").is_dir() and path.joinpath("config.py").is_file()


def bootstrap_repo_imports(script_path: Path) -> None:
    """Add likely token.place import roots for packaged and dev bridge execution."""

    candidates = []

    override = os.environ.get("TOKEN_PLACE_PYTHON_IMPORT_ROOT", "").strip()
    if override:
        candidates.append(Path(override).expanduser().resolve())

    resolved = script_path.resolve()
    candidates.append(resolved.parents[3])
    candidates.extend(resolved.parents[:4])

    for candidate in candidates:
        if _looks_like_import_root(candidate):
            text = str(candidate)
            if text not in sys.path:
                sys.path.insert(0, text)
            return
