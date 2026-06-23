"""
Model manager module for handling LLM model downloading, initialization and inference.
"""
import os
import time
import logging
from utils.networking.http_requests_compat import requests
import json
import sys
import importlib
import subprocess
import queue
import signal
import threading
import sysconfig
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock
from typing import Dict, List, Any, Optional, Iterable

from utils.system import resource_monitor

# Configure logging
logger = logging.getLogger('model_manager')
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_LLAMA_CPP_SHIM = (REPO_ROOT / 'llama_cpp.py').resolve()
DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS = 30.0

CRITICAL_STDLIB_IMPORT_MODULES = (
    'collections',
    'typing',
    'ctypes',
    'subprocess',
    'json',
    'importlib',
    'pathlib',
)


def _is_site_packages_path(path_text: Any) -> bool:
    normalized = str(path_text).replace('\\', '/').lower()
    return 'site-packages' in normalized or 'dist-packages' in normalized


def _stdlib_roots_for_import_order() -> list[str]:
    roots: list[str] = []
    for key in ('stdlib', 'platstdlib'):
        value = sysconfig.get_paths().get(key)
        if value:
            roots.append(_subprocess_safe_path_text(value))
    destshared = sysconfig.get_config_var('DESTSHARED')
    if destshared:
        roots.append(_subprocess_safe_path_text(destshared))
    for prefix in {sys.prefix, getattr(sys, 'base_prefix', sys.prefix), getattr(sys, 'exec_prefix', sys.prefix), getattr(sys, 'base_exec_prefix', sys.prefix)}:
        roots.append(_subprocess_safe_path_text(os.path.join(prefix, 'lib', f'python{sys.version_info.major}.{sys.version_info.minor}')))
        roots.append(_subprocess_safe_path_text(os.path.join(prefix, 'Lib')))
    deduped: list[str] = []
    seen: set[str] = set()
    for root in roots:
        compare = _canonical_path_for_compare(root)
        if compare and compare not in seen:
            seen.add(compare)
            deduped.append(root)
    return deduped


def _is_stdlib_path(path_text: Any) -> bool:
    if _is_site_packages_path(path_text):
        return False
    path_compare = _canonical_path_for_compare(path_text)
    if path_compare is None:
        return False
    for root in _stdlib_roots_for_import_order():
        root_compare = _canonical_path_for_compare(root)
        if not root_compare:
            continue
        try:
            if os.path.commonpath([path_compare, root_compare]) == root_compare:
                return True
        except ValueError:
            continue
    return False


def _stdlib_shadow_error(module_name: str, origin: Any) -> Optional[str]:
    if origin in (None, 'built-in', 'frozen'):
        return None
    if _is_site_packages_path(origin) or not _is_stdlib_path(origin):
        return f"stdlib module {module_name} shadowed by {origin or '<not found>'}"
    return None


def _assert_critical_stdlib_not_shadowed() -> None:
    importlib.invalidate_caches()
    for module_name in CRITICAL_STDLIB_IMPORT_MODULES:
        spec = importlib.util.find_spec(module_name)
        origin = getattr(spec, 'origin', None) if spec is not None else None
        error = _stdlib_shadow_error(module_name, origin) if spec is not None else (
            f"stdlib module {module_name} shadowed by <not found>"
        )
        if error:
            raise ImportError(error)


def _stdlib_safe_path_order(entries: Iterable[str]) -> list[str]:
    stdlib_entries: list[str] = []
    app_entries: list[str] = []
    site_entries: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            continue
        safe_entry = _subprocess_safe_path_text(entry)
        compare = _canonical_path_for_compare(safe_entry)
        if compare is None or compare in seen:
            continue
        seen.add(compare)
        if _is_stdlib_path(safe_entry):
            stdlib_entries.append(safe_entry)
        elif _is_site_packages_path(safe_entry):
            site_entries.append(safe_entry)
        else:
            app_entries.append(safe_entry)
    return stdlib_entries + app_entries + site_entries

DESKTOP_RUNTIME_PROBE_ENV = 'TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON'
_LLAMA_CPP_IMPORT_PATH_LOCK = Lock()


class LlamaCppInferenceRequestError(RuntimeError):
    """Raised when a live llama.cpp worker reports a request-scoped failure."""

    def __init__(self, message: str, *, diagnostics: Optional[Dict[str, Any]] = None) -> None:
        self.diagnostics = diagnostics or {}
        super().__init__(message)


class LlamaCppRestartableWorkerError(RuntimeError):
    """Raised when the llama.cpp worker transport is unusable and may be replaced."""


class LlamaCppWorkerDeadError(LlamaCppRestartableWorkerError):
    """Raised when the subprocess worker is no longer alive before a request."""


class LlamaCppWorkerEOFError(LlamaCppRestartableWorkerError):
    """Raised when the worker exits before returning a response."""


class LlamaCppWorkerBrokenPipeError(LlamaCppRestartableWorkerError):
    """Raised when writing to the worker transport fails."""


class LlamaCppRuntimeStageTimeout(TimeoutError):
    """Raised when a llama_cpp discovery/import stage exceeds its bounded timeout."""

    def __init__(self, stage: str, timeout_seconds: float) -> None:
        self.stage = stage
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{stage} after {timeout_seconds:g}s")


def _format_runtime_stage_timeout(exc: LlamaCppRuntimeStageTimeout) -> str:
    return f"{exc.stage}_timeout after {exc.timeout_seconds:g}s"


def _strip_windows_extended_path_prefix(path_text: str) -> str:
    """Return a path string with Windows extended-length prefixes removed for comparison."""

    if path_text.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path_text[8:]
    if path_text.startswith('\\\\?\\'):
        return path_text[4:]
    return path_text


def _subprocess_safe_path_text(path_text: Any) -> str:
    """Return a subprocess/env-safe path string without Windows extended prefixes."""

    return _strip_windows_extended_path_prefix(str(path_text))


def _sanitize_subprocess_path_env(env: Dict[str, str], pythonpath_entries: list[str]) -> Dict[str, str]:
    """Normalize path bootstrap variables shared by probes and runtime workers."""

    sanitized = dict(env)
    sanitized_entries = [_subprocess_safe_path_text(entry) for entry in pythonpath_entries]
    sanitized['PYTHONPATH'] = os.pathsep.join(sanitized_entries)
    for name in (
        'TOKEN_PLACE_PYTHON_IMPORT_ROOT',
        'TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT',
        'TOKEN_PLACE_DESKTOP_PYTHON_ROOT',
        'TOKEN_PLACE_PROBE_REPO_ROOT',
    ):
        value = sanitized.get(name)
        if value:
            sanitized[name] = _subprocess_safe_path_text(value)
    sanitized.setdefault('PYTHONNOUSERSITE', '1')
    return sanitized


def _canonical_path_for_compare(module_path: Any) -> Optional[str]:
    if not module_path:
        return None
    try:
        path_text = _strip_windows_extended_path_prefix(str(module_path))
        return os.path.normcase(os.path.normpath(os.path.realpath(os.path.abspath(path_text))))
    except (TypeError, ValueError, OSError):
        try:
            return os.path.normcase(os.path.normpath(_strip_windows_extended_path_prefix(str(module_path))))
        except (TypeError, ValueError, OSError):
            return None


def _is_repo_llama_cpp_shim(module_path: Any) -> bool:
    """Return True when llama_cpp resolves to the repository-local shim."""
    if not module_path:
        return False
    module_compare = _canonical_path_for_compare(module_path)
    shim_compare = _canonical_path_for_compare(REPO_LLAMA_CPP_SHIM)
    return bool(module_compare and shim_compare and module_compare == shim_compare)


def _runtime_stage_timeout_seconds() -> float:
    raw_value = os.getenv('TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS', '').strip()
    if not raw_value:
        return DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS


def _sanitize_llama_cpp_import_paths() -> Dict[str, Any]:
    """Keep app imports available while preventing repo-local llama_cpp shim precedence."""

    with _LLAMA_CPP_IMPORT_PATH_LOCK:
        import_root = os.environ.get('TOKEN_PLACE_PYTHON_IMPORT_ROOT', '').strip() or str(REPO_ROOT)
        moved: list[str] = []
        repo_root_compare = _canonical_path_for_compare(REPO_ROOT)
        cwd_compare = _canonical_path_for_compare(Path.cwd())
        shim_entries: list[str] = []
        preserved_entries: list[str] = []

        cwd_text = os.getcwd()
        for entry in sys.path:
            entry_text = str(entry or cwd_text)
            compare = _canonical_path_for_compare(entry_text)
            # Avoid probing every sys.path entry with stat/is_file here: on Windows,
            # offline shares or slow filesystem roots can block before the bounded
            # subprocess discovery/import stages start.  The repository shim path is
            # known, so string-normalized repo/cwd comparisons are sufficient.
            shadows_repo_shim = (
                compare is not None
                and (compare == repo_root_compare or compare == cwd_compare)
            )
            if shadows_repo_shim:
                shim_entries.append(entry)
                moved.append(entry or '<cwd>')
                continue
            preserved_entries.append(entry)

        preserved_entries = _stdlib_safe_path_order(preserved_entries)
        preferred_index = len(preserved_entries)
        for idx, entry in enumerate(preserved_entries):
            normalized = str(entry).replace('\\', '/').lower()
            if 'site-packages' in normalized or 'dist-packages' in normalized:
                preferred_index = idx + 1

        sys.path[:] = (
            preserved_entries[:preferred_index]
            + shim_entries
            + preserved_entries[preferred_index:]
        )
        return {
            'import_root': import_root,
            'deprioritized_entries': moved,
            'sys_path_count': len(sys.path),
        }


def _llama_cpp_probe_sys_path_entries() -> list[str]:
    """Return explicit child probe import paths without implicit cwd shadow entries."""

    cwd_compare = _canonical_path_for_compare(Path.cwd())
    entries: list[str] = []
    seen: set[str] = set()
    for entry in sys.path:
        if not isinstance(entry, str):
            continue
        if entry == '':
            # In a child ``python -c`` process, an empty sys.path entry means that
            # child's cwd.  Do not pass it through because either the repo cwd or a
            # shared temp cwd can shadow the packaged llama_cpp runtime.
            continue
        compare = _canonical_path_for_compare(entry)
        if compare is not None and cwd_compare is not None and compare == cwd_compare:
            continue
        dedupe_key = compare or entry
        if dedupe_key in seen:
            continue
        entries.append(_subprocess_safe_path_text(entry))
        seen.add(dedupe_key)
    return _stdlib_safe_path_order(entries)


def _llama_cpp_probe_env() -> Dict[str, str]:
    """Return subprocess env with an explicit sanitized import path contract."""

    pythonpath_entries = _llama_cpp_probe_sys_path_entries()
    env = _sanitize_subprocess_path_env(os.environ.copy(), pythonpath_entries)
    env['TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH'] = json.dumps(pythonpath_entries)
    return env


