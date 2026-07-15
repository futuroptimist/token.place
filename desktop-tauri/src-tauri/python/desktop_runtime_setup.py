"""Desktop runtime bootstrap for llama-cpp backend availability."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform as platform_module
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from desktop_gpu_packaging import (
    LlamaCppInstallPlan,
    LLAMA_CPP_CPU_WHEEL_INDEX_URL,
    LLAMA_CPP_METAL_WHEEL_INDEX_URL,
    LLAMA_CPP_PYPI_INDEX_URL,
    backend_probe_satisfies_install_plan,
    llama_cpp_install_plan_fallbacks,
    llama_cpp_requirement_spec,
)

LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS = (
    "type_k",
    "type_v",
    "flash_attn",
    "offload_kqv",
    "n_batch",
    "n_ubatch",
    "rope_scaling_type",
    "yarn_ext_factor",
    "yarn_attn_factor",
    "yarn_beta_fast",
    "yarn_beta_slow",
    "yarn_orig_ctx",
    "rope_freq_base",
    "rope_freq_scale",
)


def _strip_windows_extended_path_prefix(path_text: str) -> str:
    if path_text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_text[8:]
    if path_text.startswith("\\\\?\\"):
        return path_text[4:]
    return path_text


def _safe_resolve_path(path_text: str | Path) -> Path:
    return Path(_strip_windows_extended_path_prefix(str(path_text))).resolve()


def _canonical_llama_module_identity_path(path_text: str | Path) -> str:
    return os.path.normcase(
        os.path.normpath(str(_safe_resolve_path(path_text))).replace(os.sep, "/")
    )


def llama_module_identity_from_path(path_text):
    if not path_text:
        return None
    try:
        canonical = _canonical_llama_module_identity_path(path_text)
    except (TypeError, ValueError, OSError):
        return None
    digest = hashlib.sha256(
        ("token.place.llama_module_identity.v1\0" + canonical).encode("utf-8", "surrogatepass")
    ).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class RuntimeBootstrapPolicy:
    platform: str
    arch: str
    expected_backend: Optional[str]
    bootstrap_supported: bool
    bootstrap_reason: str


@dataclass(frozen=True)
class RuntimeProbe:
    backend: str
    gpu_offload_supported: bool
    detected_device: str
    interpreter: str
    prefix: str
    llama_module_path: str
    error: Optional[str] = None
    python_version: str = "unknown"
    base_prefix: str = "unknown"
    dependency_target: str = "unknown"
    pip_version: str = "unknown"
    llama_cpp_python_version: str = "unknown"
    yarn_rope_supported: bool = False
    yarn_resolver_source: str = "unsupported"
    rope_scaling_type_supported: bool = False
    yarn_ext_factor_supported: bool = False
    rope_freq_scale_supported: bool = False
    yarn_orig_ctx_supported: bool = False
    constructor_kwarg_support: Dict[str, bool] = field(default_factory=dict)
    constructor_has_var_kwargs: bool = False
    constructor_signature_inspectable: bool = False
    qwen_64k_yarn_support: str = "unsupported"
    yarn_enum_value: Optional[int] = None
    q8_kv_cache_type_value: Optional[int] = None
    q4_kv_cache_type_value: Optional[int] = None
    f16_kv_cache_type_value: Optional[int] = None
    capability_source: str = "desktop_runtime_setup_probe"


GPU_MODES = frozenset({"auto", "gpu", "hybrid"})
GPU_RUNTIME_FATAL_ACTIONS = frozenset(
    {
        "failed",
        "installed_cpu_fallback",
        "shadowed_repo_llama_cpp",
        "unavailable",
        "metal_install_failed",
        "metal_cpu_fallback",
    }
)
PIP_INSTALL_TIMEOUT_SECONDS = 300
DEFAULT_PIP_SOURCE_BUILD_TIMEOUT_SECONDS = 1800


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


PIP_SOURCE_BUILD_TIMEOUT_SECONDS = _parse_positive_int_env(
    "TOKEN_PLACE_DESKTOP_PIP_SOURCE_BUILD_TIMEOUT_SECONDS",
    DEFAULT_PIP_SOURCE_BUILD_TIMEOUT_SECONDS,
)
INSTALL_ERROR_SUMMARY_MAX_LEN = 512
INSTALL_LOG_TAIL_MAX_CHARS = 2000
REEXEC_GUARD_ENV = "TOKEN_PLACE_DESKTOP_RUNTIME_REEXECED"
DISABLE_BOOTSTRAP_ENV = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP"
ENABLE_BOOTSTRAP_ENV = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP"
RUNTIME_PROBE_ENV = "TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON"
SOURCE_REPAIR_COOLDOWN_SECONDS = 24 * 60 * 60
_PROCESS_SYS_PATH = sys.path
_PROCESS_PYTHON_VERSION = sys.version.split()[0]

_PROBE_SNIPPET = r"""
import json
import os
import sys
LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS = (
    "type_k",
    "type_v",
    "flash_attn",
    "offload_kqv",
    "n_batch",
    "n_ubatch",
    "rope_scaling_type",
    "yarn_ext_factor",
    "yarn_attn_factor",
    "yarn_beta_fast",
    "yarn_beta_slow",
    "yarn_orig_ctx",
    "rope_freq_base",
    "rope_freq_scale",
)


def _strip_windows_extended_path_prefix(path_text):
    prefix = chr(92) + chr(92) + "?" + chr(92)
    unc_prefix = prefix + "UNC" + chr(92)
    if path_text.startswith(unc_prefix):
        return chr(92) + chr(92) + path_text[8:]
    if path_text.startswith(prefix):
        return path_text[4:]
    return path_text

def _safe_resolve_path_text(path_text):
    return os.path.realpath(os.path.abspath(_strip_windows_extended_path_prefix(str(path_text))))


def llama_module_identity_from_path(path_text):
    if not path_text:
        return None
    try:
        canonical = os.path.normcase(
            os.path.normpath(_safe_resolve_path_text(str(path_text))).replace(os.sep, "/")
        )
    except (TypeError, ValueError, OSError):
        return None
    digest = hashlib.sha256(
        ("token.place.llama_module_identity.v1\0" + canonical).encode("utf-8", "surrogatepass")
    ).hexdigest()
    return f"sha256:{digest}"

python_root = os.environ.get("TOKEN_PLACE_DESKTOP_PYTHON_ROOT", "").strip()
if python_root and python_root not in sys.path:
    sys.path.insert(0, python_root)

dependency_target = os.environ.get("TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET", "").strip()
if dependency_target and dependency_target not in sys.path:
    sys.path.insert(0, dependency_target)

bootstrap_script = os.environ.get("TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT", "").strip()
if bootstrap_script:
    from path_bootstrap import ensure_runtime_import_paths

    ensure_runtime_import_paths(bootstrap_script, avoid_llama_cpp_shadowing=True)

import importlib
import importlib.metadata
import importlib.util
import inspect
from pathlib import Path

repo_root = Path(_safe_resolve_path_text(os.environ.get("TOKEN_PLACE_PROBE_REPO_ROOT", os.getcwd())))

repo_root_resolved = _safe_resolve_path_text(repo_root)
sanitized = []
for entry in sys.path:
    resolved_entry = _safe_resolve_path_text(entry or ".")
    if resolved_entry == repo_root_resolved:
        continue
    sanitized.append(entry)
sys.path[:] = sanitized

