"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path


def _strip_windows_extended_path_prefix(path_value: str) -> str:
    if path_value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path_value[8:]
    if path_value.startswith('\\\\?\\'):
        return path_value[4:]
    return path_value


def _normalized_path_key(path_value: object) -> str:
    stripped = _strip_windows_extended_path_prefix(str(path_value))
    try:
        resolved = str(Path(stripped).resolve())
    except (TypeError, ValueError, OSError):
        resolved = stripped
    return resolved.replace('\\', '/').lower()


def ensure_runtime_import_paths(script_file: str, *, avoid_llama_cpp_shadowing: bool = True) -> None:
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
            if not any(
                _normalized_path_key(candidate_str) == _normalized_path_key(existing)
                for existing in valid_candidates
            ):
                valid_candidates.append(candidate_str)

    # Preserve candidate priority: first valid candidate should be first on sys.path.
    for candidate_str in reversed(valid_candidates):
        while candidate_str in sys.path:
            sys.path.remove(candidate_str)
        sys.path.insert(0, candidate_str)

    if os.environ.get("PYTHONNOUSERSITE") == "1":
        user_site = getattr(site, "USER_SITE", None)
        if user_site:
            user_site_path = Path(user_site).resolve()
            sys.path[:] = [
                entry
                for entry in sys.path
                if _normalized_path_key(entry or ".") != _normalized_path_key(user_site_path)
            ]

    if not avoid_llama_cpp_shadowing:
        return

    cwd = Path.cwd().resolve()
    if (cwd / "llama_cpp.py").is_file():
        sys.path[:] = [
            entry
            for entry in sys.path
            if entry != "" and _normalized_path_key(entry) != _normalized_path_key(cwd)
        ]

    # Keep repo roots importable for `utils.*` / `config` while avoiding local
    # llama_cpp.py shim precedence over site-packages.
    for candidate_str in valid_candidates:
        candidate = Path(candidate_str)
        if not (candidate / "llama_cpp.py").is_file():
            continue

        cwd = str(Path.cwd().resolve())
        if _normalized_path_key(candidate) == _normalized_path_key(Path.cwd()):
            while "" in sys.path:
                sys.path.remove("")
            while cwd in sys.path:
                sys.path.remove(cwd)

        while candidate_str in sys.path:
            sys.path.remove(candidate_str)

        preferred_index = len(sys.path)
        for idx, entry in enumerate(sys.path):
            normalized = str(entry).replace("\\", "/").lower()
            if "site-packages" in normalized or "dist-packages" in normalized:
                preferred_index = idx + 1
        sys.path.insert(preferred_index, candidate_str)
