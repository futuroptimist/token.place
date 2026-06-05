"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

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

_STDLIB_GUARD_MODULES = (
    "collections",
    "typing",
    "ctypes",
    "subprocess",
    "json",
    "importlib",
    "pathlib",
)


def _normalize_for_compare(path_text: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(_strip_windows_extended_path_prefix(str(path_text)))))


def _looks_like_site_packages(path_text: str | Path | None) -> bool:
    if path_text is None:
        return False
    normalized = str(path_text).replace("\\", "/").lower()
    return "site-packages" in normalized or "dist-packages" in normalized


def _stdlib_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("stdlib", "platstdlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            with suppress_path_errors():
                roots.append(_safe_resolve_path(value))
    return _dedupe_paths(roots)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = _normalize_for_compare(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


class suppress_path_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, _tb) -> bool:
        return exc_type in (OSError, ValueError, TypeError)


def _is_under(path: Path, root: Path) -> bool:
    with suppress_path_errors():
        path.resolve().relative_to(root.resolve())
        return True
    return False


def _is_stdlib_path(path_text: str | Path | None, stdlib_roots: list[Path]) -> bool:
    if path_text in (None, "", "."):
        return False
    if _looks_like_site_packages(path_text):
        return False
    with suppress_path_errors():
        path = _safe_resolve_path(path_text)
        return any(path == root or _is_under(path, root) for root in stdlib_roots)
    return False


def _entry_compare(entry: str) -> str | None:
    with suppress_path_errors():
        return _normalize_for_compare(entry or Path.cwd())
    return None


def _reorder_import_paths_for_stdlib(runtime_roots: list[str]) -> None:
    """Keep packaged roots importable while ensuring stdlib precedes site-packages."""

    stdlib_roots = _stdlib_roots()
    runtime_compares = {_entry_compare(root) for root in runtime_roots}
    runtime_compares.discard(None)

    runtime_entries: list[str] = []
    stdlib_entries: list[str] = []
    other_entries: list[str] = []
    site_entries: list[str] = []
    seen: set[str] = set()

    for entry in sys.path:
        if not isinstance(entry, str):
            other_entries.append(entry)
            continue
        compare = _entry_compare(entry)
        dedupe_key = compare or entry
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if compare in runtime_compares:
            runtime_entries.append(entry)
        elif _is_stdlib_path(entry, stdlib_roots):
            stdlib_entries.append(entry)
        elif _looks_like_site_packages(entry):
            site_entries.append(entry)
        else:
            other_entries.append(entry)

    sys.path[:] = runtime_entries + stdlib_entries + other_entries + site_entries


def verify_stdlib_not_shadowed(module_names: tuple[str, ...] = _STDLIB_GUARD_MODULES) -> None:
    """Raise an actionable error if a guarded stdlib module resolves from site-packages."""

    import importlib.util

    for module_name in module_names:
        spec = importlib.util.find_spec(module_name)
        origin = getattr(spec, "origin", None) if spec else None
        if origin and _looks_like_site_packages(origin):
            raise ImportError(f"stdlib module {module_name} shadowed by {origin}")



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

    _reorder_import_paths_for_stdlib(valid_candidates)

    if os.environ.get("PYTHONNOUSERSITE") == "1":
        user_site = getattr(site, "USER_SITE", None)
        if user_site:
            user_site_path = _safe_resolve_path(user_site)
            sys.path[:] = [
                entry
                for entry in sys.path
                if _safe_resolve_path(entry or ".") != user_site_path
            ]

    extra_site_paths = os.environ.get("TOKEN_PLACE_DESKTOP_EXTRA_SITE_PACKAGES", "").strip()
    if extra_site_paths:
        for extra_site_path in extra_site_paths.split(os.pathsep):
            extra_site_path = extra_site_path.strip()
            if not extra_site_path:
                continue
            extra_site = str(_safe_resolve_path(extra_site_path))
            if extra_site not in sys.path:
                insert_at = len(sys.path)
                for index, entry in enumerate(sys.path):
                    if _looks_like_site_packages(entry):
                        insert_at = index
                        break
                sys.path.insert(insert_at, extra_site)
        _reorder_import_paths_for_stdlib(valid_candidates)

    verify_stdlib_not_shadowed()

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

    verify_stdlib_not_shadowed()