def _llama_cpp_path_prefixed_code(user_code: str, path_source: str) -> str:
    """Prefix code so cwd/sys.path[0] cannot shadow the runtime."""

    return (
        "import json as _token_place_json, os as _token_place_os, sys as _token_place_sys\n"
        f"_token_place_probe_path = _token_place_json.loads({path_source})\n"
        "_token_place_cwd = _token_place_os.path.normcase("
        "_token_place_os.path.normpath(_token_place_os.getcwd()))\n"
        "_token_place_existing = []\n"
        "for _token_place_entry in _token_place_sys.path:\n"
        "    if not isinstance(_token_place_entry, str) or not _token_place_entry:\n"
        "        continue\n"
        "    _token_place_compare = _token_place_os.path.normcase("
        "_token_place_os.path.normpath(_token_place_os.path.abspath(_token_place_entry)))\n"
        "    if _token_place_compare == _token_place_cwd:\n"
        "        continue\n"
        "    _token_place_existing.append((_token_place_compare, _token_place_entry))\n"
        "if isinstance(_token_place_probe_path, list):\n"
        "    _token_place_explicit = []\n"
        "    _token_place_seen = set()\n"
        "    for _token_place_entry in _token_place_probe_path:\n"
        "        if not isinstance(_token_place_entry, str) or not _token_place_entry:\n"
        "            continue\n"
        "        _token_place_compare = _token_place_os.path.normcase("
        "_token_place_os.path.normpath(_token_place_os.path.abspath(_token_place_entry)))\n"
        "        if _token_place_compare == _token_place_cwd or _token_place_compare in _token_place_seen:\n"
        "            continue\n"
        "        _token_place_explicit.append(_token_place_entry)\n"
        "        _token_place_seen.add(_token_place_compare)\n"
        "    _token_place_sys.path[:] = _token_place_explicit + ["
        "_token_place_entry for _token_place_compare, _token_place_entry in _token_place_existing "
        "if _token_place_compare not in _token_place_seen]\n"
        "del _token_place_json, _token_place_os, _token_place_probe_path\n"
        "del _token_place_cwd, _token_place_existing, _token_place_seen\n"
        + user_code
    )


def _llama_cpp_stdlib_guard_code() -> str:
    return (
        "import importlib.util as _token_place_importlib_util, sysconfig as _token_place_sysconfig, "
        "os as _token_place_os, sys as _token_place_sys\n"
        "_token_place_stdlib_candidates = [_token_place_sysconfig.get_paths().get('stdlib'), _token_place_sysconfig.get_paths().get('platstdlib'), _token_place_sysconfig.get_config_var('DESTSHARED')]\n"
        "for _token_place_prefix in {_token_place_sys.prefix, getattr(_token_place_sys, 'base_prefix', _token_place_sys.prefix), getattr(_token_place_sys, 'exec_prefix', _token_place_sys.prefix), getattr(_token_place_sys, 'base_exec_prefix', _token_place_sys.prefix)}:\n"
        "    _token_place_stdlib_candidates.append(_token_place_os.path.join(_token_place_prefix, 'lib', f'python{_token_place_sys.version_info.major}.{_token_place_sys.version_info.minor}'))\n"
        "    _token_place_stdlib_candidates.append(_token_place_os.path.join(_token_place_prefix, 'Lib'))\n"
        "_token_place_stdlib_roots = [_token_place_os.path.normcase(_token_place_os.path.normpath(_token_place_os.path.realpath(_token_place_os.path.abspath(_p)))) "
        "for _p in _token_place_stdlib_candidates if _p]\n"
        "def _token_place_is_site(_p):\n"
        "    return 'site-packages' in str(_p).replace('\\\\', '/').lower() or 'dist-packages' in str(_p).replace('\\\\', '/').lower()\n"
        "def _token_place_is_stdlib(_p):\n"
        "    if not _p or _p in ('built-in', 'frozen'):\n"
        "        return True\n"
        "    if _token_place_is_site(_p):\n"
        "        return False\n"
        "    _candidate = _token_place_os.path.normcase(_token_place_os.path.normpath(_token_place_os.path.realpath(_token_place_os.path.abspath(_p))))\n"
        "    for _root in _token_place_stdlib_roots:\n"
        "        try:\n"
        "            if _token_place_os.path.commonpath([_candidate, _root]) == _root:\n"
        "                return True\n"
        "        except Exception:\n"
        "            pass\n"
        "    return False\n"
        "for _token_place_module in ('collections','typing','ctypes','subprocess','json','importlib','pathlib'):\n"
        "    _token_place_spec = _token_place_importlib_util.find_spec(_token_place_module)\n"
        "    _token_place_origin = getattr(_token_place_spec, 'origin', None) if _token_place_spec else None\n"
        "    if _token_place_spec is None or not _token_place_is_stdlib(_token_place_origin):\n"
        "        _token_place_bad_origin = _token_place_origin or '<not found>'\n"
        "        raise ImportError(f'stdlib module {_token_place_module} shadowed by {_token_place_bad_origin}')\n"
        "del _token_place_importlib_util, _token_place_sysconfig, _token_place_os, _token_place_sys, _token_place_stdlib_candidates\n"
        "try:\n"
        "    del _token_place_bad_origin\n"
        "except NameError:\n"
        "    pass\n"
    )

def _llama_cpp_probe_code(user_code: str) -> str:
    """Prefix probe code using the probe sys.path environment contract."""

    return _llama_cpp_path_prefixed_code(
        _llama_cpp_stdlib_guard_code() + user_code,
        "_token_place_os.environ.get('TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH', '[]')",
    )


def _llama_cpp_runtime_worker_env() -> Dict[str, str]:
    """Return subprocess env for killable runtime workers.

    Runtime workers intentionally do not set TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH:
    that variable belongs to discovery/probe subprocesses and historically
    triggered the removed import-watchdog failure mode in packaged desktop
    builds.  The worker still receives the same sanitized import path via an
    embedded JSON literal in its bootstrap code.
    """

    pythonpath_entries = _llama_cpp_probe_sys_path_entries()
    env = _sanitize_subprocess_path_env(os.environ.copy(), pythonpath_entries)
    env.pop('TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH', None)
    return env


def _llama_cpp_runtime_worker_code(user_code: str) -> str:
    """Prefix runtime-worker code with a literal sanitized import path."""

    return _llama_cpp_path_prefixed_code(
        _llama_cpp_stdlib_guard_code() + user_code,
        repr(json.dumps(_llama_cpp_probe_sys_path_entries())),
    )


def _llama_cpp_probe_subprocess_cwd() -> str:
    """Return a cwd that should be ignored by child probe import resolution."""

    # Python prepends the subprocess cwd as sys.path[0] for ``python -c``.  Probe
    # code immediately replaces sys.path with TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH,
    # so neither the repo cwd nor a shared temp cwd can shadow the runtime.
    return os.path.dirname(sys.executable) or os.getcwd()