try:
    llama_spec = importlib.util.find_spec("llama_cpp")
    llama_module_path = getattr(llama_spec, "origin", None)
    repo_shim = str(_safe_resolve_path_text(repo_root / "llama_cpp.py"))
    if llama_module_path and str(_safe_resolve_path_text(llama_module_path)) == repo_shim:
        raise ImportError(
            "Refusing to use repository-local llama_cpp.py shim for runtime inference; "
            "install llama-cpp-python and ensure site-packages wins import priority."
        )

    llama_cpp = importlib.import_module("llama_cpp")
    llama_module_path = getattr(llama_cpp, "__file__", llama_module_path or "unknown")
    if llama_module_path and str(_safe_resolve_path_text(llama_module_path)) == repo_shim:
        raise ImportError(
            "Refusing to use repository-local llama_cpp.py shim for runtime inference; "
            "install llama-cpp-python and ensure site-packages wins import priority."
        )

    backend = "cpu"
    cuda_markers = ("GGML_USE_CUDA", "GGML_CUDA", "LLAMA_CUDA", "GGML_USE_CUBLAS", "LLAMA_CUBLAS")
    metal_markers = ("GGML_USE_METAL", "GGML_METAL", "LLAMA_METAL")
    if any(bool(getattr(llama_cpp, marker, False)) for marker in cuda_markers):
        backend = "cuda"
    elif any(bool(getattr(llama_cpp, marker, False)) for marker in metal_markers):
        backend = "metal"

    supports_gpu = getattr(llama_cpp, "llama_supports_gpu_offload", None)
    gpu_offload_supported = False
    if callable(supports_gpu):
        try:
            gpu_offload_supported = bool(supports_gpu())
        except Exception:
            gpu_offload_supported = False
    else:
        gpu_offload_supported = backend in {"cuda", "metal"}

    if gpu_offload_supported and backend == "cpu":
        backend = "metal" if sys.platform == "darwin" else "cuda"

    Llama = getattr(llama_cpp, "Llama", None)
    constructor_signature_inspectable = False
    constructor_has_var_kwargs = False
    constructor_kwarg_support = {name: False for name in LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    try:
        params = inspect.signature(getattr(Llama, "__init__", Llama)).parameters
        constructor_signature_inspectable = True
        constructor_has_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        constructor_kwarg_support = {
            name: bool(name in params or constructor_has_var_kwargs)
            for name in LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS
        }
    except (TypeError, ValueError):
        pass

    rope_scaling_type_supported = constructor_kwarg_support.get("rope_scaling_type", False)
    yarn_ext_factor_supported = constructor_kwarg_support.get("yarn_ext_factor", False)
    rope_freq_scale_supported = constructor_kwarg_support.get("rope_freq_scale", False)
    yarn_orig_ctx_supported = constructor_kwarg_support.get("yarn_orig_ctx", False)

    yarn_enum_value = getattr(llama_cpp, "LLAMA_ROPE_SCALING_TYPE_YARN", None)
    if yarn_enum_value is not None:
        yarn_resolver_source = "top_level_enum"
    else:
        yarn_enum_value = getattr(getattr(llama_cpp, "llama_cpp", None), "LLAMA_ROPE_SCALING_TYPE_YARN", None)
        if yarn_enum_value is not None:
            yarn_resolver_source = "nested_enum"
        elif rope_scaling_type_supported:
            yarn_enum_value = 2
            yarn_resolver_source = "numeric_fallback"
        else:
            yarn_resolver_source = "unsupported"
            yarn_enum_value = None
    if not isinstance(yarn_enum_value, int) or isinstance(yarn_enum_value, bool):
        yarn_enum_value = None
    def _resolve_type_constant(*names):
        for container in (llama_cpp, getattr(llama_cpp, "llama_cpp", None)):
            if container is None:
                continue
            for name in names:
                value = getattr(container, name, None)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
        return None
    q8_kv_cache_type_value = _resolve_type_constant("GGML_TYPE_Q8_0", "LLAMA_TYPE_Q8_0")
    q4_kv_cache_type_value = _resolve_type_constant("GGML_TYPE_Q4_0", "LLAMA_TYPE_Q4_0")
    f16_kv_cache_type_value = _resolve_type_constant("GGML_TYPE_F16", "LLAMA_TYPE_F16")
    yarn_rope_supported = bool(
        yarn_enum_value is not None
        and rope_scaling_type_supported
        and rope_freq_scale_supported
        and yarn_orig_ctx_supported
    )
    if yarn_rope_supported:
        qwen_64k_yarn_support = "supported"
    elif not constructor_signature_inspectable:
        qwen_64k_yarn_support = "unknown"
    else:
        qwen_64k_yarn_support = "unsupported"
    llama_cpp_python_version = getattr(llama_cpp, "__version__", None)
    if not llama_cpp_python_version:
        try:
            llama_cpp_python_version = importlib.metadata.version("llama-cpp-python")
        except importlib.metadata.PackageNotFoundError:
            llama_cpp_python_version = "unknown"

    payload = {
        "backend": backend,
        "gpu_offload_supported": gpu_offload_supported,
        "detected_device": backend if gpu_offload_supported else "cpu",
        "interpreter": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": getattr(sys, "base_prefix", sys.prefix),
        "python_version": sys.version.split()[0],
        "dependency_target": dependency_target or "unknown",
        "pip_version": os.environ.get("TOKEN_PLACE_DESKTOP_PIP_VERSION", "unknown"),
        "llama_module_path": llama_module_path or "unknown",
        "llama_cpp_python_version": llama_cpp_python_version,
        "yarn_rope_supported": yarn_rope_supported,
        "yarn_resolver_source": yarn_resolver_source,
        "rope_scaling_type_supported": rope_scaling_type_supported,
        "yarn_ext_factor_supported": yarn_ext_factor_supported,
        "rope_freq_scale_supported": rope_freq_scale_supported,
        "yarn_orig_ctx_supported": yarn_orig_ctx_supported,
        "constructor_kwarg_support": constructor_kwarg_support,
        "constructor_has_var_kwargs": constructor_has_var_kwargs,
        "constructor_signature_inspectable": constructor_signature_inspectable,
        "qwen_64k_yarn_support": qwen_64k_yarn_support,
        "yarn_enum_value": yarn_enum_value,
        "q8_kv_cache_type_value": q8_kv_cache_type_value,
        "q4_kv_cache_type_value": q4_kv_cache_type_value,
        "f16_kv_cache_type_value": f16_kv_cache_type_value,
        "capability_source": "desktop_runtime_setup_probe",
        "error": None,
    }
except Exception as exc:
    payload = {
        "backend": "missing",
        "gpu_offload_supported": False,
        "detected_device": "none",
        "interpreter": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": getattr(sys, "base_prefix", sys.prefix),
        "python_version": sys.version.split()[0],
        "dependency_target": dependency_target or "unknown",
        "pip_version": os.environ.get("TOKEN_PLACE_DESKTOP_PIP_VERSION", "unknown"),
        "llama_module_path": "missing",
        "llama_cpp_python_version": "unknown",
        "yarn_rope_supported": False,
        "yarn_resolver_source": "unsupported",
        "rope_scaling_type_supported": False,
        "yarn_ext_factor_supported": False,
        "rope_freq_scale_supported": False,
        "yarn_orig_ctx_supported": False,
        "constructor_kwarg_support": {},
        "constructor_has_var_kwargs": False,
        "constructor_signature_inspectable": False,
        "qwen_64k_yarn_support": "unsupported",
        "yarn_enum_value": None,
        "q8_kv_cache_type_value": None,
        "q4_kv_cache_type_value": None,
        "f16_kv_cache_type_value": None,
        "capability_source": "desktop_runtime_setup_probe",
        "error": str(exc),
    }

print(json.dumps(payload))
""".strip()


def _resolve_runtime_root(*, repo_root: Optional[Path] = None) -> Path:
    if repo_root is not None:
        return _safe_resolve_path(repo_root)

    explicit_root = os.environ.get("TOKEN_PLACE_PYTHON_IMPORT_ROOT", "").strip()
    if explicit_root:
        candidate = _safe_resolve_path(explicit_root)
        if (candidate / "utils").is_dir() or (candidate / "config.py").is_file():
            return candidate
        print(
            "TOKEN_PLACE_PYTHON_IMPORT_ROOT was set but does not look like a runtime root "
            f"({candidate}); expected utils/ or config.py. Falling back to auto-detection.",
            file=sys.stderr,
        )

    script_path = _safe_resolve_path(__file__)
    for candidate in script_path.parents:
        if (candidate / "utils").is_dir() or (candidate / "config.py").is_file():
            return candidate

    parents = script_path.parents
    if len(parents) > 3:
        return parents[3]
    if parents:
        return parents[-1]
    return script_path.parent


def _python_version_text() -> str:
    version = getattr(sys, "version", "")
    return version.split()[0] if version else _PROCESS_PYTHON_VERSION


def _pip_version_summary() -> str:
    try:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    except Exception as exc:
        return f"unavailable ({exc})"
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return f"unavailable (returncode={result.returncode}; {output})"
    return output or "available"


def _probe_llama_runtime(*, runtime_root: Optional[Path] = None) -> RuntimeProbe:
    repo_root = _safe_resolve_path(_resolve_runtime_root(repo_root=runtime_root))
    python_root = _safe_resolve_path(__file__).parent
    dependency_target, _dependency_target_error = _resolve_desktop_dependency_target(repo_root)
    dependency_target_env = str(dependency_target) if dependency_target is not None else ""
    dependency_target_text = dependency_target_env or "unknown"
    pip_version = _pip_version_summary()
    cmd = [sys.executable, "-c", _PROBE_SNIPPET]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    pythonpath_entries = [str(python_root)]
    if dependency_target is not None:
        pythonpath_entries.append(str(dependency_target))
    pythonpath_entries.append(str(repo_root))
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    env["TOKEN_PLACE_DESKTOP_PYTHON_ROOT"] = str(python_root)
    env["TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT"] = str(_safe_resolve_path(__file__))
    if dependency_target_env:
        env["TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET"] = dependency_target_env
    else:
        env.pop("TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET", None)
    env["TOKEN_PLACE_DESKTOP_PIP_VERSION"] = pip_version
    env["TOKEN_PLACE_PROBE_REPO_ROOT"] = str(repo_root)
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_root),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path="missing",
            error="desktop_runtime_probe_timeout_after_30s",
            python_version=_python_version_text(),
            base_prefix=getattr(sys, "base_prefix", sys.prefix),
            dependency_target=dependency_target_text,
            pip_version=pip_version,
        )
    except Exception as exc:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path="missing",
            error=str(exc),
            python_version=_python_version_text(),
            base_prefix=getattr(sys, "base_prefix", sys.prefix),
            dependency_target=dependency_target_text,
            pip_version=pip_version,
        )

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0 or not stdout:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path="missing",
            error=stderr or f"probe subprocess failed with return code {result.returncode}",
            python_version=_python_version_text(),
            base_prefix=getattr(sys, "base_prefix", sys.prefix),
            dependency_target=dependency_target_text,
            pip_version=pip_version,
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = {
            "backend": "missing",
            "gpu_offload_supported": False,
            "detected_device": "none",
            "interpreter": sys.executable,
            "prefix": sys.prefix,
            "llama_module_path": "missing",
            "error": stderr or "probe parse failure",
        }

    return RuntimeProbe(
        backend=str(payload.get("backend", "cpu")),
        gpu_offload_supported=payload.get("gpu_offload_supported") is True,
        detected_device=str(payload.get("detected_device", "cpu")),
        interpreter=str(payload.get("interpreter", sys.executable)),
        prefix=str(payload.get("prefix", sys.prefix)),
        llama_module_path=str(payload.get("llama_module_path", "missing")),
        error=payload.get("error"),
        python_version=str(payload.get("python_version", _python_version_text())),
        base_prefix=str(payload.get("base_prefix", getattr(sys, "base_prefix", sys.prefix))),
        dependency_target=str(payload.get("dependency_target", dependency_target_text)),
        pip_version=str(payload.get("pip_version", pip_version)),
        llama_cpp_python_version=str(payload.get("llama_cpp_python_version", "unknown")),
        yarn_rope_supported=payload.get("yarn_rope_supported") is True,
        yarn_resolver_source=str(payload.get("yarn_resolver_source", "unsupported")),
        rope_scaling_type_supported=payload.get("rope_scaling_type_supported") is True,
        yarn_ext_factor_supported=payload.get("yarn_ext_factor_supported") is True,
        rope_freq_scale_supported=payload.get("rope_freq_scale_supported") is True,
        yarn_orig_ctx_supported=payload.get("yarn_orig_ctx_supported") is True,
        constructor_kwarg_support={
            str(name): value
            for name, value in (payload.get("constructor_kwarg_support") or {}).items()
            if isinstance(name, str)
            and name in LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS
            and isinstance(value, bool)
        } if isinstance(payload.get("constructor_kwarg_support"), dict) else {},
        constructor_has_var_kwargs=payload.get("constructor_has_var_kwargs") is True,
        constructor_signature_inspectable=payload.get("constructor_signature_inspectable") is True,
        qwen_64k_yarn_support=str(payload.get("qwen_64k_yarn_support", "unsupported")),
        yarn_enum_value=payload.get("yarn_enum_value") if isinstance(payload.get("yarn_enum_value"), int) and not isinstance(payload.get("yarn_enum_value"), bool) else None,
        q8_kv_cache_type_value=payload.get("q8_kv_cache_type_value") if isinstance(payload.get("q8_kv_cache_type_value"), int) and not isinstance(payload.get("q8_kv_cache_type_value"), bool) else None,
        q4_kv_cache_type_value=payload.get("q4_kv_cache_type_value") if isinstance(payload.get("q4_kv_cache_type_value"), int) and not isinstance(payload.get("q4_kv_cache_type_value"), bool) else None,
        f16_kv_cache_type_value=payload.get("f16_kv_cache_type_value") if isinstance(payload.get("f16_kv_cache_type_value"), int) and not isinstance(payload.get("f16_kv_cache_type_value"), bool) else None,
        capability_source=str(payload.get("capability_source", "desktop_runtime_setup_probe")),
    )


def _probe_runtime(runtime_root: Path) -> RuntimeProbe:
    try:
        return _probe_llama_runtime(runtime_root=runtime_root)
    except TypeError as exc:
        message = str(exc)
        if "unexpected keyword argument" in message and "runtime_root" in message:
            # Backward-compatible path for tests that monkeypatch _probe_llama_runtime
            # with callables that do not accept keyword arguments.
            return _probe_llama_runtime()
        raise


def _tail_text(raw: str, *, limit: int = INSTALL_LOG_TAIL_MAX_CHARS) -> str:
    text = (raw or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _command_summary(cmd: list[str]) -> str:
    return " ".join(str(part) for part in cmd)


def _run_pip_install(
    cmd: list[str],
    env: dict[str, str],
    *,
    timeout_seconds: int = PIP_INSTALL_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    try:
        install = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _tail_text(exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        stderr = _tail_text(exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        return (
            False,
            f"pip install timed out after {timeout_seconds}s; command={_command_summary(cmd)}; "
            f"stdout_tail={stdout or 'empty'}; stderr_tail={stderr or 'empty'}",
        )

    stdout_tail = _tail_text(install.stdout or "")
    stderr_tail = _tail_text(install.stderr or "")
    detail = (
        f"command={_command_summary(cmd)}; returncode={install.returncode}; "
        f"stdout_tail={stdout_tail or 'empty'}; stderr_tail={stderr_tail or 'empty'}"
    )
    if install.returncode == 0:
        return True, detail

    return False, detail


def _source_build_repair(
    requirements_path: Path, backend: str, dependency_target: Optional[Path] = None
) -> tuple[bool, str]:
    env = os.environ.copy()
    backend_label = backend.upper()
    if backend == "cuda":
        env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
    elif backend == "metal":
        env["CMAKE_ARGS"] = "-DGGML_METAL=on -DGGML_NATIVE=off"
    else:
        env.pop("CMAKE_ARGS", None)
    env["FORCE_CMAKE"] = "1"
    package_spec = "llama-cpp-python"
    metadata_warning = ""
    try:
        package_spec = llama_cpp_requirement_spec(requirements_path)
    except FileNotFoundError:
        metadata_warning = (
            f"requirements file not found at {requirements_path}; "
            "falling back to unpinned llama-cpp-python source reinstall"
        )
    except (OSError, ValueError) as exc:
        metadata_warning = (
            "unable to resolve pinned llama-cpp-python requirement from "
            f"{requirements_path}: {exc}; falling back to unpinned source reinstall"
        )
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--force-reinstall",
        "--no-cache-dir",
    ]
    if dependency_target is not None:
        dependency_target_text = str(dependency_target)
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            [dependency_target_text, existing_pythonpath] if existing_pythonpath else [dependency_target_text]
        )
        cmd.extend(["--target", dependency_target_text])
    cmd.extend([
        "--no-binary",
        "llama-cpp-python",
        "--verbose",
        package_spec,
    ])
    ok, output = _run_pip_install(cmd, env, timeout_seconds=PIP_SOURCE_BUILD_TIMEOUT_SECONDS)
    if not metadata_warning:
        return ok, output

    detail = (output or "").strip()
    if not detail:
        return ok, metadata_warning

    lines = detail.splitlines()
    lines[-1] = f"{lines[-1]} ({metadata_warning}; attempted {backend_label} source build)"
    return ok, "\n".join(lines)


def _windows_cuda_source_repair(
    requirements_path: Path, dependency_target: Optional[Path] = None
) -> tuple[bool, str]:
    return _source_build_repair(requirements_path, "cuda", dependency_target)


def _run_windows_cuda_source_repair(
    requirements_path: Path, dependency_target: Optional[Path]
) -> tuple[bool, str]:
    try:
        return _windows_cuda_source_repair(requirements_path, dependency_target)
    except TypeError as exc:
        message = str(exc)
        if "positional" in message or "argument" in message:
            # Backward-compatible path for tests that monkeypatch
            # _windows_cuda_source_repair with callables that only accept the
            # historical requirements_path argument.
            return _windows_cuda_source_repair(requirements_path)
        raise


def _bounded_error_field(value: str) -> str:
    text = (value or "").strip()
    if len(text) <= INSTALL_ERROR_SUMMARY_MAX_LEN:
        return text
    return "..." + text[-(INSTALL_ERROR_SUMMARY_MAX_LEN - 3):]


def _extract_install_detail_value(raw: str, field: str) -> str:
    marker = f"{field}="
    start = raw.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    next_field = raw.find("; ", start)
    value = raw[start:] if next_field < 0 else raw[start:next_field]
    value = value.strip()
    if not value or value == "empty":
        return ""
    return value


def _extract_install_detail(raw: str, field: str) -> str:
    value = _extract_install_detail_value(raw, field)
    if not value:
        return ""
    return f"{field}={_bounded_error_field(value)}"


def _install_diagnostics_payload(
    raw: str, *, backend: str = "", cmake_args: str = ""
) -> Dict[str, str]:
    text = (raw or "").strip()
    payload: Dict[str, str] = {}
    command = _extract_install_detail_value(text, "command")
    stdout_tail = _extract_install_detail_value(text, "stdout_tail")
    stderr_tail = _extract_install_detail_value(text, "stderr_tail")
    if command:
        payload["install_command_summary"] = _bounded_error_field(command)
    if stdout_tail:
        payload["pip_stdout_tail"] = _bounded_error_field(stdout_tail)
    if stderr_tail:
        payload["pip_stderr_tail"] = _bounded_error_field(stderr_tail)
    if backend:
        payload["install_backend"] = backend
    if cmake_args:
        payload["cmake_args"] = cmake_args
    return payload


def _summarize_install_error(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "install failed"
    for field in ("stderr_tail", "stdout_tail"):
        detail = _extract_install_detail(text, field)
        if detail:
            return detail
    line = text.splitlines()[-1].strip()
    if len(line) <= INSTALL_ERROR_SUMMARY_MAX_LEN:
        return line
    return "..." + line[-(INSTALL_ERROR_SUMMARY_MAX_LEN - 3):]


def _runtime_probe_is_importable(probe: RuntimeProbe) -> bool:
    return probe.backend != "missing" and not probe.error


def _prepend_dependency_target_to_sys_path(runtime_root: Path) -> tuple[Optional[Path], Optional[str]]:
    dependency_target, dependency_target_error = _resolve_desktop_dependency_target(runtime_root)
    if dependency_target is not None:
        dependency_target_text = str(dependency_target)
        active_sys_path = getattr(sys, "path", _PROCESS_SYS_PATH)
        if dependency_target_text not in active_sys_path:
            active_sys_path.insert(0, dependency_target_text)
    return dependency_target, dependency_target_error


def _probe_result_payload(probe: RuntimeProbe) -> Dict[str, Any]:
    return {
        "detected_device": probe.detected_device or "cpu",
        "interpreter": probe.interpreter,
        "python_version": probe.python_version,
        "prefix": probe.prefix,
        "base_prefix": probe.base_prefix,
        "interpreter_prefix": probe.prefix,
        "dependency_target": probe.dependency_target,
        "pip_version": probe.pip_version,
        "llama_cpp_python_version": probe.llama_cpp_python_version,
        "llama_module_path_present": bool(probe.llama_module_path and probe.llama_module_path not in {"missing", "unknown"}),
        "llama_module_identity": llama_module_identity_from_path(probe.llama_module_path),
        "backend": probe.backend,
        "gpu_offload_supported": probe.gpu_offload_supported,
        "constructor_kwarg_support": dict(probe.constructor_kwarg_support),
        "constructor_has_var_kwargs": probe.constructor_has_var_kwargs,
        "constructor_signature_inspectable": probe.constructor_signature_inspectable,
        "qwen_64k_yarn_support": probe.qwen_64k_yarn_support,
        "yarn_enum_value": probe.yarn_enum_value,
        "q8_kv_cache_type_value": probe.q8_kv_cache_type_value,
        "q4_kv_cache_type_value": probe.q4_kv_cache_type_value,
        "f16_kv_cache_type_value": probe.f16_kv_cache_type_value,
        "capability_source": probe.capability_source,
        "yarn_rope_supported": probe.yarn_rope_supported,
        "yarn_resolver_source": probe.yarn_resolver_source,
        "rope_scaling_type_supported": probe.rope_scaling_type_supported,
        "yarn_ext_factor_supported": probe.yarn_ext_factor_supported,
        "rope_freq_scale_supported": probe.rope_freq_scale_supported,
        "yarn_orig_ctx_supported": probe.yarn_orig_ctx_supported,
    }


def _required_llama_cpp_spec(requirements_path: Path) -> tuple[str, str]:
    package_spec = llama_cpp_requirement_spec(requirements_path)
    _, required_version = package_spec.split("==", 1)
    return package_spec, required_version.strip()


def _llama_cpp_version_matches(installed: str, package_spec: str) -> str:
    version_text = str(installed or "").strip()
    if not version_text or version_text == "unknown":
        return "unknown"
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ModuleNotFoundError:
        return "unknown"
    try:
        spec_text = package_spec.split("llama-cpp-python", 1)[1]
        return "match" if Version(version_text) in SpecifierSet(spec_text) else "mismatch"
    except (InvalidVersion, ValueError):
        return "mismatch"


def _version_payload(probe: RuntimeProbe, required_version: str, package_spec: str) -> Dict[str, str]:
    return {
        "llama_cpp_python_installed_version": probe.llama_cpp_python_version or "unknown",
        "llama_cpp_python_required_version": required_version or "unknown",
        "llama_cpp_python_version_match": _llama_cpp_version_matches(
            probe.llama_cpp_python_version, package_spec
        ),
    }


def _qwen_64k_runtime_repair_failed_reason(probe: RuntimeProbe, *, version_match: str = "unknown") -> str:
    return (
        "Qwen 64K requires YaRN/RoPE support in llama-cpp-python; runtime repair failed; "
        f"resolver={probe.yarn_resolver_source}; version={probe.llama_cpp_python_version}; "
        f"version_match={version_match}; "
        f"rope_scaling_type_supported={probe.rope_scaling_type_supported}; "
        f"yarn_ext_factor_supported={probe.yarn_ext_factor_supported}; "
        f"rope_freq_scale_supported={probe.rope_freq_scale_supported}; "
        f"yarn_orig_ctx_supported={probe.yarn_orig_ctx_supported}"
    )


def _repo_llama_shim_path(repo_root: Path) -> str:
    return str((repo_root / "llama_cpp.py").resolve())


def _is_repo_local_llama_module(module_path: str, repo_root: Path) -> bool:
    module = str(module_path or "").strip()
    if not module:
        return False
    try:
        resolved_module = os.path.normcase(str(Path(module).resolve())).casefold()
        repo_shim = os.path.normcase(_repo_llama_shim_path(repo_root)).casefold()
        return resolved_module == repo_shim
    except OSError:
        return False


def _runtime_state_path() -> Path:
    return Path.home() / ".token_place_desktop_runtime_state.json"


def _load_runtime_state() -> dict:
    path = _runtime_state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_runtime_state(state: dict) -> None:
    path = _runtime_state_path()
    try:
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        return


def _should_attempt_source_repair() -> tuple[bool, str]:
    state = _load_runtime_state()
    failures = state.get("source_repair_failures", {})
    entry = failures.get(sys.executable, {})
    last_failed_at = float(entry.get("last_failed_at", 0))
    now = time.time()
    if now - last_failed_at >= SOURCE_REPAIR_COOLDOWN_SECONDS:
        return True, ""
    retry_in_seconds = int(SOURCE_REPAIR_COOLDOWN_SECONDS - (now - last_failed_at))
    return False, entry.get("reason") or f"source repair cooldown active ({retry_in_seconds}s remaining)"


def _record_source_repair_failure(reason: str) -> None:
    state = _load_runtime_state()
    failures = state.setdefault("source_repair_failures", {})
    failures[sys.executable] = {
        "last_failed_at": time.time(),
        "reason": reason,
    }
    _save_runtime_state(state)


def _clear_source_repair_failure() -> None:
    state = _load_runtime_state()
    failures = state.get("source_repair_failures", {})
    if sys.executable in failures:
        failures.pop(sys.executable, None)
        _save_runtime_state(state)


def maybe_reexec_for_runtime_refresh(
    runtime_setup: Dict[str, str], *, allow_reexec: bool = True
) -> None:
    if not allow_reexec:
        return
    if runtime_setup.get("runtime_action") not in {"installed_cuda_reexec", "installed_metal_reexec"}:
        return
    if os.environ.get(REEXEC_GUARD_ENV) == "1":
        return
    env = os.environ.copy()
    env[REEXEC_GUARD_ENV] = "1"
    try:
        os.execve(sys.executable, [sys.executable, *sys.argv], env)
    except OSError:
        return


def desktop_gpu_runtime_failure_message(mode: str, runtime_setup: Dict[str, str]) -> str | None:
    """Return fatal runtime bootstrap diagnostics for desktop GPU launch modes."""

    selected_mode = (mode or "auto").strip().lower()
    if selected_mode not in GPU_MODES:
        return None

    current_platform = _desktop_platform()
    if not (current_platform.startswith("win") or current_platform == "darwin"):
        return None

    selected_backend = str(runtime_setup.get("selected_backend", "cpu")).lower()
    runtime_action = str(runtime_setup.get("runtime_action", "none")).lower()
    if selected_backend != "cpu" or runtime_action not in GPU_RUNTIME_FATAL_ACTIONS:
        return None
    if runtime_action in {"probe_only", "metal_probe_only"}:
        return None
    if (
        current_platform == "darwin"
        and selected_mode != "gpu"
        and runtime_action not in {"failed", "metal_install_failed"}
    ):
        return None

    reason = runtime_setup.get("fallback_reason") or "unknown runtime bootstrap failure"
    platform_label = "macOS" if current_platform == "darwin" else "Windows"
    policy = _runtime_bootstrap_policy()
    if not policy.bootstrap_supported:
        return (
            f"GPU provisioning failed for desktop {platform_label} launch "
            f"(mode={selected_mode}, action={runtime_action}): {reason}. "
            f"GPU runtime bootstrap is not supported for platform={policy.platform} "
            f"arch={policy.arch}; install a compatible llama-cpp-python runtime manually "
            "or use CPU mode."
        )

    backend_hint = "Metal" if policy.expected_backend == "metal" else "CUDA"
    return (
        f"GPU provisioning failed for desktop {platform_label} launch "
        f"(mode={selected_mode}, action={runtime_action}): {reason}. "
        f"Verify {backend_hint} runtime prerequisites and llama-cpp-python {backend_hint} build support."
    )


def _desktop_platform() -> str:
    return str(getattr(sys, "platform", sys.platform)).lower()


def _desktop_arch() -> str:
    return platform_module.machine().lower().replace("amd64", "x86_64")


def _runtime_bootstrap_policy() -> RuntimeBootstrapPolicy:
    detected_platform = _desktop_platform()
    detected_arch = _desktop_arch()
    if detected_platform.startswith("win") and detected_arch == "x86_64":
        return RuntimeBootstrapPolicy(
            platform=detected_platform,
            arch=detected_arch,
            expected_backend="cuda",
            bootstrap_supported=True,
            bootstrap_reason="Windows x86_64 CUDA bootstrap supported",
        )
    if detected_platform == "darwin" and detected_arch in {"arm64", "aarch64", "x86_64"}:
        return RuntimeBootstrapPolicy(
            platform=detected_platform,
            arch=detected_arch,
            expected_backend="metal",
            bootstrap_supported=True,
            bootstrap_reason="macOS Metal bootstrap supported",
        )
    return RuntimeBootstrapPolicy(
        platform=detected_platform,
        arch=detected_arch,
        expected_backend=None,
        bootstrap_supported=False,
        bootstrap_reason=(
            "desktop runtime bootstrap is not supported for "
            f"platform={detected_platform} arch={detected_arch}; install a compatible "
            "llama-cpp-python runtime manually or use CPU mode"
        ),
    )


def _bootstrap_disabled_reason() -> Optional[str]:
    if os.getenv(DISABLE_BOOTSTRAP_ENV) == "1":
        return f"desktop runtime bootstrap disabled by {DISABLE_BOOTSTRAP_ENV}=1"
    if os.getenv(ENABLE_BOOTSTRAP_ENV) != "1":
        return (
            "desktop runtime bootstrap skipped during normal startup; set "
            f"{ENABLE_BOOTSTRAP_ENV}=1 to allow runtime repair/install"
        )
    return None


def _installed_reexec_action(backend: str) -> str:
    if backend == "metal":
        return "installed_metal_reexec"
    if backend == "cuda":
        return "installed_cuda_reexec"
    return "installed_gpu_reexec"


def _already_supported_action(backend: str) -> str:
    if backend == "metal":
        return "metal_already_supported"
    return "already_supported"


def _install_failure_action(expected_backend: Optional[str]) -> str:
    if expected_backend == "metal":
        return "metal_install_failed"
    return "failed"


def _cpu_fallback_action(expected_backend: Optional[str]) -> str:
    if expected_backend == "metal":
        return "metal_cpu_fallback"
    return "installed_cpu_fallback"


def _resolve_requirements_path(target_root: Path) -> Path:
    candidates = [
        target_root / "requirements.txt",
        target_root / "resources" / "requirements.txt",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _ensure_desktop_llama_runtime_impl(mode: str, *, repo_root: Optional[Path] = None, context_tier: Optional[str] = None) -> Dict[str, str]:
    """Ensure the sidecar interpreter has a GPU-capable runtime when mode prefers GPU."""

    selected_mode = (mode or "auto").strip().lower()
    target_root = _resolve_runtime_root(repo_root=repo_root)
    qwen_64k_required = (context_tier or os.getenv("TOKEN_PLACE_CONTEXT_TIER", "")).strip() == "64k-full"
    before = _probe_runtime(target_root)
    dependency_target, dependency_target_error = _prepend_dependency_target_to_sys_path(target_root)
    dependency_target_text = str(dependency_target) if dependency_target is not None else "unknown"
    requirements_path = _resolve_requirements_path(target_root)
    version_resolution_error = ""
    try:
        required_package_spec, required_version = _required_llama_cpp_spec(requirements_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        required_package_spec = "llama-cpp-python==unknown"
        required_version = "unknown"
        version_resolution_error = (
            "llama_cpp_python_required_version_unresolved: "
            f"{type(exc).__name__}"
        )
    before_version_payload = _version_payload(before, required_version, required_package_spec)
    if _is_repo_local_llama_module(before.llama_module_path, target_root):
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                "llama_cpp import shadowed by repo-local shim "
                f"({before.llama_module_path}); remove repo root from import precedence "
                "or run via desktop sidecar bootstrap so site-packages llama-cpp-python is used"
            ),
            "runtime_action": "shadowed_repo_llama_cpp",
            **_probe_result_payload(before),
            **before_version_payload,
        }

    if selected_mode not in GPU_MODES:
        return {
            "selected_backend": "cpu",
            "fallback_reason": "cpu mode explicitly selected",
            "runtime_action": "skipped",
            **_probe_result_payload(before),
            **before_version_payload,
        }

    last_error = ""
    qwen_64k_version_ok = (
        not qwen_64k_required
        or (
            not version_resolution_error
            and before_version_payload.get("llama_cpp_python_version_match") == "match"
        )
    )

    if before.gpu_offload_supported and before.backend in {"cuda", "metal"}:
        if (not qwen_64k_required or before.yarn_rope_supported) and qwen_64k_version_ok:
            return {
                "selected_backend": before.backend,
                "fallback_reason": "",
                "runtime_action": _already_supported_action(before.backend),
                **_probe_result_payload(before),
                **before_version_payload,
            }
        # Metal/CUDA import and offload are not enough for Qwen 64K. Continue
        # into deterministic reinstall/upgrade so stale packaged sites are repaired.
        last_error = (
            "Qwen 64K requires YaRN/RoPE support in llama-cpp-python; "
            f"installed runtime lacks pinned support; required_version={required_version}; "
            f"version_match={before_version_payload.get('llama_cpp_python_version_match')}; resolver={before.yarn_resolver_source}; "
            f"version={before.llama_cpp_python_version}"
        )

    policy = _runtime_bootstrap_policy()
    expected_backend = policy.expected_backend

    if before.backend == "missing" and not policy.bootstrap_supported:
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                f"desktop model runtime dependency unavailable ({before.error or 'llama_cpp missing'}); "
                f"interpreter={before.interpreter}; import_root={target_root}; "
                "install llama-cpp-python for the desktop runtime before registering with the relay"
            ),
            "runtime_action": "failed",
            **_probe_result_payload(before),
            **before_version_payload,
        }

    if not policy.bootstrap_supported:
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                f"{last_error}; " if last_error else ""
            ) + f"GPU runtime probe only ({before.error or before.backend}); {policy.bootstrap_reason}",
            "runtime_action": "probe_only",
            **_probe_result_payload(before),
            **before_version_payload,
        }

    disabled_reason = _bootstrap_disabled_reason()
    if disabled_reason:
        fatal_version_mismatch = qwen_64k_required and not qwen_64k_version_ok
        if before.backend == "missing":
            return {
                "selected_backend": "cpu",
                "fallback_reason": (
                    f"desktop model runtime dependency unavailable ({before.error or 'llama_cpp missing'}); "
                    f"interpreter={before.interpreter}; import_root={target_root}; "
                    f"prefix={before.prefix}; "
                    "install llama-cpp-python for the desktop runtime before registering with the relay"
                ),
                "runtime_action": "failed",
                **_probe_result_payload(before),
                **before_version_payload,
            }
        action = "metal_probe_only" if expected_backend == "metal" else "probe_only"
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                (f"{last_error}; " if last_error else "")
                + f"{disabled_reason}; platform={policy.platform}; arch={policy.arch}; "
                f"expected_backend={expected_backend}; interpreter={before.interpreter}; "
                f"prefix={before.prefix}"
            ),
            "runtime_action": "version_mismatch_failed" if fatal_version_mismatch else action,
            **_probe_result_payload(before),
            **before_version_payload,
        }

    if qwen_64k_required and version_resolution_error:
        return {
            "selected_backend": "cpu",
            "fallback_reason": version_resolution_error,
            "runtime_action": "version_mismatch_failed",
            **_probe_result_payload(before),
            **before_version_payload,
        }

    install_diagnostics: Dict[str, str] = {}

    if expected_backend == "cuda":
        should_repair, repair_skip_reason = _should_attempt_source_repair()
        if should_repair:
            if dependency_target is None:
                last_error = (
                    "desktop dependency target unavailable; cannot install llama-cpp-python "
                    f"without writing to interpreter prefix; detail={dependency_target_error or 'unknown'}"
                )
            else:
                source_ok, source_log = _run_windows_cuda_source_repair(requirements_path, dependency_target)
                if source_ok:
                    _clear_source_repair_failure()
                    install_diagnostics = _install_diagnostics_payload(
                        source_log, backend="cuda", cmake_args="-DGGML_CUDA=on"
                    )
                    after = _probe_runtime(target_root)
                    after_version_payload = _version_payload(after, required_version, required_package_spec)
                    if after.gpu_offload_supported and after.backend == "cuda":
                        after_version_ok = after_version_payload.get("llama_cpp_python_version_match") == "match"
                        if not qwen_64k_required or (after.yarn_rope_supported and after_version_ok):
                            return {
                                "selected_backend": "cuda",
                                "fallback_reason": "installed CUDA runtime; re-executing sidecar",
                                "runtime_action": "installed_cuda_reexec",
                                **_probe_result_payload(after),
                                **after_version_payload,
                                **install_diagnostics,
                            }
                        last_error = (
                            _qwen_64k_runtime_repair_failed_reason(
                                after,
                                version_match=str(after_version_payload.get('llama_cpp_python_version_match') or 'unknown'),
                            )
                            + f"; required_version={required_version}"
                        )
                        _record_source_repair_failure(last_error)
                    source_detail = _summarize_install_error(source_log)
                    if not last_error:
                        last_error = (
                            "CUDA source reinstall completed but runtime still CPU-only; "
                            "check CUDA toolkit/build tools"
                        )
                    if source_detail and source_detail != "install failed":
                        last_error = f"{last_error}; source repair detail: {source_detail}"
                    _record_source_repair_failure(last_error)
                else:
                    install_diagnostics = _install_diagnostics_payload(
                        source_log, backend="cuda", cmake_args="-DGGML_CUDA=on"
                    )
                    last_error = _summarize_install_error(source_log)
                    _record_source_repair_failure(last_error)
        else:
            last_error = repair_skip_reason

    try:
        plans = llama_cpp_install_plan_fallbacks(
            platform=policy.platform,
            requirements_path=requirements_path,
        )
    except (FileNotFoundError, ValueError):
        plans = _fallback_unpinned_plans(policy.platform)

    for plan in plans:
        if selected_mode == "gpu" and plan.backend == "cpu":
            continue
        if dependency_target is None:
            last_error = (
                "desktop dependency target unavailable; cannot install llama-cpp-python "
                f"without writing to interpreter prefix; detail={dependency_target_error or 'unknown'}"
            )
            break
        env = os.environ.copy()
        env.update(plan.pip_env())
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            [dependency_target_text, existing_pythonpath] if existing_pythonpath else [dependency_target_text]
        )
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--force-reinstall",
            "--target",
            dependency_target_text,
            *plan.pip_install_args(),
            plan.package_spec,
        ]
        timeout_seconds = (
            PIP_SOURCE_BUILD_TIMEOUT_SECONDS if plan.no_binary else PIP_INSTALL_TIMEOUT_SECONDS
        )
        ok, log_output = _run_pip_install(cmd, env, timeout_seconds=timeout_seconds)
        install_diagnostics = _install_diagnostics_payload(
            log_output, backend=plan.backend, cmake_args=plan.cmake_args or ""
        )
        if not ok:
            last_error = _summarize_install_error(log_output)
            continue

        after = _probe_runtime(target_root)
        after_version_payload = _version_payload(after, required_version, required_package_spec)
        plan_satisfied = backend_probe_satisfies_install_plan(plan, after)
        verified_backend = after.gpu_offload_supported and after.backend == plan.backend
        accepted_source_probe = plan_satisfied and after.backend != plan.backend
        if plan.backend in {"cuda", "metal"} and (verified_backend or accepted_source_probe):
            after_version_ok = after_version_payload.get("llama_cpp_python_version_match") == "match"
            if qwen_64k_required and (not after.yarn_rope_supported or not after_version_ok):
                last_error = (
                    _qwen_64k_runtime_repair_failed_reason(
                        after,
                        version_match=str(after_version_payload.get('llama_cpp_python_version_match') or 'unknown'),
                    )
                    + f"; required_version={required_version}"
                )
                continue
            if verified_backend:
                reason = f"installed {after.backend.upper()} runtime; re-executing sidecar"
                selected_backend = plan.backend
            else:
                reason = (
                    f"installed {plan.backend.upper()} runtime from source; follow-up probe imported "
                    f"llama_cpp from {after.llama_module_path}; re-executing sidecar for hardware probe"
                )
                selected_backend = "cpu"
                if selected_mode == "gpu":
                    reason = (
                        f"{plan.backend.upper()} source install completed but explicit GPU mode "
                        "requires the follow-up probe to report GPU offload before re-exec; "
                        f"backend={after.backend} gpu_offload_supported={after.gpu_offload_supported}; "
                        f"llama_module_path={after.llama_module_path}"
                    )
                    return {
                        "selected_backend": "cpu",
                        "fallback_reason": reason,
                        "runtime_action": _install_failure_action(expected_backend),
                        **_probe_result_payload(after),
                        **after_version_payload,
                        **install_diagnostics,
                    }
            return {
                "selected_backend": selected_backend,
                "fallback_reason": reason,
                "runtime_action": _installed_reexec_action(plan.backend),
                **_probe_result_payload(after),
                **after_version_payload,
                **install_diagnostics,
            }

        if plan.backend in {"cuda", "metal"}:
            last_error = (
                f"{plan.backend.upper()} install completed but follow-up probe reported "
                f"backend={after.backend} gpu_offload_supported={after.gpu_offload_supported}; "
                f"llama_module_path={after.llama_module_path}; dependency_target={dependency_target_text}; "
                f"pip={after.pip_version}; cmake_args={plan.cmake_args or 'none'}"
            )
            if plan.backend == "metal" and selected_mode == "gpu":
                return {
                    "selected_backend": "cpu",
                    "fallback_reason": (
                        f"Metal runtime install completed but follow-up probe did not report "
                        f"Metal GPU offload; backend={after.backend} "
                        f"gpu_offload_supported={after.gpu_offload_supported}; "
                        f"llama_module_path={after.llama_module_path}; "
                        f"dependency_target={dependency_target_text}; pip={after.pip_version}"
                    ),
                    "runtime_action": _install_failure_action(expected_backend),
                    **_probe_result_payload(after),
                    **after_version_payload,
                    **install_diagnostics,
                }
            if plan.backend == "metal":
                continue

        if plan.backend == "cpu":
            if not _runtime_probe_is_importable(after):
                last_error = (
                    "CPU runtime install completed but follow-up probe could not import llama_cpp; "
                    f"backend={after.backend}; error={after.error or 'unknown'}; "
                    f"llama_module_path={after.llama_module_path}; "
                    f"dependency_target={dependency_target_text}; pip={after.pip_version}"
                )
                continue
            reason = (
                f"{(expected_backend or 'GPU').upper()} runtime unavailable after bootstrap"
                f" ({last_error or before.error or 'probe did not report GPU offload'}); using CPU runtime; "
                f"dependency_target={dependency_target_text}; llama_module_path={after.llama_module_path}; "
                f"pip={after.pip_version}"
            )
            return {
                "selected_backend": "cpu",
                "fallback_reason": reason,
                "runtime_action": _cpu_fallback_action(expected_backend),
                **_probe_result_payload(after),
                **after_version_payload,
                **install_diagnostics,
            }

    reason = last_error or before.error or "unable to install a GPU-capable runtime"
    if expected_backend == "metal":
        reason = (
            f"Metal runtime install failed ({reason}); interpreter={before.interpreter}; "
            f"python_version={before.python_version}; prefix={before.prefix}; "
            f"base_prefix={before.base_prefix}; dependency_target={dependency_target_text}; "
            f"pip={before.pip_version}; llama_module_path={before.llama_module_path}"
        )
    return {
        "selected_backend": "cpu",
        "fallback_reason": reason,
        "runtime_action": _install_failure_action(expected_backend),
        **_probe_result_payload(before),
        **before_version_payload,
        **install_diagnostics,
    }


def _record_desktop_runtime_probe(result: Dict[str, Any]) -> Dict[str, Any]:
    """Expose the successful setup probe to later diagnostics in this process."""

    try:
        os.environ[RUNTIME_PROBE_ENV] = json.dumps(result)
    except (TypeError, ValueError):
        os.environ.pop(RUNTIME_PROBE_ENV, None)
        return result
    public_result = dict(result)
    for key in (
        "yarn_rope_supported",
        "rope_scaling_type_supported",
        "yarn_ext_factor_supported",
        "rope_freq_scale_supported",
        "yarn_orig_ctx_supported",
    ):
        if isinstance(public_result.get(key), bool):
            public_result[key] = str(public_result[key]).lower()
    return public_result


def ensure_desktop_llama_runtime(mode: str, *, repo_root: Optional[Path] = None, context_tier: Optional[str] = None) -> Dict[str, Any]:
    """Ensure the sidecar interpreter has a GPU-capable runtime when mode prefers GPU."""

    return _record_desktop_runtime_probe(
        _ensure_desktop_llama_runtime_impl(mode, repo_root=repo_root, context_tier=context_tier)
    )


def _resolve_desktop_requirements_path(repo_root: Path) -> Path:
    candidates = [
        repo_root / "python" / "requirements_desktop_runtime.txt",
        repo_root / "resources" / "python" / "requirements_desktop_runtime.txt",
        Path(__file__).resolve().parent / "requirements_desktop_runtime.txt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _desktop_dependency_target(runtime_root: Path) -> Path:
    return runtime_root / ".token_place_desktop_site"


def _is_writable_directory(candidate: Path) -> tuple[bool, Optional[str]]:
    probe = candidate / ".token_place_write_probe"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, None
    except OSError as exc:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(exc)


def _resolve_desktop_dependency_target(runtime_root: Path) -> tuple[Optional[Path], Optional[str]]:
    dependency_target = os.environ.get("TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET", "").strip()
    preferred_targets = []
    if dependency_target:
        preferred_targets.append(("env_override", Path(dependency_target)))
    preferred_targets.extend(
        [
            ("runtime_root", _desktop_dependency_target(runtime_root)),
            ("home_fallback", Path.home() / ".token_place_desktop_site"),
        ]
    )
    errors: list[str] = []
    for label, candidate in preferred_targets:
        ok, error = _is_writable_directory(candidate)
        if ok:
            return candidate, None
        errors.append(f"{label}={candidate}: {error}")
    return None, "; ".join(errors) if errors else "no writable install target"


def ensure_desktop_python_dependencies(*, repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Ensure baseline desktop bridge Python dependencies are importable."""

    root = _resolve_runtime_root(repo_root=repo_root)
    requirements_path = _resolve_desktop_requirements_path(root)

    required_modules = ("psutil", "requests", "dotenv", "cryptography", "packaging")
    missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
    if not missing:
        return {"ok": "true", "action": "already_satisfied", "missing": ""}

    if not requirements_path.is_file():
        return {
            "ok": "false",
            "action": "requirements_missing",
            "missing": ",".join(missing),
            "interpreter": sys.executable,
            "import_root": str(root),
            "requirements": str(requirements_path),
        }

    env = os.environ.copy()
    target_dir, target_error = _resolve_desktop_dependency_target(root)
    if target_dir is None:
        return {
            "ok": "false",
            "action": "install_target_unavailable",
            "missing": ",".join(missing),
            "interpreter": sys.executable,
            "import_root": str(root),
            "requirements": str(requirements_path),
            "detail": target_error or "unable to create desktop dependency install target",
        }
    target_dir_str = str(target_dir)
    if target_dir_str not in sys.path:
        sys.path.insert(0, target_dir_str)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--target",
        target_dir_str,
        "-r",
        str(requirements_path),
    ]
    ok, output = _run_pip_install(cmd, env)
    if not ok:
        return {
            "ok": "false",
            "action": "install_failed",
            "missing": ",".join(missing),
            "interpreter": sys.executable,
            "import_root": str(root),
            "requirements": str(requirements_path),
            "dependency_target": target_dir_str,
            "detail": _summarize_install_error(output),
        }

    importlib.invalidate_caches()
    missing_after = [name for name in required_modules if importlib.util.find_spec(name) is None]
    return {
        "ok": "true" if not missing_after else "false",
        "action": "installed" if not missing_after else "post_install_missing",
        "missing": ",".join(missing_after),
        "interpreter": sys.executable,
        "import_root": str(root),
        "requirements": str(requirements_path),
        "dependency_target": target_dir_str,
    }
