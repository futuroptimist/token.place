"""Runtime sys.path bootstrap for desktop Python bridge entrypoints."""

from __future__ import annotations

import importlib.util
import os
import site
import sys
import sysconfig
from collections.abc import Iterable

_CRITICAL_STDLIB_MODULES = (
    "collections",
    "typing",
    "ctypes",
    "subprocess",
    "json",
    "importlib",
    "pathlib",
)


def _strip_windows_extended_path_prefix(path_text: str) -> str:
    if path_text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_text[8:]
    if path_text.startswith("\\\\?\\"):
        return path_text[4:]
    return path_text


def _safe_resolve_path_text(path_text: str | os.PathLike[str]) -> str:
    text = _strip_windows_extended_path_prefix(os.fspath(path_text))
    return os.path.realpath(os.path.abspath(text))


def _path_exists(path_text: str | os.PathLike[str]) -> bool:
    try:
        return os.path.exists(_safe_resolve_path_text(path_text))
    except (OSError, TypeError, ValueError):
        return False


def _is_dir(path_text: str | os.PathLike[str]) -> bool:
    try:
        return os.path.isdir(_safe_resolve_path_text(path_text))
    except (OSError, TypeError, ValueError):
        return False


def _is_file(path_text: str | os.PathLike[str]) -> bool:
    try:
        return os.path.isfile(_safe_resolve_path_text(path_text))
    except (OSError, TypeError, ValueError):
        return False


def _join(path_text: str | os.PathLike[str], *parts: str) -> str:
    return os.path.join(_safe_resolve_path_text(path_text), *parts)


def _parent(path_text: str | os.PathLike[str], levels: int = 1) -> str:
    result = _safe_resolve_path_text(path_text)
    for _ in range(levels):
        result = os.path.dirname(result)
    return result


def _normcase(path_text: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(_safe_resolve_path_text(path_text)))


def _dedupe_key(path_text: str | os.PathLike[str]) -> str | None:
    try:
        return _normcase(path_text)
    except (OSError, TypeError, ValueError):
        return None


def _path_contains(path_text: str, parent_text: str) -> bool:
    try:
        common = os.path.commonpath(
            [_safe_resolve_path_text(path_text), _safe_resolve_path_text(parent_text)]
        )
    except (OSError, TypeError, ValueError):
        return False
    return _normcase(common) == _normcase(parent_text)