def _run_llama_cpp_python_probe(stage: str, code: str, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Run a llama_cpp runtime probe in a killable subprocess and return JSON output."""

    timeout = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
    env = _llama_cpp_probe_env()
    started_at = time.perf_counter()
    logger.info(
        "llama_cpp runtime process stage start stage=%s timeout_seconds=%s interpreter=%s",
        stage,
        f"{timeout:g}",
        sys.executable,
    )
    try:
        completed = subprocess.run(
            [sys.executable, '-c', _llama_cpp_probe_code(code)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=_llama_cpp_probe_subprocess_cwd(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        logger.error(
            "llama_cpp runtime process stage timeout stage=%s duration_ms=%s timeout_seconds=%s interpreter=%s",
            stage,
            duration_ms,
            f"{timeout:g}",
            sys.executable,
        )
        raise LlamaCppRuntimeStageTimeout(stage, timeout) from exc

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    if completed.returncode != 0:
        stderr = (completed.stderr or '').strip()
        raise ImportError(
            f"{stage} failed returncode={completed.returncode} stderr={stderr[:500]}"
        )

    stdout = (completed.stdout or '').strip().splitlines()
    diagnostics: Dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout[-1])
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            diagnostics = parsed
    logger.info(
        "llama_cpp runtime process stage complete stage=%s duration_ms=%s module_path=%s",
        stage,
        duration_ms,
        diagnostics.get('module_path') or diagnostics.get('llama_module_path') or 'unknown',
    )
    return diagnostics


def _find_llama_cpp_spec_in_subprocess(*, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    code = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.find_spec('llama_cpp')\n"
        "print(json.dumps({\n"
        "    'module_path': getattr(spec, 'origin', None) if spec else None,\n"
        "    'interpreter': sys.executable,\n"
        "}))\n"
    )
    return _run_llama_cpp_python_probe(
        'llama_cpp_runtime_discovery',
        code,
        timeout_seconds=timeout_seconds,
    )


def _probe_llama_cpp_capabilities_in_subprocess(*, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    code = (
        "import importlib, json, sys\n"
        "llama_cpp = importlib.import_module('llama_cpp')\n"
        "cuda_markers = ('GGML_USE_CUDA', 'GGML_CUDA', 'LLAMA_CUDA', 'GGML_USE_CUBLAS', 'LLAMA_CUBLAS')\n"
        "metal_markers = ('GGML_USE_METAL', 'GGML_METAL', 'LLAMA_METAL')\n"
        "backend = 'cpu'\n"
        "if any(bool(getattr(llama_cpp, marker, False)) for marker in cuda_markers):\n"
        "    backend = 'cuda'\n"
        "elif any(bool(getattr(llama_cpp, marker, False)) for marker in metal_markers):\n"
        "    backend = 'metal'\n"
        "supports_gpu = getattr(llama_cpp, 'llama_supports_gpu_offload', None)\n"
        "gpu_supported = False\n"
        "if callable(supports_gpu):\n"
        "    gpu_supported = bool(supports_gpu())\n"
        "else:\n"
        "    gpu_supported = backend in {'cuda', 'metal'}\n"
        "if gpu_supported and backend == 'cpu':\n"
        "    backend = 'metal' if sys.platform == 'darwin' else 'cuda'\n"
        "print(json.dumps({\n"
        "    'backend': backend,\n"
        "    'gpu_offload_supported': gpu_supported,\n"
        "    'detected_device': backend if gpu_supported else 'cpu',\n"
        "    'interpreter': sys.executable,\n"
        "    'prefix': sys.prefix,\n"
        "    'llama_module_path': getattr(llama_cpp, '__file__', 'unknown'),\n"
        "    'error': None,\n"
        "}))\n"
    )
    return _run_llama_cpp_python_probe(
        'llama_cpp_gpu_probe',
        code,
        timeout_seconds=timeout_seconds,
    )

def _run_llama_cpp_import_watchdog(*, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Validate llama_cpp import in a killable subprocess before parent import."""

    timeout = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
    env = _llama_cpp_probe_env()
    code = (
        "import importlib, json, sys\n"
        "llama_cpp = importlib.import_module('llama_cpp')\n"
        "print(json.dumps({\n"
        "    'module_path': getattr(llama_cpp, '__file__', None),\n"
        "    'interpreter': sys.executable,\n"
        "}))\n"
    )
    started_at = time.perf_counter()
    logger.info(
        "llama_cpp import watchdog start timeout_seconds=%s interpreter=%s",
        f"{timeout:g}",
        sys.executable,
    )
    try:
        completed = subprocess.run(
            [sys.executable, '-c', _llama_cpp_probe_code(code)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=_llama_cpp_probe_subprocess_cwd(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        logger.error(
            "llama_cpp import watchdog timeout duration_ms=%s timeout_seconds=%s interpreter=%s",
            duration_ms,
            f"{timeout:g}",
            sys.executable,
        )
        raise LlamaCppRuntimeStageTimeout('llama_cpp_import', timeout) from exc

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    if completed.returncode != 0:
        stderr = (completed.stderr or '').strip()
        raise ImportError(
            "llama_cpp import watchdog failed "
            f"returncode={completed.returncode} stderr={stderr[:500]}"
        )

    stdout = (completed.stdout or '').strip().splitlines()
    diagnostics: Dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout[-1])
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            diagnostics = parsed
    logger.info(
        "llama_cpp import watchdog complete duration_ms=%s module_path=%s",
        duration_ms,
        diagnostics.get('module_path') or 'unknown',
    )
    return diagnostics


def _llama_cpp_subprocess_inference_timeout_seconds() -> Optional[float]:
    """Return an optional timeout for subprocess-backed inference calls."""

    raw = os.getenv('TOKEN_PLACE_LLAMA_CPP_SUBPROCESS_INFERENCE_TIMEOUT_SECONDS')
    if raw is None or raw.strip() == '':
        # Runtime-stage timeouts bound discovery/import/probe work only.  Inference
        # can legitimately run longer, and API/relay callers already have their
        # own request deadlines, so do not apply the import-stage timeout here.
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _signal_guard_available() -> bool:
    return (
        hasattr(signal, 'SIGALRM')
        and hasattr(signal, 'ITIMER_REAL')
        and hasattr(signal, 'setitimer')
    )


def _import_llama_cpp_subprocess_module(
    *,
    module_path_hint: Any = None,
    timeout_seconds: Optional[float] = None,
    desktop_runtime_probe: Any = None,
):
    """Return a killable subprocess-backed llama_cpp module facade.

    Python's import machinery uses per-module locks.  If a daemon thread wedges
    inside a native ``llama_cpp`` import, later retries in the same bridge
    process can block behind that stuck import lock.  On Windows and desktop
    warm-load background threads, where SIGALRM cannot safely bound the active
    thread, avoid importing ``llama_cpp`` in-process at all and move the native
    import into a subprocess worker that can be terminated on timeout.
    """

    logger.info(
        "llama_cpp parent import skipped; using subprocess runtime facade "
        "module_path_hint=%s interpreter=%s thread=%s",
        module_path_hint or 'unknown',
        sys.executable,
        threading.current_thread().name,
    )
    return _SubprocessLlamaCppModule(
        module_path_hint,
        timeout_seconds=timeout_seconds,
        desktop_runtime_probe=desktop_runtime_probe,
    )


def _import_llama_cpp_in_parent_with_timeout(
    *,
    timeout_seconds: Optional[float] = None,
    module_path_hint: Any = None,
    desktop_runtime_probe: Any = None,
):
    """Import llama_cpp in-process only when the active thread can be bounded.

    Prefer a SIGALRM guard on the main thread when available because it leaves no
    extra worker behind.  Windows and desktop warm-load background threads cannot
    use SIGALRM; spawning an in-process import thread is not recoverable if the
    native import wedges, so those paths return a subprocess-backed facade whose
    worker can be killed and retried without poisoning the bridge process.
    """

    timeout = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
    already_imported = sys.modules.get('llama_cpp')
    if already_imported is not None:
        return already_imported

    if not _signal_guard_available():
        return _import_llama_cpp_subprocess_module(
            module_path_hint=module_path_hint,
            timeout_seconds=timeout,
            desktop_runtime_probe=desktop_runtime_probe,
        )

    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout)

        def _handle_timeout(_signum, _frame):
            raise LlamaCppRuntimeStageTimeout('llama_cpp_import', timeout)

        signal.signal(signal.SIGALRM, _handle_timeout)
        try:
            _assert_critical_stdlib_not_shadowed()
            return importlib.import_module('llama_cpp')
        except TimeoutError as exc:
            if isinstance(exc, LlamaCppRuntimeStageTimeout):
                raise
            raise LlamaCppRuntimeStageTimeout('llama_cpp_import', timeout) from exc
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])

    return _import_llama_cpp_subprocess_module(
        module_path_hint=module_path_hint,
        timeout_seconds=timeout,
        desktop_runtime_probe=desktop_runtime_probe,
    )

def _llama_subprocess_tail(process: subprocess.Popen, name: str) -> str:
    value = getattr(process, name, '')
    if isinstance(value, list):
        text = ''.join(
            line for line in value
            if not str(line).startswith('TOKEN_PLACE_LLAMA_CPP_JSON:')
        )
    else:
        text = str(value or '')
    return text[-2000:].strip()


def _format_llama_subprocess_early_exit_detail(process: subprocess.Popen, *, stage: str) -> str:
    poll = getattr(process, 'poll', None)
    exit_code = poll() if callable(poll) else None
    command = getattr(process, '_token_place_command', None)
    cwd = getattr(process, '_token_place_cwd', None)
    import_root = getattr(process, '_token_place_import_root', None)
    module_path_hint = getattr(process, '_token_place_module_path_hint', None)
    return (
        f"{stage} subprocess exited before JSON handshake; "
        f"exit_code={exit_code if exit_code is not None else 'running'} "
        f"program={sys.executable} command={command or 'unknown'} cwd={cwd or 'unknown'} "
        f"import_root={import_root or 'unknown'} module_path_hint={module_path_hint or 'unknown'} "
        f"stage={stage} stdout_tail={_llama_subprocess_tail(process, '_token_place_stdout_tail') or '<empty>'} "
        f"stderr_tail={_llama_subprocess_tail(process, '_token_place_stderr_tail') or '<empty>'}"
    )


def _llama_subprocess_early_exit_payload(process: subprocess.Popen, *, stage: str) -> str:
    return json.dumps({
        'status': 'transport_error',
        'transport_error': 'eof_before_response',
        'error': _format_llama_subprocess_early_exit_detail(process, stage=stage),
    })


def _read_llama_subprocess_message(
    process: subprocess.Popen,
    *,
    timeout_seconds: Optional[float],
    stage: str,
) -> Dict[str, Any]:
    result_queue: queue.Queue[str] = queue.Queue(maxsize=1)

    def _reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if line.startswith('TOKEN_PLACE_LLAMA_CPP_JSON:'):
                result_queue.put(line.split(':', 1)[1].strip())
                return
            tail = getattr(process, '_token_place_stdout_tail', None)
            if isinstance(tail, list):
                tail.append(line)
                del tail[:-100]
        try:
            process.wait(timeout=0.2)
        except Exception:
            pass
        time.sleep(0.05)
        result_queue.put(_llama_subprocess_early_exit_payload(process, stage=stage))

    reader = threading.Thread(target=_reader, name=f'{stage}_stdout_reader', daemon=True)
    reader.start()
    try:
        if timeout_seconds is None:
            raw_message = result_queue.get()
        else:
            raw_message = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        try:
            process.terminate()
            process.wait(timeout=1)
        except Exception:
            process.kill()
        raise LlamaCppRuntimeStageTimeout(stage, timeout_seconds) from exc
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'{stage} returned malformed JSON') from exc
    if not isinstance(message, dict):
        raise RuntimeError(f'{stage} returned non-object JSON')
    if message.get('status') == 'transport_error':
        raise LlamaCppWorkerEOFError(str(message.get('error') or f'{stage} worker exited before response'))
    if message.get('status') == 'error':
        error = str(message.get('error') or f'{stage} failed')
        if stage == 'llama_cpp_inference' and message.get('request_error'):
            diagnostics = message.get('diagnostics')
            safe_diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
            raise LlamaCppInferenceRequestError(error, diagnostics=safe_diagnostics)
        traceback_text = str(message.get('traceback') or '').strip()
        if traceback_text:
            error = f"{error}; child_traceback_tail={traceback_text[-2000:]}"
        raise RuntimeError(error)
    return message




def _safe_worker_error_code(value: Any) -> str:
    text = str(value or '').strip().lower()
    if isinstance(value, LlamaCppWorkerDeadError):
        return 'worker_dead'
    if isinstance(value, LlamaCppWorkerEOFError):
        return 'worker_eof'
    if isinstance(value, LlamaCppWorkerBrokenPipeError):
        return 'worker_broken_pipe'
    if isinstance(value, LlamaCppInferenceRequestError):
        code = value.diagnostics.get('code') if isinstance(value.diagnostics, dict) else None
        return str(code) if isinstance(code, str) and code else 'inference_request_error'
    if 'broken pipe' in text:
        return 'worker_broken_pipe'
    if 'exited' in text or 'dead' in text or 'liveness' in text:
        return 'worker_dead'
    if 'timeout' in text:
        return 'worker_timeout'
    if text and all(ch.isalnum() or ch in {'_', '-'} for ch in text) and len(text) <= 80:
        return text.replace('-', '_')
    return type(value).__name__ if isinstance(value, BaseException) else 'worker_error'

