"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import importlib.util
import os
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

_GUARDED_STDLIB_MODULES = (
    "collections", "typing", "ctypes", "subprocess", "json", "importlib", "pathlib",
)


def _normalize_compare_path(path_text: str | Path) -> str:
    path = _strip_windows_extended_path_prefix(str(path_text))
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _stdlib_roots() -> list[str]:
    roots: list[str] = []
    for key in ("stdlib", "platstdlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            compare = _normalize_compare_path(value)
            if compare not in roots:
                roots.append(compare)
    return roots


def _is_within(path_text: str, roots: list[str]) -> bool:
    compare = _normalize_compare_path(path_text)
    return any(compare == root or compare.startswith(root + os.sep) for root in roots)


def _is_site_packages_entry(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/").lower()
    return "site-packages" in normalized or "dist-packages" in normalized


def _dedupe_sys_path(entries: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            compare = _normalize_compare_path(entry or os.getcwd())
        except (TypeError, ValueError, OSError):
            compare = str(entry)
        if compare in seen:
            continue
        result.append(entry)
        seen.add(compare)
    return result


def harden_stdlib_import_order(*, app_roots: list[str] | None = None) -> dict[str, object]:
    """Keep stdlib import roots ahead of third-party site-packages entries."""
    app_roots = app_roots or []
    stdlib_roots = _stdlib_roots()
    app_compares = set()
    for root in app_roots:
        if root:
            try:
                app_compares.add(_normalize_compare_path(root))
            except (TypeError, ValueError, OSError):
                pass

    app_entries: list[str] = []
    stdlib_entries: list[str] = []
    other_entries: list[str] = []
    site_entries: list[str] = []
    stripped_prefixes: list[str] = []
    for raw_entry in sys.path:
        entry = raw_entry
        if isinstance(entry, str) and entry.startswith("\\\\?\\"):
            stripped_prefixes.append(entry)
            entry = _strip_windows_extended_path_prefix(entry)
        if not isinstance(entry, str):
            other_entries.append(entry)
            continue
        compare_source = entry or os.getcwd()
        try:
            compare = _normalize_compare_path(compare_source)
        except (TypeError, ValueError, OSError):
            compare = entry
        if compare in app_compares:
            app_entries.append(entry)
        elif _is_site_packages_entry(entry):
            site_entries.append(entry)
        elif _is_within(compare_source, stdlib_roots):
            stdlib_entries.append(entry)
        else:
            other_entries.append(entry)

    sys.path[:] = _dedupe_sys_path(app_entries + stdlib_entries + other_entries + site_entries)
    return {
        "stdlib_roots": stdlib_roots,
        "site_entries_moved_after_stdlib": len(site_entries),
        "extended_prefix_entries_normalized": stripped_prefixes,
        "sys_path_count": len(sys.path),
    }


def verify_stdlib_modules_not_shadowed(
    modules: tuple[str, ...] = _GUARDED_STDLIB_MODULES,
) -> dict[str, str]:
    """Raise ImportError if a guarded stdlib module resolves from site-packages."""
    origins: dict[str, str] = {}
    stdlib_roots = _stdlib_roots()
    for module_name in modules:
        spec = importlib.util.find_spec(module_name)
        origin = str(getattr(spec, "origin", "") or "")
        origins[module_name] = origin
        if not origin or origin in {"built-in", "frozen"}:
            continue
        if _is_site_packages_entry(origin) and not _is_within(origin, stdlib_roots):
            raise ImportError(f"stdlib module {module_name} shadowed by {origin}")
    return origins


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

    harden_stdlib_import_order(app_roots=valid_candidates)
    verify_stdlib_modules_not_shadowed()

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