def _stdlib_roots() -> list[str]:
    roots: list[str] = []
    for key in ("stdlib", "platstdlib"):
        value = sysconfig.get_paths().get(key)
        if value and _path_exists(value):
            roots.append(_safe_resolve_path_text(value))
    destshared = sysconfig.get_config_var("DESTSHARED")
    if destshared and _path_exists(str(destshared)):
        roots.append(_safe_resolve_path_text(str(destshared)))
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    for version in (
        f"{sys.version_info.major}.{sys.version_info.minor}",
        f"python{sys.version_info.major}.{sys.version_info.minor}",
    ):
        candidate = os.path.join(
            base_prefix, "Lib" if os.name == "nt" else "lib", version
        )
        if _path_exists(candidate):
            roots.append(_safe_resolve_path_text(candidate))
    deduped: list[str] = []
    seen: set[str] = set()
    for root in roots:
        key = _dedupe_key(root)
        if key and key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _is_site_packages_path(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/").lower()
    return "site-packages" in normalized or "dist-packages" in normalized


def _is_stdlib_path(path_text: str) -> bool:
    if _is_site_packages_path(path_text):
        return False
    return any(_path_contains(path_text, root) for root in _stdlib_roots())


def _ordered_sys_path_with_stdlib_first(app_roots: Iterable[str]) -> list[str]:
    cwd = os.getcwd()
    app_root_keys = {_dedupe_key(root) for root in app_roots}
    ordered_app_roots: list[str] = []
    seen_app: set[str] = set()
    for root in app_roots:
        key = _dedupe_key(root)
        if key and key not in seen_app:
            ordered_app_roots.append(_safe_resolve_path_text(root))
            seen_app.add(key)

    stdlib_entries: list[str] = []
    non_site_entries: list[str] = []
    site_entries: list[str] = []
    seen_existing: set[str] = set(seen_app)
    for entry in sys.path:
        entry_text = str(entry or cwd)
        key = _dedupe_key(entry_text)
        if key is None or key in app_root_keys or key in seen_existing:
            continue
        seen_existing.add(key)
        safe_entry = _subprocess_safe_path_text(entry_text)
        if _is_stdlib_path(safe_entry):
            stdlib_entries.append(safe_entry)
        elif _is_site_packages_path(safe_entry):
            site_entries.append(safe_entry)
        else:
            non_site_entries.append(safe_entry)

    for root in _stdlib_roots():
        key = _dedupe_key(root)
        if key and key not in seen_existing:
            stdlib_entries.append(root)
            seen_existing.add(key)

    # The critical invariant is stdlib before site/dist-packages.  App roots sit
    # after stdlib so bundled code stays importable without being able to shadow
    # Python's own modules such as pathlib.
    return stdlib_entries + ordered_app_roots + non_site_entries + site_entries


def _subprocess_safe_path_text(path_text: object) -> str:
    return _strip_windows_extended_path_prefix(str(path_text))


def _is_shadowed_stdlib_spec(module_name: str, origin: str | None) -> bool:
    if origin in (None, "built-in", "frozen"):
        return False
    if _is_site_packages_path(origin):
        return True
    return not _is_stdlib_path(origin)


def ensure_stdlib_not_shadowed(
    modules: Iterable[str] = _CRITICAL_STDLIB_MODULES,
) -> None:
    """Raise a clear error if a critical stdlib module resolves outside stdlib."""

    importlib.invalidate_caches()
    for module_name in modules:
        spec = importlib.util.find_spec(module_name)
        origin = getattr(spec, "origin", None) if spec is not None else None
        if spec is None or _is_shadowed_stdlib_spec(module_name, origin):
            bad_path = origin or "<not found>"
            raise RuntimeError(f"stdlib module {module_name} shadowed by {bad_path}")


def ensure_runtime_import_paths(
    script_file: str, *, avoid_llama_cpp_shadowing: bool = True
) -> None:
    """Add likely import roots for development and packaged desktop layouts."""

    script_path = _safe_resolve_path_text(script_file)
    script_dir = os.path.dirname(script_path)
    script_root = _parent(script_dir)
    explicit_import_root = os.environ.get("TOKEN_PLACE_PYTHON_IMPORT_ROOT", "").strip()
    desktop_dependency_target = os.environ.get("TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET", "").strip()
    candidates = [
        _safe_resolve_path_text(desktop_dependency_target) if desktop_dependency_target else None,
        _safe_resolve_path_text(explicit_import_root) if explicit_import_root else None,
        script_root,  # bundled resources root in packaged apps
        _join(
            script_root, "resources"
        ),  # no-bundle/debug layout when script is under <exe>/python
        _join(script_root, "Resources"),  # macOS-style resources casing
        _join(script_root, "_up_"),  # tauri ".." resources are rewritten under _up_
        _join(
            script_root, "_up_", "_up_"
        ),  # tauri "../.." resources can nest _up_ segments
        _parent(script_dir, 2),
        _parent(script_dir, 3),  # repo root in development tree
    ]

    valid_candidates: list[str] = []
    for candidate in candidates:
        if candidate is None or not _path_exists(candidate):
            continue
        has_runtime_modules = (
            _is_dir(_join(candidate, "utils"))
            or _is_file(_join(candidate, "config.py"))
            or _is_dir(_join(candidate, "llama_cpp"))
            or _is_file(_join(candidate, "llama_cpp.py"))
        )
        if has_runtime_modules:
            candidate_str = _safe_resolve_path_text(candidate)
            if candidate_str not in valid_candidates:
                valid_candidates.append(candidate_str)

    sys.path[:] = _ordered_sys_path_with_stdlib_first(valid_candidates)

    if os.environ.get("PYTHONNOUSERSITE") == "1":
        user_site = getattr(site, "USER_SITE", None)
        if user_site:
            user_site_path = _dedupe_key(user_site)
            sys.path[:] = [
                entry
                for entry in sys.path
                if _dedupe_key(entry or ".") != user_site_path
            ]

    if avoid_llama_cpp_shadowing:
        cwd = _safe_resolve_path_text(os.getcwd())
        if _is_file(_join(cwd, "llama_cpp.py")):
            sys.path[:] = [
                entry
                for entry in sys.path
                if entry != "" and _dedupe_key(entry) != _dedupe_key(cwd)
            ]

        # Keep repo roots importable for `utils.*` / `config` while avoiding local
        # llama_cpp.py shim precedence over site-packages.
        for candidate_str in valid_candidates:
            if not _is_file(_join(candidate_str, "llama_cpp.py")):
                continue

            candidate_key = _dedupe_key(candidate_str)
            sys.path[:] = [
                entry for entry in sys.path if _dedupe_key(entry) != candidate_key
            ]

            preferred_index = len(sys.path)
            for idx, entry in enumerate(sys.path):
                if _is_site_packages_path(str(entry)):
                    preferred_index = idx + 1
            sys.path.insert(preferred_index, candidate_str)

    ensure_stdlib_not_shadowed()