class _SubprocessLlamaProxy:
    """Minimal llama_cpp.Llama proxy for no-SIGALRM runtimes."""

    def __init__(
        self,
        *args,
        timeout_seconds: Optional[float] = None,
        module_path_hint: Any = None,
        **kwargs,
    ) -> None:
        self._timeout_seconds = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
        self._lock = Lock()
        self._closed = False
        command = [sys.executable, '-u', '-c', _llama_cpp_runtime_worker_code(_LLAMA_CPP_RUNTIME_WORKER_CODE)]
        env = _llama_cpp_runtime_worker_env()
        cwd = _llama_cpp_probe_subprocess_cwd()
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            bufsize=1,
        )
        self._process._token_place_command = [command[0], command[1], '<runtime-worker-code>']  # type: ignore[attr-defined]
        self._process._token_place_cwd = cwd  # type: ignore[attr-defined]
        self._process._token_place_import_root = env.get('TOKEN_PLACE_PYTHON_IMPORT_ROOT', '')  # type: ignore[attr-defined]
        self._process._token_place_module_path_hint = module_path_hint or ''  # type: ignore[attr-defined]
        self._process._token_place_stdout_tail = []  # type: ignore[attr-defined]
        self._process._token_place_stderr_tail = []  # type: ignore[attr-defined]
        self._start_stderr_tail_reader()
        try:
            self._send({'method': '__init__', 'args': args, 'kwargs': kwargs}, check_health=False)
        except (LlamaCppWorkerBrokenPipeError, BrokenPipeError, OSError) as exc:
            raise RuntimeError(
                _format_llama_subprocess_early_exit_detail(self._process, stage='llama_cpp_import')
            ) from exc
        _read_llama_subprocess_message(
            self._process,
            timeout_seconds=self._timeout_seconds,
            stage='llama_cpp_import',
        )

    def _start_stderr_tail_reader(self) -> None:
        def _reader() -> None:
            stderr = self._process.stderr
            if stderr is None:
                return
            for line in stderr:
                tail = getattr(self._process, '_token_place_stderr_tail', None)
                if isinstance(tail, list):
                    tail.append(line)
                    del tail[:-100]

        threading.Thread(target=_reader, name='llama_cpp_stderr_reader', daemon=True).start()

    def _send(self, payload: Dict[str, Any], *, check_health: bool = True) -> None:
        if check_health:
            self.assert_healthy()
        if self._process.stdin is None:
            self._closed = True
            raise LlamaCppWorkerBrokenPipeError('llama_cpp subprocess stdin is unavailable')
        try:
            self._process.stdin.write(json.dumps(payload) + '\n')
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._closed = True
            raise LlamaCppWorkerBrokenPipeError('llama_cpp subprocess transport write failed') from exc

    def is_alive(self) -> bool:
        if self._closed:
            return False
        poll = getattr(self._process, 'poll', None)
        return callable(poll) and poll() is None

    def assert_healthy(self) -> None:
        if not self.is_alive():
            raise LlamaCppWorkerDeadError(
                _format_llama_subprocess_early_exit_detail(self._process, stage='llama_cpp_liveness')
            )

    def create_chat_completion(self, *args, **kwargs):
        stream = bool(kwargs.get('stream', False))
        if stream:
            return self._stream_chat_completion(*args, **kwargs)
        with self._lock:
            self._send({'method': 'create_chat_completion', 'args': args, 'kwargs': kwargs})
            try:
                message = _read_llama_subprocess_message(
                    self._process,
                    timeout_seconds=_llama_cpp_subprocess_inference_timeout_seconds(),
                    stage='llama_cpp_inference',
                )
            except LlamaCppWorkerEOFError:
                self._closed = True
                raise
        return message.get('result')

    def _stream_chat_completion(self, *args, **kwargs):
        with self._lock:
            self._send({'method': 'create_chat_completion', 'args': args, 'kwargs': kwargs})
            while True:
                try:
                    message = _read_llama_subprocess_message(
                        self._process,
                        timeout_seconds=_llama_cpp_subprocess_inference_timeout_seconds(),
                        stage='llama_cpp_inference',
                    )
                except LlamaCppWorkerEOFError:
                    self._closed = True
                    raise
                if message.get('done'):
                    return
                yield message.get('chunk')

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except Exception:
            pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class _SubprocessLlamaCppModule:
    def __init__(
        self,
        module_path: Any,
        *,
        timeout_seconds: Optional[float] = None,
        desktop_runtime_probe: Any = None,
    ) -> None:
        self.__file__ = module_path
        self._timeout_seconds = timeout_seconds
        self.__token_place_subprocess_facade__ = True
        probe = _coerce_desktop_runtime_probe(desktop_runtime_probe)
        backend = str((probe or {}).get('backend') or '').lower()
        self.GGML_USE_CUDA = backend == 'cuda'
        self.GGML_USE_METAL = backend == 'metal'

    def llama_supports_gpu_offload(self) -> bool:
        return bool(self.GGML_USE_CUDA or self.GGML_USE_METAL)

    @property
    def Llama(self):
        timeout_seconds = self._timeout_seconds
        module_path_hint = self.__file__

        class _ConfiguredSubprocessLlama(_SubprocessLlamaProxy):
            def __init__(self, *args, **kwargs):
                super().__init__(
                    *args,
                    timeout_seconds=timeout_seconds,
                    module_path_hint=module_path_hint,
                    **kwargs,
                )

        return _ConfiguredSubprocessLlama


_LLAMA_CPP_RUNTIME_WORKER_CODE = """
import importlib, json, sys, traceback

def _jsonable(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump()
    if hasattr(value, 'dict'):
        return value.dict()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value

def _emit(payload):
    print('TOKEN_PLACE_LLAMA_CPP_JSON:' + json.dumps(_jsonable(payload)), flush=True)

def _safe_request_error(reason, *, request=None, exc=None):
    diagnostics = {'reason': reason}
    if isinstance(request, dict):
        method = request.get('method')
        if method == 'create_chat_completion':
            diagnostics['method'] = method
        elif method is not None:
            diagnostics['method'] = 'unsupported'
        kwargs = request.get('kwargs')
        if isinstance(kwargs, dict):
            diagnostics['stream'] = bool(kwargs.get('stream'))
    if exc is not None:
        diagnostics['exception_type'] = type(exc).__name__
    return {
        'status': 'error',
        'request_error': True,
        'error': 'llama_cpp request failed',
        'diagnostics': diagnostics,
    }

try:
    init_line = sys.stdin.readline()
    if not init_line:
        raise RuntimeError('llama_cpp subprocess missing init payload')
    init_payload = json.loads(init_line)
    llama_cpp = importlib.import_module('llama_cpp')
    llama = llama_cpp.Llama(*init_payload.get('args', []), **init_payload.get('kwargs', {}))
    _emit({'status': 'ok', 'module_path': getattr(llama_cpp, '__file__', None)})
except Exception as exc:
    _emit({'status': 'error', 'error': str(exc), 'traceback': traceback.format_exc()})
    raise SystemExit(1)

for line in sys.stdin:
    request = None
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            _emit(_safe_request_error('malformed_request'))
            continue
        if request.get('method') != 'create_chat_completion':
            _emit(_safe_request_error('unsupported_method', request=request))
            continue
        kwargs = request.get('kwargs', {})
        if not isinstance(kwargs, dict):
            _emit(_safe_request_error('malformed_kwargs', request=request))
            continue
        result = llama.create_chat_completion(*request.get('args', []), **kwargs)
        if kwargs.get('stream'):
            for chunk in result:
                _emit({'status': 'ok', 'chunk': chunk, 'done': False})
            _emit({'status': 'ok', 'done': True})
        else:
            _emit({'status': 'ok', 'result': result})
    except json.JSONDecodeError as exc:
        _emit(_safe_request_error('invalid_json', exc=exc))
    except Exception as exc:
        _emit(_safe_request_error('inference_exception', request=request, exc=exc))
"""



def _llama_cpp_package_parent_from_module_path(module_path: Any) -> Optional[str]:
    """Return the import parent for a probed llama_cpp module path."""

    if not module_path:
        return None
    try:
        module_file = Path(_strip_windows_extended_path_prefix(str(module_path)))
    except (TypeError, ValueError, OSError):
        return None
    if module_file.name == '__init__.py' and module_file.parent.name == 'llama_cpp':
        return str(module_file.parent.parent)
    if module_file.name == 'llama_cpp.py':
        return str(module_file.parent)
    return None


def _clear_llama_cpp_module_namespace(reason: str, *, expected_path: Any = None) -> None:
    """Remove cached llama_cpp modules so a runtime switch cannot reuse stale bindings."""

    stale_names = [
        name for name in sys.modules
        if name == 'llama_cpp' or name.startswith('llama_cpp.')
    ]
    if not stale_names:
        return
    logger.info(
        "llama_cpp clearing cached module namespace reason=%s expected_path=%s module_count=%s",
        reason,
        expected_path or 'unknown',
        len(stale_names),
    )
    for name in stale_names:
        sys.modules.pop(name, None)


def _prepare_llama_cpp_import_from_probe(module_path: Any) -> None:
    """Make a successful desktop probe durable for the real in-process import."""

    if not module_path:
        return
    loaded = sys.modules.get('llama_cpp')
    loaded_path = getattr(loaded, '__file__', None) if loaded is not None else None
    expected_compare = _canonical_path_for_compare(module_path)
    loaded_compare = _canonical_path_for_compare(loaded_path)
    cached_llama_modules = any(
        name == 'llama_cpp' or name.startswith('llama_cpp.')
        for name in sys.modules
    )
    if cached_llama_modules and expected_compare:
        stale_cached_namespace = loaded_compare != expected_compare or any(
            name.startswith('llama_cpp.') for name in sys.modules
        )
        if stale_cached_namespace:
            logger.info(
                "llama_cpp clearing stale imported module before desktop probe reuse "
                "loaded_path=%s expected_path=%s",
                loaded_path or 'unknown',
                module_path,
            )
            _clear_llama_cpp_module_namespace('desktop_probe_path_mismatch', expected_path=module_path)

    package_parent = _llama_cpp_package_parent_from_module_path(module_path)
    if not package_parent:
        return
    package_parent_compare = _canonical_path_for_compare(package_parent)
    if package_parent_compare is None:
        return
    with _LLAMA_CPP_IMPORT_PATH_LOCK:
        retained = []
        for entry in sys.path:
            entry_compare = _canonical_path_for_compare(entry or os.getcwd())
            if entry_compare == package_parent_compare:
                continue
            retained.append(entry)
        retained = _stdlib_safe_path_order(retained)
        insert_index = 0
        if _is_site_packages_path(package_parent):
            for idx, entry in enumerate(retained):
                if _is_stdlib_path(entry):
                    insert_index = idx + 1
        sys.path[:] = retained[:insert_index] + [package_parent] + retained[insert_index:]


def _desktop_runtime_probe_from_env() -> Optional[Dict[str, Any]]:
    raw_probe = os.getenv(DESKTOP_RUNTIME_PROBE_ENV, '').strip()
    if not raw_probe:
        return None
    try:
        parsed = json.loads(raw_probe)
    except json.JSONDecodeError:
        logger.warning(
            "Ignoring invalid desktop runtime probe environment payload env=%s",
            DESKTOP_RUNTIME_PROBE_ENV,
        )
        return None
    return _coerce_desktop_runtime_probe(parsed)


def _effective_desktop_runtime_probe(probe: Any) -> Optional[Dict[str, Any]]:
    return _coerce_desktop_runtime_probe(probe) or _desktop_runtime_probe_from_env()


def _probe_module_path_from_desktop_runtime_probe(probe: Any) -> Optional[str]:
    coerced = _effective_desktop_runtime_probe(probe)
    if coerced is None or coerced.get('error'):
        return None
    module_path = str(coerced.get('llama_module_path') or '').strip()
    if not module_path or module_path in {'missing', 'unknown'}:
        return None
    return module_path


