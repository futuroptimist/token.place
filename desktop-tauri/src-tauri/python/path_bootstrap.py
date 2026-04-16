"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import sys
import os
from pathlib import Path


def ensure_runtime_import_paths(script_file: str) -> None:
    """Add likely import roots for development and packaged desktop layouts."""

    script_path = Path(script_file).resolve()
    script_root = script_path.parent.parent
    explicit_import_root = os.environ.get("TOKEN_PLACE_PYTHON_IMPORT_ROOT", "").strip()
    candidates = [
        Path(explicit_import_root) if explicit_import_root else None,
        script_root,  # bundled resources root in packaged apps
        script_root / "resources",  # no-bundle/debug layout when script is under <exe>/python
        script_root / "Resources",  # macOS-style resources casing
        script_root / "_up_",  # tauri ".." resources are rewritten under _up_
        script_root / "_up_" / "_up_",  # tauri "../.." resources can nest _up_ segments
        script_path.parent.parent.parent,
    ]

    if len(script_path.parents) > 3:
        candidates.append(script_path.parents[3])  # repo root in development tree

    valid_candidates: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
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

    # Prevent repo-local `llama_cpp.py` from shadowing installed llama-cpp-python.
    for candidate_str in valid_candidates:
        candidate = Path(candidate_str)
        if not (candidate / "llama_cpp.py").is_file():
            continue

        # Keep the repo import root available, but place it behind site-packages so
        # `import llama_cpp` resolves to the installed package.
        while candidate_str in sys.path:
            sys.path.remove(candidate_str)
        sys.path.append(candidate_str)

        cwd = str(Path.cwd().resolve())
        if candidate.resolve() == Path.cwd().resolve():
            while "" in sys.path:
                sys.path.remove("")
            if cwd in sys.path:
                while cwd in sys.path:
                    sys.path.remove(cwd)
                sys.path.append(cwd)
