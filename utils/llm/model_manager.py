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
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock
from typing import Dict, List, Any, Optional, Union, Iterable

from utils.system import resource_monitor

# Configure logging
logger = logging.getLogger('model_manager')
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_LLAMA_CPP_SHIM = (REPO_ROOT / 'llama_cpp.py').resolve()
DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS = 30.0
_LLAMA_CPP_IMPORT_PATH_LOCK = Lock()


class LlamaCppRuntimeStageTimeout(TimeoutError):
    """Raised when a llama_cpp discovery/import stage exceeds its bounded timeout."""

    def __init__(self, stage: str, timeout_seconds: float) -> None:
        self.stage = stage
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{stage} after {timeout_seconds:g}s")


def _strip_windows_extended_path_prefix(path_text: str) -> str:
    """Return a path string with Windows extended-length prefixes removed for comparison."""

    if path_text.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path_text[8:]
    if path_text.startswith('\\\\?\\'):
        return path_text[4:]
    return path_text


def _canonical_path_for_compare(module_path: Any) -> Optional[str]:
    if not module_path:
        return None
    try:
        path_text = _strip_windows_extended_path_prefix(str(module_path))
        return os.path.normcase(os.path.normpath(os.path.abspath(path_text)))
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


def _run_llama_cpp_python_probe(stage: str, code: str, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Run a llama_cpp runtime probe in a killable subprocess and return JSON output."""

    timeout = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
    pythonpath_entries = [str(entry or Path.cwd()) for entry in sys.path]
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(pythonpath_entries)
    started_at = time.perf_counter()
    logger.info(
        "llama_cpp runtime process stage start stage=%s timeout_seconds=%s interpreter=%s",
        stage,
        f"{timeout:g}",
        sys.executable,
    )
    try:
        completed = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
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
    pythonpath_entries = [str(entry or Path.cwd()) for entry in sys.path]
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(pythonpath_entries)
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
            [sys.executable, '-c', code],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
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


def _import_llama_cpp_runtime(*, require_real_runtime: bool = True, timeout_seconds: Optional[float] = None):
    """Import llama_cpp while guarding against the repo-local test shim with bounded stages."""
    path_diagnostics = _sanitize_llama_cpp_import_paths()
    logger.info(
        "llama_cpp import path sanitized import_root=%s deprioritized_entries=%s sys_path_count=%s",
        path_diagnostics.get('import_root'),
        len(path_diagnostics.get('deprioritized_entries', [])),
        path_diagnostics.get('sys_path_count'),
    )

    spec_diagnostics = _find_llama_cpp_spec_in_subprocess(timeout_seconds=timeout_seconds)
    llama_module_path = spec_diagnostics.get('module_path')
    logger.info(
        "llama_cpp runtime discovery complete module_path=%s interpreter=%s",
        llama_module_path or 'missing',
        sys.executable,
    )

    if require_real_runtime and _is_repo_llama_cpp_shim(llama_module_path):
        sys.modules.pop('llama_cpp', None)
        raise ImportError(
            "Refusing to use repository-local llama_cpp.py shim for runtime inference; "
            "install llama-cpp-python and ensure site-packages wins import priority."
        )

    _run_llama_cpp_import_watchdog(timeout_seconds=timeout_seconds)
    llama_cpp = importlib.import_module('llama_cpp')
    llama_module_path = getattr(llama_cpp, '__file__', None)
    logger.info(
        "llama_cpp import complete module_path=%s interpreter=%s",
        llama_module_path or 'unknown',
        sys.executable,
    )

    if require_real_runtime and _is_repo_llama_cpp_shim(llama_module_path):
        sys.modules.pop('llama_cpp', None)
        raise ImportError(
            "Refusing to use repository-local llama_cpp.py shim for runtime inference; "
            "install llama-cpp-python and ensure site-packages wins import priority."
        )

    return llama_cpp


def detect_llama_runtime_capabilities() -> Dict[str, Any]:
    """Return backend/offload capability details from the installed llama_cpp runtime."""
    try:
        llama_cpp = _import_llama_cpp_runtime(require_real_runtime=True)
    except Exception as exc:
        return {
            'backend': 'missing',
            'gpu_offload_supported': False,
            'detected_device': 'none',
            'error': str(exc),
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

        # LLM instance and lock for thread safety
        self.llm = None
        self.llm_lock = Lock()
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

    def _resolve_compute_plan(self) -> Dict[str, Any]:
        requested = str(getattr(self, 'requested_compute_mode', 'auto')).lower()
        runtime = self._runtime_capabilities()
        runtime_error = str(runtime.get('error') or '')
        backend = str(runtime.get('backend') or 'cpu')
        backend_available = backend if backend in {'cuda', 'metal'} else 'cpu'
        gpu_runtime_supported = bool(runtime.get('gpu_offload_supported', False))
        fallback_reason = None

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

        if requested == 'cpu':
            return {
                'requested_mode': requested,
                'effective_mode': 'cpu',
                'backend_available': backend_available,
                'backend_selected': 'cpu',
                'backend_used': 'cpu',
                'n_gpu_layers': 0,
                'fallback_reason': None,
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
            self.last_compute_diagnostics = self._resolve_compute_plan()
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
                            llama_cpp = _import_llama_cpp_runtime(require_real_runtime=True)
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
                                n_ctx=self.config.get('model.context_size', 8192),
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
                                f"fallback_reason={compute_plan['fallback_reason'] or 'none'}"
                            )
                            self.log_info("Llama init completed successfully.")
                            self.log_info("Llama model initialized successfully.")
                        except Exception as e:
                            self.last_runtime_init_error = str(e)
                            if isinstance(e, LlamaCppRuntimeStageTimeout):
                                self.last_runtime_init_error = f"{e.stage}_timeout after {e.timeout_seconds:g}s"
                            self.log_error(
                                f"Failed to initialize Llama model: {self.last_runtime_init_error}",
                                exc_info=True,
                            )
                            return None

        return self.llm

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