def _import_llama_cpp_runtime(
    *,
    require_real_runtime: bool = True,
    timeout_seconds: Optional[float] = None,
    desktop_runtime_probe: Any = None,
):
    """Import llama_cpp while guarding against the repo-local test shim.

    Packaged desktop runtime setup already imports/probes llama_cpp in the
    selected interpreter to verify CUDA/Metal support.  Reuse that module-path
    diagnostic when present and avoid a second pre-import child watchdog whose
    environment can diverge from the real bridge process.
    """
    path_diagnostics = _sanitize_llama_cpp_import_paths()
    logger.info(
        "llama_cpp import path sanitized import_root=%s deprioritized_entries=%s sys_path_count=%s",
        path_diagnostics.get('import_root'),
        len(path_diagnostics.get('deprioritized_entries', [])),
        path_diagnostics.get('sys_path_count'),
    )

    desktop_runtime_probe = _effective_desktop_runtime_probe(desktop_runtime_probe)
    expected_module_path = _probe_module_path_from_desktop_runtime_probe(desktop_runtime_probe)
    if expected_module_path:
        llama_module_path = expected_module_path
        logger.info(
            "llama_cpp runtime discovery reused desktop probe module_path=%s interpreter=%s",
            llama_module_path,
            sys.executable,
        )
    else:
        spec_diagnostics = _find_llama_cpp_spec_in_subprocess(timeout_seconds=timeout_seconds)
        llama_module_path = spec_diagnostics.get('module_path')
        logger.info(
            "llama_cpp runtime discovery complete module_path=%s interpreter=%s",
            llama_module_path or 'missing',
            sys.executable,
        )

    if require_real_runtime and _is_repo_llama_cpp_shim(llama_module_path):
        _clear_llama_cpp_module_namespace('repo_local_shim_rejected', expected_path=llama_module_path)
        raise ImportError(
            "Refusing to use repository-local llama_cpp.py shim for runtime inference; "
            "install llama-cpp-python and ensure site-packages wins import priority."
        )

    if expected_module_path:
        _prepare_llama_cpp_import_from_probe(expected_module_path)

    logger.info(
        "llama_cpp direct import start module_path_hint=%s interpreter=%s",
        llama_module_path or 'unknown',
        sys.executable,
    )
    llama_cpp = _import_llama_cpp_in_parent_with_timeout(
        timeout_seconds=timeout_seconds,
        module_path_hint=llama_module_path,
        desktop_runtime_probe=desktop_runtime_probe,
    )
    imported_module_path = getattr(llama_cpp, '__file__', None)
    if (
        require_real_runtime
        and expected_module_path
        and imported_module_path
        and _canonical_path_for_compare(expected_module_path)
        != _canonical_path_for_compare(imported_module_path)
    ):
        _clear_llama_cpp_module_namespace('desktop_probe_import_mismatch', expected_path=expected_module_path)
        raise ImportError(
            "Desktop runtime probe module path mismatch; refusing mismatched llama_cpp runtime "
            f"desktop_probe_path={expected_module_path} imported_path={imported_module_path}"
        )
    llama_module_path = imported_module_path
    logger.info(
        "llama_cpp import complete module_path=%s interpreter=%s",
        llama_module_path or 'unknown',
        sys.executable,
    )

    if require_real_runtime and _is_repo_llama_cpp_shim(llama_module_path):
        _clear_llama_cpp_module_namespace('repo_local_shim_rejected', expected_path=llama_module_path)
        raise ImportError(
            "Refusing to use repository-local llama_cpp.py shim for runtime inference; "
            "install llama-cpp-python and ensure site-packages wins import priority."
        )

    return llama_cpp


def detect_llama_runtime_capabilities() -> Dict[str, Any]:
    """Return backend/offload capability details from the installed llama_cpp runtime."""
    try:
        llama_cpp = _import_llama_cpp_runtime(require_real_runtime=True)
    except LlamaCppRuntimeStageTimeout as exc:
        return {
            'backend': 'missing',
            'gpu_offload_supported': False,
            'detected_device': 'none',
            'error': _format_runtime_stage_timeout(exc),
        }
    except Exception as exc:
        return {
            'backend': 'missing',
            'gpu_offload_supported': False,
            'detected_device': 'none',
            'error': str(exc),
        }

    if getattr(llama_cpp, '__token_place_subprocess_facade__', False):
        facade_backend = 'cuda' if getattr(llama_cpp, 'GGML_USE_CUDA', False) else (
            'metal' if getattr(llama_cpp, 'GGML_USE_METAL', False) else 'cpu'
        )
        if facade_backend == 'cpu':
            try:
                probe = _probe_llama_cpp_capabilities_in_subprocess()
                facade_backend = str(probe.get('backend') or facade_backend)
                return {
                    'backend': facade_backend,
                    'gpu_offload_supported': bool(probe.get('gpu_offload_supported', False)),
                    'detected_device': str(probe.get('detected_device') or 'cpu'),
                    'interpreter': str(probe.get('interpreter') or sys.executable),
                    'prefix': str(probe.get('prefix') or sys.prefix),
                    'llama_module_path': str(
                        probe.get('llama_module_path')
                        or getattr(llama_cpp, '__file__', None)
                        or 'unknown'
                    ),
                    'error': probe.get('error'),
                }
            except LlamaCppRuntimeStageTimeout as exc:
                return {
                    'backend': 'missing',
                    'gpu_offload_supported': False,
                    'detected_device': 'none',
                    'interpreter': sys.executable,
                    'prefix': sys.prefix,
                    'llama_module_path': getattr(llama_cpp, '__file__', None) or 'unknown',
                    'error': _format_runtime_stage_timeout(exc),
                }
            except Exception as exc:
                return {
                    'backend': 'missing',
                    'gpu_offload_supported': False,
                    'detected_device': 'none',
                    'interpreter': sys.executable,
                    'prefix': sys.prefix,
                    'llama_module_path': getattr(llama_cpp, '__file__', None) or 'unknown',
                    'error': str(exc),
                }
        return {
            'backend': facade_backend,
            'gpu_offload_supported': True,
            'detected_device': facade_backend,
            'interpreter': sys.executable,
            'prefix': sys.prefix,
            'llama_module_path': getattr(llama_cpp, '__file__', None) or 'unknown',
            'error': None,
        }

    backend = 'cpu'
    cuda_markers = (
        'GGML_USE_CUDA',
        'GGML_CUDA',
        'LLAMA_CUDA',
        'GGML_USE_CUBLAS',
        'LLAMA_CUBLAS',
    )
    metal_markers = (
        'GGML_USE_METAL',
        'GGML_METAL',
        'LLAMA_METAL',
    )
    if any(bool(getattr(llama_cpp, marker, False)) for marker in cuda_markers):
        backend = 'cuda'
    elif any(bool(getattr(llama_cpp, marker, False)) for marker in metal_markers):
        backend = 'metal'

    supports_gpu = getattr(llama_cpp, 'llama_supports_gpu_offload', None)
    gpu_offload_supported = False
    module_path = getattr(llama_cpp, '__file__', None)
    if callable(supports_gpu):
        try:
            if module_path:
                probe = _probe_llama_cpp_capabilities_in_subprocess()
                gpu_offload_supported = bool(probe.get('gpu_offload_supported', False))
                backend = str(probe.get('backend') or backend)
            else:
                gpu_offload_supported = bool(supports_gpu())
        except LlamaCppRuntimeStageTimeout as exc:
            return {
                'backend': 'missing',
                'gpu_offload_supported': False,
                'detected_device': 'none',
                'interpreter': sys.executable,
                'prefix': sys.prefix,
                'llama_module_path': module_path or 'unknown',
                'error': _format_runtime_stage_timeout(exc),
            }
        except Exception:
            gpu_offload_supported = False
    else:
        gpu_offload_supported = backend in {'cuda', 'metal'}

    # Some llama_cpp builds can report runtime GPU offload support via probe
    # without exposing GGML_USE_* backend markers. Preserve prior Linux behavior
    # by inferring CUDA when offload is available and backend markers are absent.
    if gpu_offload_supported and backend == 'cpu':
        backend = 'metal' if sys.platform == 'darwin' else 'cuda'

    return {
        'backend': backend,
        'gpu_offload_supported': gpu_offload_supported,
        'detected_device': backend if gpu_offload_supported else 'cpu',
        'interpreter': sys.executable,
        'prefix': sys.prefix,
        'llama_module_path': module_path or 'unknown',
        'error': None,
    }