def _fallback_unpinned_plans(platform: str) -> list[LlamaCppInstallPlan]:
    detected_platform = platform.lower()
    if detected_platform.startswith("win"):
        return [
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="cuda",
                package_spec="llama-cpp-python",
                cmake_args="-DGGML_CUDA=on",
                force_cmake=True,
                index_url=LLAMA_CPP_PYPI_INDEX_URL,
                only_binary=False,
                no_binary=True,
            ),
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="cpu",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url=LLAMA_CPP_PYPI_INDEX_URL,
                extra_index_url=LLAMA_CPP_CPU_WHEEL_INDEX_URL,
                only_binary=True,
                no_binary=False,
            ),
        ]

    if detected_platform == "darwin":
        return [
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="metal",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url=LLAMA_CPP_PYPI_INDEX_URL,
                extra_index_url=LLAMA_CPP_METAL_WHEEL_INDEX_URL,
                only_binary=True,
                no_binary=False,
            ),
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="metal",
                package_spec="llama-cpp-python",
                cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
                force_cmake=True,
                index_url=LLAMA_CPP_PYPI_INDEX_URL,
                only_binary=False,
                no_binary=True,
            ),
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="cpu",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url=LLAMA_CPP_PYPI_INDEX_URL,
                extra_index_url=LLAMA_CPP_CPU_WHEEL_INDEX_URL,
                only_binary=True,
                no_binary=False,
            ),
        ]

    return [
        LlamaCppInstallPlan(
            platform=detected_platform,
            backend="cpu",
            package_spec="llama-cpp-python",
            cmake_args=None,
            force_cmake=False,
            index_url=None,
            only_binary=False,
            no_binary=False,
        )
    ]
