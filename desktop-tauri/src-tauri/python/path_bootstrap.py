"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import os
import importlib.util
import site
import sys
import sysconfig
from pathlib import Path


def _strip_windows_extended_path_prefix(path_text: str) -> str:
    if path_text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_text[8:]
    if path_text.startswith("\\\\?\\"):
        return path_text[4:]
    return path_text


def _safe_resolve_path(path_text: str | Path) -> Path:
    return Path(_strip_windows_extended_path_prefix(str(path_text))).resolve()


_STDLIB_GUARD_MODULES = (
    "collections",
    "typing",
    "ctypes",
    "subprocess",
    "json",
    "importlib",
    "pathlib",
)


def _is_site_package_entry(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/").lower()
    return "site-packages" in normalized or "dist-packages" in normalized


def _stdlib_roots() -> list[str]:
    roots: list[str] = []
    for key in ("stdlib", "platstdlib"):
        value = sysconfig.get_path(key)
        if not value:
            continue
        try:
            root = str(_safe_resolve_path(value))
        except (OSError, RuntimeError):
            root = _strip_windows_extended_path_prefix(str(value))
        if root not in roots:
            roots.append(root)
    return roots


def _is_stdlib_entry(path_text: str, stdlib_roots: list[str] | None = None) -> bool:
    if not path_text or _is_site_package_entry(path_text):
        return False
    roots = stdlib_roots if stdlib_roots is not None else _stdlib_roots()
    if not roots:
        return False
    try:
        candidate = str(_safe_resolve_path(path_text))
    except (OSError, RuntimeError):
        candidate = _strip_windows_extended_path_prefix(str(path_text))
    for root in roots:
        try:
            if candidate == root or Path(candidate).is_relative_to(Path(root)):
                return True
        except (ValueError, OSError):
            if candidate.startswith(root.rstrip("/\\") + os.sep):
                return True
    return False


def _move_site_packages_after_stdlib() -> None:
    """Keep stdlib import locations ahead of third-party site-package paths."""

    stdlib_roots = _stdlib_roots()
    if not stdlib_roots:
        return
    has_stdlib_after_site = False
    seen_site = False
    for entry in sys.path:
        entry_text = str(entry or ".")
        if _is_site_package_entry(entry_text):
            seen_site = True
        elif seen_site and _is_stdlib_entry(entry_text, stdlib_roots):
            has_stdlib_after_site = True
            break
    if not has_stdlib_after_site:
        return

    deferred_sites: list[str] = []
    retained: list[str] = []
    entries = list(sys.path)
    for idx, entry in enumerate(entries):
        entry_text = str(entry or ".")
        if _is_site_package_entry(entry_text):
            later_stdlib = any(
                _is_stdlib_entry(str(later or "."), stdlib_roots)
                for later in entries[idx + 1 :]
            )
            if later_stdlib:
                deferred_sites.append(entry)
                continue
        retained.append(entry)

    insert_at = 0
    for idx, entry in enumerate(retained):
        if _is_stdlib_entry(str(entry or "."), stdlib_roots):
            insert_at = idx + 1
    sys.path[:] = retained[:insert_at] + deferred_sites + retained[insert_at:]


def verify_stdlib_not_shadowed(module_names: tuple[str, ...] = _STDLIB_GUARD_MODULES) -> None:
    """Fail clearly if a critical stdlib module resolves to site-packages."""

    _move_site_packages_after_stdlib()
    for module_name in module_names:
        spec = importlib.util.find_spec(module_name)
        origin = getattr(spec, "origin", None) if spec else None
        if not origin or origin in {"built-in", "frozen"}:
            continue
        origin_text = _strip_windows_extended_path_prefix(str(origin))
        if _is_site_package_entry(origin_text):
            raise ImportError(f"stdlib module {module_name} shadowed by {origin_text}")


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

    _move_site_packages_after_stdlib()

    if not avoid_llama_cpp_shadowing:
        verify_stdlib_not_shadowed()
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

    _move_site_packages_after_stdlib()
    verify_stdlib_not_shadowed()