def _coerce_desktop_runtime_probe(probe: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(probe, dict):
        return None
    error = str(probe.get('error') or probe.get('fallback_reason') or '').strip()
    action = str(probe.get('runtime_action') or probe.get('action') or '').strip().lower()
    backend = str(probe.get('selected_backend') or probe.get('backend') or '').strip().lower()
    gpu_supported = bool(probe.get('gpu_offload_supported', backend in {'cuda', 'metal'}))
    module_path = str(probe.get('llama_module_path') or '').strip()
    if not backend or backend == 'missing' or action in {'failed', 'unavailable', 'shadowed_repo_llama_cpp'}:
        return None
    if error and action not in {'already_supported', 'metal_already_supported'}:
        return None
    return {
        'backend': backend,
        'gpu_offload_supported': gpu_supported,
        'detected_device': str(probe.get('detected_device') or probe.get('device') or backend),
        'interpreter': str(probe.get('interpreter') or sys.executable),
        'prefix': str(probe.get('prefix') or sys.prefix),
        'llama_module_path': module_path or 'unknown',
        'error': None,
        'runtime_action': action or 'unknown',
    }


def llama_cpp_verbose_logging_enabled() -> bool:
    """Return whether raw llama.cpp verbose logging should be enabled."""

    return (
        os.getenv('TOKEN_PLACE_VERBOSE_LLM_LOGS') == '1'
        or os.getenv('TOKEN_PLACE_VERBOSE_SUBPROCESS_LOGS') == '1'
    )


class ModelManager:
    """
    Manages LLM model downloading, initialization, and inference.
    """
    def __init__(self, config=None):
        """Initialize the ModelManager with configuration."""
        # Import config lazily to avoid circular imports
        if config is None:
            from config import get_config
            config = get_config()

        self.config = config

        # Llama model configuration
        self.file_name = config.get(
            'model.filename', 'Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf'
        )
        self.url = config.get(
            'model.url',
            (
                'https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/'
                'Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf'
            ),
        )
        self.canonical_family_url = config.get(
            'model.canonical_family_url',
            'https://huggingface.co/meta-llama/Meta-Llama-3-8B',
        )
        self.chunk_size_mb = config.get('model.download_chunk_size_mb', 10)
        # Network timeout for model downloads (seconds)
        self.download_timeout = config.get('model.download_timeout', 30)
        self.models_dir = config.get('paths.models_dir')
        self.model_path = os.path.join(self.models_dir, self.file_name)
        self.context_tier = None
        self.context_window_tokens = int(config.get('model.context_size', 8192) or 8192)

        # LLM instance and lock for thread safety
        self.llm = None
        self.llm_lock = Lock()
        self._llm_generation = 0
        self.worker_restart_count = 0
        self.last_worker_error_code: Optional[str] = None
        self.last_worker_exit_code: Optional[int] = None
        self.last_worker_restart_at_ms: Optional[int] = None
        self.worker_state = 'stopped'
        self.last_runtime_init_error: Optional[str] = None

        # Check if mock mode is enabled
        self.use_mock_llm = config.get('model.use_mock', False) or os.getenv('USE_MOCK_LLM') == '1'
        self.default_n_gpu_layers = config.get('model.n_gpu_layers', -1)
        self.hybrid_n_gpu_layers = config.get('model.hybrid_n_gpu_layers', 24)
        self.gpu_headroom_percent = config.get('model.gpu_memory_headroom_percent', 0.1)
        self.enforce_gpu_headroom = config.get('model.enforce_gpu_memory_headroom', True)
        self.requested_compute_mode = 'auto'
        self.desktop_runtime_probe: Optional[Dict[str, Any]] = None
        self._imported_llama_cpp_module_path: Optional[str] = None
        self.last_compute_diagnostics = {
            'requested_mode': 'auto',
            'effective_mode': 'pending',
            'backend_available': 'unknown',
            'backend_selected': 'unknown',
            'backend_used': 'unknown',
            'n_gpu_layers': self.default_n_gpu_layers,
            'fallback_reason': None,
        }

    def _runtime_capabilities(self=None) -> Dict[str, Any]:
        probe = _coerce_desktop_runtime_probe(getattr(self, 'desktop_runtime_probe', None))
        if probe is not None:
            probe_module_path = probe.get('llama_module_path')
            imported_module_path = getattr(self, '_imported_llama_cpp_module_path', None)
            if (
                probe_module_path
                and probe_module_path != 'unknown'
                and imported_module_path
                and imported_module_path != 'unknown'
                and _canonical_path_for_compare(probe_module_path)
                != _canonical_path_for_compare(imported_module_path)
            ):
                if self is not None:
                    self.log_warning(
                        "Desktop runtime probe module path mismatch; refusing to reuse probe "
                        f"desktop_probe_path={probe_module_path} "
                        f"imported_path={imported_module_path}"
                    )
                return {
                    'backend': 'cpu',
                    'gpu_offload_supported': False,
                    'detected_device': 'cpu',
                    'interpreter': sys.executable,
                    'prefix': sys.prefix,
                    'llama_module_path': imported_module_path,
                    'error': 'llama_cpp_runtime_probe_mismatch',
                }
            if self is not None:
                self.log_info(
                    "Using desktop runtime probe diagnostics for compute plan "
                    f"backend={probe['backend']} interpreter={probe['interpreter']} "
                    f"llama_module_path={probe['llama_module_path']} "
                    f"runtime_action={probe.get('runtime_action', 'unknown')}"
                )
            return probe
        return detect_llama_runtime_capabilities()

    def _platform_gpu_backend(self=None) -> Optional[str]:
        runtime = self._runtime_capabilities() if self is not None else detect_llama_runtime_capabilities()
        backend = str(runtime.get('backend') or 'cpu')
        if backend in {'cuda', 'metal'}:
            return backend
        return None

    def _llama_gpu_offload_available(self=None) -> bool:
        runtime = self._runtime_capabilities() if self is not None else detect_llama_runtime_capabilities()
        return bool(runtime.get('gpu_offload_supported', False))

    def _mock_compute_plan(self) -> Dict[str, Any]:
        """Return lightweight diagnostics for mock LLM mode without probing llama_cpp."""

        requested = str(getattr(self, 'requested_compute_mode', 'auto')).lower()
        probe = _coerce_desktop_runtime_probe(getattr(self, 'desktop_runtime_probe', None))
        runtime_error = None
        backend = 'cpu'
        gpu_runtime_supported = False
        if probe is not None:
            runtime_error = probe.get('error')
            backend = str(probe.get('backend') or 'cpu')
            gpu_runtime_supported = bool(probe.get('gpu_offload_supported', False))

        gpu_requested = requested in {'auto', 'gpu', 'hybrid'} and int(self.default_n_gpu_layers) != 0
        fallback_reason = None
        backend_selected = backend if gpu_requested else 'cpu'
        backend_used = backend_selected
        n_gpu_layers = 0
        effective_mode = 'cpu'

        if gpu_requested and backend in {'cuda', 'metal'} and gpu_runtime_supported:
            effective_mode = backend if requested == 'gpu' else (f'hybrid_{backend}' if requested == 'hybrid' else backend)
            n_gpu_layers = -1 if requested in {'auto', 'gpu'} else max(1, int(self.hybrid_n_gpu_layers))
        elif gpu_requested:
            backend_used = 'cpu'
            fallback_reason = runtime_error or 'mock LLM mode does not require llama_cpp GPU probing'
            effective_mode = 'cpu_fallback' if requested != 'cpu' else 'cpu'
        return {
            'requested_mode': requested,
            'effective_mode': effective_mode,
            'backend_available': backend,
            'backend_selected': backend_selected,
            'backend_used': backend_used,
            'n_gpu_layers': n_gpu_layers,
            'fallback_reason': fallback_reason,
            'mock_runtime': True,
        }

    def _resolve_compute_plan(self) -> Dict[str, Any]:
        requested = str(getattr(self, 'requested_compute_mode', 'auto')).lower()
        if requested == 'cpu':
            return {
                'requested_mode': requested,
                'effective_mode': 'cpu',
                'backend_available': 'cpu',
                'backend_selected': 'cpu',
                'backend_used': 'cpu',
                'n_gpu_layers': 0,
                'fallback_reason': None,
            }

        runtime = self._runtime_capabilities()
        runtime_error = str(runtime.get('error') or '')
        backend = str(runtime.get('backend') or 'cpu')
        backend_available = backend if backend in {'cuda', 'metal'} else 'cpu'
        gpu_runtime_supported = bool(runtime.get('gpu_offload_supported', False))
        fallback_reason = None

        if runtime_error.endswith('_timeout') or '_timeout after ' in runtime_error:
            raise RuntimeError(runtime_error)

        if requested == 'auto':
            requested_layers = int(self.default_n_gpu_layers)
            n_gpu_layers = requested_layers
            gpu_requested = n_gpu_layers != 0
            backend_selected = backend_available if gpu_requested else 'cpu'
            if gpu_requested and (
                backend_available == 'cpu' or not gpu_runtime_supported
            ):
                n_gpu_layers = 0
                fallback_reason = (
                    runtime_error or 'no CUDA/Metal backend is supported on this platform'
                    if backend_available == 'cpu'
                    else (
                        f'llama-cpp-python runtime does not expose {backend_available} '
                        'GPU offload support'
                    )
                )
            return {
                'requested_mode': requested,
                'effective_mode': 'cpu_fallback' if fallback_reason else backend_selected,
                'backend_available': backend_available,
                'backend_selected': backend_selected,
                'backend_used': 'cpu' if fallback_reason else backend_selected,
                'n_gpu_layers': n_gpu_layers,
                'fallback_reason': fallback_reason,
            }

        if backend_available == 'cpu':
            fallback_reason = runtime_error or 'no CUDA/Metal backend is supported on this platform'
        elif not gpu_runtime_supported:
            fallback_reason = (
                f'llama-cpp-python runtime does not expose {backend_available} GPU offload support'
            )

        if fallback_reason:
            return {
                'requested_mode': requested,
                'effective_mode': 'cpu_fallback',
                'backend_available': backend_available,
                'backend_selected': backend_available,
                'backend_used': 'cpu',
                'n_gpu_layers': 0,
                'fallback_reason': fallback_reason,
            }

        if requested == 'hybrid':
            n_gpu_layers = max(1, int(self.hybrid_n_gpu_layers))
            return {
                'requested_mode': requested,
                'effective_mode': f'hybrid_{backend_available}',
                'backend_available': backend_available,
                'backend_selected': backend_available,
                'backend_used': backend_available,
                'n_gpu_layers': n_gpu_layers,
                'fallback_reason': None,
            }

        # Explicit ``gpu`` uses full offload when backend support is available.
        return {
            'requested_mode': requested,
            'effective_mode': backend_available,
            'backend_available': backend_available,
            'backend_selected': backend_available,
            'backend_used': backend_available,
            'n_gpu_layers': -1,
            'fallback_reason': None,
        }

    def get_model_artifact_metadata(self) -> Dict[str, Any]:
        """Return runtime model metadata used by server and desktop bridges."""
        file_exists = os.path.exists(self.model_path)
        return {
            'canonical_family_url': self.canonical_family_url,
            'filename': self.file_name,
            'url': self.url,
            'models_dir': self.models_dir,
            'resolved_model_path': self.model_path,
            'exists': file_exists,
            'size_bytes': os.path.getsize(self.model_path) if file_exists else None,
        }

    def _log(self, level: int, message: str, **kwargs) -> None:
        """Log a message when not in production."""
        if self.config.is_production:
            return
        logger.log(level, message, **kwargs)

    def log_info(self, message):
        """Log info only in non-production environments"""
        self._log(logging.INFO, message)

    def log_warning(self, message):
        """Log warnings only in non-production environments"""
        self._log(logging.WARNING, message)

    def log_error(self, message, exc_info=False):
        """Log errors only in non-production environments"""
        self._log(logging.ERROR, message, exc_info=exc_info)

    def create_models_directory(self) -> str:
        """Create the models directory if it doesn't exist."""
        os.makedirs(self.models_dir, exist_ok=True)
        return self.models_dir

    def download_file_in_chunks(self, file_path: str, url: str, chunk_size_mb: int) -> bool:
        """
        Download a file in chunks with progress reporting.

        Args:
            file_path: The path to save the file to
            url: The URL to download from
            chunk_size_mb: The chunk size in MB

        Returns:
            bool: True if download was successful, False otherwise
        """
        chunk_size_bytes = chunk_size_mb * 1024 * 1024  # Convert MB to bytes
        response = None

        try:
            response = requests.get(url, stream=True, timeout=self.download_timeout)
        except requests.Timeout as e:
            self.log_error(f"Error: Download request timed out: {e}")
            return False
        except requests.RequestException as e:
            self.log_error(f"Error: Unable to start download request: {e}")
            return False

        if response.status_code != 200:
            self.log_error(f"Error: Unable to download file, status code {response.status_code}")
            return False

        total_size_in_bytes = int(response.headers.get('content-length', 0))
        if total_size_in_bytes == 0:
            self.log_error("Error: Content-Length header is missing or zero.")
            return False

        total_size_in_mb = total_size_in_bytes / (1024 * 1024)
        progress = 0
        start_time = time.time()
        times = []
        bytes_downloaded = []

        try:
            with open(file_path, 'wb') as file:
                for data in response.iter_content(chunk_size=chunk_size_bytes):
                    if not data:
                        self.log_warning("Warning: Received empty data chunk.")
                        continue

                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())

                    elapsed_time = time.time() - start_time
                    progress += len(data)
                    times.append(elapsed_time)
                    bytes_downloaded.append(progress)

                    # Keep only the last 10 seconds of data
                    times = [t for t in times if elapsed_time - t <= 10]
                    bytes_downloaded = bytes_downloaded[-len(times):]

                    # Calculate speed and estimated time remaining
                    speed = sum(bytes_downloaded) / sum(times) if times else 0
                    eta = (total_size_in_bytes - progress) / speed if speed else 0

                    downloaded_mb = progress / (1024 * 1024)
                    done = int(50 * progress / total_size_in_bytes)
                    if not self.config.is_production:
                        # Progress output is cosmetic and difficult to test
                        print(
                            f'\r[{"=" * done}{" " * (50-done)}] {progress * 100 / total_size_in_bytes:.2f}% ({downloaded_mb:.2f}/{total_size_in_mb:.2f} MB) ETA: {eta:.2f}s',
                            end='\r',
                            file=sys.stderr,
                        )  # pragma: no cover
        except Exception as e:
            self.log_error(f"Error during file download: {e}")
            return False
        finally:
            if response is not None:
                close = getattr(response, 'close', None)
                if callable(close):
                    close()

        if os.path.exists(file_path) and os.path.getsize(file_path) == total_size_in_bytes:
            self.log_info(f"File Size Immediately After Download: {os.path.getsize(file_path)} bytes")
            return True
        else:
            self.log_error("Download failed or file size does not match.")
            return False

    def download_model_if_needed(self) -> bool:
        """
        Download the model file if it doesn't exist.

        Returns:
            bool: True if the model file exists (either already present or successfully downloaded),
                 False if download failed
        """
        self.create_models_directory()

        if not os.path.exists(self.model_path):
            self.log_info(f"Downloading {self.file_name}...")
            if self.download_file_in_chunks(self.model_path, self.url, self.chunk_size_mb):
                self.log_info("Download completed!")
                return True
            else:
                self.log_error("Download failed or file is empty.")
                return False
        else:
            self.log_info(f"Model file {self.file_name} already exists.")
            return True

    def get_llm_instance(self):
        """
        Gets the Llama instance, initializing it if necessary (thread-safe),
        or returns a mock if USE_MOCK_LLM is set.

        Returns:
            A Llama instance or a MagicMock object
        """
        # Check if mocking is enabled via configuration
        if self.use_mock_llm:
            self.log_info("Using Mock LLM instance based on USE_MOCK_LLM configuration.")
            self.last_compute_diagnostics = self._mock_compute_plan()
            mock_llama_instance = MagicMock()
            mock_response = {
                'choices': [
                    {
                        'message': {
                            'role': 'assistant',
                            # Make the mock response more specific for easier debugging
                            'content': 'Mock Response: The capital of France is Paris.'
                        }
                    }
                ]
            }
            mock_llama_instance.create_chat_completion.return_value = mock_response
            return mock_llama_instance

        # Quick check without lock
        if self.llm is None:
            # Acquire lock only if we might need to initialize
            with self.llm_lock:
                # Double-check after acquiring lock
                if self.llm is None:
                    if not os.path.exists(self.model_path):
                        self.log_error(f"Error: Model file {self.model_path} does not exist. LLM not initialized.")
                        return None
                    else:
                        try:
                            self.last_runtime_init_error = None
                            # Dynamically import Llama only when needed
                            self.log_info("Locating llama_cpp runtime for model initialization...")
                            llama_cpp = _import_llama_cpp_runtime(
                                require_real_runtime=True,
                                desktop_runtime_probe=getattr(self, 'desktop_runtime_probe', None),
                            )
                            self._imported_llama_cpp_module_path = getattr(llama_cpp, '__file__', None)
                            self.log_info(
                                "llama_cpp runtime located "
                                f"module_path={self._imported_llama_cpp_module_path or 'unknown'}"
                            )
                            Llama = llama_cpp.Llama

                            self.log_info("Selecting compute plan for model initialization...")
                            compute_plan = self._resolve_compute_plan()
                            self.log_info(
                                "Selected compute plan for model initialization "
                                f"requested={compute_plan['requested_mode']} "
                                f"backend_selected={compute_plan['backend_selected']} "
                                f"n_gpu_layers={compute_plan['n_gpu_layers']}"
                            )
                            n_gpu_layers = int(compute_plan['n_gpu_layers'])
                            if self.enforce_gpu_headroom and n_gpu_layers != 0:
                                try:
                                    model_size = os.path.getsize(self.model_path)
                                except OSError:
                                    model_size = None
                                if model_size:
                                    if not resource_monitor.can_allocate_gpu_memory(
                                        model_size,
                                        headroom_percent=self.gpu_headroom_percent,
                                    ):
                                        self.log_warning(
                                            "Insufficient GPU memory headroom detected; falling back "
                                            "to CPU inference for this model."
                                        )
                                        n_gpu_layers = 0
                                        compute_plan['effective_mode'] = 'cpu_fallback'
                                        compute_plan['backend_used'] = 'cpu'
                                        compute_plan['fallback_reason'] = (
                                            'insufficient GPU memory headroom for safe offload'
                                        )

                            self.log_info(f"About to instantiate Llama model from {self.model_path}...")
                            self.log_info(f"Llama init started for {self.model_path}.")
                            self.llm = Llama(
                                model_path=self.model_path,
                                n_gpu_layers=n_gpu_layers,
                                n_ctx=self.active_context_window_tokens(),
                                chat_format=self.config.get('model.chat_format', 'llama-3'),
                                verbose=llama_cpp_verbose_logging_enabled(),
                            )
                            compute_plan['n_gpu_layers'] = n_gpu_layers
                            compute_plan['kv_cache_device'] = (
                                compute_plan['backend_used']
                                if n_gpu_layers < 0
                                else ('cpu' if n_gpu_layers == 0 else 'partial')
                            )
                            compute_plan['offloaded_layers'] = (
                                n_gpu_layers if n_gpu_layers >= 0 else 'all_supported_layers'
                            )
                            compute_plan['device_backend'] = compute_plan['backend_used']
                            compute_plan['device_name'] = 'unreported'
                            self.last_compute_diagnostics = compute_plan
                            if compute_plan['requested_mode'] == 'cpu':
                                runtime_identity = {
                                    'interpreter': sys.executable,
                                    'llama_module_path': self._imported_llama_cpp_module_path or 'unknown',
                                }
                            else:
                                runtime_identity = self._runtime_capabilities()
                            self.log_info(
                                "compute_runtime "
                                f"requested={compute_plan['requested_mode']} "
                                f"effective={compute_plan['effective_mode']} "
                                f"backend_available={compute_plan['backend_available']} "
                                f"backend_used={compute_plan['backend_used']} "
                                f"device_backend={compute_plan['device_backend']} "
                                f"device_name={compute_plan['device_name']} "
                                f"offloaded_layers={compute_plan['offloaded_layers']} "
                                f"kv_cache={compute_plan['kv_cache_device']} "
                                f"interpreter={runtime_identity.get('interpreter', sys.executable)} "
                                f"llama_module_path={runtime_identity.get('llama_module_path', 'unknown')} "
                                f"fallback_reason={compute_plan['fallback_reason'] or 'none'} "
                                f"context_window_tokens={self.active_context_window_tokens()}"
                            )
                            self.worker_state = 'ready'
                            self.last_worker_error_code = None
                            self.last_worker_exit_code = None
                            self.log_info("desktop.llama_cpp_worker.initialized event=worker_initialization worker_state=ready worker_generation=%s worker_restart_count=%s" % (self._llm_generation, self.worker_restart_count))
                            self.log_info("Llama init completed successfully.")
                            self.log_info("Llama model initialized successfully.")
                        except Exception as e:
                            self.last_runtime_init_error = str(e)
                            if isinstance(e, LlamaCppRuntimeStageTimeout):
                                self.last_runtime_init_error = _format_runtime_stage_timeout(e)
                            self.worker_state = 'failed'
                            self.last_worker_error_code = _safe_worker_error_code(e)
                            self.log_error(
                                f"Failed to initialize Llama model: {self.last_runtime_init_error}",
                                exc_info=True,
                            )
                            return None

        return self.llm

    def set_context_profile(self, profile_id: str, context_window_tokens: int) -> None:
        """Set the single static context profile before Llama construction."""
        if self.llm is not None:
            raise RuntimeError("context profile cannot change after Llama initialization")
        self.context_tier = profile_id
        self.context_window_tokens = int(context_window_tokens)
        set_config = getattr(self.config, 'set', None)
        if callable(set_config):
            set_config('model.context_size', self.context_window_tokens)
        elif hasattr(self.config, 'config') and isinstance(self.config.config, dict):
            self.config.config.setdefault('model', {})['context_size'] = self.context_window_tokens

    def active_context_window_tokens(self) -> int:
        return int(self.config.get('model.context_size', self.context_window_tokens) or self.context_window_tokens)

    def _close_llm_proxy(self, llm: Any) -> None:
        close = getattr(llm, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                self.log_warning("Failed to close old llama.cpp worker during invalidation")

    def _llm_is_usable(self, llm: Any) -> bool:
        is_alive = getattr(llm, 'is_alive', None)
        if callable(is_alive):
            try:
                return bool(is_alive())
            except Exception:
                return False
        return llm is not None

    def _worker_exit_code(self, llm: Any) -> Optional[int]:
        process = getattr(llm, '_process', None)
        poll = getattr(process, 'poll', None)
        if callable(poll):
            try:
                code = poll()
                return int(code) if code is not None else None
            except Exception:
                return None
        return None

    def worker_lifecycle_status(self) -> Dict[str, Any]:
        with self.llm_lock:
            llm = self.llm
            state = self.worker_state
            generation = self._llm_generation
            restart_count = self.worker_restart_count
            last_error_code = self.last_worker_error_code
            last_exit_code = self.last_worker_exit_code
            last_restart_at_ms = self.last_worker_restart_at_ms
        alive = self._llm_is_usable(llm) if llm is not None else False
        if llm is None and state not in {'failed', 'recovering', 'starting'}:
            state = 'stopped'
        return {
            'worker_state': state,
            'worker_generation': generation,
            'worker_restart_count': restart_count,
            'worker_alive': alive,
            'last_worker_error_code': last_error_code,
            'last_worker_exit_code': last_exit_code,
            'last_worker_restart_at_ms': last_restart_at_ms,
        }

    def _invalidate_llm_if_current(self, failed_llm: Any, error: Any = None) -> int:
        dead_worker_log_message: Optional[str] = None
        with self.llm_lock:
            if self.llm is failed_llm:
                self.last_worker_exit_code = self._worker_exit_code(failed_llm)
                self.last_worker_error_code = _safe_worker_error_code(error) if error is not None else 'worker_dead'
                self.worker_state = 'recovering'
                self.worker_restart_count += 1
                self.last_worker_restart_at_ms = int(time.time() * 1000)
                dead_worker_log_message = (
                    "desktop.llama_cpp_worker.dead_detected event=dead_worker_detection "
                    f"safe_error_code={self.last_worker_error_code} worker_generation={self._llm_generation} "
                    f"worker_restart_count={self.worker_restart_count} exit_code={self.last_worker_exit_code}"
                )
                self._close_llm_proxy(self.llm)
                self.llm = None
                self._llm_generation += 1
            generation = self._llm_generation
        if dead_worker_log_message is not None:
            self.log_warning(dead_worker_log_message)
        return generation

    def _ensure_replacement_llm(self, observed_generation: int) -> Any:
        replacement_attempt_log_message: Optional[str] = None
        with self.llm_lock:
            if self.llm is not None and self._llm_is_usable(self.llm):
                return self.llm
            if self.llm is not None:
                self._close_llm_proxy(self.llm)
                self.llm = None
            if self._llm_generation == observed_generation:
                self._llm_generation += 1
            self.worker_state = 'recovering'
            replacement_attempt_log_message = "desktop.llama_cpp_worker.replacement_attempt event=replacement_attempt worker_generation=%s worker_restart_count=%s" % (self._llm_generation, self.worker_restart_count)
            # Release llm_lock before get_llm_instance() because it initializes under
            # the same non-reentrant lock and still serializes creation internally.
        if replacement_attempt_log_message is not None:
            self.log_warning(replacement_attempt_log_message)
        return self.get_llm_instance()

    def create_chat_completion_with_recovery(self, *args, **kwargs):
        """Create a completion, replacing a dead subprocess worker at most once.

        Recovery is only supported for non-streaming completions. Passing
        ``stream=True`` returns a generator before transport IO can raise
        restartable worker errors, so callers that need recovery must use
        ``stream=False``.
        """
        if kwargs.get('stream', False):
            raise ValueError(
                'create_chat_completion_with_recovery does not support stream=True; '
                'use create_chat_completion directly for streaming.'
            )

        llm_instance = self.get_llm_instance()
        if llm_instance is None:
            raise RuntimeError('LLM runtime is unavailable')
        with self.llm_lock:
            observed_generation = self._llm_generation
        create_chat_completion = getattr(llm_instance, 'create_chat_completion', None)
        if not callable(create_chat_completion):
            raise RuntimeError('LLM runtime missing create_chat_completion')
        try:
            return create_chat_completion(*args, **kwargs)
        except LlamaCppInferenceRequestError as exc:
            safe_error_code = _safe_worker_error_code(exc)
            with self.llm_lock:
                self.last_worker_error_code = safe_error_code
                generation = self._llm_generation
                restart_count = self.worker_restart_count
            self.log_warning("desktop.llama_cpp_worker.request_failure event=request_scoped_inference_failure safe_error_code=%s worker_generation=%s worker_restart_count=%s" % (safe_error_code, generation, restart_count))
            raise
        except LlamaCppRestartableWorkerError as exc:
            self._invalidate_llm_if_current(llm_instance, exc)

        replacement = self._ensure_replacement_llm(observed_generation)
        if replacement is None:
            raise RuntimeError('LLM runtime replacement failed')
        replacement_create = getattr(replacement, 'create_chat_completion', None)
        if not callable(replacement_create):
            raise RuntimeError('LLM replacement runtime missing create_chat_completion')
        try:
            result = replacement_create(*args, **kwargs)
            with self.llm_lock:
                self.worker_state = 'ready'
                self.last_worker_error_code = None
                self.last_worker_exit_code = None
                generation = self._llm_generation
                restart_count = self.worker_restart_count
            self.log_info("desktop.llama_cpp_worker.replacement_result event=replacement_result result=succeeded worker_generation=%s worker_restart_count=%s" % (generation, restart_count))
            return result
        except LlamaCppRestartableWorkerError as exc:
            self._invalidate_llm_if_current(replacement, exc)
            with self.llm_lock:
                self.worker_state = 'failed'
                safe_error_code = self.last_worker_error_code
                generation = self._llm_generation
                restart_count = self.worker_restart_count
                exit_code = self.last_worker_exit_code
            self.log_error("desktop.llama_cpp_worker.terminal_failure event=terminal_failure safe_error_code=%s worker_generation=%s worker_restart_count=%s exit_code=%s" % (safe_error_code, generation, restart_count, exit_code))
            raise RuntimeError('LLM runtime replacement failed after one restart attempt') from exc

    def llama_cpp_get_response(self, chat_history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        Get a response from the LLM given a chat history.

        Args:
            chat_history: List of chat messages with 'role' and 'content' keys

        Returns:
            Updated chat history with the model's response appended
        """
        llm_instance = self.get_llm_instance()
        if llm_instance is None:
            # Return a simple error response if LLM initialization failed
            chat_history.append({
                "role": "assistant",
                "content": "Sorry, I'm having trouble accessing my language capabilities right now."
            })
            return chat_history

        try:
            # If we got a list of chat messages, convert it to the format expected by the Llama API
            self.log_info(
                f"Generating response for chat history with {len(chat_history)} messages"
            )

            # Create a copy of the chat history to avoid modifying the original
            result = chat_history.copy()

            # Generate the completion using streaming mode so callers receive
            # incremental deltas when available from llama.cpp.
            completion = llm_instance.create_chat_completion(
                messages=chat_history,
                max_tokens=self.config.get('model.max_tokens', 512),
                temperature=self.config.get('model.temperature', 0.7),
                top_p=self.config.get('model.top_p', 0.9),
                stop=self.config.get('model.stop_tokens', []),
                stream=True,
            )

            # Extract the assistant's response, supporting both streaming
            # generators and non-streaming fallbacks returned by mocks.
            if isinstance(completion, dict):
                assistant_message = completion['choices'][0]['message']
            else:
                assistant_message = self._consume_streaming_completion(completion)

                if not assistant_message.get('content') and not assistant_message.get('tool_calls'):
                    # Some mocks (and older llama.cpp builds) ignore the stream
                    # flag and yield empty deltas. Fall back to the traditional
                    # non-streaming request so we still provide a reply.
                    self.log_warning(
                        "Streaming completion returned no content; falling back to non-streaming mode."
                    )
                    completion = llm_instance.create_chat_completion(
                        messages=chat_history,
                        max_tokens=self.config.get('model.max_tokens', 512),
                        temperature=self.config.get('model.temperature', 0.7),
                        top_p=self.config.get('model.top_p', 0.9),
                        stop=self.config.get('model.stop_tokens', []),
                        stream=False,
                    )
                    assistant_message = completion['choices'][0]['message']
            self.log_info("Generated assistant response")

            # Append the assistant's response to the chat history
            result.append(assistant_message)

            return result

        except Exception as e:
            self.log_error(f"Error during LLM inference: {e}", exc_info=True)
            # Return an error message
            chat_history.append({
                "role": "assistant",
                "content": "I'm sorry, I encountered an error while processing your request."
            })
            return chat_history

    @staticmethod
    def _normalize_stream_chunk(chunk: Any) -> Dict[str, Any]:
        """Normalise llama.cpp streaming chunk objects into dictionaries."""
        if isinstance(chunk, dict):
            return chunk

        for attr in ('to_dict', 'model_dump', 'dict'):
            handler = getattr(chunk, attr, None)
            if callable(handler):
                try:
                    normalised = handler()
                except TypeError:
                    continue
                if isinstance(normalised, dict):
                    return normalised

        if hasattr(chunk, '__dict__') and isinstance(chunk.__dict__, dict):
            return chunk.__dict__

        return {}

    @staticmethod
    def _merge_tool_call_deltas(existing: List[Dict[str, Any]], deltas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merge streamed tool_call deltas into a stable structure."""
        for delta in deltas or []:
            index = delta.get('index')
            if index is None:
                index = len(existing)

            while len(existing) <= index:
                existing.append({
                    'id': None,
                    'type': None,
                    'function': {
                        'name': None,
                        'arguments': '',
                    },
                })

            target = existing[index]

            if delta.get('id'):
                target['id'] = delta['id']
            if delta.get('type'):
                target['type'] = delta['type']

            function_delta = delta.get('function') or {}
            if function_delta.get('name'):
                target.setdefault('function', {})['name'] = function_delta['name']
            if 'arguments' in function_delta and function_delta['arguments']:
                target.setdefault('function', {}).setdefault('arguments', '')
                target['function']['arguments'] += function_delta['arguments']

        return existing

    def _consume_streaming_completion(self, completion: Iterable[Any]) -> Dict[str, Any]:
        """Aggregate streamed llama.cpp chunks into a single assistant message."""
        role = 'assistant'
        content_segments: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        for raw_chunk in completion:
            chunk = self._normalize_stream_chunk(raw_chunk)
            if not chunk:
                continue

            choices = chunk.get('choices') or []
            if not choices:
                continue

            choice = choices[0] or {}
            delta = choice.get('delta') or {}
            if not isinstance(delta, dict):
                continue

            role = delta.get('role') or role

            content_piece = delta.get('content')
            if content_piece:
                content_segments.append(content_piece)

            if delta.get('tool_calls'):
                tool_calls = self._merge_tool_call_deltas(tool_calls, delta['tool_calls'])

            finish_reason = choice.get('finish_reason')
            if finish_reason:
                break

        message: Dict[str, Any] = {
            'role': role,
            'content': ''.join(content_segments),
        }

        cleaned_tool_calls = []
        for call in tool_calls:
            function_meta = call.get('function') or {}
            cleaned_call = {
                key: value for key, value in call.items() if key in {'id', 'type'} and value
            }
            if function_meta:
                cleaned_function = {}
                if function_meta.get('name'):
                    cleaned_function['name'] = function_meta['name']
                if function_meta.get('arguments'):
                    cleaned_function['arguments'] = function_meta['arguments']
                if cleaned_function:
                    cleaned_call['function'] = cleaned_function

            if cleaned_call:
                cleaned_tool_calls.append(cleaned_call)

        if cleaned_tool_calls:
            message['tool_calls'] = cleaned_tool_calls

        return message

# Create a singleton instance
# Delay instantiation to avoid circular imports
model_manager = None

def get_model_manager():
    """Get the global model manager instance, creating it if necessary."""
    global model_manager
    if model_manager is None:
        model_manager = ModelManager()
    return model_manager
