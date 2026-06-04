"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path


def _strip_windows_extended_path_prefix(path_text: str) -> str:
    if path_text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_text[8:]
    if path_text.startswith("\\\\?\\"):
        return path_text[4:]
    return path_text


def _safe_resolve_path(path_text: str | Path) -> Path:
    return Path(_strip_windows_extended_path_prefix(str(path_text))).resolve()


def ensure_runtime_import_paths(script_file: str, *, avoid_llama_cpp_shadowing: bool = True) -> None:
    """Add likely import roots for development and packaged desktop layouts."""

    script_path = _safe_resolve_path(script_file)
    script_root = script_path.parent.parent
    explicit_import_root = os.environ.get("TOKEN_PLACE_PYTHON_IMPORT_ROOT", "").strip()
    candidates = [
        _safe_resolve_path(explicit_import_root) if explicit_import_root else None,
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
        while candidate_str in sys.path:
            sys.path.remove(candidate_str)
        sys.path.insert(0, candidate_str)

    if os.environ.get("PYTHONNOUSERSITE") == "1":
        user_site = getattr(site, "USER_SITE", None)
        if user_site:
            user_site_path = _safe_resolve_path(user_site)
            sys.path[:] = [
                entry
                for entry in sys.path
                if _safe_resolve_path(entry or ".") != user_site_path
            ]

    if not avoid_llama_cpp_shadowing:
        return

    cwd = _safe_resolve_path(Path.cwd())
    if (cwd / "llama_cpp.py").is_file():
        sys.path[:] = [
            entry
            for entry in sys.path
            if entry != "" and _safe_resolve_path(entry) != cwd
        ]

    # Keep repo roots importable for `utils.*` / `config` while avoiding local
    # llama_cpp.py shim precedence over site-packages.
    for candidate_str in valid_candidates:
        candidate = Path(candidate_str)
        if not (candidate / "llama_cpp.py").is_file():
            continue

        cwd = str(_safe_resolve_path(Path.cwd()))
        if _safe_resolve_path(candidate) == _safe_resolve_path(Path.cwd()):
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
